"""The egress forwarder ships as an image the release builds (lane EGRESS-BUILD,
part of #1030; docs/browser-egress.md §9).

terraform/infra/egress.tf renders `swarm_egress_manifest` only once image_refs
has a `swarm-egress` digest, and its check `browser_egress_has_an_image` warns
when enable_browser_egress is on without one. image_refs is written from the
release's promotion manifest (scripts/lib/image-refs.sh --manifest), which
push-images.sh writes from what build-images.sh built. Until this lane nothing
built `swarm-egress`, so dev's enable_browser_egress = true could never render
a manifest. These hold the path, the way test_swarm_live_image.py holds the
relay's:

  * `swarm-egress` is in ALL_TARGETS of scripts/build-images.sh and
    scripts/push-images.sh, beside swarm-live, and build-images.sh finds its
    recipe, apps/swarm-egress/Dockerfile, and reads what it copies in;
  * a change under apps/swarm-egress reaches swarm-egress, and one under
    apps/swarm-live or docs/ does not (the controls);
  * a promotion manifest that names swarm-egress becomes an image_refs entry
    for it -- the file terraform reads;
  * the Dockerfile runs as the uid kubernetes/swarm-system/swarm-egress.yaml
    runs it as, and its entrypoint runs the forwarder on the manifest's port.

WHAT THIS CANNOT PROVE: that the image builds. The pull request's build-check
job (application.yml) builds it, because apps/** reaches that job.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-egress"
DOCKERFILE = APP / "Dockerfile"
MANIFEST = REPO / "kubernetes" / "swarm-system" / "swarm-egress.yaml"
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(tmp_path),
            "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
            "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1", "TMPDIR": str(tmp_path)}


def _dockerfile() -> str:
    return re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text())


def _stage(name: str) -> str:
    found = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", _dockerfile(), re.M | re.S)
    assert found, f"no `{name}` stage in {DOCKERFILE}"
    return found.group(1)


def _statefulset() -> dict:
    return next(d for d in yaml.safe_load_all(MANIFEST.read_text()) if d and d["kind"] == "StatefulSet")


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_forwarder(script):
    targets = _targets(script)
    assert "swarm-egress" in targets, targets
    # Beside the relay, the other apps/<target>/Dockerfile recipe, so the two
    # lists (held equal by test_indexer_image_split.py) read alike.
    assert targets.index("swarm-egress") == targets.index("swarm-live") + 1, targets


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_forwarders_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == "swarm-egress"}
    assert {"apps/swarm-egress/*", "apps/common/*", "apps/redaction/*"} <= inputs, sorted(inputs)


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
@pytest.mark.parametrize("changed, reaches", [
    ("apps/swarm-egress/swarm_egress/policy.py", True),
    ("apps/swarm-egress/Dockerfile", True),
    # The controls: an unrelated path, and the sibling recipe under apps/.
    ("apps/swarm-live/swarm_live/main.py", False),
    ("docs/browser-egress.md", False),
])
def test_target_selection_reaches_the_forwarder_only_for_its_own_inputs(tmp_path, changed, reaches):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--affected-by", "-"],
        input=f"{changed}\n", env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert ("swarm-egress" in proc.stdout.split()) is reaches, (changed, proc.stdout)


@pytest.mark.skipif(shutil.which("jq") is None, reason="image-refs.sh needs jq")
def test_a_promoted_forwarder_digest_becomes_its_image_refs_entry(tmp_path):
    """The last hop before terraform: the promotion manifest push-images.sh
    writes -> image_refs. Nothing in image-refs.sh names an image, so the
    forwarder arrives exactly as every other image does."""
    names = _targets("push-images.sh")
    images = []
    for i, name in enumerate(names):
        # 64 hex digits whatever the count of images (seventeen since
        # LIGHTPANDA-BUILD-SCRIPTS: a repeated digit is two digits from 16 on).
        digest = f"sha256:{i:064x}"
        images.append({"name": name, "ref": f"{REGISTRY}/{name}@{digest}"})
    manifest = tmp_path / "deployed-images-dev.json"
    manifest.write_text(json.dumps({"tag": "abc", "environment": "dev", "images": images}))
    out = tmp_path / "image-refs.tfvars.json"
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "lib" / "image-refs.sh"), "--manifest", str(manifest), "--out", str(out)],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    refs = json.loads(out.read_text())["image_refs"]
    assert refs["swarm-egress"].startswith(f"{REGISTRY}/swarm-egress@sha256:"), refs


def test_it_runs_as_the_non_root_uid_the_manifest_runs_it_as():
    runtime = _stage("runtime")
    uid = re.search(r"useradd --uid (\d+) --gid (\d+)", runtime)
    assert uid and uid.group(1) == uid.group(2) != "0"
    assert re.search(r"^USER swarm:swarm$", runtime, re.M)
    context = _statefulset()["spec"]["template"]["spec"]["securityContext"]
    assert context["runAsNonRoot"] is True
    assert str(context["runAsUser"]) == uid.group(1)


def test_its_entrypoint_runs_the_forwarder_on_the_manifests_port():
    runtime = _stage("runtime")
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_egress.main"]' in runtime
    exposed = re.search(r"^EXPOSE (\d+)$", runtime, re.M)
    container = next(c for c in _statefulset()["spec"]["template"]["spec"]["containers"] if c["name"] == "egress")
    assert container["image"] == "__SWARM_EGRESS_IMAGE__"
    assert exposed and int(exposed.group(1)) in {p["containerPort"] for p in container["ports"]}


def test_the_build_context_lets_the_forwarder_in():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    for path in ("apps", "apps/swarm-egress", "apps/swarm-egress/uv.lock", "apps/common", "apps/redaction"):
        assert not [p for p in ignored if p.rstrip("/") in (path, path + "/**", path + "/*")], path
