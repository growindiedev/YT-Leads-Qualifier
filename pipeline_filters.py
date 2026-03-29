from __future__ import annotations

"""
pipeline_filters.py — Configurable filter pipeline for lead qualification.

Each filter has the signature:
    filter_name(profile: dict, config: dict) -> FilterResult

FilterResult.discard = True means the lead should be dropped.
FilterResult.condition is the yt_condition string written to the output (e.g. "DISCARD_SIZE").
FilterResult.reason is the human-readable explanation written to the error column.

To add a new filter:
  1. Write a function: def filter_X(profile, config) -> FilterResult
  2. Append it to FILTER_PIPELINE in the desired order
  3. Add its config section to pipeline_config.json with the same key name
"""

import json
import os
from typing import NamedTuple

from lead_utils import (
    parse_company_size as _parse_company_size,
    parse_tenure_months as _parse_tenure_months,
    parse_mismatched_filters as _parse_mismatched_filters,
    parse_revenue_range as _parse_revenue_range,
)


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

class FilterResult(NamedTuple):
    discard: bool
    condition: str   # e.g. "DISCARD_SIZE" — written to yt_condition on discard
    reason: str      # written to error column on discard


_PASS = FilterResult(discard=False, condition="", reason="")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_config.json")

_DEFAULTS: dict = {
    "input": {
        "column_map": {},
        "multi_company_suffixes": ["", " (2)", " (3)", " (4)"],
        "source_type": "sales_navigator",
    },
    "size_gate":            {"enabled": True,  "min_employees": 1, "max_employees": 50},
    "prescreen":            {"enabled": True,  "rules": {
                                "primary_employee_count_mismatch":       True,
                                "all_companies_employee_count_mismatch": True,
                                "matching_filters_false":                True,
                            }},
    "title_filter":         {"enabled": False, "require_any": [], "exclude_any": []},
    "location_filter":      {"enabled": False, "mode": "include", "values": []},
    "industry_filter":      {"enabled": False, "mode": "exclude", "values": []},
    "keyword_filter":       {"enabled": False, "fields": ["company_description", "company_specialities"],
                             "require_any": [], "exclude_any": []},
    "revenue_filter":       {"enabled": False, "min_usd": None, "max_usd": None},
    "tenure_filter":        {"enabled": False, "min_months_at_primary": 6},
    "primary_score_filter": {"enabled": False, "min_score": 10, "min_score_margin": 0},
    "multi_company_filter": {"enabled": False, "max_active_roles": None},
    "contact_filter":       {"require_email": False, "require_linkedin": False},
    "activity_filter":      {"enabled": False, "max_days_since_activity": 180},
    "offer_classifier":     {"enabled": True,
                             "discard_on": ["B2C", "LOW_TICKET", "NO_WEBSITE"],
                             "flag_only":  ["FETCH_FAILED", "UNCLEAR"]},
    "youtube":              {"skip_if_no_email": False, "max_companies_per_lead": None},
    "icp":                  {"target_niche": "",
                             "scoring_weights": {"title": 3, "tenure": 2, "size": 2, "niche": 1}},
    "output":               {"leads_tab_prefix": "Leads", "write_discards": True,
                             "write_errors": True, "write_sessions": True},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively. override wins on scalar conflicts."""
    result = dict(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # skip comment keys
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_pipeline_config() -> dict:
    """
    Load pipeline_config.json and deep-merge with defaults.
    Falls back to defaults silently if the file is missing.
    """
    if not os.path.exists(_CONFIG_PATH):
        return dict(_DEFAULTS)
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            user_config = json.load(f)
        return _deep_merge(_DEFAULTS, user_config)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: Could not load pipeline_config.json ({exc}). Using defaults.",
              flush=True)
        return dict(_DEFAULTS)


# ---------------------------------------------------------------------------
# Column remapping
# ---------------------------------------------------------------------------

def remap_row(row: dict, column_map: dict) -> dict:
    """
    Apply a column map to a row, adding standard-named keys derived from
    user-defined CSV column names.

    column_map: { standard_column_name: user_csv_column_name }

    Non-destructive — original keys are preserved alongside any aliases.
    No-op when column_map is empty or when standard key already exists.
    """
    if not column_map:
        return row
    remapped = dict(row)
    for standard_key, user_key in column_map.items():
        if user_key in row and standard_key not in remapped:
            remapped[standard_key] = row[user_key]
    return remapped




# ---------------------------------------------------------------------------
# Filter functions
# ---------------------------------------------------------------------------

def filter_prescreen(profile: dict, config: dict) -> FilterResult:
    """
    Sales Navigator mismatched-filter rules.
    Only active when source_type is 'sales_navigator'.
    """
    source_type = config.get("input", {}).get("source_type", "sales_navigator")
    if source_type != "sales_navigator":
        return _PASS

    prescreen_cfg = config.get("prescreen", {})
    if not prescreen_cfg.get("enabled", True):
        return _PASS

    rules = prescreen_cfg.get("rules", {})
    mismatch = _parse_mismatched_filters(profile.get("_mismatched_filters", ""))

    if mismatch and rules.get("primary_employee_count_mismatch", True):
        if "employee count" in mismatch.get("exp_1", []):
            return FilterResult(True, "DISCARD_PRESCREEN",
                                "Sales Nav: primary company employee count mismatch")

    if mismatch and rules.get("all_companies_employee_count_mismatch", True):
        if profile.get("multi_company_flag"):
            n = len(profile.get("active_companies", []))
            active_keys = [f"exp_{i+1}" for i in range(n)]
            all_mismatch = all(
                "employee count" in mismatch.get(k, [])
                for k in active_keys if k in mismatch
            )
            if len(active_keys) > 1 and all_mismatch:
                return FilterResult(True, "DISCARD_PRESCREEN",
                                    "Sales Nav: all active companies employee count mismatch")

    if rules.get("matching_filters_false", True):
        if profile.get("_matching_filters", "").strip().lower() == "false":
            return FilterResult(True, "DISCARD_PRESCREEN",
                                "Sales Nav: lead does not match search filters")

    return _PASS


def filter_size(profile: dict, config: dict) -> FilterResult:
    """Min/max employee count gate."""
    size_cfg = config.get("size_gate", {})
    if not size_cfg.get("enabled", True):
        return _PASS

    size_int = _parse_company_size(profile.get("company_size", ""))
    if size_int is None:
        return _PASS

    max_emp = size_cfg.get("max_employees", 50)
    min_emp = size_cfg.get("min_employees", 1)

    if max_emp is not None and size_int > max_emp:
        return FilterResult(True, "DISCARD_SIZE",
                            f"Company too large: {profile['company_size']} (max {max_emp})")
    if min_emp is not None and size_int < min_emp:
        return FilterResult(True, "DISCARD_SIZE",
                            f"Company too small: {profile['company_size']} (min {min_emp})")

    return _PASS


def filter_title(profile: dict, config: dict) -> FilterResult:
    """Require or exclude job title keywords (case-insensitive substring match)."""
    title_cfg = config.get("title_filter", {})
    if not title_cfg.get("enabled", False):
        return _PASS

    title = profile.get("job_title", "").lower()
    exclude = [k.lower() for k in title_cfg.get("exclude_any", [])]
    require = [k.lower() for k in title_cfg.get("require_any", [])]

    for kw in exclude:
        if kw in title:
            return FilterResult(True, "DISCARD_TITLE",
                                f"Title excluded: '{kw}' matched in '{profile.get('job_title', '')}'")

    if require and not any(kw in title for kw in require):
        return FilterResult(True, "DISCARD_TITLE",
                            f"Title not in required list: '{profile.get('job_title', '')}'")

    return _PASS


def filter_location(profile: dict, config: dict) -> FilterResult:
    """Include or exclude leads by location substring (case-insensitive)."""
    loc_cfg = config.get("location_filter", {})
    if not loc_cfg.get("enabled", False):
        return _PASS

    location = profile.get("location", "").lower()
    mode = loc_cfg.get("mode", "include")
    values = [v.lower() for v in loc_cfg.get("values", [])]

    if not values:
        return _PASS

    if mode == "include":
        if not any(v in location for v in values):
            return FilterResult(True, "DISCARD_LOCATION",
                                f"Location not in include list: '{profile.get('location', '')}'")
    elif mode == "exclude":
        for v in values:
            if v in location:
                return FilterResult(True, "DISCARD_LOCATION",
                                    f"Location excluded: '{v}' in '{profile.get('location', '')}'")

    return _PASS


def filter_industry(profile: dict, config: dict) -> FilterResult:
    """Include or exclude leads by LinkedIn company industry (case-insensitive substring)."""
    ind_cfg = config.get("industry_filter", {})
    if not ind_cfg.get("enabled", False):
        return _PASS

    industry = profile.get("_industry", "").lower()
    mode = ind_cfg.get("mode", "exclude")
    values = [v.lower() for v in ind_cfg.get("values", [])]

    if not values:
        return _PASS

    if mode == "include":
        if not any(v in industry for v in values):
            return FilterResult(True, "DISCARD_INDUSTRY",
                                f"Industry not in include list: '{profile.get('_industry', '')}'")
    elif mode == "exclude":
        for v in values:
            if v in industry:
                return FilterResult(True, "DISCARD_INDUSTRY",
                                    f"Industry excluded: '{v}'")

    return _PASS


def filter_keywords(profile: dict, config: dict) -> FilterResult:
    """Require or exclude keywords found in company profile text fields."""
    kw_cfg = config.get("keyword_filter", {})
    if not kw_cfg.get("enabled", False):
        return _PASS

    field_key_map = {
        "company_description": "_company_description",
        "company_specialities": "_specialities",
        "summary":   "_summary",
        "headline":  "_headline",
    }
    fields = kw_cfg.get("fields", ["company_description", "company_specialities"])
    text = " ".join(
        profile.get(field_key_map.get(f, f), "") or ""
        for f in fields
    ).lower()

    for kw in [k.lower() for k in kw_cfg.get("exclude_any", [])]:
        if kw in text:
            return FilterResult(True, "DISCARD_KEYWORDS",
                                f"Excluded keyword found: '{kw}'")

    require = [k.lower() for k in kw_cfg.get("require_any", [])]
    if require and not any(kw in text for kw in require):
        return FilterResult(True, "DISCARD_KEYWORDS",
                            "No required keywords found in company profile")

    return _PASS


def filter_revenue(profile: dict, config: dict) -> FilterResult:
    """Filter by LinkedIn revenue range. Unknown revenue passes."""
    rev_cfg = config.get("revenue_filter", {})
    if not rev_cfg.get("enabled", False):
        return _PASS

    revenue_str = profile.get("company_revenue", "")
    if not revenue_str:
        return _PASS

    min_usd = rev_cfg.get("min_usd")
    max_usd = rev_cfg.get("max_usd")
    rev_lower, rev_upper = _parse_revenue_range(revenue_str)

    if min_usd is not None and rev_upper is not None and rev_upper < min_usd:
        return FilterResult(True, "DISCARD_REVENUE",
                            f"Revenue too low: {revenue_str} (min ${min_usd:,})")
    if max_usd is not None and rev_lower is not None and rev_lower > max_usd:
        return FilterResult(True, "DISCARD_REVENUE",
                            f"Revenue too high: {revenue_str} (max ${max_usd:,})")

    return _PASS


def filter_tenure(profile: dict, config: dict) -> FilterResult:
    """Require a minimum number of months at the primary company."""
    ten_cfg = config.get("tenure_filter", {})
    if not ten_cfg.get("enabled", False):
        return _PASS

    active = profile.get("active_companies", [])
    if not active:
        return _PASS

    min_months = ten_cfg.get("min_months_at_primary", 6)
    months = _parse_tenure_months(active[0].get("job_started", ""))
    if months < min_months:
        return FilterResult(True, "DISCARD_TENURE",
                            f"Tenure too short: {months} months at primary (min {min_months})")

    return _PASS


def filter_primary_score(profile: dict, config: dict) -> FilterResult:
    """Require a minimum multi-company primary score (and optional margin over second-best)."""
    score_cfg = config.get("primary_score_filter", {})
    if not score_cfg.get("enabled", False):
        return _PASS

    score = profile.get("primary_score") or 0
    min_score = score_cfg.get("min_score", 0)
    min_margin = score_cfg.get("min_score_margin", 0)

    if score < min_score:
        return FilterResult(True, "DISCARD_SCORE",
                            f"Primary score too low: {score} (min {min_score})")

    if min_margin:
        active = profile.get("active_companies", [])
        if len(active) >= 2:
            second_score = active[1].get("score", 0) or 0
            if (score - second_score) < min_margin:
                return FilterResult(True, "DISCARD_SCORE",
                                    f"Primary score margin too small: {score} vs {second_score} "
                                    f"(min margin {min_margin})")

    return _PASS


def filter_multi_company(profile: dict, config: dict) -> FilterResult:
    """Discard leads with more active roles than the configured maximum."""
    mc_cfg = config.get("multi_company_filter", {})
    if not mc_cfg.get("enabled", False):
        return _PASS

    max_roles = mc_cfg.get("max_active_roles")
    if max_roles is None:
        return _PASS

    active = profile.get("active_companies", [])
    if len(active) > max_roles:
        return FilterResult(True, "DISCARD_MULTI_COMPANY",
                            f"Too many active roles: {len(active)} (max {max_roles})")

    return _PASS


def filter_contact(profile: dict, config: dict) -> FilterResult:
    """Hard requirements on contact information."""
    contact_cfg = config.get("contact_filter", {})

    if contact_cfg.get("require_email", False):
        email = profile.get("email", "")
        if not email or email.lower() in ("not found", "none", ""):
            return FilterResult(True, "DISCARD_CONTACT", "No email address found")

    if contact_cfg.get("require_linkedin", False):
        if not profile.get("personal_linkedin_url", ""):
            return FilterResult(True, "DISCARD_CONTACT", "No LinkedIn URL found")

    return _PASS


def filter_activity(profile: dict, config: dict) -> FilterResult:
    """Discard leads with no LinkedIn activity within the configured window."""
    act_cfg = config.get("activity_filter", {})
    if not act_cfg.get("enabled", False):
        return _PASS

    last_activity = profile.get("last_activity", "")
    if not last_activity:
        return _PASS  # unknown → pass

    max_days = act_cfg.get("max_days_since_activity", 180)
    activity_date = None
    for fmt in ("%Y-%m-%d", "%b %Y", "%B %Y", "%m/%Y", "%Y"):
        try:
            activity_date = datetime.strptime(last_activity.strip(), fmt)
            break
        except ValueError:
            continue

    if activity_date:
        days_since = (datetime.now() - activity_date).days
        if days_since > max_days:
            return FilterResult(True, "DISCARD_ACTIVITY",
                                f"No LinkedIn activity in {days_since} days (max {max_days})")

    return _PASS


# ---------------------------------------------------------------------------
# Filter pipeline — ordered cheapest to most expensive
# ---------------------------------------------------------------------------

FILTER_PIPELINE = [
    filter_prescreen,
    filter_size,
    filter_title,
    filter_location,
    filter_industry,
    filter_keywords,
    filter_revenue,
    filter_tenure,
    filter_primary_score,
    filter_multi_company,
    filter_contact,
    filter_activity,
]


def apply_filters(profile: dict, config: dict) -> FilterResult:
    """
    Run all filters in FILTER_PIPELINE order.
    Returns on the first discard — remaining filters are not evaluated.
    Returns _PASS (discard=False) when all filters pass.
    """
    for filter_fn in FILTER_PIPELINE:
        result = filter_fn(profile, config)
        if result.discard:
            return result
    return _PASS
