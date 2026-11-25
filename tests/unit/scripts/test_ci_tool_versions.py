"""The uv the project requires is the uv that ships (audit, 2026-11-21).

The root pyproject.toml's `[tool.uv] required-version` names the uv the images
copy in (images/*/Dockerfile, `ARG UV_IMAGE=astral-sh/uv:<version>@...`), so a
lock resolved or a sync made by any other uv is refused rather than differ
from the images' in silence.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
GITHUB = REPO / ".github"
SEMVER = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+")


def _documents() -> dict[Path, dict]:
    files = sorted(GITHUB.glob("workflows/*.yml")) + sorted(GITHUB.glob("actions/*/action.yml"))
    assert files, "no workflow read"
    return {p: yaml.safe_load(p.read_text()) or {} for p in files}


def _steps(doc: dict) -> list[dict]:
    steps = [s for job in (doc.get("jobs") or {}).values() for s in (job.get("steps") or [])]
    return steps + list((doc.get("runs") or {}).get("steps") or [])


def _image_uv() -> str:
    found = {}
    for dockerfile in sorted((REPO / "images").glob("*/Dockerfile")):
        for m in re.finditer(r"^ARG UV_IMAGE=astral-sh/uv:([0-9][0-9.]*)@sha256:[0-9a-f]{64}$",
                             dockerfile.read_text(), re.M):
            found[str(dockerfile.relative_to(REPO))] = m.group(1)
    assert found, "no image pins uv (ARG UV_IMAGE=astral-sh/uv:<version>@sha256:...)"
    assert len(set(found.values())) == 1, f"the images pin different uv versions: {found}"
    return next(iter(found.values()))


def test_every_setup_uv_installs_the_uv_the_images_run():
    """MUTATION: drop `version:` from any setup-uv step, or bump one image's
    UV_IMAGE without the workflows."""
    uv = _image_uv()
    seen = 0
    for path, doc in _documents().items():
        for step in _steps(doc):
            if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                seen += 1
                version = str((step.get("with") or {}).get("version", ""))
                assert version == uv, f"{path.relative_to(REPO)}: setup-uv installs {version or 'its default'}, the images {uv}"
    assert seen >= 10, f"only {seen} setup-uv steps read"



def test_the_project_requires_the_same_uv():
    """MUTATION: drop `required-version`, or let it name a range."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert project["tool"]["uv"].get("required-version") == f"=={_image_uv()}", project["tool"]["uv"]


def test_every_setup_kubectl_names_a_pinned_version():
    """MUTATION: drop `version:` from a setup-kubectl step, or set
    KUBECTL_VERSION to `latest`."""
    seen = 0
    for path, doc in _documents().items():
        env = doc.get("env") or {}
        for job in (doc.get("jobs") or {}).values():
            scope = {**env, **(job.get("env") or {})}
            for step in job.get("steps") or []:
                if not str(step.get("uses", "")).startswith("azure/setup-kubectl@"):
                    continue
                seen += 1
                version = str((step.get("with") or {}).get("version", ""))
                m = re.fullmatch(r"\$\{\{\s*env\.([A-Z_]+)\s*\}\}", version)
                if m:
                    version = str({**scope, **(step.get("env") or {})}.get(m.group(1), ""))
                assert SEMVER.fullmatch(version), f"{path.relative_to(REPO)}: setup-kubectl version {version!r}"
    assert seen, "no setup-kubectl step read"
