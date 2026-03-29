# ContentScale — Leads Qualification Pipeline
## Usage Guide & Configuration Manual

---

## Table of Contents

1. [What this pipeline does](#1-what-this-pipeline-does)
2. [First-time setup](#2-first-time-setup)
3. [Running the pipeline](#3-running-the-pipeline)
4. [Understanding the output](#4-understanding-the-output)
5. [YouTube conditions reference](#5-youtube-conditions-reference)
6. [Configuration manual — `pipeline_config.json`](#6-configuration-manual) · [`batch`](#batch--batch-processing-defaults) · [`website_classifier`](#website_classifier--website-fetch-settings) · [`input`](#input--column-mapping-and-source-settings) · [`size_gate`](#size_gate--filter-by-employee-count) · [`prescreen`](#prescreen--sales-navigator-filter-mismatch-rules) · and more
7. [Using non-Sales-Navigator CSV files](#7-using-non-sales-navigator-csv-files)
8. [Multi-company leads](#8-multi-company-leads)
9. [YouTube quota management](#9-youtube-quota-management)
10. [Adding a new filter](#10-adding-a-new-filter)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What this pipeline does

Takes a CSV or Google Sheet of raw B2B leads and qualifies each one for outreach from ContentScale.

**The ideal lead:** a founder or senior executive running a small B2B service business (1–100 employees) with little or no YouTube presence — someone who would benefit from content-led growth but isn't already doing it well.

**Qualification gates (in order):**

| Gate | What it does |
|------|-------------|
| Dedup | Skips leads already written to the output sheet |
| Prescreen | Discards Sales Navigator leads that failed the search filters |
| Size gate | Discards companies outside the configured employee range (default: 1–100) |
| Multi-company scoring | When a person has multiple active roles, selects the most relevant one as the primary company |
| Website classifier | Fetches the company website; discards B2C, low-ticket, or missing websites |
| YouTube analysis | Checks for YouTube presence per company; Stage 1 is deterministic; Stage 2 needs human judgment |

Each gate runs only on leads that passed all previous gates. This keeps YouTube quota costs low.

---

## 2. First-time setup

### 2a. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2b. Google Cloud — Service Account

The pipeline writes results to Google Sheets using a service account (a bot identity that you grant access to your sheet).

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project
3. Enable **YouTube Data API v3** and **Google Sheets API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
5. Download the JSON key file
6. Save it as `leads-service-account.json` in the project root
7. Create your output Google Sheet
8. Share the sheet with the service account email (e.g. `mybot@myproject.iam.gserviceaccount.com`) as **Editor**

### 2c. YouTube API key

1. In the same Google Cloud project, go to **APIs & Services → Credentials**
2. Click **Create Credentials → API Key**
3. Optionally restrict it to YouTube Data API v3
4. Copy the key into `.env`

### 2d. Environment variables

Create a `.env` file in the project root:

```dotenv
YOUTUBE_API_KEY=AIza...
GOOGLE_CREDENTIALS_FILE=leads-service-account.json
ANTHROPIC_API_KEY=                # optional — not used in normal --no-claude flow
OUTPUT_SHEET_ID=                  # Google Sheet ID for results (see below)
INPUT_SHEET_ID=                   # optional — Google Sheet ID for input leads
TARGET_NICHE=B2B consulting       # optional — used for multi-company scoring
```

**Finding your Sheet ID:** it's the long string in the URL between `/d/` and `/edit`:
```
https://docs.google.com/spreadsheets/d/THIS_IS_THE_SHEET_ID/edit
```

### 2e. Verify setup

```bash
# Unit tests only — no API keys needed, runs in seconds
.venv/bin/python3 test_cases.py --unit-only

# Full test suite — requires YOUTUBE_API_KEY (~1,300 quota units)
.venv/bin/python3 test_cases.py
```

---

## 3. Running the pipeline

### Recommended: use the skill inside Claude Code

Drop a CSV in the `Input_lists/` folder (gitignored — safe for personal data), then run the skill:

```
/qualify-leads                          # process up to batch.default_limit new leads (default: 15)
/qualify-leads 25                       # process first 25 new leads
/qualify-leads 0                        # process ALL new leads (no limit)
/qualify-leads 10 "leads.csv" "SHEET_ID"  # explicit input and output
```

The skill handles the full flow:
1. Runs `batch_qualify.py --no-claude` — all gates through YouTube Stage 1
2. Presents any `STAGE2_NEEDED` rows for your in-session judgment (no API cost)
3. Writes the final enriched results to the output Google Sheet

### Direct CLI (advanced use)

```bash
# Process leads, output JSON to stdout (for the skill to handle)
.venv/bin/python3 batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID \
  --no-claude \
  --limit 25

# Use a custom config file
.venv/bin/python3 batch_qualify.py \
  --input leads.csv \
  --output-sheet YOUR_SHEET_ID \
  --no-claude \
  --config my_custom_config.json

# Write pre-judged results from a JSON file
.venv/bin/python3 batch_qualify.py \
  --write-results /tmp/results.json \
  --output-sheet YOUR_SHEET_ID \
  --tab-name "March 2026"
```

**Always use `--no-claude` when running the skill.** Stage 2 judgment (D vs F vs FAIL) happens in-session inside Claude Code — no Anthropic API key or credits needed.

---

## 4. Understanding the output

Each run creates or appends to these tabs in the output sheet:

| Tab | Contents | Behavior |
|-----|----------|----------|
| `Leads YYYY-MM-DD HH:MM` | Qualified leads (conditions A–F + REVIEW_FAIL) | New tab per run |
| `Discards` | All DISCARD_* rows | Appended across runs |
| `Errors` | YouTube API failures | Appended across runs |
| `Sessions` | One summary row per run | Appended across runs |

### Leads tab columns

| Column | Description |
|--------|-------------|
| Full Name | First + last name |
| Job Title | Title at primary company |
| Company | Primary company name |
| Company Size | Employee count or range |
| Company LinkedIn URL | Corporate LinkedIn page |
| Personal LinkedIn URL | Person's LinkedIn profile |
| Company Website | Primary company website |
| Email Address | Email if available |
| Other Contact Info | Phone or other contact |
| YouTube Channel URL | Channel URL if found |
| YouTube Status | Condition (A–F, FAIL, REVIEW_FAIL) |
| Last LinkedIn Activity | Date of last LinkedIn post |
| Why Chosen | 2–3 sentence explanation of fit |
| Offer Classification | Website classifier result |
| Confidence | Normal / Multi-Company / Low Confidence / etc. |
| Multi Company | `true` if multiple active roles |
| All Companies | All active roles, pipe-separated |
| Primary Score | Score of the selected primary company |
| Score Detail | JSON breakdown of scoring weights |
| YouTube Resolution | How multi-company YouTube results were resolved |
| Secondary Channels | YouTube channels found at secondary companies |

### Discards tab columns

| Column | Description |
|--------|-------------|
| Full Name | — |
| Job Title | — |
| Company | — |
| Company Size | — |
| Personal LinkedIn URL | — |
| Company Website | — |
| Discard Reason | Condition code (e.g. `DISCARD_SIZE`) |
| Mismatched Filters | Raw Sales Navigator mismatched filters value |
| Date Added | Timestamp of discard |

---

## 5. YouTube conditions reference

### Qualified conditions (written to Leads tab)

| Condition | Meaning | How decided |
|-----------|---------|-------------|
| `A` | No YouTube presence found at all | Stage 1 — deterministic |
| `B` | Dead channel — last upload >60 days ago | Stage 1 — deterministic |
| `C` | Inconsistent — 60+ day gap between recent uploads | Stage 1 — deterministic |
| `D` | Raw podcast clips only — no produced content | Stage 2 — human judgment |
| `E` | Shorts only — all videos ≤60 seconds | Stage 1 — deterministic |
| `F` | Off-topic content unrelated to their business | Stage 2 — human judgment |
| `REVIEW_FAIL` | Secondary company has an active polished channel — manual review needed | Resolution rule |

### Disqualified conditions

| Condition | Meaning |
|-----------|---------|
| `FAIL` | Active, polished channel — already doing content well |
| `DISCARD_PRESCREEN` | Sales Nav filter mismatch (company too large or didn't match search) |
| `DISCARD_SIZE` | Company is outside the configured employee range |
| `DISCARD_OFFER` | Website classified as B2C, low-ticket, or no website found |

### Internal conditions (not written to sheet)

| Condition | Meaning |
|-----------|---------|
| `STAGE2_NEEDED` | Needs human judgment (only in `--no-claude` mode) |
| `ERROR` | YouTube API failure (quota exceeded, network error) |
| `SKIP_NO_EMAIL` | Lead skipped because `skip_if_no_email` is enabled and email is missing |

---

## 6. Configuration manual

All pipeline behaviour is controlled by `pipeline_config.json` in the project root. You can also pass `--config path/to/file.json` on the CLI to use a different file.

Every filter section has an `"enabled"` field. **Setting `"enabled": false` completely skips that filter.** New filters default to `false` — existing behaviour is unchanged until you opt in.

---

### `batch` — Batch processing defaults

Controls default behaviour when running the pipeline without explicit CLI arguments.

```json
"batch": {
  "default_limit": 15
}
```

| Field | Description |
|-------|-------------|
| `default_limit` | How many new leads to process when no `--limit` flag is given (and no limit is passed to `/qualify-leads`). Set to `null` to process all leads by default. |

---

### `website_classifier` — Website fetch settings

Controls how the offer classifier fetches and reads company websites.

```json
"website_classifier": {
  "fetch_timeout": 6,
  "page_char_limit": 3500
}
```

| Field | Description |
|-------|-------------|
| `fetch_timeout` | Seconds to wait for a website response before giving up with `FETCH_FAILED`. Increase if you're seeing many timeouts on slow sites. |
| `page_char_limit` | How many characters of page body text to analyse. Higher values are more thorough but slower. |

---

### `input` — Column mapping and source settings

Controls how columns from your CSV are mapped to the standard internal names the pipeline uses.

```json
"input": {
  "column_map": {
    "first name":    "first name",
    "email":         "email address",
    "company":       "company"
  },
  "multi_company_suffixes": ["", " (2)", " (3)", " (4)"],
  "source_type": "sales_navigator"
}
```

| Field | Description |
|-------|-------------|
| `column_map` | Maps internal pipeline names (keys) to actual CSV column names (values). Only override the ones that differ. |
| `multi_company_suffixes` | The suffixes used for multi-company job groups in your CSV. Sales Navigator uses `""`, `" (2)"`, `" (3)"`, `" (4)"`. |
| `source_type` | `"sales_navigator"` enables the prescreen filter. Use `"generic"` for any other CSV source. |

**Full list of mappable internal names:**

```
first name, last name, linkedin url, email, location, job title, company,
corporate website, corporate linkedin url, linkedin employees,
linkedin company employee count, linkedin description, linkedin specialities,
linkedin industry, company revenue, job started on, job ended on,
mismatched filters, matching filters, last linkedin activity
```

---

### `size_gate` — Filter by employee count

Discards companies outside the min/max employee range.

```json
"size_gate": {
  "enabled": true,
  "min_employees": 1,
  "max_employees": 100
}
```

| Field | Description |
|-------|-------------|
| `min_employees` | Minimum number of employees to keep. Use `1` to keep solopreneurs and above. |
| `max_employees` | Maximum number of employees to keep. Leads above this are discarded as `DISCARD_SIZE`. |

---

### `prescreen` — Sales Navigator filter mismatch rules

Discards leads whose Sales Navigator `mismatched filters` column signals the primary company doesn't match your search criteria. Only applies when `source_type` is `"sales_navigator"`.

```json
"prescreen": {
  "enabled": true,
  "rules": {
    "primary_employee_count_mismatch": false,
    "all_companies_employee_count_mismatch": false,
    "matching_filters_false": false
  }
}
```

| Rule | When it discards | Default |
|------|-----------------|---------|
| `primary_employee_count_mismatch` | The primary company's LinkedIn employee count doesn't match your Sales Nav search filter | `false` |
| `all_companies_employee_count_mismatch` | Every company on the profile has a mismatched employee count | `false` |
| `matching_filters_false` | The `matching filters` column is `"false"` | `false` |

The employee count rules are off by default because Sales Nav's internal index can lag behind what's exported in the CSV — the `size_gate` handles employee count filtering with the actual data. Enable `matching_filters_false` if you want to drop leads Sales Nav itself flagged as not matching your search.

Turn off individual rules by setting them to `false`. Setting `"enabled": false` disables all prescreen checks.

---

### `title_filter` — Filter by job title

Keeps or discards leads based on their job title. Substring match, case-insensitive.

```json
"title_filter": {
  "enabled": false,
  "require_any": ["founder", "owner", "ceo", "director"],
  "exclude_any": ["intern", "coordinator", "assistant"]
}
```

| Field | Description |
|-------|-------------|
| `require_any` | Lead must match at least one of these substrings. Leave empty `[]` to allow all titles. |
| `exclude_any` | Lead is discarded if their title contains any of these substrings. Leave empty `[]` to exclude nothing. |

---

### `location_filter` — Filter by location

Keeps or excludes leads based on the location field.

```json
"location_filter": {
  "enabled": false,
  "mode": "include",
  "values": ["United States", "Canada", "United Kingdom"]
}
```

| Field | Description |
|-------|-------------|
| `mode` | `"include"` keeps only leads from listed locations. `"exclude"` drops them. |
| `values` | Substring list, case-insensitive. `"United States"` matches any location containing that string. |

---

### `industry_filter` — Filter by LinkedIn company industry

Keeps or excludes leads based on their company's LinkedIn industry category.

```json
"industry_filter": {
  "enabled": false,
  "mode": "exclude",
  "values": [
    "Higher Education",
    "Government Administration",
    "Non-profit Organizations"
  ]
}
```

| Field | Description |
|-------|-------------|
| `mode` | `"include"` or `"exclude"` (same as location_filter) |
| `values` | Substring list matched against the `linkedin industry` column |

> **Note:** LinkedIn's industry categories are broad and inconsistently assigned. Use this filter cautiously — an "exclude" list for clearly irrelevant industries (education, government, non-profit) is the most reliable use case.

---

### `keyword_filter` — Keyword scan across company profile fields

Requires or excludes leads based on keywords found in the company's profile text.

```json
"keyword_filter": {
  "enabled": false,
  "fields": ["company_description", "company_specialities"],
  "require_any": [],
  "exclude_any": ["mlm", "dropshipping", "crypto", "network marketing"]
}
```

| Field | Description |
|-------|-------------|
| `fields` | Which fields to scan. Options: `"company_description"`, `"company_specialities"`, `"summary"`, `"headline"` |
| `require_any` | Lead must have at least one keyword present across all scanned fields. Leave empty `[]` to not require anything. |
| `exclude_any` | Lead is discarded if any keyword is found across the scanned fields. |

---

### `revenue_filter` — Filter by LinkedIn revenue range

Keeps only leads whose company revenue falls within the specified range. Uses LinkedIn's revenue data.

```json
"revenue_filter": {
  "enabled": false,
  "min_usd": null,
  "max_usd": null
}
```

| Field | Description |
|-------|-------------|
| `min_usd` | Minimum revenue in USD (integer). `null` means no lower bound. Example: `500000` = $500K |
| `max_usd` | Maximum revenue in USD (integer). `null` means no upper bound. Example: `10000000` = $10M |

> **Note:** LinkedIn revenue ranges are approximate and not available for all companies. Leads missing revenue data pass this filter — they are not discarded.

---

### `tenure_filter` — Filter by minimum time in role

Discards leads who have been at their primary company for fewer months than the minimum.

```json
"tenure_filter": {
  "enabled": false,
  "min_months_at_primary": 6
}
```

| Field | Description |
|-------|-------------|
| `min_months_at_primary` | Minimum months the lead must have been at their primary company. Leads with no start date pass through. |

---

### `primary_score_filter` — Filter by multi-company scoring result

Discards leads whose primary company scored below a threshold, or where the primary didn't clearly beat the second-best option.

```json
"primary_score_filter": {
  "enabled": false,
  "min_score": 10,
  "min_score_margin": 0
}
```

| Field | Description |
|-------|-------------|
| `min_score` | Primary company must have at least this score. Leads with only one company always pass. |
| `min_score_margin` | Primary must beat the second-best company by at least this many points. Use `0` to disable margin checking. |

---

### `multi_company_filter` — Controls for multi-role leads

Limits or adjusts handling of leads with many active roles.

```json
"multi_company_filter": {
  "enabled": false,
  "max_active_roles": null
}
```

| Field | Description |
|-------|-------------|
| `max_active_roles` | Discard leads with more than this many active roles. `null` means no limit. |

---

### `contact_filter` — Require contact information

Hard requirements on what contact data must be present.

```json
"contact_filter": {
  "require_email": false,
  "require_linkedin": false
}
```

> **Note:** This filter does not use a top-level `"enabled"` field — each boolean is its own switch.

| Field | Description |
|-------|-------------|
| `require_email` | If `true`, leads without an email address are discarded. |
| `require_linkedin` | If `true`, leads without a personal LinkedIn URL are discarded. |

---

### `activity_filter` — Filter by recent LinkedIn activity

Discards leads who haven't posted on LinkedIn recently.

```json
"activity_filter": {
  "enabled": false,
  "max_days_since_activity": 180
}
```

| Field | Description |
|-------|-------------|
| `max_days_since_activity` | Leads whose last LinkedIn activity was more than this many days ago are discarded. Requires the `last linkedin activity` column to be present. |

---

### `offer_classifier` — Website offer classification behaviour

Controls what happens to leads based on the website classifier's result.

```json
"offer_classifier": {
  "enabled": true,
  "discard_on": ["B2C", "LOW_TICKET", "NO_WEBSITE"],
  "flag_only": ["FETCH_FAILED", "UNCLEAR"]
}
```

| Field | Description |
|-------|-------------|
| `discard_on` | Classifications that cause a `DISCARD_OFFER` discard. |
| `flag_only` | Classifications that pass the lead through but set a warning in the `confidence` column. |

**Possible classifier outputs:**

| Classification | Meaning |
|---------------|---------|
| `HIGH_TICKET_B2B` | Clear high-ticket B2B offer found — keep |
| `B2C` | Business sells primarily to consumers |
| `LOW_TICKET` | Pricing visible and under ~$1,000 |
| `NO_WEBSITE` | Website URL missing or unreachable |
| `FETCH_FAILED` | Website exists but couldn't be fetched (timeout, error) |
| `UNCLEAR` | Can't determine offer type from available content |

---

### `youtube` — YouTube API behaviour controls

```json
"youtube": {
  "skip_if_no_email": false,
  "max_companies_per_lead": null
}
```

| Field | Description |
|-------|-------------|
| `skip_if_no_email` | If `true`, leads without an email address skip YouTube analysis entirely (written as `SKIP_NO_EMAIL` to the Errors tab). Saves quota. |
| `max_companies_per_lead` | Cap how many companies per lead are checked for YouTube presence. `null` means check all active companies. Set to `1` to only check the primary company. |

---

### `icp` — ICP definition for multi-company scoring

Defines the target niche and scoring weights used when ranking a person's multiple active companies.

```json
"icp": {
  "target_niche": "",
  "scoring_weights": {
    "title":  3,
    "tenure": 2,
    "size":   2,
    "niche":  1
  }
}
```

| Field | Description |
|-------|-------------|
| `target_niche` | Keywords describing your ideal client niche. Overrides the `TARGET_NICHE` env var when set. Example: `"B2B SaaS consulting agency"` |
| `scoring_weights.title` | Multiplier for title seniority score (Founder/CEO = 3 pts, Manager = -1 pt) |
| `scoring_weights.tenure` | Multiplier for tenure score (6+ years = 5 pts, <6 months = 0 pts) |
| `scoring_weights.size` | Multiplier for company size score (1–5 employees = 3 pts, >50 = -2 pts) |
| `scoring_weights.niche` | Multiplier for keyword overlap with `target_niche` (0–5 pts) |

---

### `output` — Output sheet behaviour

```json
"output": {
  "leads_tab_prefix": "Leads",
  "write_discards": true,
  "write_errors": true,
  "write_sessions": true
}
```

| Field | Description |
|-------|-------------|
| `leads_tab_prefix` | Prefix for the timestamped Leads tab name. Default creates tabs named `Leads 2026-03-28 14:30`. |
| `write_discards` | If `false`, discarded leads are not written to the Discards tab. |
| `write_errors` | If `false`, API errors are not written to the Errors tab. |
| `write_sessions` | If `false`, the run summary row is not written to the Sessions tab. |

---

## 7. Using non-Sales-Navigator CSV files

The pipeline works with any CSV — you just need to tell it which column names map to the standard internal names.

**Example: a simple outbound list with different column names**

```json
"input": {
  "column_map": {
    "first name":        "First Name",
    "last name":         "Last Name",
    "email":             "Work Email",
    "job title":         "Title",
    "company":           "Organization",
    "corporate website": "Website",
    "location":          "City"
  },
  "multi_company_suffixes": [""],
  "source_type": "generic"
}
```

Key points:
- Set `source_type` to `"generic"` — this disables the prescreen filter (which requires Sales Nav columns)
- Set `multi_company_suffixes` to `[""]` if your CSV has only one job per row
- Any column not listed in `column_map` is left as-is — if your CSV already uses the standard internal names, you don't need to map them
- Missing columns are silently ignored — the pipeline skips checks that require data it doesn't have

---

## 8. Multi-company leads

When a lead has multiple active job entries (jobs with no end date), the pipeline:

1. **Scores each company** using title seniority, tenure, size, and niche fit
2. **Selects the highest-scoring company** as the primary for all subsequent gates
3. **Runs YouTube discovery on every active company** — if any company has an active polished channel, the lead gets `REVIEW_FAIL` (secondary company) or `FAIL` (primary company)

### Scoring breakdown

| Component | Max pts | Details |
|-----------|---------|---------|
| Title | 3 × weight | Founder/CEO/Owner = 3, C-suite = 2, VP/Director = 1, Manager = -1 |
| Tenure | 5 × weight | 6+ years = 5, 3-6 years = 4, 1-3 years = 3, 6mo-1yr = 2, <6mo = 0 |
| Size | 3 × weight | 1–5 = 3, 6–10 = 2, 11–50 = 1, 51–200 = 0, >200 = -1, >500 = -2 |
| Niche | 5 × weight | Keyword overlap with `target_niche` |
| Has website | +1 | Flat bonus |
| Has description | +1 | Flat bonus |

The `Score Detail` column in the output shows the exact breakdown for each qualified lead, so you can audit or override the selection.

### Auditing wrong primary company selections

If `title=0` and `tenure=0` in `Score Detail`, the scoring had little signal. Check the `All Companies` column and manually verify which company is their main focus before outreach.

---

## 9. YouTube quota management

- **Daily budget:** 10,000 units (resets at midnight Pacific time)
- **Cost per lead:** ~0 units if the channel is found via website scrape; ~100–300 units if found via API search
- **Multi-company leads cost more** — each active company runs its own discovery
- **Typical throughput:** 50–100 leads per day after pre-filters

### What happens when quota runs out

The pipeline writes `ERROR` rows with the quota error message and stops. You don't lose any progress — the dedup check reads the output sheet at the start of each run and automatically skips already-processed leads. Just re-run the next day.

### Saving quota

- Enable `skip_if_no_email: true` in the `youtube` section to skip leads without emails (you can't reach them anyway)
- Set `max_companies_per_lead: 1` to only check the primary company (faster, but you won't catch secondary FAIL cases)
- Use tight prescreen and size filters to reduce the number of leads that reach YouTube analysis

### Tracking quota usage

The Sessions tab records the estimated quota used per run. Check it to see how quickly you're burning through your daily budget.

---

## 10. Adding a new filter

The filter pipeline is designed to be extended without modifying existing code.

**Step 1:** Write a filter function in `pipeline_filters.py`:

```python
def filter_my_new_filter(profile: dict, config: dict) -> FilterResult:
    cfg = config.get("my_new_filter", {})
    if not cfg.get("enabled"):
        return _PASS

    # Read your config values
    some_value = cfg.get("some_value")

    # Read from the profile
    field = profile.get("some_field", "")

    # Return a discard if conditions are met
    if should_discard(field, some_value):
        return FilterResult(True, "DISCARD_MY_REASON", "Human-readable reason here")

    return _PASS
```

**Step 2:** Append it to `FILTER_PIPELINE` in `pipeline_filters.py`:

```python
FILTER_PIPELINE: list = [
    filter_prescreen,
    filter_size,
    # ... existing filters ...
    filter_my_new_filter,   # <-- add here
]
```

**Step 3:** Add a config section to `pipeline_config.json`:

```json
"my_new_filter": {
  "_comment": "Description of what this filter does.",
  "enabled": false,
  "some_value": "default"
}
```

**Step 4:** Add a `_comment` for any new `DISCARD_*` condition code to `CLAUDE.md` for future reference.

That's it. No changes to `batch_qualify.py` are needed — `apply_filters()` automatically runs every function in `FILTER_PIPELINE`.

**Need parse/score utilities in your filter?** Import them from `lead_utils.py` — that's the shared home for all primitives. Do not import from `batch_qualify.py` (circular import).

---

## 11. Troubleshooting

### "Quota exceeded" errors

Re-run the next day. The pipeline automatically skips already-processed leads.

If you're hitting quota too quickly:
- Enable `skip_if_no_email: true`
- Lower `max_companies_per_lead` to `1` or `2`
- Tighten prescreen and size filters to discard more leads before the YouTube step

### "Service account not found" / Sheets permission error

Make sure:
1. `leads-service-account.json` exists in the project root
2. The output sheet is shared with the service account email as **Editor**
3. `GOOGLE_CREDENTIALS_FILE` in `.env` matches the filename

### Wrong company selected as primary

Check the `Score Detail` column in the Leads tab. If the primary was selected with very low signal (title=0, tenure=0), verify manually. You can adjust scoring weights in the `icp.scoring_weights` section of `pipeline_config.json`.

### Leads not being found by dedup (re-processing already-done leads)

The dedup check reads the `Full Name` + `Company` combination from all existing Leads tabs. If you renamed or deleted a Leads tab, those leads will be processed again.

### Prescreen discarding too many (or too few) leads

Review the individual rules under `prescreen.rules`. All rules are **off by default**:
- `primary_employee_count_mismatch` and `all_companies_employee_count_mismatch` are disabled because Sales Nav's internal employee count often lags the exported data — the `size_gate` handles this more reliably
- `matching_filters_false` is the strongest signal (Sales Nav itself flagged the lead as not matching) — enable it if you want strict adherence to your search criteria

### Website classifier discarding good leads as `FETCH_FAILED`

Move `FETCH_FAILED` from `discard_on` to `flag_only` in `offer_classifier`:

```json
"offer_classifier": {
  "enabled": true,
  "discard_on": ["B2C", "LOW_TICKET", "NO_WEBSITE"],
  "flag_only": ["FETCH_FAILED", "UNCLEAR"]
}
```

This lets the lead through with an `"Offer Flag: FETCH_FAILED"` confidence note, so you can review manually.

### Tests failing

```bash
# Run just unit tests first (no API needed)
.venv/bin/python3 test_cases.py --unit-only

# Check individual utilities
.venv/bin/python3 batch_qualify.py --test-normalize
.venv/bin/python3 batch_qualify.py --test-size
.venv/bin/python3 youtube_qualifier.py --test-name-match
```

---

*For bugs or feature requests, open an issue in the project repository.*
