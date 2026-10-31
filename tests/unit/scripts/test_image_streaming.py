"""No runner image repeats a layer, so GKE can stream it, and a release refuses
one that does (IMG-STREAM; owner request 2026-10-13).

WHY. GKE Autopilot streams every eligible image, and "container images with
duplicate layers are not supported" (GKE: Image streaming, read 2026-10-13;
docs/browser-startup-speed.md §2.4). An image that is not streamed is pulled
whole before its first container starts: 42 s of the browser pod's 88 s cold
start on 2026-10-13.

WHAT REPEATED, read from Artifact Registry on 2026-10-13 (the 30 newest digests
of each image, manifest by manifest):

  * the empty layer `sha256:4f4fb700ef54...` in agent-runtime-browser and
    agent-runtime-indexer, at :dev and in most recent digests. BuildKit writes
    it for a `WORKDIR` that changes nothing, and both Dockerfiles restated the
    base's `WORKDIR /workspace`;
  * a ~100-byte layer holding only the `etc/` (and `tmp/`) directory entry, in 2
    of 30 base digests (and so in the browser and indexer built on them) and 7
    of 30 indexer digests. Every RUN leaves one, because BuildKit's mount points
    touch those directories, and its content is the directories' mtime. Two
    check-only RUNs that finish in the same second therefore write the SAME
    layer: the base's two `python -c` checks (layers 21 and 22), and the
    indexer's `--lsp-self-test` and graph `--self-test` (36 and 37 at :dev).

So the fix is a shape, held here: a derived runner image does not restate its
base's WORKDIR, and check-only steps that leave nothing behind run as ONE RUN.
The registry check itself (scripts/image-sizes.sh `--duplicate-layers`, in
test_image_sizes.py) catches whatever shape this file does not foresee.

WHAT THIS CANNOT PROVE: that GKE now streams the image. That is read after a
release, from a cold browser pod's `ImageStreaming` event and its
image_pulling -> image_pulled gap; the PR that added this file says how.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
IMAGES = REPO / "images"
BASE = IMAGES / "agent-runtime-base" / "Dockerfile"
BROWSER = IMAGES / "agent-runtime-browser" / "Dockerfile"
INDEXER = IMAGES / "agent-runtime-indexer" / "Dockerfile"
LIGHTPANDA = IMAGES / "agent-runtime-lightpanda" / "Dockerfile"
RELEASE_IMAGES = REPO / ".github" / "actions" / "release-images" / "action.yml"
RELEASE_PROMOTE = REPO / ".github" / "actions" / "release-promote" / "action.yml"


def _instructions(path: Path) -> list[str]:
    """The Dockerfile's instructions, continuations joined, comments and
    heredoc bodies dropped -- as Docker reads them."""
    out: list[str] = []
    current = ""
    heredoc = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        if raw.lstrip().startswith("#") or (not raw.strip() and not current):
            continue
        current += raw.rstrip("\\").strip() + " "
        if raw.rstrip().endswith("\\"):
            continue
        out.append(current.strip())
        marker = re.search(r"<<-?['\"]?([A-Za-z_]\w*)['\"]?", current)
        if marker:
            heredoc = marker.group(1)
        current = ""
    if current.strip():
        out.append(current.strip())
    return out


def _final_stage(path: Path) -> list[str]:
    steps = _instructions(path)
    last = max(i for i, step in enumerate(steps) if step.upper().startswith("FROM "))
    return steps[last:]


def _derived_runner_dockerfiles() -> list[Path]:
    found = sorted(p for p in IMAGES.glob("agent-runtime-*/Dockerfile")
                   if _final_stage(p)[0].split()[1] == "${BASE_IMAGE}")
    return found


def test_the_derived_runner_images_are_found():
    # The glob must not quietly match nothing: a rule over no files is no rule.
    assert _derived_runner_dockerfiles() == [BROWSER, INDEXER, LIGHTPANDA]


@pytest.mark.parametrize("dockerfile", _derived_runner_dockerfiles(), ids=lambda p: p.parent.name)
def test_a_derived_runner_image_does_not_restate_its_base_workdir(dockerfile):
    base_workdirs = [s.split(None, 1)[1] for s in _final_stage(BASE) if s.upper().startswith("WORKDIR ")]
    assert base_workdirs, "the base sets no WORKDIR; this rule has nothing to compare"
    inherited = base_workdirs[-1]
    restated = [s for s in _final_stage(dockerfile)
                if s.upper().startswith("WORKDIR ") and s.split(None, 1)[1] == inherited]
    # The base's WORKDIR is inherited. Restating it writes the empty layer the
    # base already has, and the image has a duplicate layer.
    assert not restated, f"{dockerfile.relative_to(REPO)} restates the base's WORKDIR {inherited}"


def _runs_containing(path: Path, needle: str) -> list[str]:
    return [s for s in _final_stage(path) if s.startswith("RUN ") and needle in s]


def test_the_base_checks_its_python_and_its_worker_import_in_one_run():
    # Two separate RUNs here wrote the same etc/-only layer in 2 of the 30
    # newest base digests (78b4cb46..., 9ae19db7...: layers 21 and 22).
    python = _runs_containing(BASE, "tarfile.data_filter is missing")
    worker = _runs_containing(BASE, "agent_worker.runners.mock")
    assert len(python) == 1 and python == worker, (python, worker)


def test_the_indexer_runs_its_self_tests_in_one_run():
    # Separate RUNs here wrote the same layer in 7 of the 30 newest indexer
    # digests (8af345ba... at :dev: layers 36 and 37).
    checks = ("swarm-repo-index --self-test", "swarm-repo-index --lsp-self-test",
              "swarm-repo-graph --self-test")
    runs = [_runs_containing(INDEXER, c) for c in checks]
    assert all(len(r) == 1 for r in runs) and runs[0] == runs[1] == runs[2], runs


def _steps(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["steps"]


def _gate_step() -> dict:
    found = [s for s in _steps(RELEASE_IMAGES) if "--duplicate-layers" in s.get("run", "")]
    assert len(found) == 1, "release-images has no single step running image-sizes.sh --duplicate-layers"
    return found[0]


def test_the_release_refuses_a_repeated_layer_before_anything_is_promoted():
    steps = _steps(RELEASE_IMAGES)
    gate = _gate_step()
    assert "./scripts/image-sizes.sh --duplicate-layers" in gate["run"]
    # The record the promotion promotes: the scanned one, every image in it.
    assert '"build/images-${ENVIRONMENT}.json"' in gate["run"]
    names = [s.get("name", "") for s in steps]
    scan = next(i for i, s in enumerate(steps) if "--scan-only" in s.get("run", ""))
    hand = names.index("hand the scanned record to the promotion")
    # After the record is scanned and BEFORE it is handed on: a record with a
    # repeated layer never reaches the promotion, so no channel tag moves.
    assert scan < steps.index(gate) < hand


def test_the_gate_holds_the_release_and_is_bounded():
    gate = _gate_step()
    # A refusal must fail the job, unlike the size record after the promotion.
    assert not gate.get("continue-on-error"), gate
    assert not gate.get("if"), "the gate runs whenever the scan ran"
    # A composite step has no timeout-minutes.
    assert gate["run"].lstrip().startswith("timeout ")
    for needle in ("print-access-token", "ACCESS_TOKEN", "Bearer"):
        assert needle not in gate["run"] + str(gate.get("env", {}))


def test_the_size_record_after_the_promotion_still_cannot_hold_the_release():
    # `--manifest` now exits 1 on a repeated layer too; after the promotion
    # that is a red step on the release page, never a held release.
    size = [s for s in _steps(RELEASE_PROMOTE) if "image-sizes.sh --manifest" in s.get("run", "")]
    assert len(size) == 1 and size[0].get("continue-on-error") is True, size
