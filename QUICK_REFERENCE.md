# Quick Reference - Missing Info Fix

## Problem Statement
Fuego Cantina LLC risk brief showed:
```
Missing Info
  • coverage details
  • loss history
  • prior insurance
```
Even though **all this data was provided** in the documents.

## Root Cause
The validation logic was too simplistic:
```python
if not extraction.loss_history:  # ← Only checks if ARRAY is empty
    missing.append("No loss runs...")
```

It didn't account for summarized totals like `stated_total_incurred` that exist even when individual records aren't extracted.

## The Fix (3 Layers)

### Layer 1: Deep Scan Function
**File:** `app/services/rules_engine.py`

```python
"has_loss_history": bool(extraction.loss_history) or bool(extraction.stated_total_incurred)
                     ├─ Check individual records array
                     └─ OR check summarized total
```

Now looks for data in BOTH places instead of just the array.

### Layer 2: Better LLM Instructions
**File:** `app/agents/prompts.py`

Enhanced `EXTRACT_LOSS_RUN` prompt to explicitly instruct the LLM to:
- Calculate `summary.total_incurred` by summing individual records
- Don't skip any claims
- Distinguish between individual records and summary

### Layer 3: Fallback Calculations
**File:** `app/agents/extractor.py`

Two fallbacks ensure data is populated:
1. **Post-merge**: If records exist but summary is missing → calculate summary totals
2. **Pre-build**: If no records/summary → calculate stated_total_incurred from records

## Before vs After

| Scenario | Before | After |
|----------|--------|-------|
| 5 loss records extracted, but summary.total_incurred = 0 | ❌ Flagged as missing | ✅ Not missing (sees stated_total_incurred calculated from records) |
| Loss run summary provided but no individual records | ❌ Flagged as missing | ✅ Not missing (sees stated_total_incurred from summary) |
| Truly no loss data provided | ❌ Flagged as missing | ✅ Correctly flagged as missing |

## How to Verify

1. Run pipeline on Fuego Cantina submission
2. Check extraction logs for:
   ```
   extraction_complete stated_premium=128000.0 stated_incurred=302700.0
   ```
3. Verify in rules evaluation:
   ```
   rules_evaluated missing=['...']  # Should NOT include loss_history or prior_insurance
   ```
4. Check final brief output: Should NOT show "Missing Info" badges for these items

## Key Files

| File | Change | Purpose |
|------|--------|---------|
| `rules_engine.py` | Added `_deep_scan_info_completeness()` | Check multiple data sources |
| `prompts.py` | Enhanced EXTRACT_LOSS_RUN | Better LLM instructions |
| `extractor.py` | Added post-merge calculation + record fallback | Auto-fill missing summaries |

## Deployment

```bash
git add app/services/rules_engine.py
git add app/agents/prompts.py
git add app/agents/extractor.py
git commit -m "feat: implement deep scan for missing info validation"
git push origin main
```

## Monitoring

Watch for these log entries to confirm fix is working:

```
# Good sign - stated totals are being calculated:
extraction_complete stated_premium=128000.0 stated_incurred=302700.0

# Good sign - missing info no longer includes loss_history/prior_insurance:
rules_evaluated missing=['revenue', 'property_details']

# Good sign - new deep scan is being used:
rules_evaluated missing_info=...  # Only true missing items listed
```

## Questions?

- **Q: Will this affect existing submissions?** 
  A: No. Deep scan only adds alternative sources; doesn't remove existing checks.

- **Q: What if stated_total_incurred is wrong?**
  A: It will be used by loss ratio calculations. Validate LLM extraction separately.

- **Q: Can we override missing info manually?**
  A: Not yet. Future feature: form_data override for manually provided info.

- **Q: Does this affect appetite scoring?**
  A: No. Only affects missing_info list. Scoring uses loss_ratio and other factors.
