"""A truncated graph test map widens the gate's graph-pytest check to the import pick.

Owner's pick `widen-in-prepublish` on PR #3105: a graph promoted with its
test maps cut (`truncated: symbol_test_map, file_test_map`) is carried, so a
staged snapshot may name only part of a changed module's tests -- or none.
`prepublish.select_tests` reads only the graph's tests rows, so with a cut
map graph-pytest ran fewer tests and said nothing. Now, when the snapshot's
manifest lists a test map as truncated, graph-pytest adds the pytest files
`scripts/related-tests.sh` names (its import map plus the graph's
tests_for), says how many it added, and runs even when the graph named none.
An uncut snapshot selects exactly as before.

The runner is a recording stand-in for `prepublish.Runner`: no uv runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_worker import prepublish
from agent_worker.prepublish import Ran

GRAPH_NAMED = "tests/unit/foo/test_core_0.py"
IMPORTERS = ("tests/unit/foo/test_core_1.py", "tests/unit/foo/test_edge.py")


class FakeSnapshot:
    """The parts of `graphmcp.snapshot.Snapshot` the gate's selection reads:
    the manifest, and a tests row for core.py naming one of its tests."""

    def __init__(self, truncated: Sequence[str]) -> None:
        self.manifest: dict[str, Any] = {"truncated": list(truncated)}

    def file(self, path: str) -> dict[str, Any] | None:
        if path == "apps/foo/foo/core.py":
            return {"path": path, "tests": [{"test": GRAPH_NAMED}]}
        return None

    def symbols_in(self, path: str) -> list[dict[str, Any]]:
        return []

    def tests(self, symbol: str) -> list[dict[str, Any]]:
        return []


class Recorder:
    """Answers `related-tests.sh` with the import pick, `changed-guards.sh
    --list` with nothing, and every uv run with a pass; records the calls."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> Ran:
        self.calls.append(list(argv))
        if argv[:2] == ["bash", prepublish.RELATED_SCRIPT]:
            listing = "".join(f"{t}\n" for t in (GRAPH_NAMED, *IMPORTERS))
            return Ran(0, f"{listing}\nuv run pytest -q {' '.join(IMPORTERS)}\n")
        if argv[:2] == ["bash", "scripts/changed-guards.sh"]:
            return Ran(0, "")
        return Ran(0, "3 passed in 0.10s\n")

    def pytest_files(self) -> list[str]:
        return sorted(a for argv in self.calls if argv[:1] == ["uv"]
                      for a in argv if a.endswith(".py"))

    def asked_related(self) -> bool:
        return any(argv[:2] == ["bash", prepublish.RELATED_SCRIPT] for argv in self.calls)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    files = {
        "pyproject.toml": '[project]\nname = "fixture"\n',
        prepublish.RELATED_SCRIPT: "#!/usr/bin/env bash\n",
        "apps/foo/foo/core.py": "def make():\n    return 1\n",
        GRAPH_NAMED: "def test_a():\n    pass\n",
        **{t: "def test_b():\n    pass\n" for t in IMPORTERS},
    }
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    return repo


def _graph_pytest(repo: Path, snapshot: FakeSnapshot) -> prepublish.Check:
    # A `uv` on PATH, so the check is chosen to run; the Recorder runs it.
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").chmod(0o755)
    env = {"PATH": str(bin_dir)}
    checks = prepublish.graph_checks(repo, ["apps/foo/foo/core.py"], base="base", env=env,
                                     snapshot=snapshot)
    [check] = [c for c in checks if c.name == "graph-pytest"]
    assert check.run is not None, check.skipped
    return check


def test_a_truncated_test_map_adds_the_import_pick(tmp_path):
    repo = _repo(tmp_path)
    check = _graph_pytest(repo, FakeSnapshot(["symbol_test_map", "file_test_map"]))
    runner = Recorder()
    status, output = check.run(runner, repo, {}, 600.0)
    assert status == prepublish.PASSED
    # MUTATION: graph-pytest running the graph's selection alone runs only
    # the one test the cut map still names.
    assert runner.pytest_files() == sorted({GRAPH_NAMED, *IMPORTERS})
    assert "test maps are truncated: 2 test file(s) added" in output
    assert "import pick" in check.command


def test_an_uncut_test_map_selects_as_before(tmp_path):
    repo = _repo(tmp_path)
    check = _graph_pytest(repo, FakeSnapshot(["routes"]))
    runner = Recorder()
    check.run(runner, repo, {}, 600.0)
    # MUTATION: widening on every snapshot costs each gate a related-tests run.
    assert not runner.asked_related()
    assert runner.pytest_files() == [GRAPH_NAMED]


def test_a_cut_map_that_names_no_test_still_runs_the_import_pick(tmp_path):
    class NamesNothing(FakeSnapshot):
        def file(self, path: str) -> dict[str, Any] | None:
            return None

    repo = _repo(tmp_path)
    check = _graph_pytest(repo, NamesNothing(["file_test_map"]))
    runner = Recorder()
    check.run(runner, repo, {}, 600.0)
    # MUTATION: creating graph-pytest only when the graph named a test skips
    # the check exactly when the cut map dropped every row.
    assert runner.pytest_files() == sorted({GRAPH_NAMED, *IMPORTERS})


def test_test_maps_truncated_reads_only_the_test_maps():
    assert prepublish.test_maps_truncated(FakeSnapshot(["symbol_test_map"]))
    assert not prepublish.test_maps_truncated(FakeSnapshot(["routes", "call_edges"]))
    assert not prepublish.test_maps_truncated(None)
