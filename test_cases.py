"""
Test runner for youtube_qualifier.py and batch_qualify.py utility functions.

Usage:
    python test_cases.py               # run everything
    python test_cases.py --unit-only   # only unit tests (no API, no credentials needed)

Notes on condition coverage:
    A  — Deterministic: no channel found. Stable test.
    B  — Deterministic: last upload > 60 days ago. Stable while Brian Dean stays inactive.
    C  — Deterministic: recent upload but 60+ day gap between recent uploads.
         Hard to pin to a real person (channels drift B↔C as they post). Not included.
    D  — Stage 2 judgment required (raw podcast vs. educational longform).
         Returns STAGE2_NEEDED without Claude. Tested as such.
    E  — Deterministic: ALL recent videos ≤60s. No reliable public B2B example found
         that posts exclusively Shorts. Not included.
    F  — Stage 2 judgment required (off-topic vs. on-topic content).
         Returns STAGE2_NEEDED without Claude. Tested as such.
    FAIL — Stage 2 judgment required. Tested as STAGE2_NEEDED.
"""

import sys
from youtube_qualifier import qualify_youtube
from lead_utils import (
    parse_company_size, parse_mismatched_filters,
    parse_tenure_months, score_job_title,
)
from batch_qualify import rank_active_companies
from youtube_qualifier import (
    _extract_youtube_channel_links, _websites_match, resolve_company_youtube_results,
)

# ---------------------------------------------------------------------------
# API test cases  (hit YouTube API, require YOUTUBE_API_KEY)
# ---------------------------------------------------------------------------

API_TEST_CASES = [
    {
        "label": "Condition A — No channel (unknown person)",
        "person": "Zzq Randomperson Xkj",
        "company": "Unknown Firm LLC",
        "website": None,
        "expected": "A",
    },
    {
        "label": "Condition B — Dead channel (Brian Dean / Backlinko)",
        "person": "Brian Dean",
        "company": "Backlinko",
        "website": None,
        "expected": "B",
        "note": "Confirmed inactive 60+ days. Will break if Brian Dean resumes posting.",
    },
    {
        "label": "STAGE2_NEEDED — Active polished channel (Alex Hormozi)",
        "person": "Alex Hormozi",
        "company": "Acquisition.com",
        "website": None,
        "expected": "STAGE2_NEEDED",
        "note": "Would be FAIL with Claude. Stage 2 required to distinguish D/F/FAIL.",
    },
    {
        "label": "STAGE2_NEEDED — High volume active creator (Gary Vaynerchuk)",
        "person": "Gary Vaynerchuk",
        "company": "VaynerMedia",
        "website": None,
        "expected": "STAGE2_NEEDED",
        "note": "Would be FAIL with Claude.",
    },
    {
        "label": "STAGE2_NEEDED — B2B podcast channel (Lenny Rachitsky)",
        "person": "Lenny Rachitsky",
        "company": "Lenny's Podcast",
        "website": None,
        "expected": "STAGE2_NEEDED",
        "note": "Would be D (raw podcast) with Claude.",
    },
]

# ---------------------------------------------------------------------------
# Unit tests  (no API, no credentials, always fast)
# ---------------------------------------------------------------------------

UNIT_TESTS = [
    # --- parse_company_size ---
    {"fn": "parse_company_size", "input": "11-50",        "expected": 50},
    {"fn": "parse_company_size", "input": "51-200",       "expected": 200},
    {"fn": "parse_company_size", "input": "2-10",         "expected": 10},
    {"fn": "parse_company_size", "input": "47",           "expected": 47},
    {"fn": "parse_company_size", "input": "myself only",  "expected": 1},
    {"fn": "parse_company_size", "input": "",             "expected": None},
    {"fn": "parse_company_size", "input": "10,001+",      "expected": 10001},
    {"fn": "parse_company_size", "input": "1,001-5,000",  "expected": 5000},

    # --- parse_mismatched_filters ---
    {
        "fn": "parse_mismatched_filters",
        "input": "exp_1: job, industry | exp_2: employee count",
        "expected": {"exp_1": ["job", "industry"], "exp_2": ["employee count"]},
    },
    {
        "fn": "parse_mismatched_filters",
        "input": "exp_1: employee count",
        "expected": {"exp_1": ["employee count"]},
    },
    {
        "fn": "parse_mismatched_filters",
        "input": "exp_1: job, industry, employee count",
        "expected": {"exp_1": ["job", "industry", "employee count"]},
    },
    {
        "fn": "parse_mismatched_filters",
        "input": "",
        "expected": {},
    },
]

_FN_MAP = {
    "parse_company_size":      parse_company_size,
    "parse_mismatched_filters": parse_mismatched_filters,
}


def run_7a_tests() -> tuple:
    passed = failed = 0
    print("\n=== 7A Tests: Multi-Company Scoring ===")

    def check(label, got, expected):
        nonlocal passed, failed
        if got == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")
            print(f"        expected: {expected!r}")
            print(f"        got:      {got!r}")

    # parse_tenure_months
    assert_gt = [
        ("parse_tenure_months('01/2021') > 30", parse_tenure_months("01/2021") > 30, True),
        ("parse_tenure_months('2026') < 6",     parse_tenure_months("2026") < 6,    True),
        ("parse_tenure_months('')",             parse_tenure_months("") == 0,       True),
        ("parse_tenure_months('baddata')",      parse_tenure_months("baddata") == 0, True),
    ]
    for label, got, expected in assert_gt:
        check(label, got, expected)

    # score_job_title
    title_cases = [
        ("Founder & CEO",          3),
        ("Co-Founder",             3),
        ("Board Advisor",          0),
        ("Marketing Specialist",  -1),
        ("Head of Sales",          1),
    ]
    for title, expected in title_cases:
        check(f"score_job_title({title!r})", score_job_title(title), expected)

    # rank_active_companies ordering
    companies = [
        {
            "job_title": "Board Advisor", "job_started": "01/2023",
            "company": "Big Corp", "company_size_count": "500",
            "company_size_range": "",
            "company_website": "", "company_description": "",
            "company_specialities": "", "company_industry": "",
        },
        {
            "job_title": "Founder", "job_started": "01/2020",
            "company": "My Agency", "company_size_count": "5",
            "company_size_range": "",
            "company_website": "myagency.com", "company_description": "B2B consulting",
            "company_specialities": "", "company_industry": "",
        },
    ]
    ranked = rank_active_companies(companies, target_niche="B2B consulting")
    check("rank_active_companies: My Agency first", ranked[0]["company"], "My Agency")

    if failed == 0:
        print(f"  All {passed} 7A tests passed.")
    return passed, failed


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_unit_tests() -> tuple:
    passed = failed = 0
    print("\n=== Unit Tests ===")
    for tc in UNIT_TESTS:
        fn_name = tc["fn"]
        inp     = tc["input"]
        expected = tc["expected"]
        fn = _FN_MAP[fn_name]
        try:
            result = fn(inp)
            ok = result == expected
        except Exception as e:
            print(f"  EXCEPTION {fn_name}({inp!r}): {e}")
            failed += 1
            continue

        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  {status}  {fn_name}({inp!r})")
            print(f"         expected: {expected!r}")
            print(f"         got:      {result!r}")

    if failed == 0:
        print(f"  All {passed} unit tests passed.")
    return passed, failed


def run_api_tests() -> tuple:
    passed = failed = 0
    quota_used = 0

    print("\n=== API Tests ===")
    for tc in API_TEST_CASES:
        print(f"\n{'─'*60}")
        print(f"TEST: {tc['label']}")
        if tc.get("note"):
            print(f"NOTE: {tc['note']}")
        print(f"Input: {tc['person']} / {tc['company']}")

        try:
            result = qualify_youtube(
                tc["person"], tc["company"], tc.get("website"), no_claude=True
            )
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            failed += 1
            continue

        # Quota estimate: 100 units for A (search only), 300 for anything else
        condition = result["condition"]
        quota_used += 100 if condition == "A" else 300

        expected = tc["expected"]
        ok = condition == expected
        status = "PASS" if ok else "MISMATCH"

        print(f"  Condition:   {condition} (expected {expected}) → {status}")
        print(f"  Channel:     {result['channel_name']} — {result['channel_url']}")
        print(f"  Last Upload: {result['last_upload_date']}")
        print(f"  Stage:       {result['stage']}")

        if ok:
            passed += 1
        else:
            failed += 1

    return passed, failed, quota_used


def run_7b_tests() -> tuple:
    passed = failed = 0
    print("\n=== 7B Tests: Per-Company YouTube Resolution ===")

    def check(label, got, expected):
        nonlocal passed, failed
        if got == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL  {label}")
            print(f"        expected: {expected!r}")
            print(f"        got:      {got!r}")

    # _websites_match
    check("acme.com/about vs acme.com",     _websites_match("https://www.acme.com/about", "http://acme.com"),   True)
    check("acme.com vs betacorp.com",        _websites_match("https://acme.com", "https://betacorp.com"),        False)
    check("bare domain vs www domain",       _websites_match("acme.com", "https://www.acme.com"),                True)
    check("empty vs acme.com",               _websites_match("", "acme.com"),                                    False)

    # _extract_youtube_channel_links
    from bs4 import BeautifulSoup
    html = """
<html><body>
  <a href="https://www.youtube.com/channel/UCabc123def456">YouTube</a>
  <a href="https://www.youtube.com/@johnsmith">Handle</a>
  <a href="https://www.youtube.com/watch?v=xyz">Video</a>
  <a href="https://youtu.be/abc">Short link</a>
</body></html>
"""
    soup = BeautifulSoup(html, "html.parser")
    links = _extract_youtube_channel_links(soup)
    check("extract channel links: count=2", len(links), 2)
    check("no watch?v= links", all("watch?v=" not in l for l in links), True)
    check("no youtu.be links", all("youtu.be" not in l for l in links), True)

    # resolve_company_youtube_results — FAIL primary
    r_primary_fail = [
        {"condition": "FAIL", "company_name": "Acme", "company_rank": 0,
         "channel_url": "...", "reasoning": "Active channel", "discovery_source": "website"},
        {"condition": "C",    "company_name": "Beta", "company_rank": 1,
         "channel_url": None,  "reasoning": "Inconsistent", "discovery_source": "none"},
    ]
    resolved = resolve_company_youtube_results(r_primary_fail)
    check("FAIL primary → condition=FAIL",            resolved["condition"],       "FAIL")
    check("FAIL primary → rule=fail_primary",         resolved["resolution_rule"], "fail_primary")

    # resolve_company_youtube_results — secondary FAIL via website
    r_secondary_fail = [
        {"condition": "C",    "company_name": "Acme", "company_rank": 0,
         "channel_url": "...", "reasoning": "Inconsistent", "discovery_source": "website"},
        {"condition": "FAIL", "company_name": "Beta", "company_rank": 1,
         "channel_url": "...", "reasoning": "Active", "discovery_source": "website"},
    ]
    resolved = resolve_company_youtube_results(r_secondary_fail)
    check("secondary FAIL website → condition=REVIEW_FAIL",            resolved["condition"],       "REVIEW_FAIL")
    check("secondary FAIL website → rule=fail_secondary_website",      resolved["resolution_rule"], "fail_secondary_website")

    # resolve_company_youtube_results — all pass, uses primary
    r_all_pass = [
        {"condition": "B", "company_name": "Acme", "company_rank": 0,
         "channel_url": "...", "reasoning": "Dead channel", "discovery_source": "website"},
        {"condition": "A", "company_name": "Beta", "company_rank": 1,
         "channel_url": None,  "reasoning": "No channel",   "discovery_source": "none"},
    ]
    resolved = resolve_company_youtube_results(r_all_pass)
    check("all pass → condition=B (primary)",          resolved["condition"],       "B")
    check("all pass → rule=all_pass_use_primary",      resolved["resolution_rule"], "all_pass_use_primary")

    if failed == 0:
        print(f"  All {passed} 7B tests passed.")
    return passed, failed


def print_summary(api_passed, api_failed, unit_passed, unit_failed, s7a_passed, s7a_failed, s7b_passed, s7b_failed, quota_used, run_api):
    print(f"\n{'═'*60}")
    print("TEST SUMMARY")
    print(f"{'─'*36}")
    if run_api:
        print(f"API tests:   {api_passed} passed, {api_failed} failed")
    print(f"Unit tests:  {unit_passed} passed, {unit_failed} failed")
    print(f"7A tests:    {s7a_passed} passed, {s7a_failed} failed")
    print(f"7B tests:    {s7b_passed} passed, {s7b_failed} failed")
    if run_api:
        print(f"\nEstimated quota used: ~{quota_used} units")
    print(f"{'═'*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unit_only = "--unit-only" in sys.argv

    unit_passed, unit_failed = run_unit_tests()
    s7a_passed, s7a_failed   = run_7a_tests()
    s7b_passed, s7b_failed   = run_7b_tests()

    api_passed = api_failed = quota_used = 0
    if not unit_only:
        api_passed, api_failed, quota_used = run_api_tests()

    print_summary(api_passed, api_failed, unit_passed, unit_failed, s7a_passed, s7a_failed, s7b_passed, s7b_failed, quota_used, not unit_only)

    total_failed = unit_failed + s7a_failed + s7b_failed + api_failed
    sys.exit(0 if total_failed == 0 else 1)
