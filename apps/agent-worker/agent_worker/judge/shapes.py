"""The six call shapes of context-quality.md §5.2, and how each answer is read.

Each builder returns `(state, questions)` for `JudgeClient.decide`, truncated
to §5.2's limits; each reader turns a successful call's answers into the one
thing its caller uses. J0 is called by `contextpack.tasktype.classify_with_jev`
for a step the local rules leave `implement`. The others are lane CTXQ-JW's:
J1 in the pack's ranking, J2 and J5 in the planner's post-step, J3 in the
review post-step, J4 in the gate. Every output is ADVISORY: no reader returns a verdict, and a
failed call leaves the caller with its local-only answer.

JC2 (docs/design/agent-quality/jev-contract.md section 5, lane 6) is the one
exception, and it decides nothing either: `jc2_state` and `read_jc2` build
the end-of-step contract judgment the worker's post-step posts to swarm-api's
`state/contract-verdict`, which re-reads and writes it.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: J0's options, in §5.2's order.
ROLES = ("implement", "review", "fix", "test", "design", "plan")
#: J0: below this confidence the pipeline stays `implement` (and at or above
#: it, `classify_with_jev` still takes only `tasktype.JEV_TYPES`).
CLASSIFY_MIN_CONFIDENCE = 0.6
#: J2: a requirement whose best step is under this is flagged.
PLAN_COVERAGE_MIN_P = 0.5
#: J2: a flag's `coverage` in `plan-advisory.json` (swarm-api serves it as is).
PLAN_COVERAGE_NONE = "none"
PLAN_COVERAGE_WEAK = "weak"
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


def read_plan_coverage(answers: Mapping[str, Any]) -> list[tuple[int, str]]:
    """`(index, coverage)` for each requirement no step covers with p >= 0.5.

    `coverage` is `none` when the answer is `none` (or no step), and `weak`
    when it names a step under p 0.5: the plan reaches for it, unsurely.
    """
    flagged = []
    for name in sorted((n for n in answers if n.startswith("r") and n[1:].isdigit()),
                       key=lambda n: int(n[1:])):
        choice, p = _choice(answers, name)
        if choice is None or choice == NONE:
            flagged.append((int(name[1:]), PLAN_COVERAGE_NONE))
        elif p < PLAN_COVERAGE_MIN_P:
            flagged.append((int(name[1:]), PLAN_COVERAGE_WEAK))
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


# -- JC2: the end-of-step contract judgment (jev-contract.md section 5) --------
#
# The worker's image is not swarm-api's package, so JC2's state, questions and
# reader are restated here from `swarm_api.contractjudge` (`judgment_state`,
# `judgment_questions`, `read_judgment`); tests/unit/worker/
# test_contract_post_step.py holds the two equal. The route re-reads every
# verdict with its own thresholds (`contractreask.checked_verdict`), so the
# worker's reading only decides what it posts, never what counts.

#: The ledger and usage kind (`swarm_api.contractreask.JUDGMENT_KIND`).
JC2_KIND = "contract_judgment"
#: The verification kind JC2 judges, and the proof kinds a contract may name
#: (`swarm_api.workcontract.VERIFICATION_KINDS`, `ARTIFACT_KINDS`).
JUDGMENT = "judgment"
VERIFICATION_KINDS = ("unit_tests", "ci", "review", "browser_check", "artifact_present",
                      JUDGMENT)
ARTIFACT_KINDS = ("pull_request", "file", "report")
#: A `met` counts and an `unmet` sends back only at p >= 0.7; starting values
#: the experiment calibrates (section 5.3).
JC2_MET_P = 0.7
JC2_UNMET_P = 0.7
#: The client's limits (`client.MAX_QUESTIONS`, `MAX_CHOICE_OPTIONS`,
#: `MAX_STATE_CHARS`), restated as `contractjudge` restates them.
JC2_MAX_QUESTIONS = 64
JC2_MAX_CHOICE_OPTIONS = 255
JC2_MAX_STATE_CHARS = 70_000
#: Three questions per requirement: at most 21 requirements per call.
JC2_PER_CALL = JC2_MAX_QUESTIONS // 3
JC2_MAX_MECHANICAL = len(VERIFICATION_KINDS)
JC2_MAX_CHANGED = JC2_MAX_CHOICE_OPTIONS - 1 - JC2_MAX_MECHANICAL
JC2_PATH_CHARS = 512
JC2_NAMED = 32
JC2_VERDICTS = {
    "met": "the diff and the mechanical results show the requirement done",
    "unmet": "they show it not done, or done wrongly",
    "cannot_judge": "the state does not hold what deciding it needs",
}
JC2_REASONS = ("not_in_diff", "partially_done", "contradicted_by_tests", "wrong_place",
               "scope_exceeded", "evidence_missing", "satisfied")
JC2_STATUSES = ("added", "modified", "deleted", "renamed")


def judged_requirements(document: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    """`(index, id, text)` of each requirement of a stored contract document
    that names `judgment`, in the contract's order; `index` is its position
    among ALL the requirements, which is how the route reads `v<k>`. Never
    `protected` (the owner's answer 12)."""
    rows = document.get("requirements") if isinstance(document, Mapping) else None
    out = []
    for k, row in enumerate(rows if isinstance(rows, list) else ()):
        if not isinstance(row, Mapping):
            continue
        rid, text, kinds = row.get("id"), row.get("text"), row.get("verification")
        if isinstance(rid, str) and isinstance(text, str) and isinstance(kinds, list) \
                and JUDGMENT in kinds:
            out.append((k, rid, text))
    return out


def _passed(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def jc2_state(requirements: Sequence[tuple[int, str, str]], *, commit: str,
              changed: Sequence[Mapping[str, Any]],
              mechanical: Sequence[Mapping[str, Any]],
              artifacts: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """JC2's state (section 5.2), rebuilt from closed keys only: the judged
    requirements' text by index, the commit, the diff file by file, the
    mechanical results (`passed` None for a check that did not run) and the
    artifacts. There is NO argument for the agent's summary, its PR body, its
    claims or its transcript: those are what is judged, and never reach it."""
    if len(requirements) > JC2_PER_CALL:
        raise ValueError(f"at most {JC2_PER_CALL} requirements per call")
    state: dict[str, Any] = {
        "requirements": [{"index": k, "text": text} for k, _rid, text in requirements],
        "commit": commit,
        "changed": [],
        "mechanical": [],
        "artifacts": [{"kind": a.get("kind"), "present": _passed(a.get("present"))}
                      for a in artifacts if a.get("kind") in ARTIFACT_KINDS],
        "cut": 0,
    }
    for entry in list(mechanical)[:JC2_MAX_MECHANICAL]:
        if entry.get("kind") not in VERIFICATION_KINDS or entry.get("kind") == JUDGMENT:
            continue
        row: dict[str, Any] = {"kind": entry["kind"], "passed": _passed(entry.get("passed"))}
        named = entry.get("named")
        if isinstance(named, list):
            row["named"] = [n[:JC2_PATH_CHARS] for n in named[:JC2_NAMED] if isinstance(n, str)]
        state["mechanical"].append(row)
    size = len(json.dumps(state))
    for entry in changed:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        status = entry.get("status") if entry.get("status") in JC2_STATUSES else "modified"
        row = {"item": len(state["changed"]), "path": path[:JC2_PATH_CHARS], "status": status}
        hunk = entry.get("hunk")
        with_hunk = dict(row, hunk=hunk[:HUNK_CHARS]) if isinstance(hunk, str) and hunk else row
        if len(state["changed"]) >= JC2_MAX_CHANGED:
            state["cut"] += 1
            continue
        if size + len(json.dumps(with_hunk)) + 2 <= JC2_MAX_STATE_CHARS:
            row = with_hunk
        elif size + len(json.dumps(row)) + 2 <= JC2_MAX_STATE_CHARS:
            state["cut"] += 1
        else:
            state["cut"] += 1
            continue
        size += len(json.dumps(row)) + 2
        state["changed"].append(row)
    return state


def jc2_evidence_options(state: Mapping[str, Any]) -> dict[str, str | None]:
    """`e<k>`'s closed options: `i<n>` per changed item, `m<n>` per
    mechanical entry, and `none`."""
    options: dict[str, str | None] = {
        f"i{row['item']}": f"the entry of `changed` with item = {row['item']}"
        for row in state["changed"]
    }
    for n, _ in enumerate(state["mechanical"]):
        options[f"m{n}"] = f"`mechanical` entry {n}"
    options[NONE] = "no single item"
    return options


def jc2_questions(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Three closed questions per requirement: `v<k>` the verdict, `w<k>` the
    reason code, `e<k>` the one item the verdict rests on."""
    options = jc2_evidence_options(state)
    questions: dict[str, dict[str, Any]] = {}
    for row in state["requirements"]:
        k = row["index"]
        questions[f"v{k}"] = {
            "type": "choice",
            "instructions": f"At `commit`, judging only `changed`, `mechanical` and `artifacts` "
                            f"as data, is `requirements` entry index {k} done?",
            "criteria": dict(JC2_VERDICTS),
        }
        questions[f"w{k}"] = {
            "type": "choice",
            "instructions": f"Which reason code best explains your verdict on `requirements` "
                            f"entry index {k}?",
            "criteria": {reason: None for reason in JC2_REASONS},
        }
        questions[f"e{k}"] = {
            "type": "choice",
            "instructions": f"Which one item does your verdict on `requirements` entry index "
                            f"{k} rest on?",
            "criteria": dict(options),
        }
    return questions


def _jc2_choice(answers: Any, name: str, options: Iterable[str]) -> tuple[str, float] | None:
    if not isinstance(answers, Mapping) or not isinstance(answers.get(name), Mapping):
        return None
    answer = answers[name]
    choice, probabilities = answer.get("choice"), answer.get("probabilities")
    if not isinstance(choice, str) or choice not in set(options) \
            or not isinstance(probabilities, Mapping):
        return None
    p = probabilities.get(choice)
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) \
            or not 0.0 <= p <= 1.0:
        return None
    return choice, float(p)


def read_jc2(requirements: Sequence[tuple[int, str, str]], state: Mapping[str, Any],
             answers: Any) -> list[dict[str, Any]]:
    """One verdict per requirement asked, `{requirement_id, verdict, reason,
    evidence_item, p}`, the route's `VerdictIn`. Malformed reads as
    `cannot_judge`: no answer, a choice outside the closed lists, no
    probability, an item that was not in the state, or `none` for a `met`;
    a `met` or an `unmet` under its threshold is `cannot_judge`, its p kept."""
    options = jc2_evidence_options(state)
    out = []
    for k, rid, _text in requirements:
        verdict = _jc2_choice(answers, f"v{k}", JC2_VERDICTS)
        reason = _jc2_choice(answers, f"w{k}", JC2_REASONS)
        item = _jc2_choice(answers, f"e{k}", options)
        if verdict is None or reason is None or item is None \
                or (verdict[0] == "met" and item[0] == NONE):
            out.append({"requirement_id": rid, "verdict": "cannot_judge", "reason": None,
                        "evidence_item": None, "p": 0.0})
            continue
        name, p = verdict
        if (name == "met" and p < JC2_MET_P) or (name == "unmet" and p < JC2_UNMET_P):
            name = "cannot_judge"
        out.append({"requirement_id": rid, "verdict": name, "reason": reason[0],
                    "evidence_item": None if item[0] == NONE else item[0], "p": p})
    return out


def jc2_state_sha256(state: Mapping[str, Any]) -> str:
    """The state's digest as the route stores it (`contractreask.state_digest`)."""
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()


_NAME_STATUS = {"A": "added", "C": "added", "D": "deleted", "M": "modified", "R": "renamed",
                "T": "modified"}
#: The git calls' own bound: JC2 never waits past its deadline (invariant 4).
GIT_TIMEOUT_SECONDS = 20
#: The most of `git diff` read: the state holds at most 70,000 characters.
MAX_DIFF_BYTES = 4 * 1024 * 1024


def _git(checkout: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True,
                              timeout=GIT_TIMEOUT_SECONDS, check=False,
                              env={**os.environ, "GIT_PAGER": "cat"})
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout[:MAX_DIFF_BYTES] if done.returncode == 0 else None


def head_commit(checkout: Path) -> str | None:
    """The checkout's HEAD as 40 hex, or None."""
    raw = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    text = raw.decode("ascii", "replace").strip() if raw is not None else ""
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else None


def changed_from_checkout(checkout: Path, base: str, commit: str) -> list[dict[str, Any]] | None:
    """The commit's diff against `base`, file by file, `{path, status, hunk}`
    (the hunk from its first `@@`), from the checkout alone; None when git
    cannot say. Read from the repository's objects, never the working tree,
    so it is the diff OF THE COMMIT judged."""
    names = _git(checkout, "diff", "--no-color", "--no-ext-diff", "--name-status", "-z", "-M",
                 base, commit, "--")
    if names is None:
        return None
    fields = names.decode("utf-8", "replace").split("\0")
    rows: list[dict[str, Any]] = []
    i = 0
    while i < len(fields) and fields[i]:
        code = fields[i][:1]
        if code in ("R", "C") and i + 2 < len(fields):
            path, i = fields[i + 2], i + 3
        elif i + 1 < len(fields):
            path, i = fields[i + 1], i + 2
        else:
            break
        rows.append({"path": path, "status": _NAME_STATUS.get(code, "modified"), "hunk": ""})
    patch = _git(checkout, "diff", "--no-color", "--no-ext-diff", "-M", "-U3", base, commit, "--")
    by_path: dict[str, str] = {}
    if patch is not None:
        for chunk in re.split(r"(?m)^(?=diff --git )", patch.decode("utf-8", "replace")):
            header, _, body = chunk.partition("\n")
            at = body.find("@@")
            for row in rows:
                if header.endswith(" b/" + row["path"]) and row["path"] not in by_path:
                    by_path[row["path"]] = body[at:at + HUNK_CHARS] if at >= 0 else ""
                    break
    for row in rows:
        row["hunk"] = by_path.get(row["path"], "")
    return rows
