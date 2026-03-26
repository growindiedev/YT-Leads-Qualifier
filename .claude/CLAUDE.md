# ContentScale — Leads Qualification

## What this project does
Takes a CSV or Google Sheet of raw B2B leads, qualifies each one against ContentScale's ICP, and outputs enriched results to a Google Sheet.

Qualification runs in two phases:
1. **Website pre-check** — navigates the company website to confirm a high-ticket B2B offer exists before spending any YouTube API quota. Low-ticket or B2C leads are discarded immediately.
2. **YouTube analysis** — checks the founder/company YouTube presence against conditions A–E and FAIL.

## Project structure
```
/
├── .env                         ← API keys (never commit)
├── leads-service-account.json   ← Google service account (never commit)
├── youtube_qualifier.py         ← YouTube channel discovery + Stage 1 logic
├── batch_qualify.py             ← Batch runner, dedup, Sheets writer
├── requirements.txt
├── .venv/                       ← Python virtualenv
└── .claude/skills/YT-Qualifier-skill/
    ├── SKILL.md                 ← /qualify-leads skill
    └── references/              ← condition definitions, API reference
```

## How to run leads
Drop a CSV in the project root and use:
```
/qualify-leads 25              # process 25 new leads using .env defaults
/qualify-leads                 # process all new leads using .env defaults
/qualify-leads 10 "leads.csv" "SHEET_ID"   # explicit input/output
```

## Scripts
- `youtube_qualifier.py` — pure YouTube API logic. Call `qualify_youtube(person, company, website)`.
- `batch_qualify.py` — reads input, deduplicates against output sheet, calls qualifier, writes results.

## Environment variables (.env)
```
YOUTUBE_API_KEY=
GOOGLE_CREDENTIALS_FILE=
ANTHROPIC_API_KEY=        # optional, not used in normal flow
OUTPUT_SHEET_ID=          # default output Google Sheet (used by skill if no arg supplied)
INPUT_SHEET_ID=           # default input Google Sheet (optional; falls back to CSV in root)
```

## Rules
- Always run with `--no-claude`. Stage 2 judgment happens in-session (no API cost).
- Never commit `.env` or `leads-service-account.json`.
- Output sheet must already exist and be shared with the service account email.
- YouTube quota is 10,000 units/day (~50–100 leads). Quota errors auto-resume next day via dedup.
