"""
Rules engine: deterministic underwriting rules evaluation.

NO LLM calls. All rules are if/else logic.
Results are passed to the LLM evaluator for narrative generation only.

Key fix (v2): Mitigation-weighted decisioning.
  Previously: loss_ratio > 200 → always "decline", mitigations cosmetic only.
  Now: decline triggers have a weight, mitigations reduce that weight,
       and the adjusted weight maps to decline / refer / refer_with_conditions.
  This correctly handles isolated catastrophic events (BrightTech)
  vs systemic repeated losses (which should still hard-decline).
"""

import structlog
from app.models.schemas import ExtractionResult

log = structlog.get_logger()


def _deep_scan_info_completeness(extraction: ExtractionResult) -> dict:
    """Check multiple data sources for each critical information category."""
    return {
        "has_loss_history": bool(extraction.loss_history) or bool(extraction.stated_total_incurred),
        "has_prior_insurance": bool(extraction.prior_insurance) or bool(extraction.stated_total_premium),
        "has_coverages": bool(extraction.coverages),
        "has_company_basics": bool(extraction.company.name and extraction.company.address),
        "has_industry_class": bool(extraction.company.naics_code or extraction.company.sic_code),
        "has_revenue": bool(extraction.company.annual_revenue),
        "has_property_details": bool(extraction.locations),
        "has_business_description": bool(extraction.business_description or extraction.company.description),
    }


def _format_loss_history(extraction: ExtractionResult) -> str:
    """Format loss history into a rich human-readable string for evaluator narratives."""
    if not extraction.loss_history:
        return "No claims on record."
    lines = []
    for loss in extraction.loss_history:
        amount = f"${loss.incurred:,.0f}" if loss.incurred else (f"${loss.amount_paid:,.0f}" if loss.amount_paid else "amount unknown")
        date = loss.date_of_loss or "unknown date"
        claim_type = loss.type or "unknown type"
        desc = loss.description or ""
        status = loss.status or "unknown"
        line = f"  - {date}: {claim_type} — {amount} incurred ({status})"
        if desc:
            line += f". {desc[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _format_coverages(extraction: ExtractionResult) -> str:
    """Format requested coverages into a readable list."""
    if not extraction.coverages:
        return "No coverages specified."
    seen = set()
    lobs = []
    for c in extraction.coverages:
        lob = c.lob or c.coverage_type or ""
        if lob and lob not in seen:
            seen.add(lob)
            lobs.append(lob)
    return ", ".join(lobs) if lobs else "Not specified"


def evaluate_rules(analytics: dict, extraction: ExtractionResult) -> dict:
    """
    Apply underwriting rules to pre-computed analytics.
    Returns triggers, overrides, positives, and final recommendation.
    """
    triggers  = []
    positives = []
    overrides = []
    missing   = []

    loss       = analytics.get("loss", {})
    biz        = analytics.get("business", {})
    carrier    = analytics.get("carrier", {})
    prop       = analytics.get("property", {})
    cov        = analytics.get("coverage", {})
    subrogation = analytics.get("subrogation", {})
    suppression = analytics.get("suppression", {})
    causation   = analytics.get("causation", {})

    loss_ratio        = loss.get("loss_ratio_pct")
    loss_ratio_ex_cat = loss.get("loss_ratio_ex_largest_pct")
    total_incurred    = loss.get("total_incurred", 0)
    total_premium     = loss.get("total_premium", 0)
    incurred_ex_largest = loss.get("incurred_ex_largest", 0)
    total_events      = loss.get("total_events", 0)
    open_events       = loss.get("open_events_count", 0)
    open_reserves     = loss.get("open_reserves", 0)
    claim_trend       = loss.get("claim_trend", "")
    clean_years       = loss.get("clean_years", 0)
    shock_loss        = loss.get("shock_loss_present", False)
    largest_event     = loss.get("largest_event") or {}

    revenue = biz.get("revenue", 0)
    years   = biz.get("years_in_business", 0)

    company_name  = extraction.company.name or "The insured"
    loss_history_detail = _format_loss_history(extraction)
    coverages_detail    = _format_coverages(extraction)
    notes_lower = (extraction.broker_notes or "").lower()

    # ══════════════════════════════════════════════════
    # DECLINE TRIGGERS
    # ══════════════════════════════════════════════════

    if loss_ratio is not None and loss_ratio > 200:
        triggers.append({
            "rule": "DECLINE: Loss ratio exceeds 200%",
            "severity": "decline",
            "detail": (
                f"Loss ratio: {loss_ratio:.1f}% "
                f"(total incurred ${total_incurred:,.0f} / "
                f"total premium ${total_premium:,.0f}). "
                f"This significantly exceeds the maximum acceptable threshold of 200% "
                f"for standard commercial lines. "
                f"Loss history:\n{loss_history_detail}"
            ),
        })

    if total_events >= 5 and open_events >= 2:
        triggers.append({
            "rule": "DECLINE: 5+ claims with 2+ still open",
            "severity": "decline",
            "detail": (
                f"{total_events} loss events with {open_events} still open. "
                f"Open reserves: ${open_reserves:,.0f}. "
                f"Loss history:\n{loss_history_detail}"
            ),
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
            "detail": f"{company_name} has been in operation for less than 1 year — insufficient track record for standard commercial underwriting.",
        })

    # ══════════════════════════════════════════════════
    # REFERRAL TRIGGERS
    # ══════════════════════════════════════════════════

    if loss_ratio is not None and 50 < loss_ratio <= 200:
        triggers.append({
            "rule": "REFER: Loss ratio between 50-200%",
            "severity": "refer",
            "detail": (
                f"Loss ratio: {loss_ratio:.1f}% "
                f"(total incurred ${total_incurred:,.0f} / total premium ${total_premium:,.0f}). "
                f"Exceeds the 50% standard threshold, requiring senior underwriter review. "
                f"Loss history:\n{loss_history_detail}"
            ),
        })

    if carrier.get("non_renewal"):
        triggers.append({
            "rule": "REFER: Prior carrier non-renewal",
            "severity": "refer",
            "detail": (
                f"Non-renewed by: {', '.join(carrier.get('non_renewal_carriers', [])) or 'prior carrier (from broker notes)'}. "
                f"Carrier non-renewal signals elevated risk perception. Senior underwriter must review reason for non-renewal."
            ),
        })

    if open_reserves > 100_000:
        triggers.append({
            "rule": "REFER: Open claim reserves exceed $100K",
            "severity": "refer",
            "detail": f"Open reserves: ${open_reserves:,.0f} across {open_events} open claim(s). Elevated open exposure requires senior review before binding.",
        })

    if total_events >= 3:
        triggers.append({
            "rule": "REFER: 3+ claims in history",
            "severity": "refer",
            "detail": (
                f"{company_name} has {total_events} loss events totalling ${total_incurred:,.0f} "
                f"over the policy period (loss ratio: {loss_ratio:.1f}% overall, "
                f"{loss_ratio_ex_cat:.1f}% excluding largest claim). "
                f"Claim frequency exceeds the 3-claim referral threshold for commercial property risks. "
                f"Full loss history:\n{loss_history_detail}"
            ),
        })

    if revenue > 50_000_000:
        triggers.append({
            "rule": "REFER: Revenue exceeds $50M (large account)",
            "severity": "refer",
            "detail": f"{company_name} annual revenue: ${revenue:,.0f} — exceeds $50M large account threshold requiring senior underwriter assignment.",
        })

    if cov.get("max_single_limit", 0) > 5_000_000:
        triggers.append({
            "rule": "REFER: Requested limit exceeds $5M",
            "severity": "refer",
            "detail": f"Max limit requested: ${cov['max_single_limit']:,.0f} — exceeds $5M single-limit referral threshold.",
        })

    if years < 2:
        triggers.append({
            "rule": "REFER: Business less than 2 years old",
            "severity": "refer",
            "detail": f"{company_name} has been in business for {years} year(s) — below the 2-year minimum for standard commercial approval.",
        })

    if prop.get("oldest_building_year") and (2026 - prop["oldest_building_year"]) > 40:
        age = 2026 - prop["oldest_building_year"]
        triggers.append({
            "rule": "REFER: Property older than 40 years",
            "severity": "refer",
            "detail": f"Oldest building: {prop['oldest_building_year']} ({age} years old). Properties over 40 years require senior review for structural condition and update status.",
        })

    # ══════════════════════════════════════════════════
    # OVERRIDES (mitigating factors)
    # ══════════════════════════════════════════════════

    if loss_ratio_ex_cat is not None and loss_ratio_ex_cat < 30:
        largest_type = largest_event.get("type", "largest claim") if largest_event else "largest claim"
        largest_amt  = largest_event.get("total_incurred", 0) if largest_event else 0
        largest_date = largest_event.get("date_normalized", "") if largest_event else ""
        overrides.append({
            "rule": "OVERRIDE: Excluding largest loss, ratio is excellent",
            "detail": (
                f"Excluding the largest claim ({largest_type}, ${largest_amt:,.0f}"
                f"{f', {largest_date}' if largest_date else ''}), "
                f"{company_name} has an excellent adjusted loss ratio of {loss_ratio_ex_cat:.1f}% "
                f"(remaining incurred: ${incurred_ex_largest:,.0f} / total premium: ${total_premium:,.0f}). "
                f"This demonstrates that underlying loss activity is well-controlled — the elevated overall "
                f"ratio is driven by a single isolated event rather than recurring patterns."
            ),
        })

    if subrogation.get("potential"):
        overrides.append({
            "rule": "OVERRIDE: Subrogation recovery potential",
            "detail": f"Subrogation potential identified — may reduce net incurred exposure. Details: {subrogation.get('details', '')[:200]}",
        })

    if suppression.get("effective"):
        overrides.append({
            "rule": "OVERRIDE: Fire suppression system worked as designed",
            "detail": f"Fire suppression system activated and contained the loss as intended — prevented total loss scenario. {suppression.get('details', '')[:200]}",
        })

    if not causation.get("systemic") and total_events >= 1:
        # Build human-readable cause list from actual loss history
        cause_descriptions = []
        for loss_item in extraction.loss_history:
            if loss_item.type:
                amt = f"${loss_item.incurred:,.0f}" if loss_item.incurred else ""
                date = loss_item.date_of_loss or ""
                entry = f"{loss_item.type} ({date}, {amt})" if amt else f"{loss_item.type} ({date})"
                cause_descriptions.append(entry)

        cause_text = "; ".join(cause_descriptions) if cause_descriptions else ", ".join(causation.get("unique_types", []))

        overrides.append({
            "rule": "OVERRIDE: Loss causes are isolated (not systemic pattern)",
            "detail": (
                f"{company_name} has {total_events} claims with entirely different causation types — "
                f"no single cause repeats across the policy period: {cause_text}. "
                f"The absence of systemic patterns (e.g. repeated water damage, recurring fire risk) "
                f"indicates these losses are random, unrelated events rather than an underlying operational deficiency. "
                f"This significantly mitigates forward-looking frequency risk."
            ),
        })

    if clean_years >= 3:
        overrides.append({
            "rule": "OVERRIDE: Multiple clean years in history",
            "detail": f"{clean_years} calendar years within the policy period had zero claims — demonstrates operational control and risk management discipline.",
        })

    # ══════════════════════════════════════════════════
    # POSITIVES — now with actual data in the detail
    # ══════════════════════════════════════════════════

    if years >= 3:
        revenue_str = f", ${revenue:,.0f} annual revenue" if revenue else ""
        industry = extraction.company.industry or extraction.company.description or ""
        positives.append({
            "rule": f"Established business: {years} years in operation",
            "detail": (
                f"{company_name} has been in continuous operation for {years} years"
                f"{f' ({industry})' if industry else ''}{revenue_str}. "
                f"Long-term operational continuity demonstrates management stability and "
                f"reduces the risk of sudden business failure or operational inexperience."
            ),
        })

    if cov.get("multi_line"):
        line_count = cov.get("line_count", 0)
        positives.append({
            "rule": f"Multi-line opportunity: {line_count} lines requested",
            "detail": (
                f"{company_name} is requesting {line_count} lines of coverage: {coverages_detail}. "
                f"Multi-line accounts provide greater premium volume, deeper carrier relationship, "
                f"and reduced risk of adverse selection compared to monoline submissions. "
                f"This enhances the overall account attractiveness."
            ),
        })

    if prop.get("sprinklered_count", 0) == prop.get("location_count", 1) and prop.get("location_count", 0) > 0:
        positives.append({
            "rule": "All locations fully sprinklered",
            "detail": (
                f"All {prop.get('location_count')} locations are fully sprinklered — "
                f"reduces fire loss severity and frequency, directly improving loss ratio expectations."
            ),
        })

    if claim_trend == "declining":
        positives.append({
            "rule": "Claim frequency is declining trend",
            "detail": f"Claim frequency for {company_name} shows a declining trend — recent years have fewer losses than earlier periods, indicating improving risk management.",
        })

    if clean_years >= 5:
        positives.append({
            "rule": f"Strong clean history: {clean_years} years without claims",
            "detail": f"{company_name} has {clean_years} years with zero claims in the policy period — strong indicator of operational discipline and low baseline risk.",
        })

    if any(kw in notes_lower for kw in ["tips certified", "safety program", "osha", "safety manager"]):
        positives.append({
            "rule": "Formal safety program or certification in place",
            "detail": f"Broker notes reference a formal safety program or certification — demonstrates proactive risk management beyond minimum compliance requirements.",
        })

    if any(kw in notes_lower for kw in ["camera", "security system", "guard", "monitored"]):
        positives.append({
            "rule": "Enhanced security measures in place",
            "detail": f"Security systems referenced in broker notes — reduces theft, vandalism, and premises liability exposure.",
        })

    if any(kw in notes_lower for kw in ["emr", "experience mod"]):
        import re
        # Extract current EMR value from broker notes
        emr_match = re.search(r'emr\s+(?:is\s+|of\s+|was\s+|:\s*)?(\d+\.\d+)', notes_lower)
        emr_prev_match = re.search(r'(\d+\.\d+)\s+(?:two|2|last)\s+year', notes_lower)
        emr_value = emr_match.group(1) if emr_match else None
        emr_prev = emr_prev_match.group(1) if emr_prev_match else None

        if emr_value:
            trend_text = f", down from {emr_prev} two years ago" if emr_prev else ""
            status_text = "above average vs industry peers" if float(emr_value) > 1.0 else "below average — favorable safety performance"
            detail = (
                f"{company_name} has a current EMR of {emr_value}{trend_text}. "
                f"An EMR of {emr_value} is {status_text}. "
                f"{'Downward trend from ' + emr_prev + ' indicates improving safety management and claims control.' if emr_prev else 'Verify current trend direction with broker.'}"
            )
        else:
            detail = (
                f"Experience modification rate referenced in broker notes for {company_name}. "
                f"Verify exact EMR value — below 1.0 confirms favorable safety performance vs peers; "
                f"above 1.0 indicates above-average claims history."
            )

        positives.append({
            "rule": "Experience modification rate noted (check value)",
            "detail": detail,
        })

    # ══════════════════════════════════════════════════
    # MISSING INFO
    # ══════════════════════════════════════════════════

    completeness = _deep_scan_info_completeness(extraction)
    if not completeness["has_loss_history"]:
        missing.append("No loss runs provided — cannot verify claims history")
    if not completeness["has_prior_insurance"]:
        missing.append("No prior insurance information — cannot verify continuity")
    if not completeness["has_industry_class"]:
        missing.append("No NAICS/SIC code — cannot determine industry class")
    if not completeness["has_revenue"]:
        missing.append("No revenue provided — cannot assess business size")
    if not completeness["has_property_details"] and any(
        "property" in (c.coverage_type or "").lower() for c in extraction.coverages
    ):
        missing.append("No property details for property coverage request")

    # ══════════════════════════════════════════════════
    # MITIGATION-WEIGHTED DECISIONING
    # ══════════════════════════════════════════════════

    decline_triggers = [t for t in triggers if t["severity"] == "decline"]
    refer_triggers   = [t for t in triggers if t["severity"] == "refer"]

    if decline_triggers:
        base_weight = float(len(decline_triggers))
        mitigation_total = 0.0

        largest_incurred = largest_event.get("total_incurred", 0) if largest_event else 0
        if total_incurred > 0 and largest_incurred / total_incurred > 0.50:
            if not causation.get("systemic"):
                mitigation_total += 0.35

        if loss_ratio_ex_cat is not None and loss_ratio_ex_cat < 30:
            mitigation_total += 0.25

        if subrogation.get("potential"):
            mitigation_total += 0.15

        if suppression.get("effective"):
            mitigation_total += 0.10

        if clean_years >= 5:
            mitigation_total += 0.15
        elif clean_years >= 3:
            mitigation_total += 0.10

        if any(kw in notes_lower for kw in
               ["upgrade", "improvement", "suppression installed", "electrical upgrade",
                "thermal imaging", "protocol", "power-down", "retrofit"]):
            mitigation_total += 0.10

        adjusted_weight = max(0.0, base_weight - mitigation_total)

        log.info("mitigation_weighted_decision",
            decline_triggers=len(decline_triggers),
            base_weight=base_weight,
            mitigation_total=round(mitigation_total, 2),
            adjusted_weight=round(adjusted_weight, 2))

        if adjusted_weight > 0.75:
            score = 1
            status = "decline"
            reasoning = (
                f"Decline triggers ({len(decline_triggers)}) outweigh mitigations. "
                f"Weight: {base_weight:.1f} - {mitigation_total:.2f} = {adjusted_weight:.2f}"
            )
        elif adjusted_weight > 0.35:
            score = 2
            status = "refer"
            reasoning = (
                f"Decline triggers present but significant mitigations reduce severity. "
                f"Adjusted weight {adjusted_weight:.2f}. Senior underwriter review required."
            )
        else:
            score = 3
            status = "refer_with_conditions"
            reasoning = (
                f"Decline triggers substantially mitigated. Adjusted weight {adjusted_weight:.2f}. "
                f"Likely approvable with conditions — isolated event, controls in place, "
                f"clean underlying history."
            )

    elif refer_triggers:
        override_count = len(overrides)
        refer_count    = len(refer_triggers)
        if override_count >= refer_count and not any(
            "non-renewal" in t["rule"].lower() for t in refer_triggers
        ):
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
        score = 5 if len(positives) >= 5 else 4
        status = "accept"
        reasoning = "Good risk profile with positive indicators"
    else:
        score = 3
        status = "review"
        reasoning = "Insufficient information for definitive assessment"

    # ══════════════════════════════════════════════════
    # WINNABILITY
    # ══════════════════════════════════════════════════

    winnability = 0.50

    if loss_ratio is not None:
        if loss_ratio > 200:
            winnability = 0.15
        elif loss_ratio > 100:
            winnability = 0.30
        elif loss_ratio > 50:
            winnability = 0.45
        elif loss_ratio > 30:
            winnability = 0.60
        else:
            winnability = 0.75
    elif total_events == 0:
        winnability = 0.85

    if carrier.get("non_renewal"):
        winnability -= 0.15
    if open_reserves > 100_000:
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
    if status == "refer_with_conditions":
        winnability += 0.10

    # Force low winnability for declines
    if status == "decline":
        winnability = min(winnability, 0.20)

    winnability = max(0.05, min(0.95, winnability))

    # ══════════════════════════════════════════════════
    # PRIORITY
    # ══════════════════════════════════════════════════

    priority = 0.50
    if cov.get("multi_line"):
        priority += 0.10
    if revenue > 5_000_000:
        priority += 0.10
    if loss.get("total_premium", 0) > 50_000:
        priority += 0.10
    if score >= 4:
        priority += 0.10
    if score <= 2:
        priority -= 0.10
    if carrier.get("non_renewal"):
        priority += 0.05
    priority = max(0.10, min(0.95, priority))

    # ── Flatten positives for pipeline compatibility ──
    # positives are now dicts with rule + detail
    # pipeline.py already handles them correctly via positives[i]["rule"] and ["detail"]
    # but old code expected plain strings — keep both formats available
    positives_for_output = positives  # list of dicts now

    result = {
        "score": score,
        "status": status,
        "reasoning": reasoning,
        "triggers": triggers,
        "overrides": overrides,
        "positives": positives_for_output,
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
        score=score, status=status,
        triggers=len(triggers), overrides=len(overrides),
        positives=len(positives), winnability=winnability, priority=priority)

    return result