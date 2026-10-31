"""Image builds pull from the build mirrors, never from ghcr.io, Docker Hub or a
GitHub release download (OPS-MIRROR part B; part of #1030).

The mirrors themselves, their Terraform and the refresh script are part A
(test_build_mirror.py); this file holds what changes only when the Dockerfiles,
scripts/build-images.sh and Cloud Build start reading them:

  * every FROM, and every `# syntax=` frontend, is pulled through a mirror
    repository by the digest it was pinned at, and nothing fetches from
    ghcr.io, docker.io or github.com directly;
  * images/agent-runtime-base/tool-mirror.sha256 is exactly what the base image
    reads, at its pinned sha256;
  * a Cloud Build step stages the tool mirror before the build, and a missing
    or tampered object fails it, naming the object;
  * a pull request's build (`--build-only --local`, on a GitHub runner with no
    Google identity) reads the same files from upstream instead, verified
    against the same list;
  * the refresh's --check names every mirror image reference the Dockerfiles
    carry that is not cached.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_build_images_concurrency import FAKE_GCLOUD, TAG, _sandbox
from .test_build_mirror import (
    REPO,
    DOCKERFILES,
    BASE_DOCKERFILE,
    MANIFEST,
    TOOL_HOSTS,
    GITHUB_TOOLS,
    FORBIDDEN,
    MIRRORS,
    _code_lines,
    _global_args,
    _all_args,
    _render,
    _syntax,
    _rel,
    _manifest_rows,
    _fake_content,
    _fake_manifest,
    _env,
    _write_tool,
    BUCKET,
    FAKE_CURL,
    _refresh,
)


# --------------------------------------------------------------------------- #
# The guard: nothing a Dockerfile builds from is fetched from ghcr.io, Docker
# Hub or a GitHub release.
# --------------------------------------------------------------------------- #


def test_every_dockerfile_is_read() -> None:
    # 10 under images/ and apps/swarm-live: a glob that found none would make
    # every guard below pass over nothing.
    assert len(DOCKERFILES) >= 11, [_rel(p) for p in DOCKERFILES]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_rel)
def test_no_dockerfile_fetches_from_ghcr_docker_hub_or_github(dockerfile: Path) -> None:
    hits = [line[:160] for line in _code_lines(dockerfile.read_text()) if FORBIDDEN.search(line)]
    assert not hits, (
        f"{_rel(dockerfile)} still fetches from outside the mirrors "
        f"(docs/runbooks/build-mirror.md): {hits}"
    )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_rel)
def test_the_dockerfile_frontend_comes_from_the_mirror_by_digest(dockerfile: Path) -> None:
    # `# syntax=docker/dockerfile:1.7` is a Docker Hub pull BuildKit makes
    # before reading a single instruction. The line names no project
    # (DAYONE-DESAGA-1): the build passes the same reference through the
    # project's Docker Hub mirror as BUILDKIT_SYNTAX
    # (test_build_mirror.py::test_the_build_passes_the_mirror_of_the_project_it_builds_for).
    syntax = _syntax(dockerfile.read_text())
    if syntax is None:
        return
    assert syntax.startswith("docker/dockerfile:"), (
        f"{_rel(dockerfile)}'s # syntax= frontend is {syntax}, not docker/dockerfile"
    )
    assert re.search(r"@sha256:[0-9a-f]{64}$", syntax), f"{_rel(dockerfile)}: {syntax} has no digest"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=_rel)
def test_every_base_is_pulled_from_a_mirror_by_digest(dockerfile: Path) -> None:
    text = dockerfile.read_text()
    args = _global_args(text)
    # The defaults are empty (DAYONE-DESAGA-1,
    # test_build_mirror.py::test_no_dockerfile_names_a_project); the build
    # passes the mirror of the project it builds for, which for the live
    # project is MIRRORS.
    for name, (want, _) in MIRRORS.items():
        if re.search(rf"^[ \t]*ARG[ \t]+{name}(=|[ \t]*$)", text, re.M):
            args[name] = want
    stages: set[str] = set()
    froms = 0
    for line in _code_lines(text):
        # Upper case only: a heredoc's Python (`from playwright.sync_api
        # import ...`) is not an instruction, and every FROM here is upper case.
        m = re.match(r"FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
        if not m:
            continue
        froms += 1
        raw, stage = m.group(1), m.group(2)
        if stage:
            stages.add(stage.lower())
        if raw.lower() in stages or raw == "scratch":
            continue
        # Built in this repository and always passed by the recipe or the
        # build script (agent-runtime-base, by digest); never pulled from a
        # public registry.
        if raw == "${BASE_IMAGE}":
            continue
        ref = _render(raw, args)
        assert "${" not in ref, f"{_rel(dockerfile)}: FROM {raw} names an argument with no default"
        mirrored = any(ref.startswith(m + "/") for m, _ in MIRRORS.values())
        google = ref.startswith("gcr.io/google.com/cloudsdktool/")
        assert mirrored or google, (
            f"{_rel(dockerfile)}: FROM {raw} is {ref}, which is pulled from neither "
            f"mirror ({', '.join(m for m, _ in MIRRORS.values())})"
        )
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"{_rel(dockerfile)}: {ref} is not pinned by digest"
    assert froms, f"{_rel(dockerfile)} has no FROM"



# --------------------------------------------------------------------------- #
# The tool mirror's list is exactly what the base image reads, at its pins.
# --------------------------------------------------------------------------- #


#: The Dockerfiles that read the tool mirror. agent-runtime-browser joined the
#: base on 2026-10-14 for the pinned dev-browser binary (DEVB-INSTALL, part of
#: #1030), and agent-runtime-lightpanda on 2026-10-31 for the Lightpanda
#: binaries and the tag's source archive (LIGHTPANDA-IMAGE, part of #2688);
#: the manifest is still one file, listing what all of them read.
MIRROR_READERS = (
    BASE_DOCKERFILE,
    REPO / "images" / "agent-runtime-browser" / "Dockerfile",
    REPO / "images" / "agent-runtime-lightpanda" / "Dockerfile",
)


def test_the_mirror_readers_are_every_dockerfile_that_bind_mounts_it() -> None:
    mounting = {df for df in DOCKERFILES if "source=build/tool-mirror," in df.read_text()}
    assert mounting == set(MIRROR_READERS), sorted(_rel(df) for df in mounting)


def _dockerfile_tool_reads() -> set[tuple[str, str]]:
    """(sha256, object) for every file a mirror-reading Dockerfile reads from
    /tmp/tool-mirror, paired with the sha256 its RUN checks the copy with."""
    found: set[tuple[str, str]] = set()
    for dockerfile in MIRROR_READERS:
        found |= _tool_reads(dockerfile)
    return found


def _tool_reads(dockerfile: Path) -> set[tuple[str, str]]:
    text = dockerfile.read_text()
    args = _all_args(text)
    found: set[tuple[str, str]] = set()
    for line in _code_lines(text):
        if "/tmp/tool-mirror/" not in line:
            continue
        assert line.startswith("RUN --mount=type=bind,source=build/tool-mirror,target=/tmp/tool-mirror"), (
            f"a RUN reads /tmp/tool-mirror without bind-mounting build/tool-mirror: {line[:120]}"
        )
        for m in re.finditer(r'-o\s+(\S+)\s+"file:///tmp/tool-mirror/([^"]+)"', line):
            out, obj = m.group(1), _render(m.group(2), args)
            check = re.search(r'echo\s+"\$\{([A-Z0-9_]+)\}\s+' + re.escape(out) + r'"\s*\|\s*sha256sum -c', line)
            assert check, f"{out} (from {obj}) is never checked against its sha256"
            found.add((args[check.group(1)], obj))
    return found


def test_the_manifest_is_every_file_the_base_reads_at_its_pinned_sha256() -> None:
    reads = _dockerfile_tool_reads()
    rows = set(_manifest_rows())
    assert all(_tool_reads(df) for df in MIRROR_READERS), "a mirror reader reads nothing from the tool mirror"
    assert reads == rows, (
        f"{_rel(MANIFEST)} and {', '.join(_rel(df) for df in MIRROR_READERS)} disagree.\n"
        f"  read by the Dockerfile, not listed: {sorted(reads - rows)}\n"
        f"  listed, not read: {sorted(rows - reads)}"
    )
    for sha, obj in rows:
        assert re.fullmatch(r"[0-9a-f]{64}", sha), obj
        assert obj.split("/", 1)[0] in TOOL_HOSTS, obj
    for repo in GITHUB_TOOLS:
        assert any(obj.startswith(f"github.com/{repo}/releases/download/") for _, obj in rows), repo


# --------------------------------------------------------------------------- #
# Cloud Build: the step that stages the tool mirror, and what it does when an
# object is missing.
# --------------------------------------------------------------------------- #



def _cloud_build_config(tmp_path: Path, targets: list[str]) -> tuple[Path, list[tuple[str, str]], subprocess.CompletedProcess]:
    root = _sandbox(tmp_path)
    rows = _fake_manifest(root)
    bindir = tmp_path / "bin"
    _write_tool(bindir, "gcloud", FAKE_GCLOUD)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), *targets, "--tag", TAG],
        env=_env(tmp_path, bindir), capture_output=True, text=True, timeout=180, check=False,
    )
    return root, rows, proc


def _steps(config: Path) -> list[dict]:
    return yaml.safe_load(config.read_text())["steps"]


# A gcloud that serves `storage cp gs://<bucket>/<object> <dest>` from a
# directory, as the build's own gcloud serves it from the bucket.
STORAGE_GCLOUD = r'''#!{python}
import os, shutil, sys
args = sys.argv[1:]
if args[:2] == ["storage", "cp"]:
    src, dst = [a for a in args[2:] if not a.startswith("-")][:2]
    assert src.startswith("gs://"), src
    path = os.path.join(os.environ["FAKE_BUCKET_DIR"], src[len("gs://"):])
    if not os.path.isfile(path):
        print(f"ERROR: (gcloud.storage.cp) The following URLs matched no objects or files:\n-{src}", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(path, dst)
    sys.exit(0)
print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _run_step(tmp_path: Path, step: dict, bucket_dir: Path) -> subprocess.CompletedProcess:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    bindir = tmp_path / "stepbin"
    _write_tool(bindir, "gcloud", STORAGE_GCLOUD)
    assert step.get("entrypoint") == "bash", step
    return subprocess.run(
        ["bash", *step["args"]], cwd=workspace, capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_BUCKET_DIR": str(bucket_dir)},
    )


def _fill_bucket(bucket_dir: Path, rows: list[tuple[str, str]], skip: str = "", tamper: str = "") -> None:
    for _, obj in rows:
        if obj == skip:
            continue
        path = bucket_dir / BUCKET / obj
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"tampered\n" if obj == tamper else _fake_content(obj))


def test_the_base_images_cloud_build_stages_the_tool_mirror_before_it_builds(tmp_path: Path) -> None:
    root, rows, proc = _cloud_build_config(tmp_path, ["agent-runtime-base", "swarm-api"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    steps = _steps(root / "build" / "cloudbuild-agent-runtime-base.yaml")
    assert steps[0].get("id") == "tool-mirror", [s.get("id") for s in steps]
    assert any(a == "build" for a in steps[-1]["args"]), "the docker build comes after the staging step"
    script = "\n".join(steps[0]["args"])
    for _, obj in rows:
        assert f"gs://{BUCKET}/{obj}" in script, obj
    # An image that reads nothing from the mirror gets no staging step.
    api = _steps(root / "build" / "cloudbuild-swarm-api.yaml")
    assert all(s.get("id") != "tool-mirror" for s in api)

    bucket = tmp_path / "bucket"
    _fill_bucket(bucket, rows)
    done = _run_step(tmp_path, steps[0], bucket)
    assert done.returncode == 0, done.stderr
    for sha, obj in rows:
        staged = tmp_path / "workspace" / "build" / "tool-mirror" / obj
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == sha, obj


def _browser_mirror_step() -> dict:
    """The checked-in browser recipe's staging step, rendered the way Cloud
    Build renders it: `$$` is a dollar, `${PROJECT_ID}` the build's project."""
    config = yaml.safe_load((REPO / "images" / "agent-runtime-browser" / "cloudbuild.yaml").read_text())
    steps = config["steps"]
    assert steps[0].get("id") == "tool-mirror", [s.get("id") for s in steps]
    assert [s.get("id") for s in steps][-1] == "build", "the docker build comes after the staging step"
    step = dict(steps[0])
    project = BUCKET[len("swarm-build-mirror-"):]
    step["args"] = [a.replace("${PROJECT_ID}", project).replace("$$", "$") for a in step["args"]]
    return step


def _browser_workspace(tmp_path: Path, rows: list[tuple[str, str]]) -> None:
    manifest = tmp_path / "workspace" / MANIFEST.relative_to(REPO)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("".join(f"{sha}  {obj}\n" for sha, obj in rows))


def test_the_browser_recipe_stages_every_manifest_object_before_it_builds(tmp_path: Path) -> None:
    rows = [(hashlib.sha256(_fake_content(obj)).hexdigest(), obj) for _, obj in _manifest_rows()]
    assert any("SawyerHood/dev-browser" in obj for _, obj in rows)
    _browser_workspace(tmp_path, rows)
    bucket = tmp_path / "bucket"
    _fill_bucket(bucket, rows)
    done = _run_step(tmp_path, _browser_mirror_step(), bucket)
    assert done.returncode == 0, done.stderr
    for sha, obj in rows:
        staged = tmp_path / "workspace" / "build" / "tool-mirror" / obj
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == sha, obj


def test_the_browser_recipe_names_a_missing_or_tampered_object(tmp_path: Path) -> None:
    rows = [(hashlib.sha256(_fake_content(obj)).hexdigest(), obj) for _, obj in _manifest_rows()]
    _browser_workspace(tmp_path, rows)
    dev_browser = next(obj for _, obj in rows if "SawyerHood/dev-browser" in obj)
    _fill_bucket(tmp_path / "missing", rows, skip=dev_browser)
    done = _run_step(tmp_path, _browser_mirror_step(), tmp_path / "missing")
    assert done.returncode != 0
    assert f"gs://{BUCKET}/{dev_browser}" in done.stderr and "refresh-build-mirror.sh" in done.stderr
    _fill_bucket(tmp_path / "tampered", rows, tamper=dev_browser)
    done = _run_step(tmp_path, _browser_mirror_step(), tmp_path / "tampered")
    assert done.returncode != 0
    assert dev_browser in done.stdout + done.stderr


def test_a_missing_mirror_object_fails_the_build_and_names_it(tmp_path: Path) -> None:
    root, rows, proc = _cloud_build_config(tmp_path, ["agent-runtime-base"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    step = _steps(root / "build" / "cloudbuild-agent-runtime-base.yaml")[0]
    missing = rows[len(rows) // 2][1]
    bucket = tmp_path / "bucket"
    _fill_bucket(bucket, rows, skip=missing)
    done = _run_step(tmp_path, step, bucket)
    assert done.returncode != 0, "a build with a missing mirror object went on"
    assert f"gs://{BUCKET}/{missing}" in done.stderr, done.stderr
    assert "refresh-build-mirror.sh" in done.stderr, done.stderr


def test_a_mirror_object_with_the_wrong_sha256_fails_the_build(tmp_path: Path) -> None:
    root, rows, proc = _cloud_build_config(tmp_path, ["agent-runtime-base"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    step = _steps(root / "build" / "cloudbuild-agent-runtime-base.yaml")[0]
    bad = rows[0][1]
    bucket = tmp_path / "bucket"
    _fill_bucket(bucket, rows, tamper=bad)
    done = _run_step(tmp_path, step, bucket)
    assert done.returncode != 0
    assert bad in done.stdout + done.stderr


def test_a_build_only_cloud_build_stages_the_mirror_too(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    _fake_manifest(root)
    bindir = tmp_path / "bin"
    _write_tool(bindir, "gcloud", FAKE_GCLOUD)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), "--build-only",
         "agent-runtime-base", "agent-runtime-browser", "--tag", TAG],
        env=_env(tmp_path, bindir), capture_output=True, text=True, timeout=180, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    configs = sorted((root / "build" / "build-only").glob("cloudbuild-*.yaml"))
    assert configs, proc.stderr[-2000:]
    for config in configs:
        assert _steps(config)[0].get("id") == "tool-mirror", config.name



# --------------------------------------------------------------------------- #
# A pull request's local build: upstream, the same sha256, no mirror.
# --------------------------------------------------------------------------- #


FAKE_DOCKER = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:2] == ["buildx", "build"] and "--metadata-file" in args:
    with open(args[args.index("--metadata-file") + 1], "w") as fh:
        json.dump({"containerimage.digest": "sha256:" + "0" * 64}, fh)
sys.exit(0)
'''


def _local(tmp_path: Path, targets: list[str], tamper: str = ""):
    root = _sandbox(tmp_path)
    rows = _fake_manifest(root)
    bindir = tmp_path / "bin"
    _write_tool(bindir, "curl", FAKE_CURL)
    _write_tool(bindir, "docker", FAKE_DOCKER)
    curl_log, docker_log = tmp_path / "curl.jsonl", tmp_path / "docker.jsonl"
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), "--build-only", "--local", *targets, "--tag", TAG],
        env=_env(tmp_path, bindir, FAKE_CURL_LOG=str(curl_log), FAKE_DOCKER_LOG=str(docker_log),
                 FAKE_CURL_TAMPER=tamper),
        capture_output=True, text=True, timeout=180, check=False,
    )
    urls = [json.loads(x)["url"] for x in curl_log.read_text().splitlines()] if curl_log.exists() else []
    builds = (
        [a for a in (json.loads(x) for x in docker_log.read_text().splitlines()) if a[:2] == ["buildx", "build"]]
        if docker_log.exists() else []
    )
    return root, rows, proc, urls, builds


def _build_args(argv: list[str]) -> dict[str, str]:
    return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv[:-1]) if a == "--build-arg")


def test_a_local_build_reads_upstream_at_the_same_sha256_and_overrides_the_mirrors(tmp_path: Path) -> None:
    root, rows, proc, urls, builds = _local(tmp_path, ["agent-runtime-base"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert sorted(urls) == sorted(f"https://{obj}" for _, obj in rows)
    for sha, obj in rows:
        staged = root / "build" / "tool-mirror" / obj
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == sha, obj
    assert len(builds) == 1, builds
    args = _build_args(builds[0])
    for name, (_, upstream) in MIRRORS.items():
        assert args.get(name) == upstream, (name, args)
    # The Dockerfile's own `# syntax=` line already names Docker Hub, so a
    # local build passes no BUILDKIT_SYNTAX to override it.
    assert "BUILDKIT_SYNTAX" not in args, args


def test_a_local_build_refuses_an_upstream_file_with_the_wrong_sha256(tmp_path: Path) -> None:
    bad = _manifest_rows()[0][1]
    _, _, proc, _, builds = _local(tmp_path, ["agent-runtime-base"], tamper=f"https://{bad}")
    assert proc.returncode != 0, "a tampered download was built with"
    assert bad in proc.stderr, proc.stderr[-2000:]
    assert not builds, "docker build ran after a download failed its sha256"


def test_a_local_build_of_an_image_without_tools_fetches_nothing(tmp_path: Path) -> None:
    _, _, proc, urls, builds = _local(tmp_path, ["swarm-api"])
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert urls == []
    assert len(builds) == 1
    assert _build_args(builds[0]).get("MIRROR_GHCR") == "ghcr.io"




def test_the_refresh_check_names_a_mirror_image_that_is_not_cached(tmp_path: Path) -> None:
    # The refresh expands the Dockerfiles' pulls with PROJECT_ID's mirrors
    # (_env: swarm-test-project, us-central1), not with their empty defaults.
    host = "us-central1-docker.pkg.dev/swarm-test-project"
    api = _global_args((REPO / "images/swarm-api/Dockerfile").read_text())
    uv = f"{host}/swarm-mirror-ghcr/{api['UV_IMAGE']}"
    # gcloud takes a tag or a digest, not both: the digest is what is pinned.
    name, digest = uv.split("@")
    by_digest = f"{name.rsplit(':', 1)[0]}@{digest}"
    _, _, proc, calls, _ = _refresh(tmp_path, "--check", "--images-only", missing_images=by_digest)
    assert proc.returncode != 0
    assert uv in proc.stderr, proc.stderr[-2000:]
    described = {c[4] for c in calls if c[:4] == ["artifacts", "docker", "images", "describe"]}
    assert by_digest in described
    # Every mirror reference in every Dockerfile, the frontend included.
    hub = f"{host}/swarm-mirror-dockerhub"
    assert any(d.startswith(f"{hub}/docker/dockerfile@sha256:") for d in described), sorted(described)
    assert any(d.startswith(f"{hub}/library/python@sha256:") for d in described), sorted(described)


