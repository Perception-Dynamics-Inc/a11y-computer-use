"""Reject a live-test run with failures, skipped coverage, or no tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree


def verify_report(path: Path) -> int:
    """Return the number of passing cases, or raise ValueError on missing coverage."""
    root = ElementTree.parse(path).getroot()
    for suite in root.iter():
        if suite.tag in ("testsuite", "testsuites") and any(
            int(suite.attrib.get(outcome, "0")) != 0
            for outcome in ("failures", "errors", "skipped")
        ):
            raise ValueError(f"{path}: live suite reports failures, errors, or skipped tests")
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError(f"{path}: no live tests ran")
    problems = [case.attrib.get("name", "?") for case in cases
                if any(case.find(tag) is not None for tag in ("skipped", "failure", "error"))]
    if problems:
        raise ValueError(f"{path}: live coverage failed or skipped: {', '.join(problems)}")
    return len(cases)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    print(f"Live gate: {verify_report(args.report)} passed, no skips")
