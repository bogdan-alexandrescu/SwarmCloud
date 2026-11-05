"""The release builds and pins swarm-speech (docs/design/voice-it-own-speech.md
§5.3, §14 L3, part of #2688).

The speech machine pulls the image named by its `swarm-speech-image` metadata,
which swarm-api sets from SWARM_SPEECH_IMAGE at the next start; that value is
the `swarm-speech` entry of image_refs, and image_refs is written
(scripts/lib/image-refs.sh) from the promotion manifest push-images.sh writes
for the images build-images.sh recorded. So the machine gets a new server
exactly when the release builds and promotes it -- these hold that:

  * `swarm-speech` is in ALL_TARGETS of scripts/build-images.sh and
    scripts/push-images.sh, so every release builds, scans and promotes it;
  * build-images.sh finds its recipe, apps/swarm-speech/Dockerfile, and reads
    what it copies, so a change to the server rebuilds it;
  * the reference image_refs carries for it is the form the machine's launch
    script accepts: an Artifact Registry swarm-speech image, by digest.

WHAT THIS CANNOT PROVE: that the image builds. The pull request's build-check
job builds it, because apps/** reaches that job.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LAUNCH = REPO / "apps" / "swarm-speech" / "vm" / "swarm-speech-launch.sh"


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_release_builds_and_promotes_the_speech_server(script):
    assert "swarm-speech" in _targets(script)


def test_both_scripts_list_the_same_images():
    assert _targets("build-images.sh") == _targets("push-images.sh")


@pytest.mark.skipif(shutil.which("jq") is None, reason="build-images.sh needs jq")
def test_build_images_finds_the_speech_servers_recipe_and_reads_what_it_copies(tmp_path):
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "build-images.sh"), "--inputs"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path),
             "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
             "REGION": "us-central1", "ENVIRONMENT": "dev", "NO_COLOR": "1"},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    inputs = {p for t, p in (line.split("\t") for line in proc.stdout.splitlines() if line) if t == "swarm-speech"}
    assert "apps/swarm-speech/*" in inputs, sorted(inputs)


def test_the_pinned_reference_is_what_the_machine_pulls():
    """image_refs' form (terraform/infra/variables.tf's validations) for the
    name swarm-speech, checked against the launch script's own pattern."""
    pattern = re.search(r'\[\[ ! "\$\{image\}" =~ (\S+) \]\]', LAUNCH.read_text())
    assert pattern, f"no image check in {LAUNCH}"
    ref = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images/swarm-speech@sha256:" + "0" * 64
    proc = subprocess.run(
        ["bash", "-c", f'[[ "$1" =~ {pattern.group(1)} ]]', "_", ref],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert proc.returncode == 0, (pattern.group(1), ref)
