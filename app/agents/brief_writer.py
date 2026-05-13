"""
Agent: Brief Writer — Commercial Insurance

Generates the 1-page risk brief using pre-computed analytics from the rules engine.
The LLM writes narrative ONLY — all numbers come from deterministic calculations.
"""

import re
import json
import structlog
from app.models.schemas import PipelineState, Citation
from app.services.llm_service import LLMService
from app.agents.prompts import BRIEF_SYSTEM, BRIEF_USER

log = structlog.get_logger()


class BriefWriterAgent:
    def __init__(self):
        self.llm = LLMService()

    def _format_evidence(self, state: PipelineState) -> str:
        if not state.retrieved_chunks:
            return "(No policy/guideline evidence available)"
        lines = []
        for i, chunk in enumerate(state.retrieved_chunks[:10], 1):
            source = f"[{chunk.source_doc}"
            if chunk.page:
                source += f", p.{chunk.page}"
            if chunk.section:
                source += f", {chunk.section}"
            source += "]"
            lines.append(f"Evidence {i} {source}:\n{chunk.text}\n")
        return "\n".join(lines)

    def _format_analytics(self, state: PipelineState) -> str:
        """Format pre-computed analytics as structured text for the brief writer."""
        rules_result = state.form_data.get("_rules_result", {})
        analytics = state.form_data.get("_analytics", {})

        if not rules_result:
            return "(No pre-computed analytics available)"

        loss = analytics.get("loss", {})
        biz = analytics.get("business", {})
        carrier = analytics.get("carrier", {})
        sub = analytics.get("subrogation", {})
        supp = analytics.get("suppression", {})
        caus = analytics.get("causation", {})

        sections = []

        sections.append("## PRE-COMPUTED ANALYTICS (use these exact numbers, do NOT recalculate)")
        sections.append(f"Loss Ratio: {loss.get('loss_ratio_pct', 'N/A')}%")
        sections.append(f"Loss Ratio Excluding Largest Claim: {loss.get('loss_ratio_ex_largest_pct', 'N/A')}%")
        sections.append(f"Total Claims: {loss.get('total_claims', 0)}")
        sections.append(f"Total Incurred: ${loss.get('total_incurred', 0):,.0f}")
        sections.append(f"Total Premium: ${loss.get('total_premium', 0):,.0f}")
        sections.append(f"Open Claims: {loss.get('open_claims_count', 0)}")
        sections.append(f"Open Reserves: ${loss.get('open_reserves', 0):,.0f}")
        sections.append(f"Claim Trend: {loss.get('claim_trend', 'unknown')}")
        sections.append(f"Clean Years: {loss.get('clean_years', 0)}")

        if loss.get("largest_loss"):
            lg = loss["largest_loss"]
            sections.append(f"Largest Loss: ${lg['incurred']:,.0f} ({lg.get('type', '')} on {lg.get('date', '')})")

        if sub.get("potential"):
            sections.append(f"Subrogation Potential: YES — {sub.get('details', '')[:200]}")
        if supp.get("effective"):
            sections.append(f"Suppression Effectiveness: Fire suppression worked — {supp.get('details', '')[:200]}")
        if caus.get("types"):
            sections.append(f"Causation Types: {', '.join(caus['types'])}")
            sections.append(f"Pattern: {'Systemic (repeated)' if caus.get('systemic') else 'Isolated (no repeats)'}")

        if carrier.get("non_renewal"):
            sections.append(f"Carrier Non-Renewal: YES — {', '.join(carrier.get('non_renewal_carriers', []))}")

        # Rules engine decision
        sections.append("")
        sections.append("## RULES ENGINE DECISION (FINAL — do not override)")
        sections.append(f"Score: {rules_result.get('score', 3)}/5")
        sections.append(f"Status: {rules_result.get('status', 'review')}")
        sections.append(f"Winnability: {rules_result.get('winnability', 0.5):.0%}")
        sections.append(f"Priority: {rules_result.get('priority', 0.5):.0%}")
        sections.append(f"Reasoning: {rules_result.get('reasoning', '')}")

        if rules_result.get("triggers"):
            sections.append("\nTriggers Fired:")
            for t in rules_result["triggers"]:
                sections.append(f"  - {t['rule']}: {t['detail']}")

        if rules_result.get("overrides"):
            sections.append("\nMitigating Factors:")
            for o in rules_result["overrides"]:
                sections.append(f"  - {o['rule']}: {o['detail']}")

        if rules_result.get("positives"):
            sections.append("\nPositive Signals:")
            for p in rules_result["positives"]:
                sections.append(f"  - {p}")

        return "\n".join(sections)

    async def run(self, state: PipelineState) -> PipelineState:
        state.status = "generating"
        state.current_step = "brief_generation"

        extraction_json = state.extraction.model_dump_json(indent=2)
        appetite_json = state.appetite.model_dump_json(indent=2)
        scoring_json = state.scoring.model_dump_json(indent=2)
        evidence = self._format_evidence(state)
        analytics_text = self._format_analytics(state)

        # Get company name for logging
        company_name = state.extraction.company.name if hasattr(state.extraction, 'company') else (state.extraction.insured.name if hasattr(state.extraction, 'insured') else "Unknown")

        log.info("brief_generation_starting",
            company=company_name,
            evidence_chunks=len(state.retrieved_chunks),
            appetite_score=state.appetite.score,
        )

        # Build the list of actual uploaded filenames for this submission
        source_documents = "\n".join(
            f"- {doc.filename}" for doc in state.documents if doc.filename
        )

        prompt = BRIEF_USER.format(
            extraction_json=extraction_json,
            appetite_json=appetite_json,
            scoring_json=scoring_json,
            evidence_chunks=evidence,
            source_documents=source_documents,
        )

        # Inject analytics before the prompt
        full_prompt = f"""{analytics_text}

---

{prompt}"""

        try:
            brief = await self.llm.generate_brief(
                system_prompt=BRIEF_SYSTEM,
                user_prompt=full_prompt,
            )
            state.risk_brief = brief
            state.citations = self._extract_citations(brief)
            log.info("brief_generated", length=len(brief), citations=len(state.citations))
        except Exception as e:
            log.error("brief_generation_failed", error=str(e))
            state.errors.append(f"Brief generation failed: {str(e)}")
            state.risk_brief = self._fallback_brief(state)

        # Referral note
        if state.scoring.referral_required:
            try:
                rules_result = state.form_data.get("_rules_result", {})
                triggers = [t["rule"] for t in rules_result.get("triggers", [])]
                overrides = [o["rule"] for o in rules_result.get("overrides", [])]

                referral = await self.llm.generate_brief(
                    system_prompt="You are drafting a concise referral note for a senior commercial underwriter. Include: reason for referral, key risk factors, mitigating factors, recommended action. Keep under 200 words.",
                    user_prompt=f"Company: {company_name}\nScore: {state.appetite.score}/5\nTriggers: {triggers}\nOverrides: {overrides}\nWinnability: {state.scoring.winnability_score:.0%}\nBroker questions: {state.scoring.broker_questions}",
                )
                state.referral_note = referral
            except Exception as e:
                log.error("referral_note_failed", error=str(e))

        return state

    def _extract_citations(self, brief: str) -> list:
        citations = []
        pattern = r'\[Source:\s*([^,\]]+)(?:,\s*(?:page?\s*)?(\d+))?\]'
        for match in re.finditer(pattern, brief):
            citations.append(Citation(
                claim="",
                source_doc=match.group(1).strip(),
                page=int(match.group(2)) if match.group(2) else None,
            ))
        return citations

    def _fallback_brief(self, state: PipelineState) -> str:
        ext = state.extraction
        rules = state.form_data.get("_rules_result", {})
        loss = state.form_data.get("_analytics", {}).get("loss", {})

        name = ext.company.name if hasattr(ext, 'company') else (ext.insured.name if hasattr(ext, 'insured') else "Unknown")

        return f"""# Risk Brief — {name}

**Status**: {state.appetite.status.value}
**Appetite Score**: {state.appetite.score}/5
**Winnability**: {state.scoring.winnability_score:.0%}

## Pre-Computed Analytics
- **Loss Ratio**: {loss.get('loss_ratio_pct', 'N/A')}%
- **Loss Ratio (ex. largest)**: {loss.get('loss_ratio_ex_largest_pct', 'N/A')}%
- **Total Claims**: {loss.get('total_claims', 0)}
- **Open Claims**: {loss.get('open_claims_count', 0)}

## Rules Engine
- **Decision**: {rules.get('status', 'unknown')}
- **Reasoning**: {rules.get('reasoning', 'N/A')}

## Missing Information
{chr(10).join(f'- {m}' for m in ext.missing_fields) or '- None identified'}

## Broker Questions
{chr(10).join(f'- {q}' for q in state.scoring.broker_questions) or '- None'}

---
*Auto-generated fallback brief — LLM generation failed.*
"""