"""A hotfix whose build a newer push cancelled ends green, superseded.

BGD's follow-up to PR 2446, 2026-10-26 (part of #2441). Since #2438
application.yml runs all of main in ONE concurrency group with
cancel-in-progress, so a newer push cancels the older commit's run before its
`build images` job finishes. hotfix.yml waits for exactly that job
(.github/actions/release-images, `--reuse-ci only`), and until this change the
lane then failed "never built" for a commit whose change the newer commit's
build already carries.

So hotfix.yml's `images` job gets release.yml's `after` step, on its failure
path only: main has moved past the commit AND application's push run for it
was cancelled without `build images` succeeding or failing -> green,
superseded=true, a summary naming main's newest commit, and promote, apply
and deploy skipped. Any other failure stays red.

It has NO `newest` check (the comment next to the step says why): a hotfix
that was built still ships, minutes before the newer commit's normal release.

The step is run here, as written in hotfix.yml, against the fake `gh` of
test_release_newest_only.py. WHAT THIS CANNOT PROVE is what that file cannot:
that GitHub lists a run cancelled by concurrency as `cancelled`, and that the
compare API says `ahead` for a commit main has moved past.

Also held here: the three comments that said application.yml gives each
commit on main a concurrency group of its own -- untrue since #2438 -- do not
say it again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from . import test_release_prod_gate as gate_model
from .test_release_newest_only import APP_JOBS, APP_RUNS, COMPARE, FAKE_GH, REPO_NAME, RUN_ID, SHA, _cancelled_run
from .test_release_prod_gate import _attempts
from .test_release_reuses_ci_images import REPO, _evaluate, _load_workflow, _render, _workflow

NEWER = "c" * 40
MAIN_HEAD = f"repos/{REPO_NAME}/commits/main"

PUSH = {
    "github.event_name": "push",
    "github.ref": "refs/heads/main",
    "github.event.inputs.environment": None,
    "github.event.inputs.skip_build": None,
}


def _images_steps() -> dict[str, dict]:
    steps = _load_workflow("hotfix.yml")["jobs"]["images"]["steps"]
    return {s["id"]: s for s in steps if "id" in s}


def _run_after(tmp_path: Path, responses: dict, *, stage: str) -> tuple[subprocess.CompletedProcess, dict, str, list]:
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
        ["bash", "-c", _images_steps()["after"]["run"]], env=env, capture_output=True, text=True, timeout=60
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return proc, outputs, summary.read_text(), log.read_text().splitlines()


# ---------------------------------------------------------------------------
# The step's place in hotfix.yml


def test_the_step_judges_the_stage_on_a_push_and_a_dispatch_is_untouched():
    """MUTATION: drop the `after` step, run it before the stage, or let
    `continue-on-error` hold on a dispatch (a dispatch ships main's head and
    must fail as it always did)."""
    job = _load_workflow("hotfix.yml")["jobs"]["images"]
    steps = _images_steps()
    ids = [s.get("id") for s in job["steps"]]
    assert "after" in ids, "hotfix.yml's images job has no `after` step: a build a newer push cancelled fails the lane red"
    assert ids.index("stage") < ids.index("after"), ids
    for event, expected in (("push", True), ("workflow_dispatch", False)):
        ctx = {"github.event_name": event}
        assert _evaluate(steps["stage"]["continue-on-error"], **ctx) is expected, event
        assert _evaluate(steps["after"]["if"], **ctx) is expected, event
    assert steps["after"]["env"]["STAGE"] == "${{ steps.stage.outcome }}"
    output = job["outputs"]["superseded"]
    for after, expected in (("true", "True"), ("false", "False"), ("", "False")):
        assert _render(output, **{"steps.after.outputs.superseded": after}) == expected, (after, output)


def test_a_hotfix_has_no_newest_check():
    """A hotfix that was built still ships; superseding it because main moved
    on would drop nearly every hotfix. MUTATION: copy release.yml's
    `newest` step across, and gate the stage on it."""
    steps = _images_steps()
    assert "newest" not in steps, "hotfix.yml supersedes a built hotfix as soon as anything merges after it"
    assert "steps.newest" not in json.dumps(_load_workflow("hotfix.yml"))


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setitem(gate_model.STEP_OUTPUTS, ("gate", "proceed"), ("true", "false", ""))
    monkeypatch.setitem(gate_model.STEP_OUTPUTS, ("images", "superseded"), ("true", "false"))
    return _workflow("hotfix.yml")["jobs"]


def test_a_superseded_hotfix_promotes_applies_and_deploys_nothing(gated):
    """MUTATION: drop `if: needs.images.outputs.superseded != 'true'` from
    `promote`, or give `infrastructure` or `deploy` an `always()`."""
    jobs = gated
    tried = 0
    for results, outs in _attempts(jobs, PUSH):
        if outs["images"].get("superseded") != "true":
            continue
        tried += 1
        for job_id in ("promote", "infrastructure", "deploy"):
            assert results[job_id] == "skipped", f"a superseded hotfix ran {job_id}: {results}"
    assert tried, "no attempt had images superseded: the model never asked"


def test_a_hotfix_that_is_not_superseded_still_reaches_every_stage(gated):
    clean = [
        r for r, o in _attempts(gated, PUSH, endings=("success",))
        if o["gate"].get("proceed") == "true" and o["images"].get("superseded") == "false"
    ]
    assert clean
    for results in clean:
        assert all(r == "success" for r in results.values()), results


# ---------------------------------------------------------------------------
# The step, run against a fake gh


def test_a_successful_stage_ships_even_when_main_has_moved(tmp_path):
    """No supersession after a success, unlike release.yml: the fix is built
    and goes out on the fast lane. MUTATION: supersede on `ahead` after a
    success."""
    proc, outputs, summary, calls = _run_after(tmp_path, {COMPARE: {"status": "ahead"}}, stage="success")
    assert proc.returncode == 0, proc.stderr
    assert outputs == {"superseded": "false"}
    assert summary == ""
    assert calls == [], f"a successful stage asked GitHub {calls}"


@pytest.mark.parametrize("build_conclusion", ["cancelled", "skipped", None])
def test_a_build_the_newer_push_cancelled_ends_the_hotfix_green(tmp_path, build_conclusion):
    """MUTATION: fail on it -- the hotfix red "never built" that #2438 made
    possible."""
    responses = {COMPARE: {"status": "ahead"}, MAIN_HEAD: {"sha": NEWER}, **_cancelled_run(build_conclusion)}
    proc, outputs, summary, calls = _run_after(tmp_path, responses, stage="failure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs == {"superseded": "true"}
    assert f"application run {RUN_ID}" in summary and SHA in summary
    assert NEWER in summary, f"the summary does not say which newer commit ships the change: {summary!r}"
    assert "promoted, applied and deployed nothing" in summary
    assert "::notice title=Superseded::" in proc.stdout
    assert APP_RUNS in calls and COMPARE in calls


def test_an_unreadable_main_head_still_ends_green_and_says_so(tmp_path):
    proc, outputs, summary, _ = _run_after(tmp_path, {COMPARE: {"status": "ahead"}, **_cancelled_run("cancelled")}, stage="failure")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs == {"superseded": "true"}
    assert "unreadable" in summary


@pytest.mark.parametrize(
    "case, status, responses",
    [
        ("main-not-moved", "identical", _cancelled_run("cancelled")),
        ("build-failed", "ahead", _cancelled_run("failure")),
        ("build-succeeded", "ahead", _cancelled_run("success")),
        ("run-failed", "ahead", {
            APP_RUNS: {"workflow_runs": [{"id": RUN_ID, "conclusion": "failure"}]},
            APP_JOBS: {"jobs": [{"name": "build images", "conclusion": "skipped"}]},
        }),
        ("no-run", "ahead", {APP_RUNS: {"workflow_runs": []}}),
        ("unreadable", "ahead", {}),
        ("unreadable-compare", None, _cancelled_run("cancelled")),
    ],
)
def test_any_other_failure_stays_red(tmp_path, case, status, responses):
    """MUTATION: read every failure on a moved main as superseded, or drop the
    check of the build job's own conclusion."""
    if status is not None:
        responses = {COMPARE: {"status": status}, **responses}
    proc, outputs, summary, _ = _run_after(tmp_path, {MAIN_HEAD: {"sha": NEWER}, **responses}, stage="failure")
    assert proc.returncode == 1, f"{case}: {proc.stdout}{proc.stderr}"
    assert outputs.get("superseded") != "true", case
    assert "::error title=images and scan failed::" in proc.stdout, case
    assert summary == "", case


# ---------------------------------------------------------------------------
# The comments that said each commit on main has a group of its own

STALE_COMMENTS = [
    REPO / "scripts" / "lib" / "ci-built-images.sh",
    REPO / ".github" / "actions" / "release-images" / "action.yml",
]

# Each commit on main having a concurrency group of its own, however it is
# phrased. Read over the comment text with `#` and line breaks folded away.
_OWN_GROUP = [
    re.compile(r"\bcommit\b(?:\W+\w+){0,12}?\W+(?:its|their)\s+own\s+(?:concurrency\s+)?group", re.I),
    re.compile(r"\bcommit\b(?:\W+\w+){0,12}?\W+(?:a\s+)?(?:concurrency\s+)?group\s+of\s+(?:its|their)\s+own", re.I),
    re.compile(r"\bgroup\s+per\s+commit\b", re.I),
    re.compile(r"\bper-commit\s+(?:concurrency\s+)?group", re.I),
]

# The sentences this guard exists to keep out, as they stood before #2441's
# follow-up. Each must be caught, or the guard reads nothing.
_THE_OLD_SENTENCES = [
    "On main a newer push neither cancels nor replaces the run -- application.yml gives each commit there its own "
    "concurrency group -- so a cancellation is somebody's deliberate act;",
    "On main each commit's application run has a concurrency group of its own, so it starts at the push",
    "That run exists and is not stopped by later pushes: application.yml gives each commit on main a concurrency "
    "group of its own, because GitHub cancels a PENDING run",
]


def _comment_text(path: Path) -> str:
    lines = [line.split("#", 1)[1] for line in path.read_text().splitlines() if "#" in line]
    return " ".join(" ".join(lines).split())


def _says_own_group(text: str) -> list[str]:
    return [m.group(0) for pattern in _OWN_GROUP for m in pattern.finditer(text)]


@pytest.mark.parametrize("sentence", _THE_OLD_SENTENCES)
def test_the_guard_catches_each_stale_sentence(sentence):
    assert _says_own_group(sentence), f"the guard reads past {sentence!r}"


def test_the_guard_passes_what_is_true_now():
    for sentence in (
        "since #2438 application.yml runs all of main in ONE group with cancel-in-progress, because one run per "
        "commit queued 27-78 runs at once",
        "Pull requests keep their own group.",
    ):
        assert not _says_own_group(sentence), sentence


@pytest.mark.parametrize("path", STALE_COMMENTS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_comment_says_each_commit_on_main_has_its_own_group(path):
    """Untrue since #2438: main is one group with cancel-in-progress, and a
    newer push cancels the older run. A comment that says otherwise is how
    the next reader treats a cancelled build as somebody's deliberate act.
    MUTATION: put any of _THE_OLD_SENTENCES back."""
    found = _says_own_group(_comment_text(path))
    assert not found, f"{path.relative_to(REPO)} says each commit on main has a group of its own: {found}"
    assert "#2438" in _comment_text(path), f"{path.relative_to(REPO)} no longer says why main's runs are cancelled"
