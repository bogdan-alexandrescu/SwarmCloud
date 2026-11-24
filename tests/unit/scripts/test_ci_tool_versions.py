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

REPO = Path(__file__).resolve().parents[3]


def _image_uv() -> str:
    found = {}
    for dockerfile in sorted((REPO / "images").glob("*/Dockerfile")):
        for m in re.finditer(r"^ARG UV_IMAGE=astral-sh/uv:([0-9][0-9.]*)@sha256:[0-9a-f]{64}$",
                             dockerfile.read_text(), re.M):
            found[str(dockerfile.relative_to(REPO))] = m.group(1)
    assert found, "no image pins uv (ARG UV_IMAGE=astral-sh/uv:<version>@sha256:...)"
    assert len(set(found.values())) == 1, f"the images pin different uv versions: {found}"
    return next(iter(found.values()))


def test_the_project_requires_the_same_uv():
    """MUTATION: drop `required-version`, or let it name a range."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert project["tool"]["uv"].get("required-version") == f"=={_image_uv()}", project["tool"]["uv"]
