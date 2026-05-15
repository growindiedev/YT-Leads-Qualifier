# Pipeline Architecture Reference

Complete reference for the ContentScale lead qualification pipeline.

---

## What the Pipeline Does

Takes a raw Sales Navigator CSV export and answers one question per lead:
**Should Abhishek reach out to this person to pitch ContentScale's YouTube services?**

The ICP (Ideal Customer Profile): a founder or senior executive at a small B2B service business (1–100 people) with a high-ticket offer ($25k+/month signals) and little or no YouTube presence.

---

## Two-Mode Design

`batch_qualify.py` runs in two mutually exclusive modes:

- **Process mode** (`--input ... --no-claude`): reads CSV/Sheet, runs all gates through YouTube Stage 1, outputs JSON to stdout with `STAGE2_NEEDED` rows for human judgment. The `/qualify-leads` skill calls this first, then presents Stage 2 rows in-session.
- **Write mode** (`--write-results results.json`): accepts a JSON file of already-judged rows and writes them directly to the output sheet. The skill calls this after in-session judgment is complete.

This split means the pipeline never needs the Anthropic API at runtime — Stage 2 judgment is free, done inside Claude Code.

---

## 6-Gate Pipeline

| Gate | Question | On fail |
|------|----------|---------|
| **Dedup** | Already processed this person? | Skip |
| **Prescreen** | Did Sales Nav itself flag them as a mismatch? | DISCARD_PRESCREEN |
| **Filter pipeline** | Passes all configurable gates (size, title, location, industry, keywords, revenue, tenure, score, multi-company, contact, activity)? | DISCARD_* |
| **Website classifier** | Does the website show a high-ticket B2B offer? | DISCARD_OFFER |
| **YouTube Stage 1** | Is their channel dead / inconsistent / Shorts-only? | PASS (conditions A/B/C/E → good leads) |
| **YouTube Stage 2** | Human reviews ambiguous channels | PASS (D/F) or DISCARD (FAIL) |

---

## Data Flow

```
CSV / Google Sheet
  ↓  remap_row()             — normalises column names via pipeline_config.json input.column_map
  ↓  dedup check             — reads all Leads tabs from output sheet; skips already-processed names
  ↓  apply_filters()         — runs FILTER_PIPELINE in order; first discard wins
  ↓  rank_active_companies() — scores multi-role leads; selects primary company
  ↓  classify_offer()        — fetches website; DDG fallback for UNCLEAR/FETCH_FAILED
  ↓  qualify_youtube()       — channel discovery (website scrape → DDG → API search); Stage 1 checks
  ↓  stdout JSON             — STAGE2_NEEDED rows surface to the skill for human judgment
  ↓  write to Sheets         — Leads / Discards / Errors / Sessions tabs
```

---

## Stage 1 vs Stage 2

**Stage 1 (deterministic, in `youtube_qualifier.py`):**
- `A` — no channel found
- `B` — last upload >60 days
- `C` — 60+ day gap between recent uploads
- `E` — all videos ≤60s (Shorts only)

**Stage 2 (human judgment, surfaced by skill):**
- `D` — raw podcast/webinar clips only
- `F` — off-topic content
- `FAIL` — active, polished business channel

---

## YouTube Channel Discovery Order

1. Scrape the company website for YouTube links
2. DuckDuckGo search (`ddg_search.channel_discovery: true`)
3. YouTube API `search.list` (~100 quota units) — only if previous steps failed

`person_name_search: false` in config disables person-name search to prevent false matches on common names.

DDG-first discovery cuts quota from ~300–400 units/lead to ~7 units/lead (1,400 leads/day vs 25–33).

---

## Multi-Company Leads

When a lead has multiple active roles (no `job ended on`), all companies are scored, the highest scorer becomes primary, and YouTube discovery runs on every active company. If a secondary company has an active channel, the lead gets `REVIEW_FAIL` instead of `FAIL`.

---

## Module Responsibilities

| Module | Owns | Must NOT |
|--------|------|----------|
| `lead_utils.py` | Parse/score primitives | I/O, HTTP, pipeline logic |
| `pipeline_filters.py` | Filter functions, config loading, `apply_filters` | Import from `batch_qualify` |
| `batch_qualify.py` | Orchestration, HTTP/Sheets I/O, website classifier | Define parse/score primitives |
| `youtube_qualifier.py` | YouTube discovery + Stage 1/2 logic | Import from `batch_qualify` |

**Import direction:** `batch_qualify` → `pipeline_filters` → `lead_utils`. No circular imports.

---

## Output Tabs (Google Sheets)

| Tab | Contents |
|-----|----------|
| `Leads *` | Qualified prospects with Why Chosen + Confidence |
| `Discards *` | Every rejected lead with gate + reason |
| `Sessions` | One row per pipeline run — counts and quota used |

---

## Key Rules

- **Always `--no-claude`** when running the pipeline. Stage 2 is in-session.
- **`FETCH_FAILED` / `UNCLEAR`** proceed to YouTube with a confidence flag — they are not discarded.
- **`REVIEW_FAIL`** = secondary company has an active channel. Review before dismissing.
- **Quota errors auto-resume** — dedup skips already-processed leads on re-run.
- **`DISCARD_OFFER` on `NO_WEBSITE`** is intentional — no website means offer can't be verified.

---

## Environment Variables

```dotenv
YOUTUBE_API_KEY=
GOOGLE_CREDENTIALS_FILE=leads-service-account.json
OUTPUT_SHEET_ID=
INPUT_SHEET_ID=          # optional
ANTHROPIC_API_KEY=       # optional — unused in normal --no-claude flow
TARGET_NICHE=            # overridden by pipeline_config.json icp.target_niche when set
```

**Never commit** `.env`, `leads-service-account.json`, or anything in `Input_lists/`.
