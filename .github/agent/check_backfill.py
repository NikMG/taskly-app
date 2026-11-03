#!/usr/bin/env python3
"""Check that a backfill run accounted for every file with untested code.

The list of gaps comes from the coverage report of a run the agent did not control,
and is compared with the decisions in its report. A gap passes only if the report
states why it stays untested, so a run cannot finish by ignoring part of the stack.
Whether a stated reason is any good remains for the human reviewer: all are printed.

Exit: 0 every gap is accounted for, 1 some are not, 2 unreadable input.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from verify_patch import _load_json, _stack_path


def _backend_gaps(doc: Any) -> dict[str, str]:
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        raise ValueError("coverage.json has no files")
    gaps = {}
    for key, entry in files.items():
        summary = entry.get("summary") if isinstance(entry, dict) else None
        if not isinstance(summary, dict):
            raise ValueError("coverage.json has a malformed entry")
        lines, branches = summary.get("missing_lines", 0), summary.get("missing_branches", 0)
        if lines or branches:
            gaps[_stack_path(key, "backend")] = f"{lines} lines, {branches} branches untested"
    return gaps


def _frontend_gaps(doc: Any) -> dict[str, str]:
    if not isinstance(doc, dict) or "total" not in doc:
        raise ValueError("coverage-summary.json has no total")
    gaps = {}
    for key, entry in doc.items():
        if key == "total":
            continue
        if not isinstance(entry, dict):
            raise ValueError("coverage-summary.json has a malformed entry")
        missing = {}
        for metric in ("lines", "branches", "functions"):
            data = entry.get(metric)
            if not isinstance(data, dict):
                raise ValueError("coverage-summary.json has a malformed entry")
            missing[metric] = data.get("total", 0) - data.get("covered", 0)
        if any(missing.values()):
            detail = ", ".join(f"{count} {metric}" for metric, count in missing.items() if count)
            gaps[_stack_path(key, "frontend")] = f"{detail} untested"
    return gaps


def find_gaps(results: Path, stack: str) -> dict[str, str]:
    if stack == "backend":
        return _backend_gaps(_load_json(results / "coverage" / "coverage.json"))
    return _frontend_gaps(_load_json(results / "coverage" / "coverage-summary.json"))


def decisions(report: Any, stack: str) -> dict[str, tuple[bool, str]]:
    entries = report.get("files_analyzed") if isinstance(report, dict) else None
    if not isinstance(entries, list):
        raise ValueError("report has no files_analyzed")
    return {
        entry["path"]: (entry.get("needs_tests") is not False, str(entry.get("reason") or "").strip())
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry.get("stack") == stack
    }


def _printable(text: str) -> str:
    # The reason is model output and is about to reach a terminal.
    return "".join(ch if ch.isprintable() else " " for ch in text)


def check(gaps: dict[str, str], decided: dict[str, tuple[bool, str]]) -> tuple[list[str], list[str]]:
    explained, unresolved = [], []
    for path, gap in sorted(gaps.items()):
        needs_tests, reason = decided.get(path, (True, ""))
        if needs_tests or not reason:
            state = "still needs tests" if path in decided else "not in the report"
            unresolved.append(f"{_printable(path)}: {gap}; {state}")
        else:
            explained.append(f"{_printable(path)}: {gap}; {_printable(reason)}")
    return explained, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stack", choices=("backend", "frontend"))
    ap.add_argument("--results", required=True, help="results directory of the independent run")
    ap.add_argument("--report", required=True, help="report.json of the agent")
    args = ap.parse_args()
    try:
        gaps = find_gaps(Path(args.results), args.stack)
        decided = decisions(_load_json(Path(args.report)), args.stack)
    except (ValueError, TypeError) as exc:
        print(f"check_backfill.py: {exc}", file=sys.stderr)
        return 2
    explained, unresolved = check(gaps, decided)
    print(f"Files with untested code: {len(gaps)}")
    for title, rows in (("Left untested on purpose", explained), ("NOT accounted for", unresolved)):
        if rows:
            print(f"{title}:")
            print("\n".join(f"  {row}" for row in rows))
    return 1 if unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
