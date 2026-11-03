"""A cut test map widens the gate's graph selection by the import pick.

The owner picked `widen-in-prepublish` on the index canary lane (2026-11-03,
part of #2688). The indexer caps `symbol_test_map` and `file_test_map` on a
large repository and lists the cut map in the snapshot manifest's
`truncated`; a changed module whose covering test fell off the map selected
no test in the pre-publish gate. So when either map is cut, the gate adds the
test files `scripts/related-tests.sh` names by imports (its listing, never
its `--run`), and the check's output says so. A whole map selects exactly
what it did before, and the script is not run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_worker import prepublish

from graphmcp_fixtures import graph_of, write_snapshot
from test_prepublish_gate import _change, _env, _git, _tool_calls, _tools
from test_prepublish_graph_gate import MOD, _by_name, _fixture_repo, _node_bins, _snap

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

IMPORTED = "tests/test_imported.py"
IMPORTED_UI = "apps/ui/src/Other.test.tsx"


def _restage(tmp_path: Path, repo: Path, truncated: list[str]) -> None:
    """The fixture's snapshot again, its manifest listing `truncated`."""
    document = graph_of(repo)
    document["truncated"] = truncated
    dest = tmp_path / prepublish.CONTEXT_DIR_NAME / prepublish.SNAPSHOT_DIR_NAME
    shutil.rmtree(dest, ignore_errors=True)
    write_snapshot(document, dest)


def _related_script(tmp_path: Path, repo: Path, *, code: int = 0) -> Path:
    """A stand-in related-tests.sh: its header, a Python and a vitest file the
    import map reaches, and its commands; each call's argv to a log."""
    log = tmp_path / "related.log"
    script = repo / prepublish.RELATED_SCRIPT
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f"echo \"$*\" >> '{log}'\n"
        "echo '==> 1 changed path(s); 2 related test file(s) in 2 run(s):'\n"
        f"echo '{IMPORTED}'\necho '{IMPORTED_UI}'\n"
        f"echo 'uv run pytest -q -n auto {IMPORTED}'\n"
        f"echo '(cd apps/ui && npx vitest run src/Other.test.tsx)'\n"
        f"exit {code}\n")
    script.chmod(0o755)
    return log


def _imported_tests(repo: Path) -> None:
    _change(repo, IMPORTED, "from pkg.mod import lonely\n\n\ndef test_lonely():\n"
                            "    assert lonely() == 2\n", commit=False)
    _change(repo, IMPORTED_UI, "import { test } from 'vitest'\ntest('o', () => {})\n",
            commit=False)


@pytest.mark.parametrize("cut", [["symbol_test_map"], ["file_test_map:over_cap"]])
def test_a_cut_test_map_selects_the_import_picks_tests(tmp_path, cut):
    repo, base = _fixture_repo(tmp_path)
    _imported_tests(repo)
    _restage(tmp_path, repo, cut)
    log = _related_script(tmp_path, repo)
    _node_bins(repo, "apps/ui", ("vitest",))
    _change(repo, "pkg/mod.py", MOD.replace("return 2", "return 2  # changed"), commit=False)
    env = _env(tmp_path, _tools(tmp_path, ("uv",)))
    checks = _by_name(prepublish.select_checks(repo, ["pkg/mod.py"], base=base, env=env,
                                               snapshot=_snap(tmp_path)))
    # The graph's own covering test stays; the import pick's tests join it.
    assert set(checks["graph-pytest"].files) == {"tests/test_mod.py", IMPORTED}
    assert checks["vitest"].files == (IMPORTED_UI,)
    # Listed against the round's base, never run by the script itself.
    assert log.read_text().split() == ["--base", base]
    (outcome,) = prepublish.run_checks(repo, [checks["graph-pytest"]], env=env)
    assert outcome.status == "passed", outcome.output
    assert outcome.output.startswith(
        f"the staged graph's {cut[0].split(':')[0]} is truncated, so the selection is "
        f"widened by the import pick ({prepublish.RELATED_SCRIPT}): it named 2 test file(s), "
        "2 not already selected")
    (call,) = [c for c in _tool_calls(tmp_path) if c["tool"] == "uv"]
    assert IMPORTED in call["argv"]


def test_the_pick_says_why_each_test_was_chosen(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _imported_tests(repo)
    _restage(tmp_path, repo, ["symbol_test_map", "file_test_map"])
    _related_script(tmp_path, repo)
    pick = prepublish.pick_tests(repo, ["pkg/mod.py"], _snap(tmp_path), base=base,
                                 env=_env(tmp_path, _tools(tmp_path, ())))
    assert pick.tests == {"tests/test_mod.py": "graph", IMPORTED: "import",
                          IMPORTED_UI: "import"}
    assert "file_test_map and symbol_test_map is truncated" in pick.note
    section = prepublish.advisory_section(
        graph={"snapshot": "s", "selected": pick.tests, "widened": pick.note},
        outcomes=[], reach=None, self_check=None, secrets=[])
    assert f"Widened: {pick.note}." in section


def test_a_whole_test_map_selects_exactly_todays_set_and_runs_no_script(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _imported_tests(repo)
    # Cut, but not a test map: the graph is still whole where tests are concerned.
    _restage(tmp_path, repo, ["call_edges:below_0.4", "context:history"])
    log = _related_script(tmp_path, repo)
    env = _env(tmp_path, _tools(tmp_path, ("uv",)))
    snapshot = _snap(tmp_path)
    pick = prepublish.pick_tests(repo, ["pkg/mod.py"], snapshot, base=base, env=env)
    assert pick == prepublish.TestPick(prepublish.select_tests(repo, ["pkg/mod.py"], snapshot))
    assert pick.tests == {"tests/test_mod.py": "graph"} and pick.note == ""
    checks = _by_name(prepublish.select_checks(repo, ["pkg/mod.py"], base=base, env=env,
                                               snapshot=snapshot))
    assert checks["graph-pytest"].files == ("tests/test_mod.py",)
    assert "vitest" not in checks
    assert not log.exists()


def test_a_failing_import_pick_keeps_the_graphs_selection_and_says_so(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _imported_tests(repo)
    _restage(tmp_path, repo, ["symbol_test_map"])
    _related_script(tmp_path, repo, code=2)
    pick = prepublish.pick_tests(repo, ["pkg/mod.py"], _snap(tmp_path), base=base,
                                 env=_env(tmp_path, _tools(tmp_path, ())))
    assert pick.tests == {"tests/test_mod.py": "graph"}
    assert pick.note == (
        "the staged graph's symbol_test_map is truncated and the import pick could not "
        f"widen the selection ({prepublish.RELATED_SCRIPT} exited 2); the graph's "
        "selection runs alone")


def test_without_the_related_script_a_cut_map_says_so(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _restage(tmp_path, repo, ["symbol_test_map"])
    pick = prepublish.pick_tests(repo, ["pkg/mod.py"], _snap(tmp_path), base=base,
                                 env=_env(tmp_path, _tools(tmp_path, ())))
    assert pick.tests == {"tests/test_mod.py": "graph"}
    assert f"the repository has no {prepublish.RELATED_SCRIPT}" in pick.note


def test_the_gate_records_the_widened_selection(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _imported_tests(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "imported tests")
    base = _git(repo, "rev-parse", "HEAD")
    _restage(tmp_path, repo, ["symbol_test_map"])
    _related_script(tmp_path, repo)
    _change(repo, "pkg/mod.py", MOD.replace("return 2", "return 2  # changed"))
    from test_prepublish_gate import Turns, _gate

    env = {**_env(tmp_path, _tools(tmp_path, ("uv",))), prepublish.CLONE_BASE_ENV: base}
    record = _gate(repo, tmp_path, env, Turns(lambda: None)).run()
    # The graph covers the committed Python test; only the import map reaches the UI one.
    assert record["graph"]["selected"][IMPORTED_UI] == "import"
    assert record["graph"]["widened"].startswith("the staged graph's symbol_test_map")
