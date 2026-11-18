"""The planner reads the card catalogue with the session grant
(docs/design/voice-it.md §3.3, §3.6; swarm_voice/planner.py).

swarm-voice's planner offers the model exactly the catalogue's `draw_<kind>`
tools as swarm-api serves them, read from `GET /v1/voice/cards/schema` with the
session grant, so that route is on the grant's allowlist: a read, taking no
body, open only to swarm-voice's identity for a live session of an allowed
person, and audited `via: voice-it` like every other grant read.
"""

from __future__ import annotations

from swarm_api.voice import cards as vcards
from swarm_api.voice import grant as vg

from .test_voice_grant import _audit, _grant_headers, _start, client, voice  # noqa: F401 - fixtures


def test_the_card_schema_route_is_on_the_grants_allowlist():
    assert ("GET", "/v1/voice/cards/schema") in vg.ALLOWED_ROUTES


def test_a_grant_reads_the_card_catalogue_and_is_audited(client, db, voice):  # noqa: F811
    call = _start(client, voice)

    response = client.get("/v1/voice/cards/schema", headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    assert [t["name"] for t in response.json()["tools"]] == [t["name"] for t in vcards.tool_schemas()]
    rows = [r for r in _audit(db) if r["route"] == "/v1/voice/cards/schema"]
    assert len(rows) == 1 and rows[0]["via"] == vg.VIA and rows[0]["session_id"] == call.session_id
