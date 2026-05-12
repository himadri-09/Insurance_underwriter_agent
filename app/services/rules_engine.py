"""
Rules engine: deterministic underwriting rules evaluation.

NO LLM calls. All rules are if/else logic.
Results are passed to the LLM for narrative generation only.
"""

import structlog
from app.models.schemas import ExtractionResult

log = structlog.get_logger()


def _deep_scan_info_completeness(extraction: ExtractionResult) -> dict:
    """
    Deep scan across all data sources to determine true information completeness.
    Returns dict with flags for each critical information category.
    """
    return {
        "has_loss_history": bool(extraction.loss_history) or bool(extraction.stated_total_incurred),
        "has_prior_insurance": bool(extraction.prior_insurance) or bool(extraction.stated_total_premium),
        "has_coverages": bool(extraction.coverages),
        "has_company_basics": bool(extraction.company.name and extraction.company.address),
        "has_industry_class": bool(extraction.company.naics_code or extraction.company.sic_code),
        "has_revenue": bool(extraction.company.annual_revenue),
        "has_property_details": bool(extraction.locations),
        "has_business_description": bool(extraction.business_description or extraction.company.description),
        "all_coverage_requested_data": bool(
            extraction.coverages 
            or extraction.stated_total_premium 
            or extraction.business_description
        ),
    }


def evaluate_rules(analytics: dict, extraction: ExtractionResult) -> dict:
    """
    Apply underwriting rules to pre-computed analytics.
    Returns triggers, overrides, and final recommendation.
    """
    triggers = []       # rules that fired (negative)
    positives = []      # positive signals
    overrides = []      # mitigating factors that override triggers
    missing = []        # critical missing info

    loss = analytics.get("loss", {})
    biz = analytics.get("business", {})
    carrier = analytics.get("carrier", {})
    prop = analytics.get("property", {})
    cov = analytics.get("coverage", {})
    subrogation = analytics.get("subrogation", {})
    suppression = analytics.get("suppression", {})
    causation = analytics.get("causation", {})

    loss_ratio = loss.get("loss_ratio_pct")
    loss_ratio_ex_cat = loss.get("loss_ratio_ex_largest_pct")
    total_events = loss.get("total_events", 0)
    open_events = loss.get("open_events_count", 0)
    open_reserves = loss.get("open_reserves", 0)
    claim_trend = loss.get("claim_trend", "")
    clean_years = loss.get("clean_years", 0)

    revenue = biz.get("revenue", 0)
    years = biz.get("years_in_business", 0)
    headcount = biz.get("headcount", 0)

    # ══════════════════════════════════════════════════
    # DECLINE TRIGGERS (score 1)
    # ══════════════════════════════════════════════════

    if loss_ratio is not None and loss_ratio > 200:
        triggers.append({
            "rule": "DECLINE: Loss ratio exceeds 200%",
            "severity": "decline",
            "detail": f"Loss ratio: {loss_ratio:.1f}% (total incurred ${loss['total_incurred']:,.0f} / total premium ${loss['total_premium']:,.0f})",
        })

    if total_events >= 5 and open_events >= 2:
        triggers.append({
            "rule": "DECLINE: 5+ claims with 2+ still open",
            "severity": "decline",
            "detail": f"{total_events} loss events, {open_events} open",
        })

    if carrier.get("non_renewal_count", 0) >= 2:
        triggers.append({
            "rule": "DECLINE: Non-renewed by 2+ carriers",
            "severity": "decline",
            "detail": f"Non-renewed by: {', '.join(carrier.get('non_renewal_carriers', []))}",
        })

    if years < 1:
        triggers.append({
            "rule": "DECLINE: Business less than 1 year old",
            "severity": "decline",
            "detail": f"Years in business: {years}",
        })

    # ══════════════════════════════════════════════════
    # REFERRAL TRIGGERS (score 2-3)
    # ══════════════════════════════════════════════════

    if loss_ratio is not None and 50 < loss_ratio <= 200:
        triggers.append({
            "rule": "REFER: Loss ratio between 50-200%",
            "severity": "refer",
            "detail": f"Loss ratio: {loss_ratio:.1f}%",
        })

    if carrier.get("non_renewal"):
        triggers.append({
            "rule": "REFER: Prior carrier non-renewal",
            "severity": "refer",
            "detail": f"Non-renewed by: {', '.join(carrier.get('non_renewal_carriers', [])) or 'carrier (from broker notes)'}",
        })

    if open_reserves > 100000:
        triggers.append({
            "rule": "REFER: Open claim reserves exceed $100K",
            "severity": "refer",
            "detail": f"Open reserves: ${open_reserves:,.0f}",
        })

    if total_events >= 3:
        triggers.append({
            "rule": "REFER: 3+ claims in history",
            "severity": "refer",
            "detail": f"{total_events} loss events",
        })

    if revenue > 50000000:
        triggers.append({
            "rule": "REFER: Revenue exceeds $50M (large account)",
            "severity": "refer",
            "detail": f"Revenue: ${revenue:,.0f}",
        })

    if cov.get("max_single_limit", 0) > 5000000:
        triggers.append({
            "rule": "REFER: Requested limit exceeds $5M",
            "severity": "refer",
            "detail": f"Max limit requested: ${cov['max_single_limit']:,.0f}",
        })

    if years < 2:
        triggers.append({
            "rule": "REFER: Business less than 2 years old",
            "severity": "refer",
            "detail": f"Years in business: {years}",
        })

    if prop.get("oldest_building_year") and (2026 - prop["oldest_building_year"]) > 40:
        triggers.append({
            "rule": "REFER: Property older than 40 years",
            "severity": "refer",
            "detail": f"Oldest building: {prop['oldest_building_year']}",
        })

    # ══════════════════════════════════════════════════
    # OVERRIDES (mitigating factors)
    # ══════════════════════════════════════════════════

    if loss_ratio_ex_cat is not None and loss_ratio_ex_cat < 30:
        overrides.append({
            "rule": "OVERRIDE: Excluding largest loss, ratio is excellent",
            "detail": f"Loss ratio excluding largest claim: {loss_ratio_ex_cat:.1f}% (incurred ${loss['incurred_ex_largest']:,.0f})",
        })

    if subrogation.get("potential"):
        overrides.append({
            "rule": "OVERRIDE: Subrogation recovery potential",
            "detail": f"May reduce net incurred. Details: {subrogation.get('details', '')[:200]}",
        })

    if suppression.get("effective"):
        overrides.append({
            "rule": "OVERRIDE: Fire suppression system worked as designed",
            "detail": f"Prevented total loss. Details: {suppression.get('details', '')[:200]}",
        })

    if causation.get("isolated") and not causation.get("systemic"):
        overrides.append({
            "rule": "OVERRIDE: Loss causes are isolated (not systemic pattern)",
            "detail": f"Causation types: {', '.join(causation.get('types', []))}",
        })

    if clean_years >= 3:
        overrides.append({
            "rule": "OVERRIDE: Multiple clean years in history",
            "detail": f"{clean_years} claim-free years",
        })

    if claim_trend == "declining":
        overrides.append({
            "rule": "OVERRIDE: Claim frequency is declining",
            "detail": f"Trend: {claim_trend}",
        })

    # ══════════════════════════════════════════════════
    # POSITIVE SIGNALS
    # ══════════════════════════════════════════════════

    if years >= 5:
        positives.append(f"Established business: {years} years in operation")

    if loss_ratio is not None and loss_ratio < 40:
        positives.append(f"Excellent loss ratio: {loss_ratio:.1f}%")

    if total_events == 0:
        positives.append("Zero claims history")

    if clean_years >= 5:
        positives.append(f"{clean_years} claim-free years")

    if cov.get("multi_line"):
        positives.append(f"Multi-line opportunity: {cov.get('line_count')} lines requested")

    if not carrier.get("non_renewal"):
        positives.append("No carrier non-renewals — continuous coverage")

    if prop.get("sprinklered_count", 0) > 0 and prop.get("unsprinklered_count", 0) == 0:
        positives.append("All locations fully sprinklered")

    if claim_trend == "declining":
        positives.append("Claim frequency trending downward")

    # Broker notes positives
    notes = (extraction.broker_notes or "").lower()
    if "osha" in notes and "vpp" in notes:
        positives.append("OSHA VPP participant")
    if "safety manager" in notes or "safety program" in notes:
        positives.append("Formal safety program in place")
    if "experience mod" in notes or "emr" in notes:
        positives.append("Experience modification rate noted (check value)")

    # ══════════════════════════════════════════════════
    # MISSING INFO — DEEP SCAN
    # ══════════════════════════════════════════════════
    # Check not only extraction arrays, but also alternative data sources

    completeness = _deep_scan_info_completeness(extraction)
    
    # Only flag as truly missing if NO data found in any source
    if not completeness["has_loss_history"]:
        missing.append("No loss runs provided — cannot verify claims history")
    if not completeness["has_prior_insurance"]:
        missing.append("No prior insurance information — cannot verify continuity")
    if not completeness["has_industry_class"]:
        missing.append("No NAICS/SIC code — cannot determine industry class")
    if not completeness["has_revenue"]:
        missing.append("No revenue provided — cannot assess business size")
    if not completeness["has_property_details"] and any("property" in (c.coverage_type or "").lower() for c in extraction.coverages):
        missing.append("No property details for property coverage request")

    # ══════════════════════════════════════════════════
    # DETERMINE FINAL SCORE AND STATUS
    # ══════════════════════════════════════════════════

    decline_triggers = [t for t in triggers if t["severity"] == "decline"]
    refer_triggers = [t for t in triggers if t["severity"] == "refer"]

    if decline_triggers:
        # Check if overrides can rescue a decline
        if len(overrides) >= 3 and not any("200%" in t["rule"] for t in decline_triggers if "Loss ratio" in t["rule"]):
            score = 2
            status = "refer"
            reasoning = "Decline triggers present but multiple mitigating factors suggest referral for senior review"
        else:
            score = 1
            status = "decline"
            reasoning = "One or more automatic decline triggers fired"
    elif refer_triggers:
        # Check if overrides cancel refer triggers
        override_count = len(overrides)
        refer_count = len(refer_triggers)
        if override_count >= refer_count and not any("non-renewal" in t["rule"].lower() for t in refer_triggers):
            score = 4
            status = "accept"
            reasoning = "Referral triggers present but fully overridden by mitigating factors"
        elif override_count > 0:
            score = 3
            status = "refer"
            reasoning = "Referral triggers partially offset by mitigating factors"
        else:
            score = 2
            status = "refer"
            reasoning = "Referral triggers with no mitigating factors"
    elif positives:
        if len(positives) >= 5:
            score = 5
            status = "accept"
            reasoning = "Strong positive signals across all dimensions"
        else:
            score = 4
            status = "accept"
            reasoning = "Good risk profile with positive indicators"
    else:
        score = 3
        status = "review"
        reasoning = "Insufficient information for definitive assessment"

    # ══════════════════════════════════════════════════
    # WINNABILITY CALCULATION
    # ══════════════════════════════════════════════════

    winnability = 0.50  # baseline

    # Loss ratio impact
    if loss_ratio is not None:
        if loss_ratio > 200:
            winnability = 0.10
        elif loss_ratio > 100:
            winnability = 0.25
        elif loss_ratio > 50:
            winnability = 0.40
        elif loss_ratio > 30:
            winnability = 0.60
        elif loss_ratio > 0:
            winnability = 0.75
        else:
            winnability = 0.85
    elif total_events == 0:
        winnability = 0.85

    # Adjustments
    if carrier.get("non_renewal"):
        winnability -= 0.15
    if open_reserves > 100000:
        winnability -= 0.10
    if subrogation.get("potential"):
        winnability += 0.05
    if loss_ratio_ex_cat is not None and loss_ratio_ex_cat < 20:
        winnability += 0.10
    if suppression.get("effective"):
        winnability += 0.05
    if years >= 10:
        winnability += 0.05
    if cov.get("multi_line"):
        winnability += 0.05
    if len(positives) >= 5:
        winnability += 0.05

    winnability = max(0.05, min(0.95, winnability))

    # ══════════════════════════════════════════════════
    # PRIORITY CALCULATION
    # ══════════════════════════════════════════════════

    priority = 0.50

    if cov.get("multi_line"):
        priority += 0.10
    if revenue > 5000000:
        priority += 0.10
    if loss.get("total_premium", 0) > 50000:
        priority += 0.10
    if score >= 4:
        priority += 0.10
    if score <= 2:
        priority -= 0.10
    if carrier.get("non_renewal"):
        priority += 0.05  # time pressure

    priority = max(0.10, min(0.95, priority))

    result = {
        "score": score,
        "status": status,
        "reasoning": reasoning,
        "triggers": triggers,
        "overrides": overrides,
        "positives": positives,
        "missing_info": missing,
        "winnability": round(winnability, 2),
        "priority": round(priority, 2),
        "analytics_summary": {
            "loss_ratio": loss.get("loss_ratio_pct"),
            "loss_ratio_ex_cat": loss.get("loss_ratio_ex_largest_pct"),
            "total_claims": total_events,
            "open_claims": open_events,
            "open_reserves": open_reserves,
            "clean_years": clean_years,
            "claim_trend": claim_trend,
            "subrogation_potential": subrogation.get("potential", False),
            "suppression_effective": suppression.get("effective", False),
            "non_renewal": carrier.get("non_renewal", False),
            "revenue": revenue,
        },
    }

    log.info("rules_evaluated",
        score=score,
        status=status,
        triggers=len(triggers),
        overrides=len(overrides),
        positives=len(positives),
        winnability=winnability,
        priority=priority,
    )

    return result