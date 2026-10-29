"""A Dockerfile Cloud Build builds without BuildKit uses nothing only BuildKit reads.

scripts/build-images.sh writes a Cloud Build config for every image with no
checked-in cloudbuild.yaml (generate_config). Its one step is
`gcr.io/cloud-builders/docker build`, and it sets DOCKER_BUILDKIT=1 only for a
Dockerfile that bind-mounts the tool mirror; every other image is built by the
classic builder. A pull request's `build-check` builds the same Dockerfile with
`docker buildx build` (BuildKit) instead, so a BuildKit-only line passes the
pull request and fails only main's build.

That is how swarm-ui broke main's `build images` job from 16:01Z on 2026-10-29
(SWARM-UI-IMAGE-FIX): DAYONE-UIENV-1 (#2744) wrote the entrypoint script with
a `RUN cat > ... <<"EOF"` heredoc. Heredocs are a BuildKit Dockerfile feature;
the classic builder does not run the body. The script is now a file in
images/swarm-ui, copied in.

This test generates each such config the way the script does and, for every
one that leaves the classic builder in charge, refuses a heredoc or a
BuildKit-only instruction flag in its Dockerfile.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BUILD_SH = REPO / "scripts" / "build-images.sh"

# Instruction flags the classic builder refuses or ignores.
BUILDKIT_FLAGS = ("--mount", "--chmod", "--link", "--parents", "--exclude", "--network", "--security", "--checksum")
HEREDOC = re.compile(r"<<-?\s*[\"']?[A-Za-z_][A-Za-z0-9_]*[\"']?")


def _targets() -> list[str]:
    m = re.search(r"^ALL_TARGETS=\(([^)]*)\)", BUILD_SH.read_text(), re.M)
    assert m, "ALL_TARGETS not found in scripts/build-images.sh"
    return m.group(1).split()


def _generated_dockerfile(target: str) -> str | None:
    """The Dockerfile a GENERATED config builds for `target`, or None when a
    checked-in cloudbuild.yaml is the recipe (that file sets its own builder)."""
    recipe = "agent-runtime-base" if target == "agent-runtime-base-nocodex" else target
    for base in ("images", "apps"):
        if (REPO / base / recipe / "cloudbuild.yaml").is_file():
            return None
    for base in ("images", "apps", "docker"):
        if (REPO / base / recipe / "Dockerfile").is_file():
            return f"{base}/{recipe}/Dockerfile"
    return None


def _generated(dockerfile: str, target: str) -> str:
    """The cloudbuild yaml build-images.sh would write for `target`."""
    script = f"""
set -euo pipefail
cd {REPO}
ENVIRONMENT=dev
iso_now() {{ printf '1970-01-01T00:00:00Z'; }}
BUILD_TIMEOUT=1200s
BUILD_MACHINE=E2_HIGHCPU_8
TAG=testtag
BUILD_ONLY=0
LOCAL_BUILD=0
REPO_ROOT={REPO}
PROJECT_ID=swarm-test-project
REGION=us-central1
APT_REFRESH=testtag
BUILD_MIRROR_BUCKET=swarm-test-bucket
die() {{ printf '%s\\n' "$*" >&2; exit 1; }}
source scripts/lib/build-mirror.sh
for fn in target_build_args apt_build_args mirror_build_args_yaml mirror_step_for generate_config; do
  body="$(sed -n "/^${{fn}}() {{/,/^}}/p" scripts/build-images.sh)"
  if [[ -n "${{body}}" ]]; then eval "${{body}}"; fi
done
generate_config {dockerfile} IMAGE /dev/stdout {target}
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _instructions(dockerfile: Path) -> list[str]:
    """Each instruction as one line: comments dropped, continuations joined,
    heredoc bodies skipped so their text is not read as instructions."""
    out: list[str] = []
    cont = ""
    heredoc: str | None = None
    for raw in dockerfile.read_text().splitlines():
        if heredoc is not None:
            if raw.strip() == heredoc:
                heredoc = None
            continue
        line = raw.strip()
        if not cont and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            cont += line[:-1] + " "
            continue
        full = cont + line
        cont = ""
        out.append(full)
        m = HEREDOC.search(full)
        if m and full.split(None, 1)[0].upper() in ("RUN", "COPY", "ADD"):
            heredoc = re.sub(r"^<<-?\s*", "", m.group(0)).strip("\"'")
    if cont:
        out.append(cont)
    return out


def _buildkit_only(instruction: str) -> str | None:
    words = instruction.split()
    keyword = words[0].upper()
    if keyword not in ("RUN", "COPY", "ADD"):
        return None
    if HEREDOC.search(instruction):
        return "a heredoc"
    for word in words[1:]:
        if not word.startswith("--"):
            break
        flag = word.split("=", 1)[0]
        if flag in BUILDKIT_FLAGS:
            return f"{keyword} {flag}"
    return None


def test_the_generated_configs_are_found():
    found = [t for t in _targets() if _generated_dockerfile(t)]
    assert "swarm-ui" in found, found
    assert len(found) >= 5, found


def test_a_classic_builder_dockerfile_uses_nothing_only_buildkit_reads():
    offenders = []
    visited = 0
    for target in _targets():
        dockerfile = _generated_dockerfile(target)
        if dockerfile is None:
            continue
        if "DOCKER_BUILDKIT=1" in _generated(dockerfile, target):
            continue
        visited += 1
        for instruction in _instructions(REPO / dockerfile):
            why = _buildkit_only(instruction)
            if why:
                offenders.append(f"{dockerfile} ({target}): {why}: {instruction[:120]}")
    assert visited, "no image is built by the classic builder; this test checks nothing"
    assert not offenders, (
        "Cloud Build builds these with the classic builder (generate_config sets "
        "DOCKER_BUILDKIT=1 only for a tool-mirror Dockerfile), which does not run "
        "BuildKit-only Dockerfile syntax; the pull request's buildx build-check "
        "passes them, main's build fails:\n" + "\n".join(offenders)
    )


def test_the_heredoc_reader_sees_a_heredoc(tmp_path):
    """The check above must not pass because it cannot see one."""
    df = tmp_path / "Dockerfile"
    df.write_text('FROM x\nRUN cat > /a.sh <<"EOF" && chmod 0755 /a.sh\n#!/bin/sh\nset -eu\nEOF\nUSER 101\n')
    found = [_buildkit_only(i) for i in _instructions(df)]
    assert found == [None, "a heredoc", None]
