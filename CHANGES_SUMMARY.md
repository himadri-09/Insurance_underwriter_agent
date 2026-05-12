# Changes Summary - Deep Scan & Missing Info Fix

## Overview
Fixed the "Missing Info" false positives by implementing a comprehensive 3-layer solution that:
1. Validates data across all sources (arrays, summaries, and totals)
2. Ensures loss run summaries are properly calculated from individual records
3. Provides fallback mechanisms to populate stated_total_incurred and stated_total_premium

## Files Changed

### 1. `/app/services/rules_engine.py`
**Added:** `_deep_scan_info_completeness()` function

**What it does:**
- Deep scans the extraction to find data in multiple places
- Checks both array forms (loss_history array, prior_insurance array) AND summary totals (stated_total_incurred, stated_total_premium)
- Returns a dict with flags for each data category

**Key logic:**
```python
"has_loss_history": bool(extraction.loss_history) or bool(extraction.stated_total_incurred)
"has_prior_insurance": bool(extraction.prior_insurance) or bool(extraction.stated_total_premium)
```

**Impact:**
- Loss runs showing as "0 losses" won't be flagged as missing IF stated_total_incurred > 0
- Prior insurance showing as empty array won't be flagged IF stated_total_premium > 0

---

### 2. `/app/agents/prompts.py`
**Modified:** `EXTRACT_LOSS_RUN` prompt

**Changes:**
- Added explicit instruction to extract EVERY claim row
- Added instruction to CALCULATE summary totals from records (not just ask for them)
- Added DON'T-SKIP instruction to ensure all claims are extracted
- Clarified the importance of LOB classification

**Before:**
```
Extract every row. Use null for unreadable values, not guesses.
```

**After:**
```
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

**Impact:**
- LLM will now always try to provide summary.total_incurred even if calculated
- No more missing summaries that should have been provided

---

### 3. `/app/agents/extractor.py`
**Added two fallback mechanisms:**

#### 3a. Post-Merge Summary Auto-Calculation (in `_merge_extractions()`)
If individual loss records were extracted but summary totals are missing/zero, calculate them from records:

```python
# If summary.total_incurred is 0 or missing, sum from records
if merged["loss_history"] and merged["summary"]:
    if not summary.get("total_incurred") or summary.get("total_incurred") == 0:
        total_inc = sum((r.get("total_incurred") or ...) for r in merged["loss_history"])
        if total_inc > 0:
            summary["total_incurred"] = total_inc
```

#### 3b. Record-Based Fallback (in `_build_extraction_result()`)
If no summary data available, calculate stated_total_incurred from loss records:

```python
# FALLBACK: Calculate incurred from records if summary not provided
if result.stated_total_incurred == 0.0 and result.loss_history:
    total_inc = sum((r.incurred or ...) for r in result.loss_history)
    if total_inc > 0:
        result.stated_total_incurred = total_inc
```

**Impact:**
- Even if LLM extraction fails to provide summary, we calculate it post-hoc
- stated_total_incurred gets populated from records as last resort
- Deep scan can then find the data via stated_total_incurred

---

## How It All Works Together

### Scenario: Loss run extracted with 5 claims but no summary

**Before (Breaking):**
```
Extraction Result:
  loss_history: [claim1, claim2, claim3, claim4, claim5]  ← 5 records
  stated_total_incurred: 0.0                              ← not set!
  
Rules Engine Check:
  has_loss_history = bool([]) or bool(0.0) = False       ← WRONG! We have records
  Result: "Missing loss history" ❌
```

**After (Fixed):**
```
Extraction Result (with fallback):
  loss_history: [claim1, claim2, claim3, claim4, claim5]  ← 5 records
  stated_total_incurred: 302700.0                         ← calculated from records!
  
Rules Engine Check:
  has_loss_history = bool([...]) or bool(302700) = True  ← CORRECT!
  Result: Loss history IS present ✅
```

---

## Testing the Fix

### Test 1: Loss Run with Individual Records Only
**File:** `test_deep_scan.py` (can be created for validation)

```python
extraction = ExtractionResult()
extraction.loss_history = [LossRecord(...), LossRecord(...)]  # 2 records
extraction.stated_total_incurred = 0.0  # No summary yet

# After extraction (with fallback):
# stated_total_incurred = 150000.0 (calculated from records)

completeness = _deep_scan_info_completeness(extraction)
assert completeness["has_loss_history"] == True  ✅
```

### Test 2: Loss Run with Summary Only
```python
extraction = ExtractionResult()
extraction.loss_history = []  # No individual records extracted
extraction.stated_total_incurred = 302700.0  # Summary provided

completeness = _deep_scan_info_completeness(extraction)
assert completeness["has_loss_history"] == True  ✅
```

### Test 3: Truly Missing Loss Data
```python
extraction = ExtractionResult()
extraction.loss_history = []  # No records
extraction.stated_total_incurred = 0.0  # No summary

completeness = _deep_scan_info_completeness(extraction)
assert completeness["has_loss_history"] == False  ✅ (correctly flagged as missing)
```

---

## Deployment Checklist

- [ ] Review and test all three file changes
- [ ] Run existing test suite to ensure no regressions
- [ ] Test with Fuego Cantina submission to verify "Missing Info" is fixed
- [ ] Test with truly incomplete submissions to verify they're still flagged
- [ ] Monitor logs for `extraction_complete` showing `stated_total_incurred > 0`
- [ ] Verify appetite scores don't change for existing submissions

---

## Rollback Plan

If issues occur:
1. Revert `rules_engine.py` to remove deep scan function
2. Revert `prompts.py` to remove new LLM instructions
3. Revert `extractor.py` to remove fallback calculations
4. All changes are isolated to these three files

---

## Future Improvements

1. **Confidence Scores**: Track which source provided the data (LLM-extracted vs. calculated)
2. **Freshness Checks**: Validate that loss runs are recent (within 12 months)
3. **Form Data Integration**: Add form_data as another completeness source
4. **Retrieved Chunks**: Use RAG-retrieved knowledge base chunks as fallback
5. **Severity Levels**: Distinguish between "critical missing" and "nice-to-have missing"
