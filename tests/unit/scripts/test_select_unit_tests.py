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
import secrets
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


# --------------------------------------------------------------------------
# A doc between a changed path and its test (#2299). PR #2267 moved
# `function PushTestCell(` out of apps/swarm-ui/src/Access.tsx while
# docs/onboarding.md still cited it there; test_onboarding_migration and
# test_onboarding_docs_and_mockup resolve that citation but name only
# onboarding.md, so the pull_request run was green and main's push shards 4
# and 5 went red. A doc citing a changed path by its full path counts as
# changed, one level deep.
# --------------------------------------------------------------------------
DOC_CITES = {
    "docs/x.md": "The cell (`apps/swarm-ui/src/A.tsx` (`function F(`)).\n",
    "apps/swarm-ui/src/A.tsx": "export function F() {}\n",
    "tests/unit/scripts/test_x_docs.py": 'DOC = REPO / "docs" / "x.md"\n',
    # The control: another doc, citing another file, read by another test.
    "docs/y.md": "The other cell (`apps/swarm-ui/src/B.tsx` (`function G(`)).\n",
    "apps/swarm-ui/src/B.tsx": "export function G() {}\n",
    "tests/unit/scripts/test_y_docs.py": 'DOC = REPO / "docs" / "y.md"\n',
}


def test_a_doc_that_cites_a_changed_path_selects_the_tests_that_read_the_doc(tmp_path):
    root = _tree(tmp_path, {**FILLER, **DOC_CITES})
    out = sel.select(root, ["apps/swarm-ui/src/A.tsx"])
    assert out["python"] == "selected", out
    # MUTATION: drop doc_closure from select() and this is red (#2299).
    assert "tests/unit/scripts/test_x_docs.py" in out["tests"], out
    assert "tests/unit/scripts/test_y_docs.py" not in out["tests"], out
    assert sel.doc_closure(root, ["apps/swarm-ui/src/A.tsx"]) == ["apps/swarm-ui/src/A.tsx", "docs/x.md"]


def test_a_doc_naming_only_the_basename_does_not_select_its_readers(tmp_path):
    # App.tsx, main.py, common.sh: a basename is shared by too many files for a
    # doc that names one to be citing THIS one. Full path only.
    root = _tree(tmp_path, {
        **FILLER, **DOC_CITES,
        "docs/z.md": "Some other `A.tsx` (`function F(`), in another package.\n",
        "tests/unit/scripts/test_z_docs.py": 'DOC = REPO / "docs" / "z.md"\n',
    })
    out = sel.select(root, ["apps/swarm-ui/src/A.tsx"])
    # MUTATION: match a doc on the changed path's basename and this is red.
    assert "tests/unit/scripts/test_z_docs.py" not in out["tests"], out
    assert "tests/unit/scripts/test_x_docs.py" in out["tests"], out


def test_the_doc_closure_is_one_level_deep(tmp_path):
    root = _tree(tmp_path, {
        **FILLER, **DOC_CITES,
        # w.md cites x.md, which cites A.tsx: two hops from the change.
        "docs/w.md": "See `docs/x.md`.\n",
        "tests/unit/scripts/test_w_docs.py": 'DOC = REPO / "docs" / "w.md"\n',
    })
    # A changed doc is not expanded: a doc citing it is not changed with it.
    out = sel.select(root, ["docs/x.md"])
    assert "tests/unit/scripts/test_x_docs.py" in out["tests"], out
    # MUTATION: let the closure take docs/ paths as sources and this is red.
    assert "tests/unit/scripts/test_w_docs.py" not in out["tests"], out
    assert sel.doc_closure(root, ["docs/x.md"]) == ["docs/x.md"]
    # Nor is a doc the closure added: A.tsx reaches x.md and stops there.
    out = sel.select(root, ["apps/swarm-ui/src/A.tsx"])
    assert "tests/unit/scripts/test_x_docs.py" in out["tests"], out
    assert "tests/unit/scripts/test_w_docs.py" not in out["tests"], out


# --------------------------------------------------------------------------
# The repository-wide guard set, always (CI-GUARDS-ALWAYS, part of #1822).
# Main went red for 19 commits on test_collection_stays_cheap and for 6 on
# test_awaiting_input_readers: whole-tree tests no changed path names, so no
# pull request ran them. The set is changed-guards.sh's GUARD_TESTS, read from
# there: a second copy of the list is the drift this lane removes.
# --------------------------------------------------------------------------
WHOLE_TREE = (
    "tests/unit/scripts/test_collection_stays_cheap.py",
    "tests/unit/common/test_awaiting_input_readers.py",
)


def _guards_script(names: list[str], array: str = "GUARD_TESTS") -> str:
    lines = "".join(f"  {n}\n" for n in names)
    return f"#!/usr/bin/env bash\nset -euo pipefail\n{array}=(\n{lines})\n"


def test_a_diff_that_only_adds_an_unrelated_file_still_runs_the_whole_tree_guards():
    unrelated = f"notes/unrelated-{secrets.token_hex(4)}.txt"
    out = sel.select(REPO, [unrelated])
    assert out["python"] == "selected", out
    # MUTATION: drop the GUARD_TESTS read from select() (or either file from
    # GUARD_TESTS) and the unrelated file selects nothing -- red here.
    for guard in WHOLE_TREE:
        assert guard in out["tests"], (guard, out)
    # MUTATION: leave out any GUARD_TESTS entry (select from a shorter
    # mirrored list in select_unit_tests.py) and this subset turns red.
    assert set(sel.guard_tests(REPO)) <= set(out["tests"]), out


# The walkers outside apps/, tests/ and docs/ (CI-GUARDS-MORE, part of #1822):
# each reads every file of a scripts/, terraform/ or kubernetes/ directory, so
# a new file there breaks it without any test naming the file. All weigh
# under 5 CI-seconds; the heavier ones stay with main's push run
# (test_shellcheck_ci has no weight recorded yet but takes minutes: it stays
# out, which the median rule below cannot see).
OUTSIDE_WALKERS = (
    "tests/unit/common/test_startup_progress_contract.py",
    "tests/unit/control_plane/test_reconciler_cloud_run_stall_guard.py",
    "tests/unit/scripts/test_cloudbuild_connection_retired.py",
    "tests/unit/worker/test_spec_verify_keys_configmap.py",
    "tests/unit/scripts/test_workspace_apply_approval.py",
    "tests/unit/worker/test_kubernetes_manifests.py",
    "tests/unit/control_plane/test_dispatch_judgment.py",
    "tests/unit/control_plane/test_gke_cache_volume.py",
    "tests/unit/control_plane/test_gke_worker_metadata_env.py",
    "tests/unit/control_plane/test_schedules_migration_seed.py",
    "tests/unit/control_plane/test_tfvars_refusal_switches.py",
    "tests/unit/control_plane/test_workspace_gate_dev.py",
    "tests/unit/scripts/test_create_secrets_workspace_grant_key.py",
    "tests/unit/scripts/test_deployer_role_count_prose.py",
    "tests/unit/scripts/test_github_app_runbook.py",
    "tests/unit/scripts/test_owner_pinned_jobs.py",
    "tests/unit/scripts/test_tenancy_retired_295_identities.py",
    "tests/unit/worker/test_checkpoint_retention.py",
    "tests/unit/worker/test_workspace_provisioner_scope.py",
    "tests/unit/scripts/test_swarm_verify_image_helpers.py",
)


def test_an_unrelated_one_file_diff_runs_the_walkers_outside_apps_tests_and_docs():
    unrelated = f"notes/unrelated-{secrets.token_hex(4)}.txt"
    out = sel.select(REPO, [unrelated])
    assert out["python"] == "selected", out
    for walker in OUTSIDE_WALKERS:
        # MUTATION: drop any of these from GUARD_TESTS in changed-guards.sh and
        # the unrelated file no longer selects it -- red here, naming it.
        assert walker in out["tests"], (walker, out)


def test_the_walkers_outside_apps_tests_and_docs_stay_cheap():
    import json
    import statistics
    weights = json.loads((REPO / "tests" / "unit" / "scripts" / "unit_test_durations.json").read_text())
    # A file with no weight yet counts at the median, the way the shards cut it.
    median = statistics.median(weights.values())
    for walker in OUTSIDE_WALKERS:
        assert (REPO / walker).is_file(), walker
        # MUTATION: list a walker of 5 CI-seconds or more here (every pull
        # request pays for it) and this is red, naming it.
        assert weights.get(walker, median) < 5, (walker, weights.get(walker))


# The walkers of .github/ and images/ (CI-GUARDS-DOCS, part of #1822): each
# reads every workflow file, every issue form, every Dockerfile or every file of
# one image directory, so a new workflow or image breaks it without naming it.
# All weigh under 5 CI-seconds, about 12 s together; the heavier ones
# (test_ci_gate, test_merge_queue_workflows, test_build_images_*, ...) stay
# with main's push run. The last three -- test_auto_merge_base_merge (2.6 s),
# test_issue_forms (0.6 s) and test_verify_deployer_trust (4.0 s), +7.2
# CI-seconds a pull request -- read CLAUDE.md or docs/runbooks/merge-app.md by
# name, so test_changed_guards.py's docs-only case allows a non-doc guard that
# names a changed doc (CI-GUARDS-THREE, part of #1822).
GITHUB_AND_IMAGES_WALKERS = (
    "tests/unit/scripts/test_fork_prs_off_self_hosted.py",
    "tests/unit/scripts/test_workflow_step_reachability.py",
    "tests/unit/scripts/test_image_streaming.py",
    "tests/unit/scripts/test_pr_image_scan.py",
    "tests/unit/scripts/test_ui_node_line.py",
    "tests/unit/worker/test_jev_browser_image.py",
    "tests/unit/worker/test_repo_index_extract.py",
    "tests/unit/scripts/test_auto_merge_base_merge.py",
    "tests/unit/scripts/test_issue_forms.py",
    "tests/unit/scripts/test_verify_deployer_trust.py",
)


def test_an_unrelated_one_file_diff_runs_the_walkers_of_github_and_images():
    unrelated = f"notes/unrelated-{secrets.token_hex(4)}.txt"
    out = sel.select(REPO, [unrelated])
    assert out["python"] == "selected", out
    for walker in GITHUB_AND_IMAGES_WALKERS:
        # MUTATION: drop any of these from GUARD_TESTS in changed-guards.sh and
        # the unrelated file no longer selects it -- red here, naming it.
        assert walker in out["tests"], (walker, out)


def test_the_walkers_of_github_and_images_stay_cheap():
    import json
    import statistics
    weights = json.loads((REPO / "tests" / "unit" / "scripts" / "unit_test_durations.json").read_text())
    median = statistics.median(weights.values())
    for walker in GITHUB_AND_IMAGES_WALKERS:
        assert (REPO / walker).is_file(), walker
        # MUTATION: list a walker of 5 CI-seconds or more here (test_ci_gate,
        # 28.2 s) and this is red, naming it.
        assert weights.get(walker, median) < 5, (walker, weights.get(walker))


def test_changed_guards_and_the_ci_selection_list_one_guard_set():
    unrelated = f"notes/unrelated-{secrets.token_hex(4)}.txt"
    listed = subprocess.run(
        ["bash", str(REPO / "scripts" / "changed-guards.sh"), "--list", "--files", "-"],
        cwd=REPO, input=unrelated + "\n", capture_output=True, text=True, timeout=120,
        env={**os.environ, "NO_COLOR": "1"})
    assert listed.returncode == 0, listed.stderr
    from_script = sorted(line for line in listed.stdout.splitlines() if line.startswith("tests/"))
    # MUTATION: give select_unit_tests.py its own copy of the list, or have
    # guard_tests() read anything but changed-guards.sh's GUARD_TESTS, and the
    # two stop being one set the day either is edited -- red here.
    assert sorted(sel.guard_tests(REPO)) == from_script
    for guard in WHOLE_TREE:
        assert guard in from_script, guard
    # MUTATION: paste a guard path into select_unit_tests.py and this is red:
    # the selector reads the list, it never restates it.
    text = SELECT.read_text()
    for guard in from_script:
        assert guard not in text, f"select_unit_tests.py restates the guard {guard}"


def test_the_guard_set_is_read_from_changed_guards_sh_and_always_selected(tmp_path):
    root = _tree(tmp_path, {**FILLER, "tests/unit/g/test_tree.py": "def test_g(): pass\n",
                            "apps/svc/x.py": "",
                            "scripts/changed-guards.sh": _guards_script(["tests/unit/g/test_tree.py"])})
    # MUTATION: include the guard set only when --guards is passed and this is red.
    out = sel.select(root, ["apps/svc/x.py"])
    assert out["python"] == "selected" and "tests/unit/g/test_tree.py" in out["tests"], out
    # MUTATION: skip a GUARD_TESTS entry that is not a test here and a stale
    # list narrows silently -- red here; it runs everything instead.
    (root / "scripts" / "changed-guards.sh").write_text(_guards_script(["tests/unit/g/test_gone.py"]))
    out = sel.select(root, ["apps/svc/x.py"])
    assert out["python"] == "all" and "test_gone.py" in out["why"], out
    # MUTATION: read an unparseable script as an empty guard set and this is red.
    (root / "scripts" / "changed-guards.sh").write_text(_guards_script(["tests/unit/g/test_tree.py"], "OTHER"))
    out = sel.select(root, ["apps/svc/x.py"])
    assert out["python"] == "all" and "GUARD_TESTS" in out["why"], out


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
    # The selection lives in scripts/lib/unit_shards.py since LANE CI-EARLY
    # (the `changes` job sizes the matrix with the same code).
    assert "scripts/lib/unit_shards.py pick" in pick["run"], pick["run"]
    script = (REPO / "scripts" / "lib" / "unit_shards.py").read_text()
    assert 'mode == "selected"' in script and "PYTHON_TESTS" in script
