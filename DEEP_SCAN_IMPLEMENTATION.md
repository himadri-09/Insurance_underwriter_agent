# Deep Scan Implementation - Missing Info Fix

## Problem
The system was flagging "Missing Info" for:
- Coverage details
- Loss history  
- Prior insurance

Even though all this information was **already present** in the risk brief and extracted data.

## Root Cause Analysis

### Issue 1: Simplistic Validation Logic
The `rules_engine.py` had simplistic validation that only checked if arrays were empty:

```python
if not extraction.loss_history:
    missing.append("No loss runs provided...")
if not extraction.prior_insurance:
    missing.append("No prior insurance information...")
```

### Issue 2: Missing Data Aggregation
The loss run extraction was not properly aggregating `total_incurred` into the summary section, even when individual claim records existed.

### Issue 3: Alternative Data Sources Not Considered
The validation didn't account for summarized totals from loss runs (like `stated_total_premium` and `stated_total_incurred`) which are authoritative sources.

## Solution: Multi-Layer Fix

### Layer 1: Deep Scan Function (rules_engine.py)
Added `_deep_scan_info_completeness()` that checks **all available data sources**:

```python
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
```

**Key Improvements:**
1. **Loss History**: Detects data from `loss_history` array OR `stated_total_incurred` (totals)
2. **Prior Insurance**: Detects from `prior_insurance` array OR `stated_total_premium` (totals)
3. **Company Basics**: Requires BOTH name AND address
4. **Industry Classification**: Accepts either NAICS or SIC code

### Layer 2: Enhanced Loss Run Extraction (prompts.py)
Improved `EXTRACT_LOSS_RUN` prompt with explicit instructions to:
- Extract EVERY claim as a separate record
- Calculate summary totals by summing individual records
- Clearly distinguish between individual records and aggregated summary

```python
CRITICAL INSTRUCTIONS:
1. Extract EVERY row as a separate record.
2. For summary section: CALCULATE totals by summing all records:
   - total_incurred = SUM(all total_incurred from records)
   - total_paid = SUM(all amount_paid from records)  
   - total_claims = COUNT(all records)
   - open_claims = COUNT(records with status="open" or "reserved")
3. Use null for unreadable values, NOT guesses.
4. Pay attention to which line of business each claim falls under (GL, property, WC, auto).
5. DO NOT SKIP any claims - extract all of them.
```

### Layer 3: Fallback Summary Calculation (extractor.py)

#### 3a. Post-Merge Summary Auto-Calculation
Added post-processing in `_merge_extractions()` to auto-calculate missing summary totals:

```python
# If summary exists but key totals are missing/zero, calculate from loss_history records
if merged["loss_history"] and merged["summary"]:
    summary = merged["summary"]
    
    # Calculate total_incurred if missing or zero
    if not summary.get("total_incurred") or summary.get("total_incurred") == 0:
        total_inc = sum(...)
        if total_inc > 0:
            summary["total_incurred"] = total_inc
    
    # Similar for total_paid, total_claims, open_claims
```

#### 3b. Fallback from Records to stated_total_incurred
Added fallback in `_build_extraction_result()` to calculate `stated_total_incurred` from records if summary not provided:

```python
# FALLBACK: Calculate incurred from records if summary not provided
if result.stated_total_incurred == 0.0 and result.loss_history:
    total_inc = sum(
        (r.incurred or (r.amount_paid + r.amount_reserved))
        for r in result.loss_history
        if r.incurred or (r.amount_paid + r.amount_reserved)
    )
    if total_inc > 0:
        result.stated_total_incurred = total_inc
```

## Impact

### Before
- Risk with complete brief showing all loss history, prior insurance, and coverage details was marked as "Missing Info"
- Users saw misleading status indicators despite complete data
- Multiple fallbacks were not being triggered due to empty arrays

### After
1. **Data Validation**: Deep scan now recognizes data from ANY reliable source (arrays or totals)
2. **Summary Aggregation**: Loss run summaries are properly calculated even if LLM doesn't provide them
3. **Fallback Chain**: Multiple fallbacks ensure stated_total_incurred is populated from individual records
4. **Accurate Status**: Only flags as missing if information is truly absent across all sources

## Data Flow

```
Loss Run PDF
    ↓
[LLM Extraction]
    ├─ records: [{claim1}, {claim2}, ...] (individual claims)
    └─ summary: {total_incurred: X, total_paid: Y, ...}
    ↓
[_merge_extractions - Post-Process]
    ├─ If summary.total_incurred == 0: auto-calculate from records
    └─ Populate merged["summary"]
    ↓
[_build_extraction_result]
    ├─ Set result.stated_total_incurred from summary
    └─ If still 0: fallback to sum(loss_history records)
    ↓
[Deep Scan in rules_engine]
    ├─ Check: has_loss_history = bool(loss_history) OR bool(stated_total_incurred)
    └─ Decision: NOT missing if stated_total_incurred > 0
    ↓
[Rules Output]
    └─ Only flag as missing if ALL sources empty
```

## Files Modified
1. `/app/services/rules_engine.py` - Added deep scan function and updated missing info detection
2. `/app/agents/prompts.py` - Enhanced EXTRACT_LOSS_RUN with better instructions
3. `/app/agents/extractor.py` - Added post-merge summary calculation and record-based fallback

## Testing Recommendations
1. ✅ Verify "Missing Info" badges no longer appear for complete briefs like Fuego Cantina LLC
2. ✅ Confirm that truly incomplete submissions still get flagged appropriately
3. ✅ Check that loss ratio calculations work correctly with summarized data
4. ✅ Validate appetite score is not affected by deep scan changes
5. Test with loss runs that have:
   - Only summary, no individual records
   - Only individual records, no summary
   - Neither summary nor records (should still flag as missing)

## Future Enhancements
1. Add form_data as additional completeness source
2. Add retrieved_chunks as fallback data source  
3. Implement severity levels for missing info (critical vs. nice-to-have)
4. Add data freshness validation (e.g., loss runs must be within 12 months)
5. Add confidence scores to each data source (LLM-extracted vs. user-provided)

