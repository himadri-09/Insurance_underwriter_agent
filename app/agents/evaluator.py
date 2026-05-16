"""
Agent: Appetite Evaluator + Scorer (with tool-calling RAG)

Auto-routes tool-calling to the correct provider based on reasoning_model name:
  claude-*  → Anthropic tool calling
  gpt-* / azure/* → Azure OpenAI function calling
  anything else → fallback to non-tool-calling reasoning via LLMService
"""

import json
import structlog
from app.models.schemas import (
    PipelineState, AppetiteAssessment, AppetiteStatus,
    SubmissionScoring, RuleResult, RetrievedChunk
)
from app.core.config import get_settings
from app.agents.tools import REASONING_TOOLS
from app.agents.tool_executor import ToolExecutor
from app.agents.prompts import (
    APPETITE_ASSESSMENT_SYSTEM, APPETITE_ASSESSMENT_USER,
    SCORING_SYSTEM, SCORING_USER,
)

log = structlog.get_logger()

MAX_TOOL_ROUNDS = 5

def _detect_provider(model: str) -> str:
    if model.startswith("claude"):
        return "anthropic"
    elif model.startswith("gpt") or model.startswith("azure/"):
        return "azure"
    else:
        return "openrouter"

class EvaluatorAgent:
    def __init__(self):
        self.settings = get_settings()
        self.model = self.settings.reasoning_model
        self.provider = _detect_provider(self.model)
        self.tool_executor = ToolExecutor()
        self._client = None

    @property
    def client(self):
        """Lazy init the right client."""
        if self._client is None:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
            elif self.provider == "azure":
                from openai import AzureOpenAI
                self._client = AzureOpenAI(
                    azure_endpoint=self.settings.azure_openai_endpoint,
                    api_key=self.settings.azure_openai_api_key,
                    api_version=self.settings.azure_openai_api_version,
                )
            elif self.provider == "openrouter":
                # OpenRouter doesn't support tool calling well — fallback to non-tool
                self._client = None
        return self._client

    def _azure_deployment(self) -> str:
        m = self.model
        return m.split("/", 1)[1] if m.lower().startswith("azure/") else m

    # ── Tool-calling loop (provider-agnostic) ─────────

    async def _run_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        state: PipelineState,
    ) -> tuple[dict, list[RetrievedChunk]]:
        if self.provider == "anthropic":
            return await self._anthropic_tool_loop(system_prompt, user_prompt)
        elif self.provider == "azure":
            return await self._azure_tool_loop(system_prompt, user_prompt)
        else:
            # Fallback: no tool calling, just reason with all evidence baked in
            return await self._fallback_no_tools(system_prompt, user_prompt)

    # ── Anthropic tool-calling loop ───────────────────

    async def _anthropic_tool_loop(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[dict, list[RetrievedChunk]]:
        messages = [{"role": "user", "content": user_prompt}]
        all_new_chunks: list[RetrievedChunk] = []

        for round_num in range(MAX_TOOL_ROUNDS + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                messages=messages,
                tools=REASONING_TOOLS,
                temperature=0.2,
            )

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        log.info("tool_called", tool=block.name, round=round_num)
                        result_text, new_chunks = await self.tool_executor.execute(
                            block.name, block.input
                        )
                        all_new_chunks.extend(new_chunks)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                final_text = "".join(
                    block.text for block in response.content if hasattr(block, "text")
                )
                log.info("reasoning_complete", provider="anthropic", rounds=round_num + 1)
                return self._parse_json_safe(final_text), all_new_chunks

        return {"error": "max_tool_rounds_reached"}, all_new_chunks

    # ── Azure OpenAI tool-calling loop ────────────────

    async def _azure_tool_loop(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[dict, list[RetrievedChunk]]:
        # Convert our tool schemas to OpenAI function format
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in REASONING_TOOLS
        ]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        all_new_chunks: list[RetrievedChunk] = []
        deployment = self._azure_deployment()

        for round_num in range(MAX_TOOL_ROUNDS + 1):
            response = self.client.chat.completions.create(
                model=deployment,
                messages=messages,
                tools=openai_tools,
                temperature=0.2,
                max_tokens=4096,
            )

            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                # Add assistant message with tool calls
                messages.append(choice.message)

                for tc in choice.message.tool_calls:
                    log.info("tool_called", tool=tc.function.name, round=round_num)
                    tool_input = json.loads(tc.function.arguments)
                    result_text, new_chunks = await self.tool_executor.execute(
                        tc.function.name, tool_input
                    )
                    all_new_chunks.extend(new_chunks)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    })
            else:
                final_text = choice.message.content or ""
                log.info("reasoning_complete", provider="azure", rounds=round_num + 1)
                return self._parse_json_safe(final_text), all_new_chunks

        return {"error": "max_tool_rounds_reached"}, all_new_chunks

    # ── Fallback: no tool calling ─────────────────────

    async def _fallback_no_tools(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[dict, list[RetrievedChunk]]:
        """For providers without tool calling — just reason with what we have."""
        from app.services.llm_service import LLMService
        llm = LLMService()
        result = await llm.reason(system_prompt, user_prompt, response_format="json")
        return (result if isinstance(result, dict) else {"raw_text": result}), []

    # ── JSON parsing ──────────────────────────────────

    def _parse_json_safe(self, text: str) -> dict:
        cleaned = text.strip()
        if "```json" in cleaned:
            start = cleaned.index("```json") + 7
            end = cleaned.index("```", start)
            cleaned = cleaned[start:end].strip()
        elif "```" in cleaned:
            start = cleaned.index("```") + 3
            end = cleaned.index("```", start)
            cleaned = cleaned[start:end].strip()

        if "{" in cleaned:
            start = cleaned.index("{")
            depth = 0
            for i, ch in enumerate(cleaned[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        cleaned = cleaned[start:i + 1]
                        break
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            log.error("json_parse_failed", raw=text[:300])
            return {"raw_text": text, "parse_error": True}

    # ── Evidence formatting ───────────────────────────

    def _format_initial_evidence(self, state: PipelineState) -> str:
        if not state.retrieved_chunks:
            return "(No initial evidence. Use search tools to find relevant appetite guide sections.)"
        lines = []
        for i, chunk in enumerate(state.retrieved_chunks, 1):
            source = f"[{chunk.source_doc}"
            if chunk.page:
                source += f", p.{chunk.page}"
            if chunk.section:
                source += f", §{chunk.section}"
            source += "]"
            lines.append(f"Evidence {i} {source}:\n{chunk.text}\n")
        return "\n".join(lines)

    def _accumulate_chunks(self, state: PipelineState, new_chunks: list[RetrievedChunk]):
        existing_ids = {c.chunk_id for c in state.retrieved_chunks}
        for chunk in new_chunks:
            if chunk.chunk_id not in existing_ids:
                state.retrieved_chunks.append(chunk)
                existing_ids.add(chunk.chunk_id)

    # ── Evaluate: LLM adds narrative + broker questions to pre-computed results ──

    async def run(self, state: PipelineState) -> PipelineState:
        """
        The analytics_service + rules_engine already computed:
          - appetite score, status, triggers, overrides
          - winnability, priority
          - referral decision

        The evaluator LLM now ONLY:
          1. Searches RAG for supporting policy language
          2. Generates broker follow-up questions
          3. Assigns queue
          4. Adds narrative context to the pre-computed decision
          5. Does NOT recalculate scores, loss ratios, or referral decisions
        """
        state.status = "scoring"
        state.current_step = "evaluation"

        # Get pre-computed results from analyze step
        analytics = state.form_data.get("_analytics", {})
        rules_result = state.form_data.get("_rules_result", {})

        import json
        analytics_summary = json.dumps(rules_result.get("analytics_summary", {}), indent=2, default=str)
        triggers_text = "\n".join(f"- {t['rule']}: {t['detail']}" for t in rules_result.get("triggers", []))
        overrides_text = "\n".join(f"- {o['rule']}: {o['detail']}" for o in rules_result.get("overrides", []))
        positives_text = "\n".join(f"- {p}" for p in rules_result.get("positives", []))

        system = SCORING_SYSTEM + """

IMPORTANT: The rules engine has ALREADY computed the following. Do NOT recalculate or override these:
- Appetite score, status, winnability, priority, and referral decision are FINAL from the rules engine.
- You must NOT change the score, winnability, priority, or referral_required values.
- Your job is ONLY to: assign a queue, generate broker questions, and add context.

You have search tools to look up policy language if needed for the broker questions."""

        user_prompt = f"""## Pre-Computed Analytics (from rules engine — DO NOT RECALCULATE)
{analytics_summary}

## Rules Engine Decision
Score: {rules_result.get('score', 3)}/5
Status: {rules_result.get('status', 'review')}
Winnability: {rules_result.get('winnability', 0.5)}
Priority: {rules_result.get('priority', 0.5)}
Reasoning: {rules_result.get('reasoning', '')}

## Triggers Fired
{triggers_text or 'None'}

## Mitigating Factors / Overrides
{overrides_text or 'None'}

## Positive Signals
{positives_text or 'None'}

## Extracted Submission Facts
{state.extraction.model_dump_json(indent=2)}

## Retrieved Evidence
{self._format_initial_evidence(state)}

Based on the ABOVE pre-computed decision, provide ONLY:
1. recommended_queue — pick from: preferred-commercial, standard-commercial, specialty-commercial, large-account, referral-senior-uw, decline-review
2. broker_questions — 3-5 specific questions for the broker based on missing info and risk concerns
3. coverage_recommendations — any suggested coverage modifications

Return JSON:
{{
  "recommended_queue": "<queue>",
  "broker_questions": ["q1", "q2", "q3"],
  "coverage_recommendations": ["rec1", "rec2"],
  "narrative_context": "brief explanation of the decision for the underwriter"
}}"""

        try:
            result, new_chunks = await self._run_with_tools(system, user_prompt, state)
            self._accumulate_chunks(state, new_chunks)

            if isinstance(result, dict) and "parse_error" not in result:
                # Keep pre-computed scores from rules engine, only add LLM outputs
                state.scoring.recommended_queue = result.get("recommended_queue", "general")
                state.scoring.broker_questions = result.get("broker_questions", [])

                log.info("evaluation_done",
                    queue=state.scoring.recommended_queue,
                    broker_questions=len(state.scoring.broker_questions),
                )
            else:
                state.scoring.recommended_queue = "referral-senior-uw" if state.scoring.referral_required else "standard-commercial"
                state.errors.append("Evaluation returned invalid JSON — using defaults")

        except Exception as e:
            log.error("evaluation_failed", error=str(e))
            state.scoring.recommended_queue = "referral-senior-uw" if state.scoring.referral_required else "standard-commercial"
            state.errors.append(f"Evaluation failed: {str(e)}")

        return state