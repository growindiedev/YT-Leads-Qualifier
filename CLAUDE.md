# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Skills

| Skill | Trigger | Purpose |
|-------|---------|---------|
| `lead-qualification` | `/qualify-leads` | Run pipeline, Stage 2 judgment, write to Sheets |
| `lead-qualification` | `/check-quota` | Check YouTube API quota status |

Skill files: `.claude/skills/lead-qualification/`

---

## What this project does

Qualifies raw B2B leads (CSV or Google Sheet) through a multi-stage pipeline and writes enriched results to a Google Sheet. The goal: find founders running small B2B service businesses with no YouTube presence — ContentScale's ideal clients.

**Qualification order:**
1. **Dedup** — skips leads already in the output sheet (reads all existing `Leads *` tabs)
2. **Filter pipeline** (`pipeline_filters.py`) — configurable gates, each producing a `DISCARD_*` condition
3. **Multi-company scoring** — ranks active roles by title/tenure/size/niche; promotes best match as primary
4. **Website classifier** — discards B2C, low-ticket, no-website leads; uses DuckDuckGo fallback for `UNCLEAR`
5. **YouTube analysis** — per-company channel discovery + Stage 1 deterministic checks
6. **Stage 2 judgment** — human in-session via `/qualify-leads` skill

---

## Commands

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r .claude/skills/lead-qualification/src/requirements.txt

# Tests (run before every commit — 2 seconds, no API keys needed)
.venv/bin/python3 .claude/skills/lead-qualification/src/test_cases.py --unit-only

# Full test suite (requires YOUTUBE_API_KEY, ~1,300 quota units)
.venv/bin/python3 .claude/skills/lead-qualification/src/test_cases.py

# Targeted debug utilities
.venv/bin/python3 .claude/skills/lead-qualification/src/batch_qualify.py --test-normalize
.venv/bin/python3 .claude/skills/lead-qualification/src/batch_qualify.py --test-size
.venv/bin/python3 .claude/skills/lead-qualification/src/youtube_qualifier.py --test-name-match

# Direct pipeline run (used by the skill internally)
.venv/bin/python3 .claude/skills/lead-qualification/src/batch_qualify.py \
  --input Input_lists/leads.csv \
  --output-sheet SHEET_ID \
  --no-claude \
  --limit 25

# Write pre-judged Stage 2 results to the sheet
.venv/bin/python3 .claude/skills/lead-qualification/src/batch_qualify.py \
  --write-results /tmp/results.json \
  --output-sheet SHEET_ID \
  --tab-name "May 2026"
```

---

## Architecture

### Two-mode design

`batch_qualify.py` runs in two mutually exclusive modes:

- **Process mode** (`--input ... --no-claude`): reads CSV/Sheet, runs all gates through YouTube Stage 1, outputs JSON to stdout with `STAGE2_NEEDED` rows for human judgment. The `/qualify-leads` skill calls this first, then presents Stage 2 rows in-session.
- **Write mode** (`--write-results results.json`): accepts a JSON file of already-judged rows and writes them directly to the output sheet. The skill calls this after in-session judgment is complete.

This split means the pipeline never needs the Anthropic API at runtime — Stage 2 judgment is free, done inside Claude Code.

### Data flow

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

### Stage 1 vs Stage 2

Stage 1 (deterministic, in `youtube_qualifier.py`):
- `A` — no channel found
- `B` — last upload >60 days
- `C` — 60+ day gap between recent uploads
- `E` — all videos ≤60s (Shorts only)

Stage 2 (human judgment, surfaced by skill):
- `D` — raw podcast/webinar clips only
- `F` — off-topic content
- `FAIL` — active, polished business channel

### YouTube channel discovery order
1. Scrape the company website for YouTube links
2. DuckDuckGo search (`ddg_search.channel_discovery: true`)
3. YouTube API `search.list` (~100 quota units) — only if previous steps failed

`person_name_search: false` in config disables Steps 3–4 (search by person name) to prevent false matches on common names.

### Multi-company leads

When a lead has multiple active roles (no `job ended on`), all companies are scored, the highest scorer becomes primary, and YouTube discovery runs on every active company. If a secondary company has an active channel, the lead gets `REVIEW_FAIL` instead of `FAIL`.

---

## Module responsibilities

| Module | Owns | Must NOT |
|--------|------|----------|
| `lead_utils.py` | Parse/score primitives | I/O, HTTP, pipeline logic |
| `pipeline_filters.py` | Filter functions, config loading, `apply_filters` | Import from `batch_qualify` |
| `batch_qualify.py` | Orchestration, HTTP/Sheets I/O, website classifier | Define parse/score primitives |
| `youtube_qualifier.py` | YouTube discovery + Stage 1/2 logic | Import from `batch_qualify` |

**Import direction:** `batch_qualify` → `pipeline_filters` → `lead_utils`. No circular imports.

---

## Code style

- **Filter signature:** `filter_x(profile: dict, config: dict) -> FilterResult` — no side effects
- **Open/Closed:** extend the pipeline by appending to `FILTER_PIPELINE`. Never edit the `apply_filters` loop.
- **Thresholds** belong in `pipeline_config.json`. No magic numbers in code.
- **Config defaults** live only in `pipeline_filters._DEFAULTS`.
- **Parse/score utilities** live only in `lead_utils.py`.

### Adding a new filter

1. Write `def filter_my_rule(profile, config) -> FilterResult` in `pipeline_filters.py`
2. Append to `FILTER_PIPELINE`
3. Add config section (with `"enabled": false`) to `pipeline_config.json`
4. Import parse/score helpers from `lead_utils` — never from `batch_qualify`

---

## Key rules

- **Always `--no-claude`** when running the pipeline. Stage 2 is in-session.
- **`FETCH_FAILED` / `UNCLEAR`** proceed to YouTube with a confidence flag — they are not discarded.
- **`REVIEW_FAIL`** = secondary company has an active channel. Review before dismissing.
- **Quota errors auto-resume** — dedup skips already-processed leads on re-run.
- **`DISCARD_OFFER` on `NO_WEBSITE`** is intentional — no website means offer can't be verified.

---

## YouTube conditions

| Condition | Meaning | How decided |
|-----------|---------|-------------|
| `A` | No channel found | Stage 1 |
| `B` | Dead channel — last upload >60 days | Stage 1 |
| `C` | Inconsistent — 60+ day gap in recent uploads | Stage 1 |
| `D` | Raw podcast/webinar clips only | Stage 2 — human |
| `E` | Shorts only (all ≤60s) | Stage 1 |
| `F` | Off-topic content | Stage 2 — human |
| `FAIL` | Active, polished business channel | Stage 2 — human |
| `REVIEW_FAIL` | Secondary company has active channel | Resolution rule |
| `STAGE2_NEEDED` | Needs in-session judgment (`--no-claude` mode) | — |
| `ERROR` | YouTube API failure | — |

---

## Environment variables (`.env`)

```dotenv
YOUTUBE_API_KEY=
GOOGLE_CREDENTIALS_FILE=leads-service-account.json
OUTPUT_SHEET_ID=
INPUT_SHEET_ID=          # optional
ANTHROPIC_API_KEY=       # optional — unused in normal --no-claude flow
TARGET_NICHE=            # overridden by pipeline_config.json icp.target_niche when set
```

**Never commit** `.env`, `leads-service-account.json`, or anything in `Input_lists/`.
