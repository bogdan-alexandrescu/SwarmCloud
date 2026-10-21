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
    assert out.read_text().splitlines() == ["reused=[]", "reused_from="], out.read_text()
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


def test_main_and_a_same_repository_pull_request_look_up_and_everyone_records():
    """CI-TREE-REUSE (#1798). MUTATION: drop either half of the step's `if`,
    or let a fork's pull request look a record up."""
    jobs = _jobs()
    (step,) = [s for s in jobs["changes"]["steps"] if s.get("id") == "reuse"]
    assert step["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/main' || "
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository"), step["if"]
    assert "ci_reuse.py find" in step["run"] and '--event "${EVENT}"' in step["run"], step["run"]
    assert step["env"]["EVENT"] == "${{ github.event_name }}", step["env"]
    assert jobs["changes"]["outputs"]["reused"].replace(" ", "") == "${{steps.reuse.outputs.reused}}"
    # MUTATION: drop the output ci-gate cites the reused run from.
    assert jobs["changes"]["outputs"]["reused_from"].replace(" ", "") == "${{steps.reuse.outputs.reused_from}}"
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


# --------------------------------------------------------------------------
# A pull request reuses a green lane push of its merge tree (CI-TREE-REUSE,
# #1798, owner approval 2026-10-21; docs/ci.md "Main reuses the tested tree").
# --------------------------------------------------------------------------
def _pr_case(**over):
    """A pull request whose merge tree is TREE, and a green, completed push
    run of `swarm/lane` in this repository whose commit has the same tree."""
    case = {
        "tree": TREE,
        "repo": REPO_NAME,
        "rec": {"tree": TREE, "sha": HEAD, "event": "push", "python": "all",
                "passed": sorted(reuse.REUSABLE)},
        "run": {"id": 9, "path": reuse.WORKFLOW, "event": "push", "head_sha": HEAD,
                "head_branch": "swarm/lane", "status": "completed", "conclusion": "success",
                "head_repository": {"full_name": REPO_NAME}},
        "jobs": _jobs_all_green(),
        "head_tree": TREE,
    }
    for key, value in over.items():
        case[key] = value
    return case


def test_a_pull_request_reuses_a_green_lane_push_of_its_exact_merge_tree():
    """MUTATION: make decide_pull_request return [] always."""
    reused, why = reuse.decide_pull_request(**_pr_case())
    assert set(reused) == set(reuse.REUSABLE), reused
    assert "reused from run 9" in why and "swarm/lane" in why, why


@pytest.mark.parametrize("change, needle", [
    # A different tree: the record's, or the one git computes for the run's commit.
    (lambda c: c["rec"].update(tree="d" * 40), "record is of tree"),
    (lambda c: c.update(head_tree="d" * 40), "has tree"),
    (lambda c: c.update(head_tree=None), "has tree"),
    # A run that did not pass, or has not finished.
    (lambda c: c["run"].update(conclusion="cancelled"), "cancelled"),
    (lambda c: c["run"].update(conclusion="failure"), "failure"),
    (lambda c: c["run"].update(status="in_progress", conclusion=None), "in_progress"),
    # Not a lane push of this workflow in this repository.
    (lambda c: c["run"].update(head_branch="main"), "not a swarm/"),
    (lambda c: c["run"].update(head_branch="feature/x"), "not a swarm/"),
    (lambda c: c["run"].update(event="pull_request") or c["rec"].update(event="pull_request"), "only a lane push"),
    (lambda c: c["rec"].update(event="pull_request"), "the record says pull_request"),
    (lambda c: c["run"].update(path=".github/workflows/other.yml"), "other.yml"),
    (lambda c: c["run"].update(head_repository={"full_name": "fork/name"}), "another repository"),
])
def test_a_pull_request_reuses_nothing_from_any_other_run(change, needle):
    """MUTATION: drop any one of decide_pull_request()'s refusals."""
    case = _pr_case()
    change(case)
    reused, why = reuse.decide_pull_request(**case)
    assert reused == [] and needle in why, (reused, why)


def test_a_partial_lane_push_yields_only_what_it_proved():
    """The unit shards only from the whole suite; a job the API does not show
    `success` for -- skipped, failed, a shard missing -- runs again.
    MUTATION: drop the python=all rule or the API's per-job check for pull requests."""
    case = _pr_case()
    case["rec"]["python"] = "subset"
    reused, _ = reuse.decide_pull_request(**case)
    assert "python-unit" not in reused and "shell" in reused, reused

    case = _pr_case()
    case["jobs"] = [dict(j, conclusion="skipped") if j["name"] == "kubernetes manifests" else j
                    for j in case["jobs"]]
    case["jobs"] = [j for j in case["jobs"] if j["name"] != "swarm-ui component tests (2/4)"]
    reused, _ = reuse.decide_pull_request(**case)
    assert "manifests" not in reused and "ui-tests" not in reused and "integration" in reused, reused

    case = _pr_case()
    case["rec"]["passed"] = []
    reused, why = reuse.decide_pull_request(**case)
    assert reused == [] and "passed no job" in why, (reused, why)


# --- find(): which records each event reads ---------------------------------
def _zip_of(rec: dict) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"tested-tree-{rec['tree']}.json", json.dumps(rec))
    return buf.getvalue()


def _fake_api(monkeypatch, records: list[tuple[dict, dict, list[dict]]], *, tree: str = TREE,
              trees: dict | None = None, pulls: list | None = None):
    """records: (artifact's workflow_run, the run, its jobs), newest first."""
    arts, zips, runs, jobs = [], {}, {}, {}
    for n, (wr, rec, run_jobs) in enumerate(records):
        art_id = 100 + n
        arts.append({"id": art_id, "expired": False, "created_at": f"2026-10-21T0{9 - n}:00:00Z",
                     "workflow_run": wr})
        zips[f"artifacts/{art_id}/zip"] = _zip_of(rec)
        runs[wr["id"]] = (wr["run"], run_jobs)
    calls: list[str] = []

    def gh(path):
        calls.append(path)
        for suffix, raw in zips.items():
            if path.endswith(suffix):
                return raw
        raise AssertionError(path)

    def gh_json(path):
        calls.append(path)
        if "/actions/artifacts?name=" in path:
            assert f"tested-tree-{tree}" in path, path
            return {"artifacts": arts}
        if path.endswith("/pulls"):
            return pulls or []
        for run_id, (run, run_jobs) in runs.items():
            if path.endswith(f"/actions/runs/{run_id}"):
                return run
            if f"/actions/runs/{run_id}/jobs" in path:
                return {"jobs": run_jobs}
        raise AssertionError(path)

    known = {"merge": tree, HEAD: tree, **(trees or {})}
    monkeypatch.setattr(reuse, "_gh", gh)
    monkeypatch.setattr(reuse, "_gh_json", gh_json)
    monkeypatch.setattr(reuse, "tree_of", lambda sha: known.get(sha))
    return calls


def _lane_push(run_id: int = 9, **run_over) -> tuple[dict, dict, list[dict]]:
    case = _pr_case()
    run = dict(case["run"], id=run_id, html_url=f"https://github.com/{REPO_NAME}/actions/runs/{run_id}",
               **run_over)
    wr = {"id": run_id, "head_branch": run["head_branch"], "head_sha": run["head_sha"], "run": run}
    return wr, case["rec"], case["jobs"]


def _pr_record(run_id: int = 7, head: str = HEAD) -> tuple[dict, dict, list[dict]]:
    case = _case()
    run = dict(case["run"], id=run_id, head_sha=head, head_branch="swarm/lane", status="completed",
               html_url=f"https://github.com/{REPO_NAME}/actions/runs/{run_id}")
    wr = {"id": run_id, "head_branch": "swarm/lane", "head_sha": head, "run": run}
    return wr, case["rec"], case["jobs"]


def test_find_for_a_pull_request_reuses_the_lane_push_and_names_it(monkeypatch):
    """MUTATION: let find(event=pull_request) read the record through main's decide()."""
    _fake_api(monkeypatch, [_lane_push()])
    reused, why, source = reuse.find(REPO_NAME, "merge", event="pull_request")
    assert set(reused) == set(reuse.REUSABLE), why
    assert source == f"https://github.com/{REPO_NAME}/actions/runs/9", source


def test_find_for_a_pull_request_never_reuses_another_pull_requests_run(monkeypatch):
    """Only a lane PUSH run is reused by a pull request -- not an earlier pull
    request run of the same tree, and not a cancelled push. MUTATION: drop the
    lane-push filter."""
    _fake_api(monkeypatch, [_pr_record(), _lane_push(conclusion="cancelled")])
    reused, why, source = reuse.find(REPO_NAME, "merge", event="pull_request")
    assert reused == [] and source == "", (reused, why)


def test_find_for_a_pull_request_with_a_different_tree_reuses_nothing(monkeypatch):
    """The lane push's commit has another tree (main moved under the pull
    request). MUTATION: compare the run's head sha instead of its tree."""
    _fake_api(monkeypatch, [_lane_push()], trees={HEAD: "e" * 40})
    reused, why, source = reuse.find(REPO_NAME, "merge", event="pull_request")
    assert reused == [] and "has tree" in why and source == "", (reused, why)


def test_main_s_reuse_is_unchanged(monkeypatch):
    """Main's push run still reuses the merged pull request's run, through
    decide(), and still asks which pull request it merged.
    MUTATION: route main's lookup through decide_pull_request."""
    calls = _fake_api(monkeypatch, [_pr_record()],
                      pulls=[{"merge_commit_sha": "merge", "merged_at": "2026-10-21T09:00:00Z",
                              "head": {"sha": HEAD}}])
    reused, why, source = reuse.find(REPO_NAME, "merge")
    assert set(reused) == set(reuse.REUSABLE) and "reused from run 7" in why, why
    assert source.endswith("/actions/runs/7"), source
    assert any(c.endswith("/commits/merge/pulls") for c in calls), calls
    # And a merged pull request's head that is not the run's: nothing, as before.
    _fake_api(monkeypatch, [_pr_record(head="e" * 40)],
              pulls=[{"merge_commit_sha": "merge", "merged_at": "x", "head": {"sha": HEAD}}])
    reused, why, _ = reuse.find(REPO_NAME, "merge")
    assert reused == [] and "not the merged head" in why, why


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_a_pull_request_lookup_that_cannot_read_the_api_reuses_nothing(tmp_path, event):
    """MUTATION: let an API error escape find --event pull_request."""
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
        ["python3", str(SCRIPT), "find", "--repo", REPO_NAME, "--sha", sha, "--event", event,
         "--github-output", str(out)],
        cwd=repo, env={**env, "PATH": f"{bindir}{os.pathsep}{env['PATH']}"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().splitlines() == ["reused=[]", "reused_from="], out.read_text()


# --- ci-gate's own verdict ----------------------------------------------------
def _needs(reused: list[str], source: str = "", **outputs) -> dict:
    outs = {"app": "true", "python": "all", "ui": "true", "reused": json.dumps(reused),
            "reused_from": source, **outputs}
    needs = {"changes": {"result": "success", "outputs": outs}}
    for key in reuse.REUSABLE:
        needs[key] = {"result": "skipped" if key in reused else "success", "outputs": {}}
    return needs


RUN_URL = f"https://github.com/{REPO_NAME}/actions/runs/9"


def test_the_gate_passes_a_listed_reuse_and_names_the_run():
    """MUTATION: drop the summary line that cites the reused run."""
    problems, summary = reuse.gate(_needs(sorted(reuse.REUSABLE), RUN_URL), "pull_request")
    assert problems == [], problems
    assert RUN_URL in summary and "`python-unit`" in summary, summary
    problems, summary = reuse.gate(_needs([]), "pull_request")
    assert problems == [] and "nothing" in summary.lower(), summary


def test_the_gate_fails_a_skip_the_reuse_step_did_not_list():
    """A job this change reaches, skipped, and not listed by `reuse`: its
    green has no source. MUTATION: count every skip as a pass again."""
    needs = _needs(["shell"], RUN_URL)
    needs["python-unit"]["result"] = "skipped"
    problems, _ = reuse.gate(needs, "pull_request")
    assert len(problems) == 1 and "python-unit" in problems[0], problems


def test_the_gate_passes_a_skip_its_own_if_explains():
    """Not reached by the change (app, python, ui): skipped is a pass, as
    before. MUTATION: drop REACH and fail every unlisted skip."""
    needs = _needs([], app="false", python="none", ui="false")
    for key in reuse.REUSABLE:
        needs[key]["result"] = "skipped"
    problems, _ = reuse.gate(needs, "pull_request")
    assert problems == [], problems
    needs = _needs([], python="subset", ui="false")
    for key in ("python-checks", "ui-build", "ui-tests"):
        needs[key]["result"] = "skipped"
    problems, _ = reuse.gate(needs, "pull_request")
    assert problems == [], problems


@pytest.mark.parametrize("needs, event, needle", [
    (_needs(["shell"]), "pull_request", "names no run"),
    (_needs(["not-a-job"], RUN_URL), "pull_request", "not-a-job"),
    (_needs(["shell"], RUN_URL), "merge_group", "merge_group"),
    ({"changes": {"result": "success", "outputs": {"reused": "{oops", "app": "true"}}}, "push", "not a JSON list"),
])
def test_the_gate_refuses_a_reuse_it_cannot_vouch_for(needs, event, needle):
    """MUTATION: drop any of gate()'s refusals."""
    problems, _ = reuse.gate(needs, event)
    assert any(needle in p for p in problems), problems


def test_every_reach_condition_is_the_job_s_own_if():
    """gate() judges an unlisted skip by REACH, so REACH must be what each
    job's `if:` says before its reuse clause. MUTATION: change a job's `if:`
    without REACH."""
    jobs = _jobs()
    assert set(reuse.REACH) == set(reuse.REUSABLE)
    for key in reuse.REUSABLE:
        clause = "!contains(fromJSON(needs.changes.outputs.reused || '[]'), '%s')" % key
        assert jobs[key]["if"] == f"{reuse.reach_if(key)} && {clause}", (key, jobs[key]["if"])


def test_ci_gate_runs_the_reuse_verdict_after_its_own():
    """MUTATION: drop the step, or run it before the first verdict."""
    steps = _jobs()["ci-gate"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    (idx,) = [i for i, r in enumerate(runs) if "ci_reuse.py gate" in r]
    assert idx > 0 and "every job passed or was skipped" in runs[0], runs
    step = steps[idx]
    assert '--needs "${NEEDS}"' in step["run"] and '--event "${EVENT}"' in step["run"], step["run"]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}", step["env"]
    # The checkout it runs from must precede it on every event it runs on.
    checkout = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkout and checkout[0] < idx and steps[checkout[0]].get("if") == step.get("if"), steps


# --- the merge step reads a reused green as green ----------------------------
def test_the_merge_step_reads_a_reused_green_as_green():
    """At a lane's head: the push run's ci-gate passed, and the pull request's
    ci-gate passed with the heavy jobs skipped by reuse. Every run of a
    required check is green there (merge.py REQUIRED_GREEN = success,
    skipped). MUTATION (merge.py): drop `skipped` from REQUIRED_GREEN, or let
    one red run of a check be outvoted."""
    # Imported here: test_collection_stays_cheap holds every module under
    # tests/unit/scripts to collecting without the service stack.
    from agent_worker import merge

    push_gate = {"name": "ci-gate", "status": "completed", "conclusion": "success"}
    pr_gate = {"name": "ci-gate", "status": "completed", "conclusion": "success"}
    assert merge.required_check_state([push_gate, pr_gate]) == "green"
    for name in ("unit tests (1/4)", "integration tests (emulator)"):
        push_job = {"name": name, "status": "completed", "conclusion": "success"}
        pr_job = {"name": name, "status": "completed", "conclusion": "skipped"}
        assert merge.required_check_state([push_job, pr_job]) == "green", name
    # Control: a pull request's ci-gate that refused the reuse holds the merge.
    assert merge.required_check_state([push_gate, dict(pr_gate, conclusion="failure")]) == "failed"
