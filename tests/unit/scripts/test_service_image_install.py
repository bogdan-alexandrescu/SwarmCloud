"""The four control-plane images and the agent runtime install third-party
packages from uv.lock by hash, and our own packages from the local wheelhouse
only (#663).

THE DEFECT THIS PINS. Every service image installed with
`uv pip install --find-links /wheels <service>`. `--find-links` ADDS the
wheelhouse and keeps PyPI as an index, so:

  * every internal name (`swarm-common`, `swarm-redaction`, the service) was
    looked up on pypi.org, and main's `build images` failed on 3a0d488c
    (run 37293655337, 2026-10-05) when that lookup got a 503;
  * a package published on PyPI under one of those names at a higher version
    could be installed instead of ours -- dependency confusion into every
    control-plane image;
  * third-party dependencies were resolved at build time, unpinned, although
    the repository has a uv.lock.

images/agent-runtime-base installed `swarm-agent-worker` the same way, so the
same properties hold for it. images/agent-runtime-indexer installs none of our
packages (it is built FROM agent-runtime-base and adds a hashed tree-sitter
environment), so for it the assertion is that it stays that way, and no stage
of any image passes `--find-links` without `--no-index`.

The properties asserted here, against the real Dockerfiles and the real
uv.lock:

  * no `uv pip install` in a builder stage passes `--find-links` without
    `--no-index`;
  * the third-party install is `--require-hashes --no-deps -r <file>`, and that
    file is written by `uv export --frozen` with hashes, no dev group, and no
    local package in it;
  * the export prunes exactly the internal packages the service does not
    depend on (read from uv.lock), and the internal install names exactly
    the ones it does -- so the image carries the service's own dependency set,
    no more and no less;
  * the export command, run as written against a build context holding only
    what the Dockerfile COPYs, emits a hash for every requirement and exactly
    the third-party closure uv.lock records for the service;
  * every builder RUN is valid shell (`bash -n`), since images are not built on
    a pull request (#650) and a typo would first surface on main;
  * `.dockerignore` lets uv.lock into the build context.

WHAT THIS CANNOT PROVE: that the images build. Only the `build images` job on
main builds them.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# image -> the uv.lock package that image ships.
SERVICES = {
    "swarm-api": "swarm-api",
    "swarm-quota-broker": "swarm-quota-broker",
    "swarm-reconciler": "swarm-reconciler",
    "swarm-scheduler": "swarm-scheduler",
    "agent-runtime-base": "swarm-agent-worker",
}

# image -> packages the builder may install OUTSIDE the two #663 installs, by
# name from an index. agent-runtime-base keeps pip in its venv for the agent, so
# it raises the venv's setuptools and wheel to their patched floors and adds
# pytest for the generic runner's `python -m pytest`. None is one of our names,
# so none can be substituted by a same-named public package.
EXTRA_INSTALLS = {
    "agent-runtime-base": {"setuptools", "wheel", "pytest"},
}

# Images that must install none of our packages at all.
NO_INTERNAL = ("agent-runtime-indexer",)


def _lock() -> dict:
    return tomllib.loads((REPO / "uv.lock").read_text())


def _lock_packages() -> dict[str, dict]:
    return {p["name"]: p for p in _lock()["package"]}


def _internal_packages() -> dict[str, str]:
    """Every package uv.lock builds from this repository: name -> path."""
    out: dict[str, str] = {}
    for name, pkg in _lock_packages().items():
        path = pkg["source"].get("editable") or pkg["source"].get("directory")
        if path:
            out[name] = path
    return out


def _closure(root: str) -> tuple[set[str], set[str]]:
    """(internal, third-party) packages `root` reaches in uv.lock, extras included."""
    packages = _lock_packages()
    internal = set(_internal_packages())
    seen: set[tuple[str, str]] = set()
    todo: list[tuple[str, str]] = [(root, "")]
    while todo:
        name, extra = todo.pop()
        if (name, extra) in seen:
            continue
        seen.add((name, extra))
        pkg = packages[name]
        deps = pkg.get("dependencies", []) if not extra else []
        if extra:
            deps = pkg.get("optional-dependencies", {}).get(extra, [])
            todo.append((name, ""))
        for dep in deps:
            todo.append((dep["name"], ""))
            for e in dep.get("extra", []):
                todo.append((dep["name"], e))
    names = {name for name, _ in seen}
    return names & internal, names - internal


def _builder_runs(image: str) -> list[str]:
    """The builder stage's RUN bodies, continuation lines joined, comments dropped."""
    return _runs(image, "builder")


def _runs(image: str, only_stage: str | None = None) -> list[str]:
    """RUN bodies of one stage (or of every stage), continuation lines joined."""
    lines = (REPO / "images" / image / "Dockerfile").read_text().splitlines()
    instructions: list[str] = []
    current = ""
    for line in lines:
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        instructions.append(current + stripped)
        current = ""
    stage: list[str] = []
    in_stage = False
    for ins in instructions:
        if ins.upper().startswith("FROM "):
            in_stage = only_stage is None or ins.split()[-1] == only_stage
            continue
        if in_stage:
            stage.append(ins)
    assert stage, f"{image}: no {only_stage or 'any'} stage found"
    return [ins[4:].strip() for ins in stage if ins.upper().startswith("RUN ")]


def _builder_copies(image: str) -> list[str]:
    lines = (REPO / "images" / image / "Dockerfile").read_text().splitlines()
    out: list[str] = []
    in_builder = False
    for line in lines:
        if line.startswith("FROM "):
            in_builder = line.split()[-1] == "builder"
        elif in_builder and line.startswith("COPY ") and "--from=" not in line:
            out.append(line)
    return out


def _commands(image: str, only_stage: str | None = "builder") -> list[list[str]]:
    """Every simple command in a stage's RUNs, split on && and tokenised."""
    out: list[list[str]] = []
    for body in _runs(image, only_stage):
        for part in re.split(r"&&|\|\||;", body):
            tokens = shlex.split(part.replace(">", " > "))
            if tokens:
                out.append(tokens)
    return out


def _pip_installs(image: str, only_stage: str | None = "builder") -> list[list[str]]:
    if only_stage is not None:
        return [c for c in _commands(image, only_stage) if c[:3] == ["uv", "pip", "install"]]
    # Every stage: the runtime stages carry shell (case, printf with quoted
    # `;`) that the && split cannot tokenise, so cut out each `uv pip install`
    # up to the next command separator and tokenise only that.
    out: list[list[str]] = []
    for body in _runs(image):
        for m in re.finditer(r"\buv pip install\b(?:(?!&&|\|\||;).)*", body):
            out.append(shlex.split(m.group(0)))
    return out


def _all_images() -> list[str]:
    return sorted(p.parent.name for p in (REPO / "images").glob("*/Dockerfile"))


def _exports(image: str) -> list[list[str]]:
    return [c for c in _commands(image) if c[:2] == ["uv", "export"]]


def _one_export(image: str) -> list[str]:
    exports = _exports(image)
    assert len(exports) == 1, f"{image}: expected one `uv export` in the builder, got {exports}"
    return exports[0]


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_no_install_reaches_an_index_through_find_links(image: str) -> None:
    installs = _pip_installs(image)
    assert installs, f"{image}: no `uv pip install` in the builder stage"
    for cmd in installs:
        if any(t == "--find-links" or t.startswith("--find-links=") for t in cmd):
            assert "--no-index" in cmd, (
                f"{image}: `--find-links` without `--no-index` keeps PyPI as an "
                f"index for our own package names (#663): {' '.join(cmd)}"
            )


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_third_party_install_requires_hashes_from_the_lock_export(image: str) -> None:
    installs = _pip_installs(image)
    hashed = [c for c in installs if "--require-hashes" in c]
    assert len(hashed) == 1, f"{image}: expected one `--require-hashes` install, got {hashed}"
    cmd = hashed[0]
    assert "--no-deps" in cmd, f"{image}: the hashed install must not resolve: {cmd}"
    assert "-r" in cmd, f"{image}: the hashed install must read a requirements file: {cmd}"
    req = cmd[cmd.index("-r") + 1]

    export = _one_export(image)
    for flag in ("--frozen", "--no-dev", "--no-emit-local"):
        assert flag in export, f"{image}: `uv export` lacks {flag}: {export}"
    assert "--no-hashes" not in export, f"{image}: the export must carry hashes"
    written = (
        export[export.index("-o") + 1]
        if "-o" in export
        else export[export.index("--output-file") + 1]
        if "--output-file" in export
        else export[export.index(">") + 1]
    )
    assert written == req, f"{image}: installs {req} but the export writes {written}"

    # Every other install is the wheelhouse one, or one of the image's named
    # extras; nothing else may install.
    others = [c for c in installs if c is not cmd]
    wheels = [c for c in others if any(t.startswith("--find-links") for t in c)]
    assert len(wheels) == 1, f"{image}: expected one wheelhouse install, got {wheels}"
    wheel = wheels[0]
    allowed = EXTRA_INSTALLS.get(image, set())
    for extra in (c for c in others if c is not wheel):
        assert "-r" not in extra and "--requirement" not in extra, (
            f"{image}: a second requirements-file install: {extra}"
        )
        names = {
            re.split(r"[=<>!~\[; ]", tok, maxsplit=1)[0].lower()
            for tok in _positional(extra, {"--python"})
        }
        assert names and names <= allowed, (
            f"{image}: installs {sorted(names - allowed)} outside the lock export "
            f"and the wheelhouse: {' '.join(extra)}"
        )
    for flag in ("--no-index", "--no-deps", "--find-links"):
        assert flag in wheel, f"{image}: the wheelhouse install lacks {flag}: {wheel}"
    assert wheel[wheel.index("--find-links") + 1] == "/wheels"


def _positional(cmd: list[str], valued: set[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for tok in cmd[3:]:
        if skip:
            skip = False
            continue
        if tok in valued:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        out.append(tok)
    return out


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_export_and_wheelhouse_cover_exactly_the_services_packages(image: str) -> None:
    service = SERVICES[image]
    internal_needed, _ = _closure(service)
    internal_all = set(_internal_packages())

    export = _one_export(image)
    pruned = {export[i + 1] for i, t in enumerate(export) if t == "--prune"}
    assert pruned == internal_all - internal_needed, (
        f"{image}: the export must prune every internal package {service} does "
        f"not depend on, and none it does. pruned={sorted(pruned)} "
        f"needed={sorted(internal_needed)}"
    )

    wheel = [
        c
        for c in _pip_installs(image)
        if "--require-hashes" not in c and any(t.startswith("--find-links") for t in c)
    ][0]
    names = set(_positional(wheel, {"--python", "--find-links"}))
    assert names == internal_needed, (
        f"{image}: the wheelhouse install must name exactly {sorted(internal_needed)}, "
        f"got {sorted(names)}"
    )

    built = [c for c in _commands(image) if c[:2] == ["uv", "build"]]
    built_paths = {c[-1].removeprefix("/src/") for c in built}
    paths = _internal_packages()
    assert built_paths == {paths[n] for n in internal_needed}, (
        f"{image}: builds wheels for {sorted(built_paths)}"
    )


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_builder_copies_the_lock_and_root_project(image: str) -> None:
    copies = " ".join(_builder_copies(image))
    assert re.search(r"\buv\.lock\b", copies), f"{image}: builder does not COPY uv.lock"
    assert re.search(r"(?<![\w/-])pyproject\.toml\b", copies), (
        f"{image}: builder does not COPY the root pyproject.toml"
    )


@pytest.mark.parametrize("image", _all_images())
def test_no_stage_of_any_image_reaches_an_index_through_find_links(image: str) -> None:
    """Every stage, not only `builder`: a runtime-stage install is as exposed."""
    for cmd in _pip_installs(image, None):
        if any(t == "--find-links" or t.startswith("--find-links=") for t in cmd):
            assert "--no-index" in cmd, (
                f"{image}: `--find-links` without `--no-index` keeps PyPI as an "
                f"index for our own package names (#663): {' '.join(cmd)}"
            )


@pytest.mark.parametrize("image", NO_INTERNAL)
def test_images_without_a_wheelhouse_install_none_of_our_packages(image: str) -> None:
    """The indexer adds only a hashed third-party environment (#663 does not
    apply to it today); this holds it there. Should it ever ship one of our
    packages, it must move to SERVICES and the two-install pattern."""
    internal = set(_internal_packages())
    installs = _pip_installs(image, None)
    assert installs, f"{image}: no `uv pip install` found; the parser lost the image"
    for cmd in installs:
        names = {
            re.split(r"[=<>!~\[; ]", tok, maxsplit=1)[0].lower()
            for tok in _positional(cmd, {"--python", "--only-binary", "-r", "--find-links"})
        }
        assert not names & internal, f"{image}: installs our {sorted(names & internal)}"
        for flag in ("--require-hashes", "--no-deps", "-r"):
            assert flag in cmd, f"{image}: its install lacks {flag}: {' '.join(cmd)}"
        assert not any(t.startswith("--find-links") for t in cmd), (
            f"{image}: a wheelhouse install in an image that ships none of our packages"
        )
    text = (REPO / "images" / image / "Dockerfile").read_text()
    assert not re.search(r"^COPY\s+(?!--from)\S*apps/", text, re.M), (
        f"{image}: COPYs an app directory, so it may be building one of our packages"
    )


def test_dockerignore_lets_the_lock_into_the_context() -> None:
    rules = [
        line.strip()
        for line in (REPO / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "uv.lock" not in rules and "/uv.lock" not in rules, (
        ".dockerignore drops uv.lock, so `COPY uv.lock` fails the build"
    )


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_every_builder_run_is_valid_shell(image: str) -> None:
    runs = _builder_runs(image)
    assert len(runs) >= 3, f"{image}: expected at least three builder RUNs, got {len(runs)}"
    for body in runs:
        result = subprocess.run(
            ["bash", "-n", "-c", body], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{image}: `bash -n` rejects: {body}\n{result.stderr}"


@pytest.mark.parametrize("image", sorted(SERVICES))
def test_export_as_written_emits_the_hashed_closure(image: str, tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv, "uv is not on PATH; this suite runs under `uv run`"
    service = SERVICES[image]
    internal_needed, third_party = _closure(service)
    paths = _internal_packages()

    # Only what the builder COPYs: the root project, the lock, and the
    # service's own internal packages. --frozen must not need the rest.
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(REPO / "uv.lock", tmp_path / "uv.lock")
    for name in internal_needed:
        shutil.copytree(
            REPO / paths[name],
            tmp_path / paths[name],
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", ".venv"),
        )

    export = _one_export(image)
    args = [t for t in export[2:]]
    if ">" in args:
        args = args[: args.index(">")]
    for flag in ("-o", "--output-file"):
        if flag in args:
            i = args.index(flag)
            del args[i : i + 2]
    result = subprocess.run(
        [uv, "export", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "UV_OFFLINE": "1"},
    )
    assert result.returncode == 0, result.stderr

    requirements: dict[str, int] = {}
    current = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            current = re.split(r"[=<>;\[ ]", line, maxsplit=1)[0].lower()
            requirements[current] = 0
        if current and "--hash=sha256:" in line:
            requirements[current] += 1
    assert requirements, f"{image}: the export printed no requirement"
    unhashed = sorted(n for n, h in requirements.items() if h == 0)
    assert not unhashed, f"{image}: exported without a hash: {unhashed}"
    assert set(requirements) == third_party, (
        f"{image}: export differs from uv.lock's closure for {service}: "
        f"extra={sorted(set(requirements) - third_party)} "
        f"missing={sorted(third_party - set(requirements))}"
    )
