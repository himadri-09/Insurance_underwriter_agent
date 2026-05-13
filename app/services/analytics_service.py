"""
Analytics service: pure Python calculations for underwriting metrics.

NO LLM calls. All math is deterministic.

Key fixes (v4):
  1. Premium: stated_total_premium from the loss run is used DIRECTLY as total_premium.
     No multiplication. No division. No stated_premium_is_annual check.
     The carrier already computed it over the correct period — trust it.
     Only falls back to estimation when no loss run premium exists.
  2. Gross incurred = sum of ALL deduplicated loss events.
     stated_total_incurred used only as double-count detector (sum ≈ 2× stated).
  3. Claims: deduplicates by date_of_loss — same-date claims = one loss event.
  4. Causation: classifies per EVENT, not per claim line.
"""

import re
import structlog
from datetime import datetime
from typing import Optional
from collections import defaultdict
from app.models.schemas import ExtractionResult

log = structlog.get_logger()


def compute_analytics(extraction: ExtractionResult) -> dict:
    analytics = {}

    losses = extraction.loss_history
    premiums = extraction.prior_insurance

    # ══════════════════════════════════════════════════
    # Step 1: Deduplicate claims by date → loss EVENTS
    # ══════════════════════════════════════════════════

    claims_with_source = []
    for loss in losses:
        claim_num = loss.claim_number or ""
        source = (loss.carrier or "").lower()
        priority = "loss_run" if (claim_num or source) else "other"

        claims_with_source.append({
            "date": loss.date_of_loss,
            "date_normalized": _normalize_date(loss.date_of_loss) or loss.date_of_loss or "unknown",
            "type": loss.type,
            "lob": loss.lob,
            "description": loss.description,
            "status": loss.status,
            "paid": loss.amount_paid,
            "reserved": loss.amount_reserved,
            "incurred": loss.incurred or (loss.amount_paid + loss.amount_reserved),
            "subrogation": loss.subrogation,
            "claim_number": claim_num,
            "source_priority": priority,
        })

    events_by_date = defaultdict(list)
    for claim in claims_with_source:
        events_by_date[claim["date_normalized"]].append(claim)

    loss_events = []
    for date_key, claims in events_by_date.items():
        loss_run_claims = [c for c in claims if c["source_priority"] == "loss_run"]
        other_claims    = [c for c in claims if c["source_priority"] != "loss_run"]
        authoritative_claims = loss_run_claims if loss_run_claims else other_claims

        event = {
            "date": date_key,
            "claim_count": len(authoritative_claims),
            "types": list(set(c["type"] for c in authoritative_claims if c["type"])),
            "lobs":  list(set(c["lob"]  for c in authoritative_claims if c["lob"])),
            "descriptions": [c["description"] for c in authoritative_claims if c["description"]],
            "statuses": [c["status"] for c in authoritative_claims],
            "total_paid":     sum(c["paid"]     for c in authoritative_claims),
            "total_reserved": sum(c["reserved"] for c in authoritative_claims),
            "total_incurred": sum(c["incurred"] for c in authoritative_claims),
            "is_open": any(
                c["status"] and c["status"].lower() in ("open", "reserved")
                for c in authoritative_claims
            ),
            "subrogation": any(c["subrogation"] for c in authoritative_claims),
            "subrogation_details": next(
                (c["subrogation"] for c in authoritative_claims if c["subrogation"]), ""
            ),
            "duplicates_skipped": len(other_claims) if loss_run_claims else 0,
        }
        loss_events.append(event)

    total_events  = len(loss_events)
    total_paid    = sum(e["total_paid"]     for e in loss_events)
    total_reserved = sum(e["total_reserved"] for e in loss_events)
    open_events   = [e for e in loss_events if e["is_open"]]
    open_reserves = sum(e["total_reserved"] for e in open_events)

    largest_event = max(loss_events, key=lambda e: e["total_incurred"]) if loss_events else None

    # ── Resolve total_incurred ────────────────────────────────────────────
    # Always sum individual records as the gross incurred.
    # Use stated_total_incurred ONLY to detect the double-count pattern (sum ≈ 2× stated).
    sum_from_records = sum(e["total_incurred"] for e in loss_events)
    stated_incurred  = extraction.stated_total_incurred or 0

    if stated_incurred > 0 and sum_from_records > 0:
        ratio_check = sum_from_records / stated_incurred
        if 1.7 < ratio_check < 2.3:
            # Sum is ~2× stated → summary row was also extracted as a claim (double-count)
            log.warning("double_count_detected",
                stated=stated_incurred, summed=sum_from_records,
                ratio=round(ratio_check, 2), action="using_stated_to_correct")
            total_incurred = stated_incurred
            incurred_source = "loss_run_stated_double_count_corrected"
        else:
            total_incurred = sum_from_records
            incurred_source = "summed_from_records"
    elif sum_from_records > 0:
        total_incurred = sum_from_records
        incurred_source = "summed_from_records"
    elif stated_incurred > 0:
        total_incurred = stated_incurred
        incurred_source = "loss_run_stated_only"
    else:
        total_incurred = 0
        incurred_source = "no_data"

    log.info("total_incurred_resolved",
        stated=stated_incurred, summed=sum_from_records,
        used=total_incurred, source=incurred_source)

    incurred_ex_largest = (
        total_incurred - largest_event["total_incurred"]
        if largest_event and len(loss_events) > 1
        else 0
    )

    # ── Loss period from claim dates ─────────────────────────────────────
    loss_years_set    = set(_extract_year(e["date"]) for e in loss_events if _extract_year(e["date"]))
    loss_period_start = min(loss_years_set) if loss_years_set else datetime.now().year
    loss_period_end   = max(loss_years_set) if loss_years_set else datetime.now().year
    loss_period_years = (loss_period_end - loss_period_start + 1) if loss_years_set else 1

    # ══════════════════════════════════════════════════
    # Step 2: Premium
    #
    # RULE: if the loss run stated a total premium, use it DIRECTLY as total_premium.
    # Do NOT treat it as annual. Do NOT multiply it. Do NOT divide it.
    # The carrier already computed it over the correct period.
    # stated_premium_is_annual is NOT used — it is unreliable because it can be
    # set by the ACORD (which refers to a different number) during the merge.
    #
    # Only estimate when no loss run premium exists.
    # ══════════════════════════════════════════════════

    stated_premium = extraction.stated_total_premium or 0

    if stated_premium > 0:
        # Loss run gave us the number. Use it as-is.
        total_premium    = stated_premium
        premium_source   = "loss_run_stated_direct"
        premium_confidence = "high"
        annual_premium   = stated_premium / max(loss_period_years, 1)  # for logging only

        log.info("premium_resolved",
            stated_premium=stated_premium,
            total_premium=total_premium,
            annual_implied=round(annual_premium, 2),
            premium_source=premium_source,
            premium_confidence=premium_confidence,
            incurred_source=incurred_source,
            total_incurred=total_incurred)

    else:
        # No loss run premium — estimate from prior_insurance records.
        # Deduplicate by year so multi-line policies in the same year don't double-count.
        prior_by_year = {}
        for p in premiums:
            if p.premium and p.premium > 0:
                yr  = _extract_year(p.effective_date) or _extract_year(p.expiration_date)
                key = yr or "unknown"
                if key not in prior_by_year or p.premium > prior_by_year[key]:
                    prior_by_year[key] = p.premium

        if prior_by_year:
            annual_premium   = sum(prior_by_year.values()) / len(prior_by_year)
            total_premium    = annual_premium * loss_period_years
            premium_source   = f"prior_insurance_avg_{len(prior_by_year)}yr_scaled"
            premium_confidence = "medium" if len(prior_by_year) >= 3 else "low"
        else:
            # Last resort — text scan. Treat result as annual and scale.
            all_text = (extraction.broker_notes or "") + " " + (extraction.business_description or "")
            for loss in losses:
                if loss.description:
                    all_text += " " + loss.description
            best_text = _extract_premium_from_text(all_text)

            if best_text > 0:
                annual_premium   = best_text
                total_premium    = annual_premium * loss_period_years
                premium_source   = "text_scan_scaled"
                premium_confidence = "low"
            else:
                annual_premium   = 0.0
                total_premium    = 0.0
                premium_source   = "unknown"
                premium_confidence = "none"

        log.info("premium_resolved",
            stated_premium=stated_premium,
            annual_premium=round(annual_premium, 2),
            loss_period_years=loss_period_years,
            total_premium=round(total_premium, 2),
            premium_source=premium_source,
            premium_confidence=premium_confidence,
            incurred_source=incurred_source,
            total_incurred=total_incurred)

    # ── Loss ratios ──────────────────────────────────────────────────────
    loss_ratio          = (total_incurred / total_premium * 100) if total_premium > 0 else None
    loss_ratio_ex_largest = (incurred_ex_largest / total_premium * 100) if total_premium > 0 else None

    # ══════════════════════════════════════════════════
    # Step 3: Frequency and trend
    # ══════════════════════════════════════════════════

    events_by_year   = {}
    severity_by_year = {}
    for e in loss_events:
        year = _extract_year(e["date"])
        if year:
            events_by_year[year]   = events_by_year.get(year, 0) + 1
            severity_by_year[year] = severity_by_year.get(year, 0) + e["total_incurred"]

    sorted_years = sorted(events_by_year.keys())

    if len(sorted_years) >= 2:
        mid         = len(sorted_years) // 2
        recent_freq = sum(events_by_year[y] for y in sorted_years[mid:])
        older_freq  = sum(events_by_year[y] for y in sorted_years[:mid])
        frequency_trend = (
            "declining"  if recent_freq < older_freq else
            "increasing" if recent_freq > older_freq else
            "stable"
        )
    elif len(sorted_years) == 1:
        frequency_trend = "single_year_data"
    else:
        frequency_trend = "no_claims"

    if len(sorted_years) >= 2 and largest_event:
        avg_severity_ex_largest = incurred_ex_largest / max(total_events - 1, 1)
        if largest_event["total_incurred"] > avg_severity_ex_largest * 5:
            severity_trend = "severity_spike"
        else:
            recent_sev = sum(severity_by_year.get(y, 0) for y in sorted_years[len(sorted_years)//2:])
            older_sev  = sum(severity_by_year.get(y, 0) for y in sorted_years[:len(sorted_years)//2])
            severity_trend = (
                "increasing" if recent_sev > older_sev * 1.5 else
                "declining"  if recent_sev < older_sev * 0.5 else
                "stable"
            )
    else:
        severity_trend = "insufficient_data"

    if frequency_trend == "stable" and severity_trend == "severity_spike":
        claim_trend = "stable_frequency_severity_spike"
    elif frequency_trend in ("declining", "increasing"):
        claim_trend = frequency_trend
    else:
        claim_trend = frequency_trend

    if sorted_years:
        all_years   = set(range(min(sorted_years), max(sorted_years) + 1))
        clean_years = len(all_years - set(sorted_years))
    else:
        clean_years = extraction.company.years_in_business or 0

    analytics["loss"] = {
        "total_events":          total_events,
        "total_claim_lines":     len(losses),
        "total_incurred":        total_incurred,
        "incurred_source":       incurred_source,
        "total_paid":            total_paid,
        "total_reserved":        total_reserved,
        "annual_premium":        round(annual_premium if stated_premium == 0 else total_premium / max(loss_period_years, 1), 2),
        "total_premium":         round(total_premium, 2),
        "premium_source":        premium_source,
        "premium_confidence":    premium_confidence,
        "loss_period_years":     loss_period_years,
        "loss_period_start":     loss_period_start,
        "loss_period_end":       loss_period_end,
        "loss_ratio_pct":        round(loss_ratio, 1)          if loss_ratio          is not None else None,
        "loss_ratio_ex_largest_pct": round(loss_ratio_ex_largest, 1) if loss_ratio_ex_largest is not None else None,
        "normalized_loss_ratio_pct": round(loss_ratio_ex_largest, 1) if loss_ratio_ex_largest is not None else None,
        "shock_loss_present":    (
            largest_event["total_incurred"] > total_incurred * 0.5
            if largest_event and total_incurred > 0 else False
        ),
        "incurred_ex_largest":   incurred_ex_largest,
        "open_events_count":     len(open_events),
        "open_reserves":         open_reserves,
        "largest_event":         largest_event,
        "events_by_year":        events_by_year,
        "claim_trend":           claim_trend,
        "frequency_trend":       frequency_trend,
        "severity_trend":        severity_trend,
        "clean_years":           clean_years,
    }

    # ══════════════════════════════════════════════════
    # Step 4: Subrogation potential
    # ══════════════════════════════════════════════════

    notes_lower = (extraction.broker_notes or "").lower()

    subrogation = False
    subrogation_details = ""
    for event in loss_events:
        if event["subrogation"]:
            subrogation = True
            subrogation_details = event["subrogation_details"]
            break
        combined_desc = " ".join(event["descriptions"]).lower()
        if any(kw in combined_desc for kw in
               ["subrogation", "recovery", "contractor", "defective", "manufacturer", "third party"]):
            subrogation = True
            subrogation_details = event["descriptions"][0] if event["descriptions"] else ""

    if "subrogation" in notes_lower or "recovery against" in notes_lower:
        subrogation = True
        if not subrogation_details:
            subrogation_details = "Mentioned in broker notes"

    analytics["subrogation"] = {"potential": subrogation, "details": subrogation_details[:300]}

    # ══════════════════════════════════════════════════
    # Step 5: Suppression effectiveness
    # ══════════════════════════════════════════════════

    suppression_effective = False
    suppression_details   = ""
    for event in loss_events:
        combined_desc = " ".join(event["descriptions"]).lower()
        if any(kw in combined_desc for kw in
               ["sprinkler", "suppression", "contained", "activated", "prevented total loss"]):
            suppression_effective = True
            suppression_details   = event["descriptions"][0] if event["descriptions"] else ""

    analytics["suppression"] = {"effective": suppression_effective, "details": suppression_details[:300]}

    # ══════════════════════════════════════════════════
    # Step 6: Causation — per EVENT, not per claim line
    # ══════════════════════════════════════════════════

    causation_types = []
    for event in loss_events:
        combined_desc  = " ".join(event["descriptions"]).lower()
        combined_types = " ".join(event["types"]).lower()

        if any(kw in combined_desc for kw in ["electrical", "wiring", "panel", "arc"]):
            causation_types.append("isolated_electrical")
        elif any(kw in combined_desc for kw in ["arson", "intentional"]):
            causation_types.append("arson")
        elif any(kw in combined_desc for kw in ["grease", "cooking", "kitchen", "fryer"]):
            causation_types.append("kitchen_fire")
        elif any(kw in combined_desc for kw in ["slip", "fall", "trip", "wet"]):
            causation_types.append("premises_liability")
        elif any(kw in combined_desc for kw in ["theft", "burglary", "stolen", "break-in"]):
            causation_types.append("theft")
        elif any(kw in combined_desc for kw in ["liquor", "alcohol", "over-served", "dram shop"]):
            causation_types.append("liquor_liability")
        elif any(kw in combined_desc for kw in ["collision", "vehicle", "auto", "backing"]):
            causation_types.append("auto")
        elif any(kw in combined_desc for kw in ["scaffold", "fall from", "height"]):
            causation_types.append("construction_fall")
        elif any(kw in combined_desc for kw in ["heat", "exhaustion", "dehydration"]):
            causation_types.append("heat_exposure")
        elif any(kw in combined_desc for kw in ["strain", "lifting", "back"]):
            causation_types.append("ergonomic")
        elif any(kw in combined_desc for kw in ["cut", "laceration", "saw"]):
            causation_types.append("tool_injury")
        elif "fire" in combined_desc or "fire" in combined_types:
            causation_types.append("fire_other")
        else:
            causation_types.append("other")

    type_counts    = defaultdict(int)
    for ct in causation_types:
        type_counts[ct] += 1
    repeated_types = [t for t, c in type_counts.items() if c > 1]

    analytics["causation"] = {
        "types":          causation_types,
        "unique_types":   list(set(causation_types)),
        "systemic":       len(repeated_types) > 0,
        "repeated_types": repeated_types,
        "isolated":       len(repeated_types) == 0,
    }

    # ══════════════════════════════════════════════════
    # Step 7: Carrier non-renewal
    # ══════════════════════════════════════════════════

    non_renewal = False
    non_renewal_carriers_raw = []
    for pi in premiums:
        if pi.cancelled_by_carrier:
            non_renewal = True
            non_renewal_carriers_raw.append(pi.carrier)

    if any(kw in notes_lower for kw in
           ["non-renew", "nonrenew", "non-renewal", "cancelled by", "will not renew"]):
        non_renewal = True

    if non_renewal and not non_renewal_carriers_raw:
        for carrier_name in ["Hartford", "Travelers", "Zurich", "EMC", "Progressive",
                              "Texas Mutual", "Liberty Mutual", "Chubb", "AIG", "Employers Mutual"]:
            if carrier_name.lower() in notes_lower:
                non_renewal_carriers_raw.append(carrier_name)

    non_renewal_carriers = list(set(_normalize_carrier(c) for c in non_renewal_carriers_raw if c))

    analytics["carrier"] = {
        "non_renewal":       non_renewal,
        "non_renewal_count": len(non_renewal_carriers),
        "non_renewal_carriers": non_renewal_carriers,
    }

    # ══════════════════════════════════════════════════
    # Step 8: Business, property, coverage
    # ══════════════════════════════════════════════════

    revenue   = extraction.company.annual_revenue or 0
    payroll   = extraction.company.annual_payroll  or 0
    headcount = extraction.company.headcount        or 0
    years     = extraction.company.years_in_business or 0

    analytics["business"] = {
        "revenue":          revenue,
        "payroll":          payroll,
        "headcount":        headcount,
        "years_in_business": years,
        "naics":            extraction.company.naics_code,
        "sic":              extraction.company.sic_code,
        "state":            extraction.company.state,
        "entity_type":      extraction.company.entity_type,
    }

    locations           = extraction.locations
    total_tiv           = sum(
        (loc.building_value or 0) + (loc.contents_value or 0) + (loc.bi_value or 0)
        for loc in locations
    )
    sprinklered_count   = sum(1 for loc in locations if loc.sprinklered)
    unsprinklered_count = len(locations) - sprinklered_count
    oldest_building     = min((loc.year_built for loc in locations if loc.year_built), default=None)

    analytics["property"] = {
        "location_count":      len(locations),
        "total_tiv":           total_tiv,
        "sprinklered_count":   sprinklered_count,
        "unsprinklered_count": unsprinklered_count,
        "oldest_building_year": oldest_building,
    }

    lines_requested = list(set(c.coverage_type for c in extraction.coverages if c.coverage_type))
    max_limit       = max((c.limit or 0 for c in extraction.coverages), default=0)

    analytics["coverage"] = {
        "lines_requested": lines_requested,
        "line_count":      len(lines_requested),
        "max_single_limit": max_limit,
        "multi_line":      len(lines_requested) > 1,
    }

    log.info("analytics_computed",
        loss_ratio=analytics["loss"]["loss_ratio_pct"],
        loss_ratio_ex_cat=analytics["loss"]["loss_ratio_ex_largest_pct"],
        total_events=total_events,
        total_claim_lines=len(losses),
        total_incurred=total_incurred,
        incurred_source=incurred_source,
        total_premium=total_premium,
        premium_source=premium_source,
        loss_period_years=loss_period_years,
        open_events=len(open_events),
        subrogation=subrogation,
        non_renewal=non_renewal,
        systemic=analytics["causation"]["systemic"],
    )

    return analytics


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _extract_premium_from_text(text: str) -> float:
    """Extract a premium number from free text. Returns a single float."""
    patterns = [
        r'total\s+(?:annual\s+)?premium[:\s]*\$?([\d,]+)',
        r'total\s+current[:\s]*~?\$?([\d,]+)',
        r'premium\s+(?:paid|total)[:\s]*\$?([\d,]+)',
        r'~\$?([\d,]+)\s+(?:total|in total|premium)',
        r'total\s+premium\s+(?:paid|charged)[:\s]*\$?([\d,]+)',
        r'estimated\s+total\s+premium[:\s]*\$?([\d,]+)',
        r'loss\s+ratio.*?(?:premium|premiums)[:\s]*\$?([\d,]+)',
        r'total\s+all\s+carriers[:\s]*~?\$?([\d,]+)',
    ]
    best = 0.0
    for pattern in patterns:
        for m in re.findall(pattern, text.lower()):
            try:
                val = float(m.replace(",", ""))
                if 1000 <= val <= 10_000_000 and val > best:
                    best = val
            except ValueError:
                pass
    return best


def _scan_dict_for_premium(d: dict, depth: int = 0) -> float:
    """Recursively scan a dict for premium-related values."""
    if depth > 5:
        return 0.0
    best = 0.0
    premium_keys = {
        "total_premium", "premium", "total_premium_paid", "premium_paid",
        "estimated_total_premium", "total_premiums", "annual_premium",
    }
    for key, val in d.items():
        key_lower = key.lower().replace(" ", "_")
        if key_lower in premium_keys:
            if isinstance(val, (int, float)) and 1000 <= val <= 10_000_000:
                best = max(best, float(val))
            elif isinstance(val, str):
                try:
                    num = float(val.replace(",", "").replace("$", ""))
                    if 1000 <= num <= 10_000_000:
                        best = max(best, num)
                except ValueError:
                    pass
        elif isinstance(val, dict):
            best = max(best, _scan_dict_for_premium(val, depth + 1))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    best = max(best, _scan_dict_for_premium(item, depth + 1))
    return best


def _extract_year(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(date_str.strip(), fmt).year
            except ValueError:
                continue
        match = re.search(r"20\d{2}", date_str)
        if match:
            return int(match.group())
    except Exception:
        pass
    return None


def _normalize_date(date_str: Optional[str]) -> Optional[str]:
    """Normalize date to YYYY-MM-DD for consistent grouping."""
    if not date_str:
        return None
    try:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y"):
            try:
                return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        match = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", date_str)
        if match:
            m, d, y = match.groups()
            if len(y) == 2:
                y = "20" + y
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    except Exception:
        pass
    return date_str.strip()


def _normalize_carrier(name: str) -> str:
    """Normalize carrier names so Hartford Financial Services == Hartford."""
    if not name:
        return name
    suffixes = [
        " Financial Services Group", " Financial Services",
        " Insurance Company", " Insurance Companies", " Insurance Group",
        " Insurance Co", " North America", " Group", " Inc.", " Inc",
        " LLC", " Corp", " Corporation", " Company", " Companies", " Mutual",
    ]
    normalized = name.strip()
    for suffix in suffixes:
        if normalized.lower().endswith(suffix.lower()):
            normalized = normalized[: -len(suffix)].strip()
    aliases = {
        "the hartford": "Hartford",
        "hartford":     "Hartford",
        "travelers":    "Travelers",
        "zurich":       "Zurich",
        "emc":          "EMC",
        "employers mutual": "EMC",
        "progressive commercial": "Progressive",
        "progressive":  "Progressive",
        "texas mutual": "Texas Mutual",
    }
    return aliases.get(normalized.lower(), normalized)