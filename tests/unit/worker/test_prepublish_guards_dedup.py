"""The pre-publish gate does not run the guard script twice on one tree (GUARDS-DEDUP).

Measured over 74 implement lanes on 2026-10-19/20 (part of #1694): the agent
ran `scripts/changed-guards.sh`, then the worker's pre-publish gate ran it
again on the same code, in 73 of 74 lanes (4.9 h); five lanes hit its 600 s
limit in BOTH rounds, and the fix turn between them cannot fix a timeout. So:

* the script records, in the checkout's git directory, the tree it ran on
  (the tree the worker would commit: HEAD plus every uncommitted and
  untracked file), the base, and how the run ended. The gate computes that
  tree ITSELF and skips its own run only when a record says `passed` on the
  same tree from the same base -- never on the agent's word, and never on a
  record whose tree does not match;
* a guard run that timed out is reported as a timeout, is never sent to the
  agent as something to fix, and is run once more in the parts the script
  advises (`--shard k/n`), never as-is.

These run the REAL script, copied into a scratch repository with a stand-in
project python, so the record the script writes and the tree the gate
computes are held to agree.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("jq") is None,
    reason="git and jq are needed to run the guard script")

REPO = Path(__file__).resolve().parents[3]
GUARDS = (
    "tests/unit/scripts/test_docs_describe_what_was_built.py",
    "tests/unit/scripts/test_docs_spec_amendments.py",
    "tests/unit/control_plane/test_runtimes_screen.py",
    "tests/unit/common/test_specsign_covers.py",
)
_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}

#: The stand-in project python: logs each pytest call to $CALLS, then exits
#: with the next code in $EXITS (one per line, consumed), or 0. Exit 124 is
#: what `timeout` returns, so the script reads it as `ran past 540 s`. The
#: n-th call prints the file $OUTS/<n>, when there is one.
FAKE_PYTHON = r'''#!/usr/bin/env bash
printf 'pytest %s\n' "$*" >> "$CALLS"
n="$(wc -l < "$CALLS" | tr -d ' ')"
if [[ -n "${OUTS:-}" && -f "$OUTS/$n" ]]; then
  cat "$OUTS/$n"
fi
code=0
if [[ -s "$EXITS" ]]; then
  code="$(head -n 1 "$EXITS")"
  tail -n +2 "$EXITS" > "$EXITS.rest" && mv "$EXITS.rest" "$EXITS"
fi
exit "$code"
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout.strip()


def _log() -> Any:
    return build_logger(task_id="t", attempt_id="a", tenant_id="acme", generation=1,
                        runner_profile="claude-code", stream=io.StringIO())


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "changed-guards.sh", repo / "scripts" / "changed-guards.sh")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", repo / "scripts" / "lib" / "common.sh")
    (repo / "scripts" / "lib" / "check-contract-parity.sh").write_text("#!/usr/bin/env bash\n")
    for guard in GUARDS:
        (repo / guard).parent.mkdir(parents=True, exist_ok=True)
        (repo / guard).write_text("")
    (repo / ".gitignore").write_text(".venv/\n")
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(FAKE_PYTHON)
    python.chmod(0o755)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "apps" / "w").mkdir(parents=True)
    (repo / "apps" / "w" / "notes.txt").write_text("the lane's change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    # Only git, jq, bash and coreutils: no shellcheck, ruff or checkov, so the
    # changed-guards check is the only one that runs.
    dirs = []
    for tool in ("git", "jq", "bash", "head", "timeout"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    env = {"PATH": ":".join(dirs + ["/usr/bin", "/bin"]), "HOME": str(tmp_path),
           "NO_COLOR": "1", "SWARM_ENV_FILE": str(tmp_path / "no.env"),
           "CALLS": str(tmp_path / "calls"), "EXITS": str(tmp_path / "exits"),
           "OUTS": str(tmp_path / "outs"),
           prepublish.CLONE_BASE_ENV: base, **_IDENT}
    return {"repo": repo, "base": base, "env": env, "tmp": tmp_path}


def _agent_runs_the_script(lane: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "scripts/changed-guards.sh", *args], cwd=lane["repo"],
                          env=lane["env"], capture_output=True, text=True, timeout=120)


def _pytest_calls(lane: dict[str, Any]) -> list[str]:
    path = lane["tmp"] / "calls"
    return path.read_text().splitlines() if path.exists() else []


def _records(lane: dict[str, Any]) -> dict[str, dict[str, Any]]:
    gitdir = Path(_git(lane["repo"], "rev-parse", "--absolute-git-dir"))
    folder = gitdir / prepublish.GUARDS_RECORD_DIR
    return {p.name: json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))}


class Turns:
    def __init__(self, *acts: Any) -> None:
        self.prompts: list[str] = []
        self._acts = list(acts)

    def __call__(self, text: str) -> bool:
        self.prompts.append(text)
        if self._acts:
            self._acts.pop(0)()
        return True


def _gate(lane: dict[str, Any], turns: Turns, env: dict[str, str] | None = None) -> prepublish.Gate:
    artifacts = lane["tmp"] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=lane["repo"], env=env or lane["env"], secrets=(), resume=turns,
                           remaining=lambda: 3600.0, log=_log(), artifacts_dir=artifacts,
                           python=sys.executable)


def _artifact(lane: dict[str, Any], name: str) -> str | None:
    path = lane["tmp"] / "artifacts" / name
    return path.read_text() if path.exists() else None


# ---------------------------------------------------------------------------
# The record the script writes
# ---------------------------------------------------------------------------


def test_the_script_records_the_tree_it_ran_on_and_the_gate_computes_the_same_tree(lane):
    (lane["repo"] / "apps" / "w" / "untracked.txt").write_text("not yet committed\n")
    ran = _agent_runs_the_script(lane)
    assert ran.returncode == 0, ran.stderr
    records = _records(lane)
    # MUTATION: removing the record write from changed-guards.sh turns this red.
    assert set(records) == {"all.json"}
    record = records["all.json"]
    assert record["status"] == "passed"
    assert record["base"] == lane["base"]
    # The tree includes the untracked file the worker will commit: the gate's
    # own computation, not the record, decides it.
    # MUTATION: computing the tree from HEAD alone (no untracked files) in
    # either the script or `prepublish.worktree_tree` turns this red.
    tree = prepublish.worktree_tree(lane["repo"], runner=prepublish.run_command, env=lane["env"])
    assert tree is not None and record["tree"] == tree
    assert tree != _git(lane["repo"], "rev-parse", "HEAD^{tree}")
    # The record is in the git directory, so the publish never commits it.
    assert _git(lane["repo"], "status", "--porcelain") == "?? apps/w/untracked.txt"


def test_a_run_that_ran_out_of_time_is_recorded_as_a_timeout(lane):
    (lane["tmp"] / "exits").write_text("124\n")
    ran = _agent_runs_the_script(lane)
    assert ran.returncode == 1
    # MUTATION: recording every non-zero exit as `failed` turns this red.
    assert _records(lane)["all.json"]["status"] == "timed_out"


def test_a_shard_records_its_own_part(lane):
    ran = _agent_runs_the_script(lane, "--shard", "2/2")
    assert ran.returncode == 0, ran.stderr
    assert _records(lane)["shard-2-of-2.json"]["status"] == "passed"


def test_a_list_or_a_files_run_records_nothing(lane):
    assert _agent_runs_the_script(lane, "--list").returncode == 0
    listed = subprocess.run(["bash", "scripts/changed-guards.sh", "--files", "-"],
                            cwd=lane["repo"], env=lane["env"], input="apps/w/notes.txt\n",
                            capture_output=True, text=True, timeout=120)
    assert listed.returncode == 0, listed.stderr
    # MUTATION: recording a --files run (whose paths are not the tree) turns this red.
    assert _records(lane) == {}


# ---------------------------------------------------------------------------
# The gate skips its run on the same tree
# ---------------------------------------------------------------------------


def test_the_gate_skips_the_guards_the_agent_already_passed_on_the_same_tree(lane):
    assert _agent_runs_the_script(lane).returncode == 0
    assert len(_pytest_calls(lane)) == 1
    record = _gate(lane, Turns()).run()
    (round_,) = record["rounds"]
    (guards,) = [c for c in round_ if c["name"] == "changed-guards"]
    # MUTATION: dropping the record lookup in `Gate._round` runs the script a
    # second time on the same tree and turns both assertions red.
    assert guards["status"] == "skipped"
    assert len(_pytest_calls(lane)) == 1, "the guard script ran twice on one tree"
    assert "same tree" in guards["reason"]
    # The published report says it was skipped, and why.
    advisory = _artifact(lane, prepublish.ADVISORY_SECTION_NAME)
    assert advisory is not None
    # MUTATION: leaving the guard notes out of `advisory_section` turns this red.
    assert "changed-guards" in advisory and "skipped" in advisory and "same tree" in advisory


def test_the_shards_the_agent_passed_on_the_same_tree_count_as_a_pass(lane):
    for part in ("1/2", "2/2"):
        assert _agent_runs_the_script(lane, "--shard", part).returncode == 0
    calls = len(_pytest_calls(lane))
    record = _gate(lane, Turns()).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    # MUTATION: reading only all.json (ignoring a complete set of shards) turns this red.
    assert guards["status"] == "skipped"
    assert len(_pytest_calls(lane)) == calls


def test_one_shard_of_two_is_not_a_pass(lane):
    assert _agent_runs_the_script(lane, "--shard", "1/2").returncode == 0
    calls = len(_pytest_calls(lane))
    record = _gate(lane, Turns()).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    # MUTATION: accepting any passed shard as the whole run turns this red.
    assert guards["status"] == "passed"
    assert len(_pytest_calls(lane)) == calls + 1


def test_a_change_after_the_agents_run_is_checked_again(lane):
    assert _agent_runs_the_script(lane).returncode == 0
    (lane["repo"] / "apps" / "w" / "notes.txt").write_text("changed after the run\n")
    record = _gate(lane, Turns()).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    # MUTATION: skipping on any passed record, without comparing trees, turns this red.
    assert guards["status"] == "passed"
    assert len(_pytest_calls(lane)) == 2


def test_a_failed_run_or_another_base_is_not_a_pass(lane):
    (lane["tmp"] / "exits").write_text("1\n")
    assert _agent_runs_the_script(lane).returncode == 1
    record = _gate(lane, Turns()).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    # MUTATION: skipping on a record whose status is not `passed` turns this red.
    assert guards["status"] == "passed" and len(_pytest_calls(lane)) == 2

    # A pass from the clone base is not a pass from another base: another
    # diff, so another selection.
    (lane["repo"] / "apps" / "w" / "more.txt").write_text("x\n")  # a non-empty diff from HEAD
    assert _agent_runs_the_script(lane).returncode == 0  # from the clone base
    calls = len(_pytest_calls(lane))
    head = _git(lane["repo"], "rev-parse", "HEAD")
    _gate(lane, Turns(), env={**lane["env"], prepublish.CLONE_BASE_ENV: head}).run()
    # MUTATION: not comparing the record's base turns this red.
    assert len(_pytest_calls(lane)) == calls + 1


def test_a_record_written_by_hand_with_another_tree_is_not_trusted(lane):
    gitdir = Path(_git(lane["repo"], "rev-parse", "--absolute-git-dir"))
    folder = gitdir / prepublish.GUARDS_RECORD_DIR
    folder.mkdir()
    (folder / "all.json").write_text(json.dumps({
        "version": 1, "tree": "0" * 40, "base": lane["base"], "part": "all",
        "status": "passed"}))
    record = _gate(lane, Turns()).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    assert guards["status"] == "passed"
    assert len(_pytest_calls(lane)) == 1


# ---------------------------------------------------------------------------
# A timeout is reported, and never re-run as-is
# ---------------------------------------------------------------------------


def test_a_guard_timeout_is_not_a_fix_turn_and_is_run_again_in_parts(lane):
    (lane["tmp"] / "exits").write_text("124\n")  # the gate's first run runs out of time
    turns = Turns()
    record = _gate(lane, turns).run()
    first = [c for c in record["rounds"][0] if c["name"] == "changed-guards"][0]
    assert first["status"] == "failed"
    # MUTATION: not detecting the script's `ran past` line turns this red.
    assert first["timed_out"] is True
    # MUTATION: sending a timed-out guard run to the fix turn turns this red.
    assert turns.prompts == [] and record["fix_turn"] is False
    # MUTATION: re-running the script as-is in the second round turns this red:
    # its calls would carry no --shard.
    shards = [c for c in record["rounds"][1] if c["name"] == "changed-guards"]
    assert shards and shards[0]["status"] == "passed"
    script_calls = _pytest_calls(lane)
    assert len(script_calls) == 3, script_calls  # the timed-out run, then parts 1/2 and 2/2
    assert record["failed"] == [] and record["pr_section"] is False
    advisory = _artifact(lane, prepublish.ADVISORY_SECTION_NAME) or ""
    assert "timed out" in advisory and "--shard 1/2" in advisory


def test_a_timeout_beside_a_fixable_failure_leaves_the_guards_out_of_the_fix_prompt(lane):
    bin_dir = lane["tmp"] / "bin"
    bin_dir.mkdir()
    shellcheck = bin_dir / "shellcheck"
    shellcheck.write_text("#!/usr/bin/env bash\ngrep -l BAD \"${@:2}\" && exit 1\nexit 0\n")
    shellcheck.chmod(0o755)
    env = {**lane["env"], "PATH": f"{bin_dir}:{lane['env']['PATH']}"}
    (lane["repo"] / "scripts" / "deploy.sh").write_text("#!/usr/bin/env bash\necho BAD\n")
    (lane["tmp"] / "exits").write_text("124\n")
    turns = Turns(lambda: (lane["repo"] / "scripts" / "deploy.sh").write_text(
        "#!/usr/bin/env bash\necho ok\n"))
    record = _gate(lane, turns, env=env).run()
    assert len(turns.prompts) == 1
    # MUTATION: listing the timed-out guards among the failures to fix turns this red.
    assert "## changed-guards" not in turns.prompts[0]
    assert "## shellcheck" in turns.prompts[0]
    assert "ran out of time" in turns.prompts[0]
    second = {c["name"]: c for c in record["rounds"][1]}
    assert second["shellcheck"]["status"] == "passed"
    assert second["changed-guards"]["status"] == "passed"
    # The timed-out run, then the two parts after the fix turn.
    assert len(_pytest_calls(lane)) == 3, _pytest_calls(lane)


def test_parts_that_time_out_too_are_reported_as_a_timeout(lane):
    (lane["tmp"] / "exits").write_text("124\n124\n0\n")
    record = _gate(lane, Turns()).run()
    assert record["failed"] == ["changed-guards"]
    section = _artifact(lane, prepublish.PR_SECTION_NAME)
    assert section is not None
    # MUTATION: reporting a timeout as an ordinary failure turns this red.
    assert "changed-guards timed out" in section


def test_the_timeout_reading_covers_the_scripts_line_and_the_runners_kill():
    assert prepublish.guards_timed_out("fail pytest ran past 540 s\n")
    assert prepublish.guards_timed_out("...\n[timed out after 600s]")
    assert not prepublish.guards_timed_out("fail pytest failed (exit 1)")
    assert prepublish.guards_parts("run it as 3 commands: scripts/changed-guards.sh "
                                   "--shard 1/3 ... --shard 3/3") == 3
    assert prepublish.guards_parts("[timed out after 600s]") == 2


#: More than OUTPUT_TAIL_CHARS of a passing part's own output: 25 lines, the
#: script's `tail -n 25` of the pytest log, ending as a part's last line does.
_LONG_PASS = "".join(f"tests/unit/test_long.py::test_case_{i:03d} PASSED {'.' * 140}\n"
                     for i in range(24)) + "ok part 4 of 4\n"


def test_a_failing_part_before_a_long_passing_part_reaches_the_fix_prompt_and_the_pr_section(lane):
    assert len(_LONG_PASS) > prepublish.OUTPUT_TAIL_CHARS
    outs = lane["tmp"] / "outs"
    outs.mkdir()
    # The full run runs out of time and advises four parts; part 1 then
    # fails and part 4 passes with a long output (task_7aa1df5f, #2927).
    (outs / "1").write_text("run it as 4 commands: scripts/changed-guards.sh "
                            "--shard 1/4 ... --shard 4/4\n")
    (outs / "2").write_text("FAILED tests/x.py::test_y - assert 1 == 2\n")
    (outs / "5").write_text(_LONG_PASS)
    (lane["tmp"] / "exits").write_text("124\n1\n0\n0\n0\n")
    record = _gate(lane, Turns()).run()
    calls = _pytest_calls(lane)
    assert len(calls) == 5, calls  # the timed-out run, then parts 1/4 ... 4/4
    assert record["failed"] == ["changed-guards"]
    section = _artifact(lane, prepublish.PR_SECTION_NAME)
    assert section is not None
    # MUTATION: joining the parts in part order in `_sharded_guards` lets part
    # 4's long output push part 1's failure out of clean_output's tail, and
    # turns this red.
    assert "FAILED tests/x.py::test_y" in section
    assert "changed-guards failed" in section


def _part_runner(outputs: dict[str, prepublish.Ran]) -> Any:
    calls: list[list[str]] = []

    def runner(argv: Any, repo: Path, env: Any, timeout: float) -> prepublish.Ran:
        calls.append(list(argv))
        return outputs[argv[argv.index("--shard") + 1]]
    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_sharded_guards_puts_the_failing_parts_output_last(tmp_path):
    runner = _part_runner({
        "1/4": prepublish.Ran(1, "fail pytest\nFAILED tests/x.py::test_y - assert 1 == 2\n"
                                 "fail pytest failed (exit 1)\n"),
        "2/4": prepublish.Ran(0, "ok part 2 of 4\n"),
        "3/4": prepublish.Ran(0, "ok part 3 of 4\n"),
        "4/4": prepublish.Ran(0, _LONG_PASS),
    })
    run = prepublish._sharded_guards(["bash", prepublish.GUARDS_SCRIPT, "--base", "b"], 4,
                                     lambda: 3600.0)
    status, output = run(runner, tmp_path, {}, 600.0)
    assert len(runner.calls) == 4
    assert status == prepublish.FAILED
    # MUTATION: returning the parts in part order turns this red: the long
    # passing part 4 fills the tail clean_output keeps.
    assert "FAILED tests/x.py::test_y" in prepublish.clean_output(output, [])
    # Every passing part is still accounted for, in one line each.
    for k in (2, 3, 4):
        assert f"--- part {k}/4 passed ---" in output
    assert output.rstrip().endswith("fail pytest failed (exit 1)")
    outcome = prepublish.Outcome(prepublish.GUARDS_CHECK, status, 1.0, ("apps/w/notes.txt",),
                                 "bash scripts/changed-guards.sh", output=output)
    # MUTATION: the same ordering defect turns both of these red.
    assert "FAILED tests/x.py::test_y" in prepublish.fix_prompt([outcome], [])
    assert "FAILED tests/x.py::test_y" in (prepublish.pr_section(None, [outcome], []) or "")


def test_sharded_guards_keeps_a_timed_out_part_readable_and_the_budget_note_last(tmp_path):
    runner = _part_runner({
        "1/2": prepublish.Ran(0, _LONG_PASS),
        "2/2": prepublish.Ran(124, "fail pytest ran past 540 s\n[timed out after 600s]",
                              timed_out=True),
    })
    status, output = prepublish._sharded_guards(["bash", prepublish.GUARDS_SCRIPT], 2,
                                                lambda: 3600.0)(runner, tmp_path, {}, 600.0)
    # MUTATION: dropping a timed-out part's output turns this red.
    assert status == prepublish.FAILED and prepublish.guards_timed_out(output)
    assert "ran past 540 s" in prepublish.clean_output(output, [])

    left = iter([3600.0, 3600.0, 1.0])
    runner = _part_runner({
        "1/3": prepublish.Ran(1, "FAILED tests/x.py::test_y\n"),
        "2/3": prepublish.Ran(0, _LONG_PASS),
    })
    status, output = prepublish._sharded_guards(["bash", prepublish.GUARDS_SCRIPT], 3,
                                                lambda: next(left))(runner, tmp_path, {}, 600.0)
    assert status == prepublish.ERROR
    # MUTATION: dropping the budget note, or putting it before the parts, turns this red.
    assert output.rstrip().endswith("[the step's budget ran out before part 3 of 3]")
    assert "FAILED tests/x.py::test_y" in prepublish.clean_output(output, [])
