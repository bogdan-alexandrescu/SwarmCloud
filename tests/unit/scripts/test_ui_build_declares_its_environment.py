"""Which environment the swarm-ui console is told, and where.

SINCE DAYONE-UIENV-1 THE IMAGE COMPILES NONE. Vite inlines
`import.meta.env.VITE_*` when the bundle is COMPILED, so a build arg made a dev
image and a prod image two different images. The bundle now reads its
environment at run time from `/config.json`, which the swarm-ui container's
entrypoint writes from its ENVIRONMENT variable; the two tests at the end of
this file hold the Dockerfile to that.

scripts/build-images.sh still passes `--build-arg VITE_SWARM_ENV=<env>` to the
swarm-ui build, and its incremental reuse and release logic still key on it;
the first three tests pin that until the lane that owns those scripts removes
it. With no ARG in the Dockerfile the arg is consumed by nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILD_SH = REPO / "scripts" / "build-images.sh"
DOCKERFILE = REPO / "images" / "swarm-ui" / "Dockerfile"
ENTRYPOINT_SCRIPT = REPO / "images" / "swarm-ui" / "40-swarm-ui-config.sh"


def _generated(target: str, environment: str) -> str:
    """The cloudbuild yaml build-images.sh would write for `target`."""
    script = f"""
set -euo pipefail
cd {REPO}
ENVIRONMENT={environment}
# Stub the bits generate_config reads that need a cloud or a clock.
iso_now() {{ printf '1970-01-01T00:00:00Z'; }}
BUILD_TIMEOUT=1200s
BUILD_MACHINE=E2_HIGHCPU_8
TAG=testtag
BUILD_ONLY=0
LOCAL_BUILD=0
REPO_ROOT={REPO}
# The project whose build mirrors the config names (DAYONE-DESAGA-1).
PROJECT_ID=swarm-test-project
REGION=us-central1
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
source scripts/lib/build-mirror.sh
# Pull in just the functions, not the whole script's side effects. The
# per-target build args are their own function, shared with --build-only's
# chained config, so both are read.
eval "$(sed -n '/^target_build_args() {{/,/^}}/p' scripts/build-images.sh)"
eval "$(sed -n '/^mirror_step_for() {{/,/^}}/p' scripts/build-images.sh)"
eval "$(sed -n '/^generate_config() {{/,/^}}/p' scripts/build-images.sh)"
generate_config images/{target}/Dockerfile IMAGE /dev/stdout {target}
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_ui_build_is_told_which_environment_it_is():
    yaml = _generated("swarm-ui", "dev")
    assert "VITE_SWARM_ENV=dev" in yaml, (
        "build-images.sh still passes VITE_SWARM_ENV to the swarm-ui build "
        "(see the module docstring); remove this test with it"
    )
    # It must be a --build-arg, not something a service could override later.
    assert re.search(r"--build-arg\s*\n\s*-\s*VITE_SWARM_ENV=", yaml), (
        "it must be a BUILD arg: Vite inlines import.meta.env.VITE_* at compile "
        "time, so a run-time variable is read by nothing"
    )


def test_the_environment_is_not_hardcoded_to_dev():
    """A prod build must say prod. The whole point of the badge is that an
    operator can tell which environment they are about to change."""
    assert "VITE_SWARM_ENV=prod" in _generated("swarm-ui", "prod")


def test_no_other_image_carries_it():
    """Only the UI compiles a bundle. Passing it elsewhere would be cargo."""
    for target in ("swarm-api", "swarm-scheduler"):
        assert "VITE_SWARM_ENV" not in _generated(target, "dev"), target


def test_the_dockerfile_compiles_no_environment():
    """DAYONE-UIENV-1: the image compiles no environment. The bundle reads it
    at run time from `/config.json`, which the container's entrypoint writes
    from its own ENVIRONMENT (apps/swarm-ui/src/runtimeConfig.ts), so one image
    serves every deployment. The build arg above is therefore consumed by
    nothing in the image; it stays only until build-images.sh's reuse and
    release logic stop keying on it (a question in that lane's PR)."""
    code = "\n".join(
        line for line in DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "VITE_SWARM_ENV" not in code, "an ARG or ENV here compiles the environment back in"


def test_the_container_writes_its_environment_at_start():
    """The other half: with nothing compiled in, the running container is what
    says which environment it is, or the console says ENVIRONMENT UNKNOWN."""
    text = DOCKERFILE.read_text()
    assert (
        "COPY images/swarm-ui/40-swarm-ui-config.sh /docker-entrypoint.d/40-swarm-ui-config.sh" in text
    )
    assert "${ENVIRONMENT:-}" in ENTRYPOINT_SCRIPT.read_text()


def test_the_entrypoint_script_is_executable_by_its_own_mode_not_a_chmod_layer():
    """nginx's entrypoint runs only executable scripts in /docker-entrypoint.d, so
    the script must be executable in the image. It is: git commits it as 100755
    and COPY keeps the mode. A `RUN chmod` after the COPY would change nothing,
    and its layer would be byte-identical to the COPY's. The release's layer
    check refuses an image that repeats a layer (GKE image streaming will not
    stream it), and that refusal stopped the release of c08dc1e75 on
    2026-10-29."""
    mode = subprocess.run(
        ["git", "ls-files", "-s", str(ENTRYPOINT_SCRIPT.relative_to(REPO))],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    assert mode == "100755", f"commit {ENTRYPOINT_SCRIPT.name} executable (git mode {mode})"
    code = [line for line in DOCKERFILE.read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not any("chmod" in line and "40-swarm-ui-config.sh" in line for line in code), (
        "a RUN chmod of the already-executable script repeats the COPY's layer"
    )
