# TriagePilot — Scoring Rules Reference

> **Source file:** `app/services/rules_engine.py`
> **Important:** The score is computed 100% by code (no LLM). The LLM only writes the narrative text *after* the score is locked.

---

## How the Score Works

The pipeline runs in four stages. The rules engine never reads the raw PDF — it reads pre-computed numbers from the analytics layer:

```
Submission PDF
      ↓
  Extraction        (LLM reads the document — extracts claims, locations, coverages, etc.)
      ↓
  Analytics         (app/services/analytics_service.py)
                    Pure math — computes loss ratio, claim counts, clean years,
                    and auto-detects subrogation / suppression / causation signals.
                    No LLM. Output is a structured dict passed to the rules engine.
      ↓
  Rules Engine      (app/services/rules_engine.py)
                    Reads the analytics dict. Applies if/else thresholds.
                    Outputs score 1–5, status, triggers, overrides, positives.
                    No LLM.
      ↓
  Evaluator         (app/agents/evaluator.py)
                    LLM writes narratives, broker questions, and assigns queue.
                    Score is passed in as a locked value — LLM cannot change it.
```

The rules engine output is a score from **1 to 5**:

| Score | Status | Meaning |
|-------|--------|---------|
| 1 | `decline` | Hard decline — triggers outweigh all mitigations |
| 2 | `refer` | Decline-level triggers partially mitigated **OR** referral triggers with zero overrides |
| 3 | `refer_with_conditions` / `refer` / `review` | Borderline — needs senior UW, conditions, or more info |
| 4 | `accept` | Referral triggers fully overridden by mitigations, or fewer than 5 positives |
| 5 | `accept` | Clean risk — no triggers, 5 or more positive indicators |

> **Note on Score 2:** It appears in two different situations — (A) a decline trigger is present but mitigations reduce the weight to 0.35–0.75, or (B) referral triggers are present with zero mitigating overrides. Same score number, different root cause.

---

## Step 1 — Decline Triggers

If **any** of these fire, the submission enters decline-weighted decisioning (see Step 4 Path A).

| Rule | Threshold | What triggers it |
|------|-----------|-----------------|
| Loss ratio > 200% | `loss_ratio > 200` | Total incurred ÷ total premium exceeds 200% |
| Too many open claims | `total_events >= 5` AND `open_events >= 2` | 5 or more claims with 2 or more still open |
| Non-renewed by 2+ carriers | `non_renewal_count >= 2` | Prior carriers declined to renew |
| Business too new | `years_in_business < 1` | Less than 1 year in operation |

---

## Step 2 — Referral Triggers

These do **not** automatically decline — they flag the submission for senior underwriter review.

| Rule | Threshold | What triggers it |
|------|-----------|-----------------|
| Elevated loss ratio | `50 < loss_ratio <= 200` | Loss ratio above 50% but below 200% |
| Prior carrier non-renewal | any non-renewal flag | Prior carrier did not renew (any reason) |
| Open reserves high | `open_reserves > $100,000` | Outstanding open claim reserves exceed $100K |
| High claim frequency | `total_events >= 3` | 3 or more claims in history |
| Large account | `annual_revenue > $50M` | Revenue exceeds $50M |
| Large limit requested | `max_single_limit > $5M` | Any single coverage limit over $5M |
| Young business | `years_in_business < 2` | Less than 2 years in operation |
| Old building, no updates | `building_age > 40 years` AND no renovations | Building older than 40 years with no documented system updates |
| ACV on large building | ACV valuation on building > $5M | Insured at actual cash value — creates underinsurance risk |
| Basic causes-of-loss form | basic form selected | Excludes wind and water — needs confirmation |
| High vacancy | `vacancy > 30%` | One or more locations above 30% vacant |
| Low coinsurance | `coinsurance < 90%` | Below 90% coinsurance — penalty risk at partial loss |
| Far from fire station | `distance > 5 miles` | More than 5 miles from nearest fire station |
| Historical landmark | landmark flag present | Preservation requirements increase replacement costs |

> **Hard-refer triggers** — these three cannot be overridden by risk mitigations. A senior UW must sign off regardless of how good the rest of the risk looks: **requested limit > $5M**, **historical landmark**, **prior non-renewal**. The system checks for these by matching the rule name string before applying override logic.

---

## Step 3 — Mitigating Overrides

Overrides reduce the severity of decline/referral triggers. They do **not** override hard-refer triggers.

| Override | What it means |
|----------|--------------|
| Loss ratio excellent excluding largest claim | Ex-cat loss ratio < 30% — underlying risk is controlled; one isolated event inflated the ratio |
| Subrogation recovery potential | Third-party recovery may reduce net incurred exposure |
| Fire suppression worked as designed | Suppression system activated and contained the loss — prevented total loss |
| Loss causes are all different (not systemic) | No single cause type repeats — losses are random unrelated events, not an operational pattern |
| Multiple clean years | 3+ or 5+ calendar years with zero claims — demonstrates operational discipline |
| Safety/system improvements mentioned | Broker notes reference upgrades, protocols, suppression install, thermal imaging, electrical retrofit, etc. |

> **How subrogation, suppression, and causation are auto-detected** — these signals are not manually entered. The analytics service (`analytics_service.py`) scans each claim's description and the broker notes automatically:
>
> - **Subrogation potential** → true if any claim description contains: `"subrogation"`, `"recovery"`, `"contractor"`, `"defective"`, `"manufacturer"`, or `"third party"` — or broker notes contain `"subrogation"` or `"recovery against"`
> - **Suppression effective** → true if any claim description contains: `"sprinkler"`, `"suppression"`, `"contained"`, `"activated"`, or `"prevented total loss"`
> - **Causation / systemic** → each claim is classified into a type (electrical, arson, kitchen fire, premises liability, theft, auto, construction fall, etc.). Systemic = true only if the same type appears more than once. The catch-all `"other"` type is excluded from systemic detection — two unclassified perils are not treated as a pattern.

---

## Step 4 — Final Score Calculation

### Path A: Decline triggers present

The engine uses **mitigation-weighted decisioning**:

```
base_weight = number of decline triggers

mitigation_total is built by adding:
  +0.35  if largest claim > 50% of total incurred AND causation is not systemic
  +0.25  if ex-cat loss ratio < 30%
  +0.15  if subrogation potential exists
  +0.10  if fire suppression was effective
  +0.15  if 5+ clean years   (or +0.10 if 3+ clean years)
  +0.10  if safety/system improvements mentioned in broker notes

adjusted_weight = base_weight − mitigation_total
```

| Adjusted Weight | Score | Status | Meaning |
|----------------|-------|--------|---------|
| > 0.75 | **1** | `decline` | Triggers outweigh mitigations |
| 0.35 – 0.75 | **2** | `refer` | Significant mitigations but decline concern remains |
| ≤ 0.35 | **3** | `refer_with_conditions` | Triggers substantially mitigated — likely approvable with conditions |

**Example:** One decline trigger (base = 1.0). Isolated event (+0.35) + clean ex-cat ratio (+0.25) + subrogation (+0.15) = 0.75 mitigation → adjusted weight 0.25 → **Score 3** instead of hard decline.

**Maximum possible mitigation in one submission:** 0.35 + 0.25 + 0.15 + 0.10 + 0.15 + 0.10 = **1.10** — meaning a single decline trigger can always be fully mitigated if every override applies.

---

### Path B: No decline triggers, referral triggers present

```
override_count = number of overrides fired
refer_count    = number of referral triggers fired
```

| Condition | Score | Status |
|-----------|-------|--------|
| overrides >= refer_count AND no hard-refer triggers | **4** | `accept` |
| overrides > 0 but fewer than refer_count | **3** | `refer` |
| zero overrides | **2** | `refer` |

---

### Path C: No triggers at all

| Condition | Score | Status |
|-----------|-------|--------|
| 5 or more positive indicators | **5** | `accept` |
| 1–4 positive indicators | **4** | `accept` |
| No positives either | **3** | `review` |

---

## Step 5 — Positive Indicators

Green signals. In Path C they determine the score. In all paths they contribute to the winnability score.

| Indicator | Condition |
|-----------|-----------|
| Established business | 3+ years in operation |
| Multi-line opportunity | More than one line of coverage requested |
| All locations fully sprinklered | Every location has a full sprinkler system |
| Declining claim trend | Recent years have fewer claims than earlier periods |
| Strong clean history | 5+ years with zero claims |
| Old building with documented system updates | Building > 40 years old but systems recently renovated — mitigates age concern |
| Formal safety program | TIPS certified, OSHA program, or safety manager mentioned in broker notes |
| Enhanced security | Cameras, monitored security system, or guards mentioned in broker notes |
| Favorable EMR | Experience modification rate referenced in broker notes (below 1.0 = favorable; above 1.0 = above-average claims history) |

> **Note:** "Old building with documented system updates" is a **Positive**, not an Override. It appears when a building is over 40 years old AND has documented renovations. If the same building has no renovations it fires a Referral Trigger instead.

---

## Known Unused Field

`shock_loss_present` is computed by the analytics service and extracted in the rules engine (`shock_loss = loss.get("shock_loss_present", False)`) but is **never referenced in any rule**. It has no effect on the score today. This is either reserved for a future rule or dead code.

---

## Winnability Score

Separate from the appetite score. Represents the probability of placing the risk.

**Base value set by loss ratio:**

| Loss Ratio | Base Winnability |
|------------|-----------------|
| > 200% | 0.15 |
| 100–200% | 0.30 |
| 50–100% | 0.45 |
| 30–50% | 0.60 |
| < 30% | 0.75 |
| No loss history (0 claims) | 0.85 |

**Adjustments:**

| Factor | Adjustment |
|--------|-----------|
| Prior non-renewal | −0.15 |
| Open reserves > $100K | −0.10 |
| Subrogation potential | +0.05 |
| Ex-cat loss ratio < 20% | +0.10 |
| Suppression effective | +0.05 |
| 10+ years in business | +0.05 |
| Multi-line account | +0.05 |
| 5+ positives | +0.05 |
| Status is `refer_with_conditions` | +0.10 |
| Status is `decline` | capped at 0.20 max |

Final value is clamped between **0.05 and 0.95**.

---

## Priority Score

Indicates how urgently this submission should be worked.

**Base: 0.50**

| Factor | Adjustment |
|--------|-----------|
| Multi-line account | +0.10 |
| Revenue > $5M | +0.10 |
| Total premium > $50K | +0.10 |
| Appetite score ≥ 4 | +0.10 |
| Appetite score ≤ 2 | −0.10 |
| Prior non-renewal | +0.05 |

Final value is clamped between **0.10 and 0.95**.

---

## Missing Information Flags

These are flagged separately and do not affect the score directly. The LLM evaluator uses them to generate broker questions.

- No loss runs provided
- No prior insurance information
- No NAICS/SIC industry code
- No revenue provided
- No property details despite property coverage being requested
- Flood zone not provided for any property location
- Vacancy percentage missing for one or more locations

---

## How to Change a Rule

All thresholds are hardcoded in `app/services/rules_engine.py`. To change a rule:

1. Open `app/services/rules_engine.py`
2. Find the relevant `if` condition (use the tables above to locate it)
3. Change the threshold value
4. No config file or database entry needed — the change takes effect on the next run

**Examples:**
- Raise decline threshold from 200% to 250%: line 111 → `if loss_ratio > 250`
- Lower referral threshold from 50% to 40%: line 154 → `if 40 < loss_ratio <= 200`
- Change claim frequency trigger from 3 to 4 claims: line 183 → `if total_events >= 4`
- Change revenue large-account threshold from $50M to $75M: line 196 → `if revenue > 75_000_000`

To add a **new keyword** to subrogation/suppression detection, edit `app/services/analytics_service.py` — not `rules_engine.py`.

---

## Rule Change Process (recommended)

Since these rules directly affect underwriting decisions, any change should follow this process:

1. **Identify the rule** — use this document to find the threshold and line number
2. **Document the reason** — record why the threshold is changing (loss data, market guidance, compliance)
3. **Edit the correct file** — `rules_engine.py` for thresholds, `analytics_service.py` for detection keywords
4. **Test with known submissions** — run a few past submissions through to verify the score changes as expected
5. **Update this document** — keep the tables above in sync with the code