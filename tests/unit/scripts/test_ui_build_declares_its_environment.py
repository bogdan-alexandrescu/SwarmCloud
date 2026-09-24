"""The swarm-ui bundle is told which environment it is.

WHY THIS IS A BUILD ARG AND NOT A CLOUD RUN ENV VAR, which is the thing a
reader will want to change: Vite inlines `import.meta.env.VITE_*` when the
bundle is COMPILED. By the time a container starts, a static bundle has already
decided what environment it thinks it is in, and setting the variable on the
service does nothing at all.

Without it, `classifyEnvironment` falls through to `unknown` and the product
header draws a loud full-width "ENVIRONMENT UNKNOWN" bar on every screen. That
bar is honest -- the build genuinely did not declare one -- but it is not what
a correctly configured deployment should show.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILD_SH = REPO / "scripts" / "build-images.sh"
DOCKERFILE = REPO / "images" / "swarm-ui" / "Dockerfile"


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
# Pull in just the function, not the whole script's side effects.
eval "$(sed -n '/^generate_config() {{/,/^}}/p' scripts/build-images.sh)"
generate_config images/{target}/Dockerfile IMAGE /dev/stdout {target}
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_ui_build_is_told_which_environment_it_is():
    yaml = _generated("swarm-ui", "dev")
    assert "VITE_SWARM_ENV=dev" in yaml, (
        "the swarm-ui image must be built with VITE_SWARM_ENV, or the header "
        "renders ENVIRONMENT UNKNOWN on every screen"
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


def test_the_dockerfile_declares_it_before_the_build_runs():
    """An ARG after `npm run build` is an ARG the build never saw, and an ARG
    after a FROM is scoped to that stage -- the failure this Dockerfile's own
    header records for NGINX_IMAGE."""
    text = DOCKERFILE.read_text()
    arg = text.index("ARG VITE_SWARM_ENV")
    env = text.index("ENV VITE_SWARM_ENV")
    build = text.index("RUN npm run build")
    assert arg < env < build, "declare, promote to ENV, then build -- in that order"


def test_it_does_not_default_to_a_guess():
    """The old `<span class="env">dev</span>` was hardcoded and said dev in
    production too. An unset build must say it does not know, loudly, rather
    than guess and be believed."""
    text = DOCKERFILE.read_text()
    m = re.search(r'ARG VITE_SWARM_ENV=(.*)', text)
    assert m and m.group(1).strip() in ('""', "''"), (
        "an unset build must fall through to the loud unknown, not to a quiet "
        f"default (found: {m.group(1) if m else 'no ARG'})"
    )
