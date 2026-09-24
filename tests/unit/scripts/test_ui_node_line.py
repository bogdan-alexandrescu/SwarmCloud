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
    production build, which is the thing a toolchain bump can break.

NOT asserted: which major is current. Whether a line is supported is a fact
about a date, not about this tree; docs/versions.md is where that is decided.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
UI_DOCKERFILE = REPO / "images" / "swarm-ui" / "Dockerfile"

_NODE_ARG = re.compile(r"^ARG NODE_IMAGE=node:(\d+)[^\s@]*@sha256:[0-9a-f]{64}\s*$")


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


def test_the_ui_job_reads_its_node_from_the_ui_image():
    """MUTATION: put a literal back, e.g. `node-version: "20"`, in the ui job."""
    steps = yaml.safe_load(APPLICATION.read_text())["jobs"]["ui"]["steps"]
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert len(setup) == 1, f"expected one setup-node step in the ui job, found {len(setup)}"
    version = str((setup[0].get("with") or {}).get("node-version", ""))
    reference = re.fullmatch(
        r"\$\{\{\s*steps\.([A-Za-z0-9_-]+)\.outputs\.[A-Za-z0-9_-]+\s*\}\}", version
    )
    assert reference, (
        f"the ui job pins node-version {version!r} itself. That is a second statement "
        f"of the Node {UI_DOCKERFILE.relative_to(REPO)} builds the bundle with, and "
        "the two drift: both still said 20 after the rest of the platform had moved to 24. "
        "Read it from the Dockerfile in a step and pass the step's output."
    )
    producers = [s for s in steps if s.get("id") == reference.group(1)]
    assert len(producers) == 1, f"no step with id {reference.group(1)!r} in the ui job"
    assert "images/swarm-ui/Dockerfile" in str(producers[0].get("run", "")), (
        f"step {reference.group(1)!r} does not read images/swarm-ui/Dockerfile"
    )
    assert steps.index(producers[0]) < steps.index(setup[0]), "the version is read after it is used"


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
