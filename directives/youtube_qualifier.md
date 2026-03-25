# Directive: YouTube Qualifier

## Goal
Given a person's name and company name, determine if they have a YouTube channel and how qualified it is as a lead (A/B/C/D/E/FAIL).

## Script
`youtube_qualifier/youtube_qualifier.py`

## Inputs
- `person_name` (str) — full name of the person
- `company_name` (str) — their company name
- `website_url` (str, optional) — their website URL; improves channel discovery reliability

## Outputs
```python
{
    "condition": "A",        # A / B / C / D / E / FAIL / ERROR
    "channel_url": str,      # Full YouTube channel URL or None
    "channel_name": str,     # Channel display name or None
    "last_upload_date": str, # ISO date string or None
    "upload_count": int,     # Total videos on channel
    "reasoning": str,        # One-sentence explanation
    "stage": int             # 1 = API logic, 2 = Claude judgment
}
```

## Condition Reference
| Condition | Meaning | Lead Quality |
|-----------|---------|--------------|
| A | No channel found | No YouTube presence |
| B | Dead channel (no upload in 60+ days) | Inactive |
| C | Inconsistent poster (60+ day gap between recent uploads) | Unreliable |
| D | Active but low production quality (raw podcast/interview clips) | Weak content — good lead |
| E | Shorts only | No long-form content |
| FAIL | Active, polished, high-production channel | Already strong — bad lead |
| ERROR | API quota exceeded or network failure | Retry later |

## Discovery Priority
1. YouTube Search API by person name
2. YouTube Search API by company name
3. Website scrape for YouTube link (most reliable when available)
4. Google fallback search

## API Costs
- YouTube Data API v3: ~5–10 units per call (free quota: 10,000/day)
- Anthropic (Haiku): Stage 2 only — only called for active channels
- Stage 2 is skipped for conditions B, C, E — no Claude cost for those

## Known Constraints
- Google fallback (Search 4) may return 429 or empty results if Google blocks the scrape
- YouTube API `forHandle` endpoint resolves `@username` handles — use before search fallback
- `UC...` channel IDs do not need resolution — pass directly
- Videos list is sorted newest-first by the uploads playlist

## Error Handling
- HTTP 403 from YouTube → return ERROR condition immediately (quota exceeded)
- Network timeout → retry once after 3s, then return ERROR
- Claude API failure → default to Condition D (safe pass), never crash pipeline
- Claude JSON parse error → default to Condition D

## Running Standalone
```bash
cd youtube_qualifier
python youtube_qualifier.py "John Smith" "Acme Consulting" "https://acmeconsulting.com"
```

## Running Tests
```bash
cd youtube_qualifier
python test_cases.py
```
