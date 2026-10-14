"""A pull request runs the unit tests its change reaches (B2, owner decision
2026-10-14; docs/ci.md "A pull request runs the unit tests its change
reaches").

scripts/lib/select_unit_tests.py decides, from a pull request's changed paths,
which test files the shards run -- or that it cannot vouch for a selection and
the whole suite runs. Main's push run and a merge-train run keep the whole
suite, so a miss here costs a red main, never an untested release; these tests
hold each rule, and each has a control that shows it could have come out the
other way.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SELECT = REPO / "scripts" / "lib" / "select_unit_tests.py"
APPLICATION = REPO / ".github" / "workflows" / "application.yml"

_spec = importlib.util.spec_from_file_location("select_unit_tests", SELECT)
sel = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sel)


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


# A tree with enough unrelated tests that a two-file selection is well under
# FULL_FRACTION of the suite.
FILLER = {f"tests/unit/z/test_filler_{i}.py": "def test_x():\n    pass\n" for i in range(12)}


# --------------------------------------------------------------------------
# When it runs everything.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    ".github/workflows/application.yml",
    ".github/actions/release-images/action.yml",
    "tests/unit/conftest.py",
    "tests/unit/worker/conftest.py",
    "pyproject.toml",
    "apps/scheduler/pyproject.toml",
    "uv.lock",
    "tests/flaky.txt",
    "tests/unit/flaky_quarantine.py",
    "apps/common/swarm_common/models.py",
])
def test_a_change_to_how_every_test_runs_runs_everything(path):
    """MUTATION: drop any entry from FALLBACK_*."""
    assert sel.fallback_reason([path]) is not None, path
    # The control: an ordinary source file beside it is selectable.
    assert sel.fallback_reason(["apps/scheduler/scheduler/dispatch.py"]) is None


def test_more_than_the_cap_runs_everything():
    many = [f"apps/x/m{i}.py" for i in range(sel.MAX_CHANGED + 1)]
    assert "more than" in (sel.fallback_reason(many) or "")
    assert sel.fallback_reason(many[: sel.MAX_CHANGED]) is None


def test_the_fallback_is_what_select_reports(tmp_path):
    root = _tree(tmp_path, {**FILLER, "tests/unit/a/test_a.py": "def test_a(): pass\n"})
    out = sel.select(root, ["uv.lock"])
    assert out["python"] == "all" and out["tests"] == [] and "uv.lock" in out["why"], out


# --------------------------------------------------------------------------
# What it selects.
# --------------------------------------------------------------------------
def test_a_test_that_imports_a_changed_module_through_a_chain_is_selected(tmp_path):
    root = _tree(tmp_path, {
        **FILLER,
        "apps/svc/pkg/__init__.py": "",
        "apps/svc/pkg/core.py": "def f():\n    return 1\n",
        "apps/svc/pkg/mid.py": "from pkg.core import f\n",
        "apps/svc/pkg/rel.py": "from .core import f\n",
        "apps/svc/pkg/other.py": "def g():\n    return 2\n",
        "tests/unit/svc/test_mid.py": "from pkg.mid import f\n",
        "tests/unit/svc/test_rel.py": "import pkg.rel\n",
        "tests/unit/svc/test_other.py": "from pkg.other import g\n",
    })
    out = sel.select(root, ["apps/svc/pkg/core.py"])
    assert out["python"] == "selected", out
    assert "tests/unit/svc/test_mid.py" in out["tests"], out          # absolute, through mid
    assert "tests/unit/svc/test_rel.py" in out["tests"], out          # relative import in rel
    assert "tests/unit/svc/test_other.py" not in out["tests"], out    # the control
    assert not any("filler" in t for t in out["tests"]), out


def test_a_changed_package_init_reaches_every_importer_of_the_package(tmp_path):
    root = _tree(tmp_path, {
        **FILLER,
        "apps/svc/pkg/__init__.py": "X = 1\n",
        "apps/svc/pkg/core.py": "",
        "tests/unit/svc/test_core.py": "from pkg.core import nothing\n",
    })
    out = sel.select(root, ["apps/svc/pkg/__init__.py"])
    assert "tests/unit/svc/test_core.py" in out["tests"], out


def test_a_changed_script_reaches_the_tests_of_every_script_that_sources_it(tmp_path):
    root = _tree(tmp_path, {
        **FILLER,
        "scripts/lib/common.sh": "x=1\n",
        "scripts/deploy-thing.sh": 'source "$(dirname "$0")/lib/common.sh"\n',
        "scripts/unrelated.sh": "echo hi\n",
        "tests/unit/scripts/test_deploy_thing.py": 'SCRIPT = REPO / "scripts" / "deploy-thing.sh"\n',
        "tests/unit/scripts/test_unrelated.py": 'SCRIPT = REPO / "scripts" / "unrelated.sh"\n',
    })
    out = sel.select(root, ["scripts/lib/common.sh"])
    assert "tests/unit/scripts/test_deploy_thing.py" in out["tests"], out
    assert "tests/unit/scripts/test_unrelated.py" not in out["tests"], out


def test_a_generic_basename_needs_one_of_its_directories_beside_it(tmp_path):
    root = _tree(tmp_path, {
        **FILLER,
        "images/thing/Dockerfile": "FROM x\n",
        "tests/unit/i/test_thing.py": 'DF = REPO / "images" / "thing" / "Dockerfile"\n',
        "tests/unit/i/test_word.py": '"""Says Dockerfile in prose, about another image."""\n',
    })
    out = sel.select(root, ["images/thing/Dockerfile"])
    assert "tests/unit/i/test_thing.py" in out["tests"], out
    assert "tests/unit/i/test_word.py" not in out["tests"], out


def test_a_changed_test_file_runs_itself(tmp_path):
    root = _tree(tmp_path, {**FILLER, "tests/unit/a/test_self.py": "def test_x(): pass\n"})
    out = sel.select(root, ["tests/unit/a/test_self.py"])
    assert out["tests"] == ["tests/unit/a/test_self.py"], out


def test_changed_guards_selection_is_included_and_an_unknown_one_runs_everything(tmp_path):
    root = _tree(tmp_path, {**FILLER, "tests/unit/g/test_guard.py": "def test_g(): pass\n",
                            "apps/svc/x.py": ""})
    out = sel.select(root, ["apps/svc/x.py"], guards=["tests/unit/g/test_guard.py"])
    assert out["python"] == "selected" and "tests/unit/g/test_guard.py" in out["tests"], out
    out = sel.select(root, ["apps/svc/x.py"], guards=["tests/unit/g/test_gone.py"])
    assert out["python"] == "all", out


def test_a_selection_of_half_the_suite_runs_all_of_it(tmp_path):
    root = _tree(tmp_path, {
        "apps/svc/core.py": "",
        **{f"tests/unit/s/test_{i}.py": "import core\n" for i in range(4)},
        "tests/unit/s/test_lone.py": "def test_x(): pass\n",
    })
    out = sel.select(root, ["apps/svc/core.py"])
    assert out["python"] == "all" and "costs little more" in out["why"], out


def test_docs_and_ui_changes_name_their_areas_for_the_pick_step(tmp_path):
    root = _tree(tmp_path, {**FILLER, "apps/svc/x.py": ""})
    out = sel.select(root, ["apps/svc/x.py", "docs/ci.md", "apps/swarm-ui/src/App.tsx"])
    assert out["python"] == "selected" and out["areas"] == "docs swarm-ui", out
    assert sel.select(root, ["apps/svc/x.py"])["areas"] == ""


def test_on_this_repository_a_scheduler_module_selects_the_tests_that_import_it():
    """The real tree: every test importing scheduler.metrics is selected, and
    this file -- which neither imports it nor names it -- is not. The path is
    built from parts so that this file does not name it."""
    parts = ["apps", "scheduler", "scheduler", "metrics" + ".py"]
    if not REPO.joinpath(*parts).exists():
        pytest.skip("the scheduler's metrics module moved; pick another module")
    out = sel.select(REPO, ["/".join(parts)])
    importer = re.compile(r"^from scheduler\.metrics import", re.M)
    importers = {str(p.relative_to(REPO)) for p in (REPO / "tests" / "unit").rglob("test_*.py")
                 if importer.search(p.read_text(errors="replace"))}
    assert importers, "no test imports scheduler.metrics; pick another module"
    if out["python"] == "all":
        assert "costs little more" in out["why"], out
        return
    assert importers <= set(out["tests"]), importers - set(out["tests"])
    assert "tests/unit/scripts/test_select_unit_tests.py" not in out["tests"], out


# --------------------------------------------------------------------------
# The workflow.
# --------------------------------------------------------------------------
def _select_step() -> dict:
    steps = yaml.safe_load(APPLICATION.read_text())["jobs"]["changes"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "select"]
    return step


@pytest.mark.parametrize("event, python, areas", [
    ("push", "all", ""), ("merge_group", "all", ""), ("workflow_dispatch", "all", ""),
    ("pull_request", "subset", "docs"), ("pull_request", "none", ""),
])
def test_everything_but_a_pull_requests_all_passes_through(tmp_path, event, python, areas):
    """Main, a train, a queue entry and a dispatch keep the whole suite.
    MUTATION: narrow on a push too."""
    body = tmp_path / "step.sh"
    body.write_text(_select_step()["run"])
    out = tmp_path / "out"
    out.write_text("")
    env = {**os.environ, "EVENT": event, "PYTHON": python, "AREAS": areas, "BASE_SHA": "", "HEAD_SHA": "",
           "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "RUNNER_TEMP": str(tmp_path)}
    proc = subprocess.run(["bash", "-e", str(body)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert got == {"python": python, "areas": areas, "tests": ""}, got


def test_the_step_uses_changed_guards_and_the_selector():
    run = _select_step()["run"]
    assert "./scripts/changed-guards.sh --files" in run and "--list" in run, run
    assert "scripts/lib/select_unit_tests.py" in run, run


def test_the_pick_step_runs_the_listed_files_in_selected_mode():
    jobs = yaml.safe_load(APPLICATION.read_text())["jobs"]
    (pick,) = [s for s in jobs["python-unit"]["steps"] if s.get("id") == "pick"]
    assert pick["env"]["PYTHON_TESTS"] == "${{ needs.changes.outputs.tests }}", pick["env"]
    assert 'mode == "selected"' in pick["run"] and "PYTHON_TESTS" in pick["run"], pick["run"]
