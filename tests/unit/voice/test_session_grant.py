"""swarm-voice receives a session's grant in the start call and holds it in
memory only (docs/design/voice-it.md §3.6; swarm_voice/grant.py).

The grant rides on the live session until the session ends, is presented
only as the two headers swarm-api reads, and never appears in a log line, a
response, a refusal or a repr. The harness is test_live_session's: a fake
OpenAI, a fake quota broker's Voice it pool, no cloud credential. Every grant is built at
runtime with `secrets`.
"""

from __future__ import annotations

import logging
import secrets

from test_live_session import FAKE_LOG, Harness, _start_body, run

from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant


def _ids() -> tuple[str, str]:
    return "vp_" + secrets.token_hex(16), "vs_" + secrets.token_hex(16)


def _body(h: Harness, project_id: str, session_id: str, grant: str) -> dict:
    return {**_start_body(voice_id=session_id), "project_id": project_id, "grant": grant}


def test_the_grant_is_held_on_the_live_session_and_presented_as_swarm_apis_headers():
    project_id, session_id = _ids()
    grant = secrets.token_urlsafe(32)

    async def scenario(h: Harness):
        r = await h.post("/internal/sessions", _body(h, project_id, session_id, grant))
        assert r.status_code == 201, r.text
        assert grant not in r.text
        held = h.sessions.get(session_id).grant
        assert held.headers() == {SESSION_HEADER: f"{project_id}/{session_id}", GRANT_HEADER: grant}
        assert grant not in repr(held) and grant not in str(held)

    run(scenario)


def test_the_grant_is_dropped_when_the_session_ends():
    project_id, session_id = _ids()

    async def scenario(h: Harness):
        assert (await h.post("/internal/sessions",
                             _body(h, project_id, session_id, secrets.token_urlsafe(32)))).status_code == 201
        session = h.sessions.get(session_id)
        assert (await h.post(f"/internal/sessions/{session_id}/end")).status_code == 200
        assert session.grant is None

    run(scenario)


def test_a_start_without_a_grant_holds_none():
    async def scenario(h: Harness):
        body = _start_body()
        assert (await h.post("/internal/sessions", body)).status_code == 201
        assert h.sessions.get(body["voice_session_id"]).grant is None

    run(scenario)


def test_a_malformed_or_mismatched_grant_is_422_and_the_refusal_never_carries_it():
    project_id, session_id = _ids()
    short = secrets.token_urlsafe(16)
    good = secrets.token_urlsafe(32)

    async def scenario(h: Harness):
        bad = await h.post("/internal/sessions", _body(h, project_id, session_id, short))
        assert bad.status_code == 422 and short not in bad.text
        alone = {**_start_body(voice_id=session_id), "grant": good}
        refused = await h.post("/internal/sessions", alone)
        assert refused.status_code == 422 and good not in refused.text
        # A grant swarm-api minted for a session id that is not this call's.
        other = await h.post("/internal/sessions", _body(h, project_id, "vs_short", good))
        assert other.status_code == 422 and good not in other.text
        assert h.openai.creates == []

    run(scenario)


def test_the_grant_never_appears_in_a_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    project_id, session_id = _ids()
    grant = secrets.token_urlsafe(32)

    async def scenario(h: Harness):
        assert (await h.post("/internal/sessions", _body(h, project_id, session_id, grant))).status_code == 201
        await h.post(f"/internal/sessions/{session_id}/end")
        await h.post("/internal/sessions", _body(h, project_id, session_id, grant[:-1] + "!"))

    run(scenario)
    records = [r for r in caplog.records if not r.name.startswith(FAKE_LOG)]
    assert records, "nothing was logged, so the check below would prove nothing"
    text = "\n".join(r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "") for r in records)
    assert grant not in text and grant[:-1] not in text


def test_session_grant_refuses_values_swarm_api_does_not_mint():
    project_id, session_id = _ids()
    for value in ("", secrets.token_hex(8), secrets.token_urlsafe(32) + "x"):
        try:
            SessionGrant(project_id=project_id, session_id=session_id, value=value)
        except ValueError as exc:
            assert value == "" or value not in str(exc)
        else:
            raise AssertionError("a value swarm-api does not mint was held")
