#!/usr/bin/env python3
"""Compare a pyright report with the per-file baseline, or write the baseline.

scripts/typecheck.sh is the entry point; this is its comparison, kept in
Python so the rule is one function the unit tests call directly.

    pyright_baseline.py compare --root <repo> --report <pyright.json> --baseline <baseline.json> [--partial]
    pyright_baseline.py write   --root <repo> --report <pyright.json> --baseline <baseline.json> --version <X.Y.Z>

An error's identity is (file, rule): its LINE is not, because an unrelated
edit above it moves it, and its message is not, because renaming a variable
changes it. The baseline holds, per repo-relative file, the number of errors
of each rule pyright reported when it was written. A run is red when some
(file, rule) has MORE errors than the baseline allows; every error of that
(file, rule) is then printed, since which one is new cannot be told apart.
A file the baseline does not name allows none, so a new file starts clean.

`--partial` says the run checked only the files named on the command line:
only the files that reported errors are compared, and nothing is said about
baseline entries that could shrink (a file that came out clean may simply not
have been checked). A full run prints, on stderr, the entries the code has
since outgrown, so the baseline can be rewritten smaller.

stdout: one line per error of a (file, rule) over its allowance,
`path:line:col: rule: message`. stderr: the counts compared, so an empty
stdout is visibly a comparison and not a run that checked nothing.

    exit 0  nothing over the baseline (or the baseline was written)
    exit 1  at least one (file, rule) over the baseline (listed)
    exit 2  the report or the baseline could not be read, or checked no file
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# pyright's own name for an error that carries no rule (a syntax error, say).
NO_RULE = "error"


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def load_report(path: Path) -> dict:
    report = json.loads(path.read_text())
    if not isinstance(report.get("generalDiagnostics"), list) or "summary" not in report:
        raise ValueError(f"{path} is not a pyright --outputjson report")
    return report


def errors(report: dict, root: Path) -> list[dict]:
    """The report's errors (warnings and information are not gated), repo-relative."""
    out = []
    for d in report["generalDiagnostics"]:
        if d.get("severity") != "error":
            continue
        start = d.get("range", {}).get("start", {})
        out.append({
            "file": _rel(d.get("file", ""), root),
            "rule": d.get("rule") or NO_RULE,
            # pyright's positions are 0-based; editors and CI annotations are 1-based.
            "line": int(start.get("line", 0)) + 1,
            "col": int(start.get("character", 0)) + 1,
            "message": " ".join(str(d.get("message", "")).split()),
        })
    return out


def counts(errs: list[dict]) -> dict[str, dict[str, int]]:
    by_file: dict[str, Counter] = {}
    for e in errs:
        by_file.setdefault(e["file"], Counter())[e["rule"]] += 1
    return {f: dict(sorted(c.items())) for f, c in sorted(by_file.items())}


def compare(errs: list[dict], baseline: dict[str, dict[str, int]]) -> tuple[list[dict], list[str]]:
    """(the errors of every (file, rule) over its allowance, the entries that could shrink)."""
    found = counts(errs)
    over = {
        (f, rule)
        for f, rules in found.items()
        for rule, n in rules.items()
        if n > baseline.get(f, {}).get(rule, 0)
    }
    new = [e for e in errs if (e["file"], e["rule"]) in over]
    shrink = [
        f"{f} {rule}: baseline {n}, found {found.get(f, {}).get(rule, 0)}"
        for f, rules in sorted(baseline.items())
        for rule, n in sorted(rules.items())
        if found.get(f, {}).get(rule, 0) < n
    ]
    return new, shrink


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    data = json.loads(path.read_text())
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{path} has no \"files\" object")
    return files


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("compare", "write"))
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--partial", action="store_true")
    ap.add_argument("--version", default="")
    args = ap.parse_args(argv)

    try:
        report = load_report(args.report)
    except (OSError, ValueError) as exc:
        print(f"pyright_baseline: cannot read the report: {exc}", file=sys.stderr)
        return 2
    analyzed = int(report["summary"].get("filesAnalyzed", 0))
    if analyzed == 0:
        print("pyright_baseline: pyright analyzed no file; nothing was checked", file=sys.stderr)
        return 2
    errs = errors(report, args.root)

    if args.mode == "write":
        if args.partial or not args.version:
            print("pyright_baseline: write needs a full run and --version", file=sys.stderr)
            return 2
        body = {
            "_comment": "Generated by `scripts/typecheck.sh --write-baseline`; "
                        "per file, the number of pyright errors of each rule it may keep.",
            "pyright": args.version,
            "files": counts(errs),
        }
        args.baseline.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n")
        print(f"pyright_baseline: wrote {len(body['files'])} files, {len(errs)} errors "
              f"({analyzed} files analyzed) to {_rel(str(args.baseline), args.root)}",
              file=sys.stderr)
        return 0

    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as exc:
        print(f"pyright_baseline: cannot read the baseline: {exc}", file=sys.stderr)
        return 2
    new, shrink = compare(errs, baseline)
    for e in new:
        print(f"{e['file']}:{e['line']}:{e['col']}: {e['rule']}: {e['message']}")
    allowed = sum(sum(r.values()) for r in baseline.values())
    print(f"pyright_baseline: {analyzed} files analyzed, {len(errs)} errors, "
          f"{allowed} allowed by the baseline over {len(baseline)} files, "
          f"{len(new)} in a (file, rule) over its allowance", file=sys.stderr)
    if shrink and not args.partial:
        print(f"pyright_baseline: {len(shrink)} baseline entries can shrink "
              "(run scripts/typecheck.sh --write-baseline):", file=sys.stderr)
        for line in shrink:
            print(f"  {line}", file=sys.stderr)
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
