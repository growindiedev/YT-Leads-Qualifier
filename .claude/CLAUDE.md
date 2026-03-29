# ContentScale — Leads Qualification Pipeline

## What this project does

Qualifies raw B2B leads (CSV or Google Sheet) through a multi-stage pipeline and writes enriched results to a Google Sheet. The goal: find founders running small B2B service businesses with no YouTube presence — ContentScale's ideal clients.

**Qualification order:**
1. **Dedup** — skips leads already in the output sheet
2. **Filter pipeline** (`pipeline_filters.py`) — configurable gates, each producing a `DISCARD_*` condition:
   `DISCARD_PRESCREEN` · `DISCARD_SIZE` · `DISCARD_TITLE` · `DISCARD_LOCATION` · `DISCARD_INDUSTRY` · `DISCARD_KEYWORDS` · `DISCARD_REVENUE` · `DISCARD_TENURE` · `DISCARD_SCORE` · `DISCARD_MULTI_COMPANY` · `DISCARD_CONTACT` · `DISCARD_ACTIVITY`
3. **Multi-company scoring** — ranks active roles by title/tenure/size/niche; promotes best match as primary
4. **Website classifier** — discards B2C, low-ticket, no-website leads
5. **YouTube analysis** — per-company channel discovery + Stage 1 deterministic checks
6. **Stage 2 judgment** — human in-session via `/qualify-leads` skill

---

## Project structure

```
/
├── .env                          ← API keys (never commit)
├── leads-service-account.json    ← Google service account key (never commit)
├── pipeline_config.json          ← All filter toggles and thresholds
├── lead_utils.py                 ← Shared parse/score primitives (no I/O, no HTTP)
├── pipeline_filters.py           ← Filter functions + apply_filters + load_pipeline_config
├── batch_qualify.py              ← Batch runner, all gates, Sheets writer
├── youtube_qualifier.py          ← YouTube channel discovery + qualification
├── test_cases.py                 ← Unit + API test suite
├── README.md                     ← Full usage guide and configuration manual
├── Input_lists/                  ← Input CSVs (gitignored — never commit)
└── .claude/
    ├── CLAUDE.md                 ← This file
    └── skills/YT-Qualifier-skill/
        ├── SKILL.md              ← /qualify-leads skill definition
        └── references/           ← Condition definitions, API reference
```

---

## Running leads

Drop a CSV in `Input_lists/`, then:

```
/qualify-leads 25              # process 25 new leads
/qualify-leads                 # process all new leads
/qualify-leads 10 "Input_lists/leads.csv" "SHEET_ID"
```

Always use `--no-claude`. Stage 2 judgment happens in-session — no API credits needed.

---

## YouTube conditions

| Condition | Meaning | How decided |
|-----------|---------|-------------|
| `A` | No channel found | Stage 1 |
| `B` | Dead channel — last upload >60 days | Stage 1 |
| `C` | Inconsistent — 60+ day gap in recent uploads | Stage 1 |
| `D` | Raw podcast/webinar clips only | Stage 2 — human |
| `E` | Shorts only (all ≤60s) | Stage 1 |
| `F` | Off-topic content unrelated to their business | Stage 2 — human |
| `FAIL` | Active, polished business channel | Stage 2 — human |
| `REVIEW_FAIL` | Secondary company has active channel | Resolution rule |
| `STAGE2_NEEDED` | Needs in-session judgment | — |
| `ERROR` | YouTube API failure | — |

---

## Rules

- **Always `--no-claude`** in the pipeline. Stage 2 is done in-session.
- **Never commit** `.env`, `leads-service-account.json`, or anything in `Input_lists/`.
- **Output sheet must exist** and be shared with the service account as Editor before running.
- **`DISCARD_OFFER` on `NO_WEBSITE`** is intentional — no website = can't verify offer.
- **`FETCH_FAILED` / `UNCLEAR`** proceed to YouTube with a confidence flag — not discarded.
- **`REVIEW_FAIL`** = secondary company has an active channel. Review before dismissing.
- **Quota errors auto-resume** — dedup skips already-processed leads on re-run.

---

## Code style guide

### Module responsibilities

| Module | Owns | Must NOT |
|--------|------|----------|
| `lead_utils.py` | Parse/score primitives | I/O, HTTP, pipeline logic |
| `pipeline_filters.py` | Filter functions, config loading, `apply_filters` | Import from `batch_qualify` |
| `batch_qualify.py` | Orchestration, HTTP/Sheets I/O, website classifier | Define parse/score primitives |
| `youtube_qualifier.py` | YouTube discovery + Stage 1/2 logic | Import from `batch_qualify` |

**Import direction:** `batch_qualify` → `pipeline_filters` → `lead_utils`. No circular imports.

### Key rules

- **Single Responsibility** — every function does one thing. Filters have the signature `filter_x(profile, config) -> FilterResult`, no side effects.
- **Open/Closed** — extend the filter pipeline by appending to `FILTER_PIPELINE`. Never edit the `apply_filters` loop.
- **No magic numbers** — all thresholds belong in `pipeline_config.json`.
- **No dead code** — remove superseded functions immediately. Don't comment out code.
- **DRY** — parse/score utilities in `lead_utils.py` only. Config defaults in `pipeline_filters._DEFAULTS` only.
- **Small functions, descriptive names** — no flag arguments; split into separate functions instead.

### Adding a new filter

1. Write `def filter_my_rule(profile, config) -> FilterResult` in `pipeline_filters.py`
2. Append to `FILTER_PIPELINE`
3. Add config section to `pipeline_config.json`
4. Import any parse/score helpers from `lead_utils` — never from `batch_qualify`

### Tests

Run `test_cases.py --unit-only` before every commit (2 seconds, no API key needed).
