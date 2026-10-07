"""A pull request builds the images it changes on the GitHub runner, with
`docker buildx`, and pushes nothing (#650, owner decision 2026-10-05).

THE DECISION THIS PINS. #673 added `build-images.sh --affected-by` and a
`build-check` job that, with no pull-request build identity, only warned which
images it would build. The owner chose NOT to create a Google identity for pull
requests: code on any branch could then run Cloud Build in the shared project.
So `build-check` builds every affected image on the runner itself, with no
registry login, no GCP authentication and no secret, in the order main uses
(agent-runtime-browser and agent-runtime-indexer FROM a base built in the same
job, never pulled), and so runs the same Dockerfile steps -- the self-tests
among them -- that main's Cloud Build runs.

The properties asserted here, each against the REAL script and workflow:

  * `build-images.sh --build-only --local` calls `docker buildx build` and
    nothing that talks to a registry: no push, no login, no registry output,
    no gcloud at all, no manifest;
  * an image built FROM agent-runtime-base is built after it, FROM the base
    this run built (by digest, through a local OCI layout), and the base is
    built even when only the derived image was named;
  * every build is of the Dockerfile main's recipe builds, to its last stage,
    uncached, so every RUN self-test in it runs; the steps it ran are READ
    from that Dockerfile into the report -- a planted RUN shows up -- and the
    workflow restates none of them;
  * a checked-in cloudbuild.yaml (swarm-verify) is replayed step by step,
    minus its push;
  * the workflow job: buildx, no `--push`, no login, no `id-token`, no
    `secrets.`, no `vars.GCP_PR_BUILD_*`; a fork's pull request and a change
    that reaches no image skip in seconds; one that reaches an image builds;
  * ci-gate fails when this job failed.

WHAT THIS CANNOT PROVE: that BuildKit resolves the base through the
`oci-layout://` named context on a real runner, that the runner has disk for
agent-runtime-base, or how long a build takes. The first pull request touching
an image after this lands is that proof -- read its `build-check` summary,
which records the free disk before and after it was freed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from .test_build_images_pr_check import _env, _sandbox
from .test_ci_gate import judge

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "application.yml"
CI_DOC = REPO / "docs" / "ci.md"
TAG = "feed00c0ffee"
JOB = "build-check"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None or shutil.which("git") is None,
    reason="build-images.sh needs bash and jq; the plan step needs git",
)

FAKE_DOCKER = r'''#!{python}
import hashlib, json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps({"tool": "docker", "argv": args, "cwd": os.getcwd()}) + "\n")
if args[:2] == ["buildx", "version"]:
    print("github.com/docker/buildx v0.29.1 fake")
    sys.exit(0)
if args[:2] == ["buildx", "inspect"]:
    sys.exit(0 if os.path.exists(os.environ["FAKE_BUILDER"]) else 1)
if args[:2] == ["buildx", "create"]:
    open(os.environ["FAKE_BUILDER"], "w").close()
    sys.exit(0)
if args[:2] == ["buildx", "build"]:
    tag = args[args.index("-t") + 1]
    target = tag.split("/")[-1].split(":")[0]
    if target in os.environ.get("FAKE_FAIL", "").split(","):
        print("#14 ERROR: process \"/bin/sh -c swarm-repo-index --lsp-self-test\" did not complete successfully: exit code: 1")
        sys.exit(1)
    if "--metadata-file" in args:
        digest = "sha256:" + hashlib.sha256(target.encode()).hexdigest()
        with open(args[args.index("--metadata-file") + 1], "w") as fh:
            json.dump({"containerimage.digest": digest}, fh)
    print("#9 DONE " + target)
    sys.exit(0)
if args[:1] == ["run"]:
    sys.exit(0)
print("fake docker: a local build-only run has no business calling docker " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

FAKE_GCLOUD = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps({"tool": "gcloud", "argv": sys.argv[1:]}) + "\n")
print("fake gcloud: a local build-only run must not call gcloud", file=sys.stderr)
sys.exit(2)
'''


def _digest(target: str) -> str:
    return "sha256:" + hashlib.sha256(target.encode()).hexdigest()


def _local(tmp_path: Path, *args: str, fail: str = "", root: Path | None = None):
    root = root or _sandbox(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("docker", FAKE_DOCKER), ("gcloud", FAKE_GCLOUD)):
        tool = bindir / name
        tool.write_text(body.replace("{python}", sys.executable))
        tool.chmod(0o755)
    events_file = tmp_path / "events.jsonl"
    env = _env(
        tmp_path,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_EVENTS=str(events_file),
        FAKE_BUILDER=str(tmp_path / "builder-exists"),
        FAKE_FAIL=fail,
        SWARM_PYTHON=sys.executable,
    )
    env.pop("GITHUB_STEP_SUMMARY", None)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    events = (
        [json.loads(line) for line in events_file.read_text().splitlines() if line]
        if events_file.exists()
        else []
    )
    return root, proc, events


def _builds(events: list[dict]) -> list[list[str]]:
    return [e["argv"] for e in events if e["tool"] == "docker" and e["argv"][:2] == ["buildx", "build"]]


def _built_target(argv: list[str]) -> str:
    return argv[argv.index("-t") + 1].split("/")[-1].split(":")[0]


def _value(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]


# ---------------------------------------------------------------------------
# build-images.sh --build-only --local
# ---------------------------------------------------------------------------
def test_local_build_uses_buildx_and_pushes_nothing(tmp_path):
    root, proc, events = _local(
        tmp_path, "--build-only", "--local", "--tag", TAG,
        "swarm-api", "swarm-verify", "agent-runtime-browser", "agent-runtime-indexer",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]

    assert not [e for e in events if e["tool"] == "gcloud"], "a local build called gcloud"
    for e in events:
        argv = e["argv"]
        joined = " ".join(argv)
        assert argv[0] in {"buildx", "run"}, f"docker {joined}: not a build"
        assert argv[:2] != ["buildx", "build"] or "--push" not in argv, joined
        assert not re.search(r"\b(login|push|pull|tag)\b", " ".join(argv[:2])), joined
        assert "type=registry" not in joined and "push=true" not in joined, joined
        assert "docker.pkg.dev" not in joined, joined
        assert "--load" not in argv, f"a build loaded an image into the daemon: {joined}"

    builds = _builds(events)
    for argv in builds:
        outputs = _value(argv, "--output")
        assert len(outputs) == 1, f"every build names its output explicitly: {argv}"
        kind = outputs[0].split(",")[0]
        assert kind in {"type=cacheonly", "type=oci"}, f"a build exports somewhere other than here: {argv}"
        if kind == "type=oci":
            dest = dict(p.split("=", 1) for p in outputs[0].split(","))["dest"]
            assert Path(dest).is_absolute() and str(dest).startswith(str(root)), dest

    assert not (root / "build" / "images-dev.json").exists(), "a build-only run wrote a manifest"
    assert "pushed nothing" in proc.stderr, proc.stderr[-1500:]


def test_derived_images_are_built_after_the_base_and_from_it(tmp_path):
    """Only the indexer and the browser are named: the base is built anyway,
    once, first, and both are built FROM the digest this run produced."""
    _, proc, events = _local(
        tmp_path, "--build-only", "--local", "--tag", TAG,
        "agent-runtime-indexer", "agent-runtime-browser",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    order = [_built_target(argv) for argv in _builds(events)]
    assert order[0] == "agent-runtime-base" and order.count("agent-runtime-base") == 1, order
    assert set(order) == {"agent-runtime-base", "agent-runtime-indexer", "agent-runtime-browser"}, order

    base = _builds(events)[0]
    layout = dict(p.split("=", 1) for p in _value(base, "--output")[0].split(","))
    assert layout["type"] == "oci" and layout.get("tar") == "false", layout
    digest = _digest("agent-runtime-base")
    for argv in _builds(events)[1:]:
        base_ref = f"swarm-build-only/agent-runtime-base@{digest}"
        assert f"BASE_IMAGE={base_ref}" in _value(argv, "--build-arg"), argv
        # The indexer's Dockerfile refuses a BASE_IMAGE without a digest; the
        # named context is what resolves it with no registry.
        assert f"{base_ref}=oci-layout://{layout['dest']}@{digest}" in _value(argv, "--build-context"), argv


def test_a_base_that_failed_builds_nothing_from_it(tmp_path):
    _, proc, events = _local(
        tmp_path, "--build-only", "--local", "--tag", TAG,
        "agent-runtime-browser", "swarm-ui", fail="agent-runtime-base",
    )
    assert proc.returncode != 0
    built = [_built_target(argv) for argv in _builds(events)]
    assert "agent-runtime-browser" not in built, built
    assert "swarm-ui" in built, "an independent image is still built and judged"
    last = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "agent-runtime-base" in last and "agent-runtime-browser" in last, last
    assert "swarm-ui" not in last.split("failed:")[-1].split(";")[0], last


def test_a_failed_self_test_fails_the_run_naming_the_image(tmp_path):
    _, proc, _ = _local(
        tmp_path, "--build-only", "--local", "--tag", TAG,
        "agent-runtime-indexer", "swarm-ui", fail="agent-runtime-indexer",
    )
    assert proc.returncode != 0
    last = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "agent-runtime-indexer" in last and "swarm-ui" not in last, last
    assert "lsp-self-test" in proc.stderr, "the failing step's output is shown"


def test_each_build_is_the_dockerfile_main_builds_to_its_last_stage_uncached(tmp_path):
    """The self-tests are RUN steps of these Dockerfiles: building the same
    file, to its final stage and with no cache, is running them. A `--target`
    would stop before them; a cache hit would skip them."""
    _, proc, events = _local(
        tmp_path, "--build-only", "--local", "--tag", TAG,
        "agent-runtime-browser", "agent-runtime-indexer", "swarm-api", "swarm-ui",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    for argv in _builds(events):
        target = _built_target(argv)
        recipe = REPO / "images" / target / "cloudbuild.yaml"
        if recipe.exists():
            # What main's checked-in recipe builds: its _DOCKERFILE.
            expected = yaml.safe_load(recipe.read_text())["substitutions"]["_DOCKERFILE"]
        else:
            expected = f"images/{target}/Dockerfile"
        assert _value(argv, "-f") == [expected], argv
        assert "--target" not in argv, argv
        assert "--no-cache" in argv, argv
        assert _value(argv, "--platform") == ["linux/amd64"], argv
        assert f"GIT_SHA={TAG}" in _value(argv, "--build-arg"), argv
    ui = next(a for a in _builds(events) if _built_target(a) == "swarm-ui")
    assert "VITE_SWARM_ENV=dev" in _value(ui, "--build-arg"), "swarm-ui is built for dev, as main builds it"


def test_the_steps_it_ran_are_read_from_the_dockerfile(tmp_path):
    """The report lists every RUN step each image's build ran, read from its
    Dockerfile: a planted step appears with nothing else edited."""
    root = _sandbox(tmp_path)
    dockerfile = root / "images" / "agent-runtime-indexer" / "Dockerfile"
    text = dockerfile.read_text()
    anchor = "RUN swarm-repo-graph --self-test\n"
    assert anchor in text
    dockerfile.write_text(text.replace(anchor, anchor + "RUN probe-tool --planted-self-test\n", 1))
    _, proc, _ = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "agent-runtime-indexer", root=root)
    assert proc.returncode == 0, proc.stderr[-3000:]
    report = (root / "build" / "build-only" / "local-report.md").read_text()
    for step in ("swarm-repo-index --self-test", "swarm-repo-index --lsp-self-test",
                 "swarm-repo-graph --self-test", "probe-tool --planted-self-test"):
        assert step in report, report
    # The base's own checks run in the base's build, and are reported there.
    assert "agent_worker" in report and "RUN tools=" in report, report


def test_browser_report_carries_the_chromium_smoke_check(tmp_path):
    root, proc, _ = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "agent-runtime-browser")
    assert proc.returncode == 0, proc.stderr[-3000:]
    report = (root / "build" / "build-only" / "local-report.md").read_text()
    assert "RUN python - <<'PY'" in report and "agent-runtime-browser" in report, report


def test_a_checked_in_recipe_is_replayed_without_its_push(tmp_path):
    root, proc, events = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "swarm-verify")
    assert proc.returncode == 0, proc.stderr[-3000:]
    runs = [e["argv"] for e in events if e["argv"][:1] == ["run"]]
    assert len(runs) == 1, runs
    assemble = runs[0]
    assert "gcr.io/cloud-builders/docker" in assemble and _value(assemble, "--entrypoint") == ["bash"], assemble
    assert f"{root}:/workspace" in _value(assemble, "-v"), assemble
    assert any("verify-ctx" in a for a in assemble), "the step's own script runs"
    assert "/var/run/docker.sock" not in " ".join(assemble), "a replayed step gets no docker socket"
    (build,) = _builds(events)
    assert build[-1] == f"{root}/verify-ctx", build
    assert _value(build, "--output") == ["type=cacheonly"], build


def test_a_recipe_that_asks_for_a_secret_is_refused(tmp_path):
    root = _sandbox(tmp_path)
    recipe = root / "images" / "swarm-verify" / "cloudbuild.yaml"
    recipe.write_text(recipe.read_text() + "\navailableSecrets:\n  secretManager: []\n")
    _, proc, events = _local(tmp_path, "--build-only", "--local", "--tag", TAG, "swarm-verify", root=root)
    assert proc.returncode != 0
    assert "secret" in proc.stderr.lower(), proc.stderr
    assert not _builds(events) and not [e for e in events if e["argv"][:1] == ["run"]], events


@pytest.mark.parametrize("args", [["--local"], ["--local", "--affected-by", "-"], ["--build-only", "--local", "--async"]])
def test_local_is_only_a_build_only_mode(tmp_path, args):
    _, proc, events = _local(tmp_path, *args)
    assert proc.returncode != 0, proc.stderr
    assert "--local" in proc.stderr or "--build-only" in proc.stderr or "--affected-by" in proc.stderr, proc.stderr
    assert not _builds(events), events


# ---------------------------------------------------------------------------
# The workflow job.
# ---------------------------------------------------------------------------
def _job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][JOB]


def _step(job: dict, step_id: str) -> dict:
    hits = [s for s in job["steps"] if s.get("id") == step_id]
    assert len(hits) == 1, f"expected one step with id {step_id}"
    return hits[0]


def test_the_job_builds_with_buildx_and_pushes_logs_in_and_authenticates_nowhere():
    job = _job()
    text = json.dumps(job)
    # Pull requests only, and only those the `changes` job says reach this
    # workflow -- the gate that was application.yml's path filter.
    assert job.get("if") == "github.event_name == 'pull_request' && needs.changes.outputs.app == 'true'", job.get("if")
    assert job.get("needs") == "changes", job.get("needs")
    assert job.get("permissions") == {"contents": "read"}, job.get("permissions")
    assert "build-images.sh --build-only --local" in text, "the job builds on the runner"
    for banned in ("secrets.", "id-token", "google-github-actions/", "docker/login-action",
                   "docker login", "--push", "push: true", "GCP_PR_BUILD", "vars.GCP_",
                   "build-push-action", "CLOUDBUILD_"):
        assert banned not in text, f"{JOB} uses {banned!r}"
    checkouts = [s for s in job["steps"] if "actions/checkout" in str(s.get("uses", ""))]
    assert checkouts and all(s.get("with", {}).get("persist-credentials") is False for s in checkouts), checkouts


def test_the_workflow_restates_no_self_test():
    """The self-tests are read from the Dockerfiles by the script; the job
    names none of them."""
    runs = "\n".join(str(s.get("run", "")) for s in _job()["steps"])
    for literal in ("--self-test", "--lsp-self-test", "playwright", "chromium", "swarm-repo-"):
        assert literal not in runs, f"{JOB} restates {literal!r}"


def test_no_pull_request_identity_is_left_anywhere():
    for path in (WORKFLOW, CI_DOC, REPO / "scripts" / "build-images.sh"):
        assert "GCP_PR_BUILD" not in path.read_text(), f"{path.name} still describes the pull-request identity"
    doc = CI_DOC.read_text()
    section = doc.split("## Images are built on a pull request, without pushing", 1)[1].split("\n## ", 1)[0]
    assert "no Google identity" in section and "shared project" in section, section[:2000]
    assert "docker buildx" in section, section[:2000]


def _run_step(step: dict, cwd: Path, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, dict, str]:
    out = tmp_path / "github_output"
    summary = tmp_path / "summary.md"
    temp = tmp_path / "runner_temp"
    temp.mkdir(exist_ok=True)
    out.write_text("")
    summary.write_text("")
    full = {
        **os.environ,
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "RUNNER_TEMP": str(temp),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "NO_COLOR": "1",
        **env,
    }
    proc = subprocess.run(["bash", "-c", step["run"]], cwd=cwd, env=full, capture_output=True,
                          text=True, timeout=120, check=False)
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    return proc, outputs, summary.read_text()


def test_a_fork_pull_request_skips_before_checking_anything_out(tmp_path):
    job = _job()
    scope = _step(job, "scope")
    assert "checkout" not in str(job["steps"][0].get("uses", "")), "the fork check comes before the checkout"
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    started = time.monotonic()
    proc, outputs, summary = _run_step(scope, empty, tmp_path, HEAD_REPO="someone/fork", REPOSITORY="owner/swarm")
    assert proc.returncode == 0, proc.stderr
    assert time.monotonic() - started < 10
    assert outputs.get("build") == "false", outputs
    assert "fork" in summary.lower(), summary
    # Every later step is gated on it.
    for step in job["steps"][1:]:
        assert "steps.scope.outputs.build" in str(step.get("if", "")) or "steps.plan.outputs.build" in str(step.get("if", "")), step
    # The control: the same repository proceeds.
    proc, outputs, _ = _run_step(scope, empty, tmp_path, HEAD_REPO="owner/swarm", REPOSITORY="owner/swarm")
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("build") == "true", outputs


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize(
    "change, expected",
    [
        ("docs/notes.md", ""),
        ("images/swarm-ui/nginx.conf", "swarm-ui"),
        ("images/agent-runtime-base/Dockerfile", "agent-runtime-base agent-runtime-browser agent-runtime-indexer"),
    ],
)
def test_the_plan_builds_only_when_an_image_input_changed(tmp_path, change, expected):
    root = _sandbox(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    path = root / change
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write("\n# changed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "head")
    head = _git(root, "rev-parse", "HEAD")
    proc, outputs, summary = _run_step(_step(_job(), "plan"), root, tmp_path, BASE_SHA=base, HEAD_SHA=head)
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("images", "") == expected, outputs
    assert outputs.get("build") == ("true" if expected else "false"), outputs
    if not expected:
        assert "no image input changed" in summary, summary


def test_ci_gate_fails_when_the_build_check_failed(tmp_path):
    """ci-gate needs every job of application.yml and judges their results,
    so a red image build holds the pull request."""
    job_id = JOB
    needs = yaml.safe_load(WORKFLOW.read_text())["jobs"]["ci-gate"]["needs"]
    assert job_id in needs, "the gate does not wait for the image build"
    results = {name: "success" for name in needs}
    results[job_id] = "failure"
    proc = judge(tmp_path, "ci-gate", results)
    assert proc.returncode != 0, proc.stdout
    assert f"{job_id} (failure)" in proc.stdout, proc.stdout
    # The control: the same results with the build green pass.
    results[job_id] = "success"
    proc = judge(tmp_path, "ci-gate", results)
    assert proc.returncode == 0, proc.stdout + proc.stderr
