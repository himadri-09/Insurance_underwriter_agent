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


def evaluate_rules(analytics: dict, extraction: ExtractionResult) -> dict:
    """
    Apply underwriting rules to pre-computed analytics.
    Returns triggers, overrides, positives, and final recommendation.
    """
    triggers  = []   # negative signals (fired rules)
    positives = []   # positive signals
    overrides = []   # mitigating factors
    missing   = []   # critical missing info

    loss       = analytics.get("loss", {})
    biz        = analytics.get("business", {})
    carrier    = analytics.get("carrier", {})
    prop       = analytics.get("property", {})
    cov        = analytics.get("coverage", {})
    subrogation = analytics.get("subrogation", {})
    suppression = analytics.get("suppression", {})
    causation   = analytics.get("causation", {})

    loss_ratio       = loss.get("loss_ratio_pct")
    loss_ratio_ex_cat = loss.get("loss_ratio_ex_largest_pct")
    total_incurred   = loss.get("total_incurred", 0)
    incurred_ex_largest = loss.get("incurred_ex_largest", 0)
    total_events     = loss.get("total_events", 0)
    open_events      = loss.get("open_events_count", 0)
    open_reserves    = loss.get("open_reserves", 0)
    claim_trend      = loss.get("claim_trend", "")
    clean_years      = loss.get("clean_years", 0)
    shock_loss       = loss.get("shock_loss_present", False)
    largest_event    = loss.get("largest_event") or {}

    revenue   = biz.get("revenue", 0)
    years     = biz.get("years_in_business", 0)

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
                f"total premium ${loss.get('total_premium', 0):,.0f})"
            ),
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
    # REFERRAL TRIGGERS
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

    if open_reserves > 100_000:
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

    if revenue > 50_000_000:
        triggers.append({
            "rule": "REFER: Revenue exceeds $50M (large account)",
            "severity": "refer",
            "detail": f"Revenue: ${revenue:,.0f}",
        })

    if cov.get("max_single_limit", 0) > 5_000_000:
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
            "detail": f"Loss ratio excluding largest claim: {loss_ratio_ex_cat:.1f}% (incurred ${incurred_ex_largest:,.0f})",
        })

    if subrogation.get("potential"):
        overrides.append({
            "rule": "OVERRIDE: Subrogation recovery potential",
            "detail": f"May reduce net incurred. Details: {subrogation.get('details', '')[:200]}",
        })

    if suppression.get("effective"):
        overrides.append({
            "rule": "OVERRIDE: Fire suppression system worked as designed",
            "detail": "Prevented total loss. Controls functioned correctly.",
        })

    if not causation.get("systemic") and total_events >= 1:
        overrides.append({
            "rule": "OVERRIDE: Loss causes are isolated (not systemic pattern)",
            "detail": f"Causation types: {', '.join(causation.get('unique_types', []))}. No repeated patterns.",
        })

    if clean_years >= 3:
        overrides.append({
            "rule": "OVERRIDE: Multiple clean years in history",
            "detail": f"{clean_years} years with no claims — demonstrates operational control.",
        })

    # ══════════════════════════════════════════════════
    # POSITIVES
    # ══════════════════════════════════════════════════

    if years >= 3:
        positives.append(f"Established business: {years} years in operation")
    if cov.get("multi_line"):
        positives.append(f"Multi-line opportunity: {cov.get('line_count', 0)} lines requested")
    if prop.get("sprinklered_count", 0) == prop.get("location_count", 1) and prop.get("location_count", 0) > 0:
        positives.append("All locations fully sprinklered")
    if claim_trend == "declining":
        positives.append("Claim frequency is declining trend")
    if clean_years >= 5:
        positives.append(f"Strong clean history: {clean_years} years without claims")

    notes_lower = (extraction.broker_notes or "").lower()
    if any(kw in notes_lower for kw in ["tips certified", "safety program", "osha", "safety manager"]):
        positives.append("Formal safety program or certification in place")
    if any(kw in notes_lower for kw in ["camera", "security system", "guard", "monitored"]):
        positives.append("Enhanced security measures in place")
    if any(kw in notes_lower for kw in ["emr", "experience mod"]):
        positives.append("Experience modification rate noted (check value)")

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
    #
    # Instead of: loss_ratio > 200 → always decline
    # We compute: decline_weight - mitigation_credits → final status
    #
    # Each hard decline trigger has weight 1.0.
    # Each mitigation credit reduces that weight.
    # Adjusted weight maps to: decline / refer / refer_with_conditions.
    #
    # This correctly handles:
    #   - BrightTech: 1 decline trigger, 4 mitigations → refer_with_conditions
    #   - Fuego: 1 decline trigger, 2 mitigations, 2 carrier non-renewals → refer
    #   - Truly bad risk: 3 decline triggers, no mitigations → decline
    # ══════════════════════════════════════════════════

    decline_triggers = [t for t in triggers if t["severity"] == "decline"]
    refer_triggers   = [t for t in triggers if t["severity"] == "refer"]

    if decline_triggers:
        # Base weight: 1.0 per hard decline trigger
        base_weight = float(len(decline_triggers))

        # Mitigation credits — each meaningful factor reduces the weight
        mitigation_total = 0.0

        # Isolated catastrophic event (largest > 50% of total, not systemic)
        largest_incurred = largest_event.get("total_incurred", 0) if largest_event else 0
        if total_incurred > 0 and largest_incurred / total_incurred > 0.50:
            if not causation.get("systemic"):
                # Catastrophic but isolated — significant credit
                mitigation_total += 0.35

        # Excellent underlying ratio (ex-largest)
        if loss_ratio_ex_cat is not None and loss_ratio_ex_cat < 30:
            mitigation_total += 0.25

        # Subrogation potential reduces net exposure
        if subrogation.get("potential"):
            mitigation_total += 0.15

        # Suppression worked — controls functioned correctly
        if suppression.get("effective"):
            mitigation_total += 0.10

        # Clean years — good historical track record
        if clean_years >= 5:
            mitigation_total += 0.15
        elif clean_years >= 3:
            mitigation_total += 0.10

        # Improvements documented in broker notes
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

        # Map adjusted weight to final status
        # adjusted_weight > 0.75  → still a firm decline
        # adjusted_weight 0.35–0.75 → refer (needs senior review)
        # adjusted_weight < 0.35  → refer_with_conditions (likely salvageable)
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

    # Adjustments
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
    # Upgrade for refer_with_conditions — we're likely to write this
    if status == "refer_with_conditions":
        winnability += 0.10

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
        score=score, status=status,
        triggers=len(triggers), overrides=len(overrides),
        positives=len(positives), winnability=winnability, priority=priority)

    return result