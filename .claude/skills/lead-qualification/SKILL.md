---
name: lead-qualification
description: Qualify B2B leads against ContentScale's ICP. Checks website offer + YouTube presence. Trigger when qualifying leads, running the pipeline, checking YouTube quota, or processing a CSV/Sheet of prospects.
---

# Lead Qualification Skill

Qualifies raw B2B leads (CSV or Google Sheet) for ContentScale outreach. Runs a 6-gate pipeline: dedup, filters, multi-company scoring, website classifier, YouTube Stage 1 (deterministic), then surfaces Stage 2 rows for human judgment in-session.

Read `References/conditions.md` before making any YouTube qualification judgments.
Read `References/pipeline.md` for full architecture and data flow.
Read `References/youtube-api.md` only when debugging API issues.

---

## Workflow Routing

| Trigger | Workflow | When to use |
|---------|----------|-------------|
| `/qualify-leads` | `Workflows/Qualify.md` | Process a batch of leads — runs pipeline, handles Stage 2, writes to Sheets |
| `/check-quota` | `Workflows/CheckQuota.md` | Check YouTube API quota status before starting a run |

---

## References

| File | Contents |
|------|----------|
| `References/conditions.md` | Full A/B/C/D/E/F/FAIL/REVIEW_FAIL definitions + decision tree + multi-company resolution rules |
| `References/youtube-api.md` | YouTube Data API v3 endpoints, quota costs, duration parsing, error handling |
| `References/pipeline.md` | 6-gate pipeline architecture, two-mode design, data flow, module responsibilities |
