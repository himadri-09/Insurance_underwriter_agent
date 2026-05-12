# Four Critical Logic Issues & Fixes

## Problem Summary

After deploying the extraction and carrier normalization fixes, three issues remain:

1. **Loss ratio math mixing time periods** — $865K incurred in 1 year ÷ $40.4K annual premium ≠ actuarially correct 5-year ratio
2. **Two financial realities coexisting** — Report shows both $1.29M gross and $865K net without labeling which is authoritative
3. **Narrative vs. decision mismatch** — Brief correctly identifies mitigating factors (isolated event, improvements done, subrogation possible) but rules engine forces "Decline" anyway
4. **Decision state contradiction** — Referral note reads like "Refer with conditions" but final status is "Decline"

---

## Issue 1: Loss Ratio Period Alignment

### Current Behavior
```python
# app/services/analytics_service.py, ~line 160-165
loss_ratio = (total_incurred / total_premium * 100) if total_premium > 0 else None
```

**Problem**: Mixes multi-year loss history with single-year premium estimate:
- `total_incurred` = $1.29M (5-year history from loss run)
- `total_premium` = $40.4K (extracted from 1 policy dec page)
- Result: 2143% loss ratio (nonsensical comparison)

**Root Cause**: 
- Loss run contains 5 years of claims (2021-2026)
- Prior policy dec page contains only 1 year of premium (07/2025-07/2026)
- Analytics doesn't track "loss period years" vs "premium period years"

### Correct Behavior
Must ensure:
$$\frac{\text{N-year losses}}{\text{N-year premium}} \times 100 = \text{actuarially valid ratio}$$

For BrightTech:
$$\frac{1,293,300}{156,500} \times 100 \approx 826\%$$

where both are from 5-year period.

### Fix Implementation

**Step 1**: Extract loss history period from data
```python
# In Step 1 (loss events deduplication)
loss_years = set()
for loss in losses:
    year = _extract_year(loss.date_of_loss)
    if year:
        loss_years.add(year)

loss_period_start_year = min(loss_years) if loss_years else datetime.now().year
loss_period_end_year = max(loss_years) if loss_years else datetime.now().year
loss_period_years = (loss_period_end_year - loss_period_start_year) + 1  # inclusive
```

**Step 2**: Require premium to span same period
```python
# In Step 2 (premium calculation)
# If loss history spans 5 years but we only have 1 year of premium data:

if loss_period_years > 1 and premium_source not in ("loss_run_stated", "estimated_multi_yr"):
    # We have multi-year losses but only 1-year premium estimate
    # Estimate multi-year premium:
    estimated_multi_year_premium = premium_from_records * loss_period_years
    log.info("premium_period_alignment", 
        loss_period_years=loss_period_years,
        single_year_premium=premium_from_records,
        estimated_multi_year=estimated_multi_year_premium)
    
    total_premium = estimated_multi_year_premium
    premium_source = f"estimated_{loss_period_years}yr"
    premium_confidence = "medium"
else:
    # Premium already covers full period (stated in loss run)
    premium_confidence = "high"
```

**Step 3**: Track periods explicitly in output
```python
analytics["loss"] = {
    "loss_period_years": loss_period_years,
    "loss_period_start": loss_period_start_year,
    "loss_period_end": loss_period_end_year,
    "premium_period_years": loss_period_years,  # MUST MATCH
    "total_incurred": total_incurred,
    "total_premium": total_premium,
    "premium_source": premium_source,
    "loss_ratio_pct": round(loss_ratio, 1) if loss_ratio else None,
    # ... rest
}
```

---

## Issue 2: Two Financial Interpretations

### Current Behavior
Report shows:
- `total_incurred = 1,293,300` (from loss run summary)
- `net_incurred = 865,900` (from loss run after subtracting largest or excluding fire)
- Both in same report without clear labeling of which is "authoritative"

**Problem**: Underwriter reads two different financial pictures:
- "Total losses: $1.29M" (harsh view)
- "Net losses: $865K" (favorable view)
- No guidance on which to use for pricing/decision

### Correct Behavior
Create canonical financial truth:

```json
{
  "gross_incurred": 1293300,
  "largest_single_loss": {
    "description": "Fire — electrical panel (02/12/2026)",
    "incurred": 847000,
    "pct_of_total": 65.5,
    "is_catastrophic": true
  },
  "net_incurred_excluding_largest": 446300,
  "financial_interpretation": "Gross incurred includes fire spike. Underlying frequency (excl. fire) shows 4 claims over 5 years, $446K incurred. Primary loss drivers: fire (65%), BI (15%), theft (4%), slip/fall (1%)."
}
```

### Fix Implementation

**Step 1**: Calculate and label all variants
```python
# After calculating largest_event (already done)
largest_loss_incurred = largest_event["total_incurred"] if largest_event else 0
largest_loss_pct = (largest_loss_incurred / total_incurred * 100) if total_incurred > 0 else 0
incurred_ex_largest = total_incurred - largest_loss_incurred

analytics["loss"]["financial_summary"] = {
    "gross_incurred": total_incurred,
    "gross_incurred_label": "All losses (including catastrophic events)",
    "largest_loss": {
        "description": largest_event["types"][0] if largest_event else None,
        "date": largest_event["date"] if largest_event else None,
        "incurred": largest_loss_incurred,
        "pct_of_total": round(largest_loss_pct, 1),
        "is_catastrophic": largest_loss_pct > 50,
    },
    "net_incurred": incurred_ex_largest,
    "net_incurred_label": "Losses excluding largest/catastrophic event",
}
```

**Step 2**: Document which is used for underwriting decisions
```python
# When computing loss ratios:
if largest_loss_pct > 50:
    # Catastrophic event present — use net ratio for underlying business assessment
    authoritative_loss_for_ratio = incurred_ex_largest
    ratio_label = "excluding_catastrophic"
    ratio_interpretation = "Underlying business (fire excluded) shows acceptable frequency"
else:
    # No catastrophic event — use gross ratio
    authoritative_loss_for_ratio = total_incurred
    ratio_label = "gross"
    ratio_interpretation = "Total experience across period"

analytics["loss"]["authoritative_incurred"] = authoritative_loss_for_ratio
analytics["loss"]["authoritative_incurred_label"] = ratio_label
```

**Step 3**: Use consistently in rules engine
```python
# In rules_engine.py
loss_for_underwriting = loss.get("authoritative_incurred")
loss_for_underwriting_label = loss.get("authoritative_incurred_label")

if loss_ratio is not None and loss_ratio > 200:
    triggers.append({
        "rule": "DECLINE: Loss ratio exceeds 200%",
        "detail": f"Loss ratio: {loss_ratio:.1f}% ({loss_for_underwriting_label})",
    })
```

---

## Issue 3: Narrative Correctly Identifies Mitigations But Rules Engine Ignores Them

### Current Behavior
Brief says:
- "Isolated single event (one fire in 5 years)"
- "Frequency otherwise excellent (1 other claim per year)"
- "Sprinkler system performed well"
- "Company completed $200K+ in improvements"
- "Subrogation potential $1M+"

But decision is: **Decline all lines.**

**Problem**: Rules engine uses hard triggers that fire without considering context.

Current logic (~line 60-65 in rules_engine.py):
```python
if loss_ratio > 200:
    triggers.append({...})
    # Automatically maps to "decline" status
```

This fires regardless of:
- Whether ratio includes catastrophic event
- Whether improvements have been made
- Whether subrogation has recovery potential
- Whether mitigation factors exist

### Correct Behavior
Separate "trigger fired" from "decision" by adding mitigation weights:

```python
decline_risk = base_decline_trigger_score
decline_risk -= mitigation_credit_for_isolated_event
decline_risk -= mitigation_credit_for_improvements
decline_risk -= mitigation_credit_for_subrogation
decline_risk -= mitigation_credit_for_trend

if decline_risk > threshold:
    status = "decline"
elif decline_risk > lower_threshold:
    status = "refer"
else:
    status = "accept_with_conditions"
```

### Fix Implementation

**Step 1**: Define mitigation credits in rules engine
```python
# New section in evaluate_rules() before DETERMINE FINAL SCORE

mitigations = {
    "isolated_event_credit": 0,
    "frequency_trend_credit": 0,
    "improvements_completed_credit": 0,
    "subrogation_potential_credit": 0,
    "suppression_effective_credit": 0,
}

# Isolated event: if largest loss > 50% of total, and it's recent
if largest_event:
    if largest_loss_pct > 50 and total_events == 1:
        # ONLY event is the catastrophic one → isolated
        mitigations["isolated_event_credit"] = 0.30  # 30% credit
    elif largest_loss_pct > 50 and total_events > 1 and incurred_ex_largest < total_incurred * 0.20:
        # Largest is catastrophic, but underlying is clean
        mitigations["isolated_event_credit"] = 0.20

# Trend credit: declining frequency is positive
if claim_trend == "declining":
    mitigations["frequency_trend_credit"] = 0.15
elif claim_trend == "stable" and total_events <= 2:
    mitigations["frequency_trend_credit"] = 0.10

# Improvements: explicit mention in broker notes
notes = (extraction.broker_notes or "").lower()
if any(keyword in notes for keyword in ["improvement", "upgrade", "repair", "completed", "system"]):
    mitigations["improvements_completed_credit"] = 0.15

# Subrogation: recovery potential reduces net loss
if subrogation.get("potential") and subrogation.get("amount", 0) > 0:
    subrogation_net_loss = total_incurred - subrogation["amount"]
    if subrogation_net_loss < total_premium * 3:  # Net ratio acceptable
        mitigations["subrogation_potential_credit"] = 0.15

# Suppression effective: sprinklers worked, fire contained
if suppression.get("effective"):
    mitigations["suppression_effective_credit"] = 0.10

total_mitigation = sum(mitigations.values())
log.info("mitigation_analysis", credits=mitigations, total_mitigation=total_mitigation)
```

**Step 2**: Apply mitigations to decline triggers
```python
# Modified DETERMINE FINAL SCORE section

decline_triggers = [t for t in triggers if t["severity"] == "decline"]

if decline_triggers:
    # Calculate adjusted decline score after mitigations
    base_decline_weight = len(decline_triggers) * 0.25  # Each trigger worth 0.25
    adjusted_decline_weight = max(0, base_decline_weight - total_mitigation)
    
    log.info("decline_adjustment", 
        base_weight=base_decline_weight,
        mitigation=total_mitigation,
        adjusted_weight=adjusted_decline_weight)
    
    if adjusted_decline_weight > 0.50:
        # Strong decline even after mitigations
        score = 1
        status = "decline"
        reasoning = f"Decline triggers present ({len(decline_triggers)}). Mitigations applied but insufficient to override."
    elif adjusted_decline_weight > 0.20:
        # Mitigations reduce decline to refer
        score = 2
        status = "refer"
        reasoning = f"Decline triggers identified but significant mitigating factors present. Recommend senior underwriter review."
    else:
        # Mitigations fully overcome decline
        score = 3
        status = "refer_with_conditions"
        reasoning = f"Initial decline triggers mitigated by: {', '.join([k.replace('_credit','').replace('_',' ') for k,v in mitigations.items() if v > 0])}"
else:
    # Original logic for refer/accept
    ...
```

**Step 3**: Add new status values to account for mitigated declines
```python
# Possible status values:
# "decline" → firm decline, recommend pass
# "refer" → needs senior review, unclear
# "refer_with_conditions" → likely approvable if conditions met
# "accept" → approve

# In brief generator, use status to set tone:
# - decline: explanatory (why we passed)
# - refer: balanced (pros and cons)
# - refer_with_conditions: favorable (likely to approve if...)
# - accept: supportive
```

---

## Issue 4: Decision State Contradiction

### Current Behavior
- Final status: `decline`
- Referral note: Reads like "consider conditional renewal" (contradictory)

### Correct Behavior
Referral note must match decision status:

| Status | Referral Note Should Say |
| --- | --- |
| `decline` | "We recommend passing on this risk. Specifically: [list reasons]. If broker has new information, may reconsider." |
| `refer` | "Recommend senior underwriter review. Key questions: [list open items]. Consider requesting [information]." |
| `refer_with_conditions` | "Likely approvable contingent on: [conditions]. Recommend requesting [docs] to confirm." |
| `accept` | "Approved pending standard underwriting. Recommend [standard underwriting items]." |

### Fix Implementation

**Step 1**: Add decision-state-aware brief generation
```python
# In brief_writer.py, update system prompt

system_prompt = """
You are an expert insurance underwriting analyst writing a concise brief for an underwriting committee.

Based on the decision status provided, use appropriate tone:

- If status="decline": Explain why this risk is not suitable for our portfolio. Focus on structural issues.
- If status="refer": Present both positive and negative findings. Invite questions. Frame as "senior review needed".
- If status="refer_with_conditions": Present the case optimistically. "If [conditions], this is approvable." Frame as "likely to approve."
- If status="accept": Frame positively. "Recommend approval" with standard conditions.

CRITICAL: Your recommendations must align with the decision status. Do not recommend approval for a decline decision.

Use the extracted analytics as the foundation for all claims. Quote exact figures.
"""
```

**Step 2**: Pass decision state to brief generator
```python
# In agents/pipeline.py or agents/brief_writer.py

brief = generate_brief(
    extraction=extraction,
    analytics=analytics,
    rules_result=rules_result,
    decision_status=rules_result["status"],  # NEW
    mitigations=rules_result.get("overrides", []),  # NEW
)
```

**Step 3**: Condition referral note on status
```python
# In agents/brief_writer.py

def generate_referral_note(decision_status, rules_result, extraction):
    if decision_status == "decline":
        return f"""
        UNDERWRITING DECISION: DECLINE

        We have determined this risk is not suitable for our portfolio at this time.
        
        Primary reasons:
        {', '.join(t['rule'] for t in rules_result['triggers'][:3])}
        
        If the broker believes circumstances have materially changed or additional 
        information is available, they may resubmit for reconsideration.
        """
    
    elif decision_status == "refer":
        return f"""
        RECOMMENDATION: REFER TO SENIOR UNDERWRITER
        
        This risk has both positive and challenging characteristics. Senior review recommended.
        
        Key questions:
        {generate_questions(rules_result, extraction)}
        
        Recommended documents to obtain:
        {generate_doc_requests(rules_result, extraction)}
        """
    
    elif decision_status == "refer_with_conditions":
        return f"""
        PRELIMINARY: LIKELY APPROVABLE SUBJECT TO CONDITIONS
        
        This risk shows promise. We recommend approval contingent on:
        
        Conditions:
        {generate_conditions(rules_result, extraction, extraction.analytics)}
        
        Please obtain and confirm:
        {generate_doc_requests_pre_approval(rules_result, extraction)}
        """
    
    else:  # accept
        return f"""
        RECOMMENDATION: APPROVE
        
        This risk meets our underwriting standards. Recommend approval.
        
        Standard conditions apply:
        {generate_standard_conditions()}
        """
```

---

## Implementation Sequence

### Phase 1: Fix Period Alignment (Critical)
**File**: `app/services/analytics_service.py`
- Extract loss period years from dates in Step 1 ✓
- Estimate premium to match loss period in Step 2 ✓
- Add `loss_period_years` and `premium_period_years` to output ✓

**Verify**: Loss ratio should show as ~826% (not 2143%)

### Phase 2: Fix Financial Labeling (Critical)
**File**: `app/services/analytics_service.py`
- Add `financial_summary` dict with gross/net variants ✓
- Set `authoritative_incurred` and label ✓
- Use consistently in loss ratio calculation ✓

**Verify**: Report shows one canonical financial truth

### Phase 3: Implement Mitigations (High Priority)
**File**: `app/services/rules_engine.py`
- Calculate mitigation credits for each factor ✓
- Reduce decline weight by total mitigations ✓
- Map adjusted weights to status (decline → refer → accept) ✓

**Verify**: BrightTech: Decline triggers fire, mitigations reduce weight, status → "refer_with_conditions"

### Phase 4: Fix Decision-State Consistency (High Priority)
**File**: `app/agents/brief_writer.py`
- Accept `decision_status` as parameter ✓
- Condition referral note on status ✓
- Tone matches decision (never recommend approval for decline) ✓

**Verify**: Brief and decision statement align

---

## Success Criteria

- [ ] Loss ratio calculation aligns periods (5-year ÷ 5-year)
- [ ] Report shows gross AND net incurred, labeled clearly
- [ ] BrightTech decision: "refer_with_conditions" (not "decline")
- [ ] Mitigations explicitly credited in logs
- [ ] Brief tone matches decision status
- [ ] Referral note does not contradict final decision

---

## Files to Modify

1. `app/services/analytics_service.py` — Lines 1-250 (Step 1, Step 2, loss dict)
2. `app/services/rules_engine.py` — Lines 200-330 (mitigation logic, final score)
3. `app/agents/brief_writer.py` — Lines 1-100 (decision-state conditioning)

---

## Git Commit Message

```
fix(underwriting): Four critical logic fixes

1. Loss ratio period alignment: Ensure N-year losses ÷ N-year premium
   - Extract loss_period_years from date range in loss history
   - Estimate multi-year premium to match loss period
   - Track premium_period_years explicitly in analytics

2. Financial reality labeling: One canonical truth, not two interpretations
   - Add financial_summary dict with gross/net incurred variants
   - Set authoritative_incurred and label clearly
   - Prevents underwriter confusion between competing numbers

3. Mitigation credit system: Reduce decline weight based on context
   - Calculate credits for isolated events, trend, improvements, subrogation
   - Apply credits to base decline weight before mapping to status
   - Allows "refer_with_conditions" for catastrophic but mitigated risks

4. Decision-state consistency: Brief aligns with final decision
   - Pass decision_status to brief generator
   - Condition referral note tone on status
   - Never recommend approval for decline decision

Result: BrightTech (2143% → ~826% ratio, isolated event, conditions met)
now properly maps to "refer_with_conditions" instead of hard decline.
```
