"""The build mirrors exist and the refresh script fills them (OPS-MIRROR part A;
part of #1030).

THE FAILURE THIS PINS. On 2026-10-12 three release image builds failed on
fetches outside Google inside Cloud Build: twice a ghcr.io timeout pulling the
uv image, once a github.com 504 downloading shellcheck in agent-runtime-base.

Part A creates what the builds will read, and nothing reads it yet:

  * two Artifact Registry REMOTE repositories, terraform/infra/build_mirror.tf
    -- `swarm-mirror-ghcr` (upstream https://ghcr.io) and
    `swarm-mirror-dockerhub` (Docker Hub);
  * a GCS bucket, `swarm-build-mirror-<project>`, for the release tarballs at
    their upstream path, listed with their sha256 in
    images/agent-runtime-base/tool-mirror.sha256;
  * scripts/refresh-build-mirror.sh, which the operator runs to fill the
    bucket from upstream, refusing anything whose sha256 does not match.

The tests that the Dockerfiles, scripts/build-images.sh and Cloud Build read
the mirrors are part B's (test_build_mirror_switch.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from .test_build_images_concurrency import FAKE_GCLOUD, TAG, _sandbox


REPO = Path(__file__).resolve().parents[3]
DOCKERFILES = sorted([*REPO.glob("apps/*/Dockerfile"), *REPO.glob("images/*/Dockerfile")])
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
MANIFEST = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"
MIRROR_TF = REPO / "terraform" / "infra" / "build_mirror.tf"
COMMON = REPO / "scripts" / "lib" / "common.sh"
LIB = REPO / "scripts" / "lib" / "build-mirror.sh"
REFRESH = REPO / "scripts" / "refresh-build-mirror.sh"
RUNBOOK = REPO / "docs" / "runbooks" / "build-mirror.md"
RUNBOOK_INDEX = REPO / "docs" / "runbooks" / "README.md"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"


def _tfvar(name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', DEV_TFVARS.read_text(), flags=re.M)
    assert m, f"{name} is not set in {DEV_TFVARS.relative_to(REPO)}"
    return m.group(1)


PROJECT = _tfvar("project_id")
REGION = _tfvar("region")
MIRROR_HOST = f"{REGION}-docker.pkg.dev/{PROJECT}"
#: The build argument each Dockerfile names a mirror by -> the upstream it
#: stands in for. A pull request's local build passes the upstream instead.
MIRRORS = {
    "MIRROR_DOCKER_HUB": (f"{MIRROR_HOST}/swarm-mirror-dockerhub", "docker.io"),
    "MIRROR_GHCR": (f"{MIRROR_HOST}/swarm-mirror-ghcr", "ghcr.io"),
}
#: Hosts the tool mirror's objects come from. An object's name IS its upstream
#: URL without `https://`, so the refresh needs no second list.
TOOL_HOSTS = ("github.com", "releases.hashicorp.com")
#: The GitHub-hosted tools the base image fetched on 2026-10-12.
GITHUB_TOOLS = (
    "cli/cli", "opentofu/opentofu", "terraform-linters/tflint",
    "koalaman/shellcheck", "aquasecurity/trivy",
)

FORBIDDEN = re.compile(
    r"ghcr\.io|docker\.io|https?://(?:[a-z0-9-]+\.)*github(?:usercontent)?\.com", re.I
)


# --------------------------------------------------------------------------- #
# Reading a Dockerfile.
# --------------------------------------------------------------------------- #


def _code_lines(text: str) -> list[str]:
    """Logical instructions, continuations joined, comment lines dropped
    (a comment inside a continued RUN is dropped by Docker too)."""
    out: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#") or (not s and not cur):
            continue
        cur.append(s.rstrip("\\").rstrip())
        if not s.endswith("\\"):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _global_args(text: str) -> dict[str, str]:
    """`ARG NAME=default` before the first FROM: what a FROM may expand."""
    args: dict[str, str] = {}
    for line in _code_lines(text):
        if re.match(r"FROM\s", line, re.I):
            break
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if m:
            args[m.group(1)] = _render(m.group(2), args)
    return args


def _all_args(text: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for line in _code_lines(text):
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if m:
            args[m.group(1)] = _render(m.group(2), args)
    return args


def _render(value: str, args: dict[str, str]) -> str:
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: args.get(m.group(1), m.group(0)), value)


def _syntax(text: str) -> str | None:
    first = text.splitlines()[0] if text else ""
    m = re.match(r"#\s*syntax\s*=\s*(\S+)", first)
    return m.group(1) if m else None


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _manifest_rows(path: Path = MANIFEST) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        sha, obj = line.split(None, 1)
        rows.append((sha, obj.strip()))
    return rows




def test_the_mirrors_are_the_ones_terraform_declares() -> None:
    tf = MIRROR_TF.read_text()
    repos = re.findall(
        r'resource\s+"google_artifact_registry_repository"\s+"[a-z_]+"\s*\{(.*?)\n\}', tf, flags=re.S
    )
    assert len(repos) == 2, f"expected two remote repositories in {_rel(MIRROR_TF)}, found {len(repos)}"
    by_id = {}
    for body in repos:
        rid = re.search(r'repository_id\s*=\s*"\$\{var\.name_prefix\}-([a-z-]+)"', body)
        assert rid, body[:200]
        by_id[rid.group(1)] = body
        assert re.search(r'mode\s*=\s*"REMOTE_REPOSITORY"', body)
        assert re.search(r'format\s*=\s*"DOCKER"', body)
        assert re.search(r"labels\s*=\s*local\.labels", body), "every resource carries managed-by"
    assert set(by_id) == {"mirror-ghcr", "mirror-dockerhub"}, sorted(by_id)
    assert re.search(r'uri\s*=\s*"https://ghcr\.io"', by_id["mirror-ghcr"])
    assert re.search(r'public_repository\s*=\s*"DOCKER_HUB"', by_id["mirror-dockerhub"])
    # The build names the repositories terraform creates under the default
    # prefix, in the project it builds for (design section 7): the Dockerfiles
    # name none, and scripts/lib/build-mirror.sh derives each from
    # REGION and PROJECT_ID, so the live project's build reads the live mirror.
    prefix = re.search(
        r'variable\s+"name_prefix"\s*\{.*?default\s*=\s*"([^"]+)"', (REPO / "terraform/infra/variables.tf").read_text(), re.S
    ).group(1)
    for name, (ref, _) in MIRRORS.items():
        assert ref.split("/")[-1].startswith(prefix + "-mirror-"), (name, ref)
        assert _mirror_repository(name, PROJECT, REGION) == ref, name


def _mirror_repository(name: str, project: str, region: str) -> str:
    """What scripts/lib/build-mirror.sh says the build passes as NAME."""
    done = subprocess.run(
        ["bash", "-c", 'source scripts/lib/common.sh && source scripts/lib/build-mirror.sh && mirror_repository "$1"', "_", name],
        cwd=REPO, capture_output=True, text=True, check=False, timeout=60,
        env={**os.environ, "PROJECT_ID": project, "REGION": region, "SWARM_ENV_FILE": "/nonexistent/.env", "NO_COLOR": "1"},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


#: The live project's id, built at runtime so that this file names it in no
#: literal the guard below would have to except.
_LIVE_PROJECT = "-".join(("saga", "agents", "staging"))


def test_no_dockerfile_names_a_project() -> None:
    """DAYONE-DESAGA-1 (design section 7): a Dockerfile that names the live
    project's mirror builds only where that project's registry is readable.
    No tracked Dockerfile names the project or a mirror repository, and every
    `ARG MIRROR_*` default is empty: scripts/build-images.sh passes the mirror
    of the project it builds for."""
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*Dockerfile*"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert sorted(tracked) == sorted(_rel(p) for p in DOCKERFILES), tracked
    assert len(tracked) == 16, tracked
    hits = []
    for rel in tracked:
        text = (REPO / rel).read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if _LIVE_PROJECT in line or "swarm-mirror-" in line:
                hits.append(f"{rel}:{n}: {line[:120]}")
        for m in re.finditer(r"^[ \t]*ARG[ \t]+(MIRROR_[A-Za-z0-9_]*)(=[^\n]*)?$", text, re.M):
            if m.group(2) not in (None, "="):
                hits.append(f"{rel}: ARG {m.group(1)} defaults to {m.group(2)[1:]}")
    assert not hits, "\n".join(hits)


def _generated_config(tmp_path: Path, project: str, target: str) -> dict:
    root = _sandbox(tmp_path)
    bindir = tmp_path / "bin"
    _write_tool(bindir, "gcloud", FAKE_GCLOUD)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), target, "--tag", TAG],
        env=_env(tmp_path, bindir, PROJECT_ID=project), capture_output=True, text=True, timeout=180, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return yaml.safe_load((root / "build" / f"cloudbuild-{target}.yaml").read_text())


def _docker_build_args(config: dict) -> dict[str, list[str]]:
    builds = [s for s in config["steps"] if (s.get("args") or [""])[0] == "build"]
    assert len(builds) == 1, config["steps"]
    argv = builds[0]["args"]
    out: dict[str, list[str]] = {}
    for i, a in enumerate(argv[:-1]):
        if a == "--build-arg":
            k, v = argv[i + 1].split("=", 1)
            out.setdefault(k, []).append(v)
    return out


@pytest.mark.parametrize("project", [PROJECT, "swarm-test-project"])
def test_the_build_passes_the_mirror_of_the_project_it_builds_for(tmp_path: Path, project: str) -> None:
    """The live project's build is unchanged -- every pull still goes through
    its own mirror, the `# syntax=` frontend included (BUILDKIT_SYNTAX) -- and
    any other project's build reads that project's mirror."""
    dockerfile = REPO / "images" / "swarm-api" / "Dockerfile"
    args = _docker_build_args(_generated_config(tmp_path, project, "swarm-api"))
    host = f"{REGION}-docker.pkg.dev/{project}"
    assert args.get("MIRROR_DOCKER_HUB") == [f"{host}/swarm-mirror-dockerhub"], args
    assert args.get("MIRROR_GHCR") == [f"{host}/swarm-mirror-ghcr"], args
    syntax = _syntax(dockerfile.read_text())
    assert syntax and re.fullmatch(r"docker/dockerfile:[0-9.]+@sha256:[0-9a-f]{64}", syntax), syntax
    assert args.get("BUILDKIT_SYNTAX") == [f"{host}/swarm-mirror-dockerhub/{syntax}"], args


def test_the_build_passes_only_the_mirrors_a_dockerfile_declares(tmp_path: Path) -> None:
    # swarm-ui pulls only from Docker Hub: an argument it does not declare
    # would be a warning in every build log.
    args = _docker_build_args(_generated_config(tmp_path, PROJECT, "swarm-ui"))
    assert "MIRROR_GHCR" not in args, args
    assert args.get("MIRROR_DOCKER_HUB") == [MIRRORS["MIRROR_DOCKER_HUB"][0]], args


RECIPES = sorted(REPO.glob("images/*/cloudbuild.yaml"))


def _declared_mirrors(dockerfile: Path) -> set[str]:
    text = dockerfile.read_text()
    return {n for n in MIRRORS if re.search(rf"^[ \t]*ARG[ \t]+{n}(=|[ \t]*$)", text, re.M)}


@pytest.mark.parametrize("recipe", RECIPES, ids=_rel)
def test_every_checked_in_recipe_passes_the_mirrors_it_is_given(recipe: Path) -> None:
    """A checked-in recipe is submitted as it is, so it cannot be handed build
    arguments: build-images.sh gives it the project's mirrors as substitutions
    (recipe_mirror_subs), and its `docker build` must pass each one the
    Dockerfile declares, and the frontend as BUILDKIT_SYNTAX. Without them its
    FROM lines would expand the empty defaults (DAYONE-DESAGA-1)."""
    dockerfile = recipe.parent / "Dockerfile"
    assert dockerfile.is_file(), f"{_rel(recipe)} has no Dockerfile beside it"
    text = recipe.read_text()
    declared = _declared_mirrors(dockerfile)
    for name in MIRRORS:
        passed = f"{name}=${{_{name}}}" in text
        assert passed == (name in declared), (
            f"{_rel(recipe)}: {name} {'is not passed' if not passed else 'is passed, but its Dockerfile does not declare it'}"
        )
    assert "BUILDKIT_SYNTAX=${_BUILDKIT_SYNTAX}" in text, f"{_rel(recipe)} does not pass BUILDKIT_SYNTAX"


def test_a_checked_in_recipe_is_given_its_project_s_mirrors(tmp_path: Path) -> None:
    """The substitutions build-images.sh hands a checked-in recipe name the
    mirror of the project it builds for, the frontend included."""
    from .test_build_images_pr_check import _build_only

    _, proc, events = _build_only(tmp_path, "--build-only", "--tag", TAG, "workspace-apply")
    assert proc.returncode == 0, proc.stderr[-3000:]
    submits = [e for e in events if e["args"][:2] == ["builds", "submit"]]
    assert len(submits) == 1, submits
    subs = dict(kv.split("=", 1) for kv in submits[0]["subs"].split(","))
    host = "us-central1-docker.pkg.dev/swarm-test-project"
    assert subs.get("_MIRROR_DOCKER_HUB") == f"{host}/swarm-mirror-dockerhub", subs
    assert subs.get("_MIRROR_GHCR") == f"{host}/swarm-mirror-ghcr", subs
    syntax = _syntax((REPO / "images" / "workspace-apply" / "Dockerfile").read_text())
    assert subs.get("_BUILDKIT_SYNTAX") == f"{host}/swarm-mirror-dockerhub/{syntax}", subs


def test_the_refresh_check_reads_the_project_s_own_mirror(tmp_path: Path) -> None:
    """The Dockerfiles name no mirror, so the refresh expands their pulls with
    the mirror of PROJECT_ID; were it still reading ARG defaults, it would
    find nothing to check (or the live project's mirror)."""
    _, _, proc, calls, _ = _refresh(tmp_path, "--check", "--images-only")
    assert proc.returncode == 0, proc.stderr[-2000:]
    described = sorted({c[4] for c in calls if c[:4] == ["artifacts", "docker", "images", "describe"]})
    hub = "us-central1-docker.pkg.dev/swarm-test-project/swarm-mirror-dockerhub"
    ghcr = "us-central1-docker.pkg.dev/swarm-test-project/swarm-mirror-ghcr"
    assert any(d.startswith(f"{hub}/docker/dockerfile@sha256:") for d in described), described
    assert any(d.startswith(f"{hub}/library/python@sha256:") for d in described), described
    assert any(d.startswith(f"{ghcr}/astral-sh/uv@sha256:") for d in described), described
    assert all(d.startswith((hub + "/", ghcr + "/")) for d in described), described


def test_the_tool_bucket_is_terraform_managed_and_grants_nothing() -> None:
    tf = MIRROR_TF.read_text()
    bucket = re.search(r'resource\s+"google_storage_bucket"\s+"build_mirror"\s*\{(.*?)\n\}', tf, flags=re.S)
    assert bucket, f"{_rel(MIRROR_TF)} declares no google_storage_bucket.build_mirror"
    body = bucket.group(1)
    assert re.search(r'name\s*=\s*"\$\{var\.name_prefix\}-build-mirror-\$\{var\.project_id\}"', body)
    assert re.search(r'public_access_prevention\s*=\s*"enforced"', body)
    assert re.search(r"uniform_bucket_level_access\s*=\s*true", body)
    assert "labels" in body and "local.labels" in body
    # The build runs as the project's default compute account, which already
    # reads both (roles/editor, docs/ci.md); no grant is added here.
    assert not re.search(r'resource\s+"google_[a-z_]*iam_[a-z_]+"', tf), "no IAM in the mirror file"
    # common.sh names the bucket the way terraform does.
    assert 'BUILD_MIRROR_BUCKET="${BUILD_MIRROR_BUCKET:-swarm-build-mirror-${PROJECT_ID}}"' in COMMON.read_text()




def _fake_content(obj: str) -> bytes:
    return f"fake https://{obj}\n".encode()


def _fake_manifest(root: Path) -> list[tuple[str, str]]:
    """Rewrite the sandbox's manifest with the sha256 of each object's fake
    content, so fakes can serve a file that verifies."""
    rows = [(hashlib.sha256(_fake_content(obj)).hexdigest(), obj) for _, obj in _manifest_rows()]
    (root / MANIFEST.relative_to(REPO)).write_text("".join(f"{s}  {o}\n" for s, o in rows))
    return rows


def _env(tmp_path: Path, bindir: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        "ALLOW_DIRTY_BUILD": "1",
        "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
        "FAKE_BUILD_SECONDS": "0.05",
        "BUILD_PARALLELISM": "2",
        "BUILD_POLL_INTERVAL": "0.1",
        "BUILD_SUBMIT_STAGGER": "0",
        "BUILD_STATUS_INTERVAL": "0",
        "SWARM_PYTHON": sys.executable,
        **extra,
    }
    env.pop("GITHUB_ACTIONS", None)
    return env


def _write_tool(bindir: Path, name: str, body: str) -> None:
    bindir.mkdir(exist_ok=True)
    path = bindir / name
    path.write_text(body.replace("{python}", sys.executable))
    path.chmod(0o755)


BUCKET = "swarm-build-mirror-swarm-test-project"


FAKE_CURL = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else None
url = [a for a in args if a.startswith("http")][-1]
with open(os.environ["FAKE_CURL_LOG"], "a") as fh:
    fh.write(json.dumps({"url": url}) + "\n")
body = ("fake " + url + "\n").encode()
if url in os.environ.get("FAKE_CURL_TAMPER", "").split(","):
    body = b"tampered\n"
if out:
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(body)
else:
    sys.stdout.buffer.write(body)
'''


# gcloud for the refresh: `storage objects describe` answers from a directory,
# `storage cp LOCAL gs://...` uploads into it, and every call is logged.
REFRESH_GCLOUD = r'''#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GCLOUD_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
root = os.environ["FAKE_BUCKET_DIR"]
if args[:3] == ["storage", "objects", "describe"]:
    path = os.path.join(root, args[3][len("gs://"):])
    if os.path.isfile(path):
        print("name: x")
        sys.exit(0)
    print("ERROR: (gcloud.storage.objects.describe) gs://x not found: 404.", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["storage", "cp"]:
    src, dst = [a for a in args[2:] if not a.startswith("-")][:2]
    path = os.path.join(root, dst[len("gs://"):])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(src, path)
    sys.exit(0)
if args[:4] == ["artifacts", "docker", "images", "describe"]:
    if args[4] in os.environ.get("FAKE_MISSING_IMAGES", "").split(","):
        print("ERROR: NOT_FOUND", file=sys.stderr)
        sys.exit(1)
    print("image_summary: {}")
    sys.exit(0)
print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _refresh(tmp_path: Path, *argv: str, present: tuple[str, ...] = (), tamper: str = "", missing_images: str = ""):
    root = _sandbox(tmp_path)
    rows = _fake_manifest(root)
    bucket = tmp_path / "bucket"
    for obj in present:
        p = bucket / BUCKET / obj
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_fake_content(obj))
    bindir = tmp_path / "bin"
    _write_tool(bindir, "gcloud", REFRESH_GCLOUD)
    _write_tool(bindir, "curl", FAKE_CURL)
    glog, clog = tmp_path / "gcloud.jsonl", tmp_path / "curl.jsonl"
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "refresh-build-mirror.sh"), *argv],
        env=_env(tmp_path, bindir, FAKE_GCLOUD_LOG=str(glog), FAKE_CURL_LOG=str(clog),
                 FAKE_BUCKET_DIR=str(bucket), FAKE_CURL_TAMPER=tamper, FAKE_MISSING_IMAGES=missing_images),
        capture_output=True, text=True, timeout=120, check=False,
    )
    calls = [json.loads(x) for x in glog.read_text().splitlines()] if glog.exists() else []
    urls = [json.loads(x)["url"] for x in clog.read_text().splitlines()] if clog.exists() else []
    return rows, bucket, proc, calls, urls


def test_the_refresh_script_is_a_house_script() -> None:
    assert os.access(REFRESH, os.X_OK), f"{_rel(REFRESH)} is not executable"
    lines = [ln for ln in REFRESH.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "set -euo pipefail"
    assert "lib/common.sh" in REFRESH.read_text()


def test_the_refresh_uploads_every_missing_object_after_checking_its_sha256(tmp_path: Path) -> None:
    rows, bucket, proc, calls, urls = _refresh(tmp_path, "--tools-only", present=(_manifest_rows()[0][1],))
    assert proc.returncode == 0, proc.stderr[-3000:]
    # What was already there is not fetched again.
    assert sorted(urls) == sorted(f"https://{obj}" for _, obj in rows[1:])
    for sha, obj in rows:
        assert hashlib.sha256((bucket / BUCKET / obj).read_bytes()).hexdigest() == sha, obj


def test_the_refresh_refuses_an_upstream_file_with_the_wrong_sha256(tmp_path: Path) -> None:
    bad = _manifest_rows()[1][1]
    rows, bucket, proc, calls, _ = _refresh(tmp_path, "--tools-only", tamper=f"https://{bad}")
    assert proc.returncode != 0
    assert bad in proc.stderr, proc.stderr[-2000:]
    assert not (bucket / BUCKET / bad).exists(), "a file that failed its sha256 was uploaded"
    # Every other object still went up: one bad release does not stall the rest.
    for _, obj in rows:
        if obj != bad:
            assert (bucket / BUCKET / obj).exists(), obj


def test_the_refresh_check_names_every_missing_object_and_changes_nothing(tmp_path: Path) -> None:
    rows, bucket, proc, calls, urls = _refresh(
        tmp_path, "--check", "--tools-only", present=tuple(o for _, o in _manifest_rows()[1:])
    )
    assert proc.returncode != 0
    assert f"gs://{BUCKET}/{rows[0][1]}" in proc.stderr, proc.stderr[-2000:]
    assert urls == [], "--check fetched from upstream"
    assert not [c for c in calls if c[:2] == ["storage", "cp"]], "--check uploaded"



def test_the_runbook_is_indexed_and_names_the_script() -> None:
    assert RUNBOOK.exists()
    text = RUNBOOK.read_text()
    assert "scripts/refresh-build-mirror.sh" in text
    assert "build-mirror.md" in RUNBOOK_INDEX.read_text()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_new_scripts_are_shellcheck_clean() -> None:
    done = subprocess.run(
        ["shellcheck", "-x", str(REFRESH), str(LIB)], cwd=REPO / "scripts",
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr

