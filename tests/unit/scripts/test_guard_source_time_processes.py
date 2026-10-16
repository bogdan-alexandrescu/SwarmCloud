"""What a guarded call pays before the guard judges anything, in process starts.

PR 1426 (GUARD-SPEED) cut scripts/lib/workspace-guard.sh to one jq per judged
call and counted three more starts outside it, on every call (part of #1334):
the shim's own dirname, common.sh's dirname for SWARM_LIB_DIR, and the cat
common.sh ran at source time to read the Dockerfile COPY reader's awk program.
Every workspace job runs ~129 guarded calls per create, and every script that
sources common.sh paid the last two. These tests hold all three at zero, and
hold that the paths and the program those lines produced are unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "scripts" / "lib"
COMMON = LIB / "common.sh"
SHIMS = LIB / "guard-bin"
COUNTED = ("dirname", "basename", "cat", "realpath", "readlink", "pwd", "sed", "awk")


def _wrappers(tmp: Path) -> tuple[Path, Path]:
    """A PATH directory whose every counted command records that it started."""
    log = tmp / "started"
    wrap = tmp / "wrap"
    wrap.mkdir()
    for name in COUNTED:
        real = shutil.which(name)
        if real:
            (wrap / name).write_text(f'#!/bin/sh\necho {name} >> "{log}"\nexec "{real}" "$@"\n')
            (wrap / name).chmod(0o755)
    return wrap, log


def _started(log: Path) -> list[str]:
    return log.read_text().split() if log.exists() else []


def test_the_wrappers_see_a_dirname_start(tmp_path: Path) -> None:
    """The control: a counted command that does start is seen, so a zero below is a measurement."""
    wrap, log = _wrappers(tmp_path)
    subprocess.run(["bash", "-c", "dirname /a/b >/dev/null; cat </dev/null"], check=True,
                   env={**os.environ, "PATH": f"{wrap}:{os.environ['PATH']}"})
    assert _started(log) == ["dirname", "cat"]


@pytest.mark.parametrize("guarded", [False, True])
def test_sourcing_common_sh_starts_no_counted_process(tmp_path: Path, guarded: bool) -> None:
    wrap, log = _wrappers(tmp_path)
    env = {**os.environ, "PATH": f"{wrap}:{os.environ['PATH']}"}
    if guarded:
        env["SWARM_CALL_GUARD"] = str(tmp_path / "e.json")
    out = subprocess.run(["bash", "-c", f'source "{COMMON}"; printf "%s\\n%s" "$SWARM_LIB_DIR" "$REPO_ROOT"'],
                         env=env, capture_output=True, text=True, check=True)
    assert _started(log) == []
    assert out.stdout.split("\n") == [str(LIB), str(REPO)]


@pytest.mark.parametrize("how", ["absolute", "relative", "bare", "dotted"])
def test_common_sh_resolves_its_directory_however_it_is_named(tmp_path: Path, how: str) -> None:
    """BASH_SOURCE is whatever the sourcing line wrote; the directory is the same absolute path."""
    cwd, name = {
        "absolute": (tmp_path, str(COMMON)),
        "relative": (REPO, "scripts/lib/common.sh"),
        "bare": (LIB, "common.sh"),
        "dotted": (LIB / "guard-bin", "../common.sh"),
    }[how]
    out = subprocess.run(["bash", "-c", f'source "{name}"; printf "%s\\n%s" "$SWARM_LIB_DIR" "$REPO_ROOT"'],
                         cwd=cwd, capture_output=True, text=True, check=True)
    assert out.stdout.split("\n") == [str(LIB), str(REPO)]


def _guard_copy(tmp: Path, tool: str) -> Path:
    """The real shim, beside a stand-in guard that prints where it was found and what it was handed."""
    lib = tmp / "lib"
    (lib / "guard-bin").mkdir(parents=True)
    shim = lib / "guard-bin" / tool
    shutil.copy2(SHIMS / tool, shim)
    guard = lib / "workspace-guard.sh"
    guard.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$0" "$@"\n')
    guard.chmod(0o755)
    return shim


@pytest.mark.parametrize("tool", ["gcloud", "kubectl", "curl"])
def test_a_shim_starts_no_counted_process_before_the_guard(tmp_path: Path, tool: str) -> None:
    shim = _guard_copy(tmp_path, tool)
    wrap, log = _wrappers(tmp_path)
    arg = "a b"
    out = subprocess.run([str(shim), arg, "--x=1"], env={**os.environ, "PATH": f"{wrap}:{os.environ['PATH']}"},
                         capture_output=True, text=True, check=True)
    assert _started(log) == []
    assert out.stdout.splitlines() == [str(tmp_path / "lib" / "workspace-guard.sh"), "run", tool, arg, "--x=1"]


@pytest.mark.parametrize("how", ["bare", "relative", "dotted"])
def test_a_shim_finds_the_guard_however_it_is_named(tmp_path: Path, how: str) -> None:
    shim = _guard_copy(tmp_path, "gcloud")
    cwd, name = {
        "bare": (shim.parent, "gcloud"),
        "relative": (tmp_path, "lib/guard-bin/gcloud"),
        "dotted": (shim.parent, "../guard-bin/gcloud"),
    }[how]
    out = subprocess.run(["bash", name, "projects", "list"], cwd=cwd, capture_output=True, text=True, check=True)
    assert out.stdout.splitlines() == [str(tmp_path / "lib" / "workspace-guard.sh"), "run", "gcloud",
                                       "projects", "list"]


# --------------------------------------------------------------------------
# the Dockerfile COPY reader, unchanged by where its program is assigned
# --------------------------------------------------------------------------

DOCKERFILE = """\
# syntax=docker/dockerfile:1
FROM python:3.11 AS build
COPY --from=build /out /out
COPY apps/common \\
  # a comment inside a continuation is dropped
  apps/worker /app/
ADD scripts/lib/common.sh /opt/
RUN <<'EOF'
COPY not/an/instruction /nowhere
EOF
copy lower/case /dest
"""

REFUSED = """\
FROM scratch
COPY ["a", "/b"]
COPY ${SRC} /dest
ADD https://example.invalid/x /x
COPY onlyone
"""


def _sources(tmp: Path, text: str) -> subprocess.CompletedProcess[str]:
    df = tmp / "Dockerfile"
    df.write_text(text)
    return subprocess.run(["bash", "-c", f'source "{COMMON}"; dockerfile_copy_sources "$1"', "_", str(df)],
                          capture_output=True, text=True, check=False)


def test_dockerfile_copy_sources_reads_the_sources(tmp_path: Path) -> None:
    out = _sources(tmp_path, DOCKERFILE)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["apps/common", "apps/worker", "scripts/lib/common.sh", "lower/case"]


def test_dockerfile_copy_sources_refuses_what_it_cannot_map(tmp_path: Path) -> None:
    out = _sources(tmp_path, REFUSED)
    assert out.returncode != 0 and out.stdout == ""
    df = tmp_path / "Dockerfile"
    assert out.stderr.splitlines() == [
        f"{df}:2: COPY in JSON form: write it as `COPY <src>... <dest>`",
        f"{df}:3: COPY source ${{SRC}} is built from a variable",
        f"{df}:4: ADD of a URL, https://example.invalid/x",
        f"{df}:5: COPY with no source",
    ]


def test_the_awk_program_is_the_text_between_its_markers() -> None:
    """The variable holds the heredoc body byte for byte, less its one trailing newline, as $( ) left it."""
    text = COMMON.read_text()
    start = text.index("\n", text.index("<<'AWK'")) + 1
    body = text[start:text.index("\nAWK\n", start)]
    out = subprocess.run(["bash", "-c", f'source "{COMMON}"; printf "%s" "$DOCKERFILE_COPY_AWK"'],
                         capture_output=True, text=True, check=True)
    assert body.startswith("function refuse(msg) {") and body.endswith("END { exit bad }")
    assert out.stdout == body
