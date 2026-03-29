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
from pipeline_filters import apply_filters, load_pipeline_config, remap_row
from lead_utils import (
    parse_company_size,
    parse_tenure_months,
    score_job_title,
    score_company_size,
    score_niche_fit,
)

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
    "Primary Score", "Score Detail",
    "YouTube Resolution", "Secondary Channels",
    "Revenue Confidence", "Revenue Score",
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


def _normalize_row(row: dict, suffixes: list = None) -> dict:
    """
    Extract all relevant fields from a row, handling up to 4 job experience
    groups from Sales Nav exports (primary + suffixes " (2)", " (3)", " (4)").
    Returns a dict with standardised keys ready for output and qualification.

    suffixes: list of job-group column suffixes to scan. Defaults to
              ["", " (2)", " (3)", " (4)"]. Override via pipeline_config.json
              input.multi_company_suffixes for non-Sales-Nav CSVs.
    """
    if suffixes is None:
        suffixes = ["", " (2)", " (3)", " (4)"]

    def get(*keys):
        for k in keys:
            v = row.get(k, "").strip()
            if v:
                return v
        return ""

    # Collect all job groups
    groups = []
    for suffix in suffixes:
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
        "company_revenue":     primary.get("company_revenue", ""),

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

        # Activity signal (used by activity_filter)
        "last_activity":         get("last linkedin activity", "last activity"),
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
    "accelerator program", "investment required",
    # Common B2B CTA variants
    "let's talk", "lets talk", "get in touch", "book a meeting",
    "free consultation", "free discovery", "discovery call",
    "strategy call", "clarity call", "sales call",
    # Content signals
    "case studies", "case study", "how we work", "our process",
    "who we work with", "client success", "industries we serve",
    # Growth/outcome language
    "scale your", "grow your business", "increase revenue",
    "sales pipeline", "mid-market", "enterprise",
    # Identity signals
    "ceo", "chief executive", "b2b saas", "service business",
    "agency owner", "consulting firm", "coaching business",
    # Engagement model
    "1-on-1", "one-on-one", "monthly retainer", "quarterly",
    "engagement", "proposal", "done with you",
]

STRONG_B2B_SIGNALS = [
    "book a call", "book a discovery", "discovery call", "strategy call",
    "clarity call", "sales call", "apply now", "schedule a call", "retainer",
    "done-for-you", "done for you", "fractional cmo", "fractional coo",
    "fractional cfo", "executive coaching", "management consulting",
    "application required", "apply to work with",
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


def _classify_text(text: str) -> "tuple[str, str]":
    """Score a block of lowercased page text and return (classification, reason)."""
    strong_b2b = sum(1 for s in STRONG_B2B_SIGNALS if s in text)
    b2b_score  = sum(1 for s in HIGH_TICKET_B2B_SIGNALS if s in text)
    b2c_score  = sum(1 for s in B2C_SIGNALS if s in text)
    low_score  = sum(1 for s in LOW_TICKET_SIGNALS if s in text)

    # Strong B2B signals trump everything
    if strong_b2b >= 1:
        return ("HIGH_TICKET_B2B", f"Strong B2B signal: {strong_b2b}")
    if b2b_score >= 2:
        return ("HIGH_TICKET_B2B", f"B2B signals: {b2b_score}")
    if b2b_score == 1 and b2c_score == 0 and low_score == 0:
        return ("HIGH_TICKET_B2B", "Weak B2B signal, no counter-signals")
    if b2c_score >= 2:
        return ("B2C", f"B2C signals: {b2c_score}")
    if low_score >= 2:
        return ("LOW_TICKET", f"Low-ticket signals: {low_score}")

    return ("UNCLEAR", f"B2B:{b2b_score} B2C:{b2c_score} Low:{low_score}")


_WEBSITE_CLASSIFIER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ContentScaleBot/1.0)"}
_FALLBACK_PATHS = ["/services", "/work-with-me", "/how-it-works",
                   "/offerings", "/coaching", "/consulting", "/about"]


def _fetch_page_text(url: str, char_limit: int = 3500, timeout: int = 6) -> "tuple[str, str] | None":
    """
    Fetch a URL and return (lowercased_text, base_scheme_host).
    Returns None on failure. Extracts title + meta description before stripping tags.
    """
    from urllib.parse import urlparse
    try:
        resp = requests.get(url, headers=_WEBSITE_CLASSIFIER_HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract title and meta description before stripping tags
        title_tag = soup.find("title")
        meta_desc = soup.find("meta", attrs={"name": "description"})
        og_desc   = soup.find("meta", attrs={"property": "og:description"})
        title_text = title_tag.get_text(strip=True) if title_tag else ""
        meta_text  = (meta_desc.get("content", "") if meta_desc else "") or \
                     (og_desc.get("content", "") if og_desc else "")
        prefix = (title_text + " " + meta_text + " ").lower()

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        body_text = soup.get_text(separator=" ", strip=True)[:char_limit].lower()

        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return (prefix + body_text, base)
    except Exception:
        return None


CASE_STUDY_SIGNALS = [
    "case study", "case studies", "client story", "client stories",
    "success story", "success stories", "our work", "portfolio",
    "client results", "before and after", "transformation",
    "how we helped", "how i helped", "client wins", "recent wins",
]

TESTIMONIAL_SIGNALS = [
    "testimonial", "testimonials", "what clients say", "what our clients say",
    "client feedback", "hear from", "trusted by", "they say",
    "client spotlight", "what they say", "don't take our word",
    "here's what", "in their words", "client reviews",
]

ROI_LANGUAGE_SIGNALS = [
    "revenue", "pipeline", "generated", "increased by", "grew by",
    "percent", " roi", "return on investment", "leads generated",
    "clients signed", "deals closed", "booked", "within 90 days",
    "within 60 days", "within 6 months", "doubled", "tripled",
    "scaled", "from zero to", "in 30 days", "in 60 days",
    "qualified leads", "sales calls", "discovery calls booked",
    "new clients", "added to pipeline", "in new revenue",
]

DOLLAR_SIGNALS = [
    "$", "k per month", "k/month", "k in revenue", "m in revenue",
    "million", "six figures", "seven figures", "six-figure", "seven-figure",
    "100k", "250k", "500k", "per year", "annually",
]

SOCIAL_PROOF_PAGES = [
    "/case-studies", "/case-study", "/testimonials", "/reviews",
    "/results", "/success-stories", "/client-stories", "/clients",
    "/our-work", "/portfolio", "/proof",
]


def _has_colocated_signals(
    text: str, primary: list, secondary: list, window: int = 500
) -> bool:
    """
    Return True if any secondary signal appears within `window` chars
    of any primary signal in the text.
    """
    for p in primary:
        idx = text.find(p)
        while idx != -1:
            surrounding = text[max(0, idx - window): idx + len(p) + window]
            if any(s in surrounding for s in secondary):
                return True
            idx = text.find(p, idx + 1)
    return False


def _detect_social_proof(text: str) -> dict:
    """
    Scan page text for social proof signals.
    Returns a dict with has_* bools and a total social_proof_score.
    """
    text = text.lower()
    proof_signals = CASE_STUDY_SIGNALS + TESTIMONIAL_SIGNALS

    has_case_studies   = any(s in text for s in CASE_STUDY_SIGNALS)
    has_testimonials   = any(s in text for s in TESTIMONIAL_SIGNALS)
    has_roi_language   = (
        _has_colocated_signals(text, proof_signals, ROI_LANGUAGE_SIGNALS)
        if (has_case_studies or has_testimonials) else False
    )
    has_dollar_amounts = (
        _has_colocated_signals(text, proof_signals, DOLLAR_SIGNALS)
        if (has_case_studies or has_testimonials) else False
    )

    score = 0
    if has_case_studies:
        score += 3
    if has_testimonials:
        score += 1
    if has_roi_language:
        score += 2
    if has_dollar_amounts:
        score += 2

    return {
        "has_case_studies":   has_case_studies,
        "has_testimonials":   has_testimonials,
        "has_roi_language":   has_roi_language,
        "has_dollar_amounts": has_dollar_amounts,
        "social_proof_score": score,
    }


def classify_website_offer(
    url: "str | None",
    config: "dict | None" = None,
) -> "tuple[str, str, str]":
    """
    Returns (classification, reason, combined_text) where combined_text
    is the homepage + social-proof page text for use with _detect_social_proof.
    combined_text is "" on fetch failure.
    """
    wc_cfg = (config or {}).get("website_classifier", {})
    fetch_timeout = wc_cfg.get("fetch_timeout", 6)
    page_char_limit = wc_cfg.get("page_char_limit", 3500)

    if not url:
        return ("NO_WEBSITE", "No website URL available", "")

    if not url.startswith("http"):
        url = "https://" + url

    try:
        result = _fetch_page_text(url, char_limit=page_char_limit, timeout=fetch_timeout)
    except requests.Timeout:
        return ("FETCH_FAILED", "Request timed out", "")
    except Exception as e:
        return ("FETCH_FAILED", str(e)[:80], "")

    if result is None:
        return ("FETCH_FAILED", "Could not fetch page", "")

    text, base = result
    classification, reason = _classify_text(text)

    if classification == "UNCLEAR":
        # Try each fallback page in order; stop at first non-UNCLEAR result
        for path in _FALLBACK_PATHS:
            fallback = _fetch_page_text(base + path, char_limit=2000, timeout=fetch_timeout)
            if fallback is None:
                continue
            fb_text, _ = fallback
            fb_class, fb_reason = _classify_text(fb_text)
            if fb_class != "UNCLEAR":
                return (fb_class, fb_reason + f" (from {path})", text + " " + fb_text)

    # Try to find a social proof page and append its text
    social_text = ""
    for sp_path in SOCIAL_PROOF_PAGES:
        sp_result = _fetch_page_text(base + sp_path, char_limit=2000, timeout=fetch_timeout)
        if sp_result is not None:
            social_text = sp_result[0]
            break

    return (classification, reason, text + " " + social_text)


# ---------------------------------------------------------------------------
# Revenue confidence scoring
# ---------------------------------------------------------------------------

def score_revenue_range(revenue_string: str) -> int:
    """Parse LinkedIn revenue range string and return a confidence score."""
    if not revenue_string:
        return 0
    r = revenue_string.lower().replace(",", "")
    if any(x in r for x in ["100m", "500m", "1b", "10b"]):
        return 1
    if any(x in r for x in ["10m", "20m", "50m"]):
        return 2
    if any(x in r for x in ["2.5m", "5m"]):
        return 4
    if "1m usd" in r or "1m -" in r:
        return 4
    if "500t" in r:
        return 2
    return 0


def estimate_revenue_confidence(
    profile: dict,
    social_proof: dict = None,
) -> "tuple[str, int, dict]":
    """
    Score a lead's likelihood of being a $25k+/month business.
    Returns (confidence_label, total_score, score_breakdown).
    Labels: "High" (>=12), "Medium" (>=8), "Low" (>=4), "Unknown" (<4)
    """
    # Revenue range
    rev_pts = score_revenue_range(profile.get("company_revenue", ""))

    # Company size
    size = parse_company_size(profile.get("company_size", ""))
    if size is None:
        size_pts = 0
    elif size <= 5:
        size_pts = 2
    elif size <= 20:
        size_pts = 3
    elif size <= 50:
        size_pts = 2
    else:
        size_pts = 0

    # Title
    title_raw = score_job_title(profile.get("job_title", ""))
    title_pts = max(0, title_raw) if title_raw <= 3 else 0
    # map: 3→3, 2→2, 1→1, 0→0, -1→0

    # Tenure
    months = parse_tenure_months(profile.get("job_started", ""))
    if months >= 48:
        tenure_pts = 3
    elif months >= 24:
        tenure_pts = 2
    elif months >= 12:
        tenure_pts = 1
    else:
        tenure_pts = 0

    # Social proof
    sp = social_proof or {}
    case_pts  = 3 if sp.get("has_case_studies") else 0
    test_pts  = 1 if sp.get("has_testimonials") else 0
    roi_pts   = 2 if sp.get("has_roi_language") else 0
    dollar_pts = 2 if sp.get("has_dollar_amounts") else 0

    # Offer strength
    offer_class  = profile.get("offer_classification", "")
    offer_reason = profile.get("_offer_reason", "")
    if offer_class == "HIGH_TICKET_B2B" and "strong" in offer_reason.lower():
        offer_pts = 2
    elif offer_class == "HIGH_TICKET_B2B":
        offer_pts = 1
    else:
        offer_pts = 0

    total = (rev_pts + size_pts + title_pts + tenure_pts
             + case_pts + test_pts + roi_pts + dollar_pts + offer_pts)

    if total >= 12:
        label = "High"
    elif total >= 8:
        label = "Medium"
    elif total >= 4:
        label = "Low"
    else:
        label = "Unknown"

    breakdown = {
        "revenue_range":  rev_pts,
        "size":           size_pts,
        "title":          title_pts,
        "tenure":         tenure_pts,
        "case_studies":   case_pts,
        "testimonials":   test_pts,
        "roi_language":   roi_pts,
        "dollar_amounts": dollar_pts,
        "offer_strength": offer_pts,
        "total":          total,
    }
    return (label, total, breakdown)




# ---------------------------------------------------------------------------
# Multi-company scoring helpers
# ---------------------------------------------------------------------------

def rank_active_companies(
    active_companies: list,
    target_niche: str = "",
    weights: dict = None,
) -> list:
    """
    Score and rank all active companies for a lead.
    Returns the list sorted best-first with a 'score' key added to each dict.

    Default scoring weights (overridable via pipeline_config.json icp.scoring_weights):
      title_score  * 3  — operational control
      tenure_score * 2  — long tenure = likely main business
      size_score   * 2  — small = likely founder-led
      niche_score  * 1  — niche fit
      has_website  + 1  — basic viability signal
      has_desc     + 1  — more established
    """
    _DEFAULT_WEIGHTS = {"title": 3, "tenure": 2, "size": 2, "niche": 1}
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}

    scored = []

    for company in active_companies:
        title_score   = score_job_title(company.get("job_title", ""))
        tenure_months = parse_tenure_months(company.get("job_started", ""))

        if tenure_months < 6:
            tenure_score = 0
        elif tenure_months < 12:
            tenure_score = 1
        elif tenure_months < 24:
            tenure_score = 2
        elif tenure_months < 48:
            tenure_score = 3
        elif tenure_months < 72:
            tenure_score = 4
        else:
            tenure_score = 5

        size_score  = score_company_size(
            company.get("company_size_count", "") or
            company.get("company_size_range", "")
        )
        niche_score = score_niche_fit(company, target_niche)
        has_website = 1 if company.get("company_website") else 0
        has_desc    = 1 if company.get("company_description") else 0

        total = (
            title_score  * w["title"]  +
            tenure_score * w["tenure"] +
            size_score   * w["size"]   +
            niche_score  * w["niche"]  +
            has_website                +
            has_desc
        )

        scored.append({
            **company,
            "score": total,
            "score_detail": {
                "title":   title_score,
                "tenure":  tenure_score,
                "size":    size_score,
                "niche":   niche_score,
                "website": has_website,
                "desc":    has_desc,
            }
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored




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
    config: dict = None,
) -> "tuple[list, dict]":
    """
    Run YouTube qualification for every row.
    Returns (results, summary) where results is a list of result dicts and
    summary is a dict suitable for write_session_summary().
    """
    if config is None:
        config = load_pipeline_config()

    # Resolve target_niche: explicit arg > config icp > env var
    icp_cfg = config.get("icp", {})
    resolved_niche = target_niche or icp_cfg.get("target_niche", "") or os.getenv("TARGET_NICHE", "")
    scoring_weights = icp_cfg.get("scoring_weights") or None

    input_cfg = config.get("input", {})
    column_map = input_cfg.get("column_map", {})
    suffixes   = input_cfg.get("multi_company_suffixes", ["", " (2)", " (3)", " (4)"])

    youtube_cfg = config.get("youtube", {})
    skip_youtube_if_no_email  = youtube_cfg.get("skip_if_no_email", False)
    max_companies_per_lead    = youtube_cfg.get("max_companies_per_lead")
    person_name_search        = youtube_cfg.get("person_name_search", True)

    offer_cfg      = config.get("offer_classifier", {})
    offer_discard_on = set(offer_cfg.get("discard_on", ["B2C", "LOW_TICKET", "NO_WEBSITE"]))
    offer_flag_on    = set(offer_cfg.get("flag_only",  ["FETCH_FAILED", "UNCLEAR"]))

    start_time = time.time()
    results = []
    total = len(rows)

    skipped = 0
    filter_discard_counts: dict = {}
    offer_discard_count = 0

    for i, row in enumerate(rows, 1):
        remapped = remap_row(row, column_map)
        profile  = _normalize_row(remapped, suffixes=suffixes)

        # --- Multi-company: rank and reassign primary ---
        ranked = rank_active_companies(
            profile.get("active_companies", []),
            target_niche=resolved_niche,
            weights=scoring_weights,
        )
        if ranked:
            primary = ranked[0]
            profile["company"]              = primary["company"]
            profile["job_title"]            = primary["job_title"]
            profile["company_size"]         = (primary.get("company_size_count") or
                                               primary.get("company_size_range") or "")
            profile["company_linkedin_url"] = primary.get("company_linkedin", "")
            profile["website"]              = primary.get("company_website") or None
            profile["_company_description"] = primary.get("company_description", "")
            profile["_specialities"]        = primary.get("company_specialities", "")
            profile["_industry"]            = primary.get("company_industry", "")
            profile["primary_score"]        = primary["score"]
            profile["primary_score_detail"] = primary["score_detail"]

            if profile["multi_company_flag"]:
                print(
                    f"  ⚠ Multi-company: {profile['full_name']}\n"
                    f"    Primary (score {ranked[0]['score']}): "
                    f"{ranked[0]['company']} — {ranked[0]['job_title']}\n" +
                    "".join(
                        f"    Other   (score {c['score']}): {c['company']} — {c['job_title']}\n"
                        for c in ranked[1:]
                    ),
                    file=sys.stderr,
                )

        profile["all_companies_str"] = " | ".join(profile.get("all_companies", []))

        person = profile["full_name"]
        company = profile["company"]

        if already_done and _is_duplicate(profile, already_done):
            print(f"[{i}/{total}] SKIP (already qualified) — {person} / {company}", file=sys.stderr)
            skipped += 1
            continue

        # --- Configurable pre-offer filter pipeline ---
        filter_result = apply_filters(profile, config)
        if filter_result.discard:
            print(f"[{i}/{total}] {filter_result.condition} ({filter_result.reason}) — {person} / {company}",
                  file=sys.stderr)
            results.append({
                **profile,
                "error": filter_result.reason,
                "yt_condition": filter_result.condition,
                "yt_channel_url": None,
                "yt_channel_name": None,
                "yt_last_upload": None,
                "why_chosen": "",
                "confidence": "",
            })
            filter_discard_counts[filter_result.condition] = (
                filter_discard_counts.get(filter_result.condition, 0) + 1
            )
            continue

        if limit and len(results) >= limit:
            print(f"Limit of {limit} new leads reached.", file=sys.stderr)
            break

        if not person or not company:
            msg = (
                f"[{i}/{total}] SKIP — missing person or company (row keys: "
                f"{list(remapped.keys())[:5]})"
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

        # --- Offer classifier (discard list driven by config) ---
        offer_class, offer_reason, offer_text = classify_website_offer(profile.get("website"), config)
        profile["offer_classification"] = offer_class
        profile["_offer_reason"] = offer_reason
        profile["_social_proof"] = _detect_social_proof(offer_text) if offer_text else None
        if offer_class in offer_discard_on:
            print(f"[{i}/{total}] DISCARD_OFFER ({offer_class}: {offer_reason}) — {person} / {company}",
                  file=sys.stderr)
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
        elif offer_class in offer_flag_on:
            profile["_offer_flag"] = offer_class

        # --- Skip YouTube if no email (saves quota) ---
        if skip_youtube_if_no_email:
            email = profile.get("email", "")
            if not email or email.lower() in ("not found", "none", ""):
                print(f"[{i}/{total}] SKIP_YOUTUBE (no email) — {person} / {company}", file=sys.stderr)
                results.append({
                    **profile,
                    "error": "Skipped YouTube: no email (youtube.skip_if_no_email=true)",
                    "yt_condition": "SKIP_NO_EMAIL",
                    "yt_channel_url": None,
                    "yt_channel_name": None,
                    "yt_last_upload": None,
                    "why_chosen": "",
                    "confidence": "",
                })
                continue

        print(f"[{i}/{total}] {person} / {company}", file=sys.stderr)

        # Cap companies per lead to save quota
        active_for_yt = profile.get("active_companies", [])
        if max_companies_per_lead:
            active_for_yt = active_for_yt[:max_companies_per_lead]

        try:
            yt = qualify_youtube(
                person_name=profile["full_name"],
                company_name=profile["company"],
                website_url=profile.get("website"),
                no_claude=no_claude,
                active_companies=active_for_yt,
                person_name_search=person_name_search,
            )
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
                    "yt_resolution_rule": "",
                    "yt_secondary_channels": "",
                    "why_chosen": "",
                    "confidence": "",
                    "_yt_videos": None,
                    "_yt_reasoning": "",
                    "_all_company_results": [],
                }
            )
            continue

        condition = yt.get("condition", "?")
        print(f"  → {condition}: {yt.get('reasoning', '')[:80]}", file=sys.stderr)

        rev_confidence, rev_score, rev_breakdown = estimate_revenue_confidence(
            profile,
            social_proof=profile.get("_social_proof"),
        )

        results.append(
            {
                **profile,
                "error":                 None,
                "yt_condition":          condition,
                "yt_channel_url":        yt.get("channel_url"),
                "yt_channel_name":       yt.get("channel_name"),
                "yt_last_upload":        yt.get("last_upload_date"),
                "yt_resolution_rule":    yt.get("resolution_rule", ""),
                "yt_secondary_channels": yt.get("secondary_channels", ""),
                "why_chosen":            "",
                "confidence":            "",
                "revenue_confidence":    rev_confidence,
                "revenue_score":         rev_score,
                "revenue_score_detail":  rev_breakdown,
                "_yt_videos":            yt.get("videos"),
                "_yt_reasoning":         yt.get("reasoning", ""),
                "_all_company_results":  yt.get("all_company_results", []),
            }
        )

    condition_counts: dict = {}
    for r in results:
        c = r.get("yt_condition", "")
        condition_counts[c] = condition_counts.get(c, 0) + 1

    # Derived counts for backward-compat summary keys
    prescreen_count   = filter_discard_counts.get("DISCARD_PRESCREEN", 0)
    size_discard_count = filter_discard_counts.get("DISCARD_SIZE", 0)
    total_filter_discards = sum(filter_discard_counts.values())

    if skipped:
        print(f"Skipped {skipped} already-qualified lead(s).", file=sys.stderr)
    for condition, count in sorted(filter_discard_counts.items()):
        print(f"Discarded {count} lead(s) — {condition}.", file=sys.stderr)
    if offer_discard_count:
        print(f"Discarded {offer_discard_count} lead(s) — website offer not high-ticket B2B.", file=sys.stderr)

    elapsed = round(time.time() - start_time, 1)

    summary = {
        "session_id":          generate_session_id(),
        "date":                datetime.utcnow().strftime("%Y-%m-%d"),
        "input_file":          input_file_name,
        "total_loaded":        total,
        "skipped_dedup":       skipped,
        "discarded_prescreen": prescreen_count,
        "discarded_size":      size_discard_count,
        "discarded_filters":   total_filter_discards,
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
        r.get("primary_score", ""),
        json.dumps(r.get("primary_score_detail", {})) if r.get("primary_score_detail") else "",
        r.get("yt_resolution_rule", ""),
        r.get("yt_secondary_channels", ""),
        r.get("revenue_confidence", ""),
        r.get("revenue_score", ""),
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

    _ERROR_CONDITIONS = {"ERROR"}
    _SKIP_CONDITIONS  = {"STAGE2_NEEDED", "SKIP_NO_EMAIL"}

    def _is_discard(cond: str) -> bool:
        return str(cond).startswith("DISCARD_")

    qualified = [r for r in results
                 if not _is_discard(r.get("yt_condition", ""))
                 and r.get("yt_condition") not in _ERROR_CONDITIONS | _SKIP_CONDITIONS]
    discards  = [r for r in results if _is_discard(r.get("yt_condition", ""))]
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
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Path to pipeline_config.json (default: pipeline_config.json in project root)",
    )

    args = parser.parse_args()
    sheet_id = _extract_sheet_id(args.output_sheet)

    # Load pipeline config (custom path or default location)
    import pipeline_filters as _pf
    if args.config:
        _pf._CONFIG_PATH = os.path.abspath(args.config)
    config = load_pipeline_config()
    print(f"Config loaded from: {_pf._CONFIG_PATH}", file=sys.stderr)

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
        config=config,
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
