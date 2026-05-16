---
task: "Port lead-qualification pipeline from Python to TypeScript + bun"
project: lead-qualification
effort: E3
phase: observe
progress: 0/40
mode: interactive
started: 2026-05-16T00:00:00Z
updated: 2026-05-16T00:00:00Z
---

## Problem

The lead-qualification pipeline is implemented in Python, violating PAI's TypeScript-always rule. It cannot use PAI shared tooling, cannot be run with `bun`, and creates a split-language codebase where the skill's prompting layer is TypeScript-first but its implementation is not. The `.venv/` dependency also makes setup fragile and OS-specific.

## Vision

A TypeScript + bun implementation that runs `bun .claude/skills/lead-qualification/src/cli.ts process ...` from the project root and produces identical output to the Python pipeline. No venv, no pip, no Python. Setup is `bun install`. The skill workflows reference `bun` commands. Service account auth is pure Bun Web Crypto — no googleapis. `.env` loading is zero-config because bun auto-reads it from CWD. The pipeline is faster, type-safe, and fully PAI-native.

## Out of Scope

- Playwright JS-rendered site fallback — deferred, not in this rebuild
- Stage 2 Claude judgment — unchanged, handled by the skill in-session, not in the CLI
- Any new features or config options beyond Python parity
- Merging `dev1.1` → `main` — separate task
- Removing the Python source files — keep them until TypeScript parity is verified

## Constraints

- `bun` / `bunx` only — no npm / npx, no `node_modules` installed via npm
- TypeScript only — no Python files in the TypeScript implementation
- No `googleapis`, `google-auth-library`, `axios`, `node-fetch` — native `fetch` everywhere
- Service account JWT must use Bun Web Crypto (RS256) — no external auth library
- `cheerio` for HTML parsing (BeautifulSoup equivalent)
- Bun auto-loads `.env` from CWD — no `dotenv` library needed
- `import.meta.dir` for all file paths relative to the script — no hardcoded paths
- All files live in `src/` inside the skill: `.claude/skills/lead-qualification/src/`
- `cli.ts` lives in `src/` alongside the modules
- `pipeline_config.json` and `session_counter.json` stay in `src/`

## Goal

Port all pipeline logic to TypeScript + bun with full output parity against the Python implementation. `bun test` passes a test suite equivalent to Python's 64 tests. A side-by-side run on 3 real leads produces matching `ytCondition`, `offerClassification`, and `primaryScore` fields. Skill workflows updated to call `bun` instead of `.venv/bin/python3`.

## Criteria

### Phase 1 — Primitives

- [ ] ISC-1: `src/types.ts` exists and exports `Profile`, `CompanyRecord`, `OfferClassification`, `YouTubeCondition`, `DiscardCondition`, `FilterResult`, `YouTubeResult`, `VideoRecord`, `SocialProofResult`, `PipelineConfig` — all matching the type spec in the rebuild plan
- [ ] ISC-2: `src/config.ts` exports `loadConfig()` that reads `pipeline_config.json` via `import.meta.dir` and merges with typed defaults
- [ ] ISC-3: `src/lead-utils.ts` exports `parseCompanySize`, `parseTenureMonths`, `parseMismatchedFilters`, `parseRevenueRange`, `scoreJobTitle`, `scoreCompanySize`, `scoreNicheFit` — pure functions, no I/O
- [ ] ISC-4: `bun test src/tests/lead-utils.test.ts` passes — 22 tests covering all parse/score cases
- [ ] ISC-5: `parseCompanySize("11-50")` = 50, `("myself only")` = 1, `("10,001+")` = 10001, `("")` = null
- [ ] ISC-6: `parseCompanySize` and `scoreJobTitle` match Python output for all 12 unit test cases verbatim

### Phase 2 — Filters

- [ ] ISC-7: `src/filters.ts` exports `applyFilters`, `remapRow`, and all 12 named filter functions
- [ ] ISC-8: `FILTER_PIPELINE` array contains all 12 filters in the same order as Python's `FILTER_PIPELINE`
- [ ] ISC-9: Each filter function is pure — `filterX(profile: Profile, config: PipelineConfig): FilterResult` — no side effects
- [ ] ISC-10: `applyFilters` returns the first discard encountered and stops — does not run remaining filters
- [ ] ISC-11: `bun test src/tests/filters.test.ts` passes — 10 tests covering filter pipeline cases

### Phase 3 — Website Classifier

- [ ] ISC-12: `src/website-classifier.ts` exports `classifyWebsiteOffer(url, config)` returning `{ classification, reason, combinedText }`
- [ ] ISC-13: Returns `NO_WEBSITE` when url is null, `FETCH_FAILED` on network error
- [ ] ISC-14: `detectSocialProof(text)` returns `SocialProofResult` with all five fields populated
- [ ] ISC-15: `HIGH_TICKET_B2B_SIGNALS`, `B2C_SIGNALS`, and `STRONG_B2B_SIGNALS` are typed `readonly string[]` constants — not magic strings inside a function

### Phase 4 — Sheets

- [ ] ISC-16: `src/sheets.ts` exports `readCsv`, `readSheet`, `getAlreadyQualified`, `isDuplicate`, `deduKey`, `writeToSheet`, `writeSessionSummary`, `generateSessionId`
- [ ] ISC-17: `getServiceAccountToken` uses Bun Web Crypto (`crypto.subtle.importKey`, `crypto.subtle.sign`) for RS256 JWT — no googleapis import
- [ ] ISC-18: `getAlreadyQualified` returns `Set<string>` built from all `Leads *` tabs in the output sheet
- [ ] ISC-19: `LEADS_HEADERS` constant lists exactly 18 columns in the same order as Python's `LEADS_HEADERS`
- [ ] ISC-20: `generateSessionId()` reads `session_counter.json` via `import.meta.dir` and returns `YYYY-MM-DD-NNN` format

### Phase 5 — YouTube

- [ ] ISC-21: `src/youtube.ts` exports `qualifyYoutube(personName, companyName, websiteUrl, activeCompanies, config): Promise<YouTubeResult>`
- [ ] ISC-22: `scrapeWebsiteForChannel` finds `youtube.com/` URLs in fetched page HTML using cheerio
- [ ] ISC-23: `runStage1` returns `A` (no channel), `B` (last upload >60d), `C` (60d gap between recent uploads), `E` (all videos ≤60s) — null if none match
- [ ] ISC-24: `nameMatch` validates channel against person name and company name — same logic as Python
- [ ] ISC-25: `ytApi<T>` is a typed fetch wrapper — uses `Bun.env.YOUTUBE_API_KEY` directly, no dotenv call
- [ ] ISC-26: `bun test src/tests/pipeline.test.ts` passes — 42 tests covering channel discovery and Stage 1 conditions

### Phase 6 — Pipeline + CLI

- [ ] ISC-27: `src/pipeline.ts` exports `processLeads(rows, config, options)` orchestrating all gates in the correct order
- [ ] ISC-28: `src/cli.ts` supports process mode: `bun cli.ts process --input X --output-sheet Y --no-claude [--limit N]`
- [ ] ISC-29: `src/cli.ts` supports write mode: `bun cli.ts write --results /tmp/results.json --output-sheet Y [--tab-name "May 2026"]`
- [ ] ISC-30: Process mode writes STAGE2_NEEDED rows as JSON to stdout (pipe-friendly)
- [ ] ISC-31: Exit codes: 0 = success, 1 = bad args, 2 = API/auth error
- [ ] ISC-32: `src/package.json` exists with `"type": "module"` and bun scripts: `test`, `process`, `write`

### Phase 7 — Skill Update + Parity

- [ ] ISC-33: `Workflows/Qualify.md` Step 1 command updated to `bun .claude/skills/lead-qualification/src/cli.ts process ...`
- [ ] ISC-34: `Workflows/Qualify.md` Step 5 command updated to `bun .claude/skills/lead-qualification/src/cli.ts write ...`
- [ ] ISC-35: `Workflows/CheckQuota.md` updated to a TypeScript quota check (`bun`) — no Python/venv reference
- [ ] ISC-36: Root `CLAUDE.md` commands updated to `bun` equivalents
- [ ] ISC-37: `bun test` (full suite) passes — 74+ tests (22 + 10 + 42 + any new integration tests)
- [ ] ISC-38: Side-by-side parity check — same 3-lead CSV input through Python and TypeScript produces matching `ytCondition`, `offerClassification`, and `primaryScore` for all 3 leads

### Anti-criteria

- [ ] ISC-39: Anti: No import of `googleapis`, `google-auth-library`, `axios`, `node-fetch` in any `src/*.ts` file
- [ ] ISC-40: Anti: No `import { config } from 'dotenv'` or `load_dotenv` call in any TypeScript file — Bun reads `.env` from CWD automatically

## Test Strategy

| ISC | Type | Check | Threshold | Tool |
|-----|------|-------|-----------|------|
| ISC-1..3 | Read | File exists, exports listed symbols | All symbols present | Read + Grep |
| ISC-4..6 | Bash | `bun test src/tests/lead-utils.test.ts` | 22 pass, 0 fail | Bash |
| ISC-7..10 | Read | FILTER_PIPELINE array, function signatures | 12 filters, correct order | Read |
| ISC-11 | Bash | `bun test src/tests/filters.test.ts` | 10 pass, 0 fail | Bash |
| ISC-12..15 | Read + Bash | File exists, manual test on 3 known URLs | Classifications correct | Bash |
| ISC-16..20 | Read | File exists, export list, header constant | 18 columns, YYYY-MM-DD-NNN | Read |
| ISC-17 | Read | No googleapis import, `crypto.subtle` present | 0 googleapis imports | Grep |
| ISC-21..25 | Read | File exists, function signatures | All exports present | Read |
| ISC-26 | Bash | `bun test src/tests/pipeline.test.ts` | 42 pass, 0 fail | Bash |
| ISC-27..31 | Bash | `bun src/cli.ts process` dry run | Exit 0, JSON to stdout | Bash |
| ISC-32 | Read | `package.json` has bun scripts | `test`, `process`, `write` present | Read |
| ISC-33..36 | Read | Workflow files and CLAUDE.md | `bun` commands present, no `.venv` | Grep |
| ISC-37 | Bash | `bun test` full suite | 74+ pass, 0 fail | Bash |
| ISC-38 | Bash | Python vs TypeScript 3-lead diff | 3/3 key fields match | Bash |
| ISC-39..40 | Bash | `grep -r "googleapis\|dotenv" src/` | 0 matches | Bash |

## Features

| Name | Description | Satisfies | Depends On | Parallelizable |
|------|-------------|-----------|------------|----------------|
| Phase 1 — Primitives | `types.ts`, `config.ts`, `lead-utils.ts`, `tests/lead-utils.test.ts` | ISC-1..6 | — | no |
| Phase 2 — Filters | `filters.ts`, `tests/filters.test.ts` | ISC-7..11 | Phase 1 | no |
| Phase 3 — Website | `website-classifier.ts` | ISC-12..15 | Phase 1 | yes |
| Phase 4 — Sheets | `sheets.ts` | ISC-16..20 | Phase 1 | yes |
| Phase 5 — YouTube | `youtube.ts`, `tests/pipeline.test.ts` | ISC-21..26 | Phase 1, Phase 2 | no |
| Phase 6 — Pipeline + CLI | `pipeline.ts`, `cli.ts`, `package.json` | ISC-27..32 | All above | no |
| Phase 7 — Skill + Parity | Update workflows, CLAUDE.md, verify parity | ISC-33..40 | Phase 6 | no |

## Decisions

- **2026-05-16** — `src/` lives in `.claude/skills/lead-qualification/src/`, not at repo root. The rebuild plan (`Plans/i-want-to-rebuild-golden-lollipop.md`) shows `src/` at repo root — that was written before the PAI skill reorganization. This ISA is authoritative; the plan file is a reference for module specs only.
- **2026-05-16** — No dotenv library in TypeScript. Bun auto-reads `.env` from the CWD when the script is invoked from the project root. All env vars accessed via `Bun.env.VARIABLE_NAME` directly. This is simpler and more PAI-native than the Python `find_dotenv()` workaround.
- **2026-05-16** — `cli.ts` lives in `src/` (not at skill root). All source in one directory keeps `package.json`, tests, and modules together. Invocation: `bun .claude/skills/lead-qualification/src/cli.ts process ...` from project root.
- **2026-05-16** — Python source files (`batch_qualify.py` etc.) stay in `src/` until TypeScript parity is confirmed via ISC-38. Delete Python files as a separate task after parity is verified.

---

*Detailed module specs (type interfaces, function signatures, signal arrays): see `Plans/i-want-to-rebuild-golden-lollipop.md`.*
