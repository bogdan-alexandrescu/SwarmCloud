"""Every image's wheelhouse install carries the whole local closure of what it installs.

WHY THIS EXISTS. On 2026-10-31 main's release run for 73a41dede shipped
nothing: application.yml's build refused swarm-graph (Cloud Build
83b66252-962e-42ac-bbc1-f2305861d767) at `uv pip check`, with "The package
`swarm-agent-worker` requires `swarm-workflow-rollup`, but it's not installed".
The worker had depended on the rollup since the derived-state change of
2026-10-30 (#2838, pull request 2853), and every other image that installs a
package depending on it was given the rollup's wheel then. apps/swarm-graph's
Dockerfile installs the worker too and was missed. Its image was reused from an
ancestor build until a lock change forced a full build, so the gap stayed
invisible for a day and then stopped the second release candidate.

WHAT IS ASSERTED. For each Dockerfile under apps/ and images/ that installs our
own packages from the wheelhouse (`--no-index --find-links /wheels --no-deps`),
every local package those depend on -- read from each pyproject's
`[project] dependencies` and its `[tool.uv.sources]` path entries, followed to
the end -- is installed by the same line and has its wheel built from
/src/apps/<dir>. `--no-deps` means nothing else would bring it in.

NOT asserted: third-party packages. They come from each lock's hashed export,
and `uv pip check` in the build reads those.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: `uv pip install ... --no-index --find-links /wheels --no-deps \` and the names on the next line.
_INSTALL = re.compile(r"--no-index --find-links /wheels --no-deps \\\n((?:[ \t]+[a-z0-9-]+)+)")


def _local_packages() -> dict[str, tuple[str, set[str]]]:
    """Each local project's name -> (its directory under apps/, the local projects it depends on)."""
    found: dict[str, tuple[str, set[str]]] = {}
    for pyproject in sorted(REPO.glob("apps/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text())
        project = data.get("project", {})
        if "name" not in project:
            continue
        sources = data.get("tool", {}).get("uv", {}).get("sources", {})
        local = {name for name, source in sources.items() if isinstance(source, dict) and "path" in source}
        wanted = {re.split(r"[<>=!~;\[ ]", dep, maxsplit=1)[0].strip() for dep in project.get("dependencies", [])}
        found[project["name"]] = (pyproject.parent.name, wanted & local)
    return found


def _closure(names: set[str], packages: dict[str, tuple[str, set[str]]]) -> set[str]:
    seen: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name in seen or name not in packages:
            continue
        seen.add(name)
        todo.extend(packages[name][1])
    return seen


def _wheelhouse_installs() -> list[tuple[Path, set[str]]]:
    installs = []
    for dockerfile in sorted([*REPO.glob("apps/*/Dockerfile"), *REPO.glob("images/*/Dockerfile")]):
        for match in _INSTALL.finditer(dockerfile.read_text()):
            installs.append((dockerfile, set(match.group(1).split())))
    return installs


def test_the_scan_finds_the_wheelhouse_installs() -> None:
    """A guard over nothing passes: the images known to install from the wheelhouse are read."""
    read = {dockerfile.relative_to(REPO).as_posix() for dockerfile, _ in _wheelhouse_installs()}
    for expected in (
        "apps/swarm-graph/Dockerfile",
        "images/agent-runtime-base/Dockerfile",
        "images/swarm-api/Dockerfile",
        "images/swarm-scheduler/Dockerfile",
        "images/swarm-reconciler/Dockerfile",
    ):
        assert expected in read, f"{expected}'s wheelhouse install was not found; the pattern no longer matches it"


def test_every_wheelhouse_install_carries_its_local_closure() -> None:
    packages = _local_packages()
    assert "swarm-workflow-rollup" in packages, "the local project list was not read"
    missing = {}
    for dockerfile, installed in _wheelhouse_installs():
        gap = _closure(installed, packages) - installed
        if gap:
            missing[dockerfile.relative_to(REPO).as_posix()] = sorted(gap)
    assert missing == {}, f"installed with --no-deps but their local dependencies are not: {missing}"


def test_every_installed_local_package_has_its_wheel_built() -> None:
    packages = _local_packages()
    unbuilt = {}
    for dockerfile, installed in _wheelhouse_installs():
        text = dockerfile.read_text()
        gap = sorted(
            name
            for name in installed
            if name in packages and f"--out-dir /wheels /src/apps/{packages[name][0]}" not in text
        )
        if gap:
            unbuilt[dockerfile.relative_to(REPO).as_posix()] = gap
    assert unbuilt == {}, f"installed from /wheels but no wheel is built for: {unbuilt}"
