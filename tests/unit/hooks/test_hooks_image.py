"""The receiver ships as an image the release builds and terraform deploys
(lane GS-HOOKS, part of #1384), the way tests/unit/typesafe_fwd's image test
holds swarm-typesafe-fwd's path:

  * `swarm-github-hooks` is in ALL_TARGETS of build-images.sh and
    push-images.sh, after swarm-graph, and build-images.sh finds its recipe,
    apps/swarm-github-hooks/Dockerfile, and reads what it copies in;
  * a change under apps/swarm-github-hooks reaches it, one under
    apps/swarm-graph or docs/ does not (the controls);
  * a promoted digest becomes its image_refs entry, and
    terraform/infra/github_hooks.tf looks it up under the same name;
  * the Dockerfile runs as a non-root uid, its entrypoint runs the receiver on
    the port the Cloud Run service declares, and the image carries no
    credential.

WHAT THIS CANNOT PROVE: that the image builds. The pull request's build check
(application.yml) builds it, because apps/** reaches that job.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-github-hooks"
DOCKERFILE = APP / "Dockerfile"
TF = REPO / "terraform" / "infra" / "github_hooks.tf"
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
NAME = "swarm-github-hooks"


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


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_receiver(script):
    targets = _targets(script)
    assert NAME in targets, targets
    assert targets.index(NAME) == targets.index("swarm-graph") + 1, targets


def test_both_scripts_build_the_same_list():
    assert _targets("build-images.sh") == _targets("push-images.sh")


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == NAME}
    assert {"apps/swarm-github-hooks/*", "apps/common/*", "apps/redaction/*"} <= inputs, sorted(inputs)


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
@pytest.mark.parametrize("changed, reaches", [
    ("apps/swarm-github-hooks/swarm_github_hooks/receiver.py", True),
    ("apps/swarm-github-hooks/Dockerfile", True),
    ("apps/swarm-github-hooks/uv.lock", True),
    ("apps/swarm-graph/swarm_graph/server.py", False),
    ("docs/runbooks/github-app.md", False),
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
    assert f'lookup(var.image_refs, "{NAME}", "")' in TF.read_text()


def test_it_runs_as_a_non_root_uid():
    runtime = _stage("runtime")
    uid = re.search(r"useradd --uid (\d+) --gid (\d+)", runtime)
    assert uid and uid.group(1) == uid.group(2) != "0"
    assert re.search(r"^USER swarm:swarm$", runtime, re.M)


def test_its_entrypoint_runs_the_receiver_on_the_services_port():
    from swarm_github_hooks.main import DEFAULT_PORT

    runtime = _stage("runtime")
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "swarm_github_hooks.main"]' in runtime
    exposed = re.search(r"^EXPOSE (\d+)$", runtime, re.M)
    assert exposed and int(exposed.group(1)) == DEFAULT_PORT
    assert re.search(rf"container_port\s*=\s*{DEFAULT_PORT}\b", TF.read_text())


def test_the_build_proves_the_receiver_imports_and_writes_no_bytecode():
    runtime = _stage("runtime")
    assert "import swarm_github_hooks.main" in runtime
    assert "PYTHONDONTWRITEBYTECODE=1" in runtime


def test_every_from_is_pinned_and_packages_install_by_hash():
    text = DOCKERFILE.read_text()
    for image in re.findall(r"^ARG (?:PYTHON|UV)_IMAGE=(\S+)$", text, re.M):
        assert "@sha256:" in image, image
    assert "--require-hashes" in text and "--no-hashes" not in text.replace("`--no-hashes` must never", "")
    assert "--no-index --find-links /wheels" in text


def test_the_environment_it_reads_is_what_terraform_sets():
    from swarm_github_hooks import main

    text = TF.read_text()
    for name in (main.SECRET_ENV, main.API_URL_ENV, main.DATABASE_ENV):
        assert re.search(rf'name\s*=\s*"{name}"', text), f"github_hooks.tf does not set {name}"


def test_the_build_context_lets_the_receiver_in():
    ignored = [line.strip() for line in (REPO / ".dockerignore").read_text().splitlines()
               if line.strip() and not line.startswith("#")]
    for path in ("apps", "apps/swarm-github-hooks", "apps/swarm-github-hooks/uv.lock",
                 "apps/common", "apps/redaction"):
        assert not [p for p in ignored if p.rstrip("/") in (path, path + "/**", path + "/*")], path


def test_its_lock_names_the_receiver_and_no_unexpected_package():
    names = set(re.findall(r'^name = "([^"]+)"$', (APP / "uv.lock").read_text(), re.M))
    assert "swarm-github-hooks" in names
    assert {"google-auth", "requests", "swarm-common", "swarm-redaction"} <= names
    # The forwarder's closure and no more: no web framework, no Firestore SDK.
    assert not names & {"fastapi", "starlette", "uvicorn", "google-cloud-firestore", "grpcio"}, names
