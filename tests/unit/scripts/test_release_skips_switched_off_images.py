"""A dev release neither rebuilds nor scans an image dev switches off.

MEASURED. The release of main's commit of 2026-11-06 11:28 UTC waited 16
minutes for main's build, rebuilt 18 images (2 to 5 minutes each, four at a
time) and swarm-speech alone in 28 minutes 50 seconds, then scanned for about
7 minutes. Nothing in dev runs swarm-speech: the speech machine exists only
while `voice_speech.enabled`, and dev.tfvars has it off. Bogdan's pick, 6
November 2026: skip it in releases.

Asserted against the REAL scripts, with a real tfvars file in the copied
repository:

  * `build-images.sh --reuse-ci` (only and or-build) in dev with voice_speech
    off does not build swarm-speech, leaves it out of build/images-dev.json,
    says so in one line, and still builds or reuses the other 18;
  * the same release with voice_speech on, or with a switch that cannot be
    read, builds it as today; so does prod, whatever its switch says;
  * `push-images.sh --manifest` with a manifest that does not hold
    swarm-speech, its switch off, scans and promotes the rest and reports it
    as not deployed: not rebuilt, not scanned;
  * a manifest that holds it, switch on or unreadable, scans it as today.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from . import test_push_images_promotes_recorded_digests as recorded_digests
from .test_build_images_incremental import _submitted
from .test_ci_built_images import IMAGES, ci_run
from .test_push_images_promotes_recorded_digests import _channel, _manifest, _registry, _run, recorded
from .test_release_rebuilds_stale_images import Release

SPEECH = "swarm-speech"
OTHERS = [i for i in IMAGES if i != SPEECH]

OFF = "voice_it = \"off\"\n\n# The speech machine: OFF.\nvoice_speech = {\n  enabled = false\n}\n"
ON = "voice_it = \"on\"\n\nvoice_speech = {\n  enabled = true\n  zone    = \"us-central1-a\"\n}\n"
NOT_A_LITERAL = "voice_speech = {\n  enabled = var.anything\n}\n"

pytestmark = recorded_digests.pytestmark


def _tfvars(root: Path, env: str, text: str | None, *, mode: int | None = None) -> None:
    if text is None:
        return
    path = root / "terraform" / "environments" / env / f"{env}.tfvars"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode is not None:
        path.chmod(mode)


# ---------------------------------------------------------------------------
# build-images.sh --reuse-ci
# ---------------------------------------------------------------------------

def _release(box: Release, mode: str, *, env: str = "dev", runs: bool = True, tfvars_mode: int | None = None):
    """`build-images.sh --reuse-ci MODE` at HEAD. Every digest in CI's record
    is stale (scripts/lib/common.sh changed since), so each image the release
    keeps is rebuilt -- the 11:28 release's shape. runs=False: CI never built
    this commit for ENV."""
    deployed = box.first
    head = box.commit("N: how every image is built", box.append("scripts/lib/common.sh"))
    if tfvars_mode is not None:
        # After the commit: git cannot add a file it cannot read.
        (box.root / "terraform" / "environments" / env / f"{env}.tfvars").chmod(tfvars_mode)
    record = json.loads(box.record(head, environment=env).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], deployed))
    world = {"runs": [ci_run(901, sha=head, record=record)] if runs else []}
    (box.tmp / "world.json").write_text(json.dumps(world))
    (box.tmp / "events.jsonl").unlink(missing_ok=True)
    manifest_path = box.root / "build" / f"images-{env}.json"
    manifest_path.unlink(missing_ok=True)
    proc = subprocess.run(
        ["bash", str(box.root / "scripts" / "build-images.sh"), "--reuse-ci", mode],
        env={**box.env, "ENVIRONMENT": env}, capture_output=True, text=True, timeout=300, check=False,
    )
    path = box.tmp / "events.jsonl"
    events = [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    return proc, manifest, events


@pytest.fixture
def box(tmp_path: Path) -> Release:
    return Release(tmp_path)


@pytest.mark.parametrize("mode", ["only", "or-build"])
def test_a_dev_release_with_speech_off_does_not_build_it_and_leaves_it_out(box, mode):
    _tfvars(box.root, "dev", OFF)
    proc, manifest, events = _release(box, mode)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(OTHERS), f"submitted {sorted(_submitted(events))}"
    assert manifest is not None
    assert [i["name"] for i in manifest["images"]] == OTHERS
    said = [l for l in proc.stderr.splitlines() if SPEECH in l and "not deployed in dev" in l]
    assert len(said) == 1, proc.stderr[-4000:]
    assert "voice_speech" in said[0] and "not rebuilt" in said[0], said


def test_a_dev_release_ci_never_built_skips_it_too(box):
    _tfvars(box.root, "dev", OFF)
    proc, manifest, events = _release(box, "or-build", runs=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(OTHERS), f"submitted {sorted(_submitted(events))}"
    assert [i["name"] for i in manifest["images"]] == OTHERS


def test_a_release_whose_ci_record_is_current_promotes_it_without_speech(box):
    """Nothing to rebuild: CI's record is promoted as fetched, less the
    image dev does not run, so the promotion does not scan it either."""
    _tfvars(box.root, "dev", OFF)
    head = box.commit("docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set()
    assert [i["name"] for i in manifest["images"]] == OTHERS
    assert manifest["images"] == [i for i in record["images"] if i["name"] != SPEECH]


@pytest.mark.parametrize(
    ("tfvars", "mode"),
    [(ON, None), (None, None), (NOT_A_LITERAL, None), (OFF, 0o000)],
    ids=["speech-on", "no-tfvars", "not-a-literal", "unreadable-file"],
)
def test_a_dev_release_whose_switch_is_not_literally_off_builds_it(box, tfvars, mode):
    if mode == 0o000 and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    if mode is not None:
        # Untracked: git status cannot read it either, and the build would
        # call the tree dirty.
        with (box.root / ".git" / "info" / "exclude").open("a") as fh:
            fh.write("/terraform/\n")
    _tfvars(box.root, "dev", tfvars)
    proc, manifest, events = _release(box, "only", tfvars_mode=mode)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(IMAGES), f"submitted {sorted(_submitted(events))}"
    assert [i["name"] for i in manifest["images"]] == IMAGES
    assert "not deployed in dev" not in proc.stderr


def test_a_prod_release_builds_it_whatever_its_switch_says(box):
    _tfvars(box.root, "prod", OFF)
    proc, manifest, events = _release(box, "or-build", env="prod", runs=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert SPEECH in _submitted(events)
    assert SPEECH in [i["name"] for i in manifest["images"]]


# ---------------------------------------------------------------------------
# push-images.sh --manifest
# ---------------------------------------------------------------------------

PUSHED = ["swarm-api", "swarm-scheduler", "swarm-ui"]


@pytest.fixture
def _with_speech(monkeypatch):
    monkeypatch.setattr(recorded_digests, "IMAGES", [*PUSHED, SPEECH])


def _promote(tmp_path: Path, tfvars: str | None, *, holds_speech: bool, mode: int | None = None):
    _tfvars(tmp_path / "repo", "dev", tfvars, mode=mode)
    manifest = _manifest()
    if not holds_speech:
        manifest["images"] = [i for i in manifest["images"] if i["name"] != SPEECH]
    summary = tmp_path / "summary.md"
    before = _registry()
    root, proc, registry, log = _run(tmp_path, before, manifest, GITHUB_STEP_SUMMARY=str(summary))
    return before, root, proc, registry, log, summary


def _deployed(root: Path) -> list[str]:
    return [e["name"] for e in json.loads((root / "build" / "deployed-images-dev.json").read_text())["images"]]


@pytest.mark.usefixtures("_with_speech")
def test_a_manifest_without_switched_off_speech_scans_and_promotes_the_rest(tmp_path):
    before, root, proc, registry, log, summary = _promote(tmp_path, OFF, holds_speech=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert {e["image"] for e in log if e["event"] == "scan"} == set(PUSHED)
    assert {e["image"] for e in log if e["event"] == "tag"} == set(PUSHED)
    assert _channel(registry)[SPEECH] == _channel(before)[SPEECH]
    assert sorted(_deployed(root)) == sorted(PUSHED)
    assert {i: _channel(registry)[i] for i in PUSHED} == {i: recorded(i) for i in PUSHED}
    assert f"{SPEECH}: not deployed in dev: not rebuilt, not scanned" in proc.stderr, proc.stderr[-3000:]
    assert SPEECH in summary.read_text() and "not scanned" in summary.read_text()


@pytest.mark.usefixtures("_with_speech")
@pytest.mark.parametrize(
    ("tfvars", "mode"),
    [(ON, None), (NOT_A_LITERAL, None), (OFF, 0o000)],
    ids=["speech-on", "not-a-literal", "unreadable-file"],
)
def test_a_manifest_holding_speech_with_its_switch_not_off_scans_and_promotes_it(tmp_path, tfvars, mode):
    if mode == 0o000 and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    _, root, proc, registry, log, _ = _promote(tmp_path, tfvars, holds_speech=True, mode=mode)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SPEECH in {e["image"] for e in log if e["event"] == "scan"}
    assert SPEECH in {e["image"] for e in log if e["event"] == "tag"}
    assert SPEECH in _deployed(root)
    assert "not rebuilt, not scanned" not in proc.stderr
