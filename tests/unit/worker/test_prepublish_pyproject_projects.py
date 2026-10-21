"""graph-pytest runs once per Python project, from that project's directory (#1756).

Lane PREPUB-PYPROJECT. Reported 2026-10-20 on sagaxyz/ai-studio PRs 715, 717,
718, 721 and 725: that repository has no root `pyproject.toml` -- its
projects are `api/` and `runner/` -- so the single `uv run pytest <files>`
the gate ran from the repository root found no project and failed in 0.0 s
with "Failed to spawn: `pytest`". Every lane then spent its fix turn proving
its code was fine.

So the gate groups the selected test files by the nearest directory above
each that holds a `pyproject.toml` (the way vitest groups by package.json),
runs one `uv run pytest` per project from that directory with paths relative
to it, and passes only when every project's run passes. A file under no
project, and a uv that cannot start pytest, could not be checked: SKIPPED,
never FAILED. The SwarmCloud repository itself -- one root project -- runs
the argv it always ran.

The runner here is a recording stand-in for `prepublish.Runner`: no uv runs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from agent_worker import prepublish
from agent_worker.prepublish import Ran

SPAWN = "error: Failed to spawn: `pytest`\n  Caused by: No such file or directory (os error 2)\n"


class Recorder:
    """A `prepublish.Runner`: records (argv, cwd) and answers each uv run with
    the reply for its directory (relative to the repository), else a pass.
    `listed` is what `changed-guards.sh --list` prints."""

    def __init__(self, repo: Path, replies: Mapping[str, Ran] | None = None,
                 listed: Sequence[str] = ()) -> None:
        self.repo = repo
        self.replies = dict(replies or {})
        self.listed = list(listed)
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> Ran:
        where = Path(cwd).resolve().relative_to(self.repo.resolve()).as_posix()
        self.calls.append((list(argv), where))
        if argv[:2] == ["bash", "scripts/changed-guards.sh"]:
            return Ran(0, "".join(f"{line}\n" for line in self.listed))
        return self.replies.get(where, Ran(0, f"{where}: 3 passed in 0.10s\n"))

    def uv_calls(self) -> list[tuple[list[str], str]]:
        return [(argv, where) for argv, where in self.calls if argv[0] == "uv"]


def _repo(tmp_path: Path, files: Mapping[str, str]) -> Path:
    repo = tmp_path / "repo"
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    return repo


def _studio(tmp_path: Path, **extra: str) -> Path:
    """ai-studio's shape: api/ and runner/ projects, no root pyproject."""
    return _repo(tmp_path, {
        "api/pyproject.toml": '[project]\nname = "api"\n',
        "api/tests/test_routes.py": "def test_routes():\n    pass\n",
        "api/tests/unit/test_models.py": "def test_models():\n    pass\n",
        "runner/pyproject.toml": '[project]\nname = "runner"\n',
        "runner/tests/test_run.py": "def test_run():\n    pass\n",
        **extra,
    })


def _run(repo: Path, tests: Sequence[str], runner: Recorder) -> tuple[str, str]:
    return prepublish._graph_pytest(tests, "base")(runner, repo, {}, 600.0)


def _files(argv: Sequence[str]) -> list[str]:
    return [a for a in argv if a.endswith(".py")]


def _junit(argv: Sequence[str]) -> list[str]:
    return [a for a in argv if a.startswith("--junitxml=")]


STUDIO_TESTS = ("api/tests/test_routes.py", "runner/tests/test_run.py",
                "api/tests/unit/test_models.py")


def test_two_projects_and_no_root_project_run_twice_from_their_own_directories(tmp_path):
    repo = _studio(tmp_path)
    runner = Recorder(repo)
    status, output = _run(repo, STUDIO_TESTS, runner)
    calls = runner.uv_calls()
    # MUTATION: running one uv from the repository root (the #1756 code) turns
    # this red: one call, from "." with repository-relative paths.
    assert [where for _argv, where in calls] == ["api", "runner"]
    (api_argv, _), (runner_argv, _) = calls
    # MUTATION: passing repository-relative paths (`api/tests/...`) turns this red.
    assert _files(api_argv) == ["tests/test_routes.py", "tests/unit/test_models.py"]
    assert _files(runner_argv) == ["tests/test_run.py"]
    # The argv the gate always ran, apart from the paths.
    for argv in (api_argv, runner_argv):
        # MUTATION: dropping `-n auto` or the junit report from the per-project argv turns this red.
        assert argv[:9] == ["uv", "run", "--quiet", "pytest", "-q", "-n", "auto", "-p",
                            "no:cacheprovider"]
        assert len(_junit(argv)) == 1
    # MUTATION: sharing one junit report between projects turns this red: the
    # second run would overwrite the first one's report before it is read.
    assert _junit(api_argv) != _junit(runner_argv)
    # MUTATION: returning PASSED without naming each project turns this red.
    assert status == prepublish.PASSED
    assert re.search(r"^api: passed$", output, re.M), output
    assert re.search(r"^runner: passed$", output, re.M), output


def test_one_failing_project_fails_the_check_and_is_named(tmp_path):
    repo = _studio(tmp_path)
    runner = Recorder(repo, {"runner": Ran(1, "FAILED tests/test_run.py::test_run\n")})
    status, output = _run(repo, STUDIO_TESTS, runner)
    # MUTATION: reading only the last (or the first) project's result turns this red.
    assert status == prepublish.FAILED
    assert re.search(r"^api: passed$", output, re.M), output
    assert re.search(r"^runner: failed$", output, re.M), output
    assert "FAILED tests/test_run.py::test_run" in output
    # Both projects ran: a failure in one does not hide the other.
    assert [where for _argv, where in runner.uv_calls()] == ["api", "runner"]


def test_a_file_outside_any_project_is_skipped_and_the_rest_still_run(tmp_path):
    repo = _studio(tmp_path, **{"scripts/test_tool.py": "def test_tool():\n    pass\n"})
    runner = Recorder(repo)
    status, output = _run(repo, ("api/tests/test_routes.py", "scripts/test_tool.py"), runner)
    # MUTATION: handing the orphan to some project's run (or the root) turns this red.
    assert [(where, _files(argv)) for argv, where in runner.uv_calls()] == [
        ("api", ["tests/test_routes.py"])]
    assert status == prepublish.PASSED
    # MUTATION: dropping the orphan without a note turns this red.
    assert "no Python project above scripts/test_tool.py; CI runs it" in output


def test_every_file_outside_any_project_skips_the_whole_check_and_runs_no_uv(tmp_path):
    repo = _studio(tmp_path, **{"scripts/test_tool.py": "def test_tool():\n    pass\n"})
    runner = Recorder(repo)
    status, output = _run(repo, ("scripts/test_tool.py",), runner)
    # MUTATION: running uv from the root for a file under no project (the
    # #1756 code) turns this red: it FAILS on "Failed to spawn".
    assert runner.uv_calls() == []
    assert status == prepublish.SKIPPED
    assert output == "no Python project above scripts/test_tool.py; CI runs it"
    # Through run_checks: SKIPPED with the note as its reason, never FAILED.
    check = prepublish.Check("graph-pytest", ("scripts/test_tool.py",), "uv run pytest",
                             run=prepublish._graph_pytest(["scripts/test_tool.py"], "base"))
    (outcome,) = prepublish.run_checks(repo, [check], env={}, runner=Recorder(repo))
    assert outcome.status == "skipped"
    assert outcome.reason == "no Python project above scripts/test_tool.py; CI runs it"


def test_uv_failing_to_spawn_pytest_is_skipped_with_uvs_first_line(tmp_path):
    repo = _studio(tmp_path)
    runner = Recorder(repo, {"api": Ran(2, SPAWN), "runner": Ran(2, SPAWN)})
    status, output = _run(repo, STUDIO_TESTS, runner)
    # MUTATION: reading uv's exit 2 as a failing test (the #1756 code) turns this red.
    assert status == prepublish.SKIPPED, output
    assert "error: Failed to spawn: `pytest`" in output
    assert "Caused by" not in output.splitlines()[0]


def test_uv_failing_to_spawn_in_a_root_project_is_skipped_with_that_line_as_the_reason(
        tmp_path):
    repo = _repo(tmp_path, {"pyproject.toml": '[project]\nname = "x"\n',
                            "tests/test_a.py": "def test_a():\n    pass\n"})
    check = prepublish.Check("graph-pytest", ("tests/test_a.py",), "uv run pytest",
                             run=prepublish._graph_pytest(["tests/test_a.py"], "base"))
    (outcome,) = prepublish.run_checks(repo, [check], env={},
                                       runner=Recorder(repo, {".": Ran(2, SPAWN)}))
    # MUTATION: matching only "Failed to spawn" anywhere in the output, or
    # keeping uv's whole text as the reason, turns this red.
    assert outcome.status == "skipped"
    assert outcome.reason == "error: Failed to spawn: `pytest`"


def test_uv_finding_no_project_is_skipped_too(tmp_path):
    repo = _studio(tmp_path)
    no_project = Ran(2, "error: No `project` table found in: `api/pyproject.toml`\n")
    runner = Recorder(repo, {"api": no_project})
    status, output = _run(repo, STUDIO_TESTS, runner)
    # One project could not run, the other passed: the check passes, and says so.
    # MUTATION: treating uv's "no project" as a failing test turns this red.
    assert status == prepublish.PASSED, output
    assert re.search(r"^api: skipped \(error: No `project` table found in", output, re.M)
    assert re.search(r"^runner: passed$", output, re.M)


def test_a_failing_test_that_mentions_spawning_is_still_failed(tmp_path):
    repo = _repo(tmp_path, {"pyproject.toml": "", "tests/test_a.py": ""})
    text = ("F\nE   AssertionError: error: Failed to spawn: `pytest`\n"
            "FAILED tests/test_a.py::test_a\n1 failed in 0.1s\n")
    runner = Recorder(repo, {".": Ran(1, text)})
    # MUTATION: searching the whole output for "Failed to spawn" turns this red.
    assert _run(repo, ("tests/test_a.py",), runner)[0] == prepublish.FAILED


def test_a_root_project_repository_runs_the_argv_it_always_ran(tmp_path):
    # SwarmCloud's shape: one root pyproject; app pyprojects hold no tests.
    repo = _repo(tmp_path, {
        "pyproject.toml": '[project]\nname = "swarm"\n',
        "apps/agent-worker/pyproject.toml": '[project]\nname = "agent-worker"\n',
        "tests/unit/worker/test_a.py": "",
        "tests/unit/worker/test_b.py": "",
    })
    tests = ("tests/unit/worker/test_a.py", "tests/unit/worker/test_b.py")
    reply = Ran(0, "2 passed in 0.10s\n")
    runner = Recorder(repo, {".": reply})
    status, output = _run(repo, tests, runner)
    (argv, where), = runner.uv_calls()
    # MUTATION: any change to the single root run's argv, cwd or output turns this red.
    assert where == "."
    assert argv[:9] == ["uv", "run", "--quiet", "pytest", "-q", "-n", "auto", "-p",
                        "no:cacheprovider"]
    assert len(_junit(argv)) == 1
    assert argv[10:] == list(tests)
    assert len(argv) == 12
    assert (status, output) == (prepublish.PASSED, reply.output)


def test_the_guards_selection_is_taken_out_of_each_project(tmp_path):
    repo = _studio(tmp_path, **{"scripts/changed-guards.sh": "#!/usr/bin/env bash\n"})
    runner = Recorder(repo, listed=["runner/tests/test_run.py", "bash scripts/lib/x.sh"])
    status, output = _run(repo, STUDIO_TESTS, runner)
    # MUTATION: comparing the guards' repository-relative paths against
    # project-relative ones (so nothing is taken out) turns this red.
    assert [where for _argv, where in runner.uv_calls()] == ["api"]
    assert status == prepublish.PASSED, output
    # Everything the graph found is run by the guards: no uv at all.
    runner = Recorder(repo, listed=list(STUDIO_TESTS))
    assert _run(repo, STUDIO_TESTS, runner)[0] == prepublish.PASSED
    assert runner.uv_calls() == []


def test_an_unreachable_service_in_one_project_skips_that_project_only(tmp_path):
    repo = _studio(tmp_path)
    refused = ("<testsuites><testsuite><testcase classname=\"t\" name=\"test_routes\">"
               "<error message='failed on setup with &quot;ConnectionRefusedError: [Errno 111] "
               "Connect call failed (&apos;127.0.0.1&apos;, 5432)&quot;'>E   refused</error>"
               "</testcase></testsuite></testsuites>")

    class Writes(Recorder):
        def __call__(self, argv, cwd, env, timeout):  # type: ignore[override]
            ran = super().__call__(argv, cwd, env, timeout)
            if argv[0] == "uv" and Path(cwd).name == "api":
                (report,) = _junit(argv)
                Path(report.split("=", 1)[1]).write_text(refused)
                return Ran(1, "E   ConnectionRefusedError\n")
            return ran

    runner = Writes(repo)
    status, output = _run(repo, STUDIO_TESTS, runner)
    # MUTATION: reading the unreachable-service report for the whole check
    # instead of per project turns this red (api's run would read as FAILED).
    assert status == prepublish.PASSED, output
    assert re.search(r"^api: skipped \(a service the tests need is unreachable "
                     r"\(127\.0\.0\.1:5432\)", output, re.M), output
    assert re.search(r"^runner: passed$", output, re.M), output
