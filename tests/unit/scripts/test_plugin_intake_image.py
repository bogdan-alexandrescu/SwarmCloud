"""The release builds and pins swarm-plugin-intake (docs/design/managed-plugins.md
§2.2, the intake job of terraform/infra/plugins.tf).

plugins.tf creates `google_cloud_run_v2_job.plugin_intake` only when
`var.image_refs` has a `swarm-plugin-intake` entry (`local.plugin_intake_on`),
and image_refs is written (scripts/lib/image-refs.sh) from the promotion
manifest push-images.sh writes for the images build-images.sh recorded. On
2026-11-09 neither script listed the image, so no deployment had the job and
every Add plugin answered 503 "The intake job could not be started". These
hold that:

  * `swarm-plugin-intake` is in ALL_TARGETS of scripts/build-images.sh and
    scripts/push-images.sh, so every release builds, scans and promotes it;
  * build-images.sh finds its recipe, apps/plugin-intake/Dockerfile -- the app
    directory has no `swarm-` prefix, so the image name alone would not find
    it -- and reads what it copies, so a change to the job rebuilds it;
  * the image is not switched off by any tfvars block: the job is planned on
    every deployment once the digest is there;
  * it is the gated image terraform/infra/images.json names for the job.

WHAT THIS CANNOT PROVE: that the image builds. The pull request's build-check
job builds it, because apps/** reaches that job.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
IMAGE = "swarm-plugin-intake"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


def _env(tmp_path: Path) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path),
            "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
            "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1"}


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_intake_image(script):
    assert IMAGE in _targets(script)


def test_both_scripts_list_the_same_images():
    assert _targets("build-images.sh") == _targets("push-images.sh")


def test_terraform_gates_the_intake_job_on_this_image_name():
    gated = json.loads((REPO / "terraform" / "infra" / "images.json").read_text())["gated"]
    assert gated[IMAGE]["resources"] == ["google_cloud_run_v2_job.plugin_intake"], gated[IMAGE]
    assert f'lookup(var.image_refs, "{IMAGE}", "")' in (REPO / "terraform" / "infra" / "plugins.tf").read_text()


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_intake_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == IMAGE}
    assert "apps/plugin-intake/*" in inputs, sorted(inputs)
    assert "apps/plugin-intake/plugin_intake/*" in inputs, sorted(inputs)


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
@pytest.mark.parametrize(
    ("changed", "reaches"),
    [
        ("apps/plugin-intake/plugin_intake/intake.py", True),
        ("apps/plugin-intake/Dockerfile", True),
        ("apps/swarm-api/swarm_api/plugins.py", False),
    ],
)
def test_a_change_to_the_job_rebuilds_the_image_and_nothing_else_does(tmp_path, changed, reaches):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--affected-by", "-"],
        input=changed + "\n", env=_env(tmp_path), capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (IMAGE in proc.stdout.split()) is reaches, proc.stdout


def test_no_tfvars_switch_turns_the_intake_image_off():
    """The job is planned on every deployment once its digest is in image_refs,
    so a release never skips the image (scripts/lib/image-switches.sh)."""
    proc = subprocess.run(
        ["bash", "-c", 'source scripts/lib/image-switches.sh; printf "[%s]" "$(image_switch "$1")"', "_", IMAGE],
        cwd=REPO, capture_output=True, text=True, timeout=10, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "[]", proc.stdout
