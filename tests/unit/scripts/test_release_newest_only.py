"""Only the newest commit on main is built and released.

BGD's decision, 2026-10-26 (08:1xZ), #2438: "Release only the newest". From
2026-10-25 23:32Z to past 08:00Z nothing deployed. application.yml ran ONE
CONCURRENCY GROUP PER COMMIT on main, never cancelled (the 2026-09-24 rule,
docs/ci.md), so every merge queued a full run of about 25 jobs on the
GitHub-hosted runners main's push jobs use, and 27 to 78 of them were queued at
once. release.yml waited 2700 s for its own commit's `build images` job, which
had not reached a runner, and every release timed out: 34 merges were waiting
to deploy.

So now:

  * application.yml puts every run on main in ONE group with
    cancel-in-progress, so a newer push cancels an older run that has not
    finished. Main is linear: the cancelled commit's change is in the newer
    commit's build, and it was tested on its pull request.
  * release.yml runs on EVERY push to main. The path filter it had is what
    stranded a release on 2026-09-24: a docs-only push cancelled the previous
    commit's build and started no release of its own.
  * a release whose commit main has moved past ends GREEN, superseded, and
    promotes nothing -- asked before it waits for its images, and again after,
    when the newer push has cancelled its commit's build. Any other failure
    stays red.
  * a pending release is replaced by the next (GitHub keeps one pending run
    per group); a running one is never cancelled, because past `approval` it
    may be promoting, applying or deploying.

The two decision steps are run here, as written in release.yml, against a
fake `gh`. WHAT THIS CANNOT PROVE: that GitHub's compare API says `ahead` for
a commit main has moved past, or lists a run cancelled by concurrency as
`cancelled` with its build job `cancelled` or absent -- that is GitHub's
documented behaviour, read once against the live API by the first burst of
merges after this lands.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_release_reuses_ci_images import _evaluate, _load_workflow, _render

REPO_NAME = "owner/repo"
SHA = "a" * 40
NEWER = "b" * 40
RUN_ID = 4242

COMPARE = f"repos/{REPO_NAME}/compare/{SHA}...main"
APP_RUNS = f"repos/{REPO_NAME}/actions/workflows/application.yml/runs?head_sha={SHA}&branch=main&event=push&per_page=1"
APP_JOBS = f"repos/{REPO_NAME}/actions/runs/{RUN_ID}/jobs?per_page=100"


def _build_steps() -> dict[str, dict]:
    steps = _load_workflow("release.yml")["jobs"]["build"]["steps"]
    return {s["id"]: s for s in steps if "id" in s}


# ---------------------------------------------------------------------------
# application.yml: one group for main, and the newest run wins


def test_every_run_on_main_shares_one_group_and_a_newer_push_cancels_the_older():
    """MUTATION: put main back on a per-commit group, or turn
    cancel-in-progress off on main -- the 2026-10-26 stall."""
    concurrency = _load_workflow("application.yml")["concurrency"]

    def group(ref: str, sha: str) -> str:
        return _render(concurrency["group"], **{"github.ref": ref, "github.sha": sha})

    main_a = group("refs/heads/main", SHA)
    assert main_a == group("refs/heads/main", NEWER), (
        f"main's commits land in different groups ({main_a!r}): no push to main supersedes "
        "an older run, and a burst of merges queues one full run per commit"
    )
    assert _evaluate(concurrency["cancel-in-progress"], **{"github.ref": "refs/heads/main"}) is True, (
        "a newer push to main does not cancel the run in progress, so the queue still grows by one run per merge"
    )
    # Pull requests keep their own group, and a newer push still supersedes.
    pr = group("refs/pull/7/merge", SHA)
    assert pr == group("refs/pull/7/merge", NEWER)
    assert pr != group("refs/pull/8/merge", SHA), "two pull requests share one group"
    assert pr != main_a, "a pull request's push can cancel main's run"
    assert _evaluate(concurrency["cancel-in-progress"], **{"github.ref": "refs/pull/7/merge"}) is True


# ---------------------------------------------------------------------------
# release.yml: on every push, one pending at a time, never cancelled running


def test_the_release_starts_on_every_push_to_main():
    """A path filter lets a push cancel the previous commit's build and start
    no release to ship it (2026-09-24). MUTATION: put `paths:` back."""
    push = _load_workflow("release.yml")["on"]["push"]
    assert push["branches"] == ["main"]
    assert "paths" not in push and "paths-ignore" not in push, push


def test_a_pending_release_is_superseded_and_a_running_one_is_never_cancelled():
    """GitHub keeps one pending run per group and cancels the older pending
    one when a newer one queues, so a group shared by every push to an
    environment is what lets the newest pending release replace the others.
    MUTATION: key the group by commit or run (nothing pending is ever
    superseded), or set cancel-in-progress (a running deploy is cancelled)."""
    concurrency = _load_workflow("release.yml")["concurrency"]
    text = str(concurrency["group"])
    for per_commit in ("github.sha", "github.run_id", "github.run_number", "github.event.after", "github.ref"):
        assert per_commit not in text, f"release.yml's group reads {per_commit}: a newer release never replaces a pending one"
    # Every push renders the one dev group; a prod dispatch has its own.
    assert _render(text, **{"github.event.inputs.environment": ""}) == "release-dev"
    assert _render(text, **{"github.event.inputs.environment": "prod"}) == "release-prod"
    assert concurrency["cancel-in-progress"] is False, (
        "a newer push cancels the release in progress, which may be past `approval` and promoting, applying or deploying"
    )


def _push_context(superseded: str) -> dict[str, str]:
    return {
        "github.event_name": "push",
        "github.event.inputs.skip_build": "",
        "needs.verify.result": "skipped",
        "needs.build.result": "success",
        "needs.build.outputs.superseded": superseded,
    }


@pytest.mark.parametrize("job_id", ["approval", "plugin-tag"])
def test_a_superseded_release_approves_promotes_and_tags_nothing(job_id):
    """`approval` gates promote, the applies and the deploy (test_release_prod_gate.py).
    MUTATION: drop the superseded clause from either condition."""
    condition = _load_workflow("release.yml")["jobs"][job_id]["if"]
    assert _evaluate(condition, **_push_context("false")) is True, f"{job_id} no longer runs for the newest commit"
    assert _evaluate(condition, **_push_context("true")) is False, f"{job_id} runs for a release a newer commit superseded"


def test_the_decision_steps_run_on_a_push_and_a_dispatch_is_untouched():
    """A dispatch ships main's head by construction and fails as it always
    did. MUTATION: let `continue-on-error` hold on a dispatch, or run the
    decision steps there."""
    job = _load_workflow("release.yml")["jobs"]["build"]
    steps = _build_steps()
    ids = [s.get("id") for s in job["steps"]]
    assert ids.index("newest") < ids.index("stage") < ids.index("after"), ids
    for event, expected in (("push", True), ("workflow_dispatch", False)):
        ctx = {"github.event_name": event}
        assert _evaluate(steps["newest"]["if"], **ctx) is expected
        assert _evaluate(steps["stage"]["continue-on-error"], **ctx) is expected
        assert _evaluate(steps["after"]["if"], **{**ctx, "steps.newest.outputs.superseded": "false"}) is expected
    # Superseded before the wait: nothing authenticates, nothing waits.
    for step_id in ("auth", "stage"):
        assert _evaluate(steps[step_id]["if"], **{"steps.newest.outputs.superseded": "true"}) is False, step_id
        assert _evaluate(steps[step_id]["if"], **{"steps.newest.outputs.superseded": "false"}) is True, step_id
    assert _evaluate(steps["after"]["if"], **{"github.event_name": "push", "steps.newest.outputs.superseded": "true"}) is False
    output = job["outputs"]["superseded"]
    for newest, after, expected in (("true", "", "True"), ("false", "true", "True"), ("false", "false", "False"), ("", "", "False")):
        got = _render(output, **{"steps.newest.outputs.superseded": newest, "steps.after.outputs.superseded": after})
        assert got == expected, (newest, after, got)


# ---------------------------------------------------------------------------
# The two steps, run against a fake gh

FAKE_GH = """\
import json, os, subprocess, sys
args = sys.argv[1:]
if not args or args[0] != "api":
    sys.exit(2)
path = args[1]
jq = args[args.index("--jq") + 1] if "--jq" in args else "."
with open(os.environ["FAKE_GH_RESPONSES"]) as f:
    responses = json.load(f)
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(path + "\\n")
if path not in responses:
    sys.stderr.write("HTTP 502: unreadable\\n")
    sys.exit(1)
sys.exit(subprocess.run(["jq", "-r", jq], input=json.dumps(responses[path]), text=True).returncode)
"""


def _run_step(tmp_path: Path, step_id: str, responses: dict, *, stage: str = "") -> tuple[subprocess.CompletedProcess, dict, str, list]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    output, summary, log = tmp_path / "output", tmp_path / "summary", tmp_path / "gh.log"
    for f in (output, summary, log):
        f.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": REPO_NAME,
        "GITHUB_SHA": SHA,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": "unused-by-the-fake",
        "STAGE": stage,
        "FAKE_GH_RESPONSES": str(tmp_path / "responses.json"),
        "FAKE_GH_LOG": str(log),
    }
    proc = subprocess.run(
        ["bash", "-c", _build_steps()[step_id]["run"]], env=env, capture_output=True, text=True, timeout=60
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return proc, outputs, summary.read_text(), log.read_text().splitlines()


def _cancelled_run(build_conclusion: str | None) -> dict:
    jobs = [{"name": "unit tests (1/4)", "conclusion": "cancelled"}]
    if build_conclusion is not None:
        jobs.append({"name": "build images", "conclusion": build_conclusion})
    return {
        APP_RUNS: {"workflow_runs": [{"id": RUN_ID, "conclusion": "cancelled"}]},
        APP_JOBS: {"jobs": jobs},
    }


@pytest.mark.parametrize(
    "status, superseded",
    [("ahead", "true"), ("identical", "false"), ("behind", "false"), ("diverged", "false")],
)
def test_newest_supersedes_only_a_commit_main_has_moved_past(tmp_path, status, superseded):
    """MUTATION: supersede on anything but `ahead` -- `identical` is main's
    head itself, which must be released."""
    proc, outputs, summary, _ = _run_step(tmp_path, "newest", {COMPARE: {"status": status}})
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"superseded": superseded}
    if superseded == "true":
        assert "Superseded" in summary and SHA in summary
        assert "::notice title=Superseded::" in proc.stdout
    else:
        assert summary == ""


def test_newest_reads_an_unreadable_api_as_release_this_commit(tmp_path):
    """Never "superseded" on a guess: a release that ends green having
    shipped nothing, for an API blip, would leave the commit undeployed.
    MUTATION: treat a failed compare as superseded."""
    proc, outputs, _, _ = _run_step(tmp_path, "newest", {})
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"superseded": "false"}
    assert "::warning::" in proc.stdout


@pytest.mark.parametrize("status, superseded", [("ahead", "false"), ("identical", "false")])
def test_after_a_successful_wait_the_release_deploys_even_when_main_moved(tmp_path, status, superseded):
    """Built and scanned: it deploys, even when main moved on meanwhile (#3000:
    a busy main superseded every release and none deployed for five hours).
    MUTATION: supersede a successful stage when the compare says `ahead`."""
    proc, outputs, _, _ = _run_step(tmp_path, "after", {COMPARE: {"status": status}}, stage="success")
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"superseded": superseded}


@pytest.mark.parametrize("build_conclusion", ["cancelled", "skipped", None])
def test_a_build_the_newer_push_cancelled_ends_the_release_green(tmp_path, build_conclusion):
    """The newer push cancelled this commit's application run before its
    build finished: `--reuse-ci only` said "never built", and that is
    superseded, not red. None: cancelled while pending, so no jobs at all.
    MUTATION: fail on it -- the 2026-10-26 red releases again."""
    responses = {COMPARE: {"status": "ahead"}, **_cancelled_run(build_conclusion)}
    proc, outputs, summary, calls = _run_step(tmp_path, "after", responses, stage="failure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs == {"superseded": "true"}
    assert f"application run {RUN_ID}" in summary
    assert APP_RUNS in calls, "it never asked for application's push run of this commit on main"


@pytest.mark.parametrize(
    "case, status, responses",
    [
        # main is still at this commit: the failure is this commit's.
        ("main-not-moved", "identical", _cancelled_run("cancelled")),
        # main moved, but the build itself FAILED: B's release will hit it too,
        # and it must be seen here.
        ("build-failed", "ahead", _cancelled_run("failure")),
        # main moved and the build succeeded: the failure is after it (a scan
        # refusal), not a cancellation.
        ("build-succeeded", "ahead", _cancelled_run("success")),
        # main moved, but application's run was not cancelled: it failed.
        ("run-failed", "ahead", {
            APP_RUNS: {"workflow_runs": [{"id": RUN_ID, "conclusion": "failure"}]},
            APP_JOBS: {"jobs": [{"name": "build images", "conclusion": "skipped"}]},
        }),
        # main moved, and no push run of this commit exists to say why.
        ("no-run", "ahead", {APP_RUNS: {"workflow_runs": []}}),
        # main moved, and the run could not be read.
        ("unreadable", "ahead", {}),
    ],
)
def test_any_other_failure_stays_red(tmp_path, case, status, responses):
    """MUTATION: read every failure on a moved main as superseded, or drop the
    check of the build job's own conclusion."""
    proc, outputs, summary, _ = _run_step(tmp_path, "after", {COMPARE: {"status": status}, **responses}, stage="failure")
    assert proc.returncode == 1, f"{case}: {proc.stdout}{proc.stderr}"
    assert outputs.get("superseded") != "true", case
    assert "::error title=images and scan failed::" in proc.stdout, case
    assert summary == "", case


def test_a_failure_with_an_unreadable_compare_stays_red(tmp_path):
    proc, outputs, _, _ = _run_step(tmp_path, "after", _cancelled_run("cancelled"), stage="failure")
    assert proc.returncode == 1, proc.stdout
    assert outputs.get("superseded") != "true"
