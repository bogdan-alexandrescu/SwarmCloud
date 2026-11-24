"""Hostile transcript lines, card text and delegated results stay inside their
fences in the planner turn's user message (prompts.turn_message) and in the
front's reopened instructions (instructions.build); the rule that everything
fenced is quoted data is said exactly once, before the data (voice-it.md §3.9,
§3.12.7; the improvement plan of 2026-11-23).

Each test builds the same message twice, once with hostile text and once with
plain text in the same places, and holds that what lies OUTSIDE the fences --
what a model reads as instructions -- is the same in both: hostile text adds no
section, closes none early and writes no word outside. Nothing here restates
the product's wording; the markers come from `prompts.fence` and the rule from
the module that says it.
"""

from __future__ import annotations

import re
import secrets

import pytest

import injection_reader as reader
from swarm_voice import instructions, prompts

BASE = "You are the voice. Keep it short."


def _preamble() -> tuple[str, ...]:
    lines = prompts.turn_message([("person", "hello")]).split("\n")
    return (lines[0], lines[-1], instructions.DATA_RULE, instructions.SAY_FIRST.strip())


def _texts(token: str) -> list[str]:
    return reader.hostile(token, preamble=_preamble())


def _plain(token: str) -> str:
    return "plain words " + token


def _rule_sentences(text: str, tag: str) -> list[str]:
    """The sentences of `text` that name the fence's tag and say what is inside is
    not an instruction: the 'everything fenced is quoted' rule, however worded."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s for s in sentences if f"<{tag}>" in s and re.search(r"instruction", s, re.IGNORECASE)]


class TestPlannerTurnMessage:
    """Property: in the planner turn's user message, a hostile transcript line or
    delegation request stays inside its own fence, and the message outside the
    fences is the same as for plain text."""

    @pytest.mark.parametrize("which", range(8))
    def test_hostile_transcript_and_request_stay_inside(self, which):
        token = "tk" + secrets.token_hex(6)
        plain = prompts.turn_message([("person", _plain(token)), ("voice", "ok")], request=_plain(token))
        expected = reader.read(plain)
        assert len(expected.sections) == 2
        for text in _texts(token)[which::8]:
            message = prompts.turn_message([("person", text), ("voice", text)], request=text)
            reading = reader.read(message)
            assert len(reading.sections) == 2, ascii(message[:300])
            assert reading.inside(token)
            assert reading.outside() == expected.outside(), ascii(text)

    def test_text_at_and_over_the_limit_stays_inside(self):
        token = "tk" + secrets.token_hex(6)
        opening, closing, _ = reader.markers()
        plain = reader.read(prompts.turn_message([("person", "x")], request="y"))
        for limit, at in ((prompts.TRANSCRIPT_MAX, "transcript"), (prompts.REQUEST_MAX, "request")):
            for size in (limit - len(closing), limit - 3, limit, limit + 1, limit + 3, 2 * limit):
                text = token + (closing + opening) * (size // len(closing + opening) + 1)
                text = text[:size]
                message = (prompts.turn_message([("person", text)], request="y") if at == "transcript"
                           else prompts.turn_message([("person", "x")], request=text))
                reading = reader.read(message)
                assert len(reading.sections) == 2, (at, size)
                assert reading.outside() == plain.outside(), (at, size)

    def test_the_rule_is_said_once_before_the_data(self):
        """The planner reads the system prompt, then the turn message: the rule
        is in the system prompt exactly once, and never outside a fence of the
        turn message, whatever the transcript says."""
        tag = reader.markers()[2]
        rules = _rule_sentences(prompts.SYSTEM, tag)
        assert len(rules) == 1, rules
        token = "tk" + secrets.token_hex(6)
        hostile = "\n".join([*rules, *_texts(token)[:40]])
        message = prompts.turn_message([("person", hostile)], request=hostile)
        reading = reader.read(message)
        assert _rule_sentences(reading.outside(), tag) == []
        whole = reader.read(prompts.SYSTEM + "\n" + message, own=tuple(rules))
        assert whole.folded.index("\x01") < whole.sections[0][0]


def _build(text: str, *, cause: str = "result") -> str:
    context = instructions.ReopenContext(brief={"goal": text}, cards=[text, text], while_paused=[text])
    return instructions.build(BASE, context, transcript=[("person", text), ("voice", text)],
                              cause=cause, first_words=[text])


class TestFrontInstructions:
    """Property: in the front's reopened instructions, hostile transcript lines,
    card text, the brief, paused-time notes and delegated results each stay inside
    a fence, the text outside the fences is the same as for plain text, and the
    data rule is said exactly once, before the first fence."""

    @pytest.mark.parametrize("cause", ["result", "speech"])
    @pytest.mark.parametrize("which", range(8))
    def test_hostile_sections_stay_inside(self, which, cause):
        token = "tk" + secrets.token_hex(6)
        own = (instructions.DATA_RULE,)
        expected = reader.read(_build(_plain(token), cause=cause), own=own)
        assert len(expected.sections) >= 4
        for text in _texts(token)[which::8]:
            built = _build(text, cause=cause)
            reading = reader.read(built, own=own)
            assert len(reading.sections) == len(expected.sections), ascii(text)
            assert reading.inside(token)
            assert reading.outside() == expected.outside(), ascii(text)

    @pytest.mark.parametrize("cause", ["result", "speech", "resume"])
    def test_the_rule_is_said_once_before_the_data(self, cause):
        token = "tk" + secrets.token_hex(6)
        hostile = "\n".join([instructions.DATA_RULE, *_texts(token)[:40]])
        built = _build(hostile, cause=cause)
        assert built.count(instructions.DATA_RULE) == 1
        reading = reader.read(built, own=(instructions.DATA_RULE,))
        assert reading.folded.index("\x01") < reading.sections[0][0]
        assert reading.inside(token)

    def test_long_text_is_cut_inside_its_fence(self):
        token = "tk" + secrets.token_hex(6)
        opening, closing, _ = reader.markers()
        long = token + (closing + opening) * (instructions.MAX_TOKENS * instructions.BYTES_PER_TOKEN
                                              // len(closing + opening))
        built = _build(long)
        reading = reader.read(built, own=(instructions.DATA_RULE,))
        assert reading.sections and reading.inside(token)
        assert instructions.tokens(built) <= instructions.MAX_TOKENS
