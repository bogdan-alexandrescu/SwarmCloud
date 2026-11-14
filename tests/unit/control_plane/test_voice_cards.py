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

#: §4's table, in its order: the kinds the brief names with §3.13.6's new
#: wireframe, code, diff, document and test results after the mock-up; then
#: voice-it-own-speech.md's `readback` (§7.2) and `spec` (§8.1), and
#: voice-it.md's `navigate` (§3.12.3).
DESIGN_KINDS = ("work", "review", "proposal", "diagram", "mockup", "wireframe", "code", "diff",
                "document", "test_results", "details_input", "credential_input", "plan",
                "budget", "stop", "rescue", "readback", "spec", "navigate")

#: §3.13.6's registry: the content each kind names. A content shared by two
#: kinds (`frames`, `markdown`) is drawn by one renderer; the kinds of the
#: last row name their own.
DESIGN_CONTENTS = {
    "proposal": "options", "mockup": "frames", "wireframe": "frames", "diagram": "graph",
    "code": "code", "diff": "diff", "document": "markdown", "spec": "markdown",
    "test_results": "tests",
    **{kind: kind for kind in ("work", "review", "plan", "budget", "stop", "details_input",
                               "credential_input", "rescue", "readback", "navigate")},
}

#: §3.13.6: every kind also takes these; Send posts the waiting comments now
#: (question 19), and Open full size is the console's alone.
DESIGN_COMMON_ACTIONS = ("open_full_size", "comment", "send_comments", "ask_about", "dismiss")


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
        kinds[kind] = {"label": one("label"), "content": one("content"),
                       "fields": many("fields"), "states": many("states"),
                       "actions": many("actions")}
    return kinds


def _ts_contents(text: str) -> list[tuple[str, str]]:
    found = re.search(r"export const CARD_CONTENTS\b[^=]*=\s*\[(.*?)\n\]", text, re.S)
    assert found, "apps/swarm-ui/src/voice/cards.ts has no `export const CARD_CONTENTS = [...]`"
    return re.findall(r"\{\s*content:\s*'([^']*)',\s*renderer:\s*'([^']*)'\s*\}",
                      found.group(1))


def test_the_console_names_the_same_kinds_as_the_schema():
    assert list(_ts_kinds(_ts())) == [k.kind for k in vcards.KINDS], (
        "apps/swarm-ui/src/voice/cards.ts's CARD_KINDS and swarm_api/voice/cards.py's KINDS "
        "name different kinds; change both sides together")


def test_the_console_names_the_same_fields_states_and_actions():
    ts = _ts_kinds(_ts())
    for kind in vcards.KINDS:
        assert ts[kind.kind] == {
            "label": kind.label,
            "content": kind.content,
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


def test_the_console_names_the_same_contents_renderers_and_common_actions():
    text = _ts()
    assert _ts_contents(text) == [(c.content, c.renderer) for c in vcards.CONTENTS], (
        "apps/swarm-ui/src/voice/cards.ts's CARD_CONTENTS and swarm_api/voice/cards.py's "
        "CONTENTS name different contents or renderers; change both sides together")
    assert _const_list(text, "COMMON_ACTIONS") == list(vcards.COMMON_ACTIONS)


def test_the_parser_reads_a_drifted_list_as_drifted():
    # Guards the guard: a kind dropped from the console must not parse as equal.
    text = _ts().replace("kind: 'rescue'", "kind: 'rescued'")
    assert list(_ts_kinds(text)) != [k.kind for k in vcards.KINDS]
    # And a content renamed on one side only.
    text = _ts().replace("content: 'tests', renderer: 'tests'",
                         "content: 'tests', renderer: 'table'")
    assert _ts_contents(text) != [(c.content, c.renderer) for c in vcards.CONTENTS]


# ---- the catalogue ----------------------------------------------------------


def test_every_kind_the_design_lists_is_in_the_catalogue():
    assert tuple(vcards.BY_KIND) == DESIGN_KINDS


def test_every_kind_names_its_content_from_the_registry():
    assert {k.kind: k.content for k in vcards.KINDS} == DESIGN_CONTENTS
    names = [c.content for c in vcards.CONTENTS]
    assert len(names) == len(set(names))
    used = {k.content for k in vcards.KINDS}
    # Every content a kind names is in the registry, every registry entry is
    # used by a kind or is `generic` (a kind this console does not know).
    assert used <= set(names)
    assert set(names) - used == {vcards.GENERIC}
    for content in vcards.CONTENTS:
        assert re.fullmatch(r"[a-z][a-z_]*", content.renderer), content.content
        assert content.renderer in names or content.renderer == vcards.GENERIC, content
    assert vcards.BY_CONTENT[vcards.GENERIC].renderer == vcards.GENERIC
    # Two kinds that share a content share its renderer (§3.13.6).
    assert vcards.renderer_of("spec") == vcards.renderer_of("document") == "markdown"
    assert vcards.renderer_of("wireframe") == vcards.renderer_of("mockup") == "frames"
    assert vcards.renderer_of("a-kind-from-a-newer-server") == vcards.GENERIC


def test_the_common_actions_are_every_kinds_but_open_full_size_is_never_posted():
    assert vcards.COMMON_ACTIONS == DESIGN_COMMON_ACTIONS
    for kind in vcards.KINDS:
        for action in ("comment", "send_comments", "ask_about", "dismiss"):
            assert vcards.allows_action(kind.kind, action), (kind.kind, action)
        # The console's alone: it opens the viewer and calls nothing.
        assert not vcards.allows_action(kind.kind, "open_full_size"), kind.kind
        assert not set(kind.actions) & {"open_full_size", "send_comments", "ask_about",
                                         "dismiss"}, kind.kind
    # Send is an action, not a state: no kind waits in a "sent" state.
    assert "sent" not in vcards.STATES
    assert not vcards.allows_action("nonsense", "comment")


def test_the_new_kinds_carry_the_designs_fields_states_and_actions():
    def shape(kind: str):
        k = vcards.BY_KIND[kind]
        return [f.name for f in k.fields], k.states, k.actions, k.created_by

    assert shape("wireframe") == (
        ["frames", "viewport", "height", "annotations"], ("proposed", "done", "dismissed"),
        ("approve", "comment", "ask_again"), ("planner",))
    assert shape("code") == (
        ["repository", "path", "ref", "language", "first_line", "text"],
        ("proposed", "done"), ("comment", "ask_again"), ("planner",))
    assert shape("diff") == (
        ["repository", "pull_request", "files", "blob_key"], ("proposed", "waiting", "done"),
        ("approve", "ask_for_change", "comment"), ("planner", "platform"))
    assert shape("document") == (
        ["doc_type", "markdown", "headings"], ("proposed", "done"),
        ("approve", "comment", "ask_again"), ("planner",))
    assert shape("test_results") == (
        ["source", "totals", "cases"], ("waiting", "done", "failed"),
        ("rerun_failed", "file_issue"), ("planner", "platform"))
    assert vcards.BY_KIND["document"].fields[0].values == ("spec", "notes", "report", "catch_up")


def _field(kind: str, *path: str) -> vcards.Field:
    fields = vcards.BY_KIND[kind].fields
    found = None
    for name in path:
        found = next(f for f in fields if f.name == name)
        inner = found.item if found.type == "list" else found
        fields = inner.fields if inner is not None else ()
    assert found is not None
    return found


def test_diagram_types_mockup_refs_frame_refs_and_viewports():
    assert _field("diagram", "diagram_type").values == (
        "architecture", "system", "flow", "user_flow", "product_flow", "sequence", "er",
        "state")
    for ref in (_field("proposal", "options", "mockup_ref"),
                _field("diagram", "graph", "nodes", "frame_ref")):
        assert ref.nullable and [f.name for f in ref.fields] == ["card_id", "frame"]
    for kind in ("mockup", "wireframe"):
        assert _field(kind, "viewport").values == ("desktop", "phone")
        assert _field(kind, "height").nullable


def test_every_card_carries_the_designs_envelope():
    names = [f.name for f in vcards.ENVELOPE]
    for required in ("card_id", "kind", "project_id", "session_id", "version", "state",
                     "title", "created_by", "references", "summary", "at", "outcome"):
        assert required in names
    assert {f.name for f in vcards.ENVELOPE if f.set_by == "server"} == {
        "project_id", "session_id", "version", "at", "outcome"}
    assert [f.name for f in vcards.OUTCOME.fields] == ["action", "summary", "by", "at"]
    assert vcards.DECIDING_ACTIONS == ("pick", "mix", "pick_frame", "approve", "dismiss")
    assert vcards.STATES == ("proposed", "acting", "waiting", "done", "failed",
                             "cancelled", "dismissed", "accepted")


def test_each_kind_uses_only_common_states_and_has_actions():
    for kind in vcards.KINDS:
        assert kind.states and set(kind.states) <= set(vcards.STATES), kind.kind
        assert kind.actions, kind.kind
        assert set(kind.created_by) <= set(vcards.CREATORS), kind.kind
    assert vcards.allows_action("proposal", "pick")
    assert not vcards.allows_action("proposal", "merge")
    assert vcards.allows_action("diff", "approve") and vcards.allows_action("test_results",
                                                                            "rerun_failed")
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


CARD_ID = "vc_" + "0" * 32

#: A valid body of each new kind, and of the kinds that gained a field.
NEW_BODIES: dict[str, dict[str, Any]] = {
    "wireframe": {"frames": [{"name": "Home", "html": "<main>Home</main>", "blob_key": None}],
                  "viewport": "phone", "height": 800,
                  "annotations": [{"frame": "Home", "point": {"x": 0.5, "y": 0.25},
                                   "text": "the search sits here"}]},
    "code": {"repository": "example/app", "path": "src/app.py", "ref": "main",
             "language": "python", "first_line": 10, "text": "def main():\n    return 0\n"},
    "diff": {"repository": "example/app", "pull_request": None, "blob_key": None,
             "files": [{"path": "src/app.py", "status": "modified",
                        "patch": "@@ -1 +1 @@\n-a\n+b\n", "blob_key": None}]},
    "document": {"doc_type": "notes", "markdown": "# Notes\n\nOne line.",
                 "headings": [{"id": "notes", "label": "Notes"}]},
    "test_results": {"source": {"kind": "run", "id": "run-1"},
                     "totals": {"passed": 1, "failed": 1, "skipped": 0, "errored": 0},
                     "cases": [{"name": "test_a", "status": "failed", "duration_s": 0.2,
                                "message": "assert 1 == 2", "file": "tests/test_a.py"},
                               {"name": "test_b", "status": "passed", "duration_s": None,
                                "message": None, "file": None}]},
    "mockup": {"drawn_by": "planner", "viewport": "desktop", "height": None,
               "frames": [{"name": "Home", "html": "<main>Home</main>", "blob_key": None}]},
    "diagram": {"diagram_type": "product_flow", "source": None, "blob_key": None,
                "repaired": False,
                "graph": {"nodes": [{"id": "home", "label": "Home", "group": None,
                                     "frame_ref": {"card_id": CARD_ID, "frame": "Home"}},
                                    {"id": "cart", "label": "Cart", "group": None,
                                     "frame_ref": None}],
                          "edges": [{"from": "home", "to": "cart", "label": "add"}]}},
}


def _new(kind: str, **body: Any) -> dict[str, Any]:
    k = vcards.BY_KIND[kind]
    return _card(kind, state=k.states[0], created_by=k.created_by[0],
                 body={**NEW_BODIES[kind], **body})


@pytest.mark.parametrize("kind", sorted(NEW_BODIES))
def test_each_new_kind_and_new_field_is_accepted(kind):
    out = vcards.validate(_new(kind))
    assert out["kind"] == kind and set(out["body"]) == set(NEW_BODIES[kind])


def test_a_proposal_option_may_point_at_a_mockup_frame():
    card = _card()
    card["body"]["options"][0]["mockup_ref"] = {"card_id": CARD_ID, "frame": "Home"}
    out = vcards.validate(card)
    assert out["body"]["options"][0]["mockup_ref"] == {"card_id": CARD_ID, "frame": "Home"}
    assert out["body"]["options"][1]["mockup_ref"] is None
    card["body"]["options"][0]["mockup_ref"] = {"card_id": "https://example.com/x",
                                                "frame": "Home"}
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(card)
    assert refused.value.detail == {"field": "body.options[0].mockup_ref.card_id"}


def test_the_new_kinds_bounds_are_held():
    refusals = (
        (_new("wireframe", annotations=[{"frame": "Home", "point": {"x": 1.5, "y": 0},
                                         "text": "off the frame"}]),
         "body.annotations[0].point.x"),
        (_new("wireframe", viewport="watch"), "body.viewport"),
        (_new("code", text="x\n" * (vcards.CODE_LINES_MAX + 1)), "body.text"),
        (_new("code", first_line=0), "body.first_line"),
        (_new("diff", files=[{"path": f"f{i}", "status": "added", "patch": "+",
                              "blob_key": None} for i in range(vcards.DIFF_FILES_MAX + 1)]),
         "body.files"),
        (_new("diff", pull_request="../42"), "body.pull_request"),
        (_new("test_results", cases=[{"name": "t", "status": "passed", "duration_s": None,
                                      "message": None, "file": None}]
              * (vcards.TEST_CASES_MAX + 1)), "body.cases"),
        (_new("test_results", source={"kind": "laptop", "id": "x"}), "body.source.kind"),
        (_new("document", doc_type="poem"), "body.doc_type"),
    )
    for card, field_path in refusals:
        with pytest.raises(ValidationFailed) as refused:
            vcards.validate(card)
        assert refused.value.detail == {"field": field_path}, field_path


def test_a_diff_over_its_total_goes_to_a_blob():
    patch = "+" + "x" * (vcards.SOURCE_MAX - 1)
    files = [{"path": f"f{i}", "status": "added", "patch": patch, "blob_key": None}
             for i in range(vcards.DIFF_TOTAL_MAX // vcards.SOURCE_MAX + 1)]
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate(_new("diff", files=files))
    assert refused.value.detail == {"field": "body.files"}
    by_blob = [{**f, "patch": None, "blob_key": f"tenants/eng/voice/diff/{i}"}
               for i, f in enumerate(files)]
    assert vcards.validate(_new("diff", files=by_blob))["body"]["files"][0]["patch"] is None


def test_a_writer_cannot_set_the_outcome():
    with pytest.raises(ValidationFailed) as refused:
        vcards.validate({**_card(), "outcome": {"action": "pick", "summary": "CSV",
                                                "by": "person", "at": "2026-11-12T00:00:00Z"}})
    assert refused.value.detail == {"field": "outcome"}


def test_the_card_view_carries_the_stored_outcome():
    outcome = {"action": "pick", "summary": "CSV", "by": "person", "at": "2026-11-12T00:00:00Z"}
    doc = {"kind": "proposal", "project_id": "p", "session_id": "s", "version": 2,
           "updated_at": datetime(2026, 11, 12, tzinfo=timezone.utc), "outcome": outcome,
           "body": vcards.validate(_card(state="done"))}
    view = vcards.card_view(CARD_ID, doc)
    assert view["outcome"] == outcome and list(view) == [f.name for f in vcards.ENVELOPE]
    assert vcards.card_view(CARD_ID, {**doc, "outcome": None})["outcome"] is None


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
    assert served["version"] == vcards.CATALOGUE_VERSION == 4
    assert served["contents"] == [{"content": c.content, "renderer": c.renderer}
                                  for c in vcards.CONTENTS]
    assert served["common_actions"] == list(DESIGN_COMMON_ACTIONS)
    assert [k["content"] for k in served["kinds"]] == [DESIGN_CONTENTS[k] for k in DESIGN_KINDS]
    assert served["outcome"]["fields"][0]["values"] == list(vcards.DECIDING_ACTIONS)
    assert [k["kind"] for k in served["kinds"]] == list(DESIGN_KINDS)
    assert [f["name"] for f in served["fields"]] == [f.name for f in vcards.ENVELOPE]
    assert served["tools"] == vcards.tool_schemas()


def test_the_schema_route_is_403_for_anyone_not_on_the_list(client):
    assert client.get("/v1/voice/cards/schema",
                      headers=auth_header("carol")).status_code == 403
