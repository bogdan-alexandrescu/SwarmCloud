"""The release, the hotfix and the image scan name swarm-voice beside the
graph service (lane VOICE-ROLL, part of #2688; docs/design/voice-it.md §3.4,
section 10 phase one).

  * release.yml and hotfix.yml each run `scripts/roll-voice.sh --tf-init
    --get-credentials --confirm` exactly once, as the step right after the
    swarm-typesafe-fwd roll (which follows the swarm-graph roll), with the
    same `if:` and timeout-minutes as the graph's step and a comment naming
    Voice it's design;
  * security.yml's trivy loop scans swarm-voice exactly once, beside
    swarm-graph, with a one-line comment saying why: it holds the platform
    owner's OpenAI subscription credential in memory and relays the voice
    session.

The workflow lines are pushed by the operator from workflow-lines.patch (an
agent cannot push .github/workflows/). Until any of them lands, this module
is skipped with PATCH_HINT as its reason, so the PR's CI is not red on lines
it cannot push; once any of the three files names swarm-voice, every test
here runs in full and a partial or misplaced patch fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
ROLL_GRAPH = "./scripts/roll-graph.sh --tf-init --get-credentials --confirm"
ROLL_FWD = "./scripts/roll-typesafe-fwd.sh --tf-init --get-credentials --confirm"
ROLL_VOICE = "./scripts/roll-voice.sh --tf-init --get-credentials --confirm"
PATCH_HINT = "the operator applies workflow-lines.patch from the VOICE-ROLL pull request"
PATCHED = ("release.yml", "hotfix.yml", "security.yml")

pytestmark = pytest.mark.skipif(
    not any("swarm-voice" in (WORKFLOWS / name).read_text() for name in PATCHED),
    reason=f"no workflow names swarm-voice yet: {PATCH_HINT}",
)


def _steps_around_the_graph_roll(name: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    for job in data["jobs"].values():
        steps = job.get("steps") or []
        if any(step.get("run") == ROLL_GRAPH for step in steps):
            return steps
    raise AssertionError(f"{name} has no swarm-graph roll step")


@pytest.mark.parametrize("name", ["release.yml", "hotfix.yml"])
def test_the_voice_roll_is_the_step_after_the_typesafe_fwd_roll(name):
    text = (WORKFLOWS / name).read_text()
    assert text.count("roll-voice.sh") == 1, f"{name} runs roll-voice.sh {text.count('roll-voice.sh')} times; {PATCH_HINT}"
    steps = _steps_around_the_graph_roll(name)
    runs = [step.get("run") for step in steps]
    graph = runs.index(ROLL_GRAPH)
    assert runs[graph + 1] == ROLL_FWD, runs[graph:graph + 3]
    voice = steps[graph + 2]
    assert voice.get("run") == ROLL_VOICE, f"{name}: the step after the swarm-typesafe-fwd roll is {voice.get('name')!r}; {PATCH_HINT}"
    assert voice["name"] == "roll swarm-voice to this release's image"
    for key in ("if", "timeout-minutes"):
        assert voice.get(key) == steps[graph].get(key), (key, voice.get(key), steps[graph].get(key))
    # No env (no PYTHON_BIN) and no continue-on-error, as the graph's step.
    assert "env" not in voice and "continue-on-error" not in voice, voice


@pytest.mark.parametrize("name", ["release.yml", "hotfix.yml"])
def test_the_voice_roll_comment_names_voice_its_design_in_the_graphs_shape(name):
    lines = (WORKFLOWS / name).read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "- name: roll swarm-voice to this release's image"]
    assert len(starts) == 1, PATCH_HINT
    comment = []
    for line in lines[starts[0] + 1:]:
        if not line.strip().startswith("#"):
            break
        comment.append(line.strip())
    assert comment and comment[0].startswith("# Lane VOICE-ROLL (part of #2688; docs/design/voice-it.md"), comment[:1]
    joined = " ".join(comment)
    assert "kubernetes/apply.sh --swarm-voice" in joined and "agents-staging" in joined, joined
    assert "Not served is a notice, not a failure" in joined, joined


def test_the_image_scan_names_swarm_voice_once_beside_swarm_graph():
    lines = (WORKFLOWS / "security.yml").read_text().splitlines()
    [loop] = [i for i, line in enumerate(lines) if line.strip().startswith("for image in ") and "swarm-graph" in line]
    images = lines[loop].strip().removeprefix("for image in ").removesuffix("; do").split()
    assert images.count("swarm-voice") == 1, f"the trivy loop scans swarm-voice {images.count('swarm-voice')} times; {PATCH_HINT}"
    assert images.index("swarm-voice") == images.index("swarm-graph") + 1, images
    # The comment block above the loop says why, in one line, after the graph's.
    comments = []
    for line in reversed(lines[:loop]):
        if not line.strip().startswith("#"):
            break
        comments.insert(0, line.strip())
    voice = [c for c in comments if c.startswith("# swarm-voice too")]
    assert len(voice) == 1, comments
    graph = [i for i, c in enumerate(comments) if c.startswith("# swarm-graph too")]
    assert graph and comments.index(voice[0]) == graph[0] + 1, comments
    assert "OpenAI subscription credential in memory" in voice[0] and "voice session" in voice[0], voice[0]
