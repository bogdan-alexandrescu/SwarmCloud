"""The pre-publish tsc check generates Next.js's route types first (#1856).

Next.js writes `PageProps`/`LayoutProps` only during `next build` or `next
typegen`. The gate ran `tsc` alone on a clean checkout -- it fell back to
`--noEmit -p tsconfig.json` for any `typecheck` script that was not a plain
`tsc ...`, so `next typegen && tsc --noEmit` lost its typegen -- and tsc
reported 17 errors in route files the lane never touched (Product Oracle's
lanes, 2026-10-21). So, for a package that depends on `next` or whose
typecheck script runs `next typegen`, the check runs the package's own `next
typegen` first, inside the same time budget; when typegen cannot run, tsc's
failure is an advisory skip with the reason, never sent to the agent.

A fake runner records each command; nothing here starts node.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Mapping, Sequence

from agent_worker import prepublish

BUDGET = 300.0


class FakeRunner:
    """Records (tool, argv, cwd, timeout); answers from `codes` by tool name."""

    def __init__(self, codes: Mapping[str, int] | None = None, *,
                 timed_out: Sequence[str] = ()) -> None:
        self.codes = dict(codes or {})
        self.timed_out = set(timed_out)
        self.calls: list[tuple[str, list[str], Path, float]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> prepublish.Ran:
        tool = Path(argv[0]).name
        self.calls.append((tool, list(argv[1:]), Path(cwd), timeout))
        if tool in self.timed_out:
            return prepublish.Ran(124, "[timed out]", timed_out=True)
        code = self.codes.get(tool, 0)
        return prepublish.Ran(code, f"{tool} said {code}")


def _package(tmp_path: Path, manifest: dict, bins: Sequence[str] = ("tsc", "next")) -> Path:
    repo = tmp_path / "repo"
    package = repo / "web"
    (package / "app").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps(manifest))
    (package / "tsconfig.json").write_text("{}\n")
    (package / "app" / "page.tsx").write_text("export default function Page() { return null }\n")
    bin_dir = package / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    for name in bins:
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    return repo


def _tsc(repo: Path) -> prepublish.Check:
    checks = prepublish.graph_checks(repo, ["web/app/page.tsx"], base="HEAD", env={},
                                     snapshot=None)
    (tsc,) = [c for c in checks if c.name == "tsc"]
    assert tsc.run is not None, tsc.skipped
    return tsc


def _run(repo: Path, runner: FakeRunner) -> prepublish.Outcome:
    (outcome,) = prepublish.run_checks(repo, [_tsc(repo)], env={}, runner=runner,
                                       timeout=BUDGET)
    return outcome


def test_a_next_package_runs_its_own_next_typegen_before_tsc(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.5.0"},
                               "scripts": {"typecheck": "tsc --noEmit"}})
    runner = FakeRunner()
    outcome = _run(repo, runner)
    # MUTATION: drop the typegen step and the first call is tsc, not next.
    assert [(tool, argv) for tool, argv, _cwd, _t in runner.calls] == [
        ("next", ["typegen"]), ("tsc", ["--noEmit"])]
    # MUTATION: run typegen from the repository root and this cwd is `repo`.
    assert {cwd for _tool, _argv, cwd, _t in runner.calls} == {repo / "web"}
    # MUTATION: report typegen's run as the check's verdict and a passing tsc
    # could still read failed.
    assert outcome.status == prepublish.PASSED
    assert "next typegen" in _tsc(repo).command


def test_next_in_dev_dependencies_also_runs_typegen(tmp_path):
    repo = _package(tmp_path, {"devDependencies": {"next": "15.5.0"}})
    runner = FakeRunner()
    _run(repo, runner)
    # MUTATION: read only `dependencies` and typegen never runs here.
    assert [tool for tool, *_ in runner.calls] == ["next", "tsc"]
    # No typecheck script: today's fallback arguments.
    assert runner.calls[1][1] == ["--noEmit", "-p", "tsconfig.json"]


def test_the_next_typegen_and_tsc_script_is_that_pair_with_its_arguments(tmp_path):
    repo = _package(tmp_path, {"scripts": {
        "typecheck": "next typegen && tsc --noEmit -p tsconfig.app.json"}})
    runner = FakeRunner()
    outcome = _run(repo, runner)
    # MUTATION: keep falling back for every compound script and tsc gets
    # `-p tsconfig.json`; skip typegen for a script that runs it (no `next`
    # dependency here) and only tsc is called.
    assert [(tool, argv) for tool, argv, _cwd, _t in runner.calls] == [
        ("next", ["typegen"]), ("tsc", ["--noEmit", "-p", "tsconfig.app.json"])]
    assert outcome.status == prepublish.PASSED


def test_any_other_compound_script_keeps_the_fallback(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.5.0"},
                               "scripts": {"typecheck": "next typegen && tsc -p a.json && eslint ."}})
    runner = FakeRunner()
    _run(repo, runner)
    # MUTATION: strip `next typegen &&` and take the rest as tsc's arguments
    # and tsc is handed `&& eslint .`.
    assert runner.calls[-1][:2] == ("tsc", ["--noEmit", "-p", "tsconfig.json"])


def test_a_package_without_next_is_unchanged(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"react": "19.0.0"},
                               "scripts": {"typecheck": "tsc -b --noEmit"}})
    runner = FakeRunner({"tsc": 2})
    outcome = _run(repo, runner)
    # MUTATION: run typegen for every package and next is called here.
    assert [(tool, argv) for tool, argv, _cwd, _t in runner.calls] == [("tsc", ["-b", "--noEmit"])]
    # MUTATION: soften every tsc failure into a skip and this reads skipped.
    assert outcome.status == prepublish.FAILED
    assert outcome.reason is None
    assert "next" not in _tsc(repo).command


def test_a_failing_typegen_is_an_advisory_with_its_reason(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.0.0"}})
    runner = FakeRunner({"next": 1, "tsc": 2})
    outcome = _run(repo, runner)
    assert [tool for tool, *_ in runner.calls] == ["next", "tsc"]
    # MUTATION: report tsc's errors as the lane's failure and this reads failed,
    # which sends the missing route types to the agent as something to fix.
    assert outcome.status == prepublish.SKIPPED
    # MUTATION: drop the reason and the advisory cannot say why it skipped.
    assert outcome.reason and "next typegen" in outcome.reason
    assert "exited 1" in outcome.reason


def test_a_failing_typegen_does_not_hide_a_passing_tsc(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.0.0"}})
    outcome = _run(repo, FakeRunner({"next": 1}))
    # MUTATION: skip whenever typegen fails and a clean tsc is not reported.
    assert outcome.status == prepublish.PASSED


def test_a_next_package_without_the_next_binary_is_an_advisory(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.5.0"}}, bins=("tsc",))
    runner = FakeRunner({"tsc": 2})
    outcome = _run(repo, runner)
    # MUTATION: call a missing binary and the runner sees `next`.
    assert [tool for tool, *_ in runner.calls] == ["tsc"]
    assert outcome.status == prepublish.SKIPPED
    assert outcome.reason and "node_modules/.bin/next" in outcome.reason


def test_a_typegen_that_timed_out_leaves_tsc_unrun_as_an_advisory(tmp_path):
    repo = _package(tmp_path, {"dependencies": {"next": "15.5.0"}})
    runner = FakeRunner(timed_out=("next",))
    outcome = _run(repo, runner)
    # MUTATION: run tsc after a typegen that spent the whole budget and the
    # check runs past its limit.
    assert [tool for tool, *_ in runner.calls] == ["next"]
    # MUTATION: treat a timed-out typegen as success and this reads passed.
    assert outcome.status == prepublish.SKIPPED
    assert outcome.reason and "timed out" in outcome.reason


def test_tsc_gets_what_typegen_left_of_the_budget(tmp_path, monkeypatch):
    repo = _package(tmp_path, {"dependencies": {"next": "15.5.0"}})
    now = [1000.0]

    class Clocked(FakeRunner):
        def __call__(self, argv, cwd, env, timeout):
            if Path(argv[0]).name == "next":
                now[0] += 100.0
            return super().__call__(argv, cwd, env, timeout)

    monkeypatch.setattr(prepublish, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    runner = Clocked()
    _run(repo, runner)
    budgets = {tool: t for tool, _argv, _cwd, t in runner.calls}
    assert budgets["next"] == BUDGET
    # MUTATION: give tsc a fresh budget and this reads BUDGET, not BUDGET - 100.
    assert budgets["tsc"] == BUDGET - 100.0
