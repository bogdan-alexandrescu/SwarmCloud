"""Main's push run reuses only what a run of the IDENTICAL tree passed (B1,
owner decision 2026-10-14; docs/ci.md "Main reuses the tested tree").

scripts/lib/ci_reuse.py's `decide` is pure: every fact -- the record, the
run, its jobs, the merged pull request's head, the tree git computes -- is
passed in, so each rule that can say "do not reuse" is tested here with a
control that says "reuse" when only that rule's fact is fixed.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "ci_reuse.py"
APPLICATION = REPO / ".github" / "workflows" / "application.yml"

_spec = importlib.util.spec_from_file_location("ci_reuse", SCRIPT)
reuse = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(reuse)

TREE = "a" * 40
HEAD = "b" * 40
REPO_NAME = "owner/name"


def _jobs_all_green(shards: int = 4, ui_shards: int = 4) -> list[dict]:
    jobs = [{"name": name, "conclusion": "success"} for key, name in reuse.REUSABLE.items() if key not in reuse.MATRIX]
    jobs += [{"name": f"unit tests ({k}/{shards})", "conclusion": "success"} for k in range(1, shards + 1)]
    jobs += [{"name": f"swarm-ui component tests ({k}/{ui_shards})", "conclusion": "success"}
             for k in range(1, ui_shards + 1)]
    return jobs


def _case(**over):
    case = {
        "tree": TREE,
        "repo": REPO_NAME,
        "rec": {"tree": TREE, "sha": "c" * 40, "event": "pull_request", "python": "all",
                "passed": sorted(reuse.REUSABLE)},
        "run": {"id": 7, "path": reuse.WORKFLOW, "event": "pull_request", "head_sha": HEAD,
                "conclusion": "success", "head_repository": {"full_name": REPO_NAME}},
        "jobs": _jobs_all_green(),
        "merged_head": HEAD,
        "head_tree": None,
    }
    for key, value in over.items():
        case[key] = value
    return case


def _decide(case):
    return reuse.decide(**case)


def test_an_up_to_date_pull_requests_whole_run_is_reused_entirely():
    reused, why = _decide(_case())
    assert set(reused) == set(reuse.REUSABLE), reused
    assert "reused from run 7" in why, why


@pytest.mark.parametrize("change, needle", [
    (lambda c: c["rec"].update(tree="d" * 40), "not"),
    (lambda c: c["run"].update(path=".github/workflows/other.yml"), "other.yml"),
    (lambda c: c["run"].update(head_repository={"full_name": "fork/name"}), "another repository"),
    (lambda c: c["run"].update(conclusion="failure"), "failure"),
    (lambda c: c["run"].update(conclusion=None), "None"),
    (lambda c: c.update(merged_head=None), "no pull request"),
    (lambda c: c.update(merged_head="e" * 40), "not the merged head"),
    (lambda c: c["run"].update(event="workflow_dispatch") or c["rec"].update(event="workflow_dispatch"), "never reused"),
    (lambda c: c["rec"].update(event="push"), "the record says push"),
])
def test_any_doubt_about_the_run_reuses_nothing(change, needle):
    """MUTATION: drop any of decide()'s refusals."""
    case = _case()
    change(case)
    reused, why = _decide(case)
    assert reused == [] and needle in why, (reused, why)


def test_a_train_or_queue_run_is_reused_only_when_its_commit_has_this_tree():
    for event in ("push", "merge_group"):
        case = _case(merged_head=None, head_tree=TREE)
        case["run"]["event"] = case["rec"]["event"] = event
        reused, _ = _decide(case)
        assert set(reused) == set(reuse.REUSABLE), (event, reused)
        case["head_tree"] = "f" * 40
        reused, why = _decide(case)
        assert reused == [] and "tree" in why, (event, why)


def test_a_selected_unit_run_is_never_main_s_full_run():
    """B2's subset is not the whole suite. MUTATION: drop the python=all check."""
    for mode in ("selected", "subset"):
        case = _case()
        case["rec"]["python"] = mode
        reused, _ = _decide(case)
        assert "python-unit" not in reused and "shell" in reused, (mode, reused)


def test_a_job_the_run_skipped_is_run_again():
    case = _case()
    case["rec"]["passed"] = [k for k in reuse.REUSABLE if k not in ("ui-build", "ui-tests")]
    reused, _ = _decide(case)
    assert "ui-build" not in reused and "ui-tests" not in reused and "integration" in reused, reused


@pytest.mark.parametrize("jobs_change", [
    lambda jobs: [j for j in jobs if j["name"] != "unit tests (3/4)"],          # a shard missing
    lambda jobs: [dict(j, conclusion="failure") if j["name"] == "unit tests (2/4)" else j for j in jobs],
    lambda jobs: [dict(j, conclusion="skipped") if j["name"] == "unit tests (1/4)" else j for j in jobs],
    lambda jobs: jobs + [{"name": "unit tests (5/5)", "conclusion": "success"}],  # two totals
])
def test_the_unit_shards_are_reused_only_when_every_shard_passed(jobs_change):
    case = _case()
    case["jobs"] = jobs_change(copy.deepcopy(case["jobs"]))
    reused, _ = _decide(case)
    assert "python-unit" not in reused and "shell" in reused, reused


def test_the_api_must_agree_with_the_record():
    case = _case()
    case["jobs"] = [dict(j, conclusion="failure") if j["name"] == "kubernetes manifests" else j for j in case["jobs"]]
    reused, _ = _decide(case)
    assert "manifests" not in reused and "shell" in reused, reused


def test_record_lists_only_jobs_that_succeeded():
    needs = {k: {"result": "success"} for k in reuse.REUSABLE}
    needs["ui-tests"] = {"result": "skipped"}
    needs["integration"] = {"result": "failure"}
    needs["changes"] = {"result": "success"}
    rec = reuse.record(needs, "all", "pull_request", "c" * 40, TREE)
    assert "ui-tests" not in rec["passed"] and "integration" not in rec["passed"], rec
    assert "changes" not in rec["passed"] and "shell" in rec["passed"], rec
    assert rec["tree"] == TREE and rec["python"] == "all", rec


def test_a_lookup_that_cannot_read_the_api_reuses_nothing_and_fails_nothing(tmp_path):
    """The CLI never fails the `changes` job: an unreadable API is `[]`."""
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
                         capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
    (bindir / "gh").chmod(0o755)
    out = tmp_path / "out"
    proc = subprocess.run(
        ["python3", str(SCRIPT), "find", "--repo", REPO_NAME, "--sha", sha, "--github-output", str(out),
         "--summary", str(tmp_path / "summary")],
        cwd=repo, env={**env, "PATH": f"{bindir}{os.pathsep}{env['PATH']}"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().strip() == "reused=[]", out.read_text()
    assert "Nothing reused" in (tmp_path / "summary").read_text()


# --------------------------------------------------------------------------
# The workflow.
# --------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def test_every_reusable_name_is_the_name_its_job_reports():
    """The API is read by job name. MUTATION: rename a job without REUSABLE."""
    jobs = _jobs()
    for key, name in reuse.REUSABLE.items():
        assert key in jobs, key
        reported = str(jobs[key]["name"])
        if key in reuse.MATRIX:
            assert reported.startswith(name) and "${{ matrix.shard }}" in reported, (key, reported)
        else:
            assert reported == name, (key, reported)


def test_every_reusable_job_is_skipped_when_it_is_reused():
    jobs = _jobs()
    for key in reuse.REUSABLE:
        clause = "!contains(fromJSON(needs.changes.outputs.reused || '[]'), '%s')" % key
        assert clause in str(jobs[key].get("if")), (key, jobs[key].get("if"))


def test_main_looks_up_and_everyone_else_records():
    jobs = _jobs()
    (step,) = [s for s in jobs["changes"]["steps"] if s.get("id") == "reuse"]
    assert step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'", step["if"]
    assert "ci_reuse.py find" in step["run"], step["run"]
    assert jobs["changes"]["outputs"]["reused"].replace(" ", "") == "${{steps.reuse.outputs.reused}}"
    perms = jobs["changes"]["permissions"]
    assert perms.get("actions") == "read" and "id-token" not in perms, perms
    record = jobs["tested-tree"]
    assert "github.ref != 'refs/heads/main'" in record["if"] and "always()" in record["if"], record["if"]
    assert set(reuse.REUSABLE) | {"changes"} == set(record["needs"]), record["needs"]
    upload = [s for s in record["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(upload) == 1 and upload[0]["with"]["name"] == "tested-tree-${{ steps.record.outputs.tree }}", upload
    # Before the merge, so main's lookup never races the upload.
    assert "tested-tree" in jobs["ci-gate"]["needs"]


def test_the_build_still_waits_for_what_was_not_reused():
    """A reused job is skipped by its own if, which reads as passed; the build
    still needs the checks it always needed."""
    assert {"python", "shell", "manifests"} <= set(_jobs()["build"]["needs"])


def test_the_record_json_round_trips_through_find_s_reader(tmp_path):
    """`record` writes what `find` reads back: `tested-tree-<tree>.json`."""
    needs = {k: {"result": "success"} for k in reuse.REUSABLE}
    rec = reuse.record(needs, "all", "push", HEAD, TREE)
    path = tmp_path / f"tested-tree-{TREE}.json"
    path.write_text(json.dumps(rec))
    case = _case(merged_head=None, head_tree=TREE, rec=json.loads(path.read_text()))
    case["run"]["event"] = "push"
    reused, _ = _decide(case)
    assert set(reused) == set(reuse.REUSABLE)
