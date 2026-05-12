# Architecture Diagram - Deep Scan Fix

## Data Flow with Deep Scan

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    FUEGO CANTINA LOSS RUN DOCUMENT                       │
│  5 Claims: Kitchen Fire ($165K), 2 Liquor Liability ($45K, $78K),      │
│  Slip/Fall ($8.5K), Theft ($6.2K) = Total $302.7K                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    ↓
                         [LLM Extraction]
                                    ↓
        ┌───────────────────────────────────────────────────────────┐
        │  Extraction Result from LLM                                │
        ├───────────────────────────────────────────────────────────┤
        │  loss_history: [                                           │
        │    {date: "2020-01-01", amount_paid: 165000, ...},       │
        │    {date: "2022-06-15", amount_paid: 45000, ...},        │
        │    {date: "2024-03-20", amount_paid: 78000, ...},        │
        │    ... 2 more records                                      │
        │  ]                                                         │
        │                                                            │
        │  summary: {                                                │
        │    total_claims: 5,                                        │
        │    total_incurred: 0  ❌ (LLM didn't calculate!)          │
        │  }                                                         │
        └───────────────────────────────────────────────────────────┘
                                    ↓
                    [Layer 3a: Post-Merge Calculation]
                                    ↓
        ┌───────────────────────────────────────────────────────────┐
        │  After Merge Processing                                    │
        ├───────────────────────────────────────────────────────────┤
        │  merged["loss_history"]: [5 records]                       │
        │  merged["summary"]["total_incurred"]: 302700 ✅           │
        │    (Auto-calculated: sum of all records)                   │
        └───────────────────────────────────────────────────────────┘
                                    ↓
                      [Build ExtractionResult]
                                    ↓
        ┌───────────────────────────────────────────────────────────┐
        │  ExtractionResult Object                                   │
        ├───────────────────────────────────────────────────────────┤
        │  .loss_history: [5 LossRecord objects]                     │
        │  .stated_total_incurred: 302700.0 ✅                      │
        │    (Set from merged["summary"] or calculated from records) │
        │  .stated_total_premium: 128000.0                           │
        └───────────────────────────────────────────────────────────┘
                                    ↓
                      [Deep Scan Validation]
                                    ↓
    ┌─────────────────────────────────────────────────────────────────┐
    │  _deep_scan_info_completeness(extraction)                        │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  Check 1: has_loss_history                                       │
    │  ├─ extraction.loss_history = [5 records] ✅  OR                 │
    │  └─ extraction.stated_total_incurred = 302700 ✅                 │
    │     → Result: TRUE (found in either place)                       │
    │                                                                   │
    │  Check 2: has_prior_insurance                                    │
    │  ├─ extraction.prior_insurance = [EMC, Travelers] ✅  OR         │
    │  └─ extraction.stated_total_premium = 128000 ✅                  │
    │     → Result: TRUE (found in either place)                       │
    │                                                                   │
    │  Check 3: has_coverages                                          │
    │  └─ extraction.coverages = [GL, Liquor Liability, ...] ✅        │
    │     → Result: TRUE                                               │
    │                                                                   │
    │  ... other checks (company, revenue, property, etc.)             │
    │                                                                   │
    └─────────────────────────────────────────────────────────────────┘
                                    ↓
                      [Rules Engine Evaluation]
                                    ↓
    ┌─────────────────────────────────────────────────────────────────┐
    │  Missing Info Determination                                      │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  FOR EACH required field:                                        │
    │  ├─ if completeness["has_loss_history"] = TRUE:                 │
    │  │  ✅ DON'T add "No loss runs provided" to missing[]            │
    │  │                                                               │
    │  ├─ if completeness["has_prior_insurance"] = TRUE:              │
    │  │  ✅ DON'T add "No prior insurance" to missing[]               │
    │  │                                                               │
    │  └─ if completeness["has_coverages"] = TRUE:                    │
    │     ✅ DON'T add "No coverage details" to missing[]              │
    │                                                                   │
    │  Final Result:                                                   │
    │  missing[] = [] (EMPTY - no missing info!)                       │
    │                                                                   │
    └─────────────────────────────────────────────────────────────────┘
                                    ↓
                            [Brief Output]
                                    ↓
    ┌─────────────────────────────────────────────────────────────────┐
    │  Risk Brief                                                      │
    ├─────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  Status: REFER                                                   │
    │  Appetite: 2/5                                                   │
    │  Winnability: 80%                                                │
    │  Priority: 55%                                                   │
    │                                                                   │
    │  Missing Info:           ✅ CLEAN (no false positives!)          │
    │  ├─ coverage details     ✓ (provided)                            │
    │  ├─ loss history         ✓ (provided)                            │
    │  └─ prior insurance      ✓ (provided)                            │
    │                                                                   │
    │  ... rest of brief with analysis ...                             │
    │                                                                   │
    └─────────────────────────────────────────────────────────────────┘
```

## Before vs After Comparison

### BEFORE (Broken Logic)

```
Loss Run Extraction:
  ├─ Individual records extracted: [5 claims]
  └─ Summary calculated: {total_incurred: 0}
                            ↓
Validation Logic:
  if not extraction.loss_history:  ← Array has 5 items, so FALSE
  if not extraction.stated_total_incurred:  ← Value is 0, so TRUE ❌
                            ↓
Result: "No loss runs provided" ❌ (WRONG! We have 5 records!)
```

### AFTER (Fixed Logic)

```
Loss Run Extraction:
  ├─ Individual records extracted: [5 claims]
  ├─ Summary calculated: {total_incurred: 0}
  └─ [Post-merge]: summary.total_incurred auto-set to 302700
                            ↓
Deep Scan Logic:
  has_loss_history = bool([5 items]) OR bool(302700)
                   = TRUE OR TRUE
                   = TRUE ✅
                            ↓
Result: Loss history IS present ✅ (CORRECT!)
```

## Data Source Priority

When determining if information is present, the deep scan checks in this priority:

```
Data Category         Primary Source          Fallback Source
──────────────────────────────────────────────────────────────
Loss History      → loss_history array  →  stated_total_incurred
Prior Insurance   → prior_insurance[]  →  stated_total_premium
Coverages         → coverages array    →  (no fallback needed)
Company Basics    → company object     →  (direct fields)
Revenue           → company.annual_revenue  → (direct field)
Property Details  → locations array    →  (no fallback)
```

## Fallback Chain for stated_total_incurred

```
Step 1: LLM Extraction
  └─ Try to get summary.total_incurred from loss run
     └─ If provided ✓ → Use it
     └─ If 0/missing → Continue to Step 2

Step 2: Post-Merge Calculation
  └─ Check if individual loss records exist
     └─ If yes → Sum them up and set summary.total_incurred
     └─ If no → Continue to Step 3

Step 3: Build ExtractionResult Fallback
  └─ Check if loss_history array has records
     └─ If yes → Sum them and set stated_total_incurred
     └─ If no → stated_total_incurred stays 0.0

Final State:
  ├─ If ANY step populated stated_total_incurred → Deep scan finds it ✓
  └─ If ALL steps fail → Truly no loss data, correctly flag as missing ✓
```

## Files Modified in This Fix

```
app/
├── services/
│   └── rules_engine.py
│       ├── Added: _deep_scan_info_completeness()
│       └── Modified: Missing info detection logic
│
├── agents/
│   ├── prompts.py
│   │   └── Modified: EXTRACT_LOSS_RUN with better instructions
│   │
│   └── extractor.py
│       ├── Modified: _merge_extractions() with post-merge calc
│       └── Modified: _build_extraction_result() with fallback

Documentation/
├── SOLUTION_SUMMARY.md (this high-level overview)
├── DEEP_SCAN_IMPLEMENTATION.md (technical details)
├── QUICK_REFERENCE.md (quick lookup guide)
└── CHANGES_SUMMARY.md (detailed change log)
```

## Testing Scenarios

```
Scenario 1: Individual records only
┌──────────────────────┐
│ loss_history: [5]    │
│ summary: {total: 0}  │
└──────────────────────┘
         ↓
    [Post-merge]
         ↓
┌────────────────────────┐
│ stated_total: 302700 ✓ │
└────────────────────────┘


Scenario 2: Summary only
┌────────────────────────┐
│ loss_history: []       │
│ summary: {total: 302K} │
└────────────────────────┘
         ↓
    [No post-merge needed]
         ↓
┌────────────────────────┐
│ stated_total: 302K ✓   │
└────────────────────────┘


Scenario 3: Neither (truly missing)
┌──────────────────────┐
│ loss_history: []     │
│ summary: {total: 0}  │
└──────────────────────┘
         ↓
    [No data to calc]
         ↓
┌──────────────────────┐
│ stated_total: 0 ✓    │ → Correctly flagged as MISSING
└──────────────────────┘
```
