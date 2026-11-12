"""The Voice it card catalogue (docs/design/voice-it.md §4, §10 phase 2).

One schema in swarm-api (swarm_api/voice/cards.py) validates every card
write, is served to the planner as strict tool schemas, and is held here to
the console's list in apps/swarm-ui/src/voice/cards.ts, the way
test_issue_forms.py holds the forms to SECTIONS: red until both sides name the
same kinds, fields, states and actions. An unknown kind, or a state its kind
does not use, is refused on write and nothing is stored. A credential card has
no field that can hold a value, and no card carries a credential-shaped string.
Firestore is the in-memory fake; no network.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.errors import ValidationFailed
from swarm_api.voice import cards as vcards
from swarm_api.voice import sessions as vs
from swarm_api.voice import store as vstore

from .conftest import auth_header
from .test_voice_sessions import FakeVoice, _client, _start

ROOT = Path(__file__).resolve().parents[3]
CARDS_TS = ROOT / "apps" / "swarm-ui" / "src" / "voice" / "cards.ts"

#: §4's table, in its order: the kinds the brief names; then voice-it-own-speech.md's
#: `readback` (§7.2) and `spec` (§8.1), and voice-it.md's `navigate` (§3.12.3).
DESIGN_KINDS = ("work", "review", "proposal", "diagram", "mockup", "details_input",
                "credential_input", "plan", "budget", "stop", "rescue", "readback", "spec",
                "navigate")


# ---- reading the console's list ---------------------------------------------


def _ts() -> str:
    return CARDS_TS.read_text(encoding="utf-8")


def _strings(block: str) -> list[str]:
    return re.findall(r"'([^'\\]*)'", block)


def _const_list(text: str, name: str) -> list[str]:
    found = re.search(rf"export const {name}\b[^=]*=\s*\[(.*?)\]", text, re.S)
    assert found, f"apps/swarm-ui/src/voice/cards.ts has no `export const {name} = [...]`"
    return _strings(found.group(1))


def _ts_kinds(text: str) -> dict[str, dict[str, Any]]:
    found = re.search(r"export const CARD_KINDS\b[^=]*=\s*\[(.*)\n\]", text, re.S)
    assert found, "apps/swarm-ui/src/voice/cards.ts has no `export const CARD_KINDS = [...]`"
    kinds: dict[str, dict[str, Any]] = {}
    for entry in re.findall(r"\{(.*?)\n  \}", found.group(1), re.S):
        def one(key: str) -> str:
            hit = re.search(rf"\b{key}:\s*'([^']*)'", entry)
            assert hit, f"a CARD_KINDS entry has no {key}: {entry!r}"
            return hit.group(1)

        def many(key: str) -> list[str]:
            hit = re.search(rf"\b{key}:\s*\[(.*?)\]", entry, re.S)
            assert hit, f"a CARD_KINDS entry has no {key}: {entry!r}"
            return _strings(hit.group(1))

        kind = one("kind")
        assert kind not in kinds, f"cards.ts lists {kind!r} twice"
        kinds[kind] = {"label": one("label"), "fields": many("fields"),
                       "states": many("states"), "actions": many("actions")}
    return kinds


def test_the_console_names_the_same_kinds_as_the_schema():
    assert list(_ts_kinds(_ts())) == [k.kind for k in vcards.KINDS], (
        "apps/swarm-ui/src/voice/cards.ts's CARD_KINDS and swarm_api/voice/cards.py's KINDS "
        "name different kinds; change both sides together")


def test_the_console_names_the_same_fields_states_and_actions():
    ts = _ts_kinds(_ts())
    for kind in vcards.KINDS:
        assert ts[kind.kind] == {
            "label": kind.label,
            "fields": [f.name for f in kind.fields],
            "states": list(kind.states),
            "actions": list(kind.actions),
        }, kind.kind


def test_the_console_names_the_common_fields_states_creators_and_references():
    text = _ts()
    assert _const_list(text, "CARD_FIELDS") == [f.name for f in vcards.ENVELOPE]
    assert _const_list(text, "CARD_STATES") == list(vcards.STATES)
    assert _const_list(text, "CARD_CREATORS") == list(vcards.CREATORS)
    assert _const_list(text, "CARD_REFERENCES") == list(vcards.REFERENCES)


def test_the_parser_reads_a_drifted_list_as_drifted():
    # Guards the guard: a kind dropped from the console must not parse as equal.
    text = _ts().replace("kind: 'rescue'", "kind: 'rescued'")
    assert list(_ts_kinds(text)) != [k.kind for k in vcards.KINDS]


# ---- the catalogue ----------------------------------------------------------


def test_every_kind_the_design_lists_is_in_the_catalogue():
    assert tuple(vcards.BY_KIND) == DESIGN_KINDS


def test_every_card_carries_the_designs_envelope():
    names = [f.name for f in vcards.ENVELOPE]
    for required in ("card_id", "kind", "project_id", "session_id", "version", "state",
                     "title", "created_by", "references", "summary", "at"):
        assert required in names
    assert {f.name for f in vcards.ENVELOPE if f.set_by == "server"} == {
        "project_id", "session_id", "version", "at"}
    assert vcards.STATES == ("proposed", "acting", "waiting", "done", "failed",
                             "cancelled", "dismissed", "accepted")


def test_each_kind_uses_only_common_states_and_has_actions():
    for kind in vcards.KINDS:
        assert kind.states and set(kind.states) <= set(vcards.STATES), kind.kind
        assert kind.actions, kind.kind
        assert set(kind.created_by) <= set(vcards.CREATORS), kind.kind
    assert vcards.allows_action("proposal", "pick")
    assert not vcards.allows_action("proposal", "merge")
    assert not vcards.allows_action("nonsense", "pick")


def _walk(schema: Any, path: str = "$"):
    if isinstance(schema, dict):
        yield path, schema
        for key, value in schema.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for i, value in enumerate(schema):
            yield from _walk(value, f"{path}[{i}]")


def test_the_planners_tools_are_strict_closed_schemas():
    tools = vcards.tool_schemas()
    planner_kinds = [k.kind for k in vcards.KINDS if "planner" in k.created_by]
    assert [t["name"] for t in tools] == [f"draw_{k}" for k in planner_kinds]
    for tool in tools:
        assert tool["strict"] is True
        objects = 0
        for path, node in _walk(tool["input_schema"]):
            if node.get("type") == "object":
                objects += 1
                assert node["additionalProperties"] is False, (tool["name"], path)
                assert node["required"] == list(node["properties"]), (tool["name"], path)
        assert objects >= 2, tool["name"]
        state = tool["input_schema"]["properties"]["state"]
        assert state["enum"] == list(vcards.BY_KIND[tool["name"][5:]].states)


def test_a_credential_card_has_no_field_that_can_hold_a_value():
    kind = vcards.BY_KIND["credential_input"]
    assert [f.name for f in kind.fields] == ["name", "provider", "why", "status"]
    by_name = {f.name: f for f in kind.fields}
    # The name and provider are name-shaped; the status is set or missing.
    assert by_name["name"].pattern is not None and by_name["provider"].pattern is not None
    assert by_name["status"].values == ("set", "missing")
    value_words = re.compile(r"value|secret|token|password|passwd|key|credential", re.I)
    assert not [f.name for f in kind.fields if value_words.search(f.name)]
    tool = vcards.tool_schema(kind)
    assert tool["input_schema"]["properties"]["body"]["additionalProperties"] is False


# ---- validating a write -----------------------------------------------------


def _card(kind: str = "proposal", **over: Any) -> dict[str, Any]:
    bodies = {
        "proposal": {"question": "Which export?", "recommendation": "CSV first",
                     "options": [
                         {"label": "CSV", "gets_right": "simple", "costs": "little",
                          "recommended": True},
                         {"label": "Parquet", "gets_right": "typed", "costs": "more",
                          "recommended": False}]},
        "credential_input": {"name": "openai", "provider": "openai",
                             "why": "the voice needs it", "status": "missing"},
    }
    card = {"kind": kind, "state": "waiting" if kind == "credential_input" else "proposed",
            "title": "Export", "created_by": "planner",
            "references": {name: None for name in vcards.REFERENCES},
            "summary": "Two ways to export.", "body": bodies.get(kind, bodies["proposal"])}
    card.update(over)
    return card


def test_a_valid_card_is_accepted():
    out = vcards.validate(_card())
    assert out["kind"] == "proposal" and out["card_id"] is None
    assert out["body"]["options"][0]["label"] == "CSV"


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(_card(kind="banner"))
    assert refused.value.detail == {"field": "kind"}


def test_a_state_its_kind_does_not_use_is_refused():
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(_card(state="acting"))  # a proposal is never acting
    assert refused.value.detail == {"field": "state"}
    with pytest.raises(ValidationFailed):
        vcards.validate(_card(state="nonsense"))


def test_a_field_the_kind_does_not_carry_is_refused():
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(_card(body={**_card()["body"], "script": "alert(1)"}))
    assert refused.value.detail == {"field": "body.script"}
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate({**_card(), "tenant_id": "research"})
    assert refused.value.detail == {"field": "tenant_id"}


def test_a_proposal_needs_two_to_four_options():
    one = _card()
    one["body"] = {**one["body"], "options": one["body"]["options"][:1]}
    with pytest.raises(ValidationFailed):
        vcards.validate(one)


def test_a_credential_card_refuses_a_value_field():
    card = _card("credential_input")
    card["body"] = {**card["body"], "value": secrets.token_hex(20)}
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(card)
    assert refused.value.detail == {"field": "body.value"}


def test_a_credential_shaped_string_is_refused_and_never_echoed():
    fake = "sk-" + secrets.token_hex(24)
    for kind, where in (("credential_input", "why"), ("proposal", "question")):
        card = _card(kind)
        card["body"] = {**card["body"], where: f"use {fake}"}
        with pytest.raises(ValidationFailed) as refused:
            vcards.validate(card)
        assert refused.value.detail == {"field": f"body.{where}"}
        assert fake not in str(refused.value)
        assert fake not in json.dumps(refused.value.detail)


# ---- writing through the store ----------------------------------------------


@pytest.fixture
def client(db, tokens, group_map, objects) -> TestClient:
    return _client(db, tokens, group_map, objects, FakeVoice(),
                   voice_url="http://voice.internal:8080",
                   voice_people=("alice@example.com", "bob@example.com"),
                   child_key=secrets.token_hex(32))


def _store(db) -> vstore.VoiceStore:
    return vstore.VoiceStore(db, now=lambda: datetime.now(timezone.utc))


def _cards(db) -> dict[str, dict]:
    return {path: data for path, data in db.docs.items()
            if path.startswith(vs.PROJECTS + "/") and f"/{vstore.CARDS}/" in path}


def test_write_card_stores_the_card_and_its_stream_event(client, db):
    started = _start(client, "alice").json()
    kw = {"tenant_id": "eng", "person": "alice@example.com",
          "project_id": started["project_id"]}

    view = vcards.write_card(_store(db), **kw, session_id=started["session_id"], card=_card())

    assert view["card_id"].startswith("vc_") and view["version"] == 1
    assert view["project_id"] == started["project_id"]
    assert [f.name for f in vcards.ENVELOPE] == list(view)
    events = _store(db).events_after(**kw, session_id=started["session_id"])
    assert [e["type"] for e in events] == ["card.upsert"]
    assert events[0]["card"] == view

    again = vcards.write_card(_store(db), **kw, session_id=started["session_id"],
                              card=_card(card_id=view["card_id"], state="done"))
    assert again["version"] == 2 and again["state"] == "done"


def test_a_refused_write_stores_nothing(client, db):
    started = _start(client, "alice").json()
    kw = {"tenant_id": "eng", "person": "alice@example.com",
          "project_id": started["project_id"], "session_id": started["session_id"]}

    for card in (_card(kind="banner"), _card(state="acting")):
        with pytest.raises(ValidationFailed):
            vcards.write_card(_store(db), **kw, card=card)

    assert _cards(db) == {}
    assert _store(db).events_after(**{k: v for k, v in kw.items()}) == []


# ---- the schema route -------------------------------------------------------


def test_the_schema_route_serves_the_catalogue(client):
    response = client.get("/v1/voice/cards/schema", headers=auth_header("alice"))

    assert response.status_code == 200, response.text
    served = response.json()
    assert served["version"] == vcards.CATALOGUE_VERSION
    assert [k["kind"] for k in served["kinds"]] == list(DESIGN_KINDS)
    assert [f["name"] for f in served["fields"]] == [f.name for f in vcards.ENVELOPE]
    assert served["tools"] == vcards.tool_schemas()


def test_the_schema_route_is_403_for_anyone_not_on_the_list(client):
    assert client.get("/v1/voice/cards/schema",
                      headers=auth_header("carol")).status_code == 403
