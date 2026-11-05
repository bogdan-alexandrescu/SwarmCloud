"""The planner's visual tools and `speak`'s `point_at` (docs/design/voice-it-own-speech.md
§8.1, §8.3-§8.5; build plan §14, L9; swarm_voice/tools_visual.py, planner.py, prompts.py).

Against a fake swarm-api on an `httpx.MockTransport`, this holds that:

* the five tools are strict schemas, and the planner is offered them beside the
  catalogue's draws, the read tools and `speak`;
* `show_file`, `show_diff` and `show_log` send swarm-api a source and never a
  body, on the visual write the grant reaches, with the grant's headers;
* `add_note` and `notes_to_work` write to the grant's own project's notes,
  a note always spoken (`via: "voice"`), an anchor in swarm-api's form;
* input that could steer a call is refused before anything is sent;
* `speak`'s `point_at` reaches the page as a `focus` with the spoken summary as
  its caption; a refused one is recorded and the speech still plays;
* every route the tools call is on the grant's write list;
* a secret the planner knows never reaches swarm-api in a tool's arguments.

Every token and grant is built at runtime with `secrets`; nothing needs a cloud
credential or an emulator.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_api.voice import grant as vg  # noqa: E402

from swarm_voice import planner as pl  # noqa: E402
from swarm_voice import prompts, tools_read, tools_visual  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant  # noqa: E402

API = "http://swarm-api.test"
VID = "vv_" + "a" * 32
NID = "vn_" + "b" * 32


class FakeApi:
    """swarm-api's two visual writes, recording each request."""

    def __init__(self, *, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"code": "refused"})
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/visuals") and "visual" in body:
            return httpx.Response(200, json={"visual": {
                "visual_id": VID, "kind": body["visual"]["kind"], "version": 1,
                "parts": [], "lines": {"from": 1, "to": 3}, "body_ref": "tenants/x"}})
        if request.url.path.endswith("/visuals"):
            return httpx.Response(200, json={"focus": body["focus"]})
        if "to_work" in body:
            return httpx.Response(200, json={"work": {"kind": body["to_work"]["kind"],
                                                      "ref": "card/vc_1"},
                                             "notes": [{"note_id": NID}]})
        return httpx.Response(200, json={"note": {"note_id": NID,
                                                  "anchor": body["note"]["anchor"]}})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16),
                        session_id="vs_" + secrets.token_hex(16), value=secrets.token_urlsafe(32))


def _api(fake: FakeApi, grant: SessionGrant) -> tools_read.SwarmApi:
    id_token = "idt-" + secrets.token_hex(8)

    async def id_tokens() -> str:
        return id_token

    return tools_read.SwarmApi(fake.http, base_url=API, id_tokens=id_tokens, grant=grant)


def _run(coro):
    return asyncio.run(coro)


# ---- the schemas ------------------------------------------------------------


def test_the_five_tools_are_strict_schemas():
    schemas = tools_visual.tool_schemas()
    assert [s["name"] for s in schemas] == list(tools_visual.NAMES) == [
        "show_file", "show_diff", "show_log", "add_note", "notes_to_work"]

    def closed(schema: dict[str, Any], path: str) -> None:
        if "properties" in schema:
            assert schema["additionalProperties"] is False, path
            assert set(schema["required"]) == set(schema["properties"]), path
            for name, prop in schema["properties"].items():
                closed(prop, f"{path}.{name}")

    for schema in schemas:
        assert schema["strict"] is True
        closed(schema["input_schema"], schema["name"])
    closed(prompts.speak_tool()["input_schema"], "speak")
    assert "point_at" in prompts.speak_tool()["input_schema"]["required"]


def test_the_visual_tools_are_in_the_system_prompt_and_it_stays_fixed():
    for name in tools_visual.NAMES:
        assert f"`{name}`" in prompts.SYSTEM
    assert "point_at" in prompts.SYSTEM


def test_every_route_the_tools_call_is_on_the_grants_write_list():
    for route in (tools_visual.VISUAL_WRITE_ROUTE, tools_visual.NOTE_WRITE_ROUTE):
        assert ("POST", route) in vg.WRITE_ROUTES


# ---- show_file, show_diff, show_log: a source, never a body -----------------


def test_show_file_sends_the_source_alone_with_the_grant():
    fake, grant = FakeApi(), _grant()
    answer = _run(tools_visual.run_visual_tool(_api(fake, grant), "show_file", {
        "repo": "acme/widgets", "ref": "main", "path": "src/app.py", "from": 10, "to": None,
        "title": "app.py"}))
    assert answer["visual_id"] == VID and answer["lines"] == {"from": 1, "to": 3}
    [request] = fake.requests
    assert request.method == "POST"
    assert request.url.path == f"/v1/voice/sessions/{grant.session_id}/visuals"
    assert request.headers[GRANT_HEADER] == grant.headers()[GRANT_HEADER]
    assert request.headers[SESSION_HEADER] == f"{grant.project_id}/{grant.session_id}"
    visual = fake.bodies()[0]["visual"]
    assert visual["kind"] == "code" and visual["body"] is None
    assert visual["source"] == {"repo": "acme/widgets", "ref": "main", "path": "src/app.py",
                                "from": 10, "to": None}


def test_show_diff_and_show_log_send_their_sources():
    fake, grant = FakeApi(), _grant()
    api = _api(fake, grant)
    _run(tools_visual.run_visual_tool(api, "show_diff", {"repo": "acme/widgets", "pull": 42,
                                                         "title": "PR 42"}))
    _run(tools_visual.run_visual_tool(api, "show_log", {"task_id": "task_1", "step": None,
                                                        "title": "why it failed"}))
    diff, log = (b["visual"] for b in fake.bodies())
    assert diff["source"] == {"repo": "acme/widgets", "pull": 42} and diff["body"] is None
    assert log["kind"] == "log" and log["source"] == {"task_id": "task_1", "step": None}


@pytest.mark.parametrize("name,args", [
    ("show_file", {"repo": "../widgets", "ref": "main", "path": "a", "from": None, "to": None,
                   "title": "t"}),
    ("show_file", {"repo": "acme/widgets", "ref": "main", "path": "a", "from": 0, "to": None,
                   "title": "t"}),
    ("show_diff", {"repo": "acme/widgets", "pull": "42", "title": "t"}),
    ("show_log", {"task_id": "../x", "step": None, "title": "t"}),
    ("add_note", {"visual_id": "vv_nope", "text": "x", "anchor": None, "part": None}),
    ("add_note", {"visual_id": VID, "text": "x", "part": None,
                  "anchor": {"type": "region", "x": 0.1, "y": 0.1, "w": None, "h": None,
                             "part_id": None, "from": None, "to": None}}),
    ("notes_to_work", {"visual_id": VID, "note_ids": ["vn_x"], "kind": "issue", "title": None}),
    ("notes_to_work", {"visual_id": VID, "note_ids": [NID], "kind": "merge", "title": None}),
    ("draw_anything", {}),
])
def test_input_that_could_steer_a_call_is_refused_before_anything_is_sent(name, args):
    fake = FakeApi()
    with pytest.raises(tools_read.BadInput):
        _run(tools_visual.run_visual_tool(_api(fake, _grant()), name, args))
    assert fake.requests == []


# ---- add_note and notes_to_work ---------------------------------------------


def test_add_note_writes_a_spoken_note_on_the_grants_own_project():
    fake, grant = FakeApi(), _grant()
    answer = _run(tools_visual.run_visual_tool(_api(fake, grant), "add_note", {
        "visual_id": VID, "text": "make the header bigger", "part": None,
        "anchor": {"type": "lines", "x": None, "y": None, "w": None, "h": None,
                   "part_id": None, "from": 4, "to": 6}}))
    [request] = fake.requests
    assert request.url.path == f"/v1/voice/projects/{grant.project_id}/visuals/{VID}/notes"
    note = fake.bodies()[0]["note"]
    assert note == {"text": "make the header bigger", "via": "voice",
                    "anchor": {"type": "lines", "from": 4, "to": 6}, "part": None}
    assert answer == {"note_id": NID, "anchor": {"type": "lines", "from": 4, "to": 6}}


def test_add_note_with_no_anchor_leaves_the_place_to_the_pointer_or_the_part():
    fake = FakeApi()
    _run(tools_visual.run_visual_tool(_api(fake, _grant()), "add_note", {
        "visual_id": VID, "text": "this one", "anchor": None, "part": "the header"}))
    assert fake.bodies()[0]["note"]["anchor"] is None
    assert fake.bodies()[0]["note"]["part"] == "the header"


def test_notes_to_work_rides_on_the_note_write():
    fake, grant = FakeApi(), _grant()
    answer = _run(tools_visual.run_visual_tool(_api(fake, grant), "notes_to_work", {
        "visual_id": VID, "note_ids": [NID], "kind": "lane_brief", "title": None}))
    assert fake.bodies() == [{"to_work": {"note_ids": [NID], "kind": "lane_brief",
                                          "title": None}}]
    assert answer == {"work": {"kind": "lane_brief", "ref": "card/vc_1"}, "notes": 1}


# ---- the planner -------------------------------------------------------------


def _planner(fake: FakeApi) -> pl.Planner:
    async def id_tokens() -> str:
        return "idt-" + secrets.token_hex(8)

    return pl.Planner(fake.http, api_base=API, id_tokens=id_tokens, accounts=object(),
                      claude=object())


def test_the_planner_runs_a_visual_tool_and_records_it():
    fake, grant = FakeApi(), _grant()
    api = _api(fake, grant)
    result = pl.TurnResult(None, "")
    text, is_error, spoken = _run(_planner(fake)._tool(api, {"name": "show_diff", "input": {
        "repo": "acme/widgets", "pull": 7, "title": "PR 7"}}, set(), (), result))
    assert not is_error and spoken is None and VID in text and text.startswith("<data")
    assert result.visuals == [{"tool": "show_diff", "visual_id": VID}]


def test_a_refused_visual_tool_is_an_error_the_planner_records():
    fake = FakeApi(status=403)
    result = pl.TurnResult(None, "")
    text, is_error, _ = _run(_planner(fake)._tool(_api(fake, _grant()), {
        "name": "show_log", "input": {"task_id": "t1", "step": None, "title": "log"}},
        set(), (), result))
    assert is_error and "403" in text and result.refused == ["show_log"]


def test_a_known_secret_never_reaches_swarm_api_in_a_tools_arguments():
    fake, grant = FakeApi(), _grant()
    api = _api(fake, grant)
    known = ("k-" + secrets.token_hex(12), *api.known_secrets())
    _run(_planner(fake)._tool(api, {"name": "add_note", "input": {
        "visual_id": VID, "text": f"the key is {known[0]}", "anchor": None, "part": None}},
        set(), known, pl.TurnResult(None, "")))
    sent = fake.requests[0].content.decode()
    assert known[0] not in sent and grant.headers()[GRANT_HEADER] not in sent
    assert prompts.MASK in json.loads(sent)["note"]["text"]


def test_speaks_point_at_is_a_focus_with_the_spoken_summary_as_its_caption():
    fake, grant = FakeApi(), _grant()
    result = pl.TurnResult(None, "")
    anchor = {"type": "part", "x": None, "y": None, "w": None, "h": None,
              "part_id": "header", "from": None, "to": None}
    text, is_error, spoken = _run(_planner(fake)._tool(_api(fake, grant), {
        "name": "speak", "input": {"summary": "The header is here. It is small. Very small.",
                                   "point_at": {"visual_id": VID, "anchor": anchor}}},
        set(), (), result))
    assert (text, is_error) == ("said", False)
    assert spoken == "The header is here. It is small. Very small."
    [request] = fake.requests
    assert request.url.path == f"/v1/voice/sessions/{grant.session_id}/visuals"
    focus = fake.bodies()[0]["focus"]
    assert focus == {"visual_id": VID, "anchor": {"type": "part", "part_id": "header"},
                     "caption": "The header is here. It is small."}
    assert result.point_at == focus


def test_speak_without_point_at_sends_nothing():
    fake = FakeApi()
    result = pl.TurnResult(None, "")
    _run(_planner(fake)._tool(_api(fake, _grant()), {
        "name": "speak", "input": {"summary": "Done.", "point_at": None}}, set(), (), result))
    assert fake.requests == [] and result.point_at is None


def test_a_refused_point_at_is_recorded_and_the_speech_still_plays():
    fake = FakeApi(status=404)
    result = pl.TurnResult(None, "")
    text, is_error, spoken = _run(_planner(fake)._tool(_api(fake, _grant()), {
        "name": "speak", "input": {"summary": "Look here.",
                                   "point_at": {"visual_id": VID, "anchor": None}}},
        set(), (), result))
    assert (text, is_error, spoken) == ("said", False, "Look here.")
    assert result.point_at is None and result.refused == ["speak.point_at"]
