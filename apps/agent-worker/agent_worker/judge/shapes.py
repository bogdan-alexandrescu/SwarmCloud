"""The six call shapes of context-quality.md §5.2, and how each answer is read.

Each builder returns `(state, questions)` for `JudgeClient.decide`, truncated
to §5.2's limits; each reader turns a successful call's answers into the one
thing its caller uses. The callers are lane CTXQ-JW's: J1 in the pack's
ranking, J2 and J5 in the planner's post-step, J3 in the review post-step,
J4 in the gate. Every output is ADVISORY: no reader returns a verdict, and a
failed call leaves the caller with its local-only answer.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: J0's options, in §5.2's order.
ROLES = ("implement", "review", "fix", "test", "design", "plan")
#: J0: below this confidence the pipeline stays `implement`.
CLASSIFY_MIN_CONFIDENCE = 0.6
#: J2: a requirement whose best step is under this is flagged.
PLAN_COVERAGE_MIN_P = 0.5
#: J3: a finding under this reaches the fixer marked "verify before acting".
FINDING_VERIFY_BELOW = 0.15
#: J5: owner decision 2026-10-14 (§8 Q5 A): hold an auto-approved sweep plan
#: for a human at or above this. Jev never decides; a human does.
DUPLICATE_HOLD_P = 0.85

BRIEF_CHARS = 4_000
MAX_CANDIDATES = 60
EXCERPT_CHARS = 600
STEP_PROMPT_CHARS = 1_500
HUNK_CHARS = 1_200
TEST_EXCERPT_CHARS = 800
ISSUE_BODY_CHARS = 3_000
MAX_IN_FLIGHT = 40
#: J4's options are the tests plus `none`, inside the API's 255.
MAX_CHOICE_TESTS = 254
NONE = "none"

_CANDIDATE_KEYS = ("kind", "path", "symbol", "signature", "excerpt", "why")


def _cut(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _choice(answers: Mapping[str, Any], name: str) -> tuple[str | None, float]:
    answer = answers.get(name)
    if not isinstance(answer, Mapping):
        return None, 0.0
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    p = probabilities.get(choice) if isinstance(probabilities, Mapping) else None
    return (choice if isinstance(choice, str) else None,
            float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else 0.0)


def j0_classify(brief: str) -> tuple[dict, dict]:
    return ({"task": {"brief": _cut(brief, BRIEF_CHARS)}},
            {"role": {"type": "choice",
                      "instructions": "Which kind of engineering work does `task.brief` ask for?",
                      "criteria": {role: None for role in ROLES}}})


def read_classify(answers: Mapping[str, Any]) -> str:
    answer = answers.get("role")
    confidence = answer.get("confidence") if isinstance(answer, Mapping) else None
    choice, _ = _choice(answers, "role")
    if choice in ROLES and isinstance(confidence, (int, float)) \
            and confidence >= CLASSIFY_MIN_CONFIDENCE:
        return choice
    return "implement"


def j1_rerank(task_type: str, brief: str,
              candidates: Sequence[Mapping[str, Any]]) -> tuple[dict, dict]:
    rows = []
    for i, candidate in enumerate(candidates[:MAX_CANDIDATES]):
        row: dict[str, Any] = {"i": i}
        for key in _CANDIDATE_KEYS:
            value = candidate.get(key)
            if isinstance(value, str) and value:
                row[key] = value[:EXCERPT_CHARS] if key == "excerpt" else value
        rows.append(row)
    questions = {
        f"c{row['i']}": {"type": "noul",
                         "instructions": "Would an engineer doing `task` need to read the entry "
                                         f"of `candidates` with i = {row['i']} to get it right?"}
        for row in rows
    }
    return ({"task": {"type": task_type, "brief": _cut(brief, BRIEF_CHARS)},
             "candidates": rows}, questions)


def read_rerank(answers: Mapping[str, Any]) -> dict[int, float]:
    """Candidate index -> p(needed). Missing answers are absent, not zero."""
    out: dict[int, float] = {}
    for name, answer in answers.items():
        if name.startswith("c") and name[1:].isdigit() and isinstance(answer, Mapping):
            p = answer.get("noul")
            if isinstance(p, (int, float)) and not isinstance(p, bool):
                out[int(name[1:])] = float(p)
    return out


def j2_plan_coverage(requirements: Sequence[str],
                     steps: Sequence[Mapping[str, Any]]) -> tuple[dict, dict]:
    rows = [{"step_id": str(s.get("step_id", "")), "title": _cut(s.get("title"), 300),
             "files": [f for f in s.get("files") or [] if isinstance(f, str)],
             "tests": [t for t in s.get("tests") or [] if isinstance(t, str)],
             "prompt": _cut(s.get("prompt"), STEP_PROMPT_CHARS)} for s in steps]
    options = {row["step_id"]: row["title"] or None for row in rows if row["step_id"]}
    options[NONE] = "no step covers it"
    questions = {
        f"r{k}": {"type": "choice",
                  "instructions": f"Which entry of `steps` delivers `requirements[{k}]`?",
                  "criteria": dict(options)}
        for k in range(len(requirements))
    }
    return {"requirements": list(requirements), "steps": rows}, questions


def read_plan_coverage(answers: Mapping[str, Any]) -> list[int]:
    """The requirement indices no step covers with p >= 0.5."""
    flagged = []
    for name in sorted((n for n in answers if n.startswith("r") and n[1:].isdigit()),
                       key=lambda n: int(n[1:])):
        choice, p = _choice(answers, name)
        if choice is None or choice == NONE or p < PLAN_COVERAGE_MIN_P:
            flagged.append(int(name[1:]))
    return flagged


def j3_finding(finding: Mapping[str, Any], code: Mapping[str, Any],
               diff_hunk: str) -> tuple[dict, dict]:
    return ({"finding": {k: finding[k] for k in ("summary", "file", "line") if k in finding},
             "code": {k: code[k] for k in ("path", "window") if k in code},
             "diff_hunk": diff_hunk},
            {"real": {"type": "noul",
                      "instructions": "Is `finding` a real defect in this diff, as stated?"}})


def read_finding(answers: Mapping[str, Any]) -> float | None:
    answer = answers.get("real")
    p = answer.get("noul") if isinstance(answer, Mapping) else None
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None


def j4_test_gap(changed: Sequence[Mapping[str, Any]],
                tests: Sequence[Mapping[str, Any]]) -> tuple[dict, dict]:
    test_rows = [{"i": i, "path": _cut(t.get("path"), 500), "name": _cut(t.get("name"), 300),
                  "excerpt": _cut(t.get("excerpt"), TEST_EXCERPT_CHARS)}
                 for i, t in enumerate(tests[:MAX_CHOICE_TESTS])]
    options: dict[str, Any] = {str(row["i"]): None for row in test_rows}
    options[NONE] = "no listed test would fail"
    changed_rows = [{"symbol": _cut(c.get("symbol"), 300), "hunk": _cut(c.get("hunk"), HUNK_CHARS)}
                    for c in changed]
    questions = {
        f"s{k}": {"type": "choice",
                  "instructions": f"Which entry of `tests` would fail if `changed[{k}]` "
                                  "were reverted?",
                  "criteria": dict(options)}
        for k in range(len(changed_rows))
    }
    return {"changed": changed_rows, "tests": test_rows}, questions



def read_test_gap(answers: Mapping[str, Any]) -> list[int]:
    """The changed-symbol indices no test covers."""
    return sorted(int(n[1:]) for n in answers
                  if n.startswith("s") and n[1:].isdigit() and _choice(answers, n)[0] == NONE)


def j5_duplicate(title: str, body: str,
                 in_flight: Iterable[Mapping[str, Any]]) -> tuple[dict, dict]:
    rows = [{"i": i, "title": _cut(lane.get("title"), 300),
             "files": [f for f in lane.get("files") or [] if isinstance(f, str)][:50]}
            for i, lane in enumerate(list(in_flight)[:MAX_IN_FLIGHT])]
    options: dict[str, Any] = {str(row["i"]): None for row in rows}
    options[NONE] = "no lane already does this"
    return ({"issue": {"title": _cut(title, 300), "body": _cut(body, ISSUE_BODY_CHARS)},
             "in_flight": rows},
            {"dup": {"type": "choice",
                     "instructions": "Which entry of `in_flight` already does the work `issue` "
                                     "asks for?",
                     "criteria": options}})


def read_duplicate(answers: Mapping[str, Any]) -> int | None:
    """The in-flight lane to hold the plan for, or None. A hold, never a drop."""
    choice, p = _choice(answers, "dup")
    if choice is None or choice == NONE or not choice.isdigit() or p < DUPLICATE_HOLD_P:
        return None
    return int(choice)
