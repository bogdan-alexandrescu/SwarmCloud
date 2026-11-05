"""The plugin's bridge tag is created once the unit tests pass, and its absence goes red.

WHY THIS EXISTS (#272). A plugin version bump pins `sc-vX.Y.Z` twice in
plugin/.claude-plugin/plugin.json -- `version`, and the `@sc-vX.Y.Z` ref its
MCP server installs the swarm-mcp bridge from -- and
tests/unit/mcp/test_plugin_bridge_install.py holds the two equal. But the tag
itself was something only a human made, and sc-v0.5.6 did not exist when main
already pinned it: every fresh install of that version failed to fetch its
bridge, and nothing in CI could have said so.

Two halves, both held here by EXECUTING their `run:` text, read out of the
workflow files, against stub `git` and `gh` executables:

* application.yml's `plugin-tag` job creates the annotated tag on the commit
  main was pushed to, after `format / unit tests` and NOT after the image
  build (owner decision 2026-11-04, "Tag once tests pass": release.yml's job
  waited about an hour for `images and scan`, and every plugin reload in that
  hour lost its bridge), on a push to main only, never for a commit main has
  moved past, only when the remote does not have it -- and never moves one
  that exists, because users' installs already resolved it.
* application.yml's `shell` job, on main, fails when the tag is missing AND a
  completed push run whose `format / unit tests` passed already descends from
  the commit that bumped the version and its plugin-tag failed or never ran
  (the run that should have tagged it did not), whatever that run's overall
  conclusion, and only warns when no such run has finished since the bump.

WHAT THIS CANNOT PROVE: that GitHub's GITHUB_TOKEN with `contents: write` may
push a tag here (no ruleset protecting `sc-v*` was measured), or what the real
`git ls-remote`, `gh api`, `gh run list` and `gh run view` print -- the stubs answer in the shapes those
commands document. CI's first push to main after a bump is the measurement.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from .test_release_reuses_ci_images import _evaluate, _workflow

VERSION = "9.9.9"
TAG = f"sc-v{VERSION}"
RELEASE_SHA = "a" * 40
BUMP_SHA = "b" * 40
OTHER_SHA = "c" * 40

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="the steps read plugin.json with jq")


# ---------------------------------------------------------------------------
# Locating the code under test.
# ---------------------------------------------------------------------------


def _taggers(name: str) -> list[tuple[str, dict]]:
    return [
        (job_id, job)
        for job_id, job in _workflow(name)["jobs"].items()
        if any(re.search(r"\btag\s+-a\b", str(step.get("run", ""))) for step in job.get("steps") or [])
    ]


def _tag_job() -> tuple[str, dict]:
    """The one application.yml job that creates a tag."""
    found = _taggers("application.yml")
    assert len(found) == 1, f"application.yml has {len(found)} jobs that create an annotated tag; expected one (plugin-tag)"
    return found[0]


def _newest_step() -> dict:
    _, job = _tag_job()
    steps = [s for s in job.get("steps") or [] if s.get("id") == "newest"]
    assert len(steps) == 1, "the tag job has no single `newest` step deciding whether main moved past the commit"
    return steps[0]


def _tag_step() -> dict:
    _, job = _tag_job()
    steps = [s for s in job.get("steps") or [] if re.search(r"\btag\s+-a\b", str(s.get("run", "")))]
    assert len(steps) == 1, f"the tag job has {len(steps)} steps creating a tag; expected one"
    return steps[0]


def _shell_job() -> dict:
    return _workflow("application.yml")["jobs"]["shell"]


def _check_step() -> dict:
    steps = [s for s in _shell_job().get("steps") or [] if "pinned bridge tag" in str(s.get("name", ""))]
    assert len(steps) == 1, (
        f"application.yml's shell job has {len(steps)} steps checking the plugin's pinned bridge tag; expected one"
    )
    return steps[0]


# ---------------------------------------------------------------------------
# Running it.
# ---------------------------------------------------------------------------

_FAKE_GIT = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    """A git that logs every call and answers from FAKE_* env vars."""
    import json, os, sys

    args = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["git"] + args) + "\n")
    while args and args[0] == "-c":
        args = args[2:]
    cmd, rest = (args[0], args[1:]) if args else ("", [])
    env = os.environ

    if cmd == "ls-remote":
        state = env.get("FAKE_REMOTE_TAG", "absent")
        if state == "error":
            sys.stderr.write("fatal: unable to access remote\n")
            sys.exit(128)
        if state == "absent" or (state.startswith("after-push:") and not os.path.exists(env["FAKE_PUSHED"])):
            sys.exit(2 if "--exit-code" in rest else 0)
        sha = state.split(":", 1)[1]
        ref = rest[-1]
        print(f"{'d' * 40}\t{ref}")
        print(f"{sha}\t{ref}^{{}}")
        sys.exit(0)
    if cmd == "tag":
        sys.exit(0)
    if cmd == "push":
        if env.get("FAKE_PUSH") == "fail":
            sys.stderr.write("! [rejected] (already exists)\n")
            open(env["FAKE_PUSHED"], "w").close()
            sys.exit(1)
        open(env["FAKE_PUSHED"], "w").close()
        sys.exit(0)
    if cmd == "rev-parse" and "--is-shallow-repository" in rest:
        print("true")
        sys.exit(0)
    if cmd == "fetch":
        sys.exit(0)
    if cmd == "log":
        bump = env.get("FAKE_BUMP", "")
        if bump:
            print(bump)
        sys.exit(0)
    if cmd == "cat-file":
        sha = rest[-1].split("^", 1)[0]
        sys.exit(0 if sha in env.get("FAKE_KNOWN", "").split(",") else 128)
    if cmd == "merge-base" and rest[:1] == ["--is-ancestor"]:
        ancestor, descendant = rest[1], rest[2]
        if ancestor == env.get("FAKE_BUMP") and descendant in env.get("FAKE_DESCENDANTS", "").split(","):
            sys.exit(0)
        sys.exit(1)
    sys.stderr.write(f"fake git: unexpected call {sys.argv[1:]}\n")
    sys.exit(97)
    '''
).lstrip()

_FAKE_GH = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    """A gh that logs every call and answers `run list` from FAKE_GH_RUNS."""
    import json, os, sys

    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps(["gh"] + sys.argv[1:]) + "\n")
    if os.environ.get("FAKE_GH") == "fail":
        sys.stderr.write("HTTP 401: Bad credentials\n")
        sys.exit(1)
    # FAKE_COMPARE: what `gh api .../compare --jq .status` prints, or `fail`.
    # FAKE_GH_RUNS: comma-separated head shas, newest first; run i has
    # databaseId 100+i. FAKE_GH_JOBS: JSON {head sha: {job name: conclusion}};
    # a head it omits passed its unit tests and its plugin-tag FAILED.
    heads = [s for s in os.environ.get("FAKE_GH_RUNS", "").split(",") if s]
    if sys.argv[1:2] == ["api"]:
        compare = os.environ.get("FAKE_COMPARE", "identical")
        if compare == "fail":
            sys.stderr.write("HTTP 502\n")
            sys.exit(1)
        print(compare)
        sys.exit(0)
    if sys.argv[1:3] == ["run", "list"]:
        print(json.dumps([{"databaseId": 100 + i, "headSha": s} for i, s in enumerate(heads)]))
        sys.exit(0)
    if sys.argv[1:3] == ["run", "view"]:
        if os.environ.get("FAKE_GH_VIEW") == "fail":
            sys.stderr.write("HTTP 502\n")
            sys.exit(1)
        sha = heads[int(sys.argv[3]) - 100]
        jobs = json.loads(os.environ.get("FAKE_GH_JOBS", "{}")).get(
            sha, {"format / unit tests": "success", "tag the plugin's bridge (sc-v<version>)": "failure"}
        )
        print(json.dumps({"jobs": [{"name": n, "conclusion": c} for n, c in jobs.items()]}))
        sys.exit(0)
    sys.stderr.write(f"fake gh: unexpected call {sys.argv[1:]}\n")
    sys.exit(97)
    '''
).lstrip()


def _render_env(step: dict) -> dict[str, str]:
    rendered = {}
    for key, value in (step.get("env") or {}).items():
        text = str(value)
        text = re.sub(r"\$\{\{\s*github\.sha\s*\}\}", RELEASE_SHA, text)
        text = re.sub(r"\$\{\{\s*github\.token\s*\}\}", "fake-token", text)
        assert "${{" not in text, f"the step's env {key} is {value!r}, which this test does not render"
        rendered[key] = text
    return rendered


def _run(step: dict, tmp_path: Path, *, version: str = VERSION, **fake: str):
    work = tmp_path / "work"
    (work / "plugin" / ".claude-plugin").mkdir(parents=True)
    (work / "plugin" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "sc", "version": version}))
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("git", _FAKE_GIT), ("gh", _FAKE_GH)):
        path = stubs / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(log),
        "FAKE_PUSHED": str(tmp_path / "pushed"),
        "GITHUB_SHA": RELEASE_SHA,
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_OUTPUT": str(tmp_path / "github_output"),
        **_render_env(step),
        **fake,
    }
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=work, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return result, calls


def _git_subcommand(call: list[str]) -> tuple[str, list[str]]:
    args = call[1:]
    while args and args[0] == "-c":
        args = args[2:]
    return (args[0], args[1:]) if args else ("", [])


def _git(calls: list[list[str]], sub: str) -> list[list[str]]:
    return [_git_subcommand(c)[1] for c in calls if c[0] == "git" and _git_subcommand(c)[0] == sub]


def _forces(calls: list[list[str]]) -> list[list[str]]:
    return [
        c
        for c in calls
        if any(a in ("-f", "--force", "--force-with-lease") or a.startswith("--force") or a.startswith("+") for a in c)
    ]


# ---------------------------------------------------------------------------
# application.yml: the job that tags.
# ---------------------------------------------------------------------------


def test_an_absent_tag_is_created_annotated_on_the_pushed_commit_and_pushed(tmp_path):
    result, calls = _run(_tag_step(), tmp_path, FAKE_REMOTE_TAG="absent")
    assert result.returncode == 0, result.stdout + result.stderr
    tags = _git(calls, "tag")
    pushes = _git(calls, "push")
    assert len(tags) == 1 and tags[0][0] == "-a" and TAG in tags[0] and RELEASE_SHA in tags[0], tags
    assert pushes == [["origin", f"refs/tags/{TAG}"]], pushes
    order = [_git_subcommand(c)[0] for c in calls if c[0] == "git"]
    assert order.index("tag") < order.index("push"), order
    tagger = [c for c in calls if c[0] == "git" and _git_subcommand(c)[0] == "tag"][0]
    assert "user.name=github-actions[bot]" in tagger, f"the tag has no bot tagger identity: {tagger}"
    assert not _forces(calls), _forces(calls)


@pytest.mark.parametrize("at", (RELEASE_SHA, OTHER_SHA), ids=("on-this-commit", "on-another-commit"))
def test_an_existing_tag_is_never_moved(tmp_path, at):
    result, calls = _run(_tag_step(), tmp_path, FAKE_REMOTE_TAG=f"present:{at}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(calls, "tag") == [] and _git(calls, "push") == [], calls
    assert "::notice" in result.stdout and TAG in result.stdout, result.stdout
    if at != RELEASE_SHA:
        assert at in result.stdout and RELEASE_SHA in result.stdout, (
            f"the tag points at another commit and the job did not say so: {result.stdout}"
        )
    assert not _forces(calls), _forces(calls)


def test_a_remote_that_cannot_be_read_is_not_read_as_an_absent_tag(tmp_path):
    result, calls = _run(_tag_step(), tmp_path, FAKE_REMOTE_TAG="error")
    assert result.returncode != 0, result.stdout
    assert _git(calls, "tag") == [] and _git(calls, "push") == [], calls


def test_a_tag_pushed_first_by_someone_else_is_not_a_failure(tmp_path):
    """The tag can appear between the read and the push (pushed by hand, as
    sc-v0.5.79 was); the push is rejected, and the tag it wanted exists."""
    result, calls = _run(
        _tag_step(), tmp_path, FAKE_REMOTE_TAG=f"after-push:{RELEASE_SHA}", FAKE_PUSH="fail"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _forces(calls), _forces(calls)


def test_a_rejected_push_with_no_tag_behind_it_fails(tmp_path):
    result, _ = _run(_tag_step(), tmp_path, FAKE_REMOTE_TAG="absent", FAKE_PUSH="fail")
    assert result.returncode != 0, result.stdout


@pytest.mark.parametrize("version", ("1.2", "1.2.3; rm -rf /", "v1.2.3", "", "null", "1.2.3\n4.5.6"))
def test_a_version_that_is_not_x_y_z_is_refused(tmp_path, version):
    result, calls = _run(_tag_step(), tmp_path, version=version, FAKE_REMOTE_TAG="absent")
    assert result.returncode != 0, result.stdout
    assert _git(calls, "tag") == [] and _git(calls, "push") == [], calls


def test_only_the_tag_job_may_write_contents():
    for name in ("application.yml", "release.yml"):
        assert _workflow(name)["permissions"].get("contents") == "read", (name, _workflow(name)["permissions"])
    job_id, job = _tag_job()
    assert job_id == "plugin-tag", job_id
    assert (job.get("permissions") or {}).get("contents") == "write", job.get("permissions")
    for name in ("application.yml", "release.yml"):
        writers = sorted(
            j for j, other in _workflow(name)["jobs"].items() if (other.get("permissions") or {}).get("contents") == "write"
        )
        assert writers == ([job_id] if name == "application.yml" else []), f"jobs with contents: write in {name}: {writers}"


def test_the_release_no_longer_tags():
    """One job tags. MUTATION: put a tagging job back in release.yml."""
    assert _taggers("release.yml") == [], "release.yml still creates an annotated tag; application.yml's plugin-tag does"


def test_the_tag_waits_for_the_unit_tests_and_not_the_image_build():
    """Owner decision 2026-11-04, "Tag once tests pass": on 2026-11-04 the tag
    waited about an hour behind release.yml's `images and scan`, and every
    plugin reload in that hour lost its bridge. MUTATION: make it need `build`,
    drop `python`, or let it run past a `python` that did not pass."""
    _, job = _tag_job()
    jobs = _workflow("application.yml")["jobs"]
    assert jobs["python"]["name"] == "format / unit tests", jobs["python"]["name"]
    needs = job.get("needs")
    needs = [needs] if isinstance(needs, str) else list(needs or [])
    assert needs == ["python"], f"the tag job needs {needs}; the proof is `format / unit tests` alone"
    job_if = str(job.get("if", ""))
    assert not re.search(r"\b(always|failure|cancelled)\(\s*\)", job_if), job_if
    push_main = {"github.event_name": "push", "github.ref": "refs/heads/main", "needs.python.result": "success"}
    assert _evaluate(job_if, **push_main) is True, job_if
    for result in ("failure", "cancelled", "skipped"):
        assert _evaluate(job_if, **{**push_main, "needs.python.result": result}) is False, result


@pytest.mark.parametrize(
    "event, ref",
    (
        ("pull_request", "refs/pull/7/merge"),
        ("merge_group", "refs/heads/gh-readonly-queue/main/pr-7"),
        ("workflow_dispatch", "refs/heads/main"),
        ("workflow_dispatch", "refs/heads/feature"),
        ("push", "refs/heads/feature"),
    ),
)
def test_the_tag_job_runs_on_a_push_to_main_only(event, ref):
    """MUTATION: drop the event or the ref clause from the job's `if:`."""
    _, job = _tag_job()
    ctx = {"github.event_name": event, "github.ref": ref, "needs.python.result": "success"}
    assert _evaluate(str(job.get("if", "")), **ctx) is False, (event, ref)


def test_the_gate_waits_for_the_tag():
    """A tag that failed on main shows on main's ci-gate."""
    needs = _workflow("application.yml")["jobs"]["ci-gate"]["needs"]
    assert "plugin-tag" in needs, needs


def _outputs(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "github_output"
    text = path.read_text() if path.exists() else ""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


@pytest.mark.parametrize(
    "compare, superseded, warns",
    (("ahead", "true", False), ("identical", "false", False), ("fail", "false", True)),
)
def test_a_commit_main_has_moved_past_tags_nothing(tmp_path, compare, superseded, warns):
    """A superseded commit tags nothing; the newer commit's run tags it. An
    unreadable compare is not "superseded": the commit passed its unit tests.
    MUTATION: drop the `newest` step's condition from the tag step, or read
    an unreadable API as superseded."""
    step = _newest_step()
    result, calls = _run(step, tmp_path, FAKE_COMPARE=compare)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _outputs(tmp_path) == {"superseded": superseded}, _outputs(tmp_path)
    assert ("::warning" in result.stdout) is warns, result.stdout
    compares = [c for c in calls if c[0] == "gh" and c[1] == "api"]
    assert compares and "/compare/" in compares[0][2] and compares[0][2].endswith(f"{RELEASE_SHA}...main"), compares
    tag_if = _tag_step().get("if")
    assert _evaluate(tag_if, **{"steps.newest.outputs.superseded": superseded}) is (superseded == "false"), tag_if
    _, job = _tag_job()
    ids = [s.get("id") for s in job["steps"]]
    assert ids.index("newest") < job["steps"].index(_tag_step()), ids


def test_the_version_is_read_from_plugin_json_never_restated():
    run = _tag_step()["run"]
    assert "plugin/.claude-plugin/plugin.json" in run and "jq" in run
    assert not re.search(r"sc-v\d", run), "the tag job names a version literally"


# ---------------------------------------------------------------------------
# application.yml: the check.
# ---------------------------------------------------------------------------


def test_the_check_passes_when_the_tag_exists(tmp_path):
    result, calls = _run(_check_step(), tmp_path, FAKE_REMOTE_TAG=f"present:{OTHER_SHA}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [c for c in calls if c[0] == "gh"], "the tag exists; there is nothing to ask GitHub"


def test_the_check_fails_when_a_run_since_the_bump_did_not_tag(tmp_path):
    result, _ = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=f"{OTHER_SHA},{RELEASE_SHA}",
        FAKE_KNOWN=f"{OTHER_SHA},{RELEASE_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS=RELEASE_SHA,
    )
    assert result.returncode != 0, result.stdout
    assert "::error" in result.stdout and TAG in result.stdout, result.stdout
    assert BUMP_SHA in result.stdout and RELEASE_SHA in result.stdout, result.stdout
    assert "examined 2" in result.stdout, "the check did not say how many runs it examined"


def test_the_check_fails_when_the_run_that_should_have_tagged_concluded_failure(tmp_path):
    """The case #272 exists for: a run after the bump passed its unit tests,
    and its plugin-tag job failed, so the RUN concluded `failure`. A check that
    read only successful runs would warn here forever."""
    result, calls = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=RELEASE_SHA,
        FAKE_KNOWN=f"{RELEASE_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS=RELEASE_SHA,
        FAKE_GH_JOBS=json.dumps(
            {RELEASE_SHA: {"format / unit tests": "success", "tag the plugin's bridge (sc-v<version>)": "failure", "build images": "skipped"}}
        ),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error" in result.stdout and TAG in result.stdout, result.stdout
    assert "'failure'" in result.stdout, "the error does not name the plugin-tag job's conclusion"
    listed = [c for c in calls if c[0] == "gh" and c[1:3] == ["run", "list"]]
    assert listed and "success" not in listed[0], f"the check still filters runs on their conclusion: {listed}"
    args = listed[0]
    assert args[args.index("--workflow") + 1] == "application.yml", f"the check reads another workflow's runs: {args}"
    assert args[args.index("--event") + 1] == "push", f"the check reads runs that never tag: {args}"


@pytest.mark.parametrize("tagjob", ("success", "cancelled"))
def test_a_descended_run_a_newer_commit_superseded_only_warns(tmp_path, tagjob):
    """A plugin-tag that succeeded or was cancelled without the tag is a
    commit main moved past; the newer run tags it. MUTATION: count it as
    the run that should have tagged."""
    result, _ = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=RELEASE_SHA,
        FAKE_KNOWN=f"{RELEASE_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS=RELEASE_SHA,
        FAKE_GH_JOBS=json.dumps({RELEASE_SHA: {"format / unit tests": "success", "tag the plugin's bridge (sc-v<version>)": tagjob}}),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning" in result.stdout and "::error" not in result.stdout, result.stdout
    assert "superseded" in result.stdout, result.stdout


def test_a_descended_run_whose_unit_tests_failed_only_warns(tmp_path):
    """plugin-tag needs `format / unit tests`; a run where it failed never tried."""
    result, _ = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=RELEASE_SHA,
        FAKE_KNOWN=f"{RELEASE_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS=RELEASE_SHA,
        FAKE_GH_JOBS=json.dumps({RELEASE_SHA: {"format / unit tests": "failure", "tag the plugin's bridge (sc-v<version>)": "skipped"}}),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning" in result.stdout and "::error" not in result.stdout, result.stdout


def test_a_failed_run_view_is_a_failure_not_an_untried_run(tmp_path):
    result, _ = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=RELEASE_SHA,
        FAKE_KNOWN=f"{RELEASE_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS=RELEASE_SHA,
        FAKE_GH_VIEW="fail",
    )
    assert result.returncode != 0, result.stdout
    assert "::warning" not in result.stdout, result.stdout


def test_the_check_only_warns_when_no_run_has_finished_since_the_bump(tmp_path):
    result, _ = _run(
        _check_step(),
        tmp_path,
        FAKE_REMOTE_TAG="absent",
        FAKE_BUMP=BUMP_SHA,
        FAKE_GH_RUNS=f"{OTHER_SHA},{'e' * 40}",
        FAKE_KNOWN=f"{OTHER_SHA},{BUMP_SHA}",
        FAKE_DESCENDANTS="",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning" in result.stdout and TAG in result.stdout, result.stdout
    assert "::error" not in result.stdout, result.stdout
    assert "e" * 40 in result.stdout, "a run head not in the clone was counted silently"
    assert "examined 2" in result.stdout, result.stdout


def test_a_failed_gh_call_is_a_failure_not_no_runs(tmp_path):
    result, _ = _run(
        _check_step(), tmp_path, FAKE_REMOTE_TAG="absent", FAKE_BUMP=BUMP_SHA, FAKE_KNOWN=BUMP_SHA, FAKE_GH="fail"
    )
    assert result.returncode != 0, result.stdout
    assert "::error" in result.stdout, result.stdout
    assert "::warning" not in result.stdout, "a failed gh call was read as no runs"


def test_a_remote_that_cannot_be_read_fails_the_check(tmp_path):
    result, _ = _run(_check_step(), tmp_path, FAKE_REMOTE_TAG="error")
    assert result.returncode != 0, result.stdout


def test_the_check_runs_on_main_with_what_it_needs():
    step = _check_step()
    assert "refs/heads/main" in str(step.get("if", "")), step.get("if")
    assert "GH_TOKEN" in (step.get("env") or {}), step.get("env")
    granted = _shell_job().get("permissions") or {}
    assert granted.get("actions") == "read", granted
    assert granted.get("contents") == "read", granted
