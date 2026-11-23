"""The fast front hands out its first complete sentence while its turn is
still running (swarm_voice.claudefront `turn(on_sentence=)`,
claudeturn `Conversation.say(on_text=)`; docs/design/voice-it-own-speech.md
§3.3 step 4, §12.6).

What is handed out early is the same text the transcript records, once: the
recorded reply starts with it and the answer's `rest` is what is left to
speak. A fake child streams its text in pieces; no model, no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from test_claude_front import PERSON, FakeConversation, World, _grant, run  # noqa: E402

from swarm_voice import claudefront, claudeturn  # noqa: E402


class StreamingConversation(FakeConversation):
    """A child whose answer arrives in `pieces`; the test sees each piece
    handed to `on_text` before the turn's outcome."""

    pieces: list[str] = []
    result: str = ""
    early_seen: list[str] = []

    async def say(self, text: str, *, on_text=None) -> claudeturn.ChildOutcome:  # noqa: ANN001
        self.said.append(text)
        for piece in self.pieces:
            if on_text is not None:
                on_text(piece)
            await asyncio.sleep(0)
        # The turn's last token: the sentence was handed out before it.
        self.early_seen.append(",".join(self.script.early))
        return claudeturn.ChildOutcome(result_text=self.result, usage={"input_tokens": 5, "output_tokens": 1})


def _streaming(w: World, pieces: list[str], result: str) -> None:
    w.claude.early = []

    async def converse(*, system, token, tools, handler, known, model=None):  # noqa: ANN001
        conversation = StreamingConversation(handler, token, w.claude)
        conversation.pieces, conversation.result, conversation.early_seen = pieces, result, []
        w.claude.started.append(conversation)
        return conversation

    w.claude.converse = converse


def test_the_first_sentence_is_handed_out_before_the_turn_ends_and_recorded_once():
    async def body(w: World):
        _streaming(w, ["Two agents ", "are running. One is", " the login fix."],
                   "Two agents are running. One is the login fix.")
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        answer = await w.front.turn("vs-1", "how is it going", on_sentence=w.claude.early.append)
        assert w.claude.early == ["Two agents are running."]
        assert w.claude.started[-1].early_seen == ["Two agents are running."]
        assert answer.early == "Two agents are running."
        assert answer.reply == "Two agents are running. One is the login fix."
        assert answer.rest == "One is the login fix."
        session = w.front.get("vs-1")
        assert session.turns[-1] == ("voice", answer.reply)
    run(body)


def test_an_early_sentence_from_before_a_tool_call_is_kept_in_the_transcript():
    """The CLI's result is the last message's text; a sentence spoken before a
    read is still what was said, so the recorded reply starts with it."""
    async def body(w: World):
        _streaming(w, ["Let me look. ", "Seven agents are running."], "Seven agents are running.")
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        answer = await w.front.turn("vs-1", "how many", on_sentence=w.claude.early.append)
        assert w.claude.early == ["Let me look."]
        assert answer.reply == "Let me look. Seven agents are running."
        assert answer.rest == "Seven agents are running."
    run(body)


def test_a_reply_with_no_complete_sentence_mid_turn_hands_out_nothing_early():
    async def body(w: World):
        _streaming(w, ["Hi there"], "Hi there.")
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        answer = await w.front.turn("vs-1", "hello", on_sentence=w.claude.early.append)
        assert w.claude.early == []
        assert answer.early is None and answer.rest == answer.reply == "Hi there."
    run(body)


def test_a_turn_without_on_sentence_is_unchanged():
    async def body(w: World):
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        answer = await w.front.turn("vs-1", "hello")
        assert answer.reply == "Hi there." and answer.early is None and answer.rest == "Hi there."
    run(body)


def test_the_streamed_turn_route_sends_the_sentence_then_the_answer():
    async def body(w: World):
        _streaming(w, ["Two agents are running. ", "One is the login fix."],
                   "Two agents are running. One is the login fix.")
        await w.front.start("vs-1", person=PERSON, grant=_grant())
        lines: list[dict[str, Any]] = []
        async for raw in claudefront.turn_lines(w.front, "vs-1", "how is it going"):
            lines.append(json.loads(raw))
        assert lines[0] == {"t": "sentence", "text": "Two agents are running."}
        assert lines[-1]["t"] == "answer"
        assert lines[-1]["reply"] == "Two agents are running. One is the login fix."
        assert lines[-1]["early"] == "Two agents are running."
        assert len(lines) == 2
    run(body)


def test_the_streamed_turn_route_reports_an_ended_session_as_404():
    async def body(w: World):
        lines = [json.loads(raw) async for raw in claudefront.turn_lines(w.front, "vs-gone", "hello")]
        assert lines == [{"t": "error", "status": 404, "message": "no such speech conversation"}]
    run(body)


def test_the_child_hands_out_streamed_text_deltas():
    """`--include-partial-messages` streams text deltas; `say(on_text=)` gets
    each, and a whole assistant message's text only when no delta came."""
    seen: list[str] = []
    claudeturn.feed_text({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}}}, seen.append, {})
    state: dict[str, Any] = {}
    claudeturn.feed_text({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "there."}}}, seen.append, state)
    claudeturn.feed_text({"type": "assistant", "message": {"content": [{"type": "text", "text": "there."}]}},
                         seen.append, state)
    assert seen == ["Hi ", "there."]
    whole: list[str] = []
    claudeturn.feed_text({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello."}]}},
                         whole.append, {})
    assert whole == ["Hello."]
    argv = claudeturn.ClaudeCode(binary="claude").converse_args(system="s", mcp_config="m")
    assert "--include-partial-messages" in argv
