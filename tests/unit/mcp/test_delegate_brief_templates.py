"""The delegate skill's opt-in brief templates (plan wave 2).

An operator wrote every brief by hand and retyped the same standing rules,
some of which the worker already appends to every coding agent's prompt. The
three templates under `plugin/skills/delegate/reference/` carry only what a
lane's brief must say and the worker does not: a build lane, a read-only
review, and a one-step fixer onto an existing pull request.

Held here:

* each template exists, stays under `TEMPLATE_MAX_BYTES`, and carries its
  marked places (`{{NAME: ...}}`) and its one "left out on purpose" line;
* no template repeats a sentence the worker appends. The sentences are read
  from the worker's own source at test time, never copied here, so a note the
  worker rewords is still the note the templates are held against;
* the skill names each template in one line and says they are opt-in.

Nothing here injects a template: a brief that names none is sent unchanged,
which the bridge already guarantees (no code path reads these files).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_worker import graphstage
from agent_worker.cachedirs import scratch_note
from agent_worker.expected_outputs import agent_instructions, questions_line, verdict_line
from agent_worker.runners import claude_code, cliagent

_REPO = Path(__file__).resolve().parents[3]
_SKILL_DIR = _REPO / "plugin" / "skills" / "delegate"
_SKILL = _SKILL_DIR / "SKILL.md"
_REFERENCE = _SKILL_DIR / "reference"

#: The owner's ask is "short". The largest was 1,880 bytes when written; 2.5 KB
#: leaves room for a lane fact or two and refuses a template that grows into a
#: second copy of the worker's notes.
TEMPLATE_MAX_BYTES = 2_500

#: Each template's marked places: the lane's own facts, by name.
TEMPLATES = {
    "brief-build.md": ("GOAL", "READ_FIRST", "DONE_WHEN", "TERRITORY", "LANE_FACTS"),
    "brief-review.md": ("SUBJECT", "AGAINST", "EXTRA_OUTPUTS", "CHECKS", "CONSOLE_PATHS",
                        "LANE_FACTS"),
    "brief-fix.md": ("PULL_REQUEST", "FINDINGS", "LANE_FACTS"),
}

#: What each template must still say: the lane's own rules.
MUST_SAY = {
    "brief-build.md": ("Red first", "Territory", "merge train ejects", "title states the work",
                       "questions.json", "build the recommended option"),
    "brief-review.md": ("Edit nothing", '"verdict": "MERGE" | "NOT_YET"', '"findings"',
                        "Blockers and majors decide", "local headless browser", "screenshot"),
    "brief-fix.md": ("fix exactly the findings", "Never weaken an assertion",
                     "cannot be fixed in this step"),
}

_MARK = re.compile(r"\{\{([A-Z_]+):[^{}]+\}\}")
_LEFT_OUT = "Left out on purpose, because the worker adds it"


def _worker_paragraphs() -> list[str]:
    """Every paragraph the worker appends to a coding agent's prompt or system
    prompt, built from its source: the standing paragraphs, the test note, the
    scratch note (both volumes), the code-graph notes, the publishing note,
    the time limit, and the artifacts, questions and verdict lines."""
    paragraphs = [
        claude_code.RELATED_TESTS_NOTE,
        claude_code.SAVED_OUTPUT_NOTE,
        claude_code.SUBAGENT_NOTE,
        claude_code.GRAPH_FIRST_NOTE,
        claude_code.GRAPH_SYSTEM_NOTE,
        claude_code.GRAPH_SKILL_SYSTEM_NOTE,
        claude_code.JEV_DECIDE_NOTE,
        claude_code.JEV_SYSTEM_NOTE,
        claude_code.BROWSER_NOTE,
        claude_code.TEST_RUNNER_NOTE,
        scratch_note(own_volume=True),
        scratch_note(own_volume=False),
        graphstage.UNSTAGED_NOTE.format(reason="no graph"),
        cliagent.PUBLISH_PARAGRAPH,
        cliagent.time_limit_line(3600),
        agent_instructions(["verdict.json"], "/work/artifacts", ["/work/notes.md"]),
        questions_line(),
        verdict_line(),
    ]
    return [p for p in paragraphs if p]


def _normal(text: str) -> str:
    return " ".join(text.replace("`", "").split()).lower()


def _sentences(paragraph: str) -> list[str]:
    """A paragraph's sentences and lines of three words or more, so a short
    one such as `Commit your work.` is held too."""
    pieces = re.split(r"(?<=[.!?])\s+|\n", paragraph)
    return [_normal(p) for p in pieces if len(p.split()) >= 3]


def test_the_worker_paragraphs_were_read():
    """Empty output is not success: the list the next test checks is real."""
    sentences = [s for p in _worker_paragraphs() for s in _sentences(p)]
    assert len(sentences) > 30, len(sentences)
    assert _normal("Commit your work.") in sentences


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_each_template_exists_and_stays_short(name):
    path = _REFERENCE / name
    assert path.is_file(), f"{path} is missing"
    size = len(path.read_bytes())
    # MUTATION: paste the worker's notes into a template -> over the limit.
    assert size <= TEMPLATE_MAX_BYTES, f"{name} is {size} bytes, over {TEMPLATE_MAX_BYTES}"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_each_template_has_its_marked_places_and_its_rules(name):
    text = (_REFERENCE / name).read_text()
    marks = set(_MARK.findall(text))
    # MUTATION: fill a place in with one lane's facts -> its mark is missing.
    assert set(TEMPLATES[name]) <= marks, sorted(set(TEMPLATES[name]) - marks)
    places = text.replace("`{{...}}`", "")  # the intro's own mention of the marks
    assert places.count("{{") == places.count("}}") == len(_MARK.findall(text)), "a malformed mark"
    flat = " ".join(text.split())
    for phrase in MUST_SAY[name]:
        assert phrase in flat, f"{name} no longer says {phrase!r}"
    assert flat.startswith("# Brief template") and "Opt-in." in flat
    assert text.count(_LEFT_OUT) == 1, f"{name} must say once what it leaves to the worker"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_no_template_repeats_a_sentence_the_worker_appends(name):
    template = _normal((_REFERENCE / name).read_text())
    repeated = sorted(
        {s for p in _worker_paragraphs() for s in _sentences(p) if s in template}
    )
    # MUTATION: add "Commit your work." to a template -> named here.
    assert not repeated, f"{name} repeats what the worker appends: {repeated}"


def test_the_skill_names_each_template_once_in_one_line_and_says_opt_in():
    skill = _SKILL.read_text()
    present = {p.name for p in _REFERENCE.glob("*.md")}
    assert present == set(TEMPLATES), sorted(present ^ set(TEMPLATES))
    for name in TEMPLATES:
        lines = [line for line in skill.splitlines() if f"`reference/{name}`" in line]
        assert len(lines) == 1, f"the skill names {name} on {len(lines)} lines"
        assert lines[0].startswith("* "), "one bullet per template"
    flat = " ".join(skill.split())
    assert "**Brief templates are opt-in**" in flat
    assert "a brief that uses none is sent exactly as written" in flat
    assert "\n  - Read\n" in skill.split("---", 2)[1], "the skill reads its templates"
