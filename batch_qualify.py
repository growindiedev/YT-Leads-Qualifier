from __future__ import annotations

"""
batch_qualify.py — Batch YouTube lead qualifier

Process mode (run qualification on all leads, skip Stage 2 API calls):
    python batch_qualify.py --input leads.csv --output-sheet SHEET_ID --no-claude

Process mode (run everything end-to-end with Anthropic API):
    python batch_qualify.py --input leads.csv --output-sheet SHEET_ID

Write mode (used by skill after in-session Stage 2 judgment):
    python batch_qualify.py --write-results results.json --output-sheet SHEET_ID [--tab-name "My Tab"]

Input can be a CSV file path or a Google Sheets URL/ID.
Output sheet must be a Google Sheets ID or URL (the sheet must already exist).
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Allow running from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from youtube_qualifier import qualify_youtube

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

OUTPUT_HEADERS = [
    "Full Name",
    "Job Title",
    "Company",
    "Company Size",
    "Company LinkedIn URL",
    "Personal LinkedIn URL",
    "Company Website",
    "Email Address",
    "Other Contact Info",
    "Offer Classification",
    "Multi Company",
    "All Companies",
    "YouTube Channel URL",
    "YouTube Status",
    "Last LinkedIn Activity",
    "Why Chosen",
    "Confidence",
    "Error",
]

LEADS_HEADERS = [
    "Full Name", "Job Title", "Company", "Company Size",
    "Company LinkedIn URL", "Personal LinkedIn URL", "Company Website",
    "Email Address", "Other Contact Info", "YouTube Channel URL",
    "YouTube Status", "Last LinkedIn Activity", "Why Chosen",
    "Offer Classification", "Confidence", "Multi Company", "All Companies",
]

DISCARD_HEADERS = [
    "Full Name", "Job Title", "Company", "Company Size",
    "Personal LinkedIn URL", "Company Website",
    "Discard Reason", "Mismatched Filters", "Date Added",
]

ERROR_HEADERS = [
    "Full Name", "Company", "Personal LinkedIn URL",
    "Error Message", "Date Added",
]

CONDITION_LABELS = {
    "A": "A (no presence)",
    "B": "B (abandoned)",
    "C": "C (inconsistent)",
    "D": "D (raw podcast only)",
    "E": "E (shorts only)",
    "F": "F (off-topic content)",
    "FAIL": "FAIL",
}


# ---------------------------------------------------------------------------
# Google Sheets helpers (uses requests via AuthorizedSession — avoids httplib2/LibreSSL issues)
# ---------------------------------------------------------------------------

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


def _get_session():
    """Return an AuthorizedSession for the Google Sheets REST API."""
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession as _AuthSession
    except ImportError:
        raise RuntimeError(
            "Google auth libraries not installed. Run: "
            "pip install google-auth google-auth-oauthlib"
        )

    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    if not creds_file:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS_FILE environment variable is not set. "
            "It must point to a Google service account JSON key file.\n"
            "See: https://cloud.google.com/iam/docs/creating-managing-service-account-keys"
        )
    if not os.path.exists(creds_file):
        raise RuntimeError(
            f"Service account key file not found: {creds_file}\n"
            "Check that GOOGLE_CREDENTIALS_FILE is set to the correct path."
        )

    creds = service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    return _AuthSession(creds)


def _extract_sheet_id(url_or_id: str) -> str:
    """Extract spreadsheet ID from a Google Sheets URL, or return as-is if it's already an ID."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    return match.group(1) if match else url_or_id


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _read_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _read_google_sheet(sheet_id: str, range_: str = "A1:ZZ") -> list:
    session = _get_session()
    resp = session.get(f"{SHEETS_BASE}/{sheet_id}/values/{range_}")
    resp.raise_for_status()
    rows = resp.json().get("values", [])
    if not rows:
        return []
    headers = rows[0]
    return [
        dict(zip(headers, row + [""] * max(0, len(headers) - len(row))))
        for row in rows[1:]
    ]


def _get_already_qualified(sheet_id: str) -> set:
    """
    Read all tabs in the output sheet and return a set of dedup keys
    for leads that have already been processed.

    Dedup key priority:
      1. Personal LinkedIn URL (normalised, most reliable)
      2. (full_name.lower(), company.lower()) tuple as fallback

    Uses a single batchGet call (all tabs at once) to minimise API round-trips.
    Range capped at A1:P to avoid fetching unused columns.
    """
    session = _get_session()

    # Get list of all sheet tabs
    resp = session.get(f"{SHEETS_BASE}/{sheet_id}?fields=sheets.properties")
    if not resp.ok:
        print(f"  Warning: could not read output sheet tabs ({resp.status_code}). Dedup skipped.", file=sys.stderr)
        return set()

    tabs = [s["properties"]["title"] for s in resp.json().get("sheets", [])]
    if not tabs:
        return set()

    # Fetch all tabs in a single batchGet request, narrow range to first 16 columns
    from urllib.parse import urlencode
    params = urlencode([("ranges", f"'{t}'!A1:P") for t in tabs], doseq=True)
    batch_resp = session.get(f"{SHEETS_BASE}/{sheet_id}/values:batchGet?{params}")
    if not batch_resp.ok:
        print(f"  Warning: batchGet failed ({batch_resp.status_code}). Dedup skipped.", file=sys.stderr)
        return set()

    already_done = set()
    for value_range in batch_resp.json().get("valueRanges", []):
        rows = value_range.get("values", [])
        if len(rows) < 2:
            continue
        header = rows[0]
        if "Full Name" not in header:
            continue
        fn_idx = header.index("Full Name")
        co_idx = header.index("Company") if "Company" in header else None
        li_idx = header.index("Personal LinkedIn URL") if "Personal LinkedIn URL" in header else None

        for row in rows[1:]:
            def cell(i):
                return row[i].strip() if i is not None and i < len(row) else ""

            linkedin = cell(li_idx).lower().rstrip("/")
            if linkedin:
                already_done.add(linkedin)
            name = cell(fn_idx).lower()
            company = cell(co_idx).lower() if co_idx is not None else ""
            if name:
                already_done.add((name, company))

    return already_done


def _is_duplicate(profile: dict, already_done: set) -> bool:
    """Return True if this lead has already been processed."""
    linkedin = profile.get("personal_linkedin_url", "").lower().rstrip("/")
    if linkedin and linkedin in already_done:
        return True
    name    = profile.get("full_name", "").lower()
    company = profile.get("company", "").lower()
    if name and (name, company) in already_done:
        return True
    return False


# ---------------------------------------------------------------------------
# Row normalisation
# ---------------------------------------------------------------------------

def _extract_job_group(row: dict, suffix: str = "") -> dict:
    """
    Extract one job experience group from a row.
    suffix = "" for primary, " (2)" for second, " (3)" for third, " (4)" for fourth.
    Returns a dict with normalised keys, or None if the group has no company name.
    """
    def g(key):
        return row.get(f"{key}{suffix}", "").strip()

    company = g("company")
    if not company:
        return None

    return {
        "job_title":            g("job title"),
        "company":              company,
        "company_linkedin":     g("corporate linkedin url"),
        "company_website":      g("corporate website"),
        "company_size_range":   g("linkedin employees"),
        "company_size_count":   g("linkedin company employee count"),
        "company_description":  g("linkedin description"),
        "company_specialities": g("linkedin specialities"),
        "company_industry":     g("linkedin industry"),
        "company_revenue":      g("linkedin company revenue range"),
        "job_started":          g("job started on"),
        "job_ended":            g("job ended on"),
        "is_active":            g("job ended on") == "",
    }


def _normalize_row(row: dict) -> dict:
    """
    Extract all relevant fields from a row, handling up to 4 job experience
    groups from Sales Nav exports (primary + suffixes " (2)", " (3)", " (4)").
    Returns a dict with standardised keys ready for output and qualification.
    """

    def get(*keys):
        for k in keys:
            v = row.get(k, "").strip()
            if v:
                return v
        return ""

    # Collect all job groups
    groups = []
    for suffix in ["", " (2)", " (3)", " (4)"]:
        group = _extract_job_group(row, suffix)
        if group:
            groups.append(group)

    active_groups = [g for g in groups if g["is_active"]]
    past_groups   = [g for g in groups if not g["is_active"]]

    # If no active groups found (all have end dates), use the first group as primary
    if not active_groups and groups:
        active_groups = [groups[0]]

    primary = active_groups[0] if active_groups else {}

    return {
        # Person-level fields
        "full_name": (get("first name") + " " + get("last name")).strip()
                     or get("name", "full name"),
        "personal_linkedin_url": get("linkedin url"),
        "email":         get("email") or "Not Found",
        "other_contact": get("phone") or "None",
        "location":      get("location"),

        # Primary company fields (used by all downstream gates)
        "job_title":           primary.get("job_title", ""),
        "company":             primary.get("company", ""),
        "company_size":        primary.get("company_size_count", "")
                               or primary.get("company_size_range", ""),
        "company_linkedin_url": primary.get("company_linkedin", ""),
        "website":             primary.get("company_website") or None,

        # Multi-company fields
        "multi_company_flag": len(active_groups) > 1,
        "active_companies":   active_groups,
        "all_companies":      [g["company"] for g in active_groups],
        "past_companies":     [g["company"] for g in past_groups],

        # Profile text (used by skill for Why Chosen generation)
        "_summary":              get("summary"),
        "_headline":             get("headline"),
        "_company_description":  primary.get("company_description", ""),
        "_specialities":         primary.get("company_specialities", ""),
        "_industry":             primary.get("company_industry", ""),
        "_location":             get("location"),

        # Sales Nav filter signals (used by prescreen gate)
        "_mismatched_filters":   get("mismatched filters"),
        "_matching_filters":     get("matching filters"),
    }


# ---------------------------------------------------------------------------
# Website offer classifier
# ---------------------------------------------------------------------------

HIGH_TICKET_B2B_SIGNALS = [
    "book a call", "book a discovery", "apply now", "schedule a call",
    "strategy session", "retainer", "consulting engagement", "advisory",
    "done-for-you", "done for you", "custom proposal", "fractional",
    "coaching program", "executive coaching", "management consulting",
    "for businesses", "for companies", "for executives", "for founders",
    "for teams", "work with us", "our clients", "client results",
    "b2b", "enterprise clients", "corporate clients", "speaking fee",
    "speaking engagement", "keynote", "workshop", "mastermind",
    "accelerator program", "investment required"
]

B2C_SIGNALS = [
    "add to cart", "shop now", "buy now", "free shipping",
    "lose weight", "weight loss", "dating", "relationship advice",
    "fitness program", "meal plan", "workout plan",
    "$27", "$47", "$97", "$17", "$7", "$37",
    "order now", "checkout", "personal finance for individuals",
    "consumer", "retail store", "track your order"
]

LOW_TICKET_SIGNALS = [
    "online course", "self-paced", "lifetime access",
    "digital download", "ebook", "template pack",
    "membership site", "join the community for",
    "enroll now", "udemy", "teachable", "kajabi course",
    "buy the course", "get instant access", "mini course",
    "free training", "free webinar", "free masterclass"
]


def classify_website_offer(url: "str | None") -> "tuple[str, str]":
    if not url:
        return ("NO_WEBSITE", "No website URL available")

    if not url.startswith("http"):
        url = "https://" + url

    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ContentScaleBot/1.0)"}
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code != 200:
            return ("FETCH_FAILED", f"HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)[:4000].lower()
    except requests.Timeout:
        return ("FETCH_FAILED", "Request timed out")
    except Exception as e:
        return ("FETCH_FAILED", str(e)[:80])

    b2b_score = sum(1 for s in HIGH_TICKET_B2B_SIGNALS if s in text)
    b2c_score = sum(1 for s in B2C_SIGNALS if s in text)
    low_score = sum(1 for s in LOW_TICKET_SIGNALS if s in text)

    if b2c_score >= 2:
        return ("B2C", f"B2C signals: {b2c_score}")
    if low_score >= 2:
        return ("LOW_TICKET", f"Low-ticket signals: {low_score}")
    if b2b_score >= 2:
        return ("HIGH_TICKET_B2B", f"B2B signals: {b2b_score}")
    if b2b_score == 1 and b2c_score == 0 and low_score == 0:
        return ("HIGH_TICKET_B2B", "Weak B2B signal, no counter-signals")

    return ("UNCLEAR", f"B2B:{b2b_score} B2C:{b2c_score} Low:{low_score}")


# ---------------------------------------------------------------------------
# Mismatched-filter pre-screen helpers
# ---------------------------------------------------------------------------

def parse_mismatched_filters(mismatched: str) -> dict:
    """
    Parse the mismatched filters string into a dict keyed by experience slot.
    Returns e.g. {"exp_1": ["employee count", "industry"], "exp_2": ["job"]}
    """
    result = {}
    if not mismatched:
        return result
    for segment in mismatched.split("|"):
        segment = segment.strip()
        match = re.match(r"(exp_\d+):\s*(.+)", segment)
        if match:
            exp_key = match.group(1).strip()
            reasons = [r.strip() for r in match.group(2).split(",")]
            result[exp_key] = reasons
    return result


def should_prescreen_discard(profile: dict) -> "tuple[bool, str]":
    """
    Return (True, reason) if the lead should be discarded based on Sales Nav
    mismatched-filter signals, otherwise (False, "").
    """
    mismatch = parse_mismatched_filters(profile.get("_mismatched_filters", ""))

    if mismatch:
        # Build active exp keys — exp_1 always included; add more if multi-company
        if profile.get("multi_company_flag"):
            n = len(profile.get("active_companies", []))
            active_exp_keys = [f"exp_{i+1}" for i in range(n)]
        else:
            active_exp_keys = ["exp_1"]

        # Rule 1: primary company has employee count mismatch → discard
        if "employee count" in mismatch.get("exp_1", []):
            return (True, "Sales Nav: primary company employee count mismatch")

        # Rule 2: ALL active slots have employee count mismatch → discard
        all_active_have_count_mismatch = all(
            "employee count" in mismatch.get(k, [])
            for k in active_exp_keys
            if k in mismatch
        )
        if len(active_exp_keys) > 1 and all_active_have_count_mismatch:
            return (True, "Sales Nav: all active companies employee count mismatch")

        # Rule 3: only secondary slots have employee count mismatch → pass
        # (primary is fine; fall through)

    # Rule 4: matching_filters explicitly false → discard
    if profile.get("_matching_filters", "").strip().lower() == "false":
        return (True, "Sales Nav: lead does not match search filters")

    return (False, "")


# ---------------------------------------------------------------------------
# Niche scoring helper
# ---------------------------------------------------------------------------

def score_company_for_niche(company_dict: dict, target_niche: str) -> int:
    """
    Score a company against the target niche using keyword overlap.
    Higher score = better fit. Returns 0 if no niche provided.
    """
    if not target_niche:
        return 0

    niche_tokens = set(target_niche.lower().split())
    score = 0

    searchable = " ".join([
        company_dict.get("company_description", ""),
        company_dict.get("company_specialities", ""),
        company_dict.get("company_industry", ""),
        company_dict.get("job_title", ""),
        company_dict.get("company", ""),
    ]).lower()

    for token in niche_tokens:
        if len(token) > 3 and token in searchable:
            score += 1

    return score


# ---------------------------------------------------------------------------
# Company size helpers
# ---------------------------------------------------------------------------

def parse_company_size(size_string: str) -> "int | None":
    if not size_string:
        return None
    s = size_string.lower().strip()

    # Solo operators
    if any(word in s for word in ["myself", "self-employed", "freelance"]):
        return 1

    # Strip commas and plus signs, find all numbers
    s_clean = s.replace(",", "").replace("+", "")
    numbers = re.findall(r"\d+", s_clean)
    if not numbers:
        return None

    nums = [int(n) for n in numbers]

    # Range like "11-50" → return upper bound
    if len(nums) >= 2:
        return max(nums)

    # Single number
    return nums[0]


# ---------------------------------------------------------------------------
# Session ID + summary helpers
# ---------------------------------------------------------------------------

_SESSION_COUNTER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_counter.json")

SESSIONS_HEADERS = [
    "Session ID", "Date", "Input File", "Total Loaded", "Skipped (Dedup)",
    "Discarded (Prescreen)", "Discarded (Size)", "Discarded (Offer)",
    "YouTube Errors", "Condition A", "Condition B", "Condition C", "Condition D",
    "Condition E", "Condition F", "FAIL", "Stage2 Needed", "Total Qualified",
    "YouTube Quota (est)", "Run Time (seconds)",
]


def generate_session_id() -> str:
    """Return a session ID in YYYY-MM-DD-NNN format, incrementing a per-day counter."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        with open(_SESSION_COUNTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    count = data.get(today, 0) + 1
    data[today] = count

    with open(_SESSION_COUNTER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

    return f"{today}-{count:03d}"


def write_session_summary(sheet_id: str, summary: dict) -> None:
    """Append one row to the Sessions tab (creates tab with headers if missing)."""
    session = _get_session()
    tab = "Sessions"
    row = [
        summary.get("session_id", ""),
        summary.get("date", ""),
        summary.get("input_file", ""),
        summary.get("total_loaded", 0),
        summary.get("skipped_dedup", 0),
        summary.get("discarded_prescreen", 0),
        summary.get("discarded_size", 0),
        summary.get("discarded_offer", 0),
        summary.get("youtube_errors", 0),
        summary.get("condition_a", 0),
        summary.get("condition_b", 0),
        summary.get("condition_c", 0),
        summary.get("condition_d", 0),
        summary.get("condition_e", 0),
        summary.get("condition_f", 0),
        summary.get("fail", 0),
        summary.get("stage2_needed", 0),
        summary.get("total_qualified", 0),
        summary.get("youtube_quota_est", 0),
        summary.get("run_time_seconds", 0),
    ]
    if _tab_exists(session, sheet_id, tab):
        _append_tab(session, sheet_id, tab, [row])
    else:
        _create_tab(session, sheet_id, tab)
        _write_tab(session, sheet_id, tab, SESSIONS_HEADERS, [row])


# ---------------------------------------------------------------------------
# Core batch processor
# ---------------------------------------------------------------------------

def process_leads(
    rows: list,
    no_claude: bool,
    already_done: set = None,
    limit: int = None,
    target_niche: str = "",
    input_file_name: str = "",
) -> "tuple[list, dict]":
    """
    Run YouTube qualification for every row.
    Returns (results, summary) where results is a list of result dicts and
    summary is a dict suitable for write_session_summary().
    """
    start_time = time.time()
    results = []
    total = len(rows)

    skipped = 0
    prescreen_count = 0
    size_discard_count = 0
    offer_discard_count = 0
    for i, row in enumerate(rows, 1):
        profile = _normalize_row(row)

        # --- Multi-company: log and optionally reassign primary ---
        if profile["multi_company_flag"]:
            print(
                f"  ⚠ Multi-company: {profile['full_name']} has active roles at: "
                f"{', '.join(profile['all_companies'])}",
                file=sys.stderr,
            )
            scored = [
                (g, score_company_for_niche(g, target_niche))
                for g in profile["active_companies"]
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            best = scored[0][0]
            if best["company"] != profile["company"]:
                print(
                    f"  → Reassigned primary to: {best['company']} "
                    f"(score {scored[0][1]} vs primary {scored[-1][1]})",
                    file=sys.stderr,
                )
                profile["company"]              = best["company"]
                profile["company_size"]         = best.get("company_size_count", "") or best.get("company_size_range", "")
                profile["company_linkedin_url"] = best.get("company_linkedin", "")
                profile["website"]              = best.get("company_website") or None
                profile["job_title"]            = best.get("job_title", "")
                profile["_company_description"] = best.get("company_description", "")
                profile["_specialities"]        = best.get("company_specialities", "")
                profile["_industry"]            = best.get("company_industry", "")

        profile["all_companies_str"] = " | ".join(profile.get("all_companies", []))

        person = profile["full_name"]
        company = profile["company"]
        website = profile["website"]

        if already_done and _is_duplicate(profile, already_done):
            print(f"[{i}/{total}] SKIP (already qualified) — {person} / {company}", file=sys.stderr)
            skipped += 1
            continue

        prescreen, prescreen_reason = should_prescreen_discard(profile)
        if prescreen:
            print(f"[{i}/{total}] DISCARD_PRESCREEN ({prescreen_reason}) — {person} / {company}", file=sys.stderr)
            results.append({
                **profile,
                "error": prescreen_reason,
                "yt_condition": "DISCARD_PRESCREEN",
                "yt_channel_url": None,
                "yt_channel_name": None,
                "yt_last_upload": None,
                "why_chosen": "",
                "confidence": "",
            })
            prescreen_count += 1
            continue

        if limit and len(results) >= limit:
            print(f"Limit of {limit} new leads reached.", file=sys.stderr)
            break

        if not person or not company:
            msg = (
                f"[{i}/{total}] SKIP — missing person or company (row keys: "
                f"{list(row.keys())[:5]})"
            )
            print(msg, file=sys.stderr)
            results.append(
                {
                    **profile,
                    "error": "Missing person or company name",
                    "yt_condition": "ERROR",
                    "yt_channel_url": None,
                    "yt_channel_name": None,
                    "yt_last_upload": None,
                    "why_chosen": "",
                    "confidence": "",
                }
            )
            continue

        size_int = parse_company_size(profile["company_size"])
        if size_int is not None and size_int > 50:
            print(f"[{i}/{total}] DISCARD_SIZE ({profile['company_size']}) — {person} / {company}", file=sys.stderr)
            results.append({
                **profile,
                "error": f"Company too large: {profile['company_size']}",
                "yt_condition": "DISCARD_SIZE",
                "yt_channel_url": None,
                "yt_channel_name": None,
                "yt_last_upload": None,
                "why_chosen": "",
                "confidence": "",
            })
            size_discard_count += 1
            continue

        offer_class, offer_reason = classify_website_offer(profile.get("website"))
        profile["offer_classification"] = offer_class
        if offer_class in ("B2C", "LOW_TICKET", "NO_WEBSITE"):
            print(f"[{i}/{total}] DISCARD_OFFER ({offer_class}: {offer_reason}) — {person} / {company}", file=sys.stderr)
            results.append({
                **profile,
                "error": f"{offer_class}: {offer_reason}",
                "yt_condition": "DISCARD_OFFER",
                "yt_channel_url": None,
                "yt_channel_name": None,
                "yt_last_upload": None,
                "why_chosen": "",
                "confidence": "",
            })
            offer_discard_count += 1
            continue
        elif offer_class in ("FETCH_FAILED", "UNCLEAR"):
            profile["_offer_flag"] = offer_class

        print(f"[{i}/{total}] {person} / {company}", file=sys.stderr)

        try:
            yt = qualify_youtube(person, company, website, no_claude=no_claude)
        except Exception as exc:
            print(f"  ERROR for {person} / {company}: {exc}", file=sys.stderr)
            results.append(
                {
                    **profile,
                    "error": str(exc),
                    "yt_condition": "ERROR",
                    "yt_channel_url": None,
                    "yt_channel_name": None,
                    "yt_last_upload": None,
                    "why_chosen": "",
                    "confidence": "",
                }
            )
            continue

        condition = yt.get("condition", "?")
        print(f"  → {condition}: {yt.get('reasoning', '')[:80]}", file=sys.stderr)

        results.append(
            {
                **profile,
                "error": None,
                "yt_condition": condition,
                "yt_channel_url": yt.get("channel_url"),
                "yt_channel_name": yt.get("channel_name"),
                "yt_last_upload": yt.get("last_upload_date"),
                # Populated by skill after in-session judgment
                "why_chosen": "",
                "confidence": "",
                # Pass-through for skill Stage 2 + insight generation
                "_yt_videos": yt.get("videos"),  # present only when STAGE2_NEEDED
                "_yt_reasoning": yt.get("reasoning", ""),
            }
        )

    if skipped:
        print(f"Skipped {skipped} already-qualified lead(s).", file=sys.stderr)
    if prescreen_count:
        print(f"Discarded {prescreen_count} lead(s) — Sales Nav filter mismatch.", file=sys.stderr)
    if size_discard_count:
        print(f"Discarded {size_discard_count} lead(s) — company too large (>50 employees).", file=sys.stderr)
    if offer_discard_count:
        print(f"Discarded {offer_discard_count} lead(s) — website offer not high-ticket B2B.", file=sys.stderr)

    condition_counts: dict = {}
    for r in results:
        c = r.get("yt_condition", "")
        condition_counts[c] = condition_counts.get(c, 0) + 1

    elapsed = round(time.time() - start_time, 1)

    summary = {
        "session_id":          generate_session_id(),
        "date":                datetime.utcnow().strftime("%Y-%m-%d"),
        "input_file":          input_file_name,
        "total_loaded":        total,
        "skipped_dedup":       skipped,
        "discarded_prescreen": prescreen_count,
        "discarded_size":      size_discard_count,
        "discarded_offer":     offer_discard_count,
        "youtube_errors":      condition_counts.get("ERROR", 0),
        "condition_a":         condition_counts.get("A", 0),
        "condition_b":         condition_counts.get("B", 0),
        "condition_c":         condition_counts.get("C", 0),
        "condition_d":         condition_counts.get("D", 0),
        "condition_e":         condition_counts.get("E", 0),
        "condition_f":         condition_counts.get("F", 0),
        "fail":                condition_counts.get("FAIL", 0),
        "stage2_needed":       condition_counts.get("STAGE2_NEEDED", 0),
        "total_qualified":     len([r for r in results if r.get("yt_condition") in ("A","B","C","D","E","F")]),
        "youtube_quota_est":   (
            condition_counts.get("A", 0) * 100 +
            sum(condition_counts.get(c, 0) * 300 for c in ("B","C","D","E","F","FAIL","STAGE2_NEEDED"))
        ),
        "run_time_seconds":    elapsed,
    }

    return results, summary


# ---------------------------------------------------------------------------
# Google Sheets writer
# ---------------------------------------------------------------------------

def _create_tab(session, sheet_id: str, tab_name: str) -> None:
    """Create a new sheet tab. Silently ignores 'already exists' errors."""
    resp = session.post(
        f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    )
    if not resp.ok and "already exists" not in resp.text:
        resp.raise_for_status()


def _tab_exists(session, sheet_id: str, tab_name: str) -> bool:
    """Return True if a tab with tab_name already exists in the spreadsheet."""
    resp = session.get(f"{SHEETS_BASE}/{sheet_id}?fields=sheets.properties.title")
    if not resp.ok:
        return False
    titles = [s["properties"]["title"] for s in resp.json().get("sheets", [])]
    return tab_name in titles


def _write_tab(session, sheet_id: str, tab_name: str, headers: list, rows: list) -> None:
    """Write header + rows to a tab, overwriting from A1."""
    from urllib.parse import quote
    values = [headers] + rows
    range_ref = f"'{tab_name}'!A1"
    resp = session.put(
        f"{SHEETS_BASE}/{sheet_id}/values/{quote(range_ref)}",
        params={"valueInputOption": "RAW"},
        json={"values": values},
    )
    resp.raise_for_status()


def _append_tab(session, sheet_id: str, tab_name: str, rows: list) -> None:
    """Append rows to an existing tab (skips header)."""
    from urllib.parse import quote
    range_ref = f"'{tab_name}'!A1"
    resp = session.post(
        f"{SHEETS_BASE}/{sheet_id}/values/{quote(range_ref)}:append",
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": rows},
    )
    resp.raise_for_status()


def _build_lead_row(r: dict) -> list:
    raw_condition = r.get("yt_condition", "")
    yt_status = CONDITION_LABELS.get(raw_condition, raw_condition)
    return [
        r.get("full_name", ""),
        r.get("job_title", ""),
        r.get("company", ""),
        r.get("company_size", ""),
        r.get("company_linkedin_url", ""),
        r.get("personal_linkedin_url", ""),
        r.get("website", "") or "",
        r.get("email", "Not Found"),
        r.get("other_contact", "None"),
        r.get("yt_channel_url", "") or "None",
        yt_status if yt_status not in ("STAGE2_NEEDED", "") else "",
        "Not available",
        r.get("why_chosen", ""),
        r.get("offer_classification", ""),
        r.get("confidence", ""),
        "Yes" if r.get("multi_company_flag") else "No",
        r.get("all_companies_str", ""),
    ]


def _build_discard_row(r: dict) -> list:
    return [
        r.get("full_name", ""),
        r.get("job_title", ""),
        r.get("company", ""),
        r.get("company_size", ""),
        r.get("personal_linkedin_url", ""),
        r.get("website", "") or "",
        r.get("error", "") or "",
        r.get("_mismatched_filters", ""),
        datetime.utcnow().strftime("%Y-%m-%d"),
    ]


def _build_error_row(r: dict) -> list:
    return [
        r.get("full_name", ""),
        r.get("company", ""),
        r.get("personal_linkedin_url", ""),
        r.get("error", "") or "",
        datetime.utcnow().strftime("%Y-%m-%d"),
    ]


def write_to_sheet(results: list, sheet_id: str, tab_name: str = None) -> str:
    """
    Splits results into three tabs and writes each:
      - Leads tab (timestamped, always new) — qualified leads only
      - Discards tab (persistent, appended) — DISCARD_* rows
      - Errors tab (persistent, appended) — ERROR rows
    Returns the spreadsheet URL.
    """
    session = _get_session()

    _DISCARD_CONDITIONS = {"DISCARD_SIZE", "DISCARD_PRESCREEN", "DISCARD_OFFER", "DISCARD"}
    _ERROR_CONDITIONS   = {"ERROR"}
    _QUALIFIED_EXCLUDE  = _DISCARD_CONDITIONS | _ERROR_CONDITIONS | {"STAGE2_NEEDED"}

    qualified = [r for r in results if r.get("yt_condition") not in _QUALIFIED_EXCLUDE]
    discards  = [r for r in results if r.get("yt_condition") in _DISCARD_CONDITIONS]
    errors    = [r for r in results if r.get("yt_condition") in _ERROR_CONDITIONS]

    leads_tab    = tab_name or f"Leads {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    discards_tab = "Discards"
    errors_tab   = "Errors"

    if qualified:
        _create_tab(session, sheet_id, leads_tab)
        _write_tab(session, sheet_id, leads_tab, LEADS_HEADERS,
                   [_build_lead_row(r) for r in qualified])
        print(f"  Leads tab '{leads_tab}': {len(qualified)} rows written.", file=sys.stderr)

    if discards:
        discard_rows = [_build_discard_row(r) for r in discards]
        if _tab_exists(session, sheet_id, discards_tab):
            _append_tab(session, sheet_id, discards_tab, discard_rows)
            print(f"  Discards tab: {len(discards)} rows appended.", file=sys.stderr)
        else:
            _create_tab(session, sheet_id, discards_tab)
            _write_tab(session, sheet_id, discards_tab, DISCARD_HEADERS, discard_rows)
            print(f"  Discards tab: {len(discards)} rows written (new tab).", file=sys.stderr)

    if errors:
        error_rows = [_build_error_row(r) for r in errors]
        if _tab_exists(session, sheet_id, errors_tab):
            _append_tab(session, sheet_id, errors_tab, error_rows)
            print(f"  Errors tab: {len(errors)} rows appended.", file=sys.stderr)
        else:
            _create_tab(session, sheet_id, errors_tab)
            _write_tab(session, sheet_id, errors_tab, ERROR_HEADERS, error_rows)
            print(f"  Errors tab: {len(errors)} rows written (new tab).", file=sys.stderr)

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Batch YouTube lead qualifier",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input",
        help="CSV file path or Google Sheets URL/ID to read leads from",
    )
    parser.add_argument(
        "--output-sheet",
        required=True,
        metavar="SHEET_ID",
        help="Google Sheets ID or URL to write results to (sheet must exist)",
    )
    parser.add_argument(
        "--no-claude",
        action="store_true",
        help=(
            "Skip Stage 2 Anthropic API calls. "
            "Rows needing Stage 2 get condition=STAGE2_NEEDED. "
            "Output is JSON on stdout for the skill to handle."
        ),
    )
    parser.add_argument(
        "--write-results",
        metavar="JSON_FILE",
        help="Skip processing; read finalized results from this JSON file and write to sheet",
    )
    parser.add_argument(
        "--tab-name",
        help="Name for the new sheet tab (default: 'YT Results YYYY-MM-DD HH:MM')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Only process the first N leads (after dedup skips)",
    )

    args = parser.parse_args()
    sheet_id = _extract_sheet_id(args.output_sheet)

    # --- Write mode ---
    if args.write_results:
        with open(args.write_results, encoding="utf-8") as f:
            results = json.load(f)
        url = write_to_sheet(results, sheet_id, args.tab_name)
        print(f"\nResults written to: {url}")
        return

    # --- Process mode ---
    if not args.input:
        parser.error("--input is required unless --write-results is used")

    # Read leads from CSV or Google Sheet
    if "docs.google.com" in args.input or re.match(r"^[A-Za-z0-9_-]{20,}$", args.input):
        input_sheet_id = _extract_sheet_id(args.input)
        rows = _read_google_sheet(input_sheet_id)
    else:
        rows = _read_csv(args.input)

    if not rows:
        print("No leads found in input. Exiting.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(rows)} leads.", file=sys.stderr)

    # Dedup: check output sheet for already-qualified leads
    print("Checking output sheet for duplicates...", file=sys.stderr)
    already_done = _get_already_qualified(sheet_id)
    if already_done:
        print(f"Found {len(already_done)} existing entries in output sheet.", file=sys.stderr)

    results, summary = process_leads(
        rows,
        no_claude=args.no_claude,
        already_done=already_done,
        limit=args.limit,
        input_file_name=os.path.basename(args.input),
    )

    if not results:
        print("All leads already qualified. Nothing new to process.", file=sys.stderr)
        sys.exit(0)

    if args.no_claude:
        # Output JSON for the skill — it will handle Stage 2 and call --write-results
        print(json.dumps(results, default=str))
    else:
        # Full end-to-end: write directly to Google Sheets
        url = write_to_sheet(results, sheet_id, args.tab_name)
        write_session_summary(sheet_id, summary)
        total_discarded = (
            summary["discarded_prescreen"]
            + summary["discarded_size"]
            + summary["discarded_offer"]
        )
        print(f"\nSession {summary['session_id']} complete.")
        print(f"Qualified:        {summary['total_qualified']} leads")
        print(f"Discarded total:  {total_discarded}")
        print(f"Quota used (est): {summary['youtube_quota_est']} / 10000 units")
        print(f"Results: {url}")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "--test-size":
        assert parse_company_size("11-50")       == 50
        assert parse_company_size("51-200")      == 200
        assert parse_company_size("2-10")        == 10
        assert parse_company_size("47")          == 47
        assert parse_company_size("myself only") == 1
        assert parse_company_size("")            is None
        assert parse_company_size("10,001+")     == 10001
        assert parse_company_size("1,001-5,000") == 5000
        print("All size tests passed.")
    elif len(_sys.argv) > 1 and _sys.argv[1] == "--test-normalize":
        import csv as _csv
        with open("salees-nav-advanced-keys.csv", newline="", encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
        for row in rows[:5]:
            p = _normalize_row(row)
            print(f"\n{p['full_name']} @ {p['company']}")
            print(f"  Size:          {p['company_size']}")
            print(f"  Multi-company: {p['multi_company_flag']}")
            if p['multi_company_flag']:
                print(f"  All companies: {p['all_companies']}")
            print(f"  Mismatched:    {p['_mismatched_filters']}")
    else:
        main()
