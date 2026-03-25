# Directive: Batch YouTube Qualifier

## Goal
Given a CSV file or Google Sheet of raw leads, run YouTube qualification on each lead and write the results to a new tab in a Google Sheet.

## Script
`youtube_qualifier/batch_qualify.py`

## Inputs
| Argument | Required | Description |
|----------|----------|-------------|
| `--input` | Yes (process mode) | CSV file path, or Google Sheets URL/ID |
| `--output-sheet` | Yes | Google Sheets ID or URL to write results to (must already exist) |
| `--no-claude` | No | Skip Anthropic API Stage 2 calls; outputs JSON instead of writing to sheet |
| `--write-results` | No (write mode) | JSON file of finalized results to write; skips processing |
| `--tab-name` | No | Custom name for the output tab (default: `YT Results YYYY-MM-DD HH:MM`) |

## CSV/Sheet Column Mapping
The script looks for these columns (case-insensitive):

| Field | Accepted Column Names |
|-------|-----------------------|
| First name | `first name`, `First Name`, `first_name`, `FirstName` |
| Last name | `last name`, `Last Name`, `last_name`, `LastName` |
| Company | `company`, `Company`, `company name`, `Company Name` |
| Website | `corporate website`, `Corporate Website`, `website`, `Website` |

If only a single `name` column exists (no first/last split), that is used directly.
Rows missing both person and company are recorded as `ERROR` and reported.

## Output Columns (Google Sheet)
`Person | Company | Website | Condition | Channel Name | Channel URL | Last Upload | Upload Count | Stage | Reasoning | Processed At | Error`

## Modes of Operation

### 1. Full end-to-end (uses Anthropic API for Stage 2)
```bash
cd youtube_qualifier
python batch_qualify.py --input leads.csv --output-sheet SHEET_ID
```

### 2. No-Claude mode (for use with the batch skill)
```bash
cd youtube_qualifier
python batch_qualify.py --input leads.csv --output-sheet SHEET_ID --no-claude
```
Outputs a JSON array to stdout. Rows needing Stage 2 judgment have `condition: "STAGE2_NEEDED"` and a `videos` array. The skill evaluates those in-session, then calls write mode.

### 3. Write mode (after skill handles Stage 2)
```bash
cd youtube_qualifier
python batch_qualify.py --write-results /tmp/finalized.json --output-sheet SHEET_ID
```

## Authentication
Requires a Google service account with Sheets + Drive access.

Set `GOOGLE_CREDENTIALS_FILE` in `.env` to the path of the service account JSON key:
```
GOOGLE_CREDENTIALS_FILE=/path/to/service-account.json
```

The target spreadsheet must be **shared with the service account email** (Editor access).

## Error Handling
- Rows missing person/company: recorded as `ERROR`, printed to stderr, processing continues
- `qualify_youtube()` exceptions: recorded as `ERROR` with error message, processing continues — never silently skipped
- YouTube API 403 (quota exceeded): recorded as `ERROR` per row; all subsequent rows will also fail — stop and retry later
- Google Sheets auth failure: script exits immediately with clear error message

## API Costs
- YouTube Data API v3: ~5–10 units per lead (free quota: 10,000/day ≈ 1,000–2,000 leads/day)
- Anthropic API (Stage 2): only charged when run without `--no-claude` and channel passes Stage 1
- With `--no-claude` + the batch skill: Stage 2 uses Claude Code session (no API cost)

## Known Constraints
- Output spreadsheet must already exist (the script adds a new tab, it does not create a new file)
- Service account must have Editor access to the output spreadsheet
- Google fallback search (Stage 4 in channel discovery) may be blocked by Google; this is non-fatal
