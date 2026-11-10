"""The planner's mock-ups: shown, commented on and approved (docs/design/voice-it.md
§4; voice-it-own-speech.md §8.1, §8.3; swarm_voice/tools_visual.py, prompts.py, live.py).

Against a fake swarm-api on an `httpx.MockTransport`, this holds that:

* `show_mockup` posts ONE mockup visual per proposal holding its frames, each a
  self-contained HTML document with an id the person can name, on the visual
  write the grant reaches; drawing it again names the visual;
* `approve_mockup` records the person's spoken pick of a frame on the same
  write, and `approved_mockups` reads back what was approved;
* a comment on a frame is a note anchored to that visual and that frame;
* the planner prompt names the tools and when to draw a mock-up, and the GPT
  voice engine hands "show me" to the planner.

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

from swarm_voice import live, prompts, tools_read, tools_visual  # noqa: E402
from swarm_voice.grant import SessionGrant  # noqa: E402

API = "http://swarm-api.test"
VID = "vv_" + "c" * 32
NID = "vn_" + "d" * 32
MARK = {"frame_id": "frame-2", "label": "List", "by": "alice@example.com", "via": "voice",
        "at": "2026-11-10T11:00:00+00:00"}


class FakeApi:
    """swarm-api's visual and note writes, recording each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content or b"{}")
        if "visual" in body:
            frames = json.loads(body["visual"]["body"])["frames"]
            return httpx.Response(200, json={"visual": {
                "visual_id": VID, "kind": "mockup", "version": 1, "lines": None,
                "parts": [{"id": f["id"], "label": f["label"]} for f in frames]}})
        if "approve" in body:
            return httpx.Response(200, json={"visual": {"visual_id": VID, "kind": "mockup",
                                                        "title": "Shop", "approved": MARK}})
        if "approved" in body:
            return httpx.Response(200, json={"approved": [{"visual_id": VID, "title": "Shop",
                                                           "version": 1, **MARK}]})
        return httpx.Response(200, json={"note": {"note_id": NID, "anchor": None}})

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _api(fake: FakeApi) -> tuple[tools_read.SwarmApi, SessionGrant]:
    grant = SessionGrant(project_id="vp_" + secrets.token_hex(16),
                         session_id="vs_" + secrets.token_hex(16),
                         value=secrets.token_urlsafe(32))

    async def id_tokens() -> str:
        return "idt-" + secrets.token_hex(8)

    return tools_read.SwarmApi(fake.http, base_url=API, id_tokens=id_tokens, grant=grant), grant


def _run(coro):
    return asyncio.run(coro)


FRAMES = [{"label": "Cards", "html": "<!doctype html><h1>Shop</h1><p>cards</p>"},
          {"label": "List", "html": "<!doctype html><h1>Shop</h1><ul><li>a</li></ul>"}]


def test_show_mockup_posts_one_mockup_visual_with_its_frames():
    fake = FakeApi()
    api, grant = _api(fake)
    answer = _run(tools_visual.run_visual_tool(api, "show_mockup", {
        "title": "The shop page", "frames": FRAMES, "visual_id": None}))
    (request,) = fake.requests
    assert request.method == "POST"
    assert request.url.path == f"/v1/voice/sessions/{grant.session_id}/visuals"
    visual = fake.bodies()[0]["visual"]
    assert visual["kind"] == "mockup" and visual["title"] == "The shop page"
    assert visual["source"] is None and visual["visual_id"] is None
    assert json.loads(visual["body"]) == {"frames": [
        {"id": "frame-1", **FRAMES[0]}, {"id": "frame-2", **FRAMES[1]}]}
    assert answer["visual_id"] == VID
    assert answer["frames"] == [{"id": "frame-1", "label": "Cards"},
                                {"id": "frame-2", "label": "List"}]


def test_show_mockup_drawn_again_names_its_visual():
    fake = FakeApi()
    api, _ = _api(fake)
    _run(tools_visual.run_visual_tool(api, "show_mockup", {
        "title": "The shop page", "frames": FRAMES[:1], "visual_id": VID}))
    assert fake.bodies()[0]["visual"]["visual_id"] == VID


@pytest.mark.parametrize("args", [
    {"title": "x", "frames": [], "visual_id": None},
    {"title": "x", "frames": [{"label": "a", "html": ""}], "visual_id": None},
    {"title": "x", "frames": [{"label": "", "html": "<p>a</p>"}], "visual_id": None},
    {"title": "x", "frames": [{"label": "a", "html": "<p>a</p>"}] * 13, "visual_id": None},
    {"title": "x", "frames": FRAMES, "visual_id": "../vv"},
])
def test_show_mockup_input_that_is_not_frames_is_refused_before_anything_is_sent(args):
    fake = FakeApi()
    api, _ = _api(fake)
    with pytest.raises(tools_read.BadInput):
        _run(tools_visual.run_visual_tool(api, "show_mockup", args))
    assert fake.requests == []


def test_approve_mockup_records_the_spoken_pick_on_the_visual_write():
    fake = FakeApi()
    api, grant = _api(fake)
    answer = _run(tools_visual.run_visual_tool(api, "approve_mockup", {
        "visual_id": VID, "frame": "frame-2"}))
    (request,) = fake.requests
    assert request.url.path == f"/v1/voice/sessions/{grant.session_id}/visuals"
    assert fake.bodies() == [{"approve": {"visual_id": VID, "frame": "frame-2"}}]
    assert answer == {"visual_id": VID, "title": "Shop", "approved": MARK}


def test_approved_mockups_reads_what_was_approved():
    fake = FakeApi()
    api, _ = _api(fake)
    answer = _run(tools_visual.run_visual_tool(api, "approved_mockups", {}))
    assert fake.bodies() == [{"approved": True}]
    assert answer == {"approved": [{"visual_id": VID, "title": "Shop", "version": 1, **MARK}]}


def test_a_comment_on_a_frame_is_a_note_anchored_to_that_visual_and_frame():
    fake = FakeApi()
    api, grant = _api(fake)
    _run(tools_visual.run_visual_tool(api, "add_note", {
        "visual_id": VID, "text": "make the cards bigger",
        "anchor": {"type": "part", "x": None, "y": None, "w": None, "h": None,
                   "part_id": "frame-1", "from": None, "to": None}, "part": None}))
    (request,) = fake.requests
    assert request.url.path == f"/v1/voice/projects/{grant.project_id}/visuals/{VID}/notes"
    assert fake.bodies()[0]["note"]["anchor"] == {"type": "part", "part_id": "frame-1"}


def test_the_planner_prompt_names_the_mockup_tools_and_when_to_draw_them():
    for name in ("show_mockup", "approve_mockup", "approved_mockups", "add_note"):
        assert f"`{name}`" in prompts.SYSTEM
    assert "see something" in prompts.SYSTEM and "a page or a screen" in prompts.SYSTEM


def test_the_gpt_voice_engine_hands_a_show_me_request_to_the_planner():
    assert "shown" in live.DELEGATE_INSTRUCTIONS
