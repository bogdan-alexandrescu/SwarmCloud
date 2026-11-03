"""The changed-guards check is time-boxed and runs its parts side by side (GUARDS-TIMEBOX).

Owner decision, 3 November 2026 ("1+3"), after a full-day re-measure of the
pre-publish gate: builder steps went from 8.5 to 15.6 minutes of gate a run on
average (the slowest tenth from 36.8 to 64.3), 96% of it the changed-guards
check, whose shards ran one after another (90th percentile 79.5 minutes). So:

* the guards RELATED to the files the step changed (`related-tests.sh`) run
  first, with the parity check and the stylesheet guards;
* the rest of the selection runs as parts SIDE BY SIDE, as many at once as
  the pod's CPUs allow, and only while `GUARDS_BUDGET_SECONDS` lasts;
* what the budget does not reach is LEFT TO CI: named in the gate record and
  the advisory section of the pull request, never reported as passed;
* a failing guard still fails the gate and reaches the fix prompt.

The unit tests drive `timeboxed_guards` with a stand-in runner; the gate tests
run the REAL script in a scratch repository with a stand-in project python,
as test_prepublish_guards_dedup.py does.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import prepublish
from agent_worker.logs import build_logger

REPO = Path(__file__).resolve().parents[3]
PARITY = "bash scripts/lib/check-contract-parity.sh"
SELECTION = tuple(f"tests/unit/area/test_{name}.py" for name in "abcdefgh")
RELATED = ("tests/unit/area/test_c.py", "tests/unit/area/test_f.py")


def _listing(*tests: str, extras: tuple[str, ...] = (PARITY,)) -> str:
    """What `changed-guards.sh --list` prints, its notes merged in (stderr)."""
    return "\n".join(["==> 1 changed path(s); 8 test file(s) read them:", *tests, *extras]) + "\n"


class Script:
    """A stand-in for the guard script and related-tests.sh: answers `--list`
    and the related selection, and runs each `--only` part as `act` says."""

    def __init__(self, act: Any = None, *, listing: str | None = None,
                 related: tuple[str, ...] = RELATED, hold: float = 0.0) -> None:
        self.act = act or (lambda entries, timeout: prepublish.Ran(0, "ok part"))
        self.listing = _listing(*SELECTION) if listing is None else listing
        self.related = related
        self.hold = hold
        self.calls: list[tuple[list[str], list[str], float]] = []
        self.lock = threading.Lock()
        self.running = 0
        self.most = 0

    def __call__(self, argv: Any, cwd: Path, env: Any, timeout: float) -> prepublish.Ran:
        argv = list(argv)
        if prepublish.RELATED_SCRIPT in argv:
            self.calls.append((argv, [], timeout))
            return prepublish.Ran(0, "\n".join(self.related) + "\n\nuv run pytest -q x\n")
        if "--list" in argv:
            self.calls.append((argv, [], timeout))
            return prepublish.Ran(0, self.listing)
        entries = Path(argv[argv.index("--only") + 1]).read_text().splitlines()
        with self.lock:
            self.calls.append((argv, entries, timeout))
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            time.sleep(self.hold)
            return self.act(entries, timeout)
        finally:
            with self.lock:
                self.running -= 1

    def parts(self) -> list[tuple[list[str], list[str], float]]:
        return [c for c in self.calls if "--only" in c[0]]


def _repo(tmp_path: Path, *, related_script: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / prepublish.GUARDS_SCRIPT).write_text("")
    if related_script:
        (repo / prepublish.RELATED_SCRIPT).write_text("")
    return repo


def _run(tmp_path: Path, script: Script, *, cpus: int = 4, remaining: Any = None,
         clock: Any = time.monotonic, budget: float = prepublish.GUARDS_BUDGET_SECONDS,
         min_parts: int = 0, related_script: bool = True
         ) -> tuple[str, str, prepublish.GuardsRun]:
    repo = _repo(tmp_path, related_script=related_script)
    report = prepublish.GuardsRun()
    run = prepublish.timeboxed_guards(
        ["bash", prepublish.GUARDS_SCRIPT, "--base", "b"], report=report, cpus=cpus,
        remaining=remaining or (lambda: 3600.0), clock=clock, budget=budget,
        min_parts=min_parts, related_argv=["bash", prepublish.RELATED_SCRIPT, "--base", "b"],
        scratch=tmp_path)
    status, output = run(script, repo, {}, prepublish.CHECK_TIMEOUT_SECONDS)
    return status, output, report


# ---------------------------------------------------------------------------
# 1. The related guards run first
# ---------------------------------------------------------------------------


def test_the_related_guards_run_first_with_the_parity_check_on_every_cpu(tmp_path):
    script = Script()
    status, _, report = _run(tmp_path, script, min_parts=3)
    first, *rest = script.parts()
    # MUTATION: running the selection in its listed order, or the related
    # guards among the parts, turns this red.
    assert first[1] == [*RELATED, PARITY]
    assert "--workers" not in first[0]
    assert sorted(e for c in rest for e in c[1]) == sorted(set(SELECTION) - set(RELATED))
    assert all(PARITY not in c[1] for c in rest)
    assert status == prepublish.PASSED and report.left_to_ci == []
    assert "ran 2 related test file(s) first" in report.notes[0]


def test_without_a_related_selection_every_guard_is_a_part_and_parity_still_runs_first(tmp_path):
    script = Script()
    status, _, _ = _run(tmp_path, script, related_script=False, min_parts=2)
    first, *rest = script.parts()
    assert first[1] == [PARITY]
    assert sorted(e for c in rest for e in c[1]) == sorted(SELECTION)
    assert status == prepublish.PASSED


def test_the_parts_keep_the_listing_order_so_the_fixed_guards_run_first(tmp_path):
    parts = prepublish.cut_in_order(list(SELECTION), 3, lambda _: 1.0)
    assert [e for p in parts for e in p] == list(SELECTION)
    assert len(parts) == 3 and all(parts)
    heavy = prepublish.cut_in_order(["a", "b", "c", "d"], 2,
                                    {"a": 30.0, "b": 10.0, "c": 10.0, "d": 10.0}.__getitem__)
    assert heavy == [["a"], ["b", "c", "d"]]


# ---------------------------------------------------------------------------
# 2. The budget stops the rest, and what it stops is left to CI
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def spend(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


def test_the_budget_stops_the_rest_and_records_them_as_left_to_ci(tmp_path):
    clock = Clock()

    def act(entries: list[str], timeout: float) -> prepublish.Ran:
        clock.spend(250.0)  # every run takes 250 s of the 600 s budget
        return prepublish.Ran(0, "ok")

    # One CPU, so the parts run one at a time and the clock is the test's.
    script = Script(act)
    status, output, report = _run(tmp_path, script, cpus=1, clock=clock, min_parts=6)
    ran = script.parts()
    # The related run and one part fit in 600 s; the second part starts with
    # 100 s left and is given only that; the rest never start.
    assert len(ran) == 3
    assert ran[2][2] == pytest.approx(100.0)
    left = [e for e in SELECTION if e not in RELATED and e not in ran[1][1]
            and e not in ran[2][1]]
    # MUTATION: starting every part whatever the clock says, or counting the
    # parts not run as passed, turns this red.
    assert report.left_to_ci == left and left
    assert status == prepublish.PASSED
    assert f"{len(left)} guard entr(ies) not run within the budget: left to CI" in output
    assert "LEFT TO CI, not passed" in report.notes[0]


def test_a_part_stopped_at_the_budget_is_left_to_ci_not_a_timeout(tmp_path):
    def act(entries: list[str], timeout: float) -> prepublish.Ran:
        if RELATED[0] in entries:
            return prepublish.Ran(0, "ok")
        return prepublish.Ran(124, "....\n[timed out after 350s]", timed_out=True)

    status, output, report = _run(tmp_path, Script(act), min_parts=2)
    assert status == prepublish.PASSED
    assert sorted(report.left_to_ci) == sorted(set(SELECTION) - set(RELATED))
    # MUTATION: passing a budget stop's marker through turns the whole check
    # into a timeout, which the gate splits and runs again.
    assert not prepublish.guards_timed_out(output)


def test_nothing_run_within_the_budget_is_skipped_never_passed(tmp_path):
    left = iter([3600.0, 3600.0] + [5.0] * 20)
    status, output, report = _run(tmp_path, Script(), remaining=lambda: next(left))
    assert status == prepublish.SKIPPED
    assert "left to CI" in output
    assert set(report.left_to_ci) == {*SELECTION, PARITY}


# ---------------------------------------------------------------------------
# 3. The parts run side by side, up to the CPU count
# ---------------------------------------------------------------------------


def test_the_parts_run_concurrently_up_to_the_cpu_count(tmp_path):
    script = Script(hold=0.3)
    started = time.monotonic()
    status, _, report = _run(tmp_path, script, cpus=3, min_parts=6, related_script=False)
    elapsed = time.monotonic() - started
    parts = script.parts()[1:]
    assert status == prepublish.PASSED and len(parts) == 6
    # MUTATION: running the parts one after another turns this red (1 at a
    # time, 1.8 s); more threads than CPUs turns `most` past 3.
    assert script.most == 3
    assert elapsed < 6 * 0.3
    # Three slots on three CPUs: one pytest worker each.
    assert all(c[0][c[0].index("--workers") + 1] == "1" for c in parts)
    assert "3 at a time on 3 CPU(s)" in report.notes[0]


def test_fewer_parts_than_cpus_share_the_cpus_out(tmp_path):
    script = Script()
    _run(tmp_path, script, cpus=6, min_parts=2, related_script=False)
    parts = script.parts()[1:]
    assert len(parts) == 2
    assert all(c[0][c[0].index("--workers") + 1] == "3" for c in parts)


def test_the_pods_cpus_are_read_from_its_limit_and_the_platforms_worker_count(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.max").write_text("200000 100000\n")
    try:
        here = len(os.sched_getaffinity(0))
    except AttributeError:
        here = os.cpu_count() or 1
    assert prepublish.pod_cpus({}, cgroup=cgroup) == min(here, 2)
    (cgroup / "cpu.max").write_text("max 100000\n")
    assert prepublish.pod_cpus({}, cgroup=cgroup) == here
    assert prepublish.pod_cpus({prepublish.XDIST_WORKERS_ENV: "1"}, cgroup=cgroup) == 1
    assert prepublish.pod_cpus({prepublish.XDIST_WORKERS_ENV: "x"}, cgroup=cgroup) == here


# ---------------------------------------------------------------------------
# 4. A failing guard still fails the gate and reaches the fix prompt
# ---------------------------------------------------------------------------


#: More than OUTPUT_TAIL_CHARS of a passing part's own output.
_LONG_PASS = "".join(f"tests/unit/test_long.py::test_case_{i:03d} PASSED {'.' * 140}\n"
                     for i in range(24)) + "ok part\n"


def test_a_failing_part_fails_the_check_and_its_output_comes_last(tmp_path):
    def act(entries: list[str], timeout: float) -> prepublish.Ran:
        if SELECTION[0] in entries:
            return prepublish.Ran(1, "FAILED tests/unit/area/test_a.py::test_y - assert 1 == 2\n"
                                     "fail pytest failed (exit 1)\n")
        return prepublish.Ran(0, _LONG_PASS)

    status, output, _ = _run(tmp_path, Script(act), min_parts=3)
    assert status == prepublish.FAILED
    outcome = prepublish.Outcome(prepublish.GUARDS_CHECK, status, 1.0, ("apps/w/x.py",),
                                 "bash scripts/changed-guards.sh", output=output)
    # MUTATION: putting the long passing output after the failure cuts the
    # failure out of the tail the fix prompt and the PR section keep.
    assert "FAILED tests/unit/area/test_a.py::test_y" in prepublish.fix_prompt([outcome], [])
    assert "FAILED tests/unit/area/test_a.py::test_y" in (
        prepublish.pr_section(None, [outcome], []) or "")


def test_a_failure_before_the_budget_stop_still_reaches_the_fix_turn(tmp_path):
    def act(entries: list[str], timeout: float) -> prepublish.Ran:
        if SELECTION[0] in entries:
            return prepublish.Ran(124, "FAILED tests/unit/area/test_a.py::test_y\n"
                                       "[timed out after 300s]", timed_out=True)
        return prepublish.Ran(0, "ok")

    status, output, report = _run(tmp_path, Script(act), min_parts=3)
    assert status == prepublish.FAILED
    assert SELECTION[0] not in report.left_to_ci
    outcomes = prepublish._run_checks(
        tmp_path, [prepublish.Check(prepublish.GUARDS_CHECK, ("x",), "guards",
                                    run=lambda *a: (status, output))],
        env={}, runner=prepublish.run_command, timeout=600.0, clock=time.monotonic)
    # MUTATION: keeping the runner's kill marker reads this as a timeout,
    # which the fix prompt leaves out.
    assert outcomes[0].timed_out is False and outcomes[0].status == prepublish.FAILED


def test_a_selection_the_script_cannot_list_runs_as_one_command(tmp_path):
    calls: list[list[str]] = []

    def runner(argv: Any, cwd: Path, env: Any, timeout: float) -> prepublish.Ran:
        calls.append(list(argv))
        return prepublish.Ran(2 if "--list" in argv else 0, "")

    report = prepublish.GuardsRun()
    run = prepublish.timeboxed_guards(["bash", prepublish.GUARDS_SCRIPT], report=report,
                                      cpus=4, remaining=lambda: 3600.0)
    assert run(runner, _repo(tmp_path), {}, 600.0) == (prepublish.PASSED, "")
    assert calls == [["bash", prepublish.GUARDS_SCRIPT, "--list"],
                     ["bash", prepublish.GUARDS_SCRIPT]]


# ---------------------------------------------------------------------------
# 5. The script: --only and --workers
# ---------------------------------------------------------------------------


GUARDS = (
    "tests/unit/scripts/test_docs_describe_what_was_built.py",
    "tests/unit/scripts/test_docs_spec_amendments.py",
)
_IDENT = {"GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
          "GIT_COMMITTER_NAME": "lane", "GIT_COMMITTER_EMAIL": "lane@example.invalid"}
FAKE_PYTHON = r'''#!/usr/bin/env bash
printf 'pytest %s\n' "$*" >> "$CALLS"
exit 0
'''
FAKE_PARITY = '#!/usr/bin/env bash\nprintf "parity\\n" >> "$CALLS"\n'

needs_tools = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("jq") is None,
    reason="git and jq are needed to run the guard script")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **_IDENT}).stdout.strip()


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    repo = tmp_path / "repo"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "changed-guards.sh", repo / "scripts" / "changed-guards.sh")
    shutil.copy(REPO / "scripts" / "lib" / "common.sh", repo / "scripts" / "lib" / "common.sh")
    (repo / "scripts" / "lib" / "check-contract-parity.sh").write_text(FAKE_PARITY)
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
    dirs = []
    for tool in ("git", "jq", "bash", "head", "timeout"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    env = {"PATH": ":".join(dirs + ["/usr/bin", "/bin"]), "HOME": str(tmp_path),
           "NO_COLOR": "1", "SWARM_ENV_FILE": str(tmp_path / "no.env"),
           "CALLS": str(tmp_path / "calls"), prepublish.CLONE_BASE_ENV: base, **_IDENT}
    return {"repo": repo, "base": base, "env": env, "tmp": tmp_path}


def _script(lane: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "scripts/changed-guards.sh", "--base", lane["base"], *args],
                          cwd=lane["repo"], env=lane["env"], capture_output=True, text=True,
                          timeout=120)


def _calls(lane: dict[str, Any]) -> list[str]:
    path = lane["tmp"] / "calls"
    return path.read_text().splitlines() if path.exists() else []


@needs_tools
def test_only_runs_the_named_entries_and_parity_only_when_named(lane):
    only = lane["tmp"] / "only.txt"
    only.write_text(f"{GUARDS[1]}\ntests/unit/not/selected/test_x.py\n")
    ran = _script(lane, "--only", str(only), "--workers", "2")
    assert ran.returncode == 0, ran.stderr
    (call,) = _calls(lane)
    # MUTATION: ignoring --only runs both guards; ignoring --workers runs -n auto.
    assert GUARDS[1] in call and GUARDS[0] not in call and "test_x.py" not in call
    assert " -n 2 " in call
    # A subset of the selection is not the selection: no record.
    gitdir = Path(_git(lane["repo"], "rev-parse", "--absolute-git-dir"))
    assert not (gitdir / prepublish.GUARDS_RECORD_DIR / "all.json").exists()

    (lane["tmp"] / "calls").unlink()
    only.write_text(f"{PARITY}\n")
    assert _script(lane, "--only", str(only)).returncode == 0
    assert _calls(lane) == ["parity"]


@needs_tools
def test_workers_takes_a_count_or_auto(lane):
    assert _script(lane, "--workers", "0").returncode == 2
    assert _script(lane, "--workers", "auto").returncode == 0
    assert " -n auto " in _calls(lane)[0]


@needs_tools
def test_the_gate_runs_the_real_script_related_first_then_the_parts(lane):
    artifacts = lane["tmp"] / "artifacts"
    artifacts.mkdir()
    env = {**lane["env"], prepublish.XDIST_WORKERS_ENV: "2"}
    gate = prepublish.Gate(repo=lane["repo"], env=env, secrets=(), resume=lambda text: True,
                           remaining=lambda: 3600.0,
                           log=build_logger(task_id="t", attempt_id="a", tenant_id="acme",
                                            generation=1, runner_profile="claude-code",
                                            stream=io.StringIO()),
                           artifacts_dir=artifacts, python=sys.executable)
    record = gate.run()
    (guards,) = [c for c in record["rounds"][0] if c["name"] == prepublish.GUARDS_CHECK]
    assert guards["status"] == "passed" and "left_to_ci" not in guards
    calls = _calls(lane)
    # Parity first (the related run: no related-tests.sh here), then the guards.
    assert calls[0] == "parity"
    assert sorted(g for c in calls[1:] for g in GUARDS if g in c) == sorted(GUARDS)
    assert "related test file(s) first" in record["guards"][0]
