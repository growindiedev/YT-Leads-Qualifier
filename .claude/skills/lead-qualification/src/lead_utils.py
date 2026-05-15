from __future__ import annotations

"""
lead_utils.py — Shared parsing and scoring utilities.

Extracted here to avoid circular imports between batch_qualify.py and
pipeline_filters.py. Both modules import from this one; neither imports
from the other for utilities.

Single Responsibility: parse and score primitives only.
No I/O, no HTTP, no pipeline logic.
"""

import re
from datetime import datetime


# ---------------------------------------------------------------------------
# Company size
# ---------------------------------------------------------------------------

def parse_company_size(size_string: str) -> int | None:
    """
    Parse a LinkedIn employee count string to an integer upper bound.

    Handles:
      "11-50"           → 50
      "47"              → 47
      "myself only"     → 1
      "self-employed"   → 1
      ""                → None
    """
    if not size_string:
        return None
    s = size_string.lower().strip()
    if any(word in s for word in ["myself", "self-employed", "freelance"]):
        return 1
    s_clean = s.replace(",", "").replace("+", "")
    numbers = re.findall(r"\d+", s_clean)
    if not numbers:
        return None
    nums = [int(n) for n in numbers]
    return max(nums) if len(nums) >= 2 else nums[0]


# ---------------------------------------------------------------------------
# Tenure
# ---------------------------------------------------------------------------

def parse_tenure_months(started_on: str) -> int:
    """
    Parse a job start date string and return months in role as of today.

    Accepted formats: "MM/YYYY", "YYYY-MM", "YYYY"
    Returns 0 if unparseable. Caps at 120 months (10 years).
    """
    if not started_on:
        return 0
    now = datetime.now()
    s = started_on.strip()
    try:
        if re.match(r"^\d{2}/\d{4}$", s):
            dt = datetime.strptime(s, "%m/%Y")
        elif re.match(r"^\d{4}-\d{2}$", s):
            dt = datetime.strptime(s, "%Y-%m")
        elif re.match(r"^\d{4}$", s):
            dt = datetime(int(s), 1, 1)
        else:
            return 0
        months = (now.year - dt.year) * 12 + (now.month - dt.month)
        return min(max(months, 0), 120)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Mismatched filters
# ---------------------------------------------------------------------------

def parse_mismatched_filters(mismatched: str) -> dict:
    """
    Parse Sales Navigator mismatched filters string into a dict.

    Input:  "exp_1: employee count, industry | exp_2: job"
    Output: {"exp_1": ["employee count", "industry"], "exp_2": ["job"]}
    """
    result: dict = {}
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


# ---------------------------------------------------------------------------
# Job title scoring
# ---------------------------------------------------------------------------

_FOUNDER_TITLES  = frozenset(["founder", "co-founder", "cofounder", "ceo",
                               "chief executive", "owner", "president", "principal"])
_DIRECTOR_TITLES = frozenset(["managing director", "managing partner", "partner",
                               "director"])
_SENIOR_TITLES   = frozenset(["vp ", "vice president", "head of", "chief ",
                               "cro", "cmo", "cto", "coo", "cpo", "cso"])
_ADVISOR_TITLES  = frozenset(["advisor", "adviser", "board member", "board of",
                               "investor", "fellow", "emeritus", "volunteer",
                               "ambassador", "mentor"])
_EMPLOYEE_TITLES = frozenset(["manager", "specialist", "coordinator", "associate",
                               "analyst", "assistant", "representative", "agent"])


def score_job_title(title: str) -> int:
    """
    Score a job title by operational control it implies.

    3  — Founder / CEO / Owner / President / Principal
    2  — Managing Director / Managing Partner / Partner / Director
    1  — VP / Head of / C-suite (CRO, CMO, CTO …)
    0  — Advisor / Board Member / Investor / Volunteer
   -1  — Employee titles (Manager, Specialist, Coordinator …)
    """
    if not title:
        return 0
    t = title.lower()
    if any(x in t for x in _FOUNDER_TITLES):
        return 3
    if any(x in t for x in _DIRECTOR_TITLES):
        return 2
    if any(x in t for x in _SENIOR_TITLES):
        return 1
    if any(x in t for x in _ADVISOR_TITLES):
        return 0
    if any(x in t for x in _EMPLOYEE_TITLES):
        return -1
    return 1  # unknown title — assume some seniority


# ---------------------------------------------------------------------------
# Company size scoring
# ---------------------------------------------------------------------------

def score_company_size(size_string: str) -> int:
    """
    Score company size as a signal of a founder-led small business.

    3  — 1–5 employees
    2  — 6–15 employees
    1  — 16–50 employees
    0  — 51–200 employees
   -1  — 201–500 employees
   -2  — 500+ employees
    """
    n = parse_company_size(size_string)
    if n is None:
        return 0
    if n <= 5:
        return 3
    if n <= 15:
        return 2
    if n <= 50:
        return 1
    if n <= 200:
        return 0
    if n <= 500:
        return -1
    return -2


# ---------------------------------------------------------------------------
# Niche fit scoring
# ---------------------------------------------------------------------------

_NICHE_STOP_WORDS = frozenset([
    "the", "and", "for", "inc", "llc", "ltd", "corp", "with",
    "from", "that", "this", "your", "our", "its", "has", "are",
    "was", "have", "been", "not", "but", "all", "can",
])


def score_niche_fit(company_dict: dict, target_niche: str) -> int:
    """
    Score keyword overlap between a company profile and the target niche.
    Returns 0–5.
    """
    if not target_niche:
        return 0

    niche_tokens = [
        w for w in target_niche.lower().split()
        if len(w) > 3 and w not in _NICHE_STOP_WORDS
    ]
    if not niche_tokens:
        return 0

    searchable = " ".join([
        company_dict.get("company_description", ""),
        company_dict.get("company_specialities", ""),
        company_dict.get("company_industry", ""),
        company_dict.get("job_title", ""),
        company_dict.get("company", ""),
    ]).lower()

    matches = sum(1 for t in niche_tokens if t in searchable)
    return min(matches, 5)


# ---------------------------------------------------------------------------
# Revenue parsing (used by pipeline_filters revenue_filter)
# ---------------------------------------------------------------------------

_REVENUE_MULTIPLIERS = {"T": 1_000, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_revenue_bound(value_str: str) -> int | None:
    """Parse a single revenue bound string like "1M" or "500T" to an integer USD value."""
    s = value_str.strip().upper().replace(",", "")
    for suffix, mult in _REVENUE_MULTIPLIERS.items():
        if s.endswith(suffix):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                return None
    try:
        return int(s)
    except ValueError:
        return None


def parse_revenue_range(revenue_str: str) -> tuple[int | None, int | None]:
    """
    Parse a LinkedIn revenue range string like "1M USD - 2.5M USD"
    into (lower_bound, upper_bound) as integers.
    """
    cleaned = revenue_str.replace(" USD", "").strip()
    parts = cleaned.split(" - ")
    if len(parts) == 2:
        return parse_revenue_bound(parts[0]), parse_revenue_bound(parts[1])
    single = parse_revenue_bound(cleaned)
    return single, single
