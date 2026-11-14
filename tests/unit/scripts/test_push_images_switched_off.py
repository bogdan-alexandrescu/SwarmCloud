"""`push-images.sh` gates a release only on the images its environment deploys.

THE DEFECT THIS PINS. The release for main's commit of 2026-11-06 11:28 UTC
built and scanned 19 images and promoted none: trivy refused swarm-speech,
Voice it's own speech server image, and one refusal holds every image back.
Nothing runs swarm-speech in dev -- the speech machine exists only while
`voice_speech.enabled` (terraform/infra/voice_speech.tf), and dev.tfvars has
it off -- yet nothing deployed to SwarmCloud from 23:18 UTC on 5 November.

Asserted against the REAL script with the fake `gcloud` and `trivy` of
test_push_images_promotes_recorded_digests.py, and a real tfvars file in the
copied repository:

  * a refused image whose switch is off is still scanned, its refusal is
    reported (log and step summary), it is not promoted, and the rest are;
  * the same refusal with the switch on blocks the whole release, as before;
  * a refused image the environment deploys still blocks, switch or no switch;
  * an image that is switched off is not promoted even when its scan is clean;
  * no tfvars, or a value that is not literally `false`, counts as deployed;
  * with no tfvars in the checkout (the real ones are untracked), the private
    copy a release fetched and named in SWARM_TFVARS_FILE is the one read --
    for that ENVIRONMENT only -- and one that names no file counts as deployed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from . import test_push_images_promotes_recorded_digests as recorded_digests
from .test_push_images_promotes_recorded_digests import (
    REPO,
    _channel,
    _last_line,
    _manifest,
    _registry,
    _run,
    pytestmark,  # noqa: F401 -- the same skip when bash or jq is missing
    recorded,
)

DEPLOYED = ["swarm-api", "swarm-scheduler", "swarm-ui"]
SPEECH = "swarm-speech"

OFF = "voice_it = \"off\"\n\n# The speech machine: OFF.\nvoice_speech = {\n  enabled = false\n}\n"
ON = "voice_it = \"on\"\n\nvoice_speech = {\n  enabled = true\n  zone    = \"us-central1-a\"\n}\n"


@pytest.fixture(autouse=True)
def _with_speech(monkeypatch):
    monkeypatch.setattr(recorded_digests, "IMAGES", [*DEPLOYED, SPEECH])


def _release(tmp_path: Path, tfvars: str | None, **fakes: str):
    if tfvars is not None:
        env_dir = tmp_path / "repo" / "terraform" / "environments" / "dev"
        env_dir.mkdir(parents=True)
        (env_dir / "dev.tfvars").write_text(tfvars)
    summary = tmp_path / "summary.md"
    before = _registry()
    root, proc, registry, log = _run(
        tmp_path, before, _manifest(), GITHUB_STEP_SUMMARY=str(summary), **fakes
    )
    return before, root, proc, registry, log, summary


def _deployed(root: Path) -> dict[str, str]:
    manifest = json.loads((root / "build" / "deployed-images-dev.json").read_text())
    return {e["name"]: e["digest"] for e in manifest["images"]}


def test_a_refused_image_whose_switch_is_off_is_reported_and_the_rest_promote(tmp_path):
    before, root, proc, registry, log, summary = _release(tmp_path, OFF, FAKE_TRIVY_FAIL=SPEECH)
    assert proc.returncode == 0, proc.stderr[-3000:]

    assert SPEECH in {e["image"] for e in log if e["event"] == "scan"}, "swarm-speech was not scanned"
    moved = {e["image"] for e in log if e["event"] == "tag"}
    assert SPEECH not in moved, "a switched-off image was promoted"
    assert moved == set(DEPLOYED)
    channel = _channel(registry)
    assert channel[SPEECH] == _channel(before)[SPEECH]
    assert {i: channel[i] for i in DEPLOYED} == {i: recorded(i) for i in DEPLOYED}
    assert _deployed(root) == {i: recorded(i) for i in DEPLOYED}

    assert f"{SPEECH}: voice_speech is off" in proc.stderr and "REFUSED: trivy" in proc.stderr, proc.stderr[-3000:]
    report = summary.read_text()
    assert SPEECH in report and "REFUSED" in report, report


def test_the_same_refusal_with_the_switch_on_blocks_the_release(tmp_path):
    before, root, proc, registry, log, _ = _release(tmp_path, ON, FAKE_TRIVY_FAIL=SPEECH)
    assert proc.returncode != 0, "swarm-speech is deployed and refused, and the release went ahead"
    assert not [e for e in log if e["event"] == "tag"]
    assert _channel(registry) == _channel(before)
    assert not (root / "build" / "deployed-images-dev.json").exists()
    assert "refusing to promote a partial release" in _last_line(proc)
    assert SPEECH in _last_line(proc)


@pytest.mark.parametrize("tfvars", [OFF, ON, None], ids=["speech-off", "speech-on", "no-tfvars"])
def test_a_refused_deployed_image_still_blocks(tmp_path, tfvars):
    before, root, proc, registry, log, _ = _release(tmp_path, tfvars, FAKE_TRIVY_FAIL="swarm-ui")
    assert proc.returncode != 0, "swarm-ui is deployed and refused, and the release went ahead"
    assert not [e for e in log if e["event"] == "tag"]
    assert _channel(registry) == _channel(before)
    assert not (root / "build" / "deployed-images-dev.json").exists()
    assert "swarm-ui" in _last_line(proc) and SPEECH not in _last_line(proc), _last_line(proc)


def test_a_clean_switched_off_image_is_scanned_and_not_promoted(tmp_path):
    before, root, proc, registry, log, _ = _release(tmp_path, OFF)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SPEECH in {e["image"] for e in log if e["event"] == "scan"}
    assert SPEECH not in {e["image"] for e in log if e["event"] == "tag"}
    assert SPEECH not in _deployed(root)


@pytest.mark.parametrize(
    "tfvars",
    [
        None,
        "voice_speech = {\n  enabled = var.anything\n}\n",
        "voice_speech = {\n  # enabled = false\n  enabled = true\n}\n",
        "# voice_speech = { enabled = false }\n",
    ],
    ids=["no-tfvars", "not-a-literal", "commented-false", "commented-block"],
)
def test_anything_but_a_literal_false_counts_as_deployed(tmp_path, tfvars):
    _, root, proc, _, log, _ = _release(tmp_path, tfvars, FAKE_TRIVY_FAIL=SPEECH)
    assert proc.returncode != 0, f"swarm-speech's refusal did not block with tfvars {tfvars!r}"
    assert not [e for e in log if e["event"] == "tag"]
    assert SPEECH in _last_line(proc)


def test_a_one_line_off_block_switches_it_off(tmp_path):
    _, root, proc, _, _, _ = _release(tmp_path, "voice_speech = { enabled = false }\n", FAKE_TRIVY_FAIL=SPEECH)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SPEECH not in _deployed(root)


def test_the_fetched_private_tfvars_switches_it_off_with_none_in_the_checkout(tmp_path):
    """The release actions fetch the deployment's tfvars from the state bucket
    and pass the path as SWARM_TFVARS_FILE; the checkout holds none."""
    private = tmp_path / "runner-temp" / "dev.tfvars"
    private.parent.mkdir()
    private.write_text(OFF)
    _, root, proc, _, log, _ = _release(tmp_path, None, FAKE_TRIVY_FAIL=SPEECH, SWARM_TFVARS_FILE=str(private))
    assert not (root / "terraform").exists()
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert f"{SPEECH}: voice_speech is off" in proc.stderr, proc.stderr[-3000:]
    assert SPEECH not in {e["image"] for e in log if e["event"] == "tag"}
    assert SPEECH not in _deployed(root)


def test_the_fetched_private_tfvars_is_read_before_an_operators_copy(tmp_path):
    private = tmp_path / "runner-temp" / "dev.tfvars"
    private.parent.mkdir()
    private.write_text(ON)
    _, _, proc, _, log, _ = _release(tmp_path, OFF, FAKE_TRIVY_FAIL=SPEECH, SWARM_TFVARS_FILE=str(private))
    assert proc.returncode != 0, "the checkout's copy was read instead of the fetched one"
    assert not [e for e in log if e["event"] == "tag"]
    assert SPEECH in _last_line(proc)


def test_a_fetched_tfvars_for_another_environment_is_not_read(tmp_path):
    """SWARM_TFVARS_FILE is ENVIRONMENT's file: asked about another
    environment, the switch reads that environment's own copy instead."""
    private = tmp_path / "prod.tfvars"
    private.write_text(OFF)
    script = (
        'REPO_ROOT="$1"; source scripts/lib/image-switches.sh; '
        'printf "[%s]" "$(image_switched_off swarm-speech dev)"'
    )
    env = {**os.environ, "ENVIRONMENT": "prod", "SWARM_TFVARS_FILE": str(private)}
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path / "no-checkout")],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.stdout == "[]", (proc.stdout, proc.stderr)
    env["ENVIRONMENT"] = "dev"
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path / "no-checkout")],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.stdout == "[voice_speech]", (proc.stdout, proc.stderr)


def test_a_fetched_tfvars_that_names_no_file_counts_as_deployed_and_says_so(tmp_path):
    missing = tmp_path / "runner-temp" / "dev.tfvars"
    _, _, proc, _, log, _ = _release(tmp_path, OFF, FAKE_TRIVY_FAIL=SPEECH, SWARM_TFVARS_FILE=str(missing))
    assert proc.returncode != 0, "a missing fetched tfvars fell back to the checkout's copy"
    assert not [e for e in log if e["event"] == "tag"]
    assert "SWARM_TFVARS_FILE names no readable file" in proc.stderr, proc.stderr[-3000:]
    assert str(missing) not in proc.stderr
