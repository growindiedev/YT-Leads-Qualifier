---
name: qualify-leads
description: Qualify a batch of leads from a CSV or Google Sheet against ContentScale's ICP. First checks each company website for a high-ticket B2B offer, then runs YouTube channel analysis. Trigger when the user provides a CSV or Sheet of leads and wants them qualified, or asks to process leads.
---

# Qualify Leads Skill

Runs the ContentScale lead qualification pipeline on a batch of leads.
Takes a CSV or Google Sheet as input, checks each company website for a high-ticket B2B offer,
qualifies each lead's YouTube presence, generates Why Chosen + Confidence for each,
and writes results to a Google Sheet.

Read `references/conditions.md` before making any qualification judgments.
Read `references/youtube_api.md` if debugging API issues.

---

## Usage

```
/qualify-leads <limit> "<input_csv_or_sheet>" "<output_sheet_id>"
```

Parse $ARGUMENTS: first = number of new leads to process (optional, omit or pass `""` to process all), second = input (CSV path or Google Sheets URL/ID), third = output sheet ID or URL.

Resolve input and output as follows:

1. **Limit** — use first argument if it's a number. If omitted or empty, read `batch.default_limit` from `pipeline_config.json` (currently 15). To process all leads, pass `0` or `"all"` explicitly.
2. **Input** — use second argument if provided, else read `INPUT_SHEET_ID` from `.env`. If also empty, find the `.csv` file in the `Input_lists/` folder.
3. **Output sheet** — use third argument if provided, else read `OUTPUT_SHEET_ID` from `.env`.

If no input can be resolved, stop and tell the user.

---

## Step 1 — Run batch processing

```
.venv/bin/python batch_qualify.py \
  --input "{input}" \
  --output-sheet "{output_sheet}" \
  --no-claude \
  [--limit {n}] \
  > /tmp/yt_batch_results.json
```

Include `--limit {n}` only if a limit was specified.

The script automatically:
- Checks the output sheet for already-qualified leads and skips duplicates
- Runs Stage 1 YouTube API checks (conditions A/B/C/E deterministically)
- Returns `STAGE2_NEEDED` for channels that need judgment (D vs FAIL)
- Emits progress to stderr; results JSON to stdout

Read `/tmp/yt_batch_results.json` as the results array.

---

## Step 2 — Report errors immediately

Scan results for `yt_condition == "ERROR"`. Print each one:

```
⚠ ERROR: {full_name} / {company} — {error}
```

Common cause: YouTube API quota exceeded (10,000 units/day). If quota hit mid-batch,
the remaining leads will all be ERROR. Re-run the same command tomorrow — dedup logic
will skip already-processed leads automatically.

---

## Step 3 — Enrich each non-ERROR row

For every row where `yt_condition` is not `"ERROR"`, do the following in one pass.

---

### 4a. Score ICP tier from LinkedIn data — no WebFetch

**Do not use WebFetch in this step.** The pipeline's keyword classifier has already
determined `offer_classification`. Trust it. Your job here is tier scoring only,
using the fields already in the row: `_company_description`, `_headline`,
`_specialities`, `_industry`, `job_title`, `_summary`.

Do not create new discards based on your own judgment. If the pipeline passed a
lead, keep it. Discard decisions belong in the pipeline, not in-session.

---

**Score offer depth for every non-DISCARD, non-ERROR lead:**

**Tier 1 — Strong ICP fit:**
- Specific niche stated (not generic "I help businesses grow")
- Outcome language with numbers or timeframes in description or headline
- Title signals founder-led advisory: Founder, CEO, Owner, Principal, Partner
- Description mentions clients, results, transformations, or named case studies

**Tier 2 — Good fit:**
- Clear B2B service offer but generic messaging
- Founder/CEO title present but description is agency-style, not personal brand
- Service business confirmed but niche or outcomes not specified

**Tier 3 — Weak fit:**
- Technically B2B but vague or multi-offering
- No founder-led signal, outcomes, or niche specificity
- Reads like a large agency-of-record rather than an expert-led advisory

**For UNCLEAR / FETCH_FAILED leads:** The pipeline could not classify the site.
Set confidence to `"Offer Unclear | Offer Flag: FETCH_FAILED"` and keep the lead —
do not discard, do not WebFetch. Let it through for manual review.

**Write the tier into the `confidence` field:**
- `"Tier 1 — Strong ICP"`
- `"Tier 2 — Good Fit"`
- `"Tier 3 — Weak Fit"`
- `"Offer Unclear | Offer Flag: FETCH_FAILED"` (for UNCLEAR/FETCH_FAILED only)

---

### 4c. Handle REVIEW_FAIL rows

`REVIEW_FAIL` means a secondary company (not the primary business) was found to have
an active polished YouTube channel. The primary company passed. This requires a judgment
call — do not auto-discard.

Read `_all_company_results` to understand the full picture.

For each REVIEW_FAIL row, write a `why_chosen` note that explains:
1. What the primary company does and why it qualifies
2. Which secondary company triggered the FAIL and why it might not matter
   (e.g. "Their advisory role at BetaCorp has an active channel, but their main
   business Acme Consulting — where they are founder — has no YouTube presence")
3. A recommendation: "Recommend manual review before outreach"

Set `confidence` to `"Review Required — Secondary FAIL"`.
Do NOT discard these rows. Write them to the Leads tab with the REVIEW_FAIL status
visible so you can make the call.

---

### 4d. Stage 2 judgment (only if `yt_condition == "STAGE2_NEEDED"`)

When a lead has multiple companies and `_all_company_results` is present,
check if any of those results also have `STAGE2_NEEDED`. If so, evaluate
each one that needs judgment before applying the resolution rule.

Use the `_yt_videos` array from the relevant company result.
Refer to `references/conditions.md` for full D vs F vs FAIL criteria.

After assigning each STAGE2_NEEDED result a condition (D, F, or FAIL),
re-run the resolution rule mentally:

```
If any company now = FAIL → set yt_condition = FAIL (or REVIEW_FAIL if secondary)
If all pass → set yt_condition = primary company condition
```

Update `yt_condition` on the row accordingly.

---

### 4e. Stage 2 single-company judgment (legacy — same as before)

If `_all_company_results` is absent or has only one entry, use the existing
D vs F vs FAIL criteria from `references/conditions.md` on `_yt_videos`.

**Condition D (good lead — weak content):**
- Exclusively raw podcast recordings, webinar/Zoom recordings, or interview clips
- No editing, no motion graphics, no production value
- Titles suggest episode format: "Ep.", "#123", "with [guest]", "interview",
  "podcast", "webinar"
- No direct-to-camera scripted content from the founder

**Condition F (good lead — off-topic content):**
- Posts regularly but content is entirely unrelated to their business/offer
- Personal vlogs, hobby content, or lifestyle content
- Nothing that would attract or convert their B2B target audience

**FAIL (bad lead — strong business content):**
- Direct-to-camera scripted content from the founder about their industry/offer
- Produced and edited — custom thumbnails, branded graphics
- SEO-optimized titles, authority-building or educational content
- Consistent schedule with no long gaps

If genuinely unclear → default to **Condition D**.

---

### 4f. Why Chosen — all non-ERROR rows

For single-company leads, write 2–3 sentences:
1. What the company sells / what their business does
2. Why they fit ContentScale's ICP (B2B service, need content-led growth, YouTube gap)
3. What their specific YouTube gap is (based on condition)

For multi-company leads (`multi_company_flag = true`):
Write 2–3 sentences focused on the PRIMARY company, but include a line about
the secondary companies if relevant:
- If a secondary company has a different YouTube status, note it
- Example: "Alice runs Acme Consulting (B2B sales coaching, no YouTube) and
  also co-founded Beta SaaS (HR tech, inconsistent channel). Primary outreach
  target is Acme where she has a clear YouTube gap."

Use `_summary`, `_headline`, `_company_description`, `_specialities`, `_industry`
as source material. Write only from what's available — do not fabricate details.

---

### 4g. Confidence — all non-ERROR rows

Confidence is now a composite field. Lead with the ICP tier from Step 4a,
then append any contact or data flags separated by ` | `.

**ICP tier (always first):**
- `Tier 1 — Strong ICP` — specific niche, outcome language, social proof, founder-led
- `Tier 2 — Good Fit` — clear B2B offer but generic messaging
- `Tier 3 — Weak Fit` — technically B2B but vague or not founder-led
- `Offer Unclear` — cannot determine after website check

**Append these flags when they apply:**
- `Multi-Company` — 2+ active roles, worth verifying primary focus before outreach
- `Review Required — Secondary FAIL` — secondary company has active YouTube channel
- `Offer Flag: FETCH_FAILED` — website could not be fetched even after Playwright retry

**Examples:**
- `Tier 1 — Strong ICP` ← clean lead
- `Tier 1 — Strong ICP | Multi-Company` ← strong lead, verify primary company first
- `Tier 2 — Good Fit | Multi-Company` ← decent lead, verify primary company first
- `Tier 3 — Weak Fit` ← deprioritise
- `Offer Unclear` ← lowest priority

---


## Score Detail Reference

The `score_detail` JSON written to the "Score Detail" column in the Leads tab
shows how the primary company was selected. Format:

```json
{
  "title":   3,   // Founder/CEO/Owner weight
  "tenure":  4,   // Months in role (0-5 normalised)
  "size":    2,   // Company size signal
  "niche":   2,   // Keyword overlap with target niche
  "website": 1,   // Has website
  "desc":    1    // Has company description
}
```

Use this to audit cases where the wrong company was selected as primary.
If title=0 and tenure=0, the scoring had little to work with — consider
manually verifying which company is their main business before outreach.

## Step 4 — Remove internal fields

For each result, delete before writing:
`_yt_videos`, `_yt_reasoning`, `_summary`, `_headline`, `_company_description`, `_specialities`, `_industry`, `_location`

---

## Step 5 — Write to Google Sheets

Save the enriched results array to `/tmp/yt_batch_results.json`, then run:

```
.venv/bin/python batch_qualify.py \
  --write-results /tmp/yt_batch_results.json \
  --output-sheet "{output_sheet}"
```

---

---

## Step 6 — Print Summary

```
BATCH COMPLETE
─────────────────────────────────────────
Input:          {n_input} leads
Skipped:        {n_skipped} already qualified
Prescreened:    {n_prescreen} discarded (Sales Nav filter mismatch)
Discarded:      {n_size} — company too large (>50 employees)
Discarded:      {n_offer} — offer failed (B2C / low-ticket / no website)

YOUTUBE RESULTS:
Condition A:    {n} — No channel found              (good leads ✓)
Condition B:    {n} — Dead channel                  (good leads ✓)
Condition C:    {n} — Inconsistent poster            (good leads ✓)
Condition D:    {n} — Weak content / podcast-only   (good leads ✓)
Condition E:    {n} — Shorts only                   (good leads ✓)
Condition F:    {n} — Off-topic content             (good leads ✓)
REVIEW_FAIL:    {n} — Secondary company FAIL        (manual review needed)
FAIL:           {n} — Active polished channel       (discarded)
Stage2 Needed: {n} — Awaiting in-session judgment
ERROR:          {n} — API failure (retry tomorrow)

Multi-company leads qualified: {n}
Good leads (A+B+C+D+E+F):      {n} written to Leads tab
Review leads (REVIEW_FAIL):    {n} written to Leads tab (flagged)
Discards total:                {n} written to Discards tab

Results: https://docs.google.com/spreadsheets/d/{sheet_id}
```

---
