"""The live relay ships as an image the release builds (lane LB-L8e, part of
#1030; docs/design/live-browser.md §3.3, §7).

terraform/infra/live.tf renders the relay's manifest only once image_refs has a
`swarm-live` digest, and image_refs is written from the release's build record.
So the relay is deployable exactly when the release builds it -- these hold that:

  * `swarm-live` is in ALL_TARGETS of scripts/build-images.sh and
    scripts/push-images.sh, and build-images.sh finds its recipe,
    apps/swarm-live/Dockerfile;
  * the Dockerfile has the properties the manifest relies on: it runs as uid
    10001 (the manifest's runAsUser, under the `restricted` pod standard), its
    entrypoint runs the relay, which serves plain ws on 8080, and it carries an
    ffmpeg with libvpx for the recorder;
  * it installs like every service image (#663): third-party packages by hash
    from a lock, our own from the wheelhouse only;
  * that lock, apps/swarm-live/uv.lock, is the closure of apps/swarm-live's
    pyproject.toml -- not a stale one -- at the root lock's versions.

WHAT THIS CANNOT PROVE: that the image builds. The pull request's build-check
job (application.yml) builds it, because apps/** reaches that job.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-live"
DOCKERFILE = APP / "Dockerfile"
MANIFEST = REPO / "kubernetes" / "swarm-system" / "swarm-live.yaml"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


def _dockerfile() -> str:
    # Continuation lines joined, so an instruction reads as one line.
    return re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text())


def _stage(name: str) -> str:
    text = _dockerfile()
    found = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", text, re.M | re.S)
    assert found, f"no `{name}` stage in {DOCKERFILE}"
    return found.group(1)


def _names(requirements: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!~\[;]", r, maxsplit=1)[0].lower().replace("_", "-") for r in requirements}


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_relay(script):
    assert "swarm-live" in _targets(script)


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_relays_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path),
             "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
             "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == "swarm-live"}
    assert {"apps/swarm-live/*", "apps/common/*"} <= inputs, sorted(inputs)


def test_every_base_is_pinned_by_digest():
    text = _dockerfile()
    for arg in ("PYTHON_IMAGE", "UV_IMAGE"):
        value = re.search(rf"^ARG {arg}=(\S+)$", text, re.M)
        assert value and re.search(r"@sha256:[0-9a-f]{64}$", value.group(1)), arg
    for line in re.findall(r"^FROM (\S+)", text, re.M):
        assert line.startswith("${"), f"FROM {line} is not one of the pinned ARGs"


def test_the_runtime_carries_ffmpeg_and_proves_its_vp8_encoder():
    runtime = _stage("runtime")
    install = re.search(r"apt-get install [^\n&]*", runtime)
    assert install and "ffmpeg" in install.group(0).split(), install
    assert "-encoders" in runtime and "libvpx" in runtime


def test_it_runs_as_the_non_root_uid_the_manifest_runs_it_as():
    runtime = _stage("runtime")
    uid = re.search(r"useradd --uid (\d+) --gid (\d+)", runtime)
    assert uid and uid.group(1) == uid.group(2) != "0"
    assert re.search(r"^USER swarm:swarm$", runtime, re.M)
    statefulset = next(d for d in yaml.safe_load_all(MANIFEST.read_text()) if d and d["kind"] == "StatefulSet")
    context = statefulset["spec"]["template"]["spec"]["securityContext"]
    assert context["runAsNonRoot"] is True
    assert str(context["runAsUser"]) == uid.group(1) and str(context["runAsGroup"]) == uid.group(2)


def test_its_entrypoint_runs_the_relay_on_8080():
    runtime = _stage("runtime")
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_live.main"]' in runtime
    assert re.search(r"^EXPOSE 8080$", runtime, re.M)
    # No serving certificate or key, and no TLS setting (ca-certificates, the
    # client trust store, is not one).
    assert not re.search(r"8443|ssl[-_]|SWARM_LIVE_TLS|\.pem\b|\.crt\b|\.key\b", runtime), "the relay serves no TLS (decision (b))"


def test_it_installs_by_hash_from_its_own_lock_and_ours_from_the_wheelhouse_only():
    builder = _stage("builder")
    export = re.search(r"uv export ([^\n>]*)>", builder)
    assert export, "no `uv export` in the builder"
    flags = export.group(1).split()
    assert {"--frozen", "--no-dev", "--no-emit-local", "--no-editable"} <= set(flags)
    assert "--no-hashes" not in flags
    assert flags[flags.index("--project") + 1] == "/src/apps/swarm-live"
    installs = re.findall(r"uv pip install ([^\n]*)", builder)
    assert any("--require-hashes" in i and "--no-deps" in i for i in installs), installs
    for i in installs:
        if "--find-links" in i:
            assert "--no-index" in i, f"--find-links without --no-index keeps PyPI as an index: {i}"
            assert set(i.split()) >= {"swarm-common", "swarm-live"}


def test_the_relays_lock_is_its_pyprojects_closure_at_the_root_locks_versions():
    """A stale lock builds an image with the dependencies of a pyproject that
    no longer exists, and nothing in CI checks this lock (`uv lock --check`
    runs at the root only)."""
    declared = tomllib.loads((APP / "pyproject.toml").read_text())["project"]["dependencies"]
    lock = tomllib.loads((APP / "uv.lock").read_text())
    relay = next(p for p in lock["package"] if p["name"] == "swarm-live")
    assert _names([d["name"] for d in relay["metadata"]["requires-dist"]]) == _names(declared)
    assert _names([d["name"] for d in relay["dependencies"]]) == _names(declared)

    root = {p["name"]: p.get("version") for p in tomllib.loads((REPO / "uv.lock").read_text())["package"]}
    drift = {
        p["name"]: (root[p["name"]], p.get("version"))
        for p in lock["package"]
        if p["name"] in root and p["name"] not in {"swarm-common", "swarm-live"} and root[p["name"]] != p.get("version")
    }
    assert drift == {}, f"apps/swarm-live/uv.lock left the root lock's versions: {drift}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_the_export_the_builder_runs_hashes_every_requirement():
    proc = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-local", "--no-editable", "--project", str(APP)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    requirements = re.findall(r"^([A-Za-z0-9_.\-]+)==", proc.stdout, re.M)
    assert requirements, proc.stdout[:500]
    blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", proc.stdout.split("\n", 2)[-1])
    assert all("--hash=sha256:" in b for b in blocks if re.match(r"[A-Za-z0-9_.\-]+==", b))
    assert not {"swarm-common", "swarm-live"} & {r.lower() for r in requirements}
    assert "google-cloud-storage" in {r.lower() for r in requirements}


def test_the_build_context_lets_the_relay_in():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    for path in ("apps", "apps/swarm-live", "apps/swarm-live/uv.lock", "apps/common"):
        assert not [p for p in ignored if p.rstrip("/") in (path, path + "/**", path + "/*")], path
