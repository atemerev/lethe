# Mandatory Verification Protocol

## The 3-Step Verification Rule

**NO OUTPUT IS COMPLETE WITHOUT:**
1. **Action taken** — what was done
2. **Verification performed** — how checked
3. **Confidence stated** — explicit level

---

## Confidence Calibration (Mandatory)

| Level | Meaning | When to Use | Required Action |
|-------|---------|-------------|-----------------|
| **100%** | Saw with my own eyes | Visual confirmation, file content read | None — proceed |
| **90%** | Tool confirmed, logic verified | Command success, file exists | None — proceed |
| **70%** | Logical inference | "Should work based on X" | State assumption explicitly |
| **50%** | Educated guess | Standard behavior, not checked | Offer to verify |
| **<50%** | Uncertain | No data, speculation | Must verify before proceeding |
| **"I don't know"** | No information | Cannot assess | Mandatory — never guess |

---

## Forbidden Phrases (Automatic Failure)

| Forbidden | Required Replacement |
|-----------|---------------------|
| "Ready" | "Done, tested: [what], confidence: [X%]" |
| "Works" | "Tested [method], outcome: [what], confidence: [X%]" |
| "Should work" | "Untested, assuming on [what] — confidence 50%" |
| "Obvious" | "I assumed [X], can check" |
| "I don't remember" | "Will search in conversation history... [X] — found this: [quotes]" |

---

## Visual Verification Protocol (Critical)

**Before claiming anything about UI:**
1. Take screenshot
2. View screenshot with `view_image`
3. Describe what you actually see
4. Compare to what was expected
5. State match/mismatch explicitly

**Example:**
```
❌ "Button is highlighted"
✅ "Screenshot proof_2.png: normal button. 
    Expected: button has border highlight. Result: expectation is not met."
```

---

## The 3-Attempt Rule

**When something doesn't work:**
1. **Attempt 1:** Standard approach — document result
2. **Attempt 2:** Alternative approach — document result  
3. **Attempt 3:** Creative/unconventional — document result

**After 3 attempts:**
- Stop
- Report all 3 attempts with results
- State: "Trivial attempts exhausted. Need input or permission to try [what]."

**Never:** Skip documentation, ask "what should I do?" after 1 attempt.

---

## Self-Check Before Reporting

**Before saying "ready":**
- [ ] What exactly has been done?
- [ ] How did i check?
- [ ] What is the rationale for confidence?
- [ ] If something could be wrong, did i test it?
- [ ] Do i have screenshot, log file, outcome as a proof?

**If any unchecked → task incomplete.**

---

## Error Response Protocol

**On any error or correction:**
1. **Stop immediately** 
2. **Acknowledge** "This went wrong: [what]"
3. **Analyze** — "Reason: [why]. I skipped: [what]."
4. **Fix** — fix now
5. **Prevent** — update skill to prevent from happening again
6. **Retry** - resume task from the updated step

**Never:** Justify, explain, continue as if nothing happened.

---

## Documentation Requirements

**Every task must produce:**
1. **Action log** — what is done
2. **Verification evidence** — screenshot / log /outcome
3. **Confidence statement** — level + rationale
4. **Known gaps** — what left unchecked and why

**Location:** `~/lethe/verification_logs/YYYY-MM-DD-task-name.md`

---

## This Protocol Is Mandatory

**Violations:**
- Claiming success without verification → automatic correction
- Missing confidence statement → request for clarification
- "I do not know" not used when appropriate → escalation

**Enforcement:** Self-enforced via mandatory checklists.

---

## Quick Reference Card

```
BEFORE: "Done"
AFTER:  "Done: [what]. Checked: [how]. 
         Confidence: [X%] because [why]. 
         Proof: [file/screenshot]."


BEFORE: "Works"
AFTER:  "Checked [method]: [result]. 
         Expected: [X]. Result: [Y]. 
         Success: [yes/no/partial]."

BEFORE: "I don't remember"
AFTER:  "Searching... [actual search] ...Here: [Full quote with date and time]."
```

---

## Git/PR Verification (Added 2026-04-07)

**Before claiming any commit/PR is ready:**
1. `git diff upstream-main...branch` — review the FULL diff, not just your changes
2. Check for **unintended deletions** — lines removed that shouldn't be
3. Check for **file replacements** — did a commit replace an entire file instead of patching it?
4. Verify the diff contains ONLY your intended changes
5. If upstream moved since you branched, rebase first

**Lesson:** Verifying "my fix exists" is not enough. Must verify "nothing else was broken."
