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
test_build_images_concurrency.py, which fails the first N builds of one image
with a scripted build log (FAKE_FLAKY_*). Since CB-429-RETRY the script submits
with --async, so a failure arrives as a FAILURE status and the log is read back
with `builds log`; the fake models both that and the blocking submit.
"""

from __future__ import annotations

from pathlib import Path

from .test_build_images_concurrency import _run

# What a Cloud Build log says when a step's
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

TARGET = "swarm-api"


def _flaky_run(tmp_path: Path, log: str, times: int):
    log_file = tmp_path / "flaky.log"
    log_file.write_text(log + "\n")
    root, proc, events = _run(
        tmp_path,
        [TARGET, "swarm-scheduler"],
        parallel=2,
        seconds=0.2,
        FAKE_FLAKY_TARGET=TARGET,
        FAKE_FLAKY_TIMES=str(times),
        FAKE_FLAKY_LOG_FILE=str(log_file),
    )
    submits = len([e for e in events if e["event"] == "start" and e["target"] == TARGET])
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
