"""`scripts/build-images.sh` rebuilds an image ONCE when its build failed
because a base-image pull got no answer, and never for any other failure.

THE FAILURE THIS PINS. On 2026-09-30 the `build images` job in
application.yml failed one image with

    Get "https://ghcr.io/v2/.../manifests/sha256:..": dial tcp ...:443: i/o timeout

while pulling a base image. Nothing about the image was wrong: the registry
never answered the connect. Owner decision, 2026-09-30: a network call that
got no answer at all is retried once; anything that did answer -- a failed
RUN step, a manifest-unknown, a refused push -- fails as it did.

Runs the real script against the fake `gcloud` of
test_build_images_concurrency.py, with a thin wrapper in front of it that
fails the first N `builds submit` calls for one image with a scripted build
log, and counts every submit of that image.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .test_build_images_concurrency import _run

# What Cloud Build streams back through `gcloud builds submit` when a step's
# `docker build` cannot reach the registry for its FROM image.
PULL_NO_ANSWER = (
    'Step #0: ERROR: failed to solve: ghcr.io/astral-sh/uv:0.4: failed to resolve '
    'source metadata: Get "https://ghcr.io/v2/astral-sh/uv/manifests/sha256:0123": '
    "dial tcp 140.82.112.33:443: i/o timeout\n"
    'ERROR: build step 0 "gcr.io/cloud-builders/docker" failed: step exited with non-zero status: 1'
)

# A build that got its answers and failed on its own merits.
BUILD_BROKEN = (
    'Step #0: ERROR: failed to solve: process "/bin/sh -c uv sync --frozen" did not '
    "complete successfully: exit code: 1\n"
    'ERROR: build step 0 "gcr.io/cloud-builders/docker" failed: step exited with non-zero status: 1'
)

WRAPPER = r"""#!/usr/bin/env bash
# Fails the first FLAKY_TIMES submits of FLAKY_TARGET with FLAKY_LOG, then
# hands everything to the fake gcloud behind it.
if [[ "${1:-} ${2:-}" == "builds submit" ]]; then
  config=""
  prev=""
  for a in "$@"; do
    if [[ "${prev}" == "--config" ]]; then config="${a}"; fi
    prev="${a}"
  done
  case "${config}" in
    *"cloudbuild-${FLAKY_TARGET}.yaml"|*"/images/${FLAKY_TARGET}/cloudbuild.yaml")
      n=$(( $(cat "${FLAKY_DIR}/submits" 2>/dev/null || echo 0) + 1 ))
      printf '%s\n' "${n}" >"${FLAKY_DIR}/submits"
      if [[ "${n}" -le "${FLAKY_TIMES}" ]]; then
        cat "${FLAKY_LOG_FILE}" >&2
        exit 1
      fi
      ;;
  esac
fi
exec "${REAL_GCLOUD}" "$@"
"""

TARGET = "swarm-api"


def _flaky_run(tmp_path: Path, log: str, times: int):
    wrap = tmp_path / "wrap"
    wrap.mkdir()
    gcloud = wrap / "gcloud"
    gcloud.write_text(WRAPPER)
    gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log_file = tmp_path / "flaky.log"
    log_file.write_text(log + "\n")
    # _run puts its fake gcloud in tmp_path/bin; the wrapper goes in front.
    root, proc, events = _run(
        tmp_path,
        [TARGET, "swarm-scheduler"],
        parallel=2,
        seconds=0.2,
        PATH=f"{wrap}{os.pathsep}{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        REAL_GCLOUD=str(tmp_path / "bin" / "gcloud"),
        FLAKY_TARGET=TARGET,
        FLAKY_TIMES=str(times),
        FLAKY_DIR=str(tmp_path),
        FLAKY_LOG_FILE=str(log_file),
    )
    submits_file = tmp_path / "submits"
    submits = int(submits_file.read_text().strip()) if submits_file.exists() else 0
    return root, proc, submits


def test_a_base_image_pull_that_got_no_answer_is_rebuilt_once(tmp_path):
    root, proc, submits = _flaky_run(tmp_path, PULL_NO_ANSWER, times=1)
    assert submits == 2, (
        f"{TARGET} was submitted {submits} time(s): its only build failed on a "
        "base-image pull that never got an answer (dial tcp ... i/o timeout), "
        "which is the failure of 2026-09-30 and is rebuilt once"
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert (root / "build" / "images-dev.json").exists(), (
        "the rebuild succeeded but no manifest was written"
    )


def test_a_second_pull_with_no_answer_fails_the_image(tmp_path):
    _, proc, submits = _flaky_run(tmp_path, PULL_NO_ANSWER, times=5)
    assert submits == 2, f"expected one rebuild and no more, got {submits} submit(s)"
    assert proc.returncode != 0, "the image failed twice and the script exited 0"
    assert TARGET in proc.stderr.splitlines()[-1], proc.stderr[-2000:]


def test_a_build_that_failed_on_its_own_merits_is_not_rebuilt(tmp_path):
    _, proc, submits = _flaky_run(tmp_path, BUILD_BROKEN, times=1)
    assert submits == 1, (
        f"{TARGET} failed a RUN step and was submitted again ({submits} submits): "
        "only a pull that got no answer is rebuilt"
    )
    assert proc.returncode != 0
    assert TARGET in proc.stderr.splitlines()[-1], proc.stderr[-2000:]
