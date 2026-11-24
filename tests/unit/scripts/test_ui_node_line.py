"""The swarm-ui toolchain runs on one Node line, and it is a supported one.

WHY THIS EXISTS. docs/versions.md records a deliberate departure: the platform
pins Node 24 LTS, "because Node 20 reached end of life in April 2026". That was
true of `images/agent-runtime-base` and of nothing else Node touches. On
2026-09-24 two other places still said 20, each on its own:

  * `images/swarm-ui/Dockerfile` built the shipped bundle on
    `node:20-bookworm-slim`;
  * `application.yml`'s `ui` job tested it on `node-version: "20"`.

Neither copy was read from the other, so bumping one would have left CI proving
the UI on a Node the image no longer used -- the mirrored-value shape
docs/mirrored-values.md exists to record. The fix states the version once, in
the swarm-ui image's `ARG NODE_IMAGE`, and has the CI job READ it from there.

WHAT IS ASSERTED:

  * every image that installs Node takes it from the same Node major line, so
    "the platform's Node" in docs/versions.md is one thing and not two;
  * each of those pins is by digest, not by a floating tag;
  * the step that reads the swarm-ui Dockerfile for setup-node, EXECUTED
    (its published run block, ci_run_blocks) against a Dockerfile, writes that
    Dockerfile's major under the output name `major`;
  * a later step, EXECUTED against a stand-in `node`, fails the job when the
    Node that runs is not that major, or when the major arrived empty.

WHY THE STEPS ARE RUN. setup-node treats an empty `node-version` as "no
version": its src/main.ts calls setupNodeJs only `if (version)`, installs
nothing, and the job typechecks, tests and builds on the runner image's own
Node -- green. An empty value that reads as success is the one failure this
repository keeps meeting, so the tests below assert what the steps DO.

NOT asserted: which major is current. Whether a line is supported is a fact
about a date, not about this tree; docs/versions.md is where that is decided.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from .ci_run_blocks import run_block

REPO = Path(__file__).resolve().parents[3]
UI_DOCKERFILE = REPO / "images" / "swarm-ui" / "Dockerfile"

_NODE_ARG = re.compile(r"^ARG NODE_IMAGE=node:(\d+)[^\s@]*@sha256:[0-9a-f]{64}\s*$")

#: The output setup-node reads the major from, and the two steps' published keys.
OUTPUT = "major"
NODE_STEP = "node"
CHECK_STEP = "the Node that runs is the Node the image builds with"

#: The jobs that install the UI and prove something on its Node. Since
#: 2026-10-07 the old `ui` job is split: `ui-build` (typecheck, node:test, the
#: production build) and the `ui-tests` vitest shards. `ui` keeps the check
#: name and only reads their results.
UI_JOBS = ("ui-build", "ui-tests")


def _node_pins() -> dict[str, list[str]]:
    """image directory -> every `ARG NODE_IMAGE=` line in its Dockerfile."""
    pins: dict[str, list[str]] = {}
    for dockerfile in sorted((REPO / "images").glob("*/Dockerfile")):
        lines = [
            line.strip()
            for line in dockerfile.read_text().splitlines()
            if line.strip().startswith("ARG NODE_IMAGE=")
        ]
        if lines:
            pins[dockerfile.parent.name] = lines
    return pins


def test_every_image_that_installs_node_uses_one_line_pinned_by_digest():
    """MUTATION: set either Dockerfile's NODE_IMAGE back to a node:20 digest."""
    pins = _node_pins()
    assert {"agent-runtime-base", "swarm-ui"} <= pins.keys(), (
        f"expected both Node-based images to pin NODE_IMAGE; found {sorted(pins)}"
    )
    majors: dict[str, str] = {}
    for image, lines in pins.items():
        assert len(lines) == 1, f"images/{image}/Dockerfile pins NODE_IMAGE {len(lines)} times"
        match = _NODE_ARG.match(lines[0])
        assert match, (
            f"images/{image}/Dockerfile: {lines[0]!r} is not `node:<major>-...@sha256:<digest>`. "
            "A floating tag rebuilds onto whatever it points at that day."
        )
        majors[image] = match.group(1)
    assert len(set(majors.values())) == 1, (
        f"the images disagree on the Node major line: {majors}. docs/versions.md names "
        "one platform Node; if an image genuinely needs another, record why there and "
        "change this assertion in the same commit."
    )


def _dockerfile_major(text: str) -> str:
    majors = [m.group(1) for line in text.splitlines() if (m := _NODE_ARG.match(line.strip()))]
    assert len(majors) == 1, f"expected one pinned `ARG NODE_IMAGE=node:<major>-...` line, found {len(majors)}"
    return majors[0]


def _run_step(script: str, cwd: Path, scratch: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a step's `run` as a Linux runner does when the step names no
    `shell:` -- `bash -e <file>` -- with GITHUB_OUTPUT pointed into scratch
    unless the caller supplies one, so a test never writes to CI's own."""
    scratch.mkdir(parents=True, exist_ok=True)
    body = scratch / f"step-{len(list(scratch.glob('step-*.sh')))}.sh"
    body.write_text(script)
    merged = {**os.environ, "GITHUB_OUTPUT": str(scratch / "unused-output"), **env}
    return subprocess.run(["bash", "-e", str(body)], cwd=cwd, env=merged, capture_output=True, text=True)


def _run_producer(
    tmp_path: Path, dockerfile: str | None, job: str = "ui-build"
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the step setup-node takes its version from, in a scratch tree that
    holds `images/swarm-ui/Dockerfile` and nothing else -- so whatever it writes
    can only have come from that file. Returns (result, the `name=value` lines
    it wrote to GITHUB_OUTPUT)."""
    tree = tmp_path / "tree"
    (tree / "images" / "swarm-ui").mkdir(parents=True)
    if dockerfile is not None:
        (tree / "images" / "swarm-ui" / "Dockerfile").write_text(dockerfile)
    output = tmp_path / "github-output"
    output.write_text("")
    result = _run_step(
        run_block("application.yml", job, NODE_STEP), tree, tmp_path / "scratch", {"GITHUB_OUTPUT": str(output)}
    )
    written: dict[str, str] = {}
    for line in output.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep:
            written[name] = value
    return result, written


def _with_major(dockerfile: str, major: str) -> str:
    return re.sub(r"(?m)^ARG NODE_IMAGE=node:\d+", f"ARG NODE_IMAGE=node:{major}", dockerfile)


@pytest.mark.parametrize("job", UI_JOBS)
@pytest.mark.parametrize("bump", [0, 75], ids=["the-real-dockerfile", "a-dockerfile-on-another-major"])
def test_the_node_step_hands_setup_node_the_major_of_the_dockerfile_it_reads(tmp_path, bump, job):
    """The property is "setup-node receives the major the Dockerfile names",
    so the step is run and its `major` output read back. The second case moves the Dockerfile to a major nobody has
    written anywhere else, which only a step that READS the file can report.

    MUTATION: the step writes another output name, or echoes `major=24` beside
    a comment naming the Dockerfile -- a second copy of the number."""
    real = UI_DOCKERFILE.read_text()
    want = str(int(_dockerfile_major(real)) + bump)
    dockerfile = _with_major(real, want)
    assert _dockerfile_major(dockerfile) == want

    result, written = _run_producer(tmp_path, dockerfile, job)
    assert result.returncode == 0, f"the Node step failed on a valid Dockerfile:\n{result.stdout}{result.stderr}"
    assert written.get(OUTPUT) == want, (
        f"setup-node reads output {OUTPUT!r}, but the step wrote {written!r} for a Dockerfile "
        f"on node:{want}. setup-node treats an empty node-version as 'no version', installs "
        "nothing, and the job runs green on the runner's own Node."
    )


@pytest.mark.parametrize("job", UI_JOBS)
@pytest.mark.parametrize("case", ["no-dockerfile", "no-node-image-line", "two-node-image-lines"])
def test_the_node_step_stops_the_job_rather_than_hand_setup_node_nothing(tmp_path, case, job):
    """An empty or ambiguous major must stop the job BEFORE setup-node, which
    would otherwise accept '' and install nothing. MUTATION: drop the step's
    exactly-one-line guard, or its `set -e`."""
    real = UI_DOCKERFILE.read_text()
    arg = next(line for line in real.splitlines() if line.startswith("ARG NODE_IMAGE="))
    dockerfile = {
        "no-dockerfile": None,
        "no-node-image-line": real.replace(arg + "\n", ""),
        "two-node-image-lines": real.replace(arg + "\n", arg + "\n" + arg + "\n"),
    }[case]
    if dockerfile is not None:
        assert dockerfile.count(arg) == {"no-node-image-line": 0, "two-node-image-lines": 2}[case], (
            "the fixture did not change what it names"
        )

    result, written = _run_producer(tmp_path, dockerfile, job)
    assert result.returncode != 0, (
        f"the Node step exited 0 on {case} and wrote {written!r}; the job would go on to "
        "setup-node with no usable version"
    )


_FAKE_NODE = """#!/bin/sh
# Stands in for the `node` setup-node puts on PATH; reports FAKE_NODE_MAJOR.
# `--version` answers as node does (v24.0.0). Anything else -- `-p`/`-e` with an
# expression that prints the major -- answers with the major alone.
case "${1:-}" in
  --version|-v) printf 'v%s.0.0\\n' "$FAKE_NODE_MAJOR" ;;
  *) printf '%s\\n' "$FAKE_NODE_MAJOR" ;;
esac
"""


@pytest.mark.parametrize("job", UI_JOBS)
def test_the_ui_job_fails_when_the_node_that_runs_is_not_the_images(tmp_path, job):
    """setup-node can be handed the right expression and still not install
    that Node -- the output empty, the name drifted -- and it says nothing: it
    carries on with the runner's preinstalled Node. So a step compares the
    Node that actually runs with the major setup-node was asked for, and fails
    on a mismatch and on an empty major. It is run here with a stand-in `node`
    on PATH.

    MUTATION: have the step print the version without comparing it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text(_FAKE_NODE)
    node.chmod(node.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run(want: str, got: str) -> int:
        env = {"WANT_NODE_MAJOR": want, "FAKE_NODE_MAJOR": got,
               "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        return _run_step(run_block("application.yml", job, CHECK_STEP), tmp_path, tmp_path / "scratch", env).returncode

    major = _dockerfile_major(UI_DOCKERFILE.read_text())
    runner = str(int(major) + 1)
    assert run(major, major) == 0, "the same major fails"
    assert run(major, runner) != 0, "another major passes"
    assert run("", major) != 0, "an empty major passes"
