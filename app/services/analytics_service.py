"""
Analytics service: pure Python calculations for underwriting metrics.

NO LLM calls. All math is deterministic.

Key fixes:
  1. Premium: estimates multi-year premium from prior_insurance + years_in_business,
     also scans broker_notes and raw_fields for stated premium totals.
  2. Claims: deduplicates by date_of_loss — same-date claims = one loss event.
  3. Causation: classifies per loss EVENT (not per claim line), so fire + BI = one event.
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
    # Fire + BI on same date = 1 event, not 2 claims
    # SOURCE PRIORITY: loss_run > prior_insurance > everything else
    # If same date appears from multiple document sources, use loss_run values only
    # ══════════════════════════════════════════════════

    # First pass: collect claims with source tracking
    claims_with_source = []
    for loss in losses:
        source = (loss.carrier or "").lower()
        claim_num = loss.claim_number or ""
        # Detect source priority from claim number prefixes or carrier field
        if claim_num or source:
            priority = "loss_run"  # claims with claim numbers are from loss runs
        else:
            priority = "other"  # extracted from broker notes, fire reports, etc

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

    # Second pass: group by date, prefer loss_run source
    events_by_date = defaultdict(list)
    for claim in claims_with_source:
        events_by_date[claim["date_normalized"]].append(claim)

    # Third pass: for each date group, use loss_run claims if available
    # Skip "other" source claims if loss_run claims exist for same date
    loss_events = []
    for date_key, claims in events_by_date.items():
        loss_run_claims = [c for c in claims if c["source_priority"] == "loss_run"]
        other_claims = [c for c in claims if c["source_priority"] != "loss_run"]

        # Use loss_run claims if available, otherwise fall back to other
        authoritative_claims = loss_run_claims if loss_run_claims else other_claims

        event = {
            "date": date_key,
            "claim_count": len(authoritative_claims),
            "types": list(set(c["type"] for c in authoritative_claims if c["type"])),
            "lobs": list(set(c["lob"] for c in authoritative_claims if c["lob"])),
            "descriptions": [c["description"] for c in authoritative_claims if c["description"]],
            "statuses": [c["status"] for c in authoritative_claims],
            "total_paid": sum(c["paid"] for c in authoritative_claims),
            "total_reserved": sum(c["reserved"] for c in authoritative_claims),
            "total_incurred": sum(c["incurred"] for c in authoritative_claims),
            "is_open": any(c["status"] and c["status"].lower() in ("open", "reserved") for c in authoritative_claims),
            "subrogation": any(c["subrogation"] for c in authoritative_claims),
            "subrogation_details": next((c["subrogation"] for c in authoritative_claims if c["subrogation"]), ""),
            "duplicates_skipped": len(other_claims) if loss_run_claims else 0,
        }
        loss_events.append(event)

    total_events = len(loss_events)
    total_incurred = sum(e["total_incurred"] for e in loss_events)
    total_paid = sum(e["total_paid"] for e in loss_events)
    total_reserved = sum(e["total_reserved"] for e in loss_events)
    open_events = [e for e in loss_events if e["is_open"]]
    open_reserves = sum(e["total_reserved"] for e in open_events)

    # Largest loss event
    largest_event = max(loss_events, key=lambda e: e["total_incurred"]) if loss_events else None

    # Incurred excluding largest event
    if largest_event and len(loss_events) > 1:
        incurred_ex_largest = total_incurred - largest_event["total_incurred"]
    else:
        incurred_ex_largest = 0

    # ══════════════════════════════════════════════════
    # Step 2: Premium — use AUTHORITATIVE source first
    # Priority: stated_total_premium > text scan > broker notes > annualized
    # ══════════════════════════════════════════════════

    # Source 0: AUTHORITATIVE — stated_total_premium from loss run summary
    premium_authoritative = extraction.stated_total_premium

    # Source 1: Sum from prior_insurance records (current year only)
    premium_from_records = sum(p.premium or 0 for p in premiums)

    # Source 2: Scan broker notes
    premium_from_notes = _extract_premium_from_text(extraction.broker_notes or "")

    # Source 3: Scan raw_fields recursively
    premium_from_raw = 0
    for field in extraction.raw_fields:
        if isinstance(field.value, dict):
            premium_from_raw = max(premium_from_raw, _scan_dict_for_premium(field.value))
        elif isinstance(field.value, str):
            premium_from_raw = max(premium_from_raw, _extract_premium_from_text(field.value))

    # Source 4: Deep text scan
    all_text = (extraction.broker_notes or "")
    for loss in losses:
        if loss.description:
            all_text += " " + loss.description
    all_text += " " + (extraction.business_description or "")
    premium_from_all_text = _extract_premium_from_text(all_text)

    # Estimate multi-year from single-year
    years_of_history = len(set(_extract_year(e["date"]) for e in loss_events if _extract_year(e["date"])))
    years_of_history = max(years_of_history, 1)

    # PICK BEST — authoritative first, then largest credible
    if premium_authoritative > 0:
        total_premium = premium_authoritative
        premium_source = "loss_run_stated"
    else:
        all_sources = [
            (premium_from_raw, "raw_field_scan"),
            (premium_from_notes, "broker_notes"),
            (premium_from_all_text, "text_scan"),
        ]
        best_ext = max(all_sources, key=lambda x: x[0])
        if best_ext[0] > premium_from_records:
            total_premium = best_ext[0]
            premium_source = best_ext[1]
        elif premium_from_records > 0:
            estimated_years = max(years_of_history, 3)
            total_premium = premium_from_records * estimated_years
            premium_source = f"estimated_{estimated_years}yr"
        else:
            total_premium = 0
            premium_source = "unknown"

    # Loss ratios
    loss_ratio = (total_incurred / total_premium * 100) if total_premium > 0 else None
    loss_ratio_ex_largest = (incurred_ex_largest / total_premium * 100) if total_premium > 0 else None

    # ══════════════════════════════════════════════════
    # Step 3: Claim frequency and trend (by EVENT, not claim line)
    # ══════════════════════════════════════════════════

    events_by_year = {}
    severity_by_year = {}
    for e in loss_events:
        year = _extract_year(e["date"])
        if year:
            events_by_year[year] = events_by_year.get(year, 0) + 1
            severity_by_year[year] = severity_by_year.get(year, 0) + e["total_incurred"]

    sorted_years = sorted(events_by_year.keys())

    # Frequency trend
    if len(sorted_years) >= 2:
        mid = len(sorted_years) // 2
        recent_freq = sum(events_by_year[y] for y in sorted_years[mid:])
        older_freq = sum(events_by_year[y] for y in sorted_years[:mid])
        if recent_freq < older_freq:
            frequency_trend = "declining"
        elif recent_freq > older_freq:
            frequency_trend = "increasing"
        else:
            frequency_trend = "stable"
    elif len(sorted_years) == 1:
        frequency_trend = "single_year_data"
    else:
        frequency_trend = "no_claims"

    # Severity trend
    if len(sorted_years) >= 2 and largest_event:
        largest_year = _extract_year(largest_event["date"])
        avg_severity_ex_largest = incurred_ex_largest / max(total_events - 1, 1)
        if largest_event["total_incurred"] > avg_severity_ex_largest * 5:
            severity_trend = "severity_spike"
        else:
            recent_sev = sum(severity_by_year.get(y, 0) for y in sorted_years[len(sorted_years)//2:])
            older_sev = sum(severity_by_year.get(y, 0) for y in sorted_years[:len(sorted_years)//2])
            if recent_sev > older_sev * 1.5:
                severity_trend = "increasing"
            elif recent_sev < older_sev * 0.5:
                severity_trend = "declining"
            else:
                severity_trend = "stable"
    else:
        severity_trend = "insufficient_data"

    # Combined trend description
    if frequency_trend == "stable" and severity_trend == "severity_spike":
        claim_trend = "stable_frequency_severity_spike"
    elif frequency_trend == "declining":
        claim_trend = "declining"
    elif frequency_trend == "increasing":
        claim_trend = "increasing"
    else:
        claim_trend = frequency_trend

    # Clean years
    if sorted_years:
        all_years = set(range(min(sorted_years), max(sorted_years) + 1))
        claim_years = set(sorted_years)
        clean_years = len(all_years - claim_years)
    else:
        clean_years = extraction.company.years_in_business or 0

    analytics["loss"] = {
        "total_events": total_events,
        "total_claim_lines": len(losses),
        "total_incurred": total_incurred,
        "total_paid": total_paid,
        "total_reserved": total_reserved,
        "total_premium": total_premium,
        "premium_source": premium_source,
        "premium_confidence": "high" if premium_source in ("loss_run_stated", "broker_notes") else "low",
        "loss_ratio_pct": round(loss_ratio, 1) if loss_ratio is not None else None,
        "loss_ratio_ex_largest_pct": round(loss_ratio_ex_largest, 1) if loss_ratio_ex_largest is not None else None,
        "normalized_loss_ratio_pct": round(loss_ratio_ex_largest, 1) if loss_ratio_ex_largest is not None else None,
        "shock_loss_present": (largest_event["total_incurred"] > total_incurred * 0.5) if largest_event and total_incurred > 0 else False,
        "incurred_ex_largest": incurred_ex_largest,
        "open_events_count": len(open_events),
        "open_reserves": open_reserves,
        "largest_event": largest_event,
        "events_by_year": events_by_year,
        "claim_trend": claim_trend,
        "frequency_trend": frequency_trend,
        "severity_trend": severity_trend,
        "clean_years": clean_years,
    }

    # ══════════════════════════════════════════════════
    # Step 4: Subrogation potential
    # ══════════════════════════════════════════════════

    subrogation = False
    subrogation_details = ""
    for event in loss_events:
        if event["subrogation"]:
            subrogation = True
            subrogation_details = event["subrogation_details"]
            break
        combined_desc = " ".join(event["descriptions"]).lower()
        if any(kw in combined_desc for kw in ["subrogation", "recovery", "contractor", "defective", "manufacturer", "third party"]):
            subrogation = True
            subrogation_details = event["descriptions"][0] if event["descriptions"] else ""

    # Also check broker notes
    notes_lower = (extraction.broker_notes or "").lower()
    if "subrogation" in notes_lower or "recovery against" in notes_lower:
        subrogation = True
        if not subrogation_details:
            subrogation_details = "Mentioned in broker notes"

    analytics["subrogation"] = {
        "potential": subrogation,
        "details": subrogation_details[:300],
    }

    # ══════════════════════════════════════════════════
    # Step 5: Suppression effectiveness
    # ══════════════════════════════════════════════════

    suppression_effective = False
    suppression_details = ""
    for event in loss_events:
        combined_desc = " ".join(event["descriptions"]).lower()
        if any(kw in combined_desc for kw in ["sprinkler", "suppression", "contained", "activated", "prevented total loss"]):
            suppression_effective = True
            suppression_details = event["descriptions"][0] if event["descriptions"] else ""

    analytics["suppression"] = {
        "effective": suppression_effective,
        "details": suppression_details[:300],
    }

    # ══════════════════════════════════════════════════
    # Step 6: Causation classification — per EVENT, not per claim line
    # ══════════════════════════════════════════════════

    causation_types = []
    for event in loss_events:
        combined_desc = " ".join(event["descriptions"]).lower()
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

    # Systemic = same causation type appears more than once
    type_counts = defaultdict(int)
    for ct in causation_types:
        type_counts[ct] += 1
    repeated_types = [t for t, c in type_counts.items() if c > 1]

    analytics["causation"] = {
        "types": causation_types,
        "unique_types": list(set(causation_types)),
        "systemic": len(repeated_types) > 0,
        "repeated_types": repeated_types,
        "isolated": len(repeated_types) == 0,
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

    if any(kw in notes_lower for kw in ["non-renew", "nonrenew", "non-renewal", "cancelled by", "will not renew"]):
        non_renewal = True

    # Try to extract carrier name from notes
    if non_renewal and not non_renewal_carriers_raw:
        for carrier_name in ["Hartford", "Travelers", "Zurich", "EMC", "Progressive", "Texas Mutual", "Liberty Mutual", "Chubb", "AIG"]:
            if carrier_name.lower() in notes_lower:
                non_renewal_carriers_raw.append(carrier_name)

    # NORMALIZE carrier names — strip suffixes and de-duplicate
    # Hartford Financial Services + Hartford Financial Services Group = Hartford
    non_renewal_carriers = list(set(_normalize_carrier(c) for c in non_renewal_carriers_raw if c))
    non_renewal_count = len(non_renewal_carriers)

    analytics["carrier"] = {
        "non_renewal": non_renewal,
        "non_renewal_count": non_renewal_count,
        "non_renewal_carriers": non_renewal_carriers,
    }

    # ══════════════════════════════════════════════════
    # Step 8: Business, property, coverage (unchanged)
    # ══════════════════════════════════════════════════

    revenue = extraction.company.annual_revenue or 0
    payroll = extraction.company.annual_payroll or 0
    headcount = extraction.company.headcount or 0
    years = extraction.company.years_in_business or 0

    analytics["business"] = {
        "revenue": revenue,
        "payroll": payroll,
        "headcount": headcount,
        "years_in_business": years,
        "naics": extraction.company.naics_code,
        "sic": extraction.company.sic_code,
        "state": extraction.company.state,
        "entity_type": extraction.company.entity_type,
    }

    locations = extraction.locations
    total_tiv = sum(
        (loc.building_value or 0) + (loc.contents_value or 0) + (loc.bi_value or 0)
        for loc in locations
    )
    sprinklered_count = sum(1 for loc in locations if loc.sprinklered)
    unsprinklered_count = len(locations) - sprinklered_count
    oldest_building = min((loc.year_built for loc in locations if loc.year_built), default=None)

    analytics["property"] = {
        "location_count": len(locations),
        "total_tiv": total_tiv,
        "sprinklered_count": sprinklered_count,
        "unsprinklered_count": unsprinklered_count,
        "oldest_building_year": oldest_building,
    }

    lines_requested = list(set(c.coverage_type for c in extraction.coverages if c.coverage_type))
    max_limit = max((c.limit or 0 for c in extraction.coverages), default=0)

    analytics["coverage"] = {
        "lines_requested": lines_requested,
        "line_count": len(lines_requested),
        "max_single_limit": max_limit,
        "multi_line": len(lines_requested) > 1,
    }

    log.info("analytics_computed",
        loss_ratio=analytics["loss"]["loss_ratio_pct"],
        loss_ratio_ex_cat=analytics["loss"]["loss_ratio_ex_largest_pct"],
        total_events=total_events,
        total_claim_lines=len(losses),
        open_events=len(open_events),
        subrogation=subrogation,
        non_renewal=non_renewal,
        premium_source=premium_source,
        total_premium=total_premium,
        systemic=analytics["causation"]["systemic"],
    )

    return analytics


def _extract_premium_from_text(text: str) -> float:
    """Extract premium total from broker notes or free text."""
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
    best = 0
    for pattern in patterns:
        matches = re.findall(pattern, text.lower())
        for m in matches:
            try:
                val = float(m.replace(",", ""))
                # Sanity check: premium should be between $1K and $10M
                if 1000 <= val <= 10000000 and val > best:
                    best = val
            except ValueError:
                pass
    return best


def _scan_dict_for_premium(d: dict, depth: int = 0) -> float:
    """Recursively scan a dict for premium-related values."""
    if depth > 5:
        return 0
    best = 0
    premium_keys = {
        "total_premium", "premium", "total_premium_paid", "premium_paid",
        "estimated_total_premium", "total_premiums", "annual_premium",
    }
    for key, val in d.items():
        key_lower = key.lower().replace(" ", "_")
        if key_lower in premium_keys:
            if isinstance(val, (int, float)) and 1000 <= val <= 10000000:
                best = max(best, float(val))
            elif isinstance(val, str):
                try:
                    num = float(val.replace(",", "").replace("$", ""))
                    if 1000 <= num <= 10000000:
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
                dt = datetime.strptime(date_str.strip(), fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        # Try extracting just the date part if there's extra text
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
    """Normalize carrier names so Hartford Financial Services == Hartford.
    
    Strips common suffixes and applies known aliases.
    Returns deduplicated carrier name for counting.
    """
    if not name:
        return name
    
    # Strip common suffixes
    suffixes = [
        " Financial Services Group", " Financial Services", 
        " Insurance Company", " Insurance Companies", 
        " Insurance Group", " Insurance Co",
        " North America", " Group", " Inc.", " Inc", 
        " LLC", " Corp", " Corporation", " Company", 
        " Companies", " Mutual",
    ]
    normalized = name.strip()
    for suffix in suffixes:
        if normalized.lower().endswith(suffix.lower()):
            normalized = normalized[:len(normalized) - len(suffix)].strip()
    
    # Known aliases
    aliases = {
        "the hartford": "Hartford",
        "hartford": "Hartford",
        "travelers": "Travelers",
        "zurich": "Zurich",
        "emc": "EMC",
        "employers mutual": "EMC",
        "progressive commercial": "Progressive",
        "progressive": "Progressive",
        "texas mutual": "Texas Mutual",
    }
    lower = normalized.lower()
    return aliases.get(lower, normalized)