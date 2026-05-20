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
    SubmissionScoring, RuleResult, RetrievedChunk, SignalSource
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
                temperature=0,  # deterministic — no variation between runs
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
                temperature=0,  # deterministic — no variation between runs
                max_tokens=4096,
            )

            choice = response.choices[0]

            if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
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

    def _validate_narrative(self, narrative: str, company_name: str, rule_detail: str) -> str:
        """
        Validate LLM narrative — if too short or missing company name,
        fall back to the rule detail from the rules engine.
        """
        if not narrative or len(narrative.strip()) < 40:
            log.warning("narrative_too_short", length=len(narrative) if narrative else 0)
            return rule_detail

        # Check company name is mentioned (use first word of company name)
        first_word = company_name.split()[0].lower() if company_name else ""
        if first_word and first_word not in narrative.lower():
            log.warning("narrative_missing_company", company=company_name)
            # Still use it — LLM may have used a pronoun — just log

        return narrative.strip()

    def _validate_sources(
        self,
        raw_sources: list,
        valid_uploaded: set,
        valid_chunks: set,
    ) -> list[SignalSource]:
        """
        Validate sources — only accept filenames we actually have.
        Rejects invented filenames from LLM hallucination.
        """
        valid_all = valid_uploaded | valid_chunks
        # Build lowercase → canonical lookup for case-insensitive fallback
        lower_to_canonical = {v.lower(): v for v in valid_all}

        log.info("validating_sources",
            raw_count=len(raw_sources),
            raw_docs=[s.get("doc", "") for s in raw_sources],
            valid_uploaded=list(valid_uploaded),
            valid_chunks=list(valid_chunks),
        )

        validated = []
        for s in raw_sources:
            doc = s.get("doc", "")
            if not doc:
                continue
            # Exact match first
            if doc in valid_all:
                canonical = doc
            else:
                # Case-insensitive fallback — handles minor LLM capitalisation drift
                canonical = lower_to_canonical.get(doc.lower())
            if canonical:
                validated.append(SignalSource(
                    doc=canonical,
                    page=s.get("page"),
                    section=s.get("section", ""),
                ))
            else:
                log.warning("source_invalid_rejected", doc=doc,
                    valid_uploaded=list(valid_uploaded),
                    valid_chunks=list(valid_chunks))
        return validated

    # ── Evaluate ─────────────────────────────────────

    async def run(self, state: PipelineState) -> PipelineState:
        state.status = "scoring"
        state.current_step = "evaluation"

        analytics = state.form_data.get("_analytics", {})
        rules_result = state.form_data.get("_rules_result", {})

        analytics_summary = json.dumps(rules_result.get("analytics_summary", {}), indent=2, default=str)
        triggers_text = "\n".join(
            f"- [{t['rule']}] (rule_id: trigger_{i}): {t['detail']}"
            for i, t in enumerate(rules_result.get("triggers", []))
        )
        overrides_text = "\n".join(
            f"- [{o['rule']}] (rule_id: override_{i}): {o['detail']}"
            for i, o in enumerate(rules_result.get("overrides", []))
        )
        positives_text = "\n".join(
            f"- [{p['rule'] if isinstance(p, dict) else p}] (rule_id: positive_{i}): {p['detail'] if isinstance(p, dict) else p}"
            for i, p in enumerate(rules_result.get("positives", []))
        )

        uploaded_docs = "\n".join(f"- {doc.filename}" for doc in state.documents if doc.filename)

        # Build uploaded docs set — chunk set built after tool calls complete
        valid_uploaded = {doc.filename for doc in state.documents if doc.filename}

        # List initial chunk source names explicitly so the LLM can cite them precisely.
        # Tool-retrieved chunks will add more names (shown in [brackets] in tool results).
        initial_chunk_sources = sorted(
            {c.source_doc for c in state.retrieved_chunks if c.source_doc} - valid_uploaded
        )
        ref_sources_text = "\n".join(f"- {s}" for s in initial_chunk_sources) or "(none pre-loaded)"

        company_name = state.extraction.company.name or "The insured"

        system = SCORING_SYSTEM + """

IMPORTANT: The rules engine has ALREADY computed scores. Do NOT recalculate or override:
- Appetite score, status, winnability, priority, referral decision are FINAL.
- Your job is ONLY to:
  1. Write a narrative explanation for EACH signal (trigger, override, positive)
  2. Assign a queue
  3. Generate broker questions

You have search tools to look up policy language for the narratives and broker questions."""

        # Pull canonical facts object — single source of truth
        uw = state.form_data.get("_uw_facts", {})

        # Data integrity block — conflicts become mandatory first broker questions
        data_conflicts   = uw.get("data_conflicts", [])
        unverified_count = uw.get("unverified_claim_count", 0)
        has_loss_run     = uw.get("has_loss_run", True)

        if data_conflicts:
            conflict_lines = "\n".join(f"  {i+1}. {c}" for i, c in enumerate(data_conflicts))
            conflicts_block = f"""
## ⚠ DATA INTEGRITY CONFLICTS — MANDATORY BROKER QUESTIONS
The following mismatches were detected between the submitted documents.
You MUST include a specific broker question for EACH conflict below as your FIRST questions.
Do NOT skip or merge these — each conflict needs its own question:
{conflict_lines}
"""
        elif not has_loss_run:
            conflicts_block = """
## ⚠ NO LOSS RUN PROVIDED
No official carrier loss run was submitted. All claim data is from broker/ACORD forms only.
You MUST include this as broker question #1:
  "No official carrier loss run was provided. Please submit a 5-year carrier-issued loss run to complete underwriting review."
"""
        elif unverified_count > 0:
            conflicts_block = f"""
## ⚠ UNVERIFIED CLAIMS
{unverified_count} broker-reported claim(s) could not be matched to the official loss run.
You MUST ask the broker to reconcile these in your broker questions.
"""
        else:
            conflicts_block = ""

        loss_source_note = (
            f"(verified from loss run — {unverified_count} additional broker-reported claim(s) excluded from metrics)"
            if unverified_count > 0 else
            "(verified from loss run)" if has_loss_run else
            "(broker-reported only — no loss run submitted)"
        )

        uw_facts_block = (
            f"## ══ GROUNDED FACTS — USE THESE EXACT VALUES IN ALL NARRATIVES ══\n"
            f"Company: {uw.get('company_name', company_name)}\n"
            f"Loss Ratio: {uw.get('loss_ratio', 'N/A')}% {loss_source_note}\n"
            f"Loss Ratio (ex. largest): {uw.get('loss_ratio_ex_largest', 'N/A')}%\n"
            f"Total Claims: {uw.get('total_claims', 0)} {loss_source_note}\n"
            f"Total Incurred: ${uw.get('total_incurred', 0):,.0f}\n"
            f"Largest Single Claim: ${uw.get('largest_claim_amount', 0):,.0f}"
            f" ({uw.get('largest_claim_type', '')} on {uw.get('largest_claim_date', '')})\n"
            f"Open Claims: {uw.get('open_claims', 0)}\n"
            f"Causation: {'SYSTEMIC' if uw.get('systemic') else 'ISOLATED - do NOT use word systemic'}\n"
            f"Prior Non-Renewal: {'YES - by ' + ', '.join(uw.get('prior_nonrenewal_carriers', [])) if uw.get('prior_nonrenewal') else 'NO'}\n"
            f"Current Carrier(s): {', '.join(uw.get('current_carriers', [])) or 'Not specified'}\n"
            f"{conflicts_block}"
        )

        user_prompt = f"""{uw_facts_block}

## Pre-Computed Analytics
{analytics_summary}

## Rules Engine Decision
Score: {rules_result.get('score', 3)}/5
Status: {rules_result.get('status', 'review')}
Reasoning: {rules_result.get('reasoning', '')}

## Signals to Narrate
### Triggers (negative)
{triggers_text or 'None'}

### Overrides (mitigating)
{overrides_text or 'None'}

### Positives (green)
{positives_text or 'None'}

## Extracted Company Data
{state.extraction.model_dump_json(indent=2)}

## Retrieved Policy Evidence
{self._format_initial_evidence(state)}

## Uploaded Source Documents (submission files — cite for claim counts, loss figures, company facts)
{uploaded_docs}

## Available Policy Reference Sources (appetite guide chunks — cite for policy thresholds, rules, eligibility)
{ref_sources_text}
Any additional reference sources retrieved during tool calls will appear in [brackets] in those results — use those exact names too.

---

Your task:

1. For EACH signal listed above, write a 2-3 sentence narrative that:
   - Names the company specifically ({company_name}) — not "the insured" or "the company"
   - Uses actual numbers from the signal detail field (dollar amounts, dates, counts)
   - References the relevant policy/appetite guide language from retrieved evidence
   - Explains WHY this is a positive/negative/mitigating signal
   - CRITICAL: Use ONLY the exact numbers already in the signal detail — do NOT recalculate

2. broker_questions — write 3-5 questions that:
   - Reference specific facts from THIS submission (actual claim numbers, dollar amounts, property addresses, coverage gaps)
   - NEVER write vague questions like "confirm adequacy of X"
   - Each question must reference a specific finding from the extracted data

3. Assign recommended_queue.

Return ONLY this JSON — no extra text:
{{
  "recommended_queue": "<preferred-commercial|standard-commercial|specialty-commercial|large-account|referral-senior-uw|decline-review>",
  "broker_questions": ["q1", "q2", "q3"],
  "signal_narratives": {{
{chr(10).join(f'    "{r.rule_id}": {{"narrative": "2-3 sentence explanation.", "sources": [{{"doc": "exact_filename.pdf", "page": null, "section": ""}}]}}'  for r in state.appetite.rule_results)}
  }}
}}

CRITICAL: You MUST write a narrative for ALL {len(state.appetite.rule_results)} signals.
Required rule_ids — do not skip any: {[r.rule_id for r in state.appetite.rule_results]}

Rules:
- Key must exactly match the rule_id: trigger_0, trigger_1, override_0, positive_0, etc.
- narrative must be at least 2 sentences and must name {company_name}
- Use exact numbers from the signal detail — do NOT invent or recalculate
- sources: list ONLY the specific uploaded docs or retrieved policy chunks that directly support THIS signal's narrative
  - Use exact filenames from "Uploaded Source Documents" or exact source names from "Retrieved Policy Evidence"
  - Do NOT list every uploaded doc on every signal — only files containing evidence for this specific signal
  - For submission-data signals (loss ratio, claims count) cite the loss run / ACORD upload
  - For policy/appetite signals cite the retrieved chunk source name
  - page: use the page number from the retrieved evidence, or null for uploaded submission docs"""

        try:
            result, new_chunks = await self._run_with_tools(system, user_prompt, state)
            self._accumulate_chunks(state, new_chunks)

            # Build valid_chunks AFTER accumulate — now includes retriever + tool chunks
            # This is the complete set of real source_doc values from Pinecone metadata
            valid_chunks = {c.source_doc for c in state.retrieved_chunks if c.source_doc}

            if isinstance(result, dict) and "parse_error" not in result:
                state.scoring.recommended_queue = result.get("recommended_queue", "general")
                state.scoring.broker_questions = result.get("broker_questions", [])

                signal_narratives = result.get("signal_narratives", {})
                narratives_applied = 0

                # Fallback upload sources — used only when LLM returns no valid sources
                fallback_uploaded = [
                    SignalSource(doc=doc.filename, page=None, section="")
                    for doc in state.documents
                    if doc.filename
                ]

                # Prefer tool-retrieved chunks (fetched specifically for this submission)
                # over initial RAG chunks; within each group rank by score descending.
                def _chunk_sort_key(c):
                    tier = 0 if c.match_type == "tool_retrieval" else 1
                    return (tier, -c.score)

                ref_chunks_ranked = sorted(
                    [c for c in state.retrieved_chunks if c.source_doc and c.source_doc not in valid_uploaded],
                    key=_chunk_sort_key,
                )

                for r in state.appetite.rule_results:
                    if r.rule_id in signal_narratives:
                        sig = signal_narratives[r.rule_id]

                        # Per-signal sources — validate against real filenames only
                        raw_sources = sig.get("sources", [])
                        if raw_sources:
                            r.sources = self._validate_sources(
                                raw_sources, valid_uploaded, valid_chunks
                            )

                        # If LLM gave no valid sources at all, use uploaded submission docs
                        if not r.sources:
                            r.sources = fallback_uploaded[:2]

                        # If no chunk (policy guide) source included, find the best-matching chunk.
                        # Require at least MIN_OVERLAP shared keywords — avoids attaching
                        # a completely unrelated chunk (e.g. terrorism clauses for a loss ratio signal).
                        MIN_OVERLAP = 2
                        has_chunk_source = any(s.doc not in valid_uploaded for s in r.sources)
                        if not has_chunk_source and ref_chunks_ranked:
                            signal_keywords = set((r.rule_name + " " + r.reason).lower().split())
                            scored = [
                                (len(signal_keywords & set((c.section + " " + c.text[:300]).lower().split())), c)
                                for c in ref_chunks_ranked[:20]
                            ]
                            best_overlap, best_chunk = max(scored, key=lambda x: x[0])
                            if best_overlap >= MIN_OVERLAP:
                                r.sources.append(SignalSource(
                                    doc=best_chunk.source_doc,
                                    page=best_chunk.page,
                                    section=best_chunk.section or "",
                                ))
                                log.info("chunk_source_fallback_applied",
                                    rule_id=r.rule_id,
                                    chunk_source=best_chunk.source_doc,
                                    chunk_page=best_chunk.page,
                                    keyword_overlap=best_overlap,
                                )
                            else:
                                log.warning("chunk_source_fallback_skipped",
                                    rule_id=r.rule_id,
                                    best_overlap=best_overlap,
                                    min_required=MIN_OVERLAP,
                                )

                        # Validate narrative
                        raw_narrative = sig.get("narrative", "").strip()
                        validated_narrative = self._validate_narrative(
                            raw_narrative, company_name, r.reason
                        )
                        if validated_narrative:
                            r.narrative = validated_narrative
                            narratives_applied += 1
                    else:
                        log.warning("narrative_missing_for_signal", rule_id=r.rule_id)
                        r.sources = fallback_uploaded[:2]

                log.info("evaluation_done",
                    queue=state.scoring.recommended_queue,
                    broker_questions=len(state.scoring.broker_questions),
                    narratives_written=len(signal_narratives),
                    narratives_applied=narratives_applied,
                    signals_total=len(state.appetite.rule_results),
                )
            else:
                state.scoring.recommended_queue = "referral-senior-uw" if state.scoring.referral_required else "standard-commercial"
                state.errors.append("Evaluation returned invalid JSON — using defaults")

        except Exception as e:
            log.error("evaluation_failed", error=str(e))
            state.scoring.recommended_queue = "referral-senior-uw" if state.scoring.referral_required else "standard-commercial"
            state.errors.append(f"Evaluation failed: {str(e)}")

        return state