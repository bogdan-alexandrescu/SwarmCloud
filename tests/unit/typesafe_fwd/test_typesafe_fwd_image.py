"""The TypeSafe forwarder ships as an image the release builds (lane
SEC-INJECT-B, part of #1347; docs/design/workspace-secrets.md §3.5).

terraform/infra/typesafe_fwd.tf renders `swarm_typesafe_fwd_manifest` only
once image_refs has a `swarm-typesafe-fwd` digest, and image_refs is written
from what build-images.sh built and push-images.sh promoted. These hold the
path, the way tests/unit/scripts/test_swarm_egress_image.py holds swarm-egress's:

  * `swarm-typesafe-fwd` is in ALL_TARGETS of both scripts, beside
    swarm-egress, and build-images.sh finds its recipe,
    apps/swarm-typesafe-fwd/Dockerfile, and reads what it copies in;
  * a change under apps/swarm-typesafe-fwd reaches it, one under
    apps/swarm-egress or docs/ does not (the controls);
  * a promoted digest becomes its image_refs entry;
  * the Dockerfile runs as the manifest's non-root uid, the entrypoint runs the
    forwarder on the manifest's port, and nothing in it writes to the root
    filesystem at run time (the manifest mounts it read-only).

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
APP = REPO / "apps" / "swarm-typesafe-fwd"
DOCKERFILE = APP / "Dockerfile"
MANIFEST = REPO / "kubernetes" / "swarm-system" / "swarm-typesafe-fwd.yaml"
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
NAME = "swarm-typesafe-fwd"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(tmp_path),
            "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
            "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1", "TMPDIR": str(tmp_path)}


def _stage(name: str) -> str:
    text = re.sub(r"\\\n\s*", " ", DOCKERFILE.read_text())
    found = re.search(rf"^FROM \S+ AS {name}\n(.*?)(?=^FROM |\Z)", text, re.M | re.S)
    assert found, f"no `{name}` stage in {DOCKERFILE}"
    return found.group(1)


def _deployment() -> dict:
    text = MANIFEST.read_text().replace("__TYPESAFE_FWD_CONFIG_JSON__", "{}")
    return next(d for d in yaml.safe_load_all(text) if d and d["kind"] == "Deployment")


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_forwarder(script):
    targets = _targets(script)
    assert NAME in targets, targets
    assert targets.index(NAME) == targets.index("swarm-egress") + 1, targets


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == NAME}
    assert {"apps/swarm-typesafe-fwd/*", "apps/common/*", "apps/redaction/*"} <= inputs, sorted(inputs)


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
@pytest.mark.parametrize("changed, reaches", [
    ("apps/swarm-typesafe-fwd/swarm_typesafe_fwd/forwarder.py", True),
    ("apps/swarm-typesafe-fwd/Dockerfile", True),
    ("apps/swarm-egress/swarm_egress/server.py", False),
    ("docs/design/workspace-secrets.md", False),
])
def test_target_selection_reaches_it_only_for_its_own_inputs(tmp_path, changed, reaches):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--affected-by", "-"],
        input=f"{changed}\n", env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (NAME in proc.stdout.split()) is reaches, (changed, proc.stdout)


@pytest.mark.skipif(shutil.which("jq") is None, reason="image-refs.sh needs jq")
def test_a_promoted_digest_becomes_its_image_refs_entry(tmp_path):
    images = [{"name": name, "ref": f"{REGISTRY}/{name}@sha256:" + format(i % 16, "x") * 64}
              for i, name in enumerate(_targets("push-images.sh"))]
    manifest = tmp_path / "deployed-images-dev.json"
    manifest.write_text(json.dumps({"tag": "abc", "environment": "dev", "images": images}))
    out = tmp_path / "image-refs.tfvars.json"
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "lib" / "image-refs.sh"), "--manifest", str(manifest), "--out", str(out)],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    refs = json.loads(out.read_text())["image_refs"]
    assert refs[NAME].startswith(f"{REGISTRY}/{NAME}@sha256:"), refs


def test_terraform_looks_the_image_up_under_the_same_name():
    text = (REPO / "terraform" / "infra" / "typesafe_fwd.tf").read_text()
    assert f'lookup(var.image_refs, "{NAME}", "")' in text


def test_it_runs_as_the_non_root_uid_the_manifest_runs_it_as():
    runtime = _stage("runtime")
    uid = re.search(r"useradd --uid (\d+) --gid (\d+)", runtime)
    assert uid and uid.group(1) == uid.group(2) != "0"
    assert re.search(r"^USER swarm:swarm$", runtime, re.M)
    context = _deployment()["spec"]["template"]["spec"]["securityContext"]
    assert context["runAsNonRoot"] is True
    assert str(context["runAsUser"]) == uid.group(1)


def test_its_entrypoint_runs_the_forwarder_on_the_manifests_port():
    from swarm_typesafe_fwd.main import PORT

    runtime = _stage("runtime")
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_typesafe_fwd.main"]' in runtime
    exposed = re.search(r"^EXPOSE (\d+)$", runtime, re.M)
    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    assert container["image"] == "__SWARM_TYPESAFE_FWD_IMAGE__"
    ports = {p["containerPort"] for p in container["ports"]}
    assert exposed and int(exposed.group(1)) == PORT and PORT in ports


def test_the_root_filesystem_is_read_only_and_nothing_needs_it_writable():
    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    runtime = _stage("runtime")
    # No bytecode written at run time (the read-only root would refuse it).
    assert "PYTHONDONTWRITEBYTECODE=1" in runtime


def test_the_environment_it_reads_is_the_manifests():
    from swarm_typesafe_fwd import main

    (container,) = _deployment()["spec"]["template"]["spec"]["containers"]
    names = {e["name"] for e in container["env"]}
    assert names == {main.KEY_ENV, main.PROJECT_ENV, main.CONFIG_ENV}


def test_the_build_context_lets_the_forwarder_in():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    for path in ("apps", "apps/swarm-typesafe-fwd", "apps/swarm-typesafe-fwd/uv.lock",
                 "apps/common", "apps/redaction"):
        assert not [p for p in ignored if p.rstrip("/") in (path, path + "/**", path + "/*")], path
