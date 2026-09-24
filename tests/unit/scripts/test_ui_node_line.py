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
  * the `ui` job's `setup-node` takes its version from a step that reads the
    swarm-ui Dockerfile, not from a literal of its own -- and still runs the
    production build, which is the thing a toolchain bump can break;
  * that step, EXECUTED against a Dockerfile, writes that Dockerfile's major
    under the exact output name `setup-node` reads -- so a renamed output, or a
    literal echoed next to a comment naming the Dockerfile, fails here;
  * a later step, EXECUTED against a stand-in `node`, fails the job when the
    Node that runs is not that major, or when the major arrived empty.

WHY THE LAST TWO RUN THE STEPS INSTEAD OF MATCHING THEIR TEXT. An earlier
version of this file checked that `node-version` had the shape
`${{ steps.<id>.outputs.<name> }}` and that step <id> mentioned the Dockerfile.
Both held while the output NAME was free to drift, and setup-node treats an
empty `node-version` as "no version": its src/main.ts calls setupNodeJs only
`if (version)`, installs nothing, and the job typechecks, tests and builds on
the runner image's own Node -- green. An empty value that reads as success is
the one failure this repository keeps meeting, so the tests below assert what
the steps DO.

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
import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
UI_DOCKERFILE = REPO / "images" / "swarm-ui" / "Dockerfile"

_NODE_ARG = re.compile(r"^ARG NODE_IMAGE=node:(\d+)[^\s@]*@sha256:[0-9a-f]{64}\s*$")

# `${{ steps.<id>.outputs.<name> }}`, whitespace inside the braces optional, as
# Actions itself accepts it. Group 1 is the step id, group 2 the output name.
_OUTPUT_REF = re.compile(r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)\s*\}\}")

# The steps that PROVE something about the UI on the Node the job runs. The
# Node check has to come before all of them, or it proves nothing about them.
_PROOFS = ("npm run typecheck", "npm test", "npm run build")


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


def _ui_steps() -> list[dict]:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]["ui"]["steps"]


def _setup_node(steps: list[dict]) -> tuple[int, str, re.Match[str] | None]:
    """(index of the ui job's one setup-node step, its node-version, the
    `steps.<id>.outputs.<name>` it reads -- None when it is anything else)."""
    found = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(found) == 1, f"expected one setup-node step in the ui job, found {len(found)}"
    version = str((steps[found[0]].get("with") or {}).get("node-version", ""))
    return found[0], version, _OUTPUT_REF.fullmatch(version.strip())


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
    tmp_path: Path, dockerfile: str | None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], str]:
    """Run the step setup-node takes its version from, in a scratch tree that
    holds `images/swarm-ui/Dockerfile` and nothing else -- so whatever it writes
    can only have come from that file. Returns (result, the `name=value` lines
    it wrote to GITHUB_OUTPUT, the output name setup-node reads)."""
    steps = _ui_steps()
    setup_i, version, ref = _setup_node(steps)
    assert ref, f"setup-node's node-version {version!r} does not read a step output"
    producers = [s for s in steps if s.get("id") == ref.group(1)]
    assert len(producers) == 1, f"no step with id {ref.group(1)!r} in the ui job"
    assert steps.index(producers[0]) < setup_i, "the version is read after it is used"

    tree = tmp_path / "tree"
    (tree / "images" / "swarm-ui").mkdir(parents=True)
    if dockerfile is not None:
        (tree / "images" / "swarm-ui" / "Dockerfile").write_text(dockerfile)
    output = tmp_path / "github-output"
    output.write_text("")
    result = _run_step(
        str(producers[0].get("run", "")), tree, tmp_path / "scratch", {"GITHUB_OUTPUT": str(output)}
    )
    written: dict[str, str] = {}
    for line in output.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep:
            written[name] = value
    return result, written, ref.group(2)


def _with_major(dockerfile: str, major: str) -> str:
    return re.sub(r"(?m)^ARG NODE_IMAGE=node:\d+", f"ARG NODE_IMAGE=node:{major}", dockerfile)


def test_the_ui_job_reads_its_node_from_the_ui_image():
    """MUTATION: put a literal back, e.g. `node-version: "20"`, in the ui job."""
    _, version, ref = _setup_node(_ui_steps())
    assert ref, (
        f"the ui job pins node-version {version!r} itself. That is a second statement "
        f"of the Node {UI_DOCKERFILE.relative_to(REPO)} builds the bundle with, and "
        "the two drift: both still said 20 after the rest of the platform had moved to 24. "
        "Read it from the Dockerfile in a step and pass the step's output."
    )


@pytest.mark.parametrize("bump", [0, 75], ids=["the-real-dockerfile", "a-dockerfile-on-another-major"])
def test_the_node_step_hands_setup_node_the_major_of_the_dockerfile_it_reads(tmp_path, bump):
    """The property is "setup-node receives the major the Dockerfile names",
    so the step is run and its output read back under the name setup-node
    asks for. The second case moves the Dockerfile to a major nobody has
    written anywhere else, which only a step that READS the file can report.

    MUTATIONS, each of which left the old shape-only check green:
      * the step writes `version=...` while setup-node still reads `.major`
        (or the reverse) -- node-version arrives empty;
      * the step echoes `major=24` beside a comment naming the Dockerfile --
        a second copy of the number."""
    real = UI_DOCKERFILE.read_text()
    want = str(int(_dockerfile_major(real)) + bump)
    dockerfile = _with_major(real, want)
    assert _dockerfile_major(dockerfile) == want

    result, written, name = _run_producer(tmp_path, dockerfile)
    assert result.returncode == 0, f"the Node step failed on a valid Dockerfile:\n{result.stdout}{result.stderr}"
    assert written.get(name) == want, (
        f"setup-node reads output {name!r}, but the step wrote {written!r} for a Dockerfile "
        f"on node:{want}. setup-node treats an empty node-version as 'no version', installs "
        "nothing, and the job runs green on the runner's own Node."
    )


@pytest.mark.parametrize("case", ["no-dockerfile", "no-node-image-line", "two-node-image-lines"])
def test_the_node_step_stops_the_job_rather_than_hand_setup_node_nothing(tmp_path, case):
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

    result, written, _ = _run_producer(tmp_path, dockerfile)
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


def test_the_ui_job_fails_when_the_node_that_runs_is_not_the_images(tmp_path):
    """setup-node can be handed the right expression and still not install
    that Node -- the output empty, the name drifted -- and it says nothing: it
    carries on with the runner's preinstalled Node. So some step between
    setup-node and the first typecheck/test/build must compare the Node that
    actually runs with the major setup-node was asked for, reading the SAME
    output, and fail on a mismatch and on an empty major.

    Each candidate step is run the way the runner would: the output expression
    substituted (in `run` and in `env`), with a stand-in `node` on PATH. It
    must be a step of its own so it can be run here without `npm ci`.

    MUTATION: delete that step, point it at a different output than
    setup-node reads, or have it print the version without comparing it."""
    steps = _ui_steps()
    setup_i, version, ref = _setup_node(steps)
    assert ref, f"setup-node's node-version {version!r} does not read a step output"
    key = (ref.group(1), ref.group(2))
    proofs = [i for i, s in enumerate(steps) if str(s.get("run", "")).strip() in _PROOFS]
    assert len(proofs) == len(_PROOFS), f"expected the ui job to run each of {_PROOFS}"
    assert setup_i < min(proofs), "setup-node runs after the steps it is meant to set up"

    def reads_the_output(step: dict) -> bool:
        texts = [str(step.get("run", ""))] + [str(v) for v in (step.get("env") or {}).values()]
        return any((m.group(1), m.group(2)) == key for t in texts for m in _OUTPUT_REF.finditer(t))

    checks = [s for s in steps[setup_i + 1 : min(proofs)] if reads_the_output(s)]
    assert checks, (
        f"no step between setup-node and {steps[min(proofs)].get('name')!r} reads "
        f"steps.{key[0]}.outputs.{key[1]} -- nothing compares the Node that runs with "
        "the one images/swarm-ui/Dockerfile names, so an empty node-version goes green"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text(_FAKE_NODE)
    node.chmod(node.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run(step: dict, want: str, got: str) -> int:
        def sub(text: str) -> str:
            return _OUTPUT_REF.sub(lambda m: want if (m.group(1), m.group(2)) == key else m.group(0), text)

        env = {str(k): sub(str(v)) for k, v in (step.get("env") or {}).items()}
        env["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        env["FAKE_NODE_MAJOR"] = got
        return _run_step(sub(str(step.get("run", ""))), tmp_path, tmp_path / "scratch", env).returncode

    major = _dockerfile_major(UI_DOCKERFILE.read_text())
    runner = str(int(major) + 1)
    verdicts = {
        str(step.get("name", step.get("id", "?"))): {
            "same major passes": run(step, major, major) == 0,
            "another major fails": run(step, major, runner) != 0,
            "an empty major fails": run(step, "", major) != 0,
        }
        for step in checks
    }
    assert any(all(v.values()) for v in verdicts.values()), (
        "no step compares the running Node's major with the one setup-node was asked for "
        f"and fails on a mismatch and on an empty value: {verdicts}"
    )


def test_the_ui_job_runs_the_production_build():
    """The typecheck is `tsc -b --noEmit`; the image runs `npm run build`, which
    is vite's bundler and plugins on top. A Node bump is proven by the half that
    emits. MUTATION: delete the build step from the ui job."""
    steps = yaml.safe_load(APPLICATION.read_text())["jobs"]["ui"]["steps"]
    builds = [
        s
        for s in steps
        if str(s.get("run", "")).strip() == "npm run build"
        and s.get("working-directory") == "apps/swarm-ui"
    ]
    assert len(builds) == 1, "the ui job does not run `npm run build` in apps/swarm-ui"
    assert "RUN npm run build" in UI_DOCKERFILE.read_text(), (
        "the image no longer builds with `npm run build`; the CI step no longer proves it"
    )
