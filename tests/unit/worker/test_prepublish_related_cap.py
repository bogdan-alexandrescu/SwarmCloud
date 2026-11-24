"""The gate's report says when related-tests capped the selection it ran.

Review of the RELATED-CAP pull request, 2026-11-23 (major): the gate kept only
the test-file lines of `scripts/related-tests.sh`'s output and dropped the one
note a capped selection prints on stderr ("the selection is capped at N test
file(s) ... of the M its full closure reaches; CI (the merge train) runs the
closure"). So with a cut test map the gate ran fewer tests than the closure
and its own report never said so; a test outside the cap then failed only on
the merge train, with no word of it. Now the cap is carried in the pick's note
(`TestPick.note`, the "Widened:" line of prepublish-checks.md, which the
worker appends to the pull request's body) and in graph-pytest's widen note:
how many of how many, and that the merge train runs the rest. An uncapped
selection reads as before.

The runner is a recording stand-in for `prepublish.Runner`: no uv runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_worker import prepublish
from agent_worker.prepublish import Ran

CORE = "apps/foo/foo/core.py"
GRAPH_NAMED = "tests/unit/foo/test_core_0.py"
IMPORTERS = ("tests/unit/foo/test_core_1.py", "tests/unit/foo/test_edge.py")
# What related-tests.sh prints for a capped selection: its `warn` line on
# stderr, which the gate's runner merges into the output, in colour or not.
CAP_NOTE = ("the closure's recorded CI weight is 3954 s, more than one 540 s run: the selection is "
            "capped at 3 test file(s) -- the change's own tests, the direct importers of each changed "
            "module, the code graph's tests_for and the tests that name a changed path -- of the 189 "
            "its full closure reaches; CI (the merge train) runs the closure. --closure prints all "
            "of them")
CAPPED_WARN = (f"\033[33mwarn\033[0m {CAP_NOTE}",
               f"warn {CAP_NOTE}")


class FakeSnapshot:
    """A staged snapshot whose test maps are cut, naming one of core.py's tests."""

    manifest: dict[str, Any] = {"truncated": ["symbol_test_map", "file_test_map"]}

    def file(self, path: str) -> dict[str, Any] | None:
        if path == CORE:
            return {"path": path, "tests": [{"test": GRAPH_NAMED}]}
        return None

    def symbols_in(self, path: str) -> list[dict[str, Any]]:
        return []

    def tests(self, symbol: str) -> list[dict[str, Any]]:
        return []


class Recorder:
    """Answers `related-tests.sh` with its listing (and `warn`, a note line,
    when given), `changed-guards.sh --list` with nothing, uv with a pass."""

    def __init__(self, warn: str = "") -> None:
        self.warn = warn
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> Ran:
        self.calls.append(list(argv))
        if argv[:2] == ["bash", prepublish.RELATED_SCRIPT]:
            listing = "".join(f"{t}\n" for t in (GRAPH_NAMED, *IMPORTERS))
            lead = f"{self.warn}\n" if self.warn else ""
            return Ran(0, f"{lead}{listing}\nuv run pytest -q {' '.join(IMPORTERS)}\n")
        if argv[:2] == ["bash", "scripts/changed-guards.sh"]:
            return Ran(0, "")
        return Ran(0, "3 passed in 0.10s\n")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    files = {
        "pyproject.toml": '[project]\nname = "fixture"\n',
        prepublish.RELATED_SCRIPT: "#!/usr/bin/env bash\n",
        CORE: "def make():\n    return 1\n",
        GRAPH_NAMED: "def test_a():\n    pass\n",
        **{t: "def test_b():\n    pass\n" for t in IMPORTERS},
    }
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    return repo


def _report(pick: prepublish.TestPick) -> str:
    """prepublish-checks.md as the gate writes it for this pick."""
    graph = {"snapshot": "staged", "selected": pick.tests}
    if pick.note:
        graph["widened"] = pick.note
    return prepublish.advisory_section(graph=graph, outcomes=[], reach=None, self_check=None,
                                       secrets=[])


def _says_capped(text: str) -> None:
    assert "capped" in text, text
    assert "3 of the 189" in text, text
    assert "merge train runs the rest" in text, text
    assert "left to CI" in text, text


def test_a_capped_import_pick_is_named_in_the_report(tmp_path):
    repo = _repo(tmp_path)
    for warn in CAPPED_WARN:
        pick = prepublish.pick_tests(repo, [CORE], FakeSnapshot(), base="base", env={},
                                     runner=Recorder(warn))
        assert set(pick.tests) == {GRAPH_NAMED, *IMPORTERS}
        # MUTATION: keep only the test-file lines -> the note and the report
        # never say the selection was capped.
        _says_capped(pick.note)
        _says_capped(_report(pick))
        assert "\033" not in pick.note


def test_an_uncapped_import_pick_reads_as_before(tmp_path):
    repo = _repo(tmp_path)
    pick = prepublish.pick_tests(repo, [CORE], FakeSnapshot(), base="base", env={},
                                 runner=Recorder())
    assert pick.note == ("the staged graph's file_test_map and symbol_test_map is truncated, so "
                         f"the selection is widened by the import pick ({prepublish.RELATED_SCRIPT}): "
                         "it named 3 test file(s), 2 not already selected")
    assert "capped" not in _report(pick)


def _graph_pytest(repo: Path) -> prepublish.Check:
    bin_dir = repo.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "uv").chmod(0o755)
    # The graph's selection alone, so graph-pytest asks related-tests itself.
    pick = prepublish.TestPick({GRAPH_NAMED: "graph"})
    checks = prepublish.graph_checks(repo, [CORE], base="base", env={"PATH": str(bin_dir)},
                                     snapshot=FakeSnapshot(), pick=pick)
    [check] = [c for c in checks if c.name == "graph-pytest"]
    assert check.run is not None, check.skipped
    return check


def test_graph_pytests_widen_note_names_a_capped_selection(tmp_path):
    repo = _repo(tmp_path)
    check = _graph_pytest(repo)
    status, output = check.run(Recorder(CAPPED_WARN[0]), repo, {}, 600.0)
    assert status == prepublish.PASSED
    assert "test maps are truncated: 2 test file(s) added" in output
    # MUTATION: _import_pick returning the files alone -> no word of the cap.
    _says_capped(output)


def test_graph_pytests_widen_note_reads_as_before_when_uncapped(tmp_path):
    repo = _repo(tmp_path)
    check = _graph_pytest(repo)
    status, output = check.run(Recorder(), repo, {}, 600.0)
    assert status == prepublish.PASSED
    assert output.startswith(f"the staged graph's test maps are truncated: 2 test file(s) "
                             f"added from {prepublish.RELATED_SCRIPT}'s import pick\n")
    assert "capped" not in output
