# Leads Qualification Pipeline — Rebuild Spec

**Repo:** `Leads_Qualification/`
**Language:** TypeScript + bun (replaces Python + venv)
**Why:** Python violates PAI's TypeScript-always rule and can't leverage PAI shared tooling. Full port, no new features.

---

## Part 1 — What This System Is (Natural Language)

### The One Question It Answers

*Should Abhishek reach out to this person to pitch ContentScale's YouTube services?*

### The ICP in Plain English

ContentScale's ideal client is a founder or senior executive who:
- Runs a small B2B service business (consulting, coaching, agency, fractional) with 1–100 people
- Has a website showing a high-ticket offer (book-a-call, retainer, done-for-you, etc.)
- Has little or no YouTube presence — meaning they're missing an inbound channel ContentScale can build for them
- Makes $25k+/month (evidenced by company size, tenure, website signals)

If they already have an active, polished YouTube channel focused on their business → they don't need ContentScale → DISCARD.

### What the Pipeline Does

Takes a raw Sales Navigator CSV export and runs each lead through 6 automated gates:

| Gate | Question | On fail |
|------|----------|---------|
| **Dedup** | Already processed this person? | Skip |
| **Prescreen** | Did Sales Nav itself flag them as a mismatch? | DISCARD_PRESCREEN |
| **Size** | Company has 1–100 employees? | DISCARD_SIZE |
| **Website** | Does the website show a high-ticket B2B offer? | DISCARD_OFFER |
| **YouTube Stage 1** | Is their channel dead / inconsistent / Shorts-only? | PASS (they're a fit) |
| **YouTube Stage 2** | Human reviews ambiguous channels | PASS or DISCARD |

### Outputs

Three tabs in a Google Sheet:
1. **Leads** — qualified prospects, with "Why Chosen" + confidence (populated by skill's Stage 2 judgment)
2. **Discards** — every rejected lead with the specific gate + reason
3. **Sessions** — one row per pipeline run summarizing counts and quota used

### Two Runtime Modes

- **Process mode** (`cli.ts process`) — reads input, runs all gates through YouTube Stage 1, outputs JSON to stdout with STAGE2_NEEDED rows for the skill to surface in-session
- **Write mode** (`cli.ts write`) — reads a JSON file of already-judged rows and writes them to the output sheet

Claude is NEVER called during the pipeline run. Stage 2 judgment happens inside the `/qualify-leads` skill, not inside the CLI tool.

---

## Part 2 — Technical Spec

### Repo Structure

```
Leads_Qualification/
├── src/
│   ├── types.ts               ← All TypeScript interfaces
│   ├── config.ts              ← Load pipeline_config.json + typed defaults
│   ├── lead-utils.ts          ← Pure parse/score primitives (no I/O)
│   ├── filters.ts             ← 12-filter pipeline
│   ├── website-classifier.ts  ← Offer classification (fetch + DDG + Clutch)
│   ├── sheets.ts              ← Google Sheets I/O (service account auth)
│   ├── youtube.ts             ← Channel discovery (4 stages) + Stage 1
│   └── pipeline.ts            ← Orchestrator tying all modules together
├── cli.ts                     ← CLI entry point (process | write modes)
├── tests/
│   ├── lead-utils.test.ts
│   ├── filters.test.ts
│   └── pipeline.test.ts
├── pipeline_config.json       ← Unchanged
├── .env                       ← Unchanged
├── package.json               ← bun project
└── CLAUDE.md                  ← Updated to TypeScript commands
```

---

### Module Specs

#### `src/types.ts` — All TypeScript Interfaces

```typescript
// Profile — the normalized lead object passed through the pipeline
interface Profile {
  // Person
  fullName: string;
  personalLinkedInUrl: string;
  email: string;
  location: string;

  // Primary company
  jobTitle: string;
  company: string;
  companySize: string;          // raw string e.g. "11-50"
  companyLinkedInUrl: string;
  website: string | null;
  companyRevenue: string;

  // Multi-company
  multiCompanyFlag: boolean;
  activeCompanies: CompanyRecord[];  // sorted by score descending
  allCompanies: string[];
  pastCompanies: string[];

  // Text fields (for Stage 2 / Why Chosen)
  summary: string;
  headline: string;
  companyDescription: string;
  specialities: string;
  industry: string;

  // Sales Nav signals
  mismatchedFilters: string;
  matchingFilters: string;
  lastActivity: string;

  // Scores (set by pipeline)
  primaryScore: number;
  primaryScoreDetail: Record<string, number>;

  // Offer classification (set by website classifier)
  offerClassification: OfferClassification | null;
  offerReason: string;
  socialProof: SocialProofResult | null;
  clutchRevenueRange: string;
  clutchMinProject: string;

  // YouTube result (set by youtube module)
  ytCondition: YouTubeCondition | null;
  ytChannelUrl: string | null;
  ytChannelName: string | null;
  ytLastUpload: string;
  ytResolutionRule: string;
  ytSecondaryChannels: string;
  videos: VideoRecord[];        // only populated for STAGE2_NEEDED

  // Revenue confidence (set by pipeline)
  revenueConfidence: 'High' | 'Medium' | 'Low' | 'Unknown' | null;
  revenueScore: number;
}

interface CompanyRecord {
  company: string;
  jobTitle: string;
  website: string | null;
  companyLinkedInUrl: string;
  companyDescription: string;
  specialities: string;
  industry: string;
  companySize: string;
  companyRevenue: string;
  jobStartedOn: string;
  jobEndedOn: string;
  score: number;
  scoreDetail: Record<string, number>;
}

type OfferClassification = 'HIGH_TICKET_B2B' | 'B2C' | 'LOW_TICKET' | 'UNCLEAR' | 'NO_WEBSITE' | 'FETCH_FAILED';

type YouTubeCondition = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'FAIL' | 'REVIEW_FAIL' | 'STAGE2_NEEDED' | 'ERROR';

type DiscardCondition =
  | 'DISCARD_PRESCREEN' | 'DISCARD_SIZE' | 'DISCARD_TITLE' | 'DISCARD_LOCATION'
  | 'DISCARD_INDUSTRY' | 'DISCARD_KEYWORDS' | 'DISCARD_REVENUE' | 'DISCARD_TENURE'
  | 'DISCARD_SCORE' | 'DISCARD_MULTI_COMPANY' | 'DISCARD_CONTACT' | 'DISCARD_ACTIVITY'
  | 'DISCARD_OFFER' | 'SKIP_DEDUP' | 'SKIP_NO_EMAIL';

interface FilterResult {
  discard: boolean;
  condition: DiscardCondition | null;
  reason: string;
}

interface YouTubeResult {
  condition: YouTubeCondition;
  channelUrl: string | null;
  channelName: string | null;
  lastUploadDate: string;
  uploadCount: number;
  reasoning: string;
  resolutionRule: string;
  secondaryChannels: string;
  allCompanyResults: CompanyYouTubeResult[];
  videos: VideoRecord[];
}

interface VideoRecord {
  title: string;
  description: string;
  publishedAt: string;       // ISO date
  durationSeconds: number;
  videoUrl: string;
}

interface SocialProofResult {
  hasCaseStudies: boolean;
  hasTestimonials: boolean;
  hasRoiLanguage: boolean;
  hasDollarAmounts: boolean;
  socialProofScore: number;  // 0–8
}

interface PipelineConfig {
  batch: { defaultLimit: number };
  websiteClassifier: { fetchTimeout: number; pageCharLimit: number; minBodyChars: number };
  input: { columnMap: Record<string, string>; multiCompanySuffixes: string[]; sourceType: string };
  sizeGate: { enabled: boolean; minEmployees: number; maxEmployees: number };
  prescreen: { enabled: boolean; rules: Record<string, boolean> };
  titleFilter: { enabled: boolean; requireAny: string[]; excludeAny: string[] };
  locationFilter: { enabled: boolean; mode: 'include' | 'exclude'; values: string[] };
  industryFilter: { enabled: boolean; mode: 'include' | 'exclude'; values: string[] };
  keywordFilter: { enabled: boolean; fields: string[]; requireAny: string[]; excludeAny: string[] };
  revenueFilter: { enabled: boolean; minUsd: number | null; maxUsd: number | null };
  tenureFilter: { enabled: boolean; minMonthsAtPrimary: number };
  primaryScoreFilter: { enabled: boolean; minScore: number; minScoreMargin: number };
  multiCompanyFilter: { enabled: boolean; maxActiveRoles: number | null };
  contactFilter: { requireEmail: boolean; requireLinkedin: boolean };
  activityFilter: { enabled: boolean; maxDaysSinceActivity: number };
  offerClassifier: { enabled: boolean; discardOn: OfferClassification[]; flagOnly: OfferClassification[] };
  youtube: { skipIfNoEmail: boolean; maxCompaniesPerLead: number | null; personNameSearch: boolean };
  ddgSearch: { channelDiscovery: boolean; offerFallback: boolean; clutchEnrichment: boolean };
  icp: { targetNiche: string; scoringWeights: { title: number; tenure: number; size: number; niche: number } };
  output: { leadsTabPrefix: string; writeDiscards: boolean; writeErrors: boolean; writeSessions: boolean };
}
```

---

#### `src/lead-utils.ts` — Pure Primitives

Ports `lead_utils.py` 1:1. No I/O, no side effects, no imports from other src modules.

```typescript
// Parse "11-50" → 50 | "47" → 47 | "myself only" → 1 | "10,001+" → 10001 | "" → null
export function parseCompanySize(s: string): number | null

// Parse "03/2022" or "2022-03" → months since today (capped at 120)
export function parseTenureMonths(startDate: string): number

// Parse "exp_1: employee count, industry | exp_2: job" → { exp_1: [...], exp_2: [...] }
export function parseMismatchedFilters(raw: string): Record<string, string[]>

// 3=Founder/CEO, 2=MD/Partner, 1=VP/Head, 0=Advisor, -1=Manager/Specialist
export function scoreJobTitle(title: string): number

// 3=1-5, 2=6-15, 1=16-50, 0=51-200, -1=201-500, -2=500+
export function scoreCompanySize(size: number | null): number

// 0-5 keyword overlap between company profile fields and target niche
export function scoreNicheFit(company: CompanyRecord, targetNiche: string): number

// "1M USD - 2.5M USD" → [1000000, 2500000] | unparseable → null
export function parseRevenueRange(raw: string): [number, number] | null
```

Test parity target: replicate all 12 Python unit tests + 7A suite (10 multi-company scoring tests).

---

#### `src/filters.ts` — 12-Filter Pipeline

Ports `pipeline_filters.py`. Each filter:
- Pure function: `filterX(profile: Profile, config: PipelineConfig): FilterResult`
- No side effects
- `applyFilters(profile, config)` runs the pipeline in order, returns on first discard

```typescript
// Pipeline order (same as Python):
const FILTER_PIPELINE = [
  filterPrescreen,
  filterSize,
  filterTitle,
  filterLocation,
  filterIndustry,
  filterKeywords,
  filterRevenue,
  filterTenure,
  filterPrimaryScore,
  filterMultiCompany,
  filterContact,
  filterActivity,
];

export function remapRow(row: Record<string, string>, columnMap: Record<string, string>): Record<string, string>
export function applyFilters(profile: Profile, config: PipelineConfig): FilterResult
```

---

#### `src/website-classifier.ts` — Offer Classification

Ports the website classifier from `batch_qualify.py`.

```typescript
export async function classifyWebsiteOffer(
  url: string | null,
  config: PipelineConfig
): Promise<{ classification: OfferClassification; reason: string; combinedText: string }>

// Internal helpers
async function fetchPageText(url: string, timeout: number): Promise<string | null>
async function reclassifyUnclearViaDdg(companyName: string, url: string): Promise<OfferClassification | null>
async function enrichViaClutehdDdg(companyName: string): Promise<{ revenueRange: string; minProject: string }>
function detectSocialProof(text: string): SocialProofResult
function classifyText(text: string): { classification: OfferClassification; reason: string; isStrong: boolean }
```

Signal arrays (typed `readonly string[]` constants):
- `HIGH_TICKET_B2B_SIGNALS` (~40 phrases)
- `STRONG_B2B_SIGNALS` (subset that immediately triggers HIGH_TICKET_B2B)
- `B2C_SIGNALS` (~20 phrases)
- `LOW_TICKET_SIGNALS`
- `CASE_STUDY_SIGNALS`, `TESTIMONIAL_SIGNALS`, `ROI_LANGUAGE_SIGNALS`, `DOLLAR_SIGNALS`

HTML parsing: `cheerio` for BeautifulSoup equivalent.
Note: Playwright JS-rendered fallback deferred.

---

#### `src/sheets.ts` — Google Sheets I/O

**Auth:** Service account JSON (NOT PAI's OAuth `googleAuth.ts` — this repo uses a service account).

```typescript
// Service account JWT signing (Bun Web Crypto, RS256)
async function getServiceAccountToken(keyFilePath: string): Promise<string>

// Input
export async function readCsv(filePath: string): Promise<Record<string, string>[]>
export async function readSheet(sheetId: string, range: string, token: string): Promise<Record<string, string>[]>
export async function getAlreadyQualified(sheetId: string, token: string): Promise<Set<string>>

// Dedup
export function isDuplicate(profile: Profile, alreadyDone: Set<string>): boolean
export function deduKey(profile: Profile): string   // LinkedIn URL or (name, company) tuple

// Output
export async function writeToSheet(
  results: ProcessedLead[],
  sheetId: string,
  tabName: string,
  config: PipelineConfig,
  token: string
): Promise<void>

// Session tracking
export function generateSessionId(): string   // YYYY-MM-DD-NNN, reads session_counter.json
export async function writeSessionSummary(summary: SessionSummary, sheetId: string, token: string): Promise<void>
```

Output headers (typed constants):
- `LEADS_HEADERS`: 18 columns (same as Python)
- `DISCARD_HEADERS`, `ERROR_HEADERS`, `SESSIONS_HEADERS`

---

#### `src/youtube.ts` — Channel Discovery + Stage 1

Ports `youtube_qualifier.py`. YouTube API calls via `fetch()` with API key from env.

```typescript
export async function qualifyYoutube(
  personName: string,
  companyName: string,
  websiteUrl: string | null,
  activeCompanies: CompanyRecord[],
  config: PipelineConfig
): Promise<YouTubeResult>

// Discovery pipeline (stops at first success)
async function scrapeWebsiteForChannel(url: string): Promise<ChannelDiscovery | null>
async function findChannelViaDdg(companyName: string, personName: string): Promise<ChannelDiscovery | null>
async function searchAndValidate(query: string, requireCrossValidation: boolean): Promise<ChannelDiscovery | null>

// Channel data
async function getChannelVideos(channelId: string, apiKey: string): Promise<{ videos: VideoRecord[]; channelInfo: ChannelInfo }>

// Stage 1 deterministic checks
function runStage1(videos: VideoRecord[], lastUploadDate: Date | null): YouTubeCondition | null
// Returns: A (no channel), B (dead >60d), C (60d gap), E (all shorts) — null if none match

// Multi-company resolution
function resolveCompanyYouTubeResults(results: CompanyYouTubeResult[]): YouTubeResult

// Name matching
function nameMatch(channelName: string, personName: string, companyName: string): boolean
```

YouTube API helper — typed, fetch-based:
```typescript
async function ytApi<T>(endpoint: string, params: Record<string, string>, apiKey: string): Promise<T>
```

Quota costs (same as Python): search.list = 100 units, channels.list = 1 unit, playlistItems.list = 3 units, videos.list = 3 units.

---

#### `src/pipeline.ts` — Orchestrator

```typescript
export async function processLeads(
  rows: Record<string, string>[],
  config: PipelineConfig,
  options: {
    noClaude: boolean;
    limit: number | null;
    alreadyDone: Set<string>;
    inputFileName: string;
  }
): Promise<{ results: ProcessedLead[]; summary: SessionSummary }>

function normalizeRow(row: Record<string, string>, config: PipelineConfig): Profile
function rankActiveCompanies(profile: Profile, config: PipelineConfig): Profile

function estimateRevenueConfidence(profile: Profile): {
  label: 'High' | 'Medium' | 'Low' | 'Unknown';
  score: number;
  breakdown: Record<string, number>;
}
```

---

#### `cli.ts` — Entry Point

```typescript
// Process mode
// bun cli.ts process --input leads.csv --output-sheet SHEET_ID --no-claude [--limit N]
// bun cli.ts process --input SHEET_URL --output-sheet SHEET_ID --no-claude [--limit N]

// Write mode  
// bun cli.ts write --results /tmp/results.json --output-sheet SHEET_ID [--tab-name "May 2026"]

// Debug modes (parity with Python debug flags)
// bun cli.ts test-normalize
// bun cli.ts test-size
```

Exit codes: `0` = success, `1` = bad args, `2` = API/auth error.

Process mode writes STAGE2_NEEDED rows as JSON to stdout (pipe-friendly).

---

### External Dependencies

| Dep | Purpose | Source |
|-----|---------|--------|
| `cheerio` | HTML parsing (replaces BeautifulSoup) | npm |
| `duck-duck-scrape` OR DuckDuckGo via fetch | DDG search (replaces Python duckduckgo-search) | npm |
| `dotenv` or Bun built-in | Load `.env` | bun built-in (`Bun.env`) |
| Bun Web Crypto | Service account JWT (RS256) — no external dep needed | bun built-in |
| `@types/cheerio` | TypeScript types | npm dev |

No `googleapis`, no `google-auth-library`, no `axios`, no `node-fetch`. Use native `fetch` everywhere.

---

### Build Phases

| Phase | Files | Deliverable | Gate |
|-------|-------|-------------|------|
| 1 | `src/types.ts`, `src/config.ts`, `src/lead-utils.ts`, `tests/lead-utils.test.ts` | Typed primitives + config | `bun test tests/lead-utils.test.ts` → 22 tests pass |
| 2 | `src/filters.ts`, `tests/filters.test.ts` | 12-filter pipeline | `bun test tests/filters.test.ts` → 10 tests pass |
| 3 | `src/website-classifier.ts` | Website offer classification | Manual test on 3 known URLs |
| 4 | `src/sheets.ts` | Sheets I/O + service account auth | Read from test sheet, write 1 row |
| 5 | `src/youtube.ts` | Channel discovery + Stage 1 | `bun test tests/pipeline.test.ts` → 42 tests pass |
| 6 | `src/pipeline.ts` + `cli.ts` | Full CLI, both modes | Dry run on 3 leads, diff vs Python output |
| 7 | `.claude/skills/YT-Qualifier-skill/SKILL.md` | Skill updated to use bun CLI | `/qualify-leads 3` runs end-to-end |

---

### Verification (Final Gate)

```bash
# 1. Full test suite
bun test
# Expected: equivalent of Python's 64-test pass

# 2. Side-by-side comparison (3 leads, no YouTube API)
.venv/bin/python3 batch_qualify.py --input Input_lists/Marketing-advanced.csv \
  --output-sheet $OUTPUT_SHEET_ID --no-claude --limit 3 > /tmp/python-out.json

bun cli.ts process --input Input_lists/Marketing-advanced.csv \
  --output-sheet $OUTPUT_SHEET_ID --no-claude --limit 3 > /tmp/ts-out.json

# 3. Diff key fields: condition, offerClassification, primaryScore
# These must match between Python and TypeScript outputs
```

---

### What Is NOT in This Rebuild

- Playwright JS-rendered site fallback (deferred)
- Any new features or config options
- Merging dev1.1 → main
- Stage 2 Claude judgment (unchanged, handled by the skill in-session)
