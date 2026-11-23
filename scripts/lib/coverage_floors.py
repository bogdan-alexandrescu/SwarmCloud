#!/usr/bin/env python3
"""The coverage ratchet (issue 64): per-package line coverage against the
floors in coverage-floors.json.

    coverage_floors.py python-report --json COVERAGE_JSON --out REPORT
    coverage_floors.py ui-report --out REPORT SHARD_JSON...
    coverage_floors.py check --floors FLOORS [--report REPORT]...
    coverage_floors.py fill --floors FLOORS --about TEXT --report REPORT...

scripts/coverage-floors.sh runs all three in the `coverage` job; nothing else
calls this file.

`python-report` reads `coverage json` (the unit shards' data files already
combined by `coverage combine`) and sums each file's measured and covered
lines into its package, `apps/<dir>`.

`ui-report` reads the swarm-ui shards' istanbul coverage-final.json files
(vitest's v8 provider, `json` reporter) and merges them itself: the same
statement (by its source span) in several shards sums its counts, so a
statement one shard ran is covered once. A LINE is covered when any statement
starting on it ran -- istanbul's own line metric -- and the package is
`apps/swarm-ui`.

A report is {"suite": "python" | "ui", "packages": {name: {"covered": n,
"lines": m}}}.

`check` judges each package of coverage-floors.json whose suite's report was
given, one line each:

  BELOW     its coverage is under its floor            -> exit 1
  MISSING   its suite ran but reported no such package -> exit 1
  RAISE     2+ points above the floor: raise it (the ratchet), still a pass
  ok        at or above, under 2 points
  UNMEASURED  floor null: prints the number to set, never fails
  NO FLOOR  a reported package the floors file does not list, never fails
  not judged  its suite's report was not given (that suite did not run)

`fill` writes a floor for each package whose floor is null (and each
reported package the file does not list yet): what the reports measured,
less one point, rounded down; with --raise it also raises every floor 2+
points under what was measured. It replaces `about` with --about (when and
how the numbers were measured). The reports are the `coverage-reports`
artifact of a `coverage` job (docs/ci.md, "Coverage floors").

A floors file it cannot read WHOLE is exit 2: an entry it could not parse is
never skipped while the others pass. tests/unit/scripts/test_coverage_floors.py
holds each rule.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SUITES = ("python", "ui")
UI_PACKAGE = "apps/swarm-ui"
# A package this many points above its floor gets a hint to raise it.
RAISE_POINTS = 2.0
ENTRY_KEYS = {"suite", "floor", "measured", "note"}


class FloorsError(Exception):
    """coverage-floors.json (or a report) cannot be read whole."""


def load_floors(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise FloorsError(f"cannot read the floors file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FloorsError(f"the floors file {path} is not a JSON object")
    if not isinstance(data.get("about"), str) or not data["about"].strip():
        raise FloorsError(f"the floors file {path} has no `about` saying when and how it was measured")
    packages = data.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise FloorsError(f"the floors file {path} has no `packages`")
    for name, entry in packages.items():
        where = f"the floors file {path}, package {name!r}"
        if not isinstance(entry, dict):
            raise FloorsError(f"{where}: not an object")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise FloorsError(f"{where}: unknown key(s) {sorted(unknown)}")
        if entry.get("suite") not in SUITES:
            raise FloorsError(f"{where}: `suite` must be one of {list(SUITES)}")
        if "floor" not in entry:
            raise FloorsError(f"{where}: no `floor` (null, with a `note`, for a package not measured yet)")
        floor = entry["floor"]
        if floor is None:
            if not isinstance(entry.get("note"), str) or not entry["note"].strip():
                raise FloorsError(f"{where}: a null floor needs a `note` saying why it is not measured")
        elif not _is_percent(floor):
            raise FloorsError(f"{where}: `floor` must be a number from 0 to 100, not {floor!r}")
        if "measured" in entry and entry["measured"] is not None and not _is_percent(entry["measured"]):
            raise FloorsError(f"{where}: `measured` must be a number from 0 to 100, not {entry['measured']!r}")
    return packages


def _is_percent(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 100)


def load_report(path: Path) -> tuple[str, dict[str, tuple[int, int]]]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise FloorsError(f"cannot read the coverage report {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("suite") not in SUITES or not isinstance(data.get("packages"), dict):
        raise FloorsError(f"the coverage report {path} is not a {{suite, packages}} object")
    packages: dict[str, tuple[int, int]] = {}
    for name, value in data["packages"].items():
        covered = value.get("covered") if isinstance(value, dict) else None
        lines = value.get("lines") if isinstance(value, dict) else None
        if not (isinstance(covered, int) and isinstance(lines, int) and 0 <= covered <= lines):
            raise FloorsError(f"the coverage report {path}, package {name!r}: bad counts {value!r}")
        packages[name] = (covered, lines)
    return data["suite"], packages


def percent(covered: int, lines: int) -> float:
    return 100.0 * covered / lines if lines else 100.0


def check(floors: dict[str, dict], reports: dict[str, dict[str, tuple[int, int]]]) -> tuple[list[str], bool]:
    """One line per package, and whether any failed."""
    lines: list[str] = []
    failed = False
    for name in sorted(floors):
        entry = floors[name]
        suite, floor = entry["suite"], entry["floor"]
        if suite not in reports:
            lines.append(f"not judged  {name}: the {suite} suite did not run in full here")
            continue
        if name not in reports[suite]:
            if floor is None:
                lines.append(f"UNMEASURED  {name}: not in the {suite} report either")
            else:
                failed = True
                lines.append(f"MISSING  {name}: floor {floor}, but the {suite} report has no line of it "
                             "(were its tests deleted?)")
            continue
        covered, total = reports[suite][name]
        got = percent(covered, total)
        measured = f"{got:.2f}% ({covered}/{total} lines)"
        if floor is None:
            suggest = suggested_floor(got)
            lines.append(f"UNMEASURED  {name}: {measured}; set its floor to {suggest} in coverage-floors.json")
        elif got < floor:
            failed = True
            lines.append(f"BELOW  {name}: {measured} is under its floor {floor}")
        elif got >= floor + RAISE_POINTS:
            lines.append(f"RAISE  {name}: {measured}, floor {floor}: raise the floor to "
                         f"{suggested_floor(got)} (the ratchet)")
        else:
            lines.append(f"ok  {name}: {measured}, floor {floor}")
    for suite in sorted(reports):
        for name in sorted(set(reports[suite]) - set(floors)):
            covered, total = reports[suite][name]
            lines.append(f"NO FLOOR  {name}: {percent(covered, total):.2f}% ({covered}/{total} lines) in the "
                         f"{suite} report; add it to coverage-floors.json")
    return lines, failed


def python_report(coverage_json: Path) -> dict:
    """coverage.py's JSON report, files summed into `apps/<dir>`."""
    data = json.loads(Path(coverage_json).read_text())
    packages: dict[str, dict[str, int]] = {}
    for path, item in sorted((data.get("files") or {}).items()):
        parts = Path(path).parts
        if len(parts) < 3 or parts[0] != "apps":
            continue
        summary = item["summary"]
        package = packages.setdefault(f"apps/{parts[1]}", {"covered": 0, "lines": 0})
        package["covered"] += int(summary["covered_lines"])
        package["lines"] += int(summary["num_statements"])
    return {"suite": "python", "packages": packages}


def suggested_floor(got: float) -> int:
    return max(math.floor(got) - 1, 0)


def fill(path: Path, about: str, reports: dict[str, dict[str, tuple[int, int]]], *, raise_floors: bool) -> list[str]:
    """Set the null floors (and, with raise_floors, the stale ones) from the
    reports; returns one line per change."""
    floors = load_floors(path)
    data = json.loads(Path(path).read_text())
    changed: list[str] = []
    for suite, packages in sorted(reports.items()):
        for name, (covered, total) in sorted(packages.items()):
            got = percent(covered, total)
            entry = floors.get(name)
            if entry is not None and entry["suite"] != suite:
                raise FloorsError(f"{name} is a {entry['suite']} package, but the {suite} report has it")
            old = None if entry is None else entry["floor"]
            if old is not None and not (raise_floors and got >= old + RAISE_POINTS):
                continue
            data["packages"][name] = {"suite": suite, "floor": suggested_floor(got), "measured": round(got, 2)}
            changed.append(f"{name}: floor {old} -> {suggested_floor(got)} (measured {got:.2f}%)")
    if not about.strip():
        raise FloorsError("fill: --about must say when and how the numbers were measured")
    data["about"] = about
    data["packages"] = dict(sorted(data["packages"].items()))
    Path(path).write_text(json.dumps(data, indent=2) + "\n")
    load_floors(path)
    return changed


def _span(location: dict) -> tuple:
    start, end = location["start"], location["end"]
    return (start.get("line"), start.get("column"), end.get("line"), end.get("column"))


def _relative(path: str) -> str:
    marker = UI_PACKAGE + "/"
    return path[path.rindex(marker) + len(marker):] if marker in path else path


def ui_report(shards: list[Path]) -> dict:
    """The swarm-ui shards' istanbul JSON, merged statement by statement."""
    if not shards:
        raise FloorsError("ui-report: no shard's coverage-final.json was given")
    counts: dict[str, dict[tuple, int]] = {}
    for shard in shards:
        data = json.loads(Path(shard).read_text())
        for path, item in data.items():
            merged = counts.setdefault(_relative(item.get("path") or path), {})
            for sid, location in item.get("statementMap", {}).items():
                span = _span(location)
                merged[span] = merged.get(span, 0) + int(item.get("s", {}).get(sid, 0))
    covered = total = 0
    for merged in counts.values():
        by_line: dict[int, int] = {}
        for span, count in merged.items():
            by_line[span[0]] = max(by_line.get(span[0], 0), count)
        total += len(by_line)
        covered += sum(1 for count in by_line.values() if count > 0)
    return {"suite": "ui", "packages": {UI_PACKAGE: {"covered": covered, "lines": total}}}


def _write(path: Path, report: dict) -> None:
    Path(path).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    for name, value in sorted(report["packages"].items()):
        print(f"{report['suite']}: {name} {percent(value['covered'], value['lines']):.2f}% "
              f"({value['covered']}/{value['lines']} lines)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    py = sub.add_parser("python-report")
    py.add_argument("--json", required=True, type=Path)
    py.add_argument("--out", required=True, type=Path)
    ui = sub.add_parser("ui-report")
    ui.add_argument("--out", required=True, type=Path)
    ui.add_argument("shards", nargs="*", type=Path)
    ck = sub.add_parser("check")
    ck.add_argument("--floors", required=True, type=Path)
    ck.add_argument("--report", action="append", default=[], type=Path)
    fl = sub.add_parser("fill")
    fl.add_argument("--floors", required=True, type=Path)
    fl.add_argument("--about", required=True)
    fl.add_argument("--report", action="append", required=True, type=Path)
    fl.add_argument("--raise", dest="raise_floors", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "python-report":
            _write(args.out, python_report(args.json))
            return 0
        if args.command == "ui-report":
            _write(args.out, ui_report(args.shards))
            return 0
        floors = load_floors(args.floors)
        reports: dict[str, dict[str, tuple[int, int]]] = {}
        for path in args.report:
            suite, packages = load_report(path)
            if suite in reports:
                raise FloorsError(f"two {suite} reports given; combine the shards first")
            reports[suite] = packages
        if args.command == "fill":
            for line in fill(args.floors, args.about, reports, raise_floors=args.raise_floors) or ["no floor changed"]:
                print(line)
            return 0
    except (FloorsError, KeyError, TypeError, ValueError) as exc:
        print(f"coverage-floors: {exc}", file=sys.stderr)
        return 2
    lines, failed = check(floors, reports)
    for line in lines:
        print(line)
    if failed:
        print("coverage-floors: a package is below its floor or missing; add tests, or (if coverage fell "
              "on purpose) lower its floor in coverage-floors.json and say why in the commit")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
