"""The pre-publish gate runs the tests the graph says cover the change (CTXQ-G).

docs/design/context-quality.md §4.6 and §7, lane CTXQ-G. §3 measured the
last 150 lane PRs: 8 root causes were a covering test nobody ran (class T)
and 14 a guard or house rule not met (class G), and `changed-guards.sh` is a
grep that runs no vitest, no `tsc` and no `terraform fmt`. So the gate now:

* selects test files through the staged graph snapshot's `tests` layer (and
  the changed test files, and a `.tsx`'s sibling `.test.tsx` when no
  snapshot is staged) and runs the Python ones changed-guards.sh did not
  already run, with `pytest -n auto`, and the JavaScript ones with the
  package's own `vitest run`;
* runs `tsc` when a TypeScript file of a package with a tsconfig changed, and
  `terraform fmt -check` on changed `.tf` files;
* writes an advisory section -- test reach (M11), the agent's self-check, what
  the graph selected -- that the worker appends to the pull request and no
  merge path reads.

These are §3's T and G rows replayed as fixtures. A tool the image lacks is
`skipped`, never sent to the agent as something to fix.

The snapshot is built by KG2's own extractor and writer from the fixture
checkout (`graphmcp_fixtures`), so the gate reads the format the indexer
writes today.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from agent_worker import lifecycle, prepublish

from graphmcp_fixtures import graph_of, write_snapshot
from test_prepublish_gate import (
    Turns,
    _change,
    _env,
    _gate,
    _git,
    _section,
    _tool_calls,
    _tools,
)
from test_publish_after_prepublish_rebase import _cloned
from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    _the_fence_holds,
    _the_agent_titles_its_pull_request,
    forge,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

WIDGET = "export function Widget(): string {\n  return 'w'\n}\n"
WIDGET_TEST = (
    "import { Widget } from './Widget'\n"
    "import { test, expect } from 'vitest'\n"
    "test('widget', () => { expect(Widget()).toBe('w') })\n"
)
MOD = "def covered():\n    return 1\n\n\ndef lonely():\n    return 2\n"
MOD_TEST = "from pkg.mod import covered\n\n\ndef test_covered():\n    assert covered() == 1\n"


def _fixture_repo(tmp_path: Path, *, snapshot: bool = True) -> tuple[Path, str]:
    """A checkout at tmp_path/repo with a UI package, a Python module and a
    Terraform file, and -- unless told not to -- its graph snapshot staged
    where the stager puts it: beside the checkout, in .swarm-context."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    files = {
        "apps/ui/package.json": '{"name": "ui", "scripts": {"typecheck": "tsc -b --noEmit"}}\n',
        "apps/ui/tsconfig.json": "{}\n",
        "apps/ui/src/Widget.tsx": WIDGET,
        "apps/ui/src/Widget.test.tsx": WIDGET_TEST,
        # A root Python project: graph-pytest runs each selected file from
        # the nearest project above it (#1756), and a file under none is skipped.
        "pyproject.toml": '[project]\nname = "fixture"\nversion = "0"\n',
        "pkg/__init__.py": "",
        "pkg/mod.py": MOD,
        "tests/test_mod.py": MOD_TEST,
        "infra/main.tf": 'resource "x" "y" {}\n',
        "README.md": "hello\n",
    }
    for path, text in files.items():
        _change(repo, path, text, commit=False)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    if snapshot:
        dest = tmp_path / prepublish.CONTEXT_DIR_NAME / prepublish.SNAPSHOT_DIR_NAME
        write_snapshot(graph_of(repo), dest)
    return repo, _git(repo, "rev-parse", "HEAD")


def _node_bins(repo: Path, package: str, names: tuple[str, ...] = ("vitest", "tsc")) -> None:
    """The package's own node_modules/.bin tools, as stand-ins that record argv
    and cwd, and fail on a file holding BAD (the shared FAKE_TOOL)."""
    from test_prepublish_gate import FAKE_TOOL

    bin_dir = repo / package / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\nimport os\n"
                        "os.environ['TOOL_LOG'] and open(os.environ['TOOL_LOG'] + '.cwd', 'a')"
                        ".write(os.getcwd() + '\\n')\n" + FAKE_TOOL)
        path.chmod(0o755)


def _names(checks: list[prepublish.Check]) -> list[str]:
    return [c.name for c in checks]


def _by_name(checks: list[prepublish.Check]) -> dict[str, prepublish.Check]:
    return {c.name: c for c in checks}


def _snap(tmp_path: Path):
    snapshot, _status = prepublish.load_snapshot(tmp_path / prepublish.CONTEXT_DIR_NAME)
    return snapshot


# ---------------------------------------------------------------------------
# Class T: a changed .tsx selects its vitest file; a changed module its pytest
# ---------------------------------------------------------------------------


def test_a_changed_tsx_selects_its_vitest_file_through_the_graph(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _node_bins(repo, "apps/ui")
    _change(repo, "apps/ui/src/Widget.tsx", WIDGET.replace("'w'", "'v'"), commit=False)
    env = _env(tmp_path, _tools(tmp_path, ()))
    checks = _by_name(prepublish.select_checks(repo, ["apps/ui/src/Widget.tsx"], base=base,
                                               env=env, snapshot=_snap(tmp_path)))
    # The graph's tests layer chose it (the sibling rule would also find this
    # one; the graph is asked first and says why).
    assert prepublish.select_tests(repo, ["apps/ui/src/Widget.tsx"], _snap(tmp_path)) == {
        "apps/ui/src/Widget.test.tsx": "graph"}
    vitest = checks["vitest"]
    assert vitest.run is not None and vitest.files == ("apps/ui/src/Widget.test.tsx",)
    ran = prepublish.run_checks(repo, [vitest], env=env)
    assert [(o.name, o.status) for o in ran] == [("vitest", "passed")]
    (call,) = _tool_calls(tmp_path)
    # Run from the package, with the path relative to it, so its config applies.
    assert call["tool"] == "vitest" and call["argv"][0] == "run"
    assert call["argv"][-1] == "src/Widget.test.tsx"
    assert Path((tmp_path / "tools.jsonl.cwd").read_text().split()[0]) == repo / "apps/ui"


def test_without_a_snapshot_the_sibling_test_file_is_still_selected(tmp_path):
    repo, base = _fixture_repo(tmp_path, snapshot=False)
    _node_bins(repo, "apps/ui")
    env = _env(tmp_path, _tools(tmp_path, ()))
    assert _snap(tmp_path) is None
    checks = _by_name(prepublish.select_checks(repo, ["apps/ui/src/Widget.tsx"], base=base,
                                               env=env, snapshot=None))
    assert checks["vitest"].files == ("apps/ui/src/Widget.test.tsx",)


def test_a_failing_vitest_file_is_sent_to_the_agent_and_fixed(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _node_bins(repo, "apps/ui", ("vitest",))
    _change(repo, "apps/ui/src/Widget.test.tsx", WIDGET_TEST + "// BAD\n")
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    turns = Turns(lambda: _change(repo, "apps/ui/src/Widget.test.tsx", WIDGET_TEST + "// ok\n"))
    record = _gate(repo, tmp_path, env, turns).run()
    assert len(turns.prompts) == 1 and "## vitest" in turns.prompts[0]
    assert record["fix_turn"] is True and record["failed"] == []
    statuses = [{c["name"]: c["status"] for c in r} for r in record["rounds"]]
    assert statuses[0]["vitest"] == "failed" and statuses[1]["vitest"] == "passed"


def test_a_changed_module_runs_its_graph_selected_pytest_minus_what_the_guards_ran(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    bin_dir = _tools(tmp_path, ("uv",))
    env = _env(tmp_path, bin_dir)
    # changed-guards.sh already selects tests/test_other.py; the gate does not
    # run it twice, and runs what only the graph found.
    (repo / "scripts").mkdir()
    (repo / "scripts" / "changed-guards.sh").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' tests/test_other.py 'bash scripts/lib/x.sh'\n")
    _change(repo, "tests/test_other.py", "def test_x():\n    pass\n", commit=False)
    _change(repo, "pkg/mod.py", MOD.replace("return 1", "return 3"), commit=False)
    files = ["pkg/mod.py", "tests/test_other.py"]
    checks = _by_name(prepublish.select_checks(repo, files, base=base, env=env,
                                               snapshot=_snap(tmp_path)))
    pytest_check = checks["graph-pytest"]
    assert set(pytest_check.files) == {"tests/test_mod.py", "tests/test_other.py"}
    (outcome,) = prepublish.run_checks(repo, [pytest_check], env=env)
    assert outcome.status == "passed", outcome.output
    (call,) = [c for c in _tool_calls(tmp_path) if c["tool"] == "uv"]
    assert "-n" in call["argv"] and call["argv"][call["argv"].index("-n") + 1] == "auto"
    assert "tests/test_mod.py" in call["argv"] and "tests/test_other.py" not in call["argv"]


def test_tsc_runs_when_a_typescript_file_of_a_package_changed(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    _node_bins(repo, "apps/ui")
    env = _env(tmp_path, _tools(tmp_path, ()))
    checks = _by_name(prepublish.select_checks(repo, ["apps/ui/src/Widget.tsx"], base=base,
                                               env=env, snapshot=None))
    tsc = checks["tsc"]
    (outcome,) = prepublish.run_checks(repo, [tsc], env=env)
    assert outcome.status == "passed"
    (call,) = [c for c in _tool_calls(tmp_path) if c["tool"] == "tsc"]
    # The package's own typecheck script's arguments, with its own tsc.
    assert call["argv"] == ["-b", "--noEmit"]
    assert "tsc" not in _names(prepublish.select_checks(repo, ["pkg/mod.py"], base=base,
                                                        env=env, snapshot=None))


# ---------------------------------------------------------------------------
# Class G: terraform fmt (#1217)
# ---------------------------------------------------------------------------


def test_a_changed_tf_file_runs_terraform_fmt_check(tmp_path):
    repo, base = _fixture_repo(tmp_path, snapshot=False)
    env = _env(tmp_path, _tools(tmp_path, ("terraform",)))
    _change(repo, "infra/main.tf", 'resource "x" "y" {}\n# BAD\n', commit=False)
    checks = _by_name(prepublish.select_checks(repo, ["infra/main.tf"], base=base, env=env))
    (outcome,) = prepublish.run_checks(repo, [checks["terraform-fmt"]], env=env)
    assert outcome.status == "failed"
    (call,) = _tool_calls(tmp_path)
    assert call["argv"][:2] == ["fmt", "-check"] and call["argv"][-1] == "infra/main.tf"


# ---------------------------------------------------------------------------
# A missing tool is skipped, never a failure sent to the agent
# ---------------------------------------------------------------------------


def test_missing_tools_are_skipped_with_a_reason_and_never_sent_to_the_agent(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    # No node_modules, no uv, no terraform on PATH.
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "apps/ui/src/Widget.tsx", WIDGET + "// BAD\n")
    _change(repo, "pkg/mod.py", MOD + "# BAD\n")
    _change(repo, "infra/main.tf", 'resource "x" "y" {}\n# BAD\n')
    turns = Turns()
    record = _gate(repo, tmp_path, env, turns).run()
    (round_,) = record["rounds"]
    by_name = {c["name"]: c for c in round_}
    for name in ("vitest", "tsc", "graph-pytest", "terraform-fmt"):
        assert by_name[name]["status"] == "skipped", (name, by_name[name])
        assert by_name[name]["reason"]
    assert "node_modules" in by_name["vitest"]["reason"]
    assert turns.prompts == [] and record["fix_turn"] is False
    assert _section(tmp_path) is None


# ---------------------------------------------------------------------------
# Advisory: test reach (M11), self-check presence, the PR section
# ---------------------------------------------------------------------------


def _advisory(tmp_path: Path) -> str | None:
    path = tmp_path / "artifacts" / prepublish.ADVISORY_SECTION_NAME
    return path.read_text() if path.exists() else None


def test_test_reach_lists_the_changed_symbols_no_test_covers(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "pkg/mod.py", MOD.replace("return 1", "return 3").replace("return 2", "return 4"))
    record = _gate(repo, tmp_path, env, Turns()).run()
    reach = record["test_reach"]
    assert reach["changed_symbols"] == 2 and reach["reached"] == 1
    assert reach["unreached"] == ["pkg/mod.py#lonely"]
    section = _advisory(tmp_path)
    assert section is not None and section.startswith(prepublish.ADVISORY_SECTION_HEADING)
    assert "1 of 2 changed symbols" in section and "`pkg/mod.py#lonely`" in section


def test_a_test_added_in_the_change_counts_as_reach(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "pkg/mod.py", MOD.replace("return 2", "return 4"))
    _change(repo, "tests/test_lonely.py",
            "from pkg.mod import lonely\n\n\ndef test_lonely():\n    assert lonely() == 4\n")
    record = _gate(repo, tmp_path, env, Turns()).run()
    assert record["test_reach"]["unreached"] == []
    assert record["test_reach"]["reached"] == record["test_reach"]["changed_symbols"] == 1


def test_without_a_snapshot_reach_is_unknown_never_complete(tmp_path):
    repo, base = _fixture_repo(tmp_path, snapshot=False)
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "pkg/mod.py", MOD.replace("return 2", "return 4"))
    record = _gate(repo, tmp_path, env, Turns()).run()
    assert record["test_reach"] is None
    section = _advisory(tmp_path) or ""
    assert "unknown" in section and "no graph snapshot" in section


def test_the_self_check_is_appended_when_present_and_named_when_missing(tmp_path):
    repo, base = _fixture_repo(tmp_path)
    env = {**_env(tmp_path, _tools(tmp_path, ())), prepublish.CLONE_BASE_ENV: base}
    _change(repo, "README.md", "changed\n")
    record = _gate(repo, tmp_path, env, Turns()).run()
    assert record["self_check"] is False
    assert "wrote no `self-check.md`" in (_advisory(tmp_path) or "")

    leaked = "ghp_" + "x" * 36
    (tmp_path / "artifacts" / prepublish.SELF_CHECK_NAME).write_text(
        f"| item | done | evidence |\n| ran the listed tests | done | 4 passed |\n{leaked}\n")
    record = _gate(repo, tmp_path, env, Turns(), secrets=(leaked,)).run()
    assert record["self_check"] is True
    section = _advisory(tmp_path) or ""
    assert "ran the listed tests" in section and leaked not in section
    # Advisory only: nothing failed, so there is no failed-checks section.
    assert _section(tmp_path) is None and record["failed"] == []


def test_the_runner_and_the_worker_agree_on_the_advisory_names():
    assert prepublish.ADVISORY_SECTION_NAME == lifecycle.PREPUBLISH_ADVISORY_FILE
    assert prepublish.ADVISORY_SECTION_HEADING == lifecycle.PREPUBLISH_ADVISORY_HEADING


def test_the_advisory_section_reaches_the_pull_request_without_marking_it_failed(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-advise",
                        dispatch={"strategy": "direct-pr"})
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "lane.txt").write_text("work\n")
    (worker.ws.artifacts / prepublish.ADVISORY_SECTION_NAME).write_text(
        f"{prepublish.ADVISORY_SECTION_HEADING}\n\n### Test reach (M11)\n1 of 2 reached\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")
    assert not out.get("prepublish_checks_failed")
    (pull,) = forge.pulls
    assert prepublish.ADVISORY_SECTION_HEADING in pull["body"] and "1 of 2 reached" in pull["body"]


def test_a_forged_advisory_file_is_not_appended(
    worker_factory, monkeypatch, origin, local_urls, forge, tmp_path
):
    worker, _ = _cloned(worker_factory, monkeypatch, origin, task_id="t-forged-adv",
                        dispatch={"strategy": "direct-pr"})
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "lane.txt").write_text("work\n")
    (worker.ws.artifacts / prepublish.ADVISORY_SECTION_NAME).write_text("anything else\n")
    worker._harvest_git(publish=True)
    (pull,) = forge.pulls
    assert "anything else" not in pull["body"]
