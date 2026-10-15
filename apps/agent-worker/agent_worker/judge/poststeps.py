"""The planner's and the reviewer's Jev post-steps: J2, J5 and J3 (lane CTXQ-JW).

They run in the runner (`runners/cliagent.run_cli_agent`), after the agent's
last turn and before its result is written, over what the agent wrote into
`$SWARM_ARTIFACTS_DIR` -- and only when the worker set Jev up for the attempt
(`judge.attempt`: the workspace opted in to Jev for code).

* PLANNER (`plan_post_step`), over `plan.json`:
  * J2, plan coverage: one `choice` per requirement over the plan's steps and
    `none`. A requirement whose best step is under p 0.5, or `none`, is flagged.
  * J5, duplicate: one `choice` over the in-flight lanes the stager wrote
    (`inflight.json`, titles and files only, §5.4) and `none`. p >= 0.85 on a
    lane asks for the auto-approved plan to be HELD FOR A HUMAN (owner
    decision §8 Q5 A) -- swarm-api's planner post-step
    (`routes/runs.py::_from_planner`) reads `PLAN_ADVISORY_FILE` and does it.
    Jev never decides; it can only make a person look.
  Written to `PLAN_ADVISORY_FILE` beside `plan.json`; `plan.json` itself is
  never touched.
* REVIEWER (`review_post_step`), over `verdict.json`:
  * J3, finding triage: one `noul` per finding, "is this a real defect in
    this diff, as stated?", with the code around the finding's line and the
    patch's hunk for its file. The answers go onto the findings as `p_real`
    (`verdict.annotate_findings`); under 0.15 a finding is marked "verify
    before acting" and still reaches the fixer. `verdict` is never touched.

ALL OR NOTHING, PER QUESTION SET. A J whose call is skipped (breaker, budget),
fails, times out or answers malformed contributes nothing: with every call
failing no file is written or rewritten, so the outputs are byte for byte the
local-only run's. Nothing here raises.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_worker.judge import shapes
from agent_worker.judge.sanitize import is_denied_path

PLAN_FILE = "plan.json"
VERDICT_FILE = "verdict.json"
#: What the planner's post-step writes, and swarm-api's `_from_planner` reads
#: (`swarm_api.routes.runs.PLAN_ADVISORY_FILE` restates it).
PLAN_ADVISORY_FILE = "plan-advisory.json"
PLAN_ADVISORY_SCHEMA = "swarm.plan-advisory/v1"
#: The most of a plan or verdict read: `issueruns.MAX_PLAN_BYTES` and
#: `verdict.MAX_VERDICT_BYTES` are both 256 KiB or less.
MAX_READ_BYTES = 256 * 1024
#: J3 asks about at most this many findings: each is one call of the task's
#: eight (§5.3 estimates "mostly 0"), and the gate's J4 needs one too.
J3_MAX_FINDINGS = 5
#: §5.2: the code around a finding, ±40 lines.
WINDOW_LINES = 40
MAX_REQUIREMENTS = 64
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_READ_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.jev.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # a link planted at the name is replaced, never followed


def _ask(judge: Any, kind: str, state: Any, questions: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Every question's answer, or None."""
    try:
        decision = judge.decide(kind, state, questions)
    except Exception:  # noqa: BLE001 -- a refused question is a local-only output
        return None
    if not decision.ok or not isinstance(decision.answers, dict):
        return None
    if any(name not in decision.answers for name in questions):
        return None
    return decision.answers


# -- the planner: J2 and J5 -----------------------------------------------------


def _in_flight(context_dir: Path | None) -> list[dict[str, Any]]:
    """The stager's layer I rows: titles, refs and files, nothing else (§5.4)."""
    if context_dir is None:
        return []
    staged = _read_json(Path(context_dir) / "inflight.json")
    rows = staged.get("rows") if isinstance(staged, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and isinstance(row.get("title"), str):
            out.append({"title": row["title"], "ref": str(row.get("ref") or ""),
                        "files": [f for f in row.get("files") or [] if isinstance(f, str)]})
    return out[:shapes.MAX_IN_FLIGHT]


def plan_post_step(judge: Any, artifacts_dir: Path, context_dir: Path | None) -> dict | None:
    """J2 and J5 over the planner's plan.json; the advisory written, or None."""
    if judge is None:
        return None
    plan = _read_json(Path(artifacts_dir) / PLAN_FILE)
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return None  # a NOT_READY verdict, or no plan: nothing to judge
    steps = [s for s in plan["steps"] if isinstance(s, dict) and s.get("step_id")]
    requirements = [r for r in plan.get("requirements") or [] if isinstance(r, str)]
    advisory: dict[str, Any] = {"schema": PLAN_ADVISORY_SCHEMA, "advisory": True}
    asked = requirements[:MAX_REQUIREMENTS]
    if asked and steps:
        answers = _ask(judge, "J2", *shapes.j2_plan_coverage(asked, steps))
        if answers is not None:
            flagged = shapes.read_plan_coverage(answers)
            advisory["coverage"] = {
                "requirements": len(asked),
                "flagged": [{"index": k, "requirement": asked[k]} for k in flagged
                            if k < len(asked)],
            }
    lanes = _in_flight(context_dir)
    if lanes and steps:
        title = str(plan.get("summary") or "").split("\n", 1)[0]
        body = "\n".join([str(plan.get("summary") or ""), *requirements])
        answers = _ask(judge, "J5", *shapes.j5_duplicate(title, body, lanes))
        if answers is not None:
            lane = shapes.read_duplicate(answers)
            if lane is not None and lane < len(lanes):
                _choice, p = shapes._choice(answers, "dup")
                advisory["duplicate"] = {"title": lanes[lane]["title"], "ref": lanes[lane]["ref"],
                                         "p": round(p, 4)}
            else:
                advisory["duplicate"] = None
    if "coverage" not in advisory and "duplicate" not in advisory:
        return None
    advisory["hold_for_human"] = bool(advisory.get("duplicate"))
    _write_atomically(Path(artifacts_dir) / PLAN_ADVISORY_FILE,
                      json.dumps(advisory, indent=2, sort_keys=True) + "\n")
    return advisory


# -- the reviewer: J3 -------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def code_window(checkout: Path, file: str, line: int | None) -> dict[str, Any]:
    """`{path, window}` around `line`, or `{path}` alone: for a denied path
    (§5.4 names only), a file outside the checkout, a link, or no line."""
    out: dict[str, Any] = {"path": file}
    target = Path(checkout) / file
    if is_denied_path(file) or line is None or target.is_symlink() or not target.is_file() \
            or not _inside(target, Path(checkout)):
        return out
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    first = max(line - 1 - WINDOW_LINES, 0)
    out["window"] = "\n".join(lines[first:line + WINDOW_LINES])
    return out


def patch_hunks(patch: str) -> dict[str, list[tuple[int, int, str]]]:
    """Per new-side path, each hunk's new-side range and its text."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    current: str | None = None
    lines: list[str] = []
    start = count = 0

    def close() -> None:
        if current is not None and lines:
            out.setdefault(current, []).append((start, count, "\n".join(lines)))

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            close()
            current, lines = None, []
        elif line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current is not None and (match := _HUNK.match(line)):
            close()
            start, count = int(match.group(1)), int(match.group(2) or "1")
            lines = [line]
        elif lines and line[:1] in ("+", "-", " "):
            lines.append(line)
    close()
    return out


def _hunk_for(hunks: Mapping[str, Sequence[tuple[int, int, str]]], file: str,
              line: int | None) -> str:
    rows = list(hunks.get(file) or ())
    if not rows:
        return ""
    if line is not None:
        for start, count, text in rows:
            if start <= line <= start + max(count, 1) - 1:
                return text[:shapes.HUNK_CHARS]
    return rows[0][2][:shapes.HUNK_CHARS]


def _staged_patch(work_dir: Path, payload: Mapping[str, Any]) -> str:
    for entry in payload.get("staged_inputs") or ():
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(raw, str) and raw.endswith(".patch"):
            target = Path(work_dir) / raw
            if _inside(target, Path(work_dir)) and not target.is_symlink() and target.is_file():
                try:
                    return target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
    return ""


def _line(value: Any) -> int | None:
    return value if type(value) is int and value >= 1 else None


def review_post_step(judge: Any, artifacts_dir: Path, checkout: Path | None, work_dir: Path,
                     payload: Mapping[str, Any]) -> dict | None:
    """J3 over the review's verdict.json; `{asked, p_real}` when it was
    annotated, None (and the file untouched) otherwise."""
    from agent_worker import verdict as verdict_mod

    if judge is None:
        return None
    path = Path(artifacts_dir) / VERDICT_FILE
    document = _read_json(path)
    if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
        return None
    hunks = patch_hunks(_staged_patch(work_dir, payload))
    stops = tuple(getattr(judge, "hard_stop_paths", ()) or ())
    p_real: dict[int, float] = {}
    asked = 0
    for index, item in enumerate(document["findings"]):
        if asked >= J3_MAX_FINDINGS:
            break
        if isinstance(item, str):
            item = {"summary": item}
        if not isinstance(item, dict) or not isinstance(item.get("summary"), str):
            continue
        file = item.get("file") if isinstance(item.get("file"), str) else ""
        line = _line(item.get("line"))
        finding = {k: v for k, v in (("summary", item["summary"]), ("file", file),
                                     ("line", line)) if v}
        code = code_window(checkout, file, line) if file and checkout is not None else {}
        # §5.4: names only for a denied path. `diff_hunk` is a top-level
        # string the client's sanitizer cannot tie to a path, so withhold it here.
        hunk = "" if file and is_denied_path(file, stops) else _hunk_for(hunks, file, line)
        state, questions = shapes.j3_finding(finding, code, hunk)
        asked += 1
        answers = _ask(judge, "J3", state, questions)
        p = shapes.read_finding(answers) if answers is not None else None
        if p is None:
            return None  # all or nothing: no partial triage reaches the fixer
        p_real[index] = min(max(p, 0.0), 1.0)
    if not p_real:
        return None
    annotated = verdict_mod.annotate_findings(document, p_real)
    _write_atomically(path, json.dumps(annotated, indent=2) + "\n")
    return {"asked": asked, "p_real": {str(k): round(v, 4) for k, v in p_real.items()}}
