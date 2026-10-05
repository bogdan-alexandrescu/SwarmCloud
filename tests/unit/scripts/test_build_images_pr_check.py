"""A pull request that changes an image's inputs builds that image, without
pushing it (#650).

THE DEFECT THIS PINS. application.yml's `build images` ran only on main. #641
changed images/agent-runtime-base/Dockerfile and the repo-index LSP package;
its pull request was green, and the first build of the image happened after
merge, where `swarm-repo-index --lsp-self-test` failed (Cloud Build 650b9ebb,
main 539b5b58 and f1c4b075). Main went red and releases were blocked.

Owner decision, 2026-10-05: a pull request that changes images/** or a file an
image copies in builds the affected images in Cloud Build WITHOUT pushing, so
their build-time self-tests run before merge. The properties asserted here,
each against the REAL scripts and workflow:

  * which images a set of changed paths reaches is DERIVED from the
    Dockerfiles' COPY sources and each recipe's directory -- planting a COPY
    in a sandboxed Dockerfile changes the answer, so nothing restates the list
    -- and a COPY form the reader cannot parse is refused, never skipped;
  * a COPY'd file outside images/ (Makefile, scripts/, apps/...) reaches the
    images that copy it, and an image built FROM another is reached through it;
  * `build-images.sh --build-only` submits builds whose configs name no
    `images:` to push and no registry at all, touches no registry itself,
    writes no manifest, and builds agent-runtime-browser on a base built in
    the same Cloud Build instead of pulling one from the registry;
  * every input path the mapping knows fires application.yml on a pull
    request, so ci-gate expects the run that carries the build;
  * the workflow job runs both modes on a pull request and authenticates as
    something other than the deployer.

WHAT THIS CANNOT PROVE: that Cloud Build accepts the generated configs, that
BuildKit resolves the locally built base without a pull, or that the
pull-request identity exists. Only a pull request run after the owner creates
that identity shows those (docs/ci.md, "Images are built on a pull request,
without pushing").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .test_ci_gate import gate

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "build-images.sh"
WORKFLOW = REPO / ".github" / "workflows" / "application.yml"
TAG = "feed00c0ffee"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="build-images.sh needs bash and jq",
)

PYTHON_SERVICES = {"swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"}
WORKER = {"agent-runtime-base", "agent-runtime-browser", "agent-runtime-indexer"}
ALL = PYTHON_SERVICES | WORKER | {"swarm-ui", "swarm-verify"}


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        **extra,
    }
    env.pop("GITHUB_ACTIONS", None)
    return env


def _sandbox(tmp_path: Path) -> Path:
    """A copy of the scripts, the recipes and the context-filter files, so a
    test can change a Dockerfile and nothing is written into the checkout."""
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    shutil.copytree(REPO / "images", root / "images")
    for name in (".gitignore", ".gcloudignore", ".dockerignore"):
        if (REPO / name).exists():
            shutil.copy2(REPO / name, root / name)
    return root


def _affected(tmp_path: Path, changed: list[str], root: Path | None = None):
    script = (root or REPO) / "scripts" / "build-images.sh"
    return subprocess.run(
        ["bash", str(script), "--affected-by", "-"],
        input="".join(f"{p}\n" for p in changed),
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# ---------------------------------------------------------------------------
# Changed paths -> images.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "changed, expected",
    [
        # #641's shape: the base's Dockerfile reaches the base and both images
        # built FROM it; the repo-index package lives beside the indexer since
        # #625, so it reaches the indexer alone.
        (["images/agent-runtime-base/Dockerfile"], WORKER),
        (["images/agent-runtime-indexer/repo-index/lsp/servers.py"], {"agent-runtime-indexer"}),
        (["images/agent-runtime-indexer/Dockerfile"], {"agent-runtime-indexer"}),
        (["images/agent-runtime-indexer/cloudbuild.yaml"], {"agent-runtime-indexer"}),
        (["images/agent-runtime-browser/Dockerfile"], {"agent-runtime-browser"}),
        (["images/agent-runtime-browser/cloudbuild.yaml"], {"agent-runtime-browser"}),
        (["images/swarm-ui/nginx.conf"], {"swarm-ui"}),
        # COPY'd files outside images/.
        (["apps/common/swarm_common/models.py"], PYTHON_SERVICES | WORKER),
        (["apps/redaction/redaction/scan.py"], WORKER | {"swarm-api"}),
        (["apps/agent-worker/worker/main.py"], WORKER),
        (["apps/quota-broker/quota_broker/app.py"], {"swarm-quota-broker", "swarm-scheduler"}),
        (["apps/swarm-ui/src/App.tsx"], {"swarm-ui"}),
        # `COPY apps/swarm-ui/package-lock.json*`: a glob source.
        (["apps/swarm-ui/package-lock.json"], {"swarm-ui"}),
        # #671: the four service images export their requirements from the
        # lockfile, so a single-file COPY at the repo root is an input.
        (["uv.lock"], PYTHON_SERVICES),
        (["pyproject.toml"], PYTHON_SERVICES),
        (["Makefile"], {"swarm-verify"}),
        (["scripts/lib/common.sh"], {"swarm-verify"}),
        (["tests/acceptance/fixtures/claude-code/calc.py"], {"swarm-verify"}),
        # What every build's context is filtered through.
        ([".dockerignore"], ALL),
        ([".gcloudignore"], ALL),
        # Nothing copies these in.
        (["docs/ci.md"], set()),
        (["README.md"], set()),
        (["tests/unit/scripts/test_ci_gate.py"], set()),
        (["terraform/infra/main.tf"], set()),
        (["apps/swarm-mcp/swarm_mcp/server.py"], set()),
        (["Makefile.bak"], set()),
        ([], set()),
        # A union, not the first hit.
        (["apps/swarm-ui/src/App.tsx", "Makefile", "docs/ci.md"], {"swarm-ui", "swarm-verify"}),
    ],
)
def test_changed_paths_reach_exactly_the_images_that_take_them_in(tmp_path, changed, expected):
    proc = _affected(tmp_path, changed)
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == expected, proc.stderr


def test_the_mapping_is_read_from_the_dockerfiles_not_restated(tmp_path):
    """A COPY added to a Dockerfile changes the answer with nothing else edited."""
    root = _sandbox(tmp_path)
    # A root file no image on main copies (uv.lock and pyproject.toml are
    # inputs of the four service images since #671, so they cannot be the probe).
    before = _affected(tmp_path, ["probe-input.txt"], root)
    assert before.returncode == 0, before.stderr
    assert before.stdout.split() == [], before.stdout

    dockerfile = root / "images" / "swarm-api" / "Dockerfile"
    text = dockerfile.read_text()
    anchor = "COPY apps/common/ /src/apps/common/\n"
    assert anchor in text
    dockerfile.write_text(
        text.replace(anchor, anchor + "COPY probe-input.txt \\\n     probe-second.txt /src/\n", 1)
    )
    after = _affected(tmp_path, ["probe-input.txt"], root)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["swarm-api"], after.stdout
    # The continuation line's source is read too.
    cont = _affected(tmp_path, ["probe-second.txt"], root)
    assert cont.stdout.split() == ["swarm-api"], cont.stdout


@pytest.mark.parametrize(
    "line",
    [
        'COPY ["pyproject.toml", "/src/"]',
        "COPY <<EOF /src/x\nhello\nEOF",
    ],
)
def test_a_copy_the_reader_cannot_parse_is_refused_not_skipped(tmp_path, line):
    """A source the reader cannot see is an image a pull request would not
    build. Skipping it is the green-but-broken check #650 is about."""
    root = _sandbox(tmp_path)
    dockerfile = root / "images" / "swarm-api" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\n" + line + "\n")
    proc = _affected(tmp_path, ["docs/ci.md"], root)
    assert proc.returncode != 0, proc.stdout
    assert "images/swarm-api/Dockerfile" in proc.stderr, proc.stderr


def test_copy_from_a_stage_is_not_an_input(tmp_path):
    """`COPY --from=uvbin /uv ...` names a path in another stage, not the repo."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--inputs"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rows = [line.split("\t") for line in proc.stdout.splitlines() if line]
    assert rows and all(len(r) == 2 for r in rows), proc.stdout
    patterns = {p for _, p in rows}
    assert not any(p.startswith("/") for p in patterns), sorted(patterns)
    assert {t for t, _ in rows} == ALL


def test_every_image_input_fires_application_yml_on_a_pull_request(tmp_path):
    """The build job lives in application.yml, whose pull_request trigger is
    path-filtered. An input the filter misses starts no run, ci-gate expects
    none, and the image goes unbuilt with every check green."""
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--inputs"],
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    samples = sorted({
        # A concrete path each pattern matches: a glob star becomes a name.
        re.sub(r"\*+", "x", pattern)
        for _, pattern in (line.split("\t") for line in proc.stdout.splitlines() if line)
    })
    assert samples
    missed = []
    for sample in samples:
        listing = tmp_path / "changed.txt"
        listing.write_text(sample + "\n")
        got = gate(tmp_path, "expected", "--event", "pull_request", "--changed", str(listing))
        assert got.returncode == 0, got.stderr
        if "application.yml" not in got.stdout.split():
            missed.append(sample)
    assert not missed, f"application.yml does not run on a pull request changing {missed}"


# ---------------------------------------------------------------------------
# --build-only, against a fake gcloud.
# ---------------------------------------------------------------------------
FAKE_GCLOUD = r'''#!{python}
import json, os, re, sys, uuid
args = sys.argv[1:]
event = {"args": args}
if args[:2] == ["builds", "submit"]:
    config = args[args.index("--config") + 1]
    event["config"] = config
    with open(config) as fh:
        event["text"] = fh.read()
    if "--substitutions" in args:
        event["subs"] = args[args.index("--substitutions") + 1]
    target = re.search(r"cloudbuild-(.+)\.yaml$", config).group(1)
    event["target"] = target
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps(event) + "\n")
if args[:2] == ["builds", "submit"]:
    print("Created [https://cloudbuild.googleapis.com/v1/projects/p/locations/r/builds/"
          + str(uuid.uuid5(uuid.NAMESPACE_DNS, event["target"])) + "].", file=sys.stderr)
    if event["target"] in os.environ.get("FAKE_FAIL", "").split(","):
        print("ERROR: build step 0 failed: swarm-repo-index --lsp-self-test", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
print("fake gcloud: a build-only run has no business calling " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _build_only(tmp_path: Path, *args: str, fail: str = ""):
    root = _sandbox(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    events_file = tmp_path / "events.jsonl"
    env = _env(
        tmp_path,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        ALLOW_DIRTY_BUILD="1",
        FAKE_EVENTS=str(events_file),
        FAKE_FAIL=fail,
        BUILD_POLL_INTERVAL="0.1",
        BUILD_SUBMIT_STAGGER="0",
    )
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


def _top_level_keys(text: str) -> set[str]:
    return set((yaml.safe_load(text) or {}).keys())


def test_build_only_builds_and_pushes_nothing(tmp_path):
    root, proc, events = _build_only(
        tmp_path, "--build-only", "--tag", TAG,
        "swarm-api", "swarm-verify", "agent-runtime-base", "agent-runtime-browser",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]

    others = [e["args"] for e in events if e["args"][:2] != ["builds", "submit"]]
    assert not others, f"a build-only run called gcloud for more than a build: {others}"

    submits = {e["target"]: e for e in events}
    # The base is built INSIDE the browser's build, so it is not submitted twice.
    assert set(submits) == {"swarm-api", "swarm-verify", "agent-runtime-browser"}, sorted(submits)

    registry = "docker.pkg.dev"
    for target, event in submits.items():
        text = event["text"]
        assert "images" not in _top_level_keys(text), f"{target}'s config pushes: {text}"
        assert "artifacts" not in _top_level_keys(text), f"{target}'s config uploads: {text}"
        assert not re.search(r"\bdocker\s+(push|pull)\b|-\s+push\b|-\s+pull\b", text), (
            f"{target}'s config talks to a registry: {text}"
        )
        assert registry not in text and registry not in event.get("subs", ""), (
            f"{target}'s config names the registry: {text}\n{event.get('subs')}"
        )

    # The checked-in swarm-verify recipe still assembles its own context.
    assert "assemble-context" in submits["swarm-verify"]["text"]

    # The browser is built FROM a base built in the same Cloud Build.
    browser = yaml.safe_load(submits["agent-runtime-browser"]["text"])
    steps = [json.dumps(step) for step in browser["steps"]]
    base_at = next(i for i, s in enumerate(steps) if "images/agent-runtime-base/Dockerfile" in s)
    browser_at = next(i for i, s in enumerate(steps) if "images/agent-runtime-browser/Dockerfile" in s)
    assert base_at < browser_at, steps
    local_base = re.search(r'"-t", "([^"]+)"', steps[base_at]).group(1)
    assert f"BASE_IMAGE={local_base}" in steps[browser_at], steps[browser_at]

    assert not (root / "build" / "images-dev.json").exists(), "a build-only run wrote a manifest"
    assert "pushed nothing" in proc.stderr, proc.stderr[-1500:]


def test_build_only_names_the_image_whose_build_failed(tmp_path):
    _, proc, _ = _build_only(
        tmp_path, "--build-only", "--tag", TAG, "agent-runtime-browser", "swarm-ui",
        fail="agent-runtime-browser",
    )
    assert proc.returncode != 0
    last = [line for line in proc.stderr.splitlines() if line.strip()][-1]
    assert "agent-runtime-browser" in last and "swarm-ui" not in last, last


@pytest.mark.parametrize(
    "extra",
    [["--reuse-ci", "only"], ["--digests-only"], ["--async"], ["--create-repo"]],
)
def test_build_only_refuses_the_flags_that_read_or_write_a_registry(tmp_path, extra):
    _, proc, events = _build_only(tmp_path, "--build-only", *extra)
    assert proc.returncode != 0, proc.stderr
    assert "--build-only" in proc.stderr, proc.stderr
    assert not events, events


def test_affected_by_needs_no_gcloud_and_takes_no_targets(tmp_path):
    root = _sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), "--affected-by", "-", "swarm-api"],
        input="Makefile\n",
        env=_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode != 0
    assert "--affected-by" in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# The workflow.
# ---------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _pr_job() -> tuple[str, dict]:
    hits = [
        (job_id, job)
        for job_id, job in _jobs().items()
        if any("--build-only" in str(s.get("run", "")) for s in job.get("steps") or [])
    ]
    assert len(hits) == 1, f"expected one job running build-images.sh --build-only, found {hits}"
    return hits[0]


def test_a_pull_request_job_maps_the_change_and_builds_without_pushing():
    job_id, job = _pr_job()
    assert "pull_request" in str(job.get("if", "")), job.get("if")
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "build-images.sh --affected-by" in runs, runs
    # Three dots: what the pull request merges, not what main gained since.
    assert "...${HEAD_SHA}" in runs, runs
    for step in job["steps"]:
        assert "upload-artifact" not in str(step.get("uses", "")), (
            f"{job_id} records a build no release may promote"
        )
        assert "GCP_DEPLOY_SA" not in json.dumps(step), (
            f"{job_id} authenticates as the deployer on a pull request: {step}"
        )
    # The build and its identity only when something was mapped.
    gated = [s for s in job["steps"] if "--build-only" in str(s.get("run", ""))
             or str(s.get("uses", "")).startswith("google-github-actions/")]
    assert gated and all("steps." in str(s.get("if", "")) for s in gated), gated


def test_mains_build_and_push_is_unchanged():
    build = _jobs()["build"]
    assert build["if"] == "github.event_name != 'pull_request'"
    runs = [str(s.get("run", "")).strip() for s in build["steps"] if s.get("run")]
    assert "./scripts/build-images.sh" in runs, runs
    assert "build-not-run" not in _jobs(), "the 'not built' notice outlived the pull-request build"
