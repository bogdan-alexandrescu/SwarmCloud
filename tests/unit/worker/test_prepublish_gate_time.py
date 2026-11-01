"""The pre-publish gate spends no time it need not, and never outlives its runner (#2981).

Measured on the code intelligence experiment (2026-11-01, 16 steps): every
review spent 65-77 minutes in the gate re-running `changed-guards` on a branch
whose one commit was the implement step's patch it applied; the first guard
run burned its 600 s check limit on every step before the gate split it into
shards; and one review (task_2518ef3904ed47d0ac52) was killed by the worker
while its gate was still running. What holds now:

* a branch that adds nothing of the step's own -- no change at all, or exactly
  its staged input patch applied to its base -- runs no checks, and the record
  says why (`adds_nothing`); read from git, never from the step's name;
* a selection the guard script itself says is too heavy for one run starts
  as the parts it advises (`guards_split`);
* a check killed at its limit returns even when a process it started left
  its process group and holds the output pipe open (`run_command`'s drain).

These run the REAL guard script in a scratch repository with a stand-in
project python, as test_prepublish_guards_dedup.py does.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
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

#: The stand-in project python: logs each pytest call to $CALLS and passes.
FAKE_PYTHON = r'''#!/usr/bin/env bash
printf 'pytest %s\n' "$*" >> "$CALLS"
exit 0
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
    dirs = []
    for tool in ("git", "jq", "bash", "head", "timeout"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    env = {"PATH": ":".join(dirs + ["/usr/bin", "/bin"]), "HOME": str(tmp_path),
           "NO_COLOR": "1", "SWARM_ENV_FILE": str(tmp_path / "no.env"),
           "CALLS": str(tmp_path / "calls"), prepublish.CLONE_BASE_ENV: base, **_IDENT}
    return {"repo": repo, "base": base, "env": env, "tmp": tmp_path}


def _upstream_patch(lane: dict[str, Any]) -> Path:
    """The implement step's `swarm-work.patch`: a change made on a scratch
    branch, as a diff from the base; the checkout is back on the base."""
    repo = lane["repo"]
    _git(repo, "checkout", "-q", "-b", "upstream")
    (repo / "apps" / "w").mkdir(parents=True)
    (repo / "apps" / "w" / "notes.txt").write_text("the implement step's change\n")
    (repo / "apps" / "w" / "more.txt").write_text("and another file\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "implement")
    patch = lane["tmp"] / "work" / "swarm-work.patch"
    patch.parent.mkdir()
    patch.write_text(_git(repo, "diff", "--binary", lane["base"], "HEAD") + "\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-q", "-D", "upstream")
    return patch


def _apply_as_one_commit(lane: dict[str, Any], patch: Path) -> None:
    """What a review step does first: applies its upstream patch, one commit."""
    _git(lane["repo"], "apply", "--index", str(patch))
    _git(lane["repo"], "commit", "-q", "-m", "the implement step's patch, applied for review")


def _calls(lane: dict[str, Any]) -> list[str]:
    path = Path(lane["env"]["CALLS"])
    return path.read_text().splitlines() if path.exists() else []


def _gate(lane: dict[str, Any], **kwargs: Any) -> prepublish.Gate:
    artifacts = lane["tmp"] / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=lane["repo"], env=lane["env"], secrets=(),
                           resume=lambda text: True, remaining=lambda: 3600.0, log=_log(),
                           artifacts_dir=artifacts, python=sys.executable, **kwargs)


# ---------------------------------------------------------------------------
# 1. A step that adds nothing of its own runs no checks
# ---------------------------------------------------------------------------


def test_a_review_that_only_applied_its_upstream_patch_runs_no_checks(lane):
    patch = _upstream_patch(lane)
    _apply_as_one_commit(lane, patch)
    record = _gate(lane, staged_patches=(str(patch),)).run()
    # MUTATION: dropping the `adds_nothing` test in `Gate.run` runs the round
    # (the guard script among it) and turns all three red.
    assert record["skipped"] == "adds_nothing"
    assert "staged input patch (swarm-work.patch)" in record["skipped_reason"]
    assert record["rounds"] == [] and _calls(lane) == []


def test_a_branch_with_no_commit_at_all_runs_no_checks(lane):
    record = _gate(lane).run()
    assert record["skipped"] == "adds_nothing"
    assert record["skipped_reason"] == "the branch changes nothing beyond its base"
    assert record["rounds"] == []


def test_a_fix_step_that_changes_the_applied_patch_is_checked_as_before(lane):
    patch = _upstream_patch(lane)
    _apply_as_one_commit(lane, patch)
    (lane["repo"] / "apps" / "w" / "notes.txt").write_text("the fix step's own change\n")
    record = _gate(lane, staged_patches=(str(patch),)).run()
    # MUTATION: comparing only HEAD's tree (not the uncommitted change the
    # publish commits) skips this step and turns it red.
    assert "skipped" not in record
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    assert guards["status"] == "passed" and len(_calls(lane)) == 1


def test_an_author_step_with_a_commit_of_its_own_is_checked_as_before(lane):
    (lane["repo"] / "apps" / "w").mkdir(parents=True)
    (lane["repo"] / "apps" / "w" / "notes.txt").write_text("the implement step's change\n")
    _git(lane["repo"], "add", "-A")
    _git(lane["repo"], "commit", "-q", "-m", "implement")
    # Named like a review's input, but no staged patch reproduces this tree.
    record = _gate(lane).run()
    assert "skipped" not in record and len(record["rounds"]) == 1


def test_a_patch_that_does_not_reproduce_the_tree_is_not_a_skip(lane):
    patch = _upstream_patch(lane)
    _apply_as_one_commit(lane, patch)
    other = lane["tmp"] / "other.patch"
    other.write_text(patch.read_text().replace("and another file", "something else"))
    assert prepublish.adds_nothing(lane["repo"], lane["base"], [other],
                                   env=lane["env"]) is None
    assert prepublish.adds_nothing(lane["repo"], lane["base"], [lane["tmp"] / "missing.patch"],
                                   env=lane["env"]) is None


# ---------------------------------------------------------------------------
# 2. A selection too heavy for one run starts sharded
# ---------------------------------------------------------------------------


def _heavy(lane: dict[str, Any], seconds: int) -> None:
    """CI's recorded weights: each guard file `seconds` CI-seconds."""
    durations = lane["repo"] / "tests" / "unit" / "scripts" / "unit_test_durations.json"
    durations.write_text(json.dumps({guard: seconds for guard in GUARDS}))
    (lane["repo"] / "apps" / "w").mkdir(parents=True, exist_ok=True)
    (lane["repo"] / "apps" / "w" / "notes.txt").write_text("the lane's change\n")
    _git(lane["repo"], "add", "-A")
    _git(lane["repo"], "commit", "-q", "-m", "change")


def test_a_selection_the_script_says_is_too_heavy_starts_as_its_parts(lane):
    _heavy(lane, 400)  # 1,600 CI-seconds: the script advises 3 parts of 540
    record = _gate(lane).run()
    (round_,) = record["rounds"]
    (guards,) = [c for c in round_ if c["name"] == "changed-guards"]
    # MUTATION: dropping `guards_split` from `Gate._guards_check` runs it
    # unsharded first (one command, its parts in turn inside it, which the
    # 600 s check limit killed on every step of #2981) and turns this red.
    (note,) = record["guards"]
    assert "too heavy for one run" in note and "--shard 1/3 ... --shard 3/3" in note
    assert guards["status"] == "passed"
    assert len(_calls(lane)) == 3
    advisory = (lane["tmp"] / "artifacts" / prepublish.ADVISORY_SECTION_NAME).read_text()
    assert "too heavy for one run" in advisory


def test_a_selection_one_run_holds_runs_as_one_command(lane):
    _heavy(lane, 10)
    record = _gate(lane).run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == "changed-guards"]
    assert record["guards"] == [] and guards["status"] == "passed"
    assert len(_calls(lane)) == 1


# ---------------------------------------------------------------------------
# 3. A killed check returns, whatever it left behind
# ---------------------------------------------------------------------------


#: A check that starts a process in a session of its own (out of the check's
#: process group, so the group kill misses it) holding stdout, then hangs.
ESCAPES = r'''
import subprocess, sys, time
held = subprocess.Popen(["sleep", "60"], start_new_session=True)
open(sys.argv[1], "w").write(str(held.pid))
print("started", flush=True)
time.sleep(60)
'''


def test_a_check_killed_at_its_limit_returns_though_a_process_it_started_holds_its_output(
    tmp_path,
):
    pid_file = tmp_path / "held.pid"
    started = time.monotonic()
    try:
        ran = prepublish.run_command([sys.executable, "-c", ESCAPES, str(pid_file)], tmp_path,
                                     dict(os.environ), 2.0)
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
    # MUTATION: an unbounded `communicate()` after the kill waits the 60 s
    # the escaped process holds the pipe, and turns this red.
    assert elapsed < 2.0 + 3 * prepublish.KILL_DRAIN_SECONDS, elapsed
    assert ran.timed_out and ran.code == 124
    assert "started" in ran.output and "[timed out after 2s]" in ran.output
    assert prepublish.killed_at_limit(ran.output)


def test_a_part_never_starts_with_less_than_the_gate_needs(lane):
    """The parts stop once less than `MIN_GATE_SECONDS` is left, and each part
    keeps `CHECK_MARGIN_SECONDS` back from the deadline."""
    given: list[float] = []
    left = iter([3000.0, 400.0, 100.0])

    def runner(argv, cwd, env, timeout):
        given.append(timeout)
        return prepublish.Ran(0, "ok")

    run = prepublish._sharded_guards(["bash", prepublish.GUARDS_SCRIPT], 3, lambda: next(left))
    status, output = run(runner, lane["repo"], lane["env"], 600.0)
    assert given == [600.0, 400.0 - prepublish.CHECK_MARGIN_SECONDS]
    assert status == prepublish.ERROR and "before part 3 of 3" in output
