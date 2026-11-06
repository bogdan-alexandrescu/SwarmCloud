"""agent-runtime-lightpanda is built, promoted, warmed and its source mirrored
by the same scripts as every other runner image (LIGHTPANDA-BUILD-SCRIPTS,
part of #2688; docs/design/lightpanda-runtime.md sections 5.D, 6.4 and lane
L3 of section 7).

images/agent-runtime-lightpanda/ merged with LIGHTPANDA-IMAGE, and
build-images.sh already knew it is built after agent-runtime-base
(build_after), but no target list named it, so no build or release produced
it. These hold:

  * it is in ALL_TARGETS of scripts/build-images.sh and scripts/push-images.sh,
    right after agent-runtime-indexer (the other image built FROM the base by
    digest), so the two lists, held equal by test_indexer_image_split.py, read
    alike;
  * a change under images/agent-runtime-lightpanda reaches it, and so does a
    change to the base it is built FROM; a docs change and the browser's own
    recipe do not (the controls);
  * scripts/warm-jobs.sh counts it among the runner images whose digests
    decide whether a release warms anything (test_warm_jobs.py runs that);
  * scripts/lib/build-mirror.sh names the tag's source archive -- the
    Corresponding Source the image's SOURCE notice points at (AGPL section
    6(d)) -- as a mirror object, its sha256 and its release-home object, all
    read from the Dockerfile's pins and checked against the tool mirror
    manifest; and stages it from upstream, checked, for the release bundle.

WHAT THIS CANNOT PROVE: that the image builds, or that the release publishes
the archive. The image's build runs in CI; the publish is scripts/release/.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE = "agent-runtime-lightpanda"
DOCKERFILE = REPO / "images" / IMAGE / "Dockerfile"
MANIFEST = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(tmp_path),
            "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
            "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1", "TMPDIR": str(tmp_path),
            **extra}


def _arg(name: str) -> str:
    found = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(), re.M)
    assert found, f"no ARG {name}= in {DOCKERFILE}"
    return found.group(1)


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_lightpanda_image(script):
    targets = _targets(script)
    assert IMAGE in targets, targets
    assert targets.index(IMAGE) == targets.index("agent-runtime-indexer") + 1, targets


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
@pytest.mark.parametrize("changed, reaches", [
    ("images/agent-runtime-lightpanda/selftest.py", True),
    ("images/agent-runtime-lightpanda/Dockerfile", True),
    # Built FROM the base by digest: a base change rebuilds it (build_after).
    ("images/agent-runtime-base/Dockerfile", True),
    # The controls: the browser's own recipe, and docs.
    ("images/agent-runtime-browser/cloudbuild.yaml", False),
    ("docs/design/lightpanda-runtime.md", False),
])
def test_target_selection_reaches_the_lightpanda_image_for_its_inputs_and_its_base(tmp_path, changed, reaches):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--affected-by", "-"],
        input=f"{changed}\n", env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (IMAGE in proc.stdout.split()) is reaches, (changed, proc.stdout)


def test_warm_jobs_counts_the_lightpanda_image_as_a_runner():
    found = re.search(r"^RUNNER_IMAGES=\(([^)]*)\)$", (REPO / "scripts" / "warm-jobs.sh").read_text(), re.M)
    assert found, "no RUNNER_IMAGES in scripts/warm-jobs.sh"
    assert IMAGE in found.group(1).split(), found.group(1)


# --- scripts/lib/build-mirror.sh: the Corresponding Source ------------------


def _mirror(tmp_path: Path, call: str, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source scripts/lib/common.sh && source scripts/lib/build-mirror.sh && {call}"],
        cwd=REPO, env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=60, check=False,
    )


def test_the_source_row_is_the_dockerfiles_pins_and_a_tool_mirror_row(tmp_path):
    proc = _mirror(tmp_path, "lightpanda_source_row")
    assert proc.returncode == 0, proc.stderr
    version = _arg("LIGHTPANDA_VERSION")
    sha = _arg("LIGHTPANDA_SOURCE_SHA256")
    obj = f"github.com/lightpanda-io/browser/archive/refs/tags/{version}.tar.gz"
    release = f"third-party/lightpanda/{version}/source.tar.gz"
    assert proc.stdout.split() == [sha, obj, release], proc.stdout
    # The release object is exactly where the image's SOURCE notice sends a
    # reader, and the mirror object is what every build already reads.
    assert _arg("LIGHTPANDA_SOURCE_URL") == f"https://storage.googleapis.com/swarmcloud/{release}"
    assert f"{sha}  {obj}" in MANIFEST.read_text().splitlines()


def _sandbox(tmp_path: Path, manifest: str | None = None, source_url: str | None = None) -> Path:
    """A repository root holding the two files the row is read from."""
    root = tmp_path / "root"
    (root / "images" / IMAGE).mkdir(parents=True)
    (root / "images" / "agent-runtime-base").mkdir(parents=True)
    text = DOCKERFILE.read_text()
    if source_url is not None:
        text = re.sub(r"^ARG LIGHTPANDA_SOURCE_URL=\S+$", f"ARG LIGHTPANDA_SOURCE_URL={source_url}", text, flags=re.M)
    (root / "images" / IMAGE / "Dockerfile").write_text(text)
    (root / "images" / "agent-runtime-base" / "tool-mirror.sha256").write_text(
        MANIFEST.read_text() if manifest is None else manifest)
    return root


def _sandboxed(tmp_path: Path, root: Path, call: str, **extra: str) -> subprocess.CompletedProcess:
    # common.sh sets REPO_ROOT from its own location; the sandbox's is set after.
    return _mirror(tmp_path, f"REPO_ROOT={root} && TOOL_MIRROR_MANIFEST={root}/images/agent-runtime-base/"
                             f"tool-mirror.sha256 && {call}", **extra)


def test_a_source_pin_the_tool_mirror_does_not_carry_is_refused(tmp_path):
    kept = [line for line in MANIFEST.read_text().splitlines() if "archive/refs/tags" not in line]
    root = _sandbox(tmp_path, manifest="\n".join(kept) + "\n")
    proc = _sandboxed(tmp_path, root, "lightpanda_source_row")
    assert proc.returncode != 0
    assert "tool-mirror.sha256" in proc.stderr and "source archive" in proc.stderr, proc.stderr


def test_a_source_url_outside_the_release_home_is_refused(tmp_path):
    root = _sandbox(tmp_path, source_url="https://github.com/lightpanda-io/browser/archive/refs/tags/1.0.2.tar.gz")
    proc = _sandboxed(tmp_path, root, "lightpanda_source_row")
    assert proc.returncode != 0
    assert "LIGHTPANDA_SOURCE_URL" in proc.stderr, proc.stderr


FAKE_CURL = r'''#!/usr/bin/env bash
# curl -fsSL ... -o DEST URL: writes $FAKE_CURL_BODY to DEST, and logs the URL.
set -euo pipefail
out=""; url=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --retry|--retry-delay) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\n' "${url}" >>"${FAKE_CURL_LOG}"
printf '%s' "${FAKE_CURL_BODY}" >"${out}"
'''


def _fake_curl(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    return bindir


def test_staging_writes_the_checked_archive_at_its_release_object(tmp_path):
    body = "lightpanda source " + os.urandom(8).hex()
    sha = hashlib.sha256(body.encode()).hexdigest()
    version = _arg("LIGHTPANDA_VERSION")
    obj = f"github.com/lightpanda-io/browser/archive/refs/tags/{version}.tar.gz"
    lines = [line for line in MANIFEST.read_text().splitlines() if not line.endswith(f"  {obj}")]
    root = _sandbox(tmp_path, manifest="\n".join([*lines, f"{sha}  {obj}"]) + "\n")
    df = root / "images" / IMAGE / "Dockerfile"
    df.write_text(re.sub(r"^ARG LIGHTPANDA_SOURCE_SHA256=\S+$", f"ARG LIGHTPANDA_SOURCE_SHA256={sha}",
                         df.read_text(), flags=re.M))
    bindir = _fake_curl(tmp_path)
    out = tmp_path / "bundle"
    log = tmp_path / "curl.log"
    proc = _sandboxed(tmp_path, root, f"lightpanda_source_stage {out}",
                      PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                      FAKE_CURL_BODY=body, FAKE_CURL_LOG=str(log))
    assert proc.returncode == 0, proc.stderr
    staged = out / "third-party" / "lightpanda" / version / "source.tar.gz"
    assert staged.read_text() == body
    assert log.read_text().split() == [f"https://{obj}"]
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        f"third-party/lightpanda/{version}/source.tar.gz"]


def test_staging_refuses_an_archive_that_is_not_the_pinned_one(tmp_path):
    bindir = _fake_curl(tmp_path)
    out = tmp_path / "bundle"
    proc = _mirror(tmp_path, f"lightpanda_source_stage {out}",
                   PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   FAKE_CURL_BODY="not the tag's archive", FAKE_CURL_LOG=str(tmp_path / "curl.log"))
    assert proc.returncode != 0
    assert "not the" in proc.stderr and "refusing it" in proc.stderr, proc.stderr
    assert not any(p.is_file() for p in out.rglob("*")) if out.exists() else True
