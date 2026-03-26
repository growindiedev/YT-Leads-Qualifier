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
from batch_qualify import parse_company_size, parse_mismatched_filters

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


def print_summary(api_passed, api_failed, unit_passed, unit_failed, quota_used, run_api):
    print(f"\n{'═'*60}")
    print("TEST SUMMARY")
    print(f"{'─'*36}")
    if run_api:
        print(f"API tests:   {api_passed} passed, {api_failed} failed")
    print(f"Unit tests:  {unit_passed} passed, {unit_failed} failed")
    if run_api:
        print(f"\nEstimated quota used: ~{quota_used} units")
    print(f"{'═'*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unit_only = "--unit-only" in sys.argv

    unit_passed, unit_failed = run_unit_tests()

    api_passed = api_failed = quota_used = 0
    if not unit_only:
        api_passed, api_failed, quota_used = run_api_tests()

    print_summary(api_passed, api_failed, unit_passed, unit_failed, quota_used, not unit_only)

    total_failed = unit_failed + api_failed
    sys.exit(0 if total_failed == 0 else 1)
