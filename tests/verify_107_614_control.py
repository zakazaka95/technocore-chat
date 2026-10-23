"""Fail unless a negative control failed only at the exact intended assertions."""

import sys
import xml.etree.ElementTree as ET

report, expected_total, expected_failures, expected_name, expected_assertion = sys.argv[1:]
cases = ET.parse(report).findall(".//testcase")
assert len(cases) == int(expected_total), f"Unexpected case count: {len(cases)}"
assert all(case.find("error") is None for case in cases), "Negative control had test errors"
assert all(case.find("skipped") is None for case in cases), "Negative control skipped tests"
failed = [case for case in cases if case.find("failure") is not None]
assert len(failed) == int(expected_failures), f"Unexpected failure count: {len(failed)}"
for case in failed:
    assert case.attrib["name"].startswith(expected_name + "["), case.attrib["name"]
    failure = case.find("failure")
    assert failure is not None
    assert expected_assertion in (failure.text or ""), failure.text
print(f"Verified expected negative control: {len(failed)} failures among {len(cases)} cases")
