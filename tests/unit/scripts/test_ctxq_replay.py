"""The context-quality replay harness: docs/design/context-quality.md §6.1 (CTXQ-E).

scripts/lib/ctxq_replay.py is pure: scripts/ctxq-replay.sh reads a
quality-baseline directory (CTXQ-M's recording of the lane window), the
lanes' briefs, the replay workflows and their artifacts into one directory,
and the module decides the pilot, writes each workflow spec, labels each
replay by the arm staging actually gave it, names the `replay/*` refs to
push, and scores the pairs. So every rule is held here over fixtures in the
shapes GitHub, `git log` and swarm-api serve.

The three rules §7 names for this lane:
  * branches are `replay/*` only, and no pull request is opened;
  * scoring reads the historic-red tests from a fixture PR body;
  * the pairing is stable across runs.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "scripts" / "lib" / "ctxq_replay.py"
SCRIPT = REPO / "scripts" / "ctxq-replay.sh"

_spec = importlib.util.spec_from_file_location("ctxq_replay", HELPER)
cr = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["ctxq_replay"] = cr
_spec.loader.exec_module(cr)

BASE = "b" * 40
HEAD_A = "a" * 40
HEAD_B = "c" * 40
REPO_URL = "https://github.com/bogdan-alexandrescu/SwarmCloud.git"


# --------------------------------------------------------------------------
# recorded shapes
# --------------------------------------------------------------------------

def _body(*, base=BASE, continuation="", top=""):
    """A lane PR body as the worker writes it: the agent's text, the
    provenance block with `base commit:`, then any appended sections."""
    out = [top or "Implements the thing.", "", "---", "- task: `task_x`", f"- base commit: `{base}`"]
    if continuation:
        out += ["", continuation]
    return "\n".join(out)


#: The fixture body: a CI fixer's continuation naming the red tests the way
#: its pr-body.md quotes CI, plus a test the IMPLEMENTER named (not red).
CONTINUATION_BODY = _body(
    top="Ran tests/unit/worker/test_mentioned_by_implementer.py before publishing.",
    continuation="\n".join([
        "## Continuation (attempt 1): `task_fix1`",
        "",
        "CI went red on:",
        "FAILED tests/unit/scripts/test_new_refusals_ship_off.py::test_every_new_code_ships_off[x] - assert",
        "FAILED tests/unit/api/test_routes.py::TestRoutes::test_listed - KeyError",
        " FAIL  apps/swarm-ui/src/__tests__/Nav.test.tsx > nav > heading",
        "",
        "- task: `task_fix1`",
        "- continues: `task_x`",
        "",
        "## Continuation (attempt 2): `task_fix2`",
        "",
        "Also tests/unit/api/test_routes.py::TestRoutes::test_listed again.",
    ]),
)


def _pr(number, *, body=None, head=None, merged_at="2026-10-12T10:00:00Z"):
    return {
        "number": number,
        "state": "closed",
        "title": f"lane {number}",
        "body": _body() if body is None else body,
        "created_at": "2026-10-12T08:00:00Z",
        "merged_at": merged_at,
        "closed_at": merged_at,
        "head": {"ref": head or f"swarm/task_{number:04d}"},
    }


def _run(run_id, branch, conclusion, *, workflow="application"):
    return {"id": run_id, "name": workflow, "path": f".github/workflows/{workflow}.yml",
            "event": "pull_request", "status": "completed", "conclusion": conclusion,
            "head_branch": branch, "created_at": "2026-10-12T09:00:00Z"}


def _corpus(n_signal_per_class=None, controls=20):
    """Corpus rows as `build_corpus` writes them, without the I/O."""
    rows = []
    classes = n_signal_per_class or {}
    number = 1000
    for cls, count in classes.items():
        for _ in range(count):
            number += 1
            rows.append({"number": number, "signal": True, "classes": [cls], "replayable": True,
                         "base": BASE, "task_id": f"task_{number}", "historic_red": []})
    for _ in range(controls):
        number += 1
        rows.append({"number": number, "signal": False, "classes": [], "replayable": True,
                     "base": BASE, "task_id": f"task_{number}", "historic_red": []})
    return rows


# --------------------------------------------------------------------------
# rule 1: branches are replay/* only, and no pull request is opened
# --------------------------------------------------------------------------

def test_the_replay_branch_is_replay_lane_arm():
    assert cr.replay_branch(1215, "A") == "replay/1215/A"
    assert cr.replay_branch(1215, "B") == "replay/1215/B"


@pytest.mark.parametrize("lane, arm", [(0, "A"), (-3, "A"), (12, "C"), (12, "a"), ("12/x", "A")])
def test_a_branch_outside_the_lanes_and_arms_is_refused(lane, arm):
    with pytest.raises(cr.ReplayRefused):
        cr.replay_branch(lane, arm)


def test_a_refspec_pushes_a_full_sha_to_a_replay_branch_never_forced():
    spec = cr.replay_refspec(HEAD_A, 1215, "A")
    assert spec == f"{HEAD_A}:refs/heads/replay/1215/A"
    assert not spec.startswith("+")
    cr.check_refspec(spec)


@pytest.mark.parametrize("spec", [
    f"+{HEAD_A}:refs/heads/replay/1215/A",            # forced
    f"{HEAD_A}:refs/heads/main",                       # not a replay branch
    f"{HEAD_A}:refs/heads/swarm/task_1",               # the worker's namespace
    f"{HEAD_A}:refs/heads/replay/1215/A/x",            # deeper than lane/arm
    f"{HEAD_A}:refs/heads/replay/1215/C",              # not an arm
    f"{HEAD_A[:12]}:refs/heads/replay/1215/A",         # abbreviated sha
    f"{HEAD_A}:refs/pull/1215/head",                   # a pull request ref
    ":refs/heads/replay/1215/A",                       # a delete
])
def test_any_other_refspec_is_refused(spec):
    with pytest.raises(cr.ReplayRefused):
        cr.check_refspec(spec)


def _spec_for(lane=None, arm="A"):
    lane = lane or {"number": 1215, "base": BASE, "task_id": "task_1215",
                    "historic_red": ["tests/unit/scripts/test_new_refusals_ship_off.py::test_x"]}
    return cr.workflow_spec(lane, brief={"prompt": "Do the thing."}, repository_url=REPO_URL,
                            intended_arm=arm)


def test_the_workflow_spec_opens_no_pull_request():
    spec = _spec_for()
    # `collect` pushes each step's branch (carrier) and opens nothing
    # (docs/design/dispatch-and-integration.md §4.2); every other strategy
    # opens a pull request somewhere.
    assert spec["strategy"] == "collect"
    assert spec["carrier"] == "branches"
    for key in ("after", "merge_pr", "continues_task", "fix_rounds"):
        assert key not in spec
    assert "merge" not in spec.get("metadata", {})
    profiles = {s["runner_profile"] for s in spec["steps"]}
    assert profiles == {"claude-code"}, profiles   # no merge, no post-verdict
    assert not any("when" in s or "builds_on" in s for s in spec["steps"])


def test_the_workflow_spec_starts_at_the_lanes_base_with_its_brief_unchanged():
    spec = _spec_for()
    assert spec["repository_url"] == REPO_URL
    assert spec["repository_ref"] == BASE
    implement = spec["steps"][0]
    assert implement["step_id"] == "implement"
    assert implement["input"] == {"prompt": "Do the thing."}
    assert "depends_on" not in implement
    review = next(s for s in spec["steps"] if s["step_id"] == "review")
    assert review["depends_on"] == ["implement"]
    assert review["input_from"] == {"implement": "swarm-work.patch"}
    assert "verdict.json" in review["input"]["prompt"]
    score = next(s for s in spec["steps"] if s["step_id"] == "score")
    assert score["depends_on"] == ["implement"]
    assert score["allow_empty_diff"] is True
    assert "score.json" in score["input"]["prompt"]
    assert "tests/unit/scripts/test_new_refusals_ship_off.py::test_x" in score["input"]["prompt"]


def test_both_arms_get_the_identical_brief_and_steps():
    a, b = _spec_for(arm="A"), _spec_for(arm="B")
    assert a["steps"] == b["steps"]
    assert a["metadata"]["ctxq_replay"]["intended_arm"] == "A"
    assert b["metadata"]["ctxq_replay"]["intended_arm"] == "B"


def test_the_spec_carries_no_caller_choice_of_arm_image_or_command():
    # Invariant 10: staging is the stored setting's (§4.7), so nothing in the
    # spec may read as a request to stage or not.
    spec = _spec_for()
    text = json.dumps(spec)
    for word in ("image", "argv", "command", "context_ab", "code_intelligence", "repo_index"):
        assert f'"{word}"' not in text, word
    for step in spec["steps"]:
        assert set(step["input"]) == {"prompt"}


def test_a_brief_with_an_issue_number_is_replayed_without_it():
    # `issue` re-fetches the issue as it is NOW: closed, with the merged PR in
    # its comments. Replaying it would hand both arms the answer.
    spec = cr.workflow_spec({"number": 1, "base": BASE, "task_id": "t", "historic_red": []},
                            brief={"prompt": "Fix #5.", "issue": 5}, repository_url=REPO_URL,
                            intended_arm="B")
    assert spec["steps"][0]["input"] == {"prompt": "Fix #5."}


def test_a_lane_without_a_base_or_a_brief_has_no_spec():
    with pytest.raises(cr.ReplayRefused):
        cr.workflow_spec({"number": 1, "base": None, "task_id": "t", "historic_red": []},
                         brief={"prompt": "x"}, repository_url=REPO_URL, intended_arm="A")
    with pytest.raises(cr.ReplayRefused):
        cr.workflow_spec({"number": 1, "base": BASE, "task_id": "t", "historic_red": []},
                         brief={"prompt": "  "}, repository_url=REPO_URL, intended_arm="A")


def test_mirror_refspecs_are_replay_refs_of_finished_implementers_only(tmp_path):
    records = [
        _record(1215, "wf_1", observed="A", head=HEAD_A),
        _record(1215, "wf_2", observed="B", head=HEAD_B),
        _record(1221, "wf_3", observed="B", head=None),            # implementer pushed nothing
        _record(1255, "wf_4", observed=None, head=HEAD_A),         # arm unknown
    ]
    slots = cr.assign_slots(records)
    specs = cr.mirror_refspecs(slots)
    assert specs == [f"{HEAD_A}:refs/heads/replay/1215/A", f"{HEAD_B}:refs/heads/replay/1215/B"]
    for spec in specs:
        cr.check_refspec(spec)


# --------------------------------------------------------------------------
# rule 2: scoring reads the historic-red tests from the PR body
# --------------------------------------------------------------------------

def test_historic_red_tests_are_read_from_the_continuation_sections_of_a_body():
    tests = cr.historic_red_tests(CONTINUATION_BODY)
    assert tests == [
        "tests/unit/scripts/test_new_refusals_ship_off.py::test_every_new_code_ships_off",
        "tests/unit/api/test_routes.py::TestRoutes::test_listed",
        "apps/swarm-ui/src/__tests__/Nav.test.tsx",
    ]


def test_a_test_named_outside_a_continuation_is_not_historic_red():
    # The implementer's own body names a test it ran; that test never went red.
    assert cr.historic_red_tests(_body(top="Ran tests/unit/x/test_y.py::test_z.")) == []
    assert "tests/unit/worker/test_mentioned_by_implementer.py" not in " ".join(
        cr.historic_red_tests(CONTINUATION_BODY))


def test_a_fix_commit_after_the_first_red_run_adds_the_test_files_it_edited():
    commits = [
        {"sha": "1" * 40, "parents": ["0" * 40], "author": "bogdan", "date": "2026-10-12T08:30:00+00:00",
         "files": ["apps/x.py", "tests/unit/x/test_before.py"]},
        {"sha": "2" * 40, "parents": ["1" * 40], "author": "bogdan", "date": "2026-10-12T09:30:00+00:00",
         "files": ["apps/x.py", "tests/unit/x/test_fixed.py", "apps/swarm-ui/src/A.test.tsx"]},
        {"sha": "3" * 40, "parents": ["2" * 40, "9" * 40], "author": "bogdan",
         "date": "2026-10-12T09:40:00+00:00", "files": ["tests/unit/x/test_from_main.py"]},
    ]
    tests = cr.historic_red_tests(_body(), commits=commits, first_red_at="2026-10-12T09:00:00Z")
    assert tests == ["tests/unit/x/test_fixed.py", "apps/swarm-ui/src/A.test.tsx"]


def test_the_base_commit_is_read_from_the_body():
    assert cr.base_commit(_body()) == BASE
    assert cr.base_commit("no provenance here") is None
    assert cr.base_commit(_body(base="abc1234")) is None   # abbreviated: the clone cannot fetch it


def test_the_corpus_splits_signal_from_controls_with_the_baselines_definitions():
    lanes = [_pr(1215, body=CONTINUATION_BODY), _pr(1300), _pr(1301, body=_body(base="x"))]
    bundle = {"lanes": lanes, "runs": [_run(1, "swarm/task_1215", "failure"),
                                       _run(2, "swarm/task_1300", "success")]}
    corpus = {row["number"]: row for row in cr.build_corpus(bundle)}
    assert corpus[1215]["signal"] is True and corpus[1215]["why"] == ["red"]
    assert corpus[1215]["classes"] == ["G"]               # §3.1's table
    assert corpus[1215]["historic_red"][0].startswith("tests/unit/scripts/test_new_refusals")
    assert corpus[1300]["signal"] is False and corpus[1300]["replayable"] is True
    assert corpus[1301]["replayable"] is False
    assert "base commit" in corpus[1301]["not_replayable"]


def test_scoring_reads_recurrence_of_the_historic_red_tests_per_arm():
    corpus = [{"number": 1215, "signal": True, "classes": ["G"], "replayable": True, "base": BASE,
               "task_id": "t", "historic_red": cr.historic_red_tests(CONTINUATION_BODY),
               "merged_files": ["apps/x.py"]}]
    red = cr.historic_red_tests(CONTINUATION_BODY)
    slots = cr.assign_slots([
        _record(1215, "wf_1", observed="A", head=HEAD_A,
                score=_score(red, failed={red[0]}), files=["apps/x.py"]),
        _record(1215, "wf_2", observed="B", head=HEAD_B, score=_score(red), files=["apps/x.py", "b"]),
    ])
    report = cr.score(corpus, _pilot([1215]), slots)
    pair = report["pairs"][0]
    assert pair["arms"]["A"]["historic_red"] == {"tests": 3, "passed": 2, "failed": 1, "unknown": 0,
                                                 "recurred": True}
    assert pair["arms"]["B"]["historic_red"]["recurred"] is False
    assert pair["arms"]["A"]["similarity"]["files_jaccard"] == 1.0
    assert pair["arms"]["B"]["similarity"]["files_jaccard"] == 0.5
    rec = report["summary"]["historic_red"]
    assert (rec["pairs"], rec["recurred_A"], rec["recurred_B"]) == (1, 1, 0)
    assert rec["discordant_A_only"] == 1 and rec["discordant_B_only"] == 0


def test_a_score_that_names_no_outcome_for_a_test_is_unknown_not_passed():
    red = ["tests/unit/a/test_a.py::test_one"]
    corpus = [{"number": 7, "signal": True, "classes": [], "replayable": True, "base": BASE,
               "task_id": "t", "historic_red": red, "merged_files": []}]
    slots = cr.assign_slots([
        _record(7, "wf_1", observed="A", head=HEAD_A, score={"schema": cr.SCORE_SCHEMA, "commands": [],
                                                              "historic_red": []}),
        _record(7, "wf_2", observed="B", head=HEAD_B, score=None),
    ])
    pair = cr.score(corpus, _pilot([7]), slots)["pairs"][0]
    assert pair["arms"]["A"]["historic_red"]["unknown"] == 1
    assert pair["arms"]["A"]["historic_red"]["recurred"] is None
    assert pair["arms"]["B"]["historic_red"] is None and pair["arms"]["B"]["ci_pass"] is None


# --------------------------------------------------------------------------
# rule 3: the pairing is stable across runs
# --------------------------------------------------------------------------

def test_the_pilot_is_30_signal_lanes_across_g_t_x_p_and_10_controls():
    corpus = _corpus({"G": 15, "T": 10, "X": 6, "P": 8, "L": 5}, controls=25)
    pilot = cr.select_pilot(corpus)
    assert len(pilot["signal"]) == 30 and len(pilot["controls"]) == 10
    by_number = {row["number"]: row for row in corpus}
    picked = [by_number[n]["classes"][0] for n in pilot["signal"]]
    for cls in ("G", "T", "X", "P"):
        assert picked.count(cls) >= 6, (cls, picked)   # round-robin, not the first class
    assert "L" not in picked                           # G/T/X/P filled all 30 first
    assert all(not by_number[n]["signal"] for n in pilot["controls"])


def test_the_pilot_is_identical_whatever_order_the_corpus_is_read_in():
    corpus = _corpus({"G": 15, "T": 10, "X": 6, "P": 8, "L": 5}, controls=25)
    first = cr.select_pilot(corpus)
    for seed in range(5):
        shuffled = list(corpus)
        random.Random(seed).shuffle(shuffled)
        again = cr.select_pilot(shuffled)
        assert json.dumps(again, sort_keys=True) == json.dumps(first, sort_keys=True)


def test_each_pair_names_its_two_branches_and_they_never_change():
    pilot = cr.select_pilot(_corpus({"G": 3}, controls=2), signal=3, controls=2)
    for pair in pilot["pairs"]:
        n = pair["lane"]
        assert pair["branches"] == {"A": f"replay/{n}/A", "B": f"replay/{n}/B"}
    assert [p["lane"] for p in pilot["pairs"]] == sorted(p["lane"] for p in pilot["pairs"])


def test_the_selection_order_is_a_hash_of_the_lane_not_the_input_position():
    assert cr.order_key(1215) == cr.order_key(1215)
    assert cr.order_key(1215) != cr.order_key(1216)
    # Pinned: a change to the key reshuffles every pilot ever planned.
    assert cr.order_key(1215).startswith(
        __import__("hashlib").sha256(b"ctxq-replay/v1:1215").hexdigest()[:16])


def test_a_short_corpus_says_so_rather_than_padding():
    pilot = cr.select_pilot(_corpus({"G": 2}, controls=1), signal=30, controls=10)
    assert len(pilot["signal"]) == 2 and len(pilot["controls"]) == 1
    assert pilot["short"] == {"signal": 28, "controls": 9}


def test_unreplayable_lanes_are_never_picked():
    corpus = _corpus({"G": 3}, controls=0)
    corpus[0]["replayable"] = False
    pilot = cr.select_pilot(corpus, signal=3, controls=0)
    assert corpus[0]["number"] not in pilot["signal"]


def test_slots_are_labelled_by_the_arm_staging_gave_not_the_one_asked_for():
    # Staging is the stored setting's or sha256(task_id)'s (graphstage.decide),
    # never the caller's, so a replay asked for A may have run as B.
    slots = cr.assign_slots([
        _record(9, "wf_1", observed="B", head=HEAD_B, intended="A"),
        _record(9, "wf_2", observed="B", head=HEAD_A, intended="B", submitted_at="2026-10-14T11:00:00Z"),
    ])
    assert slots[9]["B"]["workflow_id"] == "wf_1"            # the earlier submission wins
    assert slots[9]["B"]["arm_mismatch"] is True
    assert "A" not in slots[9]
    assert [s["workflow_id"] for s in slots[9]["surplus"]] == ["wf_2"]


def test_slot_assignment_is_independent_of_record_order():
    records = [_record(9, f"wf_{i}", observed=arm, head=HEAD_A,
                       submitted_at=f"2026-10-14T1{i}:00:00Z") for i, arm in enumerate("BABA")]
    first = cr.assign_slots(records)
    again = cr.assign_slots(list(reversed(records)))
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert first[9]["B"]["workflow_id"] == "wf_0" and first[9]["A"]["workflow_id"] == "wf_1"


def test_the_observed_arm_comes_from_the_context_pack():
    assert cr.observed_arm({"schema": "swarm.context-pack/v1", "arm": "B"}, listed=True) == "B"
    assert cr.observed_arm({"schema": "swarm.context-pack/v1", "arm": "A"}, listed=True) == "A"
    # Staged with no experiment running: the pipeline arm.
    assert cr.observed_arm({"schema": "swarm.context-pack/v1", "arm": None}, listed=True) == "B"
    # No pack uploaded: nothing was staged, today's runner.
    assert cr.observed_arm(None, listed=True) == "A"
    # The listing was never read: unknown, never A.
    assert cr.observed_arm(None, listed=False) is None


def test_lanes_still_owed_an_arm_are_listed_for_the_next_submission():
    pilot = _pilot([1, 2, 3])
    slots = cr.assign_slots([_record(1, "w1", observed="A", head=HEAD_A),
                             _record(1, "w2", observed="B", head=HEAD_B),
                             _record(2, "w3", observed="A", head=HEAD_A)])
    assert cr.owed(pilot, slots, "any") == [2, 3]
    assert cr.owed(pilot, slots, "A") == [3]
    assert cr.owed(pilot, slots, "B") == [2, 3]


# --------------------------------------------------------------------------
# the McNemar arithmetic and the pilot's gate (§6.1, §8 Q8)
# --------------------------------------------------------------------------

def test_mcnemar_exact_p_value():
    assert cr.mcnemar_exact(0, 0) == 1.0
    assert cr.mcnemar_exact(6, 0) == pytest.approx(2 / 64)
    assert cr.mcnemar_exact(3, 3) == 1.0


def test_the_full_run_gate_needs_a_20_percent_relative_drop():
    assert cr.full_run_gate(10, 8)["gate"] == "pass"     # 20% drop
    assert cr.full_run_gate(10, 9)["gate"] == "fail"     # 10% drop
    assert cr.full_run_gate(0, 0)["gate"] == "not measured"


# --------------------------------------------------------------------------
# fixtures for records, scores and pilots
# --------------------------------------------------------------------------

def _score(tests, *, failed=(), exit_codes=None):
    return {
        "schema": cr.SCORE_SCHEMA,
        "commands": [{"name": n, "exit_code": c} for n, c in (exit_codes or {"unit": 0}).items()],
        "historic_red": [{"test": t, "outcome": "failed" if t in failed else "passed"} for t in tests],
    }


def _record(lane, workflow_id, *, observed, head, intended="any", submitted_at="2026-10-14T10:00:00Z",
            score=None, files=None, verdict=None):
    return {
        "lane": lane,
        "workflow_id": workflow_id,
        "intended_arm": intended,
        "submitted_at": submitted_at,
        "observed_arm": observed,
        "implement": {"state": "SUCCEEDED" if head else "FAILED", "head": head,
                      "task_id": f"task_{workflow_id}"},
        "score": score,
        "verdict": verdict or {"verdict": "MERGE", "findings": []},
        "changed_files": files or [],
        "cost_usd": 1.5,
        "duration_s": 600.0,
        "pack": {"tokens": 3900, "jev_calls": 0} if observed == "B" else None,
    }


def _pilot(numbers):
    return {"schema": cr.SCHEMA, "signal": list(numbers), "controls": [],
            "pairs": [{"lane": n, "group": "signal", "classes": [],
                       "branches": {"A": f"replay/{n}/A", "B": f"replay/{n}/B"}} for n in numbers]}


# --------------------------------------------------------------------------
# the CLI over recorded files
# --------------------------------------------------------------------------

def _cli(*args):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                          check=False)


def test_cli_plan_from_a_baseline_directory_is_stable(tmp_path):
    lanes = [_pr(n) for n in range(1100, 1160)]
    lanes[0]["body"] = CONTINUATION_BODY
    (tmp_path / "lanes.json").write_text(json.dumps(lanes), encoding="utf-8")
    (tmp_path / "runs.json").write_text(json.dumps(
        [_run(i, lanes[i]["head"]["ref"], "failure") for i in range(0, 40)]), encoding="utf-8")
    outs = []
    for _ in range(2):
        proc = _cli("plan", "--baseline", str(tmp_path))
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout)
    assert outs[0] == outs[1]
    plan = json.loads(outs[0])
    assert len(plan["pilot"]["signal"]) == 30 and len(plan["pilot"]["controls"]) == 10
    assert len(plan["corpus"]) == 60


def test_cli_refspecs_prints_only_replay_refs(tmp_path):
    records = [_record(1215, "wf_1", observed="A", head=HEAD_A),
               _record(1215, "wf_2", observed="B", head=HEAD_B)]
    (tmp_path / "records.json").write_text(json.dumps(records), encoding="utf-8")
    proc = _cli("refspecs", "--records", str(tmp_path / "records.json"))
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.split()
    assert lines and all(re.fullmatch(r"[0-9a-f]{40}:refs/heads/replay/\d+/[AB]", line) for line in lines)


def test_cli_over_an_empty_baseline_exits_2(tmp_path):
    (tmp_path / "lanes.json").write_text("[]", encoding="utf-8")
    proc = _cli("plan", "--baseline", str(tmp_path))
    assert proc.returncode == 2
    assert "no lane" in proc.stderr


# --------------------------------------------------------------------------
# the script's house rules (CLAUDE.md "Writing scripts") and its no-PR rule
# --------------------------------------------------------------------------

def _effective_lines(text):
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def test_script_carries_set_euo_pipefail_first_and_sources_common():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert next(_effective_lines(text)) == "set -euo pipefail"
    assert re.search(r"^source .*lib/common\.sh\"?$", text, re.MULTILINE)
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    assert "| redact" in text


def test_script_opens_no_pull_request_and_pushes_only_replay_refs():
    text = "\n".join(_effective_lines(SCRIPT.read_text()))
    assert not re.search(r"gh (pr|issue)\b", text)
    assert "/pulls" not in text
    # The one POST is the workflow submission.
    posts = re.findall(r"api_request POST \S+", text)
    assert posts and all("/workflows" in p for p in posts), posts
    for verb in ("PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{verb}\b", text), verb
    # Every push names refspecs the helper validated, re-checked here, never forced.
    pushes = [line for line in text.splitlines() if re.search(r"\bgit\b.*\bpush\b", line)]
    assert pushes, "the mirror step pushes"
    for line in pushes:
        assert "--force" not in line and " -f " not in line and "--delete" not in line and "+" not in line
    assert "refs/heads/replay/" in text
    assert "fs_" not in text


def test_script_avoids_bash4_only_constructs():
    text = SCRIPT.read_text()
    assert "mapfile" not in text and "readarray" not in text
    assert "declare -A" not in text
    assert ",,}" not in text and "^^}" not in text
