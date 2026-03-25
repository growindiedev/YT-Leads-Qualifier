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
from datetime import datetime

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

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
    "YouTube Channel URL",
    "YouTube Status",
    "Last LinkedIn Activity",
    "Why Chosen",
    "Confidence",
    "Error",
]


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
    """
    session = _get_session()

    # Get list of all sheet tabs
    resp = session.get(f"{SHEETS_BASE}/{sheet_id}?fields=sheets.properties")
    if not resp.ok:
        print(f"  Warning: could not read output sheet tabs ({resp.status_code}). Dedup skipped.", file=sys.stderr)
        return set()

    tabs = [s["properties"]["title"] for s in resp.json().get("sheets", [])]
    already_done = set()

    for tab in tabs:
        try:
            tab_resp = session.get(
                f"{SHEETS_BASE}/{sheet_id}/values/{tab}!A1:ZZ"
            )
            if not tab_resp.ok:
                continue
            rows = tab_resp.json().get("values", [])
            if len(rows) < 2:
                continue
            header = rows[0]
            # Only parse tabs that look like our output (have "Full Name" column)
            if "Full Name" not in header:
                continue
            fn_idx  = header.index("Full Name")
            co_idx  = header.index("Company") if "Company" in header else None
            li_idx  = header.index("Personal LinkedIn URL") if "Personal LinkedIn URL" in header else None

            for row in rows[1:]:
                def cell(i):
                    return row[i].strip() if i is not None and i < len(row) else ""

                linkedin = cell(li_idx).lower().rstrip("/")
                if linkedin:
                    already_done.add(linkedin)
                name    = cell(fn_idx).lower()
                company = cell(co_idx).lower() if co_idx is not None else ""
                if name:
                    already_done.add((name, company))
        except Exception as e:
            print(f"  Warning: could not read tab '{tab}': {e}", file=sys.stderr)

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

def _normalize_row(row: dict) -> dict:
    """
    Extract all relevant fields from a row.
    Returns a dict with standardised keys ready for output and qualification.
    """

    def get(*keys):
        for k in keys:
            v = row.get(k, "").strip()
            if v:
                return v
        return ""

    first = get("first name", "First Name", "first_name", "FirstName")
    last = get("last name", "Last Name", "last_name", "LastName")
    full_name = f"{first} {last}".strip() or get("name", "Name", "full name", "Full Name")

    company = get(
        "company", "Company", "company name", "Company Name",
        "company_name", "organization", "Organization",
    )
    website = get(
        "corporate website", "Corporate Website", "website", "Website",
        "website_url", "Website URL", "company website", "Company Website",
    ) or None

    email = get("email", "Email", "email address", "Email Address") or "Not Found"

    phone = get("phone", "Phone", "phone number", "Phone Number")
    other_contact = phone or "None"

    company_size = get(
        "linkedin employees", "LinkedIn Employees",
        "linkedin company employee count", "company size", "Company Size",
    )

    return {
        "full_name": full_name,
        "company": company,
        "website": website,
        "job_title": get("job title", "Job Title", "title", "Title"),
        "company_size": company_size,
        "company_linkedin_url": get(
            "corporate linkedin url", "Corporate LinkedIn URL",
            "company linkedin url", "Company LinkedIn URL",
        ),
        "personal_linkedin_url": get("linkedin url", "LinkedIn URL", "personal linkedin url"),
        "email": email,
        "other_contact": other_contact,
        # Profile text fields used by skill to generate Why Chosen + Confidence
        "_summary": get("summary", "Summary"),
        "_headline": get("headline", "Headline"),
        "_company_description": get("linkedin description", "LinkedIn Description"),
        "_specialities": get("linkedin specialities", "LinkedIn Specialities"),
        "_industry": get("linkedin industry", "LinkedIn Industry"),
        "_location": get("location", "Location"),
    }


# ---------------------------------------------------------------------------
# Core batch processor
# ---------------------------------------------------------------------------

def process_leads(rows: list, no_claude: bool, already_done: set = None) -> list:
    """
    Run YouTube qualification for every row.
    Returns a list of result dicts (one per input row).
    Errors are recorded in the result rather than raising.
    """
    results = []
    total = len(rows)

    skipped = 0
    for i, row in enumerate(rows, 1):
        profile = _normalize_row(row)
        person = profile["full_name"]
        company = profile["company"]
        website = profile["website"]

        if already_done and _is_duplicate(profile, already_done):
            print(f"[{i}/{total}] SKIP (already qualified) — {person} / {company}", file=sys.stderr)
            skipped += 1
            continue

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

    return results


# ---------------------------------------------------------------------------
# Google Sheets writer
# ---------------------------------------------------------------------------

def write_to_sheet(results: list, sheet_id: str, tab_name: str = None) -> str:
    """
    Write results to a new tab in the given Google Spreadsheet.
    Returns the spreadsheet URL.
    """
    session = _get_session()
    tab_name = tab_name or f"YT Results {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

    # Add a new sheet tab
    resp = session.post(
        f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
        json={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    )
    if not resp.ok and "already exists" not in resp.text:
        resp.raise_for_status()

    # Build value rows
    rows = [OUTPUT_HEADERS]
    for r in results:
        yt_status = r.get("yt_condition", "")
        rows.append(
            [
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
                r.get("confidence", ""),
                r.get("error", "") or "",
            ]
        )

    # URL-encode the tab name for the range parameter
    from urllib.parse import quote
    range_ref = f"'{tab_name}'!A1"
    resp = session.put(
        f"{SHEETS_BASE}/{sheet_id}/values/{quote(range_ref)}",
        params={"valueInputOption": "RAW"},
        json={"values": rows},
    )
    resp.raise_for_status()

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

    results = process_leads(rows, no_claude=args.no_claude, already_done=already_done)

    if not results:
        print("All leads already qualified. Nothing new to process.", file=sys.stderr)
        sys.exit(0)

    if args.no_claude:
        # Output JSON for the skill — it will handle Stage 2 and call --write-results
        print(json.dumps(results, default=str))
    else:
        # Full end-to-end: write directly to Google Sheets
        url = write_to_sheet(results, sheet_id, args.tab_name)
        errors = [r for r in results if r.get("error") or r.get("condition") == "ERROR"]
        print(f"\nDone. {len(results)} leads processed.", file=sys.stderr)
        if errors:
            print(f"Errors ({len(errors)}):", file=sys.stderr)
            for r in errors:
                print(f"  - {r['person']} / {r['company']}: {r['error']}", file=sys.stderr)
        print(f"Results: {url}")


if __name__ == "__main__":
    main()
