"""Voice it's Navigate card: the planner offers a console page from a fixed
list, and only a person's yes accepts it (docs/design/voice-it.md §3.12.3).

The planner writes a `navigate` card only as `proposed`; `accepted` is set by
swarm-api from the person's click (`POST .../cards/{card_id}/accept`) or the
read-back gate's yes, with `accepted_by: person`. One offer is open a session
at a time: a newer one replaces it, and one unanswered for two minutes, or
outlived by the next turn's output, is dismissed. The destination is one key
of a list the test reads out of apps/swarm-ui/src/paths.ts's `FIXED`, the way
test_voice_cards.py reads the kinds out of cards.ts. Firestore is the
in-memory fake; every token and grant is built at runtime; no network.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.errors import ValidationFailed
from swarm_api.voice import cards as vcards
from swarm_api.voice import readback as rb
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_grant import _build, _grant_headers, _start

ROOT = Path(__file__).resolve().parents[3]
PATHS_TS = ROOT / "apps" / "swarm-ui" / "src" / "paths.ts"
HELP_TS = ROOT / "apps" / "swarm-ui" / "src" / "help.ts"

CARDS = "/v1/voice/sessions/{session_id}/cards"
ACCEPT = "/v1/voice/sessions/{session_id}/cards/{card_id}/accept"
ANSWER = "/v1/voice/sessions/{session_id}/readbacks/answer"


# ---- the destination list against paths.ts ----------------------------------


def _fixed() -> dict[str, str]:
    """paths.ts's `FIXED`, address -> path; `[HELP_ROUTE]` read from help.ts."""
    text = PATHS_TS.read_text(encoding="utf-8")
    found = re.search(r"export const FIXED\b[^=]*=\s*\{(.*?)\n\}", text, re.S)
    assert found, "apps/swarm-ui/src/paths.ts has no `export const FIXED = {...}`"
    help_route = re.search(r"export const HELP_ROUTE\s*=\s*'([^']+)'",
                           HELP_TS.read_text(encoding="utf-8"))
    assert help_route, "apps/swarm-ui/src/help.ts has no `export const HELP_ROUTE = '...'`"
    out: dict[str, str] = {}
    for line in found.group(1).splitlines():
        line = line.strip()
        if line.startswith("//"):
            continue
        hit = re.match(r"(?:'([^']+)'|([A-Za-z_]+)|\[([A-Z_]+)\]):\s*'([^']+)',?$", line)
        if hit:
            key = hit.group(1) or hit.group(2) or (help_route.group(1) if hit.group(3) else None)
            assert key, line
            out[key] = hit.group(4)
    return out


def _reading_pages(fixed: dict[str, str]) -> list[str]:
    """The pages a person reads: never an admin page, a submit form or a
    callback (§3.12.3)."""
    return [address for address, path in fixed.items()
            if not address.startswith("admin/") and not path.startswith("/admin")
            and not path.startswith("/submit") and "callback" not in address]


def test_the_destination_list_equals_paths_ts_reading_pages():
    fixed = _fixed()
    assert len(fixed) > 30, "the FIXED parser read too little of paths.ts"
    assert list(vcards.NAVIGATE_DESTINATIONS) == _reading_pages(fixed), (
        "swarm_api/voice/cards.py's NAVIGATE_DESTINATIONS and paths.ts's reading pages "
        "differ; change both sides together")


def test_the_parser_reads_a_renamed_page_as_drifted():
    # Guards the guard: a page renamed in the console cannot leave a stale offer.
    fixed = _fixed()
    fixed["work/agents"] = fixed.pop("work/running")
    assert list(vcards.NAVIGATE_DESTINATIONS) != _reading_pages(fixed)


def test_no_destination_is_a_form_a_callback_or_an_admin_page():
    for name in vcards.NAVIGATE_DESTINATIONS:
        assert not name.startswith(("admin/", "submit", "work/new")), name
        assert "callback" not in name, name


def test_the_schema_serves_the_destination_list_and_no_address_field():
    kind = vcards.BY_KIND["navigate"]
    assert kind.created_by == ("planner",)
    fields = {f.name: f for f in kind.fields}
    assert set(fields) == {"destination", "object", "why", "accepted_by"}
    assert fields["destination"].values == vcards.NAVIGATE_DESTINATIONS
    tool = next(t for t in vcards.tool_schemas() if t["name"] == "draw_navigate")
    body = tool["input_schema"]["properties"]["body"]["properties"]
    assert body["destination"]["enum"] == list(vcards.NAVIGATE_DESTINATIONS)
    for name in ("url", "href", "path", "address", "link"):
        assert name not in body


# ---- what a card may hold ---------------------------------------------------


def _offer(**over: Any) -> dict[str, Any]:
    body = {"destination": "work/running", "object": None,
            "why": "Running agents: three live, one waiting", "accepted_by": None}
    body.update(over.pop("body", {}))
    card = {"card_id": None, "kind": "navigate", "state": "proposed", "title": "Open Running",
            "created_by": "planner", "references": {n: None for n in vcards.REFERENCES},
            "summary": "Shall I open Running?", "body": body}
    card.update(over)
    return card


def test_an_unknown_destination_is_refused():
    for bad in ("work/new", "admin/tenants", "https://example.com/x", "/agents", ""):
        with pytest.raises(ValidationFailed) as refused:
            vcards.validate(_offer(body={"destination": bad}))
        assert refused.value.detail == {"field": "body.destination"}


def test_the_object_is_one_id_of_a_listed_kind():
    ok = vcards.validate(_offer(body={"object": {"kind": "run", "id": "run_1a2b"}}))
    assert ok["body"]["object"] == {"kind": "run", "id": "run_1a2b"}
    for bad in ({"kind": "repository", "id": "x"}, {"kind": "agent", "id": "../admin"},
                {"kind": "agent", "id": "a b"}, {"kind": "agent", "id": "https://example.com"},
                {"kind": "agent", "id": "t_1", "url": "/agents/t_1"}):
        with pytest.raises(ValidationFailed):
            vcards.validate(_offer(body={"object": bad}))


# ---- the routes -------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.at

    def tick(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def voice():
    from .test_voice_sessions import FakeVoice

    return FakeVoice()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(db, tokens, group_map, objects, voice, clock) -> TestClient:
    built = _build(db, tokens, group_map, objects, voice,
                   voice_people=("alice@example.com", "bob@example.com", "root@example.com"))
    built.app.state.ctx.now = clock
    return built


def _draw(client: TestClient, call: vs.StartCall, **over: Any):
    return client.post(CARDS.format(session_id=call.session_id), json={"card": _offer(**over)},
                       headers=_grant_headers(call))


def _offered(client: TestClient, call: vs.StartCall, **over: Any) -> str:
    r = _draw(client, call, **over)
    assert r.status_code == 200, r.text
    return r.json()["card"]["card_id"]


def _stored(db, card_id: str) -> dict[str, Any]:
    found = [d for p, d in db.docs.items() if p.endswith(f"/{vstore.CARDS}/{card_id}")]
    assert len(found) == 1, card_id
    return found[0]


def _state(db, card_id: str) -> str:
    return _stored(db, card_id)["body"]["state"]


def _accept(client: TestClient, call: vs.StartCall, card_id: str, user: str = "alice"):
    return client.post(ACCEPT.format(session_id=call.session_id, card_id=card_id),
                       headers=auth_header(user))


def _say(client: TestClient, call: vs.StartCall, text: str):
    r = client.post(ANSWER.format(session_id=call.session_id), json={"text": text},
                    headers=_grant_headers(call))
    assert r.status_code == 200, r.text
    return r.json()


def test_the_planner_cannot_write_accepted(client, db, voice):
    call = _start(client, voice)
    for over in ({"state": "accepted"}, {"state": "done"}, {"state": "dismissed"},
                 {"body": {"accepted_by": "person"}}):
        r = _draw(client, call, **over)
        assert r.status_code == 422, (over, r.text)
    assert not [p for p in db.docs if f"/{vstore.CARDS}/" in p]


def test_the_planner_cannot_reopen_or_rewrite_an_offer(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)
    assert _accept(client, call, card_id).status_code == 200
    r = _draw(client, call, card_id=card_id)
    assert r.status_code == 422, r.text
    assert _state(db, card_id) == vcards.ACCEPTED


def test_an_unknown_destination_is_422_on_the_write_route(client, db, voice):
    call = _start(client, voice)
    r = _draw(client, call, body={"destination": "work/new"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"field": "body.destination"}


def test_a_click_accepts_the_open_offer_as_the_person(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)

    r = _accept(client, call, card_id)

    assert r.status_code == 200, r.text
    card = r.json()["card"]
    assert card["state"] == vcards.ACCEPTED and card["body"]["accepted_by"] == "person"
    assert card["kind"] == "navigate" and card["session_id"] == call.session_id
    assert _state(db, card_id) == vcards.ACCEPTED
    again = _accept(client, call, card_id)
    assert again.status_code == 409, again.text


def test_a_grant_cannot_accept(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)
    r = client.post(ACCEPT.format(session_id=call.session_id, card_id=card_id),
                    headers=_grant_headers(call))
    assert r.status_code == 403, r.text
    assert _state(db, card_id) == vcards.PROPOSED


def test_another_persons_or_another_tenants_accept_is_404(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)

    assert _accept(client, call, card_id, user="bob").status_code == 404
    assert _state(db, card_id) == vcards.PROPOSED

    # The session moved to a workspace alice does not belong to.
    for path, doc in list(db.docs.items()):
        if path.startswith(vs.PROJECTS + "/") and doc.get("tenant_id") == "eng":
            db.docs[path] = {**doc, "tenant_id": "research"}
    assert _accept(client, call, card_id).status_code == 404
    assert _state(db, card_id) == vcards.PROPOSED


def test_an_unknown_card_or_another_kind_is_404(client, db, voice):
    call = _start(client, voice)
    proposal = client.post(CARDS.format(session_id=call.session_id), json={"card": {
        "card_id": None, "kind": "spec", "state": "proposed", "title": "Spec",
        "created_by": "planner", "references": {n: None for n in vcards.REFERENCES},
        "summary": "s", "body": {"markdown": "# A", "headings": []}}},
        headers=_grant_headers(call))
    assert proposal.status_code == 200, proposal.text
    spec_id = proposal.json()["card"]["card_id"]
    assert _accept(client, call, spec_id).status_code == 404
    assert _accept(client, call, "vc_" + "0" * 32).status_code == 404
    assert _accept(client, call, "not-a-card").status_code == 422


def test_a_card_from_a_finished_session_is_never_accepted(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)
    ended = client.post(f"/v1/voice/sessions/{call.session_id}/end", headers=auth_header("alice"))
    assert ended.status_code == 200, ended.text

    r = _accept(client, call, card_id)

    assert r.status_code in (404, 409), r.text
    assert _state(db, card_id) != vcards.ACCEPTED


@pytest.mark.parametrize("said", ["yes", "Open it.", "go", "show me"])
def test_a_yes_through_the_gate_accepts_the_open_offer(client, db, voice, said):
    call = _start(client, voice)
    card_id = _offered(client, call)

    answered = _say(client, call, said)

    assert answered["to_planner"] is False
    assert answered["offer"]["answer"] == "accept"
    assert answered["offer"]["card"]["card_id"] == card_id
    assert _state(db, card_id) == vcards.ACCEPTED
    assert _stored(db, card_id)["body"]["body"]["accepted_by"] == "person"


@pytest.mark.parametrize("said", ["no", "Not that.", "no thanks"])
def test_a_no_through_the_gate_dismisses(client, db, voice, said):
    call = _start(client, voice)
    card_id = _offered(client, call)

    answered = _say(client, call, said)

    assert answered["to_planner"] is False
    assert answered["offer"]["answer"] == "dismiss"
    assert _state(db, card_id) == vcards.DISMISSED
    assert _accept(client, call, card_id).status_code == 409


def test_anything_else_leaves_the_offer_open_and_goes_to_the_planner(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)

    answered = _say(client, call, "how many are waiting on me")

    assert answered == {"answer": None, "readback": None, "to_planner": True}
    assert _state(db, card_id) == vcards.PROPOSED


def test_a_yes_with_no_open_offer_is_an_ordinary_turn(client, db, voice):
    call = _start(client, voice)
    assert _say(client, call, "yes") == {"answer": None, "readback": None, "to_planner": True}


def test_a_second_offer_replaces_the_first_and_a_yes_accepts_the_newest_only(client, db, voice,
                                                                          clock):
    call = _start(client, voice)
    first = _offered(client, call)
    clock.tick(5)
    second = _offered(client, call, body={"destination": "work/runs"})

    assert _state(db, first) == vcards.DISMISSED
    assert _accept(client, call, first).status_code == 409

    answered = _say(client, call, "yes")

    assert answered["offer"]["card"]["card_id"] == second
    assert _state(db, second) == vcards.ACCEPTED
    assert _state(db, first) == vcards.DISMISSED


def test_an_offer_older_than_two_minutes_is_dismissed(client, db, voice, clock):
    call = _start(client, voice)
    card_id = _offered(client, call)
    clock.tick(rb.OFFER_SECONDS + 1)

    answered = _say(client, call, "yes")

    assert answered["to_planner"] is True and "offer" not in answered
    assert _state(db, card_id) == vcards.DISMISSED
    assert _accept(client, call, card_id).status_code == 409


def test_an_offer_younger_than_two_minutes_is_still_open(client, db, voice, clock):
    call = _start(client, voice)
    card_id = _offered(client, call)
    clock.tick(rb.OFFER_SECONDS - 5)
    assert _accept(client, call, card_id).status_code == 200


def test_an_offer_outlived_by_the_next_turns_output_is_dismissed(client, db, voice, clock):
    call = _start(client, voice)
    card_id = _offered(client, call)
    clock.tick(2)
    assert _say(client, call, "what about the failing one")["to_planner"] is True
    assert _state(db, card_id) == vcards.PROPOSED
    clock.tick(2)
    # The next turn's output: a card of the planner's.
    r = client.post(CARDS.format(session_id=call.session_id), json={"card": {
        "card_id": None, "kind": "spec", "state": "proposed", "title": "Why it failed",
        "created_by": "planner", "references": {n: None for n in vcards.REFERENCES},
        "summary": "s", "body": {"markdown": "# Cause", "headings": []}}},
        headers=_grant_headers(call))
    assert r.status_code == 200, r.text
    clock.tick(1)

    assert _accept(client, call, card_id).status_code == 409
    assert _state(db, card_id) == vcards.DISMISSED


def test_a_pending_read_back_takes_go_before_an_offer(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)
    where = rb.Where(tenant_id="eng", person="alice@example.com",
                     project_id=call.project_id, session_id=call.session_id)
    rb.Readbacks(db, now=client.app.state.ctx.now).hold(
        where, action="cancel", target="agent nightly (t_1)", method="POST",
        route="/v1/tasks/{task_id}/cancel", body=b"{}")

    answered = _say(client, call, "go")

    assert answered["answer"] == "go"
    assert _state(db, card_id) == vcards.PROPOSED


def test_an_accepted_offer_reaches_the_stream(client, db, voice):
    call = _start(client, voice)
    card_id = _offered(client, call)
    assert _accept(client, call, card_id).status_code == 200
    events = [d for p, d in sorted(db.docs.items())
              if p.startswith(vs.PROJECTS + "/") and "/events/" in p
              and d.get("type") == vcards.CARD_UPSERT]
    states = [e["card"]["state"] for e in events if e["card"]["card_id"] == card_id]
    assert states[-1] == vcards.ACCEPTED and vcards.PROPOSED in states
