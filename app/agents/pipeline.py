"""
LangGraph Pipeline: TriagePilot — Commercial Insurance.

Stages:
  classify → extract → analyze → retrieve → evaluate → brief → complete
                        ↑ NEW: deterministic analytics + rules engine

If SKIP_CLASSIFICATION=true, classify is skipped (done inside extraction).
"""

import time
import json
import structlog
from typing import TypedDict, Dict, Any, List
from langgraph.graph import StateGraph, START, END

from app.models.schemas import (
    PipelineState, SubmissionStatus, SubmissionOutput,
    UploadedDocument, AppetiteAssessment, AppetiteStatus,
    SubmissionScoring, RuleResult,
)
from app.agents.classifier import ClassifierAgent
from app.agents.extractor import ExtractorAgent
from app.agents.retriever import RetrieverAgent
from app.agents.evaluator import EvaluatorAgent
from app.agents.brief_writer import BriefWriterAgent
from app.services.analytics_service import compute_analytics
from app.services.rules_engine import evaluate_rules
from app.core.config import get_settings

log = structlog.get_logger()

# Audit logger — writes to Supabase audit_log table
_audit_client = None

def _get_audit_client():
    global _audit_client
    if _audit_client is None:
        from app.services.supabase_service import get_supabase
        _audit_client = get_supabase()
    return _audit_client

def audit_log(submission_id: str, stage: str, status: str, details: dict = None):
    """Write audit log entry to Supabase."""
    try:
        client = _get_audit_client()
        client.table("audit_log").insert({
            "submission_id": submission_id,
            "stage": stage,
            "status": status,
            "details": json.dumps(details or {}, default=str),
        }).execute()
    except Exception as e:
        log.warning("audit_log_failed", stage=stage, error=str(e))

def _build_lob_display(extraction, lob: str) -> str:
    if not extraction.coverages:
        return lob
    seen = []
    seen_normalized = set()
    for c in extraction.coverages:
        name = (c.lob or c.coverage_type or "").strip()
        if not name:
            continue
        # Normalize to deduplicate "Property" vs "Commercial Property"
        normalized = name.lower().replace("commercial ", "").replace("_", " ").strip()
        if normalized and normalized not in seen_normalized:
            seen_normalized.add(normalized)
            seen.append(name)
    if not seen:
        return lob
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen)

def _build_uw_facts(analytics: dict, rules_result: dict, extraction) -> dict:
    """
    Build a single canonical underwriting facts object from deterministic analytics.
    This is the SINGLE SOURCE OF TRUTH for all LLM sections:
    - evaluator narratives
    - brief writer
    - referral note
    All sections consume ONLY this object — no independent recalculation allowed.
    """
    loss = analytics.get("loss", {})
    carrier = analytics.get("carrier", {})
    causation = analytics.get("causation", {})
 
    # Largest event
    largest_event = loss.get("largest_event") or {}
    largest_amount = largest_event.get("total_incurred", 0)
    _types = largest_event.get("types", [])
    largest_type = _types[0] if _types else "unknown type"
    largest_date = largest_event.get("date", "")
 
    # Prior non-renewal — carriers that actually non-renewed
    non_renewal_carriers = carrier.get("non_renewal_carriers", [])
 
    # Current carriers — ones that are NOT non-renewed
    current_carriers = []
    for pi in (extraction.prior_insurance or []):
        if pi.carrier and not pi.cancelled_by_carrier:
            name = pi.carrier.strip()
            if name and "none" not in name.lower():
                current_carriers.append(name)
 
    return {
        # Company
        "company_name": extraction.company.name or "The insured",

        # Loss metrics — from deterministic analytics ONLY (verified claims only when loss run present)
        "loss_ratio": loss.get("loss_ratio_pct"),
        "loss_ratio_ex_largest": loss.get("loss_ratio_ex_largest_pct"),
        "total_claims": loss.get("total_events", 0),
        "total_incurred": loss.get("total_incurred", 0),
        "total_premium": loss.get("total_premium", 0),
        "open_claims": loss.get("open_events_count", 0),
        "open_reserves": loss.get("open_reserves", 0),
        "claim_trend": loss.get("claim_trend", "stable"),
        "clean_years": loss.get("clean_years", 0),

        # Data integrity — unverified claims excluded from metrics above
        "has_loss_run": loss.get("has_loss_run", False),
        "unverified_claim_count": loss.get("unverified_claim_count", 0),
        "data_conflicts": extraction.data_conflicts,

        # Largest claim — authoritative
        "largest_claim_amount": largest_amount,
        "largest_claim_type": largest_type,
        "largest_claim_date": largest_date,

        # Causation — from analytics, never LLM
        "systemic": causation.get("systemic", False),
        "causation_types": causation.get("unique_types", []),

        # Carrier history — authoritative
        "prior_nonrenewal": carrier.get("non_renewal", False),
        "prior_nonrenewal_carriers": non_renewal_carriers,
        "current_carriers": list(set(current_carriers)),

        # Rules engine decision
        "appetite_score": rules_result.get("score", 3),
        "appetite_status": rules_result.get("status", "review"),
        "reasoning": rules_result.get("reasoning", ""),
    }
 

class GraphState(TypedDict):
    pipeline: PipelineState
    files: Dict[str, bytes]


async def classify_node(state: GraphState) -> GraphState:
    settings = get_settings()
    ps = state["pipeline"]
    if settings.skip_classification:
        log.info("pipeline_stage", stage="classify", action="SKIPPED")
        audit_log(ps.submission_id, "classify", "skipped")
        return state
    log.info("pipeline_stage", stage="classify")
    agent = ClassifierAgent()
    state["pipeline"] = await agent.run(ps, state["files"])
    audit_log(ps.submission_id, "classify", "done", {
        "documents": len(ps.documents),
    })
    return state


async def extract_node(state: GraphState) -> GraphState:
    log.info("pipeline_stage", stage="extract")
    ps = state["pipeline"]
    agent = ExtractorAgent()
    state["pipeline"] = await agent.run(ps, state["files"])
    ps = state["pipeline"]
    audit_log(ps.submission_id, "extract", "done", {
        "company": ps.extraction.company.name,
        "coverages": len(ps.extraction.coverages),
        "losses": len(ps.extraction.loss_history),
        "locations": len(ps.extraction.locations),
        "missing": ps.extraction.missing_fields,
    })
    return state


async def analyze_node(state: GraphState) -> GraphState:
    """Deterministic analytics + rules engine. No LLM."""
    log.info("pipeline_stage", stage="analyze")
    ps = state["pipeline"]

    try:
        analytics = compute_analytics(ps.extraction)
        rules_result = evaluate_rules(analytics, ps.extraction)

        # Store in pipeline state
        ps.form_data["_analytics"] = analytics
        ps.form_data["_rules_result"] = rules_result

        # ── Build rule_results with ALL three signal types ──────────────
        # Previously only triggers were added; overrides and positives were
        # lost here and only partially recovered later as raw strings by
        # the evaluator. Now all three are structured and sent to frontend.

        all_rule_results: List[RuleResult] = []

        # 1. Triggers — negative signals (decline / refer)
        for i, t in enumerate(rules_result.get("triggers", [])):
            all_rule_results.append(RuleResult(
                rule_id=f"trigger_{i}",
                rule_name=t["rule"],
                passed=False,
                reason=t["detail"],
                severity=t["severity"],   # "decline" or "refer"
            ))

        # 2. Overrides — mitigating factors (were completely dropped before)
        for i, o in enumerate(rules_result.get("overrides", [])):
            all_rule_results.append(RuleResult(
                rule_id=f"override_{i}",
                rule_name=o["rule"],
                passed=True,
                reason=o["detail"],
                severity="override",
            ))

        # 3. Positives — green signals (were dropped as plain strings before)
        for i, p in enumerate(rules_result.get("positives", [])):
            # positives are now dicts with rule + detail (rules_engine v3)
            rule_name = p["rule"] if isinstance(p, dict) else p
            rule_detail = p["detail"] if isinstance(p, dict) else p
            all_rule_results.append(RuleResult(
                rule_id=f"positive_{i}",
                rule_name=rule_name,
                passed=True,
                reason=rule_detail,
                severity="positive",
            ))

        # ── Pre-populate appetite ────────────────────────────────────────
        ps.appetite = AppetiteAssessment(
            score=rules_result["score"],
            status=AppetiteStatus(rules_result["status"]),
            reasons=[r.rule_name + " — " + r.reason for r in all_rule_results],
            rule_results=all_rule_results,
        )

        # ── Pre-populate scoring ─────────────────────────────────────────
        ps.scoring = SubmissionScoring(
            winnability_score=rules_result["winnability"],
            priority_score=rules_result["priority"],
            referral_required=rules_result["status"] in ("refer", "decline") or bool(rules_result.get("triggers")),
            referral_reasons=[
                t["rule"] for t in rules_result["triggers"]
                if t["severity"] in ("refer", "decline")
            ],
        )

        # ── Build canonical underwriting facts — single source of truth ──
        uw_facts = _build_uw_facts(analytics, rules_result, ps.extraction)
        ps.form_data["_uw_facts"] = uw_facts

        log.info("uw_facts_built",
            company=uw_facts["company_name"],
            loss_ratio=uw_facts["loss_ratio"],
            total_claims=uw_facts["total_claims"],
            largest_claim=uw_facts["largest_claim_amount"],
            prior_nonrenewal=uw_facts["prior_nonrenewal"],
            systemic=uw_facts["systemic"],
        )

        log.info("analyze_done",
            score=rules_result["score"],
            status=rules_result["status"],
            winnability=rules_result["winnability"],
            triggers=len(rules_result.get("triggers", [])),
            overrides=len(rules_result.get("overrides", [])),
            positives=len(rules_result.get("positives", [])),
            total_signals=len(all_rule_results),
        )
        audit_log(ps.submission_id, "analyze", "done", {
            "score": rules_result["score"],
            "status": rules_result["status"],
            "winnability": rules_result["winnability"],
            "priority": rules_result["priority"],
            "loss_ratio": rules_result["analytics_summary"]["loss_ratio"],
            "triggers": len(rules_result.get("triggers", [])),
            "overrides": len(rules_result.get("overrides", [])),
            "positives": len(rules_result.get("positives", [])),
        })

    except Exception as e:
        log.error("analyze_failed", error=str(e))
        ps.errors.append(f"Analytics failed: {str(e)}")
        audit_log(ps.submission_id, "analyze", "error", {"error": str(e)})

    return state


async def retrieve_node(state: GraphState) -> GraphState:
    log.info("pipeline_stage", stage="retrieve")
    ps = state["pipeline"]
    agent = RetrieverAgent()
    state["pipeline"] = await agent.run(ps)
    audit_log(ps.submission_id, "retrieve", "done", {
        "chunks_found": len(ps.retrieved_chunks),
    })
    return state


async def evaluate_node(state: GraphState) -> GraphState:
    """LLM evaluator — now receives pre-computed analytics + rules. Only writes narrative."""
    log.info("pipeline_stage", stage="evaluate")
    ps = state["pipeline"]
    agent = EvaluatorAgent()
    state["pipeline"] = await agent.run(ps)
    audit_log(ps.submission_id, "evaluate", "done", {
        "appetite_score": ps.appetite.score,
        "winnability": ps.scoring.winnability_score,
    })
    return state


async def brief_node(state: GraphState) -> GraphState:
    log.info("pipeline_stage", stage="brief")
    ps = state["pipeline"]
    agent = BriefWriterAgent()
    state["pipeline"] = await agent.run(ps)
    audit_log(ps.submission_id, "brief", "done", {
        "brief_length": len(ps.risk_brief),
        "citations": len(ps.citations),
    })
    return state


async def complete_node(state: GraphState) -> GraphState:
    log.info("pipeline_stage", stage="complete")
    ps = state["pipeline"]
    ps.status = SubmissionStatus.COMPLETE
    ps.current_step = "done"
    audit_log(ps.submission_id, "complete", "done", {
        "errors": ps.errors,
        "final_score": ps.appetite.score,
    })
    return state


def should_continue(state: GraphState) -> str:
    if len(state["pipeline"].errors) >= 5:
        log.warning("too_many_errors", count=len(state["pipeline"].errors))
        return "complete"
    return "continue"


def build_pipeline() -> StateGraph:
    graph = StateGraph(GraphState)

    graph.add_node("classify", classify_node)
    graph.add_node("extract", extract_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("brief", brief_node)
    graph.add_node("complete", complete_node)

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "extract")
    graph.add_conditional_edges("extract", should_continue, {
        "continue": "analyze", "complete": "complete",
    })
    graph.add_edge("analyze", "retrieve")
    graph.add_conditional_edges("retrieve", should_continue, {
        "continue": "evaluate", "complete": "complete",
    })
    graph.add_conditional_edges("evaluate", should_continue, {
        "continue": "brief", "complete": "complete",
    })
    graph.add_edge("brief", "complete")
    graph.add_edge("complete", END)

    return graph.compile()


_pipeline = build_pipeline()


async def run_pipeline(
    documents: List[UploadedDocument],
    files: Dict[str, bytes],
    form_data: Dict[str, Any] = None,
) -> SubmissionOutput:
    start_time = time.time()

    pipeline_state = PipelineState(
        documents=documents,
        form_data=form_data or {},
    )

    audit_log(pipeline_state.submission_id, "pipeline_start", "started", {
        "file_count": len(documents),
        "has_form_data": bool(form_data),
    })

    initial_state: GraphState = {
        "pipeline": pipeline_state,
        "files": files,
    }

    final_state = await _pipeline.ainvoke(initial_state)
    ps: PipelineState = final_state["pipeline"]
    elapsed = time.time() - start_time

    output = SubmissionOutput(
        submission_id=ps.submission_id,
        status=ps.status,
        line_of_business=_build_lob_display(ps.extraction, ps.lob),
        company=ps.extraction.company,
        extracted_facts=ps.extraction.raw_fields,
        missing_information=ps.extraction.missing_fields,
        appetite_assessment=ps.appetite,
        winnability_score=ps.scoring.winnability_score,
        priority_score=ps.scoring.priority_score,
        referral_required=ps.scoring.referral_required,
        referral_reasons=ps.scoring.referral_reasons,
        recommended_queue=ps.scoring.recommended_queue,
        broker_questions=ps.scoring.broker_questions,
        risk_brief_markdown=ps.risk_brief,
        referral_note=ps.referral_note,
        citations=ps.citations,
        retrieved_evidence=ps.retrieved_chunks,
        documents=ps.documents,
        processing_time_seconds=round(elapsed, 2),
        model_versions={
            "parsing": "llamaparse",
            "extraction": ps.extraction.company.name or "commercial",
        },
        errors=ps.errors,
    )

    log.info("pipeline_complete",
        submission_id=output.submission_id,
        elapsed=f"{elapsed:.1f}s",
        status=output.status,
        company=output.company.name,
        appetite_score=output.appetite_assessment.score,
        winnability=output.winnability_score,
    )
    return output