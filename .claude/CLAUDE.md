# ContentScale — Leads Qualification Pipeline

## What this project does

Takes a CSV or Google Sheet of raw B2B leads exported from Sales Navigator,
runs them through a multi-stage qualification pipeline, and writes enriched
results to a Google Sheet.

**The goal:** find founders and executives who run small B2B service businesses
and currently have no (or poor) YouTube presence — these are ContentScale's
ideal clients.

**Qualification order — each gate discards leads before the next runs:**

1. **Dedup** — skips leads already present in the output sheet
2. **Prescreen** — discards leads whose Sales Nav mismatched-filter column signals
   the primary company is too large or doesn't match the search criteria
3. **Size gate** — discards companies with >50 employees
4. **Multi-company scoring** — if a person has multiple active roles, scores each
   company by title seniority, tenure, size, and niche fit; promotes the best
   match as the primary company before further checks
5. **Website classifier** — fetches the company website and keyword-scores it;
   discards B2C, low-ticket, and leads with no website
6. **YouTube analysis** — per-company channel discovery (website scrape first,
   then search) + Stage 1 deterministic checks; rows needing human judgment
   come back as `STAGE2_NEEDED`
7. **Stage 2 judgment** — done in-session by the human reviewer via the
   `/qualify-leads` skill (no API cost)

---

## Project structure

```
/
├── .env                              ← API keys (never commit)
├── leads-service-account.json        ← Google service account key (never commit)
├── session_counter.json              ← Auto-generated; tracks daily session IDs
├── batch_qualify.py                  ← Batch runner, all gates, Sheets writer
├── youtube_qualifier.py              ← YouTube channel discovery + qualification
├── test_cases.py                     ← Unit + API test suite
├── requirements.txt
├── .venv/                            ← Python virtualenv
└── .claude/
    ├── CLAUDE.md                     ← This file
    ├── settings.json                 ← Claude Code permission settings
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

The pipeline writes results to Google Sheets using a service account (a bot
identity that can be granted access to specific sheets).

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **YouTube Data API v3** and **Google Sheets API**
3. Create a Service Account → download the JSON key
4. Save the key as `leads-service-account.json` in the project root
5. Create your output Google Sheet, then share it with the service account email
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
TARGET_NICHE=               # optional — e.g. "B2B consulting SaaS" for multi-company scoring
```

The Sheet ID is the long string in the URL:
`https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit`

### 5. Verify setup

```bash
# Unit tests only — no API keys needed, runs instantly
.venv/bin/python3 test_cases.py --unit-only

# Full suite — requires YOUTUBE_API_KEY (~1,300 quota units)
.venv/bin/python3 test_cases.py
```

---

## Running leads

### Normal flow (recommended)

Drop a CSV in the project root, then run the skill inside Claude Code:

```
/qualify-leads 25              # process 25 new leads using .env defaults
/qualify-leads                 # process all new leads
/qualify-leads 10 "leads.csv" "SHEET_ID"   # explicit input/output
```

The skill:
1. Runs `batch_qualify.py --no-claude` — all gates up to YouTube Stage 1
2. Outputs JSON to stdout
3. Presents each `STAGE2_NEEDED` row to you for in-session judgment
4. Writes the final results to the output Google Sheet

### Direct CLI (advanced)

```bash
# --no-claude: output JSON to stdout for the skill to handle
.venv/bin/python3 batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID \
  --no-claude \
  --limit 25

# Full end-to-end with Anthropic API (costs API credits)
.venv/bin/python3 batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID

# Write pre-judged results from a JSON file
.venv/bin/python3 batch_qualify.py \
  --write-results results.json \
  --output-sheet YOUR_SHEET_ID \
  --tab-name "March 2026"
```

**Always use `--no-claude` in the normal workflow.** Stage 2 judgment happens
in-session inside Claude Code — no Anthropic API key or credits needed.

---

## Output sheet structure

Each run creates or appends to these tabs:

| Tab | Contents | Behavior |
|-----|----------|----------|
| `Leads YYYY-MM-DD HH:MM` | Qualified leads (conditions A–F) | New tab per run |
| `Discards` | DISCARD_SIZE, DISCARD_PRESCREEN, DISCARD_OFFER rows | Appended across runs |
| `Errors` | YouTube API failures and crashes | Appended across runs |
| `Sessions` | One row per run with full counts and quota estimate | Appended across runs |

### Leads tab columns

`Full Name` · `Job Title` · `Company` · `Company Size` · `Company LinkedIn URL` ·
`Personal LinkedIn URL` · `Company Website` · `Email Address` · `Other Contact Info` ·
`YouTube Channel URL` · `YouTube Status` · `Last LinkedIn Activity` · `Why Chosen` ·
`Offer Classification` · `Confidence` · `Multi Company` · `All Companies` ·
`Primary Score` · `Score Detail` · `YouTube Resolution` · `Secondary Channels`

### Discard tab columns

`Full Name` · `Job Title` · `Company` · `Company Size` · `Personal LinkedIn URL` ·
`Company Website` · `Discard Reason` · `Mismatched Filters` · `Date Added`

---

## YouTube conditions

A lead is **qualified** (written to the Leads tab) if it gets condition A–F.
A lead **fails** if it gets FAIL or REVIEW_FAIL.

| Condition | Meaning | How decided |
|-----------|---------|-------------|
| `A` | No YouTube presence found | Stage 1 — deterministic |
| `B` | Dead channel — last upload >60 days ago | Stage 1 — deterministic |
| `C` | Inconsistent — 60+ day gap between recent uploads | Stage 1 — deterministic |
| `D` | Raw podcast clips only — no produced content | Stage 2 — human judgment |
| `E` | Shorts only — all videos ≤60 seconds | Stage 1 — deterministic |
| `F` | Off-topic content unrelated to their business | Stage 2 — human judgment |
| `FAIL` | Active, polished channel — already doing content well | Stage 2 — human judgment |
| `REVIEW_FAIL` | Secondary company has an active channel — manual review needed | Resolution rule |
| `STAGE2_NEEDED` | Needs human review (only in `--no-claude` mode) | — |
| `ERROR` | YouTube API failure (quota, network) | — |

**Discard conditions** (lead never reaches YouTube analysis):

| Condition | Meaning |
|-----------|---------|
| `DISCARD_PRESCREEN` | Sales Nav filter mismatch — employee count or matching filters |
| `DISCARD_SIZE` | Company >50 employees (from LinkedIn employee count) |
| `DISCARD_OFFER` | Website classified as B2C, low-ticket, or no website found |

---

## How multi-company leads work

Many Sales Navigator exports include people with multiple active roles
(e.g. someone who is both a founder of a small agency and an advisor at a
larger company).

**What the pipeline does:**

1. `_normalize_row()` detects all job groups where `job ended on` is blank
   and builds an `active_companies` list.
2. `rank_active_companies()` scores each company using:
   - **Title score** (×3) — Founder/CEO scores 3; Advisor scores 0; Manager scores -1
   - **Tenure score** (×2) — 6+ years scores 5; <6 months scores 0
   - **Size score** (×2) — 1–5 employees scores 3; >50 scores -2
   - **Niche fit** (×1) — keyword overlap with `TARGET_NICHE` env var
   - **Has website** (+1) and **has description** (+1)
3. The highest-scoring company becomes the primary and is used for all
   subsequent gates (size check, website classifier, YouTube).
4. `qualify_youtube()` runs discovery for **every** active company, not just
   the primary. If any company has an active polished channel (FAIL), the
   lead is discarded — even if it's a secondary role.
5. `Primary Score` and `Score Detail` columns in the output show exactly how
   the primary was selected, so you can audit or override the decision.

The `All Companies` column shows all active roles pipe-separated.

---

## How YouTube channel discovery works

For each company, discovery runs in 4 stages (stops at first success):

1. **Website scrape** (free) — checks up to 6 pages (`/`, `/about`, `/contact`,
   etc.) for YouTube links. Most legitimate business channels are linked from
   the company website, making this the most reliable signal.
2. **Company name search** — YouTube API search by company name; validates
   results by name-matching the channel title/description against the person
   and company name.
3. **Person name search** — same search and validation using the person's name.
4. **Combined search** (last resort) — `"person name + company name"` search
   with relaxed validation (no cross-validation required).

Cross-validation (stages 2–3): if the channel lists a website on its About
page, that website must match the company's website domain. Channels with no
listed website pass on name match alone.

---

## Input CSV format (Sales Navigator export)

The pipeline expects a Sales Navigator **Advanced** export. Up to 4 job
experience groups are supported using numbered suffixes (`(2)`, `(3)`, `(4)`):

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

## YouTube quota

- **Budget:** 10,000 units/day (resets at midnight Pacific)
- **Cost per lead:** ~0 units if channel found via website scrape; ~100 units
  if found via search (A); ~300 units for B–F results (search + channel data)
- **Multi-company leads cost more** — each active company runs its own discovery
- **Typical throughput:** 50–100 leads/day after pre-filters
- **Quota exceeded:** The pipeline records `ERROR` and stops. Re-run the next
  day — dedup skips already-processed leads automatically.
- **Sessions tab** tracks estimated quota used per run.

---

## Key functions reference

### `batch_qualify.py`

| Function | Purpose |
|----------|---------|
| `_normalize_row(row)` | Parse one CSV row into a standardised profile dict |
| `_extract_job_group(row, suffix)` | Extract one Sales Nav job experience group |
| `parse_company_size(s)` | Parse `"11-50"` / `"47"` / `"myself only"` → int or None |
| `parse_tenure_months(started_on)` | Parse job start date → months in role |
| `parse_mismatched_filters(s)` | Parse Sales Nav mismatched filters → `{exp_1: [...]}` |
| `score_job_title(title)` | Score title by operational control (3=Founder, -1=Specialist) |
| `score_company_size(size_string)` | Score size as founder-led signal (3=1–5 staff, -2=>50) |
| `score_niche_fit(company_dict, niche)` | Keyword overlap score against target niche (0–5) |
| `rank_active_companies(companies, niche)` | Score + sort all active companies, best-first |
| `should_prescreen_discard(profile)` | Apply prescreen rules → `(bool, reason)` |
| `classify_website_offer(url)` | Fetch + keyword-score website → `(classification, reason)` |
| `process_leads(rows, ...)` | Run full pipeline; returns `(results, summary)` |
| `write_to_sheet(results, sheet_id)` | Write Leads/Discards/Errors tabs |
| `write_session_summary(sheet_id, summary)` | Append one row to Sessions tab |
| `generate_session_id()` | Return `YYYY-MM-DD-NNN` session ID |

### `youtube_qualifier.py`

| Function | Purpose |
|----------|---------|
| `qualify_youtube(person, company, website, no_claude, active_companies)` | Main entry point |
| `qualify_all_companies(active_companies, person_name, no_claude)` | Run discovery for each company |
| `resolve_company_youtube_results(company_results)` | Apply resolution rule across all company results |
| `discover_channel_for_company(company_dict, person_name)` | 4-stage per-company channel discovery |
| `_scrape_website_for_channel(base_url, person, company)` | Scrape website pages for YT links (free) |
| `_search_and_validate(query, ...)` | YouTube search + name-match + cross-validation |
| `_extract_youtube_channel_links(soup)` | Pull channel URLs from page HTML |
| `_websites_match(url1, url2)` | Domain-level URL comparison |
| `_name_match(text, person, company)` | Token-based name match with stop words |
| `_run_stage_1(videos, channel_info)` | Deterministic condition checks (B/C/E) |
| `_run_stage_2(videos, channel_info, ...)` | Claude API judgment (D/F/FAIL) |
| `search_youtube_channels(query, max_results)` | YouTube search API wrapper |
| `get_channel_videos(channel_id)` | Fetch channel metadata + recent videos |

---

## Testing

```bash
# Unit tests only — instant, no credentials
.venv/bin/python3 test_cases.py --unit-only

# Full suite — requires YOUTUBE_API_KEY (~1,300 units)
.venv/bin/python3 test_cases.py

# Individual utility tests
.venv/bin/python3 batch_qualify.py --test-normalize   # test _normalize_row on a CSV
.venv/bin/python3 batch_qualify.py --test-size        # test parse_company_size
.venv/bin/python3 youtube_qualifier.py --test-name-match  # test _name_match

# Quick YouTube smoke test (requires YOUTUBE_API_KEY)
.venv/bin/python3 youtube_qualifier.py "Person Name" "Company Name"
```

Tests are split into suites:
- **Unit tests** — parse helpers (`parse_company_size`, `parse_mismatched_filters`)
- **7A tests** — multi-company scoring (`parse_tenure_months`, `score_job_title`, `rank_active_companies`)
- **7B tests** — YouTube resolution logic (`_websites_match`, `_extract_youtube_channel_links`, `resolve_company_youtube_results`)
- **API tests** — live YouTube API calls (Condition A, B, STAGE2_NEEDED)

---

## Rules

- **Always use `--no-claude`** in the pipeline. Stage 2 judgment happens in-session.
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
- **`REVIEW_FAIL`** means a secondary company has an active channel — review manually
  before dismissing, as the person may have abandoned that channel.
