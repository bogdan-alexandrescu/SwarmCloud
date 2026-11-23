"""swarm-api's session client reads swarm-voice's streamed speech turn
(swarm_api/voice/sessions.py `HttpVoiceClient.turn(on_sentence=)`; the lines
are claudefront.py's `turn_lines`): the first sentence is handed out as it is
read, before the answer line; an error line is the unary route's status; a
swarm-voice that answers plain JSON is read as before. urlopen is faked: no
network, no cloud credential.
"""

from __future__ import annotations

import io
import json
import secrets
from typing import Any

import pytest

from swarm_api.voice import sessions as vs


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type: str, events: list[str]) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = {"Content-Type": content_type}
        self._events = events

    def __iter__(self):  # noqa: ANN204
        for line in self.getvalue().splitlines(keepends=True):
            self._events.append("read " + json.loads(line)["t"])
            yield line

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _client(monkeypatch, lines: list[dict[str, Any]], content_type: str = vs.TURN_LINES_TYPE,
            sent: list[dict[str, Any]] | None = None, events: list[str] | None = None) -> vs.HttpVoiceClient:
    events = events if events is not None else []
    body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)

    def urlopen(request, timeout):  # noqa: ANN001
        if sent is not None:
            sent.append(json.loads(request.data))
        return FakeResponse(body, content_type, events)

    monkeypatch.setattr(vs.urllib.request, "urlopen", urlopen)
    return vs.HttpVoiceClient("http://voice.internal:8080", token=lambda audience, timeout: secrets.token_hex(8))


def test_the_first_sentence_is_handed_out_before_the_answer_line_is_read(monkeypatch):
    events: list[str] = []
    sent: list[dict[str, Any]] = []
    client = _client(monkeypatch, [
        {"t": "sentence", "text": "Two agents are running."},
        {"t": "answer", "turn_id": "vf_1", "reply": "Two agents are running. One is the fix.", "cards": [],
         "early": "Two agents are running.", "delegation_id": "vd_1"},
    ], sent=sent, events=events)
    answer = client.turn("vs_1", "how is it going", on_sentence=lambda s: events.append("sentence " + s))
    assert sent == [{"text": "how is it going", "stream": True}]
    assert events == ["read sentence", "sentence Two agents are running.", "read answer"]
    assert answer.reply == "Two agents are running. One is the fix."
    assert answer.early == "Two agents are running." and answer.delegation_id == "vd_1"
    assert client.streams_sentences is True


def test_an_error_line_is_the_unary_routes_status(monkeypatch):
    client = _client(monkeypatch, [{"t": "error", "status": 404, "message": "no such speech conversation"}])
    with pytest.raises(vs.VoiceSessionNotLive):
        client.turn("vs_1", "hello", on_sentence=lambda s: None)
    client = _client(monkeypatch, [{"t": "error", "status": 502, "message": "the fast front is down"}])
    with pytest.raises(vs.VoiceUnreachable):
        client.turn("vs_1", "hello", on_sentence=lambda s: None)


def test_a_plain_json_answer_is_read_as_before(monkeypatch):
    client = _client(monkeypatch, [{"turn_id": "vf_1", "reply": "Hi there.", "cards": []}],
                     content_type="application/json")
    handed: list[str] = []
    answer = client.turn("vs_1", "hello", on_sentence=handed.append)
    assert answer.reply == "Hi there." and answer.early is None and handed == []


def test_an_early_sentence_the_reply_does_not_start_with_is_dropped(monkeypatch):
    client = _client(monkeypatch, [
        {"t": "answer", "turn_id": "vf_1", "reply": "Hi there.", "cards": [], "early": "Something else."}])
    assert client.turn("vs_1", "hello", on_sentence=lambda s: None).early is None
