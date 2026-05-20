"""
Agent: Brief Writer — Commercial Insurance

Generates the 1-page risk brief using pre-computed analytics from the rules engine.
The LLM writes narrative ONLY — all numbers come from deterministic calculations.

Key: ALL sections (brief + referral note) consume the canonical _uw_facts object
built in analyze_node. No independent recalculation of any figure is allowed.
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
        uw = state.form_data.get("_uw_facts", {})

        if not rules_result:
            return "(No pre-computed analytics available)"

        loss = analytics.get("loss", {})
        carrier = analytics.get("carrier", {})
        sub = analytics.get("subrogation", {})
        supp = analytics.get("suppression", {})
        caus = analytics.get("causation", {})

        sections = []
        sections.append("## PRE-COMPUTED ANALYTICS — USE THESE EXACT NUMBERS, DO NOT RECALCULATE")
        sections.append(f"Loss Ratio: {uw.get('loss_ratio', 'N/A')}%")
        sections.append(f"Loss Ratio Excluding Largest Claim: {uw.get('loss_ratio_ex_largest', 'N/A')}%")
        sections.append(f"Total Claims: {uw.get('total_claims', 0)}")
        sections.append(f"Total Incurred: ${uw.get('total_incurred', 0):,.0f}")
        sections.append(f"Total Premium: ${uw.get('total_premium', 0):,.0f}")
        sections.append(f"Open Claims: {uw.get('open_claims', 0)}")
        sections.append(f"Open Reserves: ${uw.get('open_reserves', 0):,.0f}")
        sections.append(f"Clean Years: {uw.get('clean_years', 0)}")

        # Claim trend — use exact computed value, map to correct language
        claim_trend = uw.get("claim_trend", "stable")
        trend_map = {
            "stable": "loss activity remains manageable without evidence of worsening frequency or severity trends",
            "declining": "claim frequency is trending downward — improving risk profile",
            "increasing": "frequency is moderately elevated — above appetite threshold",
            "stable_frequency_severity_spike": "frequency is stable with one isolated severity outlier",
            "no_claims": "no claims in the policy period",
            "single_year_data": "single year of data available — trend indeterminate",
        }
        sections.append(f"Claim Trend: {trend_map.get(claim_trend, claim_trend)}")

        # Largest claim and average severity with TIV context
        if uw.get("largest_claim_amount"):
            sections.append(
                f"Largest Single Claim: ${uw['largest_claim_amount']:,.0f} "
                f"({uw.get('largest_claim_type', '')} on {uw.get('largest_claim_date', '')})"
            )
        total_claims = uw.get("total_claims", 0)
        total_incurred_val = uw.get("total_incurred", 0)
        if total_claims > 0 and total_incurred_val > 0:
            avg_severity = total_incurred_val / total_claims
            tiv = analytics.get("property", {}).get("total_tiv", 0)
            tiv_context = (
                f" — {avg_severity / tiv * 100:.1f}% of TIV (${tiv:,.0f})" if tiv > 0 else ""
            )
            sections.append(
                f"Average Claim Severity: ${avg_severity:,.0f}{tiv_context}. "
                f"NOTE: For multifamily property, contextualize severity against TIV and occupancy — "
                f"a moderate average severity is not automatically disqualifying."
            )

        # Causation — authoritative, never override with "systemic" if systemic=False
        systemic = uw.get("systemic", False)
        if systemic:
            sections.append(f"Causation Pattern: SYSTEMIC — repeated cause types detected")
        else:
            types = uw.get("causation_types", [])
            sections.append(
                f"Causation Pattern: ISOLATED — no repeated cause types. "
                f"Types: {', '.join(types) if types else 'varied'}. "
                f"DO NOT use the word 'systemic' in the brief."
            )

        if sub.get("potential"):
            sections.append(f"Subrogation Potential: YES — {sub.get('details', '')[:200]}")
        if supp.get("effective"):
            sections.append(f"Suppression Effective: YES — {supp.get('details', '')[:200]}")

        # Carrier history — authoritative
        prior_nonrenewal = uw.get("prior_nonrenewal", False)
        prior_carriers = uw.get("prior_nonrenewal_carriers", [])
        current_carriers = uw.get("current_carriers", [])
        if prior_nonrenewal:
            sections.append(
                f"Prior Carrier Non-Renewal: YES — non-renewed by: "
                f"{', '.join(prior_carriers) if prior_carriers else 'prior carrier'}"
            )
        else:
            sections.append("Prior Carrier Non-Renewal: NO")

        if current_carriers:
            sections.append(f"Current Carrier(s): {', '.join(current_carriers)}")

        sections.append("")
        sections.append("## RULES ENGINE DECISION (FINAL)")
        sections.append(f"Score: {rules_result.get('score', 3)}/5")
        sections.append(f"Status: {rules_result.get('status', 'review')}")
        sections.append(f"Reasoning: {rules_result.get('reasoning', '')}")

        if rules_result.get("triggers"):
            sections.append("\nTriggers Fired:")
            for t in rules_result["triggers"]:
                sections.append(f"  - {t['rule']}: {t['detail'][:200]}")

        if rules_result.get("overrides"):
            sections.append("\nMitigating Factors:")
            for o in rules_result["overrides"]:
                sections.append(f"  - {o['rule']}: {o['detail'][:200]}")

        if rules_result.get("positives"):
            sections.append("\nPositive Signals:")
            for p in rules_result["positives"]:
                if isinstance(p, dict):
                    sections.append(f"  - {p['rule']}: {p['detail'][:200]}")
                else:
                    sections.append(f"  - {p}")

        return "\n".join(sections)

    def _build_grounded_facts(self, state: PipelineState) -> str:
        """
        Non-overridable facts block — injected at very top of every LLM prompt.
        Sourced exclusively from _uw_facts (canonical object built in analyze_node).
        LLM must use these exact values. If these contradict the LLM's own reasoning,
        the LLM must defer to these.
        """
        uw = state.form_data.get("_uw_facts", {})

        prior_carriers = uw.get("prior_nonrenewal_carriers", [])
        current_carriers = uw.get("current_carriers", [])
        systemic = uw.get("systemic", False)

        # Build state context — distinguish HQ state from operational states
        ext = state.extraction
        hq_state = ext.company.state or ""
        location_states = list({loc.state for loc in ext.locations if loc.state})
        non_hq_states = [s for s in location_states if s != hq_state]
        if hq_state and non_hq_states:
            state_line = (
                f"Company HQ State: {hq_state} "
                f"(operations also in: {', '.join(sorted(non_hq_states))}). "
                f"Use HQ state for 'State:' field in the brief — do NOT use a property location state."
            )
        elif hq_state:
            state_line = f"Company HQ State: {hq_state}"
        elif location_states:
            state_line = f"Primary operations state: {location_states[0]} (HQ state not provided)"
        else:
            state_line = "State: Not specified"

        lines = [
            "## ══ GROUNDED FACTS ══ USE THESE EXACT VALUES — DO NOT CHANGE OR INVENT ALTERNATIVES",
            f"Company: {uw.get('company_name', 'Unknown')}",
            state_line,
            f"Loss Ratio: {uw.get('loss_ratio', 'N/A')}%",
            f"Loss Ratio (ex. largest claim): {uw.get('loss_ratio_ex_largest', 'N/A')}%",
            f"Total Claims: {uw.get('total_claims', 0)}",
            f"Total Incurred: ${uw.get('total_incurred', 0):,.0f}",
            f"Total Premium: ${uw.get('total_premium', 0):,.0f}",
            f"Open Claims: {uw.get('open_claims', 0)}",
            f"Appetite Score: {uw.get('appetite_score', 3)}/5",
            f"Status: {uw.get('appetite_status', 'review')}",
            f"Largest Single Claim: ${uw.get('largest_claim_amount', 0):,.0f} "
            f"({uw.get('largest_claim_type', '')} on {uw.get('largest_claim_date', '')})",
            f"Causation: {'SYSTEMIC' if systemic else 'ISOLATED — do NOT use the word systemic'}",
            f"Prior Non-Renewal: {'YES — by ' + ', '.join(prior_carriers) if uw.get('prior_nonrenewal') else 'NO'}",
            f"Current Carrier(s): {', '.join(current_carriers) if current_carriers else 'Not specified'}",
        ]
        return "\n".join(lines)

    async def run(self, state: PipelineState) -> PipelineState:
        state.status = "generating"
        state.current_step = "brief_generation"

        extraction_json = state.extraction.model_dump_json(indent=2)
        appetite_json = state.appetite.model_dump_json(indent=2)
        scoring_json = state.scoring.model_dump_json(indent=2)
        evidence = self._format_evidence(state)
        analytics_text = self._format_analytics(state)
        grounded_facts = self._build_grounded_facts(state)

        company_name = state.extraction.company.name or "Unknown"

        log.info("brief_generation_starting",
            company=company_name,
            evidence_chunks=len(state.retrieved_chunks),
            appetite_score=state.appetite.score,
        )

        # Valid source filenames for citation validation
        valid_uploaded = {doc.filename for doc in state.documents if doc.filename}
        valid_chunks = {c.source_doc for c in state.retrieved_chunks if c.source_doc}
        valid_sources = valid_uploaded | valid_chunks

        source_documents = "\n".join(f"- {doc.filename}" for doc in state.documents if doc.filename)

        prompt = BRIEF_USER.format(
            extraction_json=extraction_json,
            appetite_json=appetite_json,
            scoring_json=scoring_json,
            evidence_chunks=evidence,
            source_documents=source_documents,
        )

        # Grounded facts at very top — LLM sees these first
        full_prompt = f"""{grounded_facts}

---

{analytics_text}

---

{prompt}"""

        try:
            brief = await self.llm.generate_brief(
                system_prompt=BRIEF_SYSTEM,
                user_prompt=full_prompt,
            )
            state.risk_brief = brief
            state.citations = self._extract_citations(brief, valid_sources)
            log.info("brief_generated", length=len(brief), citations=len(state.citations))
        except Exception as e:
            log.error("brief_generation_failed", error=str(e))
            state.errors.append(f"Brief generation failed: {str(e)}")
            state.risk_brief = self._fallback_brief(state)

        # ── Referral note — fully grounded from _uw_facts ──────────────
        if state.scoring.referral_required:
            try:
                uw = state.form_data.get("_uw_facts", {})
                rules_result = state.form_data.get("_rules_result", {})

                triggers = [t["rule"] for t in rules_result.get("triggers", [])]
                overrides = [o["rule"] for o in rules_result.get("overrides", [])]

                prior_carriers = uw.get("prior_nonrenewal_carriers", [])
                current_carriers = uw.get("current_carriers", [])

                referral_user = f"""Draft a concise referral note for a senior commercial underwriter.

## ══ GROUNDED FACTS — USE THESE EXACT VALUES, DO NOT CHANGE ANY NUMBER ══
Company: {uw.get('company_name', company_name)}
Appetite Score: {uw.get('appetite_score', 3)}/5
Loss Ratio: {uw.get('loss_ratio', 'N/A')}%
Loss Ratio (ex. largest): {uw.get('loss_ratio_ex_largest', 'N/A')}%
Total Claims: {uw.get('total_claims', 0)}
Total Incurred: ${uw.get('total_incurred', 0):,.0f}
Largest Single Claim: ${uw.get('largest_claim_amount', 0):,.0f} ({uw.get('largest_claim_type', '')} on {uw.get('largest_claim_date', '')})
Open Claims: {uw.get('open_claims', 0)}
Prior Non-Renewal: {'YES — by ' + ', '.join(prior_carriers) if uw.get('prior_nonrenewal') else 'NO — no prior non-renewal'}
Current Carrier(s): {', '.join(current_carriers) if current_carriers else 'Not specified'}
Causation Pattern: {'SYSTEMIC' if uw.get('systemic') else 'ISOLATED (do NOT say systemic)'}

## Referral Triggers
{chr(10).join(f'- {t}' for t in triggers)}

## Mitigating Factors
{chr(10).join(f'- {o}' for o in overrides) if overrides else '- None'}

## Broker Questions
{chr(10).join(f'- {q}' for q in state.scoring.broker_questions)}

Write the referral note using ONLY the facts above. Keep under 200 words.
Do NOT invent or recalculate any numbers. Do NOT change carrier names."""

                referral = await self.llm.generate_brief(
                    system_prompt=(
                        "You are drafting a concise referral note for a senior commercial underwriter. "
                        "Use ONLY the exact numbers and carrier names provided in the GROUNDED FACTS section. "
                        "Never invent, estimate, or recalculate any figure."
                    ),
                    user_prompt=referral_user,
                )
                state.referral_note = referral
                log.info("referral_note_generated", length=len(referral))
            except Exception as e:
                log.error("referral_note_failed", error=str(e))

        return state

    def _extract_citations(self, brief: str, valid_sources: set = None) -> list:
        citations = []
        pattern = r'\[Source:\s*([^,\]]+)(?:,\s*(?:page?\s*)?(\d+))?\]'
        for match in re.finditer(pattern, brief):
            source_doc = match.group(1).strip()
            if valid_sources and source_doc not in valid_sources:
                log.warning("citation_invalid_source", source_doc=source_doc)
                continue
            citations.append(Citation(
                claim="",
                source_doc=source_doc,
                page=int(match.group(2)) if match.group(2) else None,
            ))
        return citations

    def _fallback_brief(self, state: PipelineState) -> str:
        uw = state.form_data.get("_uw_facts", {})
        ext = state.extraction
        rules = state.form_data.get("_rules_result", {})

        return f"""# Risk Brief — {uw.get('company_name', ext.company.name or 'Unknown')}

**Status**: {uw.get('appetite_status', state.appetite.status.value)}
**Appetite Score**: {uw.get('appetite_score', state.appetite.score)}/5

## Pre-Computed Analytics
- **Loss Ratio**: {uw.get('loss_ratio', 'N/A')}%
- **Loss Ratio (ex. largest)**: {uw.get('loss_ratio_ex_largest', 'N/A')}%
- **Total Claims**: {uw.get('total_claims', 0)}
- **Open Claims**: {uw.get('open_claims', 0)}
- **Largest Claim**: ${uw.get('largest_claim_amount', 0):,.0f} ({uw.get('largest_claim_type', '')})
- **Prior Non-Renewal**: {'YES — by ' + ', '.join(uw.get('prior_nonrenewal_carriers', [])) if uw.get('prior_nonrenewal') else 'No'}

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