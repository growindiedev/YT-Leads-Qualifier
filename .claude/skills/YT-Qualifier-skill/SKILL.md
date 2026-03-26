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

1. **Limit** — use first argument if it's a number. If omitted or empty, process all new leads.
2. **Input** — use second argument if provided, else read `INPUT_SHEET_ID` from `.env`. If also empty, find the `.csv` file in the project root.
3. **Output sheet** — use third argument if provided, else read `OUTPUT_SHEET_ID` from `.env`.

If no input can be resolved, stop and tell the user.

---

## Step 1 — Website pre-check (before any YouTube API calls)

For each lead, navigate to their company website and evaluate the offer before spending any YouTube API quota.

**HIGH-TICKET B2B PASS signals — look for:**
- Words like: retainer, book a call, apply now, done-for-you, advisory, consulting engagement, strategy session, custom proposal
- Services clearly priced above $1,000 if pricing is listed
- Target audience is clearly businesses or professionals, not consumers
- A real service offering exists, not just a blog or lead magnet with no offer

**DISCARD signals:**
- Price points under $500 visible on the page
- Primarily sells to consumers: weight loss, personal dating, general fitness, etc.
- Site is broken, parked, or has no content
- Only sells low-ticket digital products with no high-ticket service

**Outcomes:**
- Clear high-ticket B2B offer → **KEEP**, proceed to YouTube phase
- Low-ticket or B2C signals → **DISCARD** (set `yt_condition = "DISCARD"`, `why_chosen = "Offer does not match ICP — low-ticket or B2C"`)
- No website found → **DISCARD** (set `yt_condition = "DISCARD"`, `why_chosen = "No website found"`)
- Offer unclear → **FLAG** as `"Offer Unclear"` in confidence, proceed but note it

Remove all DISCARDed leads from the batch before running Step 2. Do not consume YouTube quota on them.

---

## Step 2 — Run batch processing

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

## Step 3 — Report errors immediately

Scan results for `yt_condition == "ERROR"`. Print each one:

```
⚠ ERROR: {full_name} / {company} — {error}
```

Common cause: YouTube API quota exceeded (10,000 units/day). If quota hit mid-batch,
the remaining leads will all be ERROR. Re-run the same command tomorrow — dedup logic
will skip already-processed leads automatically.

---

## Step 4 — Enrich each non-ERROR row

For every row where `yt_condition != "ERROR"`, do the following in one pass:

### 4a. Stage 2 judgment (only if `yt_condition == "STAGE2_NEEDED"`)

Use the `_yt_videos` array. Refer to `references/conditions.md` for full D vs FAIL criteria.

**Condition D (good lead — weak content):**
- Exclusively raw podcast recordings, webinar/Zoom recordings, or interview clips
- No editing, no motion graphics, no production value
- Titles suggest episode format: "Ep.", "#123", "with [guest]", "interview", "podcast", "webinar"
- No direct-to-camera scripted content from the founder

**Condition F (good lead — off-topic content):**
- Posts regularly but content is entirely unrelated to their business/offer
- Personal vlogs, hobby content, lifestyle, or generic motivational content
- Nothing that would attract or convert their B2B target audience
- They can show up on camera — they just aren't using it for business

**FAIL (bad lead — strong business content):**
- Direct-to-camera scripted content from the founder about their industry/offer
- Produced and edited — custom thumbnails, branded graphics
- SEO-optimized titles, authority-building or educational content relevant to their business
- Consistent schedule with no long gaps

If genuinely unclear → default to **Condition D**.

Update `yt_condition` to `D`, `F`, or `FAIL`.

### 4b. Why Chosen (all non-ERROR rows)

Write 2–3 sentences:
1. What they sell / what their company does
2. Why they fit ContentScale's ICP (selling a service/product, need content-led growth, lack polished YouTube)
3. What their specific YouTube gap is (based on condition: A=no channel, B=dead, C=inconsistent, D=raw unedited only, E=shorts only, F=off-topic content)

Use `_summary`, `_headline`, `_company_description`, `_specialities`, `_industry` as source material.
Write only from what's available — do not fabricate details.

Set `why_chosen` on the row.

### 4c. Confidence (all non-ERROR rows)

Set `confidence` to one of:
- **Normal** — email present, company and role are clear, offer is clear
- **Low Confidence** — no email, or profile is sparse / hard to reach
- **Offer Unclear** — cannot determine what they sell from available data
- **No Active Contacts** — no email and no phone

---

## Step 5 — Remove internal fields

For each result, delete before writing:
`_yt_videos`, `_yt_reasoning`, `_summary`, `_headline`, `_company_description`, `_specialities`, `_industry`, `_location`

---

## Step 6 — Write to Google Sheets

Save the enriched results array to `/tmp/yt_batch_results.json`, then run:

```
.venv/bin/python batch_qualify.py \
  --write-results /tmp/yt_batch_results.json \
  --output-sheet "{output_sheet}"
```

---

## Step 7 — Print summary

```
BATCH COMPLETE
─────────────────────────────
Input:        {n_input} leads
Skipped:      {n_skipped} already qualified
Processed:    {n_processed} leads

DISCARD:      {n} — Website pre-check failed (low-ticket / B2C / no site)
Condition A:  {n} — No channel
Condition B:  {n} — Dead channel
Condition C:  {n} — Inconsistent poster
Condition D:  {n} — Weak content (good leads ✓)
Condition E:  {n} — Shorts only (good leads ✓)
Condition F:  {n} — Off-topic content (good leads ✓)
FAIL:         {n} — Strong channel (discarded)
ERROR:        {n} — API failure (retry tomorrow)

Good leads:   {A+B+C+D+E+F} written to sheet

Results: https://docs.google.com/spreadsheets/d/{sheet_id}
```
