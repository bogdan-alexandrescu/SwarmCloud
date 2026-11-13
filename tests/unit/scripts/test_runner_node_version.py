"""Every coding runner's Node is a stated version, and the build proves it.

WHY THIS EXISTS. agent-runtime-browser, -lightpanda and -indexer are built
FROM images/agent-runtime-base, so that image's one `ARG NODE_IMAGE` digest is
every runner's Node. Until #3929 the digest was the only statement of it: it
resolved to Node 24.21.0, below the 24.22.1 floor ContextOS's CLI startup guard
enforces (it exits 78 below it), and nothing outside the digest said so.

WHAT IS ASSERTED (offline; reads the Dockerfile only):

  * exactly one `ARG NODE_VERSION=<x.y.z>`, with 24.22.1 <= version < 25.0.0,
    compared as integer tuples -- the version the digest carries, and the one
    swarm-api and the sc bridge state;
  * NODE_IMAGE is `node:24-...@sha256:<64 hex>`, a digest on the 24 line;
  * after the shipped (last) stage copies /usr/local/bin/node out of `nodebin`, a RUN fails
    the build unless `node --version` equals `v${NODE_VERSION}`, with
    NODE_VERSION re-declared as an ARG inside that stage so it is in scope
    (a global ARG is only visible to FROM lines).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BASE = REPO / "images" / "agent-runtime-base" / "Dockerfile"

FLOOR = (24, 22, 1)
CEILING = (25, 0, 0)
NODE_COPY = "COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node"


def _text() -> str:
    return BASE.read_text()


def _version_args(text: str) -> list[str]:
    return re.findall(r"^ARG NODE_VERSION=(\S+)\s*$", text, re.MULTILINE)


def _shipped_stage(text: str) -> str:
    """The last build stage (FROM to end of file): the image every runner pulls.

    An earlier build-only stage copies node too (the security audit's), but
    only the last stage is the runner image.
    """
    stage = re.split(r"^FROM\s", text, flags=re.MULTILINE)[-1]
    assert NODE_COPY in stage, "the shipped stage no longer copies node from nodebin"
    return stage


def _instructions(stage: str) -> list[str]:
    """The stage's instructions, continuation lines joined, comments dropped."""
    out: list[str] = []
    current = ""
    for line in stage.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        current += line.rstrip("\\").rstrip() + " "
        if not line.rstrip().endswith("\\"):
            out.append(current.strip())
            current = ""
    if current:
        out.append(current.strip())
    return out


def test_one_node_version_arg_on_the_24_line_at_or_above_the_floor():
    versions = _version_args(_text())
    assert len(versions) == 1, versions
    assert re.fullmatch(r"\d+\.\d+\.\d+", versions[0]), versions[0]
    parsed = tuple(int(part) for part in versions[0].split("."))
    assert FLOOR <= parsed < CEILING, versions[0]


def test_node_image_is_a_24_line_digest():
    images = re.findall(r"^ARG NODE_IMAGE=(\S+)\s*$", _text(), re.MULTILINE)
    assert len(images) == 1, images
    assert re.fullmatch(r"node:24-[a-z0-9.-]+@sha256:[0-9a-f]{64}", images[0]), images[0]


def test_the_build_fails_unless_the_copied_node_is_node_version():
    text = _text()
    stage = _shipped_stage(text)
    steps = _instructions(stage)
    copy_at = next(i for i, step in enumerate(steps) if step == NODE_COPY)

    # NODE_VERSION is declared inside this stage (bare or with the same default)
    # before the check runs, or `${NODE_VERSION}` expands to an empty string.
    declared = [
        i for i, step in enumerate(steps) if re.fullmatch(r"ARG NODE_VERSION(=\S+)?", step)
    ]
    checks = [
        i
        for i, step in enumerate(steps)
        if i > copy_at
        and step.startswith("RUN ")
        and "node --version" in step
        and re.search(r'"?v\$\{?NODE_VERSION\}?"?', step)
    ]
    assert checks, "no RUN after the node COPY compares `node --version` to v${NODE_VERSION}"
    assert declared and min(declared) < checks[0], "NODE_VERSION is not an ARG in scope"

    check = steps[checks[0]]
    # The comparison must FAIL the build on a mismatch, not just print.
    assert re.search(r"exit 1|false|\btest\b|\[ ", check), check
    assert re.search(r'!=|-ne|=\s*"v\$\{?NODE_VERSION', check), check


def test_stage_arg_has_no_default_of_its_own_that_could_drift():
    """Inside the stage NODE_VERSION is bare, so the global value is the one checked."""
    stage = _shipped_stage(_text())
    for step in _instructions(stage):
        if step.startswith("ARG NODE_VERSION"):
            assert step == "ARG NODE_VERSION", step
