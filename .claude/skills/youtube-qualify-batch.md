Qualify a batch of leads from a CSV or Google Sheet. Usage: /youtube-qualify-batch "<input_csv_or_sheet>" "<output_sheet_id>"

Parse the arguments from $ARGUMENTS: first is the input (CSV path or Google Sheets URL/ID), second is the output Google Sheet ID or URL.

## Step 1 — Run batch processing with --no-claude

```
cd youtube_qualifier && python batch_qualify.py --input "{input}" --output-sheet "{output_sheet}" --no-claude
```

Parse the JSON array from stdout. Each element represents one lead.

## Step 2 — Report any immediate errors

Scan results for rows where `yt_condition == "ERROR"`. Print a warning for each:
```
⚠ ERROR: {full_name} / {company} — {error}
```

## Step 3 — Enrich each row in-session

For every row in the results (including ERROR rows — skip those), do the following judgment in one pass:

### 3a. YouTube Stage 2 (only if `yt_condition == "STAGE2_NEEDED"`)

Use the `_yt_videos` array to judge the channel. Update `yt_condition` to `D` or `FAIL` and set `_yt_reasoning` to a one-sentence explanation.

CONDITION D (good lead — weak content):
- Videos are exclusively raw podcast recordings, webinar recordings, or interview clips
- No visible editing or production value
- Titles suggest episode format (Ep., #123, "with [guest]", "interview", "podcast", "webinar")
- No direct-to-camera scripted content from the founder

FAIL (bad lead — strong content):
- Direct-to-camera scripted content from the founder
- Titles/thumbnails suggest produced, edited content
- Educational or authority-building content (not just podcast clips)
- Short punchy titles, not episode-style

If genuinely unclear, default to Condition D.

### 3b. Why Chosen (all non-ERROR rows)

Write 2–3 sentences covering:
1. What they sell / what their company does
2. Why they fit ContentScale's ICP (they're selling something, need content, and lack polished YouTube presence)
3. What their YouTube gap is (based on yt_condition: A=no channel, B=dead, C=inconsistent, D=weak unedited content, E=shorts only)

Use `_summary`, `_headline`, `_company_description`, `_specialities`, and `_industry` as source material. Write only from what's available — don't fabricate details.

Set `why_chosen` to the result.

### 3c. Confidence (all non-ERROR rows)

Set `confidence` to one of:
- **Normal** — email present, clear company and role, clear offer
- **Low Confidence** — no email, or profile is sparse / hard to reach
- **Offer Unclear** — can't determine what they sell from available data
- **No Active Contacts** — no email and no phone

## Step 4 — Remove internal fields before writing

For each result, delete: `_yt_videos`, `_yt_reasoning`, `_summary`, `_headline`, `_company_description`, `_specialities`, `_industry`, `_location`

## Step 5 — Write finalized results to Google Sheets

Write the complete results array to `/tmp/yt_batch_results.json`, then run:

```
cd youtube_qualifier && python batch_qualify.py --write-results /tmp/yt_batch_results.json --output-sheet "{output_sheet}"
```

## Step 6 — Print summary

```
BATCH COMPLETE
Total:        {n} leads processed
Condition A:  {count} — No channel
Condition B:  {count} — Dead channel
Condition C:  {count} — Inconsistent poster
Condition D:  {count} — Weak content (good leads)
Condition E:  {count} — Shorts only
FAIL:         {count} — Strong content (bad leads)
ERROR:        {count} — Failed (see warnings above)

Good leads (A/B/C/D/E): {count}

Results: {url returned by write step}
```
