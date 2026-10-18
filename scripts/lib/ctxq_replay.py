#!/usr/bin/env python3
"""The context-quality offline replay (lane CTXQ-E): docs/design/context-quality.md
§6.1, computed from what scripts/ctxq-replay.sh recorded.

    ctxq_replay.py plan     --baseline DIR [--signal N] [--controls N]   corpus and pilot, as JSON
    ctxq_replay.py owed     --dir DIR --arm A|B|any                      `lane count` per line
    ctxq_replay.py spec     --dir DIR --lane N --arm A|B|any             one workflow spec
    ctxq_replay.py tasks    --dir DIR                                    `task_id` of finished steps
    ctxq_replay.py wanted   --dir DIR                                    `task_id name` to read
    ctxq_replay.py records  --dir DIR                                    one record per submission
    ctxq_replay.py slots    --dir DIR                                    `lane arm workflow base head`
    ctxq_replay.py refspecs --records FILE                               `sha:refs/heads/replay/N/X`
    ctxq_replay.py score    --dir DIR [--json]                           the paired report

WHY. §6 judges the context pipeline on the same briefs at the same base
commits, with and without it, before any of it becomes a default (§6.3, §8
Q8, Q9). A comparison nobody can re-run is a comparison nobody can check, so
the pilot's choice of lanes, the workflow each arm runs, the arm each run
landed on and every score are decided here, and the shell script only fetches
and submits. tests/unit/scripts/test_ctxq_replay.py holds each rule.

PURE. Nothing here reads the network or writes outside stdout.

THE RULES, and the choices behind each one:

  CORPUS   The lanes of a quality-baseline directory (`scripts/quality-baseline.sh
           --out DIR`, lane CTXQ-M), split with ITS definitions, imported from
           quality_metrics.py, not restated: a lane is SIGNAL when it went red
           (M2), drew NOT_YET (M4) or was fixed by hand (M6), and a CONTROL
           otherwise (§6.1: 45 signal, 105 controls). A lane is replayable
           only with a full 40-character `base commit:` in its body (an
           abbreviated one is what the clone cannot fetch, validation
           `check_repository_ref`) and a `swarm/task_*` head to read its brief
           from.
  CLASSES  §3.1's table, by PR number (`SECTION3_CLASSES`): the root-cause
           classes were read by hand from fix commits and cannot be derived.
  PILOT    §6.1: 30 signal lanes across G, T, X and P, plus 10 controls. The
           signal lanes are taken ROUND-ROBIN over the four classes, so one
           class cannot fill the pilot, then topped up from the other signal
           lanes. Within a class, and for the controls, lanes are ordered by
           `order_key`: sha256 of the harness version and the PR number, so
           the pilot is identical however and however often the corpus is
           read, and never the "first 30" of whatever page order GitHub served.
           A short corpus is reported short, never padded.
  SPEC     One `swarm_workflow` per lane per arm, at the lane's base commit,
           with the lane's brief unchanged: `implement`, then `review`
           (verdict.json) and `score` (score.json), both from the
           implementer's branch. Strategy `collect` with `carrier: branches`:
           every step pushes `swarm/<its task>` and NOTHING opens a pull
           request (design/dispatch-and-integration.md §4.2), so auto-merge
           never sees a replay. A brief's `issue` is dropped: the worker would
           fetch the issue as it is now, closed, with the merged PR in its
           comments, which hands both arms the answer.
  ARMS     The caller cannot choose whether a step is staged (§4.7, invariant
           10; agent_worker.graphstage.decide reads the stored setting, then
           sha256(task_id) under the A/B), so a replay is LABELLED BY THE ARM
           IT GOT, read from the implementer's `context-pack.json`: no pack is
           arm A (nothing staged, today's runner), a pack names its arm, and a
           pack with none was staged outside the experiment (arm B). Asked for
           A and given B is recorded as `arm_mismatch` and still counts as B.
           An unread listing is UNKNOWN, never A. The earliest finished
           submission per lane and arm takes the slot; later ones are surplus.
  BRANCHES `replay/<lane>/<arm>`, and nothing else. The worker pushes only under
           its own prefix (`swarm/`), so the script mirrors each slot's
           implementer head there: a full sha to `refs/heads/replay/N/A|B`,
           never forced, never a delete. `check_refspec` refuses every other
           shape, and the script checks again before it pushes.
  SCORE    Per arm, from score.json (`SCORE_SCHEMA`), which the score step
           writes after running the CI-equivalent commands and each
           historic-red test on its own:
             * CI-equivalent pass: every command that ran exited 0;
             * historic-red recurrence: the tests that went red on the lane in
               history -- the pytest node ids and test files its `## Continuation`
               sections quote, plus the test files its fix commits (non-merge,
               after the first red run) edited -- recur when any fails; a test
               the score does not report is UNKNOWN, never passed;
             * the review's verdict and its findings;
             * similarity to the merged change: Jaccard of the changed files,
               and of the changed lines (`hunk_overlap`);
             * cost, wall time, pack tokens and Jev calls.
           The pair statistics are McNemar's exact test on the discordant
           pairs, and the 150-pair run's gate is §8 Q8's: at least a 20%
           relative drop in historic-red recurrence, B against A.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "swarm.ctxq-replay/v1"
#: Part of every `order_key`: changing it reshuffles every pilot ever planned.
HARNESS = "ctxq-replay/v1"
#: What the score step writes (`score.json`), checked before it is read.
SCORE_SCHEMA = "swarm.ctxq-score/v1"
#: `agent_worker.contextpack.build.SCHEMA` and `layout.ARTIFACT_NAME`.
PACK_SCHEMA = "swarm.context-pack/v1"
PACK_ARTIFACT = "context-pack.json"
VERDICT_ARTIFACT = "verdict.json"
SCORE_ARTIFACT = "score.json"
#: The patch every attempt with a repository uploads (docs/workflows.md).
PATCH_ARTIFACT = "swarm-work.patch"

ARM_A, ARM_B, ANY = "A", "B", "any"
ARMS = (ARM_A, ARM_B)
BRANCH_ROOT = "replay"
STEP_IMPLEMENT, STEP_REVIEW, STEP_SCORE = "implement", "review", "score"
#: The artifact each step's record is read from.
WANTED = {STEP_IMPLEMENT: PACK_ARTIFACT, STEP_REVIEW: VERDICT_ARTIFACT, STEP_SCORE: SCORE_ARTIFACT}
#: Task states after which a step's artifacts are final (swarm_common.states).
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED", "SKIPPED")

#: §6.1's pilot: 30 signal lanes across G, T, X and P, plus 10 controls.
PILOT_CLASSES = ("G", "T", "X", "P")
PILOT_SIGNAL = 30
PILOT_CONTROLS = 10
#: §8 Q8: the 150-pair run goes ahead only on at least this relative drop in
#: historic-red recurrence, arm B against arm A.
FULL_RUN_DROP = 0.20

#: §3.1's root-cause classes by PR, as the design read them on 2026-10-14
#: from each red run's fix commit and `## Continuation` section. Only the
#: context classes, and TA, which the pilot does not stratify on but reports.
SECTION3_CLASSES: dict[str, tuple[int, ...]] = {
    "G": (1113, 1141, 1148, 1149, 1164, 1215, 1217, 1221, 1255, 1261, 1271, 1273),
    "P": (1142, 1167, 1169, 1198, 1262, 1266),
    "T": (1105, 1114, 1141, 1146, 1162, 1217, 1248),
    "X": (1107, 1132, 1181, 1273, 1293),
    "W": (1148, 1179),
    "D": (1151, 1152),
    "TA": (1105, 1180, 1181),
}
CLASS_ORDER = ("G", "P", "T", "X", "W", "D", "TA")

_SHA = re.compile(r"^[0-9a-f]{40}$")
_BASE = re.compile(r"^- base commit: `([0-9a-f]+)`", re.MULTILINE)
_HEADING = re.compile(r"^## ", re.MULTILINE)
_CONTINUATION = re.compile(r"^## Continuation \(")
#: A pytest node id (`path.py::name`, `path.py::Class::name`); a `[param]`
#: is not part of it. swarm_api.reviewcontext reads CI excerpts the same way.
_PYTEST_ID = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.py)::([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)")
#: Any repository path; kept only when it names a test file.
_ANY_PATH = re.compile(r"(?<![\w/:.-])((?:[\w.-]+/)+[\w.-]*\w\.[A-Za-z0-9]{1,8})(?![\w/])")
_REFSPEC = re.compile(r"^([0-9a-f]{40}):refs/heads/replay/([1-9][0-9]*)/([AB])$")
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$")


class ReplayRefused(ValueError):
    """A replay this harness will not submit, push or score, and why."""


# --------------------------------------------------------------------------
# the baseline's definitions, imported rather than restated
# --------------------------------------------------------------------------

def _quality_metrics() -> Any:
    loaded = sys.modules.get("quality_metrics")
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().with_name("quality_metrics.py")
    spec = importlib.util.spec_from_file_location("quality_metrics", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["quality_metrics"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# small readers
# --------------------------------------------------------------------------

def _at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _lane_number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ReplayRefused(f"a lane is a PR number, not {value!r}")
    text = str(value)
    if not text.isdigit() or int(text) < 1:
        raise ReplayRefused(f"a lane is a positive PR number, not {value!r}")
    return int(text)


def is_test_path(path: str) -> bool:
    """A Python test module, or a vitest/node test file of the UI."""
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    if re.search(r"\.(ts|tsx|js|jsx|mjs|cjs)$", name):
        return (".test." in name or ".spec." in name or "/__tests__/" in f"/{path}"
                or path.startswith("apps/swarm-ui/tests/"))
    return False


def base_commit(body: str | None) -> str | None:
    """The full base sha the worker wrote into the PR body; else None."""
    match = _BASE.search(body or "")
    if not match or not _SHA.match(match.group(1)):
        return None
    return match.group(1)


def _continuation_text(body: str | None) -> list[tuple[int, str]]:
    """Each `## Continuation (...)` section of a body, with its offset."""
    text = body or ""
    starts = [m.start() for m in _HEADING.finditer(text)] + [len(text)]
    return [(start, text[start:end]) for start, end in zip(starts, starts[1:])
            if _CONTINUATION.match(text[start:end])]


def historic_red_tests(body: str | None, *, commits: Iterable[Mapping[str, Any]] | None = None,
                       first_red_at: str | None = None) -> list[str]:
    """The tests that went red on a lane in history, in the order first named.

    From the body: the pytest node ids and test files its `## Continuation`
    sections quote (a CI fixer's own account of the red run). A test the
    implementer's own text names is NOT one: it never went red. From the
    commits, when given: the test files a fix commit -- non-merge, after the
    first red run -- edited.
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    files: set[str] = set()
    for offset, section in _continuation_text(body):
        ids = []
        for match in _PYTEST_ID.finditer(section):
            path = match.group(1).removeprefix("./")
            if ".." in path.split("/") or not is_test_path(path):
                continue
            ids.append((offset + match.start(), f"{path}::{match.group(2)}"))
            files.add(path)
        for match in _ANY_PATH.finditer(section):
            path = match.group(1).removeprefix("./")
            if ".." in path.split("/") or path in files or not is_test_path(path):
                continue
            ids.append((offset + match.start(), path))
        for position, test in ids:
            if test not in seen:
                seen.add(test)
                found.append((position, test))
    out = [test for _, test in sorted(found)]
    red_at = _at(first_red_at)
    if commits is not None and red_at is not None:
        for commit in commits:
            if len(commit.get("parents") or ()) >= 2:
                continue
            when = _at(commit.get("date"))
            if when is None or when <= red_at:
                continue
            for path in commit.get("files") or ():
                if is_test_path(path) and path not in seen and path not in files:
                    seen.add(path)
                    out.append(path)
    return out


def _classes(number: int) -> list[str]:
    return [c for c in CLASS_ORDER if number in SECTION3_CLASSES.get(c, ())]


def merged_files(commits: Iterable[Mapping[str, Any]] | None) -> list[str] | None:
    """The files a lane's own (non-merge) commits changed; None when unread."""
    if commits is None:
        return None
    out: list[str] = []
    for commit in commits:
        if len(commit.get("parents") or ()) >= 2:
            continue
        for path in commit.get("files") or ():
            if path not in out:
                out.append(path)
    return sorted(out)


# --------------------------------------------------------------------------
# the corpus and the pilot
# --------------------------------------------------------------------------

def build_corpus(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per lane of a quality-baseline bundle, with CTXQ-M's split."""
    qm = _quality_metrics()
    lanes = list(bundle.get("lanes") or [])
    report = qm.build_report(bundle)
    per_pr = {p["number"]: p for p in report["ci"]["per_pr"]}
    not_yet = set(report["review"]["m4_not_yet"])
    hand = set(report["hand_fixup"]["m6_hand_fixed"])
    commits_by = bundle.get("commits") or {}
    rows = []
    for pr in sorted(lanes, key=lambda p: int(p["number"])):
        number = int(pr["number"])
        ci = per_pr.get(number) or {}
        why = [w for w, hit in (("red", bool(ci.get("red_runs"))), ("not_yet", number in not_yet),
                                ("hand_fixup", number in hand)) if hit]
        commits = commits_by.get(number)
        base = base_commit(pr.get("body"))
        task_id = qm.task_id_of(pr)
        problems = []
        if base is None:
            problems.append("no full 40-character base commit in its body")
        if task_id is None:
            problems.append("its head is not a swarm/task_* branch, so it has no brief to read")
        rows.append({
            "number": number,
            "title": pr.get("title"),
            "head": (pr.get("head") or {}).get("ref"),
            "task_id": task_id,
            "base": base,
            "signal": bool(why),
            "why": why,
            "classes": _classes(number),
            "historic_red": historic_red_tests(pr.get("body"), commits=commits,
                                               first_red_at=ci.get("first_red_at")),
            "merged_files": merged_files(commits),
            "replayable": not problems,
            "not_replayable": "; ".join(problems) or None,
        })
    return rows


def order_key(number: int) -> str:
    """The lane's place in every selection: a hash of the lane, not its position."""
    return hashlib.sha256(f"{HARNESS}:{int(number)}".encode("utf-8")).hexdigest()


def select_pilot(corpus: Iterable[Mapping[str, Any]], signal: int = PILOT_SIGNAL,
                 controls: int = PILOT_CONTROLS) -> dict[str, Any]:
    """§6.1's pilot: round-robin over G, T, X and P, then the rest, then controls."""
    rows = {int(r["number"]): r for r in corpus if r.get("replayable")}
    ranked = sorted(rows, key=order_key)
    signal_lanes = [n for n in ranked if rows[n].get("signal")]
    queues = {c: [n for n in signal_lanes if c in (rows[n].get("classes") or ())] for c in PILOT_CLASSES}
    picked: list[int] = []
    while len(picked) < signal and any(queues.values()):
        for cls in PILOT_CLASSES:
            queue = queues[cls]
            while queue and queue[0] in picked:
                queue.pop(0)
            if queue and len(picked) < signal:
                picked.append(queue.pop(0))
    for n in signal_lanes:
        if len(picked) >= signal:
            break
        if n not in picked:
            picked.append(n)
    chosen_controls = [n for n in ranked if not rows[n].get("signal")][:controls]
    pairs = []
    for n in sorted(picked + chosen_controls):
        pairs.append({
            "lane": n,
            "group": "signal" if n in picked else "control",
            "classes": list(rows[n].get("classes") or []),
            "branches": {arm: replay_branch(n, arm) for arm in ARMS},
        })
    return {
        "schema": SCHEMA,
        "harness": HARNESS,
        "requested": {"signal": signal, "controls": controls},
        "signal": sorted(picked),
        "controls": sorted(chosen_controls),
        "short": {"signal": max(0, signal - len(picked)),
                  "controls": max(0, controls - len(chosen_controls))},
        "pairs": pairs,
    }


# --------------------------------------------------------------------------
# branches: replay/<lane>/<arm>, and nothing else
# --------------------------------------------------------------------------

def replay_branch(lane: Any, arm: str) -> str:
    number = _lane_number(lane)
    if arm not in ARMS:
        raise ReplayRefused(f"an arm is one of {', '.join(ARMS)}, not {arm!r}")
    return f"{BRANCH_ROOT}/{number}/{arm}"


def replay_refspec(sha: str, lane: Any, arm: str) -> str:
    """`<sha>:refs/heads/replay/<lane>/<arm>`: a new branch at a commit, unforced."""
    spec = f"{sha}:refs/heads/{replay_branch(lane, arm)}"
    check_refspec(spec)
    return spec


def check_refspec(spec: str) -> None:
    """Refuse anything but a full sha pushed, unforced, to a replay branch."""
    if not _REFSPEC.match(spec or ""):
        raise ReplayRefused(
            f"refusing to push {spec!r}: a replay pushes only `<40-hex sha>:refs/heads/"
            f"{BRANCH_ROOT}/<lane>/<A|B>`, never forced, never a delete")


# --------------------------------------------------------------------------
# the workflow each arm runs
# --------------------------------------------------------------------------

REVIEW_PROMPT = (
    "Review the change on this branch: swarm-work.patch is its diff against the commit it "
    "started from. Do not edit, create or delete any file in the repository. Read the code "
    "the patch touches and its callers and tests, then write $SWARM_ARTIFACTS_DIR/verdict.json "
    "as exactly {\"verdict\": \"MERGE\" or \"NOT_YET\", \"findings\": [\"one blocker per "
    "entry\"]}. NOT_YET only for a defect that should block the merge: a bug, a test that "
    "cannot fail, a broken caller, a guard or house rule in CLAUDE.md not met."
)

#: What the score step runs, by name. The commands are what application.yml
#: runs for a pull request, minus integration and images (§6.1).
SCORE_COMMANDS = (
    ("unit", "uv run pytest tests/unit -q -n auto -p no:cacheprovider"),
    ("ui-install", "(cd apps/swarm-ui && npm ci --no-audit --no-fund)"),
    ("vitest", "(cd apps/swarm-ui && npx vitest run)"),
    ("tsc", "(cd apps/swarm-ui && npx tsc --noEmit)"),
    ("terraform-fmt", "terraform fmt -check -recursive terraform"),
)


def score_prompt(historic_red: Sequence[str]) -> str:
    lines = [
        "This step scores an offline replay (docs/design/context-quality.md section 6.1). Do not "
        "edit, create or delete any file in the repository, and never fix a failure: report it.",
        "",
        "From the repository root, run each command below in order, whatever the one before it "
        "returned, and record its exit code. Record exit_code null for a command whose tool is "
        "not installed.",
    ]
    lines += [f"  {name}: {command}" for name, command in SCORE_COMMANDS]
    lines += ["", "Then run each test below on its own and record its outcome: passed, failed, "
              "error (it could not be collected or run), or not_found (the file or the test does "
              "not exist at this commit). A `.py` entry runs as `uv run pytest '<entry>' -q -p "
              "no:cacheprovider`; any other entry runs as `(cd apps/swarm-ui && npx vitest run "
              "<entry without its apps/swarm-ui/ prefix>)`."]
    lines += [f"  {test}" for test in historic_red] or ["  (none)"]
    lines += ["", "Write $SWARM_ARTIFACTS_DIR/score.json as exactly:",
              json.dumps({"schema": SCORE_SCHEMA,
                          "commands": [{"name": SCORE_COMMANDS[0][0], "exit_code": 0}],
                          "historic_red": [{"test": "<the entry as listed>", "outcome": "passed"}]}),
              "with one `commands` row per command above and one `historic_red` row per test above."]
    return "\n".join(lines)


def workflow_spec(lane: Mapping[str, Any], *, brief: Mapping[str, Any], repository_url: str,
                  intended_arm: str) -> dict[str, Any]:
    """The replay of one lane on one arm. Opens no pull request (see SPEC above)."""
    number = _lane_number(lane.get("number"))
    base = lane.get("base")
    if not isinstance(base, str) or not _SHA.match(base):
        raise ReplayRefused(f"lane {number} has no full base commit to replay from")
    prompt = brief.get("prompt") if isinstance(brief, Mapping) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise ReplayRefused(f"lane {number}'s brief has no prompt to replay")
    if intended_arm not in (*ARMS, ANY):
        raise ReplayRefused(f"an arm is A, B or any, not {intended_arm!r}")
    if not repository_url:
        raise ReplayRefused(f"lane {number} has no repository to replay on")
    return {
        "strategy": "collect",
        "carrier": "branches",
        "repository_url": repository_url,
        "repository_ref": base,
        "on_step_failure": "continue",
        "metadata": {"ctxq_replay": {"harness": HARNESS, "lane": number,
                                     "intended_arm": intended_arm,
                                     "replays_task": lane.get("task_id")}},
        "steps": [
            {"step_id": STEP_IMPLEMENT, "runner_profile": "claude-code",
             "input": {"prompt": prompt}},
            {"step_id": STEP_REVIEW, "runner_profile": "claude-code",
             "depends_on": [STEP_IMPLEMENT], "input_from": {STEP_IMPLEMENT: PATCH_ARTIFACT},
             "allow_empty_diff": True, "input": {"prompt": REVIEW_PROMPT}},
            {"step_id": STEP_SCORE, "runner_profile": "claude-code",
             "depends_on": [STEP_IMPLEMENT], "allow_empty_diff": True,
             "input": {"prompt": score_prompt(list(lane.get("historic_red") or []))}},
        ],
    }


# --------------------------------------------------------------------------
# what each replay got: arm, slot, what is still owed
# --------------------------------------------------------------------------

def observed_arm(pack: Mapping[str, Any] | None, *, listed: bool) -> str | None:
    """The arm staging gave the implementer, from its `context-pack.json`."""
    if not listed:
        return None
    if pack is None:
        return ARM_A
    arm = pack.get("arm")
    return arm if arm in ARMS else ARM_B


def _record_order(record: Mapping[str, Any]) -> tuple[str, str]:
    return (str(record.get("submitted_at") or ""), str(record.get("workflow_id") or ""))


def assign_slots(records: Iterable[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per lane: the A and B slots, by observed arm; surplus, unusable, pending."""
    slots: dict[int, dict[str, Any]] = {}
    for record in sorted(records, key=_record_order):
        lane = _lane_number(record.get("lane"))
        entry = slots.setdefault(lane, {"surplus": [], "unusable": [], "pending": []})
        if not record.get("done", True):
            entry["pending"].append(dict(record))
            continue
        implement = record.get("implement") or {}
        arm = record.get("observed_arm")
        head = implement.get("head")
        if implement.get("state") != "SUCCEEDED" or not isinstance(head, str) or not _SHA.match(head):
            entry["unusable"].append({**record, "why": "the implementer published no head"})
            continue
        if arm not in ARMS:
            entry["unusable"].append({**record, "why": "the arm it ran on was not read"})
            continue
        intended = record.get("intended_arm")
        slot = {**record, "arm_mismatch": intended in ARMS and intended != arm}
        if arm in entry:
            entry["surplus"].append(slot)
        else:
            entry[arm] = slot
    return slots


def owed_counts(pilot: Mapping[str, Any], slots: Mapping[int, Mapping[str, Any]],
                arm: str) -> list[tuple[int, int]]:
    """`(lane, submissions to make)` for the pilot's lanes still short of `arm`."""
    out = []
    for pair in pilot.get("pairs") or []:
        lane = int(pair["lane"])
        entry = slots.get(lane) or {}
        missing = [a for a in ARMS if a not in entry]
        pending = [p.get("intended_arm") for p in entry.get("pending") or []]
        if arm == ANY:
            count = len(missing) - len(pending)
        else:
            count = 1 if arm in missing and arm not in pending else 0
        if count > 0:
            out.append((lane, count))
    return out


def owed(pilot: Mapping[str, Any], slots: Mapping[int, Mapping[str, Any]], arm: str) -> list[int]:
    return [lane for lane, _ in owed_counts(pilot, slots, arm)]


def mirror_refspecs(slots: Mapping[int, Mapping[str, Any]]) -> list[str]:
    """The refspecs that put each slot's implementer head on its replay branch."""
    out = []
    for lane in sorted(slots):
        for arm in ARMS:
            slot = slots[lane].get(arm)
            if slot:
                out.append(replay_refspec(slot["implement"]["head"], lane, arm))
    return out


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def jaccard(a: Iterable[Any], b: Iterable[Any]) -> float | None:
    left, right = set(a), set(b)
    if not left and not right:
        return None
    return round(len(left & right) / len(left | right), 4)


def patch_lines(patch: str) -> set[tuple[str, str, str]]:
    """`(file, +/-, text)` for each changed, non-blank line of a unified diff."""
    out: set[tuple[str, str, str]] = set()
    current = None
    for line in (patch or "").splitlines():
        match = _DIFF_FILE.match(line)
        if match:
            current = match.group(1)
            continue
        if line.startswith(("+++", "---")) or current is None:
            continue
        if line[:1] in ("+", "-") and line[1:].strip():
            out.add((current, line[0], line[1:].strip()))
    return out


def hunk_overlap(patch_a: str | None, patch_b: str | None) -> float | None:
    if patch_a is None or patch_b is None:
        return None
    return jaccard(patch_lines(patch_a), patch_lines(patch_b))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, round(2 * tail, 6))


def full_run_gate(recurred_a: int, recurred_b: int) -> dict[str, Any]:
    """§8 Q8: the 150-pair run only on a >= 20% relative drop, B against A."""
    if recurred_a <= 0:
        return {"gate": "not measured", "relative_drop": None, "threshold": FULL_RUN_DROP}
    drop = (recurred_a - recurred_b) / recurred_a
    return {"gate": "pass" if drop + 1e-9 >= FULL_RUN_DROP else "fail",
            "relative_drop": round(drop, 4), "threshold": FULL_RUN_DROP}


def _valid_score(score: Any) -> Mapping[str, Any] | None:
    if not isinstance(score, Mapping) or score.get("schema") != SCORE_SCHEMA:
        return None
    if not isinstance(score.get("commands"), list) or not isinstance(score.get("historic_red"), list):
        return None
    return score


def historic_outcome(tests: Sequence[str], score: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if score is None or not tests:
        return None
    reported = {}
    for row in score.get("historic_red") or []:
        if isinstance(row, Mapping) and isinstance(row.get("test"), str):
            reported[row["test"]] = str(row.get("outcome") or "").lower()
    passed = sum(1 for t in tests if reported.get(t) == "passed")
    failed = sum(1 for t in tests if reported.get(t) in ("failed", "error"))
    unknown = len(tests) - passed - failed
    recurred = True if failed else (False if unknown == 0 else None)
    return {"tests": len(tests), "passed": passed, "failed": failed, "unknown": unknown,
            "recurred": recurred}


def ci_pass(score: Mapping[str, Any] | None) -> bool | None:
    if score is None:
        return None
    ran = [r for r in score.get("commands") or [] if isinstance(r, Mapping)
           and isinstance(r.get("exit_code"), int) and not isinstance(r.get("exit_code"), bool)]
    if not ran:
        return None
    return all(r["exit_code"] == 0 for r in ran)


def _verdict(verdict: Any) -> tuple[str | None, int | None]:
    if not isinstance(verdict, Mapping):
        return None, None
    word = str(verdict.get("verdict") or "").strip().upper()
    findings = verdict.get("findings")
    return (word if word in ("MERGE", "NOT_YET") else None,
            len(findings) if isinstance(findings, list) else None)


def score_arm(lane: Mapping[str, Any], slot: Mapping[str, Any] | None,
              merged_patch: str | None = None) -> dict[str, Any] | None:
    if not slot:
        return None
    score = _valid_score(slot.get("score"))
    verdict, findings = _verdict(slot.get("verdict"))
    merged = lane.get("merged_files")
    pack = slot.get("pack") or {}
    return {
        "workflow_id": slot.get("workflow_id"),
        "branch": replay_branch(lane["number"], slot["observed_arm"]),
        "head": (slot.get("implement") or {}).get("head"),
        "arm_mismatch": bool(slot.get("arm_mismatch")),
        "ci_pass": ci_pass(score),
        "historic_red": historic_outcome(list(lane.get("historic_red") or []), score),
        "verdict": verdict,
        "findings": findings,
        "similarity": {
            "files_jaccard": jaccard(slot.get("changed_files") or [], merged) if merged is not None else None,
            "hunk_overlap": hunk_overlap(slot.get("patch"), merged_patch),
        },
        "cost_usd": slot.get("cost_usd"),
        "cost_is_floor": bool(slot.get("cost_is_floor")),
        "duration_s": slot.get("duration_s"),
        "pack_tokens": pack.get("tokens"),
        "jev_calls": pack.get("jev_calls"),
    }


def _paired(pairs: Sequence[Mapping[str, Any]], read) -> dict[str, Any]:
    both = [(read(p["arms"]["A"]), read(p["arms"]["B"])) for p in pairs
            if p["arms"]["A"] and p["arms"]["B"]]
    both = [(a, b) for a, b in both if a is not None and b is not None]
    a_only = sum(1 for a, b in both if a and not b)
    b_only = sum(1 for a, b in both if b and not a)
    return {"pairs": len(both), "A": sum(1 for a, _ in both if a), "B": sum(1 for _, b in both if b),
            "discordant_A_only": a_only, "discordant_B_only": b_only,
            "mcnemar_p": mcnemar_exact(a_only, b_only)}


def _mean(values: Iterable[Any]) -> float | None:
    got = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(sum(got) / len(got), 4) if got else None


def score(corpus: Iterable[Mapping[str, Any]], pilot: Mapping[str, Any],
          slots: Mapping[int, Mapping[str, Any]],
          merged_patches: Mapping[int, str] | None = None) -> dict[str, Any]:
    """The paired report over the pilot's lanes."""
    rows = {int(r["number"]): r for r in corpus}
    pairs, incomplete = [], []
    for planned in pilot.get("pairs") or []:
        lane = int(planned["lane"])
        row = rows.get(lane) or {"number": lane, "historic_red": [], "merged_files": None}
        entry = slots.get(lane) or {}
        arms = {arm: score_arm(row, entry.get(arm), (merged_patches or {}).get(lane)) for arm in ARMS}
        missing = [arm for arm in ARMS if arms[arm] is None]
        if missing:
            incomplete.append({"lane": lane, "missing": missing,
                               "pending": len(entry.get("pending") or []),
                               "unusable": [u.get("why") for u in entry.get("unusable") or []]})
        pairs.append({"lane": lane, "group": planned.get("group"),
                      "classes": list(planned.get("classes") or []), "arms": arms})
    complete = [p for p in pairs if all(p["arms"].values())]
    signal = [p for p in complete if p["group"] == "signal"]
    controls = [p for p in complete if p["group"] == "control"]
    red = _paired(complete, lambda arm: None if arm["ci_pass"] is None else not arm["ci_pass"])
    recur = _paired(signal, lambda arm: (arm["historic_red"] or {}).get("recurred"))
    not_yet = _paired(complete, lambda arm: None if arm["verdict"] is None else arm["verdict"] == "NOT_YET")
    summary = {
        "planned": len(pairs),
        "complete": len(complete),
        "incomplete": incomplete,
        "arm_mismatches": sum(1 for p in complete for a in p["arms"].values() if a["arm_mismatch"]),
        "ci_red": red,
        "ci_red_controls": _paired(controls, lambda arm: None if arm["ci_pass"] is None
                                   else not arm["ci_pass"]),
        "historic_red": {
            "pairs": recur["pairs"], "recurred_A": recur["A"], "recurred_B": recur["B"],
            "discordant_A_only": recur["discordant_A_only"],
            "discordant_B_only": recur["discordant_B_only"],
            "mcnemar_p": recur["mcnemar_p"],
            "full_run": full_run_gate(recur["A"], recur["B"]),
        },
        "not_yet": not_yet,
        "files_jaccard": {arm: _mean(p["arms"][arm]["similarity"]["files_jaccard"] for p in complete)
                          for arm in ARMS},
        "hunk_overlap": {arm: _mean(p["arms"][arm]["similarity"]["hunk_overlap"] for p in complete)
                         for arm in ARMS},
        "cost_usd": {arm: round(sum(p["arms"][arm]["cost_usd"] or 0 for p in complete), 2) for arm in ARMS},
        "cost_is_floor": any(a["cost_is_floor"] or a["cost_usd"] is None
                             for p in complete for a in p["arms"].values()),
        "duration_s_median": {arm: _median(p["arms"][arm]["duration_s"] for p in complete) for arm in ARMS},
        "pack_tokens_mean_B": _mean(p["arms"]["B"]["pack_tokens"] for p in complete),
        "jev_calls": sum(a["jev_calls"] or 0 for p in complete for a in p["arms"].values()),
    }
    return {"schema": SCHEMA, "harness": HARNESS, "summary": summary, "pairs": pairs}


def _median(values: Iterable[Any]) -> float | None:
    got = sorted(float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not got:
        return None
    mid = len(got) // 2
    return got[mid] if len(got) % 2 else (got[mid - 1] + got[mid]) / 2


def _na(value: Any) -> str:
    """A figure, or `n/a` when there was nothing to compute it from. 0 stays 0."""
    return "n/a" if value is None else str(value)


def render(report: Mapping[str, Any]) -> list[str]:
    s = report["summary"]
    red, rec, ny = s["ci_red"], s["historic_red"], s["not_yet"]
    full = rec["full_run"]
    lines = [
        f"Pairs: {s['complete']} complete of {s['planned']} planned; "
        f"{len(s['incomplete'])} incomplete; {s['arm_mismatches']} replay(s) ran on another arm than asked",
        "",
        f"CI-equivalent red      A {red['A']} / {red['pairs']}, B {red['B']} / {red['pairs']}; "
        f"discordant A-only {red['discordant_A_only']}, B-only {red['discordant_B_only']}, "
        f"McNemar p = {red['mcnemar_p']}",
        f"  controls             A {s['ci_red_controls']['A']} / {s['ci_red_controls']['pairs']}, "
        f"B {s['ci_red_controls']['B']} / {s['ci_red_controls']['pairs']}",
        f"Historic-red recurred  A {rec['recurred_A']} / {rec['pairs']}, B {rec['recurred_B']} / {rec['pairs']}; "
        f"discordant A-only {rec['discordant_A_only']}, B-only {rec['discordant_B_only']}, "
        f"McNemar p = {rec['mcnemar_p']}",
        f"  150-pair run gate    {full['gate']} (relative drop {_na(full['relative_drop'])}, "
        f"needs {full['threshold']})",
        f"Review NOT_YET         A {ny['A']} / {ny['pairs']}, B {ny['B']} / {ny['pairs']}",
        f"Files Jaccard (mean)   A {_na(s['files_jaccard']['A'])}, B {_na(s['files_jaccard']['B'])}",
        f"Hunk overlap (mean)    A {_na(s['hunk_overlap']['A'])}, B {_na(s['hunk_overlap']['B'])}",
        f"Cost                   {'at least ' if s['cost_is_floor'] else ''}"
        f"A ${s['cost_usd']['A']}, B ${s['cost_usd']['B']}; "
        f"wall p50 A {_na(s['duration_s_median']['A'])} s, B {_na(s['duration_s_median']['B'])} s",
        f"Pack                   mean {_na(s['pack_tokens_mean_B'])} tokens on B; {s['jev_calls']} Jev call(s)",
    ]
    for item in s["incomplete"]:
        why = f"; unusable: {', '.join(item['unusable'])}" if item["unusable"] else ""
        lines.append(f"  #{item['lane']} missing {', '.join(item['missing'])} "
                     f"({item['pending']} pending{why})")
    return lines


# --------------------------------------------------------------------------
# reading a recorded directory
# --------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_plan(root: Path) -> dict[str, Any]:
    plan = _read_json(root / "plan.json")
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise ReplayRefused(f"{root / 'plan.json'} is not a replay plan: run `plan` first")
    return plan


def read_submissions(root: Path) -> list[dict[str, Any]]:
    path = root / "submissions.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _content(root: Path, task_id: str, name: str) -> tuple[bool, Any]:
    """(read, value) of a fetched artifact: read False when its content was cut
    or not served; value None when it is not JSON."""
    envelope = _read_json(root / "artifacts" / task_id / f"{name}.response")
    if not isinstance(envelope, Mapping) or envelope.get("status") != "ok" or envelope.get("truncated"):
        return False, None
    try:
        return True, json.loads(envelope.get("content") or "")
    except ValueError:
        return True, None


def _listed(root: Path, task_id: str) -> list[str] | None:
    listing = _read_json(root / "artifacts" / task_id / "listing.json")
    if not isinstance(listing, Mapping) or not isinstance(listing.get("artifacts"), list):
        return None
    return [str(a.get("name")) for a in listing["artifacts"] if isinstance(a, Mapping)]


def _steps(root: Path, workflow_id: str) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]] | None:
    envelope = _read_json(root / "workflows" / f"{workflow_id}.json")
    if not isinstance(envelope, Mapping) or not isinstance(envelope.get("workflow"), Mapping):
        return None
    steps = {str(s.get("step_id")): str(s.get("task_id")) for s in envelope["workflow"].get("steps") or []
             if isinstance(s, Mapping) and s.get("task_id")}
    tasks = {str(t["id"]): t for t in envelope.get("tasks") or [] if isinstance(t, Mapping) and t.get("id")}
    for task_id in steps.values():
        fetched = _read_json(root / "tasks" / f"{task_id}.json")
        if isinstance(fetched, Mapping) and isinstance(fetched.get("task"), Mapping):
            tasks[task_id] = fetched["task"]
    return steps, tasks


def _terminal(task: Mapping[str, Any] | None) -> bool:
    return bool(task) and str(task.get("state") or "").upper() in TERMINAL


def finished_tasks(root: Path) -> list[str]:
    out = []
    for sub in read_submissions(root):
        got = _steps(root, sub["workflow_id"])
        if got is None:
            continue
        steps, tasks = got
        out += [tid for tid in steps.values() if _terminal(tasks.get(tid))]
    return sorted(set(out))


def wanted_artifacts(root: Path) -> list[tuple[str, str]]:
    out = []
    for sub in read_submissions(root):
        got = _steps(root, sub["workflow_id"])
        if got is None:
            continue
        steps, tasks = got
        for step_id, name in WANTED.items():
            task_id = steps.get(step_id)
            listed = _listed(root, task_id) if task_id else None
            if task_id and _terminal(tasks.get(task_id)) and listed and name in listed:
                if not (root / "artifacts" / task_id / f"{name}.response").exists():
                    out.append((task_id, name))
    return out


def build_records(root: Path) -> list[dict[str, Any]]:
    out = []
    for sub in read_submissions(root):
        workflow_id = str(sub["workflow_id"])
        record: dict[str, Any] = {
            "lane": int(sub["lane"]), "workflow_id": workflow_id,
            "intended_arm": sub.get("intended_arm"), "submitted_at": sub.get("submitted_at"),
            "done": False,
        }
        got = _steps(root, workflow_id)
        if got is None:
            out.append(record)
            continue
        steps, tasks = got
        record["done"] = bool(steps) and all(_terminal(tasks.get(t)) for t in steps.values())
        impl_id = steps.get(STEP_IMPLEMENT)
        impl = tasks.get(impl_id) or {}
        summary = impl.get("result_summary") if isinstance(impl.get("result_summary"), Mapping) else {}
        branch = summary.get("branch") if isinstance(summary.get("branch"), Mapping) else {}
        record["implement"] = {"task_id": impl_id, "state": str(impl.get("state") or "").upper() or None,
                               "head": branch.get("head"), "branch": branch.get("name")}
        listed = _listed(root, impl_id) if impl_id else None
        pack_read, pack = (_content(root, impl_id, PACK_ARTIFACT)
                           if listed and PACK_ARTIFACT in listed else (True, None))
        if listed is not None and PACK_ARTIFACT in listed and not isinstance(pack, Mapping):
            listed = None   # named but unreadable: the arm is unknown, never A
        record["observed_arm"] = observed_arm(pack if isinstance(pack, Mapping) else None,
                                              listed=listed is not None and pack_read)
        record["pack"] = ({"tokens": pack.get("tokens"), "jev_calls": pack.get("jev_calls"),
                           "arm": pack.get("arm")} if isinstance(pack, Mapping) else None)
        for step_id, key in ((STEP_REVIEW, "verdict"), (STEP_SCORE, "score")):
            task_id = steps.get(step_id)
            record[key] = _content(root, task_id, WANTED[step_id])[1] if task_id else None
        costs = [tasks.get(t, {}).get("cost_usd_total") for t in steps.values()]
        known = [float(c) for c in costs if isinstance(c, (int, float)) and not isinstance(c, bool)]
        record["cost_usd"] = round(sum(known), 4) if known else None
        record["cost_is_floor"] = len(known) < len(costs) or any(
            tasks.get(t, {}).get("cost_incomplete") for t in steps.values())
        walls = [tasks.get(t, {}).get("duration_s_total") for t in steps.values()]
        record["duration_s"] = (sum(float(w) for w in walls if isinstance(w, (int, float)))
                                if any(isinstance(w, (int, float)) for w in walls) else None)
        files = root / "diffs" / f"{workflow_id}.files"
        record["changed_files"] = ([line for line in files.read_text(encoding="utf-8").splitlines() if line]
                                   if files.exists() else None)
        patch = root / "diffs" / f"{workflow_id}.patch"
        record["patch"] = patch.read_text(encoding="utf-8", errors="replace") if patch.exists() else None
        out.append(record)
    return out


def merged_patches(root: Path) -> dict[int, str]:
    folder = root / "merged"
    if not folder.is_dir():
        return {}
    return {int(p.stem): p.read_text(encoding="utf-8", errors="replace")
            for p in folder.glob("*.patch") if p.stem.isdigit()}


def load_baseline(root: Path) -> dict[str, Any]:
    return _quality_metrics().load_dir(root)


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--baseline", type=Path, required=True)
    p_plan.add_argument("--signal", type=int, default=PILOT_SIGNAL)
    p_plan.add_argument("--controls", type=int, default=PILOT_CONTROLS)
    for name in ("owed", "spec"):
        p = sub.add_parser(name)
        p.add_argument("--dir", type=Path, required=True)
        p.add_argument("--arm", choices=(*ARMS, ANY), required=True)
        if name == "spec":
            p.add_argument("--lane", type=int, required=True)
    for name in ("tasks", "wanted", "records", "slots"):
        sub.add_parser(name).add_argument("--dir", type=Path, required=True)
    p_refs = sub.add_parser("refspecs")
    p_refs.add_argument("--records", type=Path, required=True)
    p_score = sub.add_parser("score")
    p_score.add_argument("--dir", type=Path, required=True)
    p_score.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        return _run(args)
    except ReplayRefused as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 2


def _dump(value: Any) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _run(args: argparse.Namespace) -> int:
    if args.command == "plan":
        bundle = load_baseline(args.baseline)
        if not bundle["lanes"]:
            print("plan: no lane PR was recorded in the baseline, so there is nothing to replay",
                  file=sys.stderr)
            return 2
        corpus = build_corpus(bundle)
        _dump({"schema": SCHEMA, "harness": HARNESS, "corpus": corpus,
               "pilot": select_pilot(corpus, args.signal, args.controls)})
        return 0
    if args.command == "refspecs":
        records = json.loads(args.records.read_text(encoding="utf-8"))
        for spec in mirror_refspecs(assign_slots(records)):
            print(spec)
        return 0

    root = args.dir
    if args.command == "tasks":
        print("\n".join(finished_tasks(root)))
        return 0
    if args.command == "wanted":
        for task_id, name in wanted_artifacts(root):
            print(task_id, name)
        return 0
    if args.command == "records":
        _dump(build_records(root))
        return 0

    plan = read_plan(root)
    corpus = {int(r["number"]): r for r in plan["corpus"]}
    if args.command == "spec":
        brief = _read_json(root / "briefs" / f"{args.lane}.json")
        task = brief.get("task") if isinstance(brief, Mapping) else None
        if not isinstance(task, Mapping) or args.lane not in corpus:
            raise ReplayRefused(f"lane {args.lane}: no recorded brief (briefs/{args.lane}.json)")
        _dump(workflow_spec(corpus[args.lane], brief=task.get("input") or {},
                            repository_url=str(task.get("repository_url") or ""),
                            intended_arm=args.arm))
        return 0
    slots = assign_slots(build_records(root))
    if args.command == "owed":
        for lane, count in owed_counts(plan["pilot"], slots, args.arm):
            print(lane, count)
        return 0
    if args.command == "slots":
        for lane in sorted(slots):
            for arm in ARMS:
                slot = slots[lane].get(arm)
                if slot:
                    print(lane, arm, slot["workflow_id"], corpus[lane]["base"], slot["implement"]["head"])
        return 0
    report = score(corpus.values(), plan["pilot"], slots, merged_patches(root))
    if args.json:
        _dump(report)
    else:
        print("\n".join(render(report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
