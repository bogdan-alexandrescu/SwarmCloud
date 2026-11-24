"""A Voice it session's record: its transcript as one downloadable text file
and the short summary written once when it ends (routes/voice_record.py,
voice/record.py). swarm-voice and the broker are fakes on `app.state`; no
network, no model.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import TENANT_HEADER
from swarm_api.voice import ledger as vledger
from swarm_api.voice import record as vrecord
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import ENG_GROUP, RESEARCH_GROUP, auth_header
from .test_voice_text_turns import FakePool, TextVoice, _start, _text_app, _turn


class SummarisingVoice(TextVoice):
    """TextVoice that also answers the end-of-session summary call."""

    def __init__(self) -> None:
        super().__init__()
        self.summaries: list[tuple[str, dict]] = []
        self.summary_error: Exception | None = None

    def summarise(self, session_id: str, body: dict) -> dict:
        self.summaries.append((session_id, body))
        if self.summary_error is not None:
            raise self.summary_error
        return {"summary": "Asked for a rollout plan; three steps were drawn; ended by the person.",
                "model": "claude-haiku-5-5", "billing": "subscription",
                "usage": {"input_tokens": 900, "output_tokens": 60}}


class CappedLedger:
    """A ledger whose session is at its $100 cap."""

    def compute(self, *, tenant_id: str, person: str, session_id: str) -> dict:
        total = {"spent_usd": vledger.SESSION_CAP_USD, "reserved_usd": 0.0,
                 "total_usd": vledger.SESSION_CAP_USD, "cap_usd": vledger.SESSION_CAP_USD}
        day = {"spent_usd": 1.0, "reserved_usd": 0.0, "total_usd": 1.0, "cap_usd": vledger.DAY_CAP_USD}
        return {"session": total, "day": day}


@pytest.fixture
def voice() -> SummarisingVoice:
    return SummarisingVoice()


def _client(db, tokens, group_map, objects, voice) -> TestClient:
    client = _text_app(db, tokens, group_map, objects, voice, FakePool("no_voice_account_registered"))
    # The summary runs on the request's thread here, so a test reads its result.
    client.app.state.voice_summary_spawn = lambda run: run()
    return client


def _transcript(client: TestClient, session_id: str, user: str = "alice"):
    return client.get(f"/v1/voice/sessions/{session_id}/transcript", headers=auth_header(user))


def _summary(client: TestClient, session_id: str, user: str = "alice"):
    return client.get(f"/v1/voice/sessions/{session_id}/summary", headers=auth_header(user))


def _end(client: TestClient, session_id: str, user: str = "alice"):
    return client.post(f"/v1/voice/sessions/{session_id}/end", headers=auth_header(user))


def test_the_transcript_is_one_redacted_text_file_for_its_person(db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]
    token = "ghp_" + secrets.token_hex(16)
    assert _turn(client, session_id, f"Plan the rollout; my token is {token}").status_code == 200

    response = _transcript(client, session_id)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert "attachment" in response.headers["content-disposition"]
    assert f"{session_id}.txt" in response.headers["content-disposition"]
    text = response.text
    assert "person: Plan the rollout" in text
    assert "voice: Three steps are on screen." in text
    assert token not in text and token[4:] not in text


def test_another_person_or_workspace_gets_the_same_refusal_as_a_missing_session(
        db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]
    _turn(client, session_id, "Plan the rollout")

    missing = _transcript(client, "vs_" + secrets.token_hex(8))
    other = _transcript(client, session_id, user="bob")
    other_summary = _summary(client, session_id, user="bob")

    assert missing.status_code == 404
    assert other.status_code == 404 and other.json() == {**missing.json(), "message": other.json()["message"]}
    assert other_summary.status_code == 404
    assert "Plan the rollout" not in other.text


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    """alice is in two workspaces (eng, research); bob is in eng with her."""
    return {
        "alice@example.com": (ENG_GROUP, RESEARCH_GROUP),
        "bob@example.com": (ENG_GROUP,),
    }


@pytest.mark.parametrize("who, headers", [
    ("same person, another selected workspace", {TENANT_HEADER: "research"}),
    ("another person, the same workspace", None),
])
def test_each_filter_alone_refuses_the_transcript_and_summary_and_reads_no_line(
        db, tokens, group_map, objects, voice, monkeypatch, who, headers):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]  # alice, her default workspace (eng)
    _turn(client, session_id, "Plan the rollout")
    missing = "vs_" + secrets.token_hex(8)
    reads: list[str] = []
    real = vrecord.lines_of
    monkeypatch.setattr(vrecord, "lines_of", lambda *a, **k: reads.append("lines") or real(*a, **k))

    caller = "alice" if headers else "bob"
    head = {**auth_header(caller), **(headers or {})}
    for route in ("transcript", "summary"):
        refused = client.get(f"/v1/voice/sessions/{session_id}/{route}", headers=head)
        absent = client.get(f"/v1/voice/sessions/{missing}/{route}", headers=head)
        assert refused.status_code == 404, (who, route, refused.text)
        assert refused.status_code == absent.status_code
        assert "Plan the rollout" not in refused.text
    assert reads == [], who


def test_an_ended_session_gets_its_summary_once_and_it_is_counted(db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]
    _turn(client, session_id, "Plan the rollout")
    assert _summary(client, session_id).json()["state"] == vrecord.NONE

    assert _end(client, session_id).status_code == 200
    _end(client, session_id)  # a second end writes nothing again

    (called,) = voice.summaries
    assert called[0] == session_id
    assert any(line["text"] == "Plan the rollout" for line in called[1]["transcript"])
    body = _summary(client, session_id).json()
    assert body["state"] == vrecord.WRITTEN
    assert body["text"].startswith("Asked for a rollout plan")
    turns = [d for p, d in db.docs.items() if "/turns/" in p and d.get("kind") == vrecord.SUMMARY_KIND]
    (turn,) = turns
    assert turn["session_id"] == session_id
    assert turn["usage"]["model"] == "claude-haiku-5-5"
    assert turn["usage"]["output_tokens"] == 60


def test_a_failed_summary_never_blocks_the_end(db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]
    voice.summary_error = vs.VoiceUnreachable("swarm-voice could not summarise.")

    response = _end(client, session_id)

    assert response.status_code == 200 and response.json()["state"] == vs.ENDED
    assert _summary(client, session_id).json() == {
        "session_id": session_id, "state": vrecord.FAILED, "text": None, "at": None}


def test_a_session_at_its_cap_gets_no_summary(db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    session_id = _start(client).json()["session_id"]
    # Set after the start: the start's own cap check reads the day alone.
    client.app.state.voice_budget_ledger = CappedLedger()

    assert _end(client, session_id).status_code == 200

    assert voice.summaries == []
    assert _summary(client, session_id).json()["state"] == vrecord.CAPPED



def test_deleting_the_project_deletes_the_transcript(db, tokens, group_map, objects, voice):
    client = _client(db, tokens, group_map, objects, voice)
    client.app.state.voice_blobs = vstore.InMemoryVoiceBlobs()
    started = _start(client).json()
    session_id, project_id = started["session_id"], started["project_id"]
    _turn(client, session_id, "Plan the rollout")
    _end(client, session_id)
    assert any(p.startswith(f"{vrecord.COLLECTION}/{session_id}") for p in db.docs)
    title = client.get(f"/v1/voice/projects/{project_id}", headers=auth_header("alice")).json()["title"]

    deleted = client.request("DELETE", f"/v1/voice/projects/{project_id}", json={"confirm": title},
                             headers=auth_header("alice"))

    assert deleted.status_code == 200, deleted.text
    assert not any(p.startswith(f"{vrecord.COLLECTION}/{session_id}") for p in db.docs)
