"""
Test runner for youtube_qualifier.py

Run: python test_cases.py
Each test prints the result and whether it matches the expected condition.
"""

from youtube_qualifier import qualify_youtube

TEST_CASES = [
    {
        "label": "FAIL — Active polished channel (Alex Hormozi)",
        "person": "Alex Hormozi",
        "company": "Acquisition.com",
        "website": None,
        "expected": "FAIL",
    },
    {
        "label": "FAIL — High volume active creator (Gary Vaynerchuk)",
        "person": "Gary Vaynerchuk",
        "company": "VaynerMedia",
        "website": None,
        "expected": "FAIL",
    },
    {
        "label": "Condition A — No channel (unknown person)",
        "person": "Zzq Randomperson Xkj",
        "company": "Unknown Firm LLC",
        "website": None,
        "expected": "A",
    },
]


def run_tests():
    passed = 0
    failed = 0

    for tc in TEST_CASES:
        print(f"\n{'='*60}")
        print(f"TEST: {tc['label']}")
        print(f"Input: {tc['person']} / {tc['company']}")

        try:
            result = qualify_youtube(tc["person"], tc["company"], tc.get("website"))
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            failed += 1
            continue

        condition = result["condition"]
        expected = tc["expected"]
        status = "PASS" if condition == expected else "MISMATCH"

        print(f"  Condition:   {condition} (expected {expected}) → {status}")
        print(f"  Channel:     {result['channel_name']} — {result['channel_url']}")
        print(f"  Last Upload: {result['last_upload_date']}")
        print(f"  Stage:       {result['stage']}")
        print(f"  Reasoning:   {result['reasoning']}")

        if status == "PASS":
            passed += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed / {len(TEST_CASES)} total")


if __name__ == "__main__":
    run_tests()
