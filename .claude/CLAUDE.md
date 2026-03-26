# ContentScale — Leads Qualification Pipeline

## What this project does

Takes a CSV or Google Sheet of raw B2B leads, runs them through a multi-stage
qualification pipeline, and writes enriched results to a Google Sheet.

**Qualification order (each gate discards before the next runs):**

1. **Dedup** — skips leads already present in the output sheet
2. **Prescreen** — discards leads whose Sales Nav mismatched-filter column signals
   the primary company is too large or doesn't match the search
3. **Size gate** — discards companies with >50 employees (parsed from LinkedIn data)
4. **Website classifier** — fetches the company website and keyword-scores it;
   discards B2C, low-ticket, and leads with no website
5. **YouTube analysis** — 4-stage channel discovery + Stage 1 deterministic checks;
   rows needing human judgment come back as `STAGE2_NEEDED`
6. **Stage 2 judgment** — done in-session by the human reviewer via the
   `/qualify-leads` skill (no API cost)

---

## Project structure

```
/
├── .env                              ← API keys (never commit)
├── leads-service-account.json        ← Google service account key (never commit)
├── session_counter.json              ← Auto-generated; tracks daily session IDs
├── batch_qualify.py                  ← Batch runner, all gates, Sheets writer
├── youtube_qualifier.py              ← YouTube channel discovery + Stage 1 logic
├── test_cases.py                     ← API + unit test suite
├── requirements.txt
├── .venv/                            ← Python virtualenv
└── .claude/
    ├── CLAUDE.md                     ← This file
    └── skills/YT-Qualifier-skill/
        ├── SKILL.md                  ← /qualify-leads skill definition
        └── references/               ← Condition definitions, API reference
```

---

## First-time setup

### 1. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Google Cloud — Service Account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **YouTube Data API v3** and **Google Sheets API**
3. Create a Service Account → download the JSON key
4. Save the key as `leads-service-account.json` in the project root
5. Share your output Google Sheet with the service account email
   (e.g. `mybot@myproject.iam.gserviceaccount.com`) as **Editor**

### 3. YouTube Data API key

1. In the same Google Cloud project, go to **APIs & Services → Credentials**
2. Create an **API Key** (restrict it to YouTube Data API v3)
3. Copy the key into `.env`

### 4. Environment variables

Create `.env` in the project root:

```
YOUTUBE_API_KEY=AIza...
GOOGLE_CREDENTIALS_FILE=leads-service-account.json
ANTHROPIC_API_KEY=          # optional — not used in normal --no-claude flow
OUTPUT_SHEET_ID=            # Google Sheet ID for results (from the URL)
INPUT_SHEET_ID=             # optional — Google Sheet ID for input leads
```

The Sheet ID is the long string in the URL:
`https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit`

### 5. Verify setup

```bash
# Unit tests only — no API keys needed
python test_cases.py --unit-only

# Full API test — requires YOUTUBE_API_KEY (~1,300 quota units)
python test_cases.py
```

---

## Running leads

### Normal flow (recommended)

Drop a CSV in the project root, then use the skill inside Claude Code:

```
/qualify-leads 25              # process 25 new leads using .env defaults
/qualify-leads                 # process all new leads
/qualify-leads 10 "leads.csv" "SHEET_ID"   # explicit input/output
```

The skill runs `batch_qualify.py --no-claude`, outputs JSON, then presents each
`STAGE2_NEEDED` row to you for in-session judgment before writing to the sheet.

### Direct CLI (advanced)

```bash
# --no-claude: output JSON to stdout for the skill to handle
python batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID \
  --no-claude \
  --limit 25

# Full end-to-end with Anthropic API (costs API credits)
python batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID

# Write pre-judged results from a JSON file
python batch_qualify.py \
  --write-results results.json \
  --output-sheet YOUR_SHEET_ID \
  --tab-name "March 2026"
```

**Always use `--no-claude` in the normal workflow.** You have a Claude subscription
— Stage 2 judgment happens in-session at no extra cost.

---

## Output sheet structure

Each run creates or appends to these tabs:

| Tab | Contents | Behavior |
|-----|----------|----------|
| `Leads YYYY-MM-DD HH:MM` | Qualified leads only (conditions A–F) | New tab per run |
| `Discards` | DISCARD_SIZE, DISCARD_PRESCREEN, DISCARD_OFFER rows | Appended across runs |
| `Errors` | YouTube API failures and crashes | Appended across runs |
| `Sessions` | One row per run with full counts and quota estimate | Appended across runs |

### Leads tab columns

`Full Name` · `Job Title` · `Company` · `Company Size` · `Company LinkedIn URL` ·
`Personal LinkedIn URL` · `Company Website` · `Email Address` · `Other Contact Info` ·
`YouTube Channel URL` · `YouTube Status` · `Last LinkedIn Activity` · `Why Chosen` ·
`Offer Classification` · `Confidence` · `Multi Company` · `All Companies`

### Discard tab columns

`Full Name` · `Job Title` · `Company` · `Company Size` · `Personal LinkedIn URL` ·
`Company Website` · `Discard Reason` · `Mismatched Filters` · `Date Added`

---

## YouTube conditions

| Condition | Meaning | Gate |
|-----------|---------|------|
| `A` | No YouTube presence found | Stage 1 (deterministic) |
| `B` | Dead channel — last upload >60 days ago | Stage 1 (deterministic) |
| `C` | Inconsistent — 60+ day gap between recent uploads | Stage 1 (deterministic) |
| `D` | Raw podcast clips only | Stage 2 (human judgment) |
| `E` | Shorts only — all videos ≤60 seconds | Stage 1 (deterministic) |
| `F` | Off-topic content unrelated to their business | Stage 2 (human judgment) |
| `FAIL` | Active, polished channel — already doing content well | Stage 2 (human judgment) |
| `STAGE2_NEEDED` | Needs human review (only appears in `--no-claude` mode) | — |
| `ERROR` | YouTube API failure (quota, network) | — |

**Discard conditions** (never reach YouTube API):

| Condition | Meaning |
|-----------|---------|
| `DISCARD_PRESCREEN` | Sales Nav filter mismatch on employee count or matching filters |
| `DISCARD_SIZE` | Company >50 employees (from LinkedIn employee count) |
| `DISCARD_OFFER` | Website classified as B2C, low-ticket, or no website found |

---

## Input CSV format (Sales Navigator export)

The pipeline expects a Sales Navigator **Advanced** export with these column groups.
Up to 4 job experience groups are supported using numbered suffixes:

| Column | Suffix variants |
|--------|----------------|
| `first name` / `last name` | — |
| `linkedin url` | — |
| `company` | `company (2)` `company (3)` `company (4)` |
| `job title` | `job title (2)` … |
| `corporate website` | `corporate website (2)` … |
| `corporate linkedin url` | … |
| `linkedin employees` | … (range, e.g. `11-50`) |
| `linkedin company employee count` | … (exact, e.g. `47`) |
| `linkedin description` | … |
| `linkedin specialities` | … |
| `linkedin industry` | … |
| `job started on` / `job ended on` | … |
| `mismatched filters` | — |
| `matching filters` | — |

Non-Sales-Nav CSVs work too — missing columns are silently ignored.

---

## Multi-company leads

When a person has multiple active roles (e.g. founder + advisor + board member),
`_normalize_row()` detects all active job groups and sets `multi_company_flag=True`.

If `target_niche` is passed to `process_leads()`, the pipeline scores each active
company by keyword overlap against the niche and reassigns the primary company to
the best match before running any gates.

The `All Companies` column in the output sheet shows all active roles pipe-separated.

---

## YouTube quota

- **Budget:** 10,000 units/day (resets at midnight Pacific)
- **Cost per lead that reaches the API:** ~100 units (A — search only) to ~300 units (B–F — search + channel data)
- **Typical throughput:** 50–100 leads/day after pre-filters
- **Quota exceeded:** The pipeline gracefully records `ERROR` and exits. Re-run the
  next day — dedup skips already-processed leads automatically.
- **Sessions tab** tracks estimated quota used per run.

---

## Key functions reference

### `batch_qualify.py`

| Function | Purpose |
|----------|---------|
| `_normalize_row(row)` | Parse one CSV row into a standardised profile dict |
| `_extract_job_group(row, suffix)` | Extract one Sales Nav job experience group |
| `parse_company_size(s)` | Parse `"11-50"` / `"47"` / `"myself only"` → int or None |
| `parse_mismatched_filters(s)` | Parse Sales Nav mismatched filters → `{exp_1: [...]}` |
| `should_prescreen_discard(profile)` | Apply prescreen rules → `(bool, reason)` |
| `classify_website_offer(url)` | Fetch + keyword-score website → `(classification, reason)` |
| `score_company_for_niche(company_dict, niche)` | Score company against target niche |
| `process_leads(rows, ...)` | Run full pipeline; returns `(results, summary)` |
| `write_to_sheet(results, sheet_id)` | Write Leads/Discards/Errors tabs |
| `write_session_summary(sheet_id, summary)` | Append one row to Sessions tab |
| `generate_session_id()` | Return `YYYY-MM-DD-NNN` session ID |

### `youtube_qualifier.py`

| Function | Purpose |
|----------|---------|
| `qualify_youtube(person, company, website, no_claude)` | Main entry point |
| `_discover_channel(person, company, website)` | 4-stage channel discovery |
| `_name_match(text, person, company)` | Token-based name match with stop words |
| `_run_stage_1(videos, channel_info)` | Deterministic condition checks (B/C/E) |
| `_run_stage_2(videos, channel_info, ...)` | Claude API judgment (D/F/FAIL) |
| `search_youtube_channels(query, max_results)` | YouTube search API wrapper |
| `get_channel_videos(channel_id)` | Fetch channel metadata + recent videos |

---

## Testing

```bash
# Unit tests only — instant, no credentials
python test_cases.py --unit-only

# Full suite — requires YOUTUBE_API_KEY (~1,300 units)
python test_cases.py

# Individual utility tests
python batch_qualify.py --test-normalize   # test _normalize_row on a CSV
python batch_qualify.py --test-size        # test parse_company_size
python youtube_qualifier.py --test-name-match  # test _name_match

# Quick qualifier smoke test
python youtube_qualifier.py "Person Name" "Company Name"
```

---

## Rules

- **Always use `--no-claude`** in the pipeline. Stage 2 happens in-session.
- **Never commit** `.env` or `leads-service-account.json`.
- **Output sheet must exist** and be shared with the service account before running.
- **Industry mismatches** in Sales Nav filters are intentionally ignored —
  LinkedIn's industry categories don't map cleanly to ContentScale's ICP.
- **`DISCARD_OFFER` on `NO_WEBSITE`** is intentional — no website = can't verify
  high-ticket B2B offer.
- **`FETCH_FAILED` and `UNCLEAR`** from the website classifier proceed to YouTube
  with a flag — they are not discarded.
- **Quota errors auto-resume** — dedup reads the output sheet and skips already-
  processed leads, so you can re-run the next day without losing progress.
