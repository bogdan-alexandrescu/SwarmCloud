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
  voice engine hands "show me" to the planner;
* every frame asked for is drawn: whole landing pages (a charset meta, a
  viewport, in-page links) pass this tool's check and swarm-api's own
  `check_frames`; a frame that breaks a rule is repaired by the same rules, one
  that cannot be is left out with its reason while the others draw, and the
  voice names the frame left out.

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

from swarm_api.errors import ValidationFailed  # noqa: E402
from swarm_api.voice import visuals as vvis  # noqa: E402
from swarm_voice import live, prompts, tools_read, tools_visual  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
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


# ---- every frame asked for is drawn (the 11 November three-version refusal) ---

_HEAD = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width, initial-scale=1"><title>{name}</title>'
         '<style>body{{margin:0;font-family:system-ui,sans-serif;color:#1f2937}}'
         'header{{display:flex;justify-content:space-between;padding:16px 32px}}'
         '.hero{{padding:80px 32px;text-align:center;background:linear-gradient(135deg,#eef2ff,#fdf2f8)}}'
         '.cta{{padding:12px 24px;border-radius:8px;background:#4f46e5;color:#fff}}'
         '.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;padding:48px 32px}}'
         '</style></head>')
_BODY = ('<body><header><strong>Brand</strong><nav><a href="#features">Features</a> '
         '<a href="#pricing">Pricing</a></nav></header><section class="hero"><h1>{name}</h1>'
         '<p>A short promise of what the product does.</p><a class="cta" href="#signup">Get started</a>'
         '</section><section class="grid" id="features"><div><h3>Fast</h3></div><div><h3>Simple</h3>'
         '</div><div><h3>Safe</h3></div></section>{extra}<footer>&copy; Brand</footer></body></html>')


def landing_page(name: str, extra: str = "") -> str:
    """A whole landing page as a model writes one: a charset and a viewport
    meta, a title, one style block, in-page links."""
    return _HEAD.format(name=name) + _BODY.format(name=name, extra=extra)


THREE = [{"label": name, "html": landing_page(name)} for name in ("Classic", "Bold", "Minimal")]


def _drawn_frames(fake: FakeApi) -> list[dict[str, str]]:
    return json.loads(fake.bodies()[0]["visual"]["body"])["frames"]


def test_three_typical_landing_page_frames_pass_the_tools_check_and_swarm_apis():
    fake = FakeApi()
    api, _ = _api(fake)
    answer = _run(tools_visual.run_visual_tool(api, "show_mockup", {
        "title": "The landing page", "frames": THREE, "visual_id": None}))
    # MUTATION: post a whole page's <meta charset> as it is and swarm-api refuses all three.
    _, parts = vvis.check_frames(fake.bodies()[0]["visual"]["body"])
    assert [p["id"] for p in parts] == ["frame-1", "frame-2", "frame-3"]
    frames = _drawn_frames(fake)
    assert [f["label"] for f in frames] == ["Classic", "Bold", "Minimal"]
    assert all(tools_visual.frame_refusal(f["html"]) is None for f in frames)
    assert answer["asked"] == 3 and "dropped" not in answer
    assert [r["n"] for r in answer["repaired"]] == [1, 2, 3]
    assert all("<h1>" in f["html"] and "<style>" in f["html"] for f in frames)


@pytest.mark.parametrize("html", [
    '<meta charset="utf-8"><p>a</p>',
    '<meta http-equiv="refresh" content="0;url=https://example.com"><p>a</p>',
    '<link rel="stylesheet" href="https://fonts.example.com/css"><p>a</p>',
    "<style>@import url('https://example.com/x.css'); p{color:red}</style><p>a</p>",
    '<p>a</p><img src="https://example.com/hero.png" alt="">',
    '<p>a</p><img src="//example.com/x.png">',
    '<p>a</p><a href="/pricing">Pricing</a><a href="mailto:hi@example.com">Mail</a>',
    '<div style="background:url(https://example.com/x.png)">a</div>',
    '<form action="https://example.com"><input type="email"><button>Join</button></form>',
    '<button onclick="go()">a</button><a href="javascript:go()">b</a>',
    '<p>a</p><script>go()</script><iframe src="https://example.com"></iframe>',
])
def test_a_frame_the_server_refuses_is_repaired_by_the_same_rules(html):
    assert tools_visual.frame_refusal(html) is not None
    body = json.dumps({"frames": [{"id": "frame-1", "label": "One", "html": html}]})
    with pytest.raises(ValidationFailed):
        vvis.check_frames(body)
    repaired, changed = tools_visual.repair_frame(html)
    assert changed and tools_visual.frame_refusal(repaired) is None
    body = json.dumps({"frames": [{"id": "frame-1", "label": "One", "html": repaired}]})
    vvis.check_frames(body)
    assert "example.com" not in repaired.replace("hi@example.com", "")


def test_a_frame_with_an_outside_address_is_repaired_and_the_others_still_draw():
    fake = FakeApi()
    api, _ = _api(fake)
    frames = [THREE[0],
              {"label": "Bold", "html": landing_page(
                  "Bold", '<img src="https://images.example.com/hero.jpg" alt="hero">'
                          '<a href="/pricing">See pricing</a>')},
              THREE[2]]
    answer = _run(tools_visual.run_visual_tool(api, "show_mockup", {
        "title": "The landing page", "frames": frames, "visual_id": None}))
    drawn = _drawn_frames(fake)
    assert [f["label"] for f in drawn] == ["Classic", "Bold", "Minimal"]
    assert "images.example.com" not in drawn[1]["html"] and 'href="/pricing"' not in drawn[1]["html"]
    assert "See pricing" in drawn[1]["html"]
    vvis.check_frames(fake.bodies()[0]["visual"]["body"])
    assert any("outside addresses" in c for c in answer["repaired"][1]["changed"])


def test_a_frame_that_cannot_be_repaired_is_left_out_with_its_reason_and_the_others_draw():
    fake = FakeApi()
    api, _ = _api(fake)
    frames = [THREE[0], {"label": "Bold", "html": "<script>draw()</script>"}, THREE[2]]
    answer = _run(tools_visual.run_visual_tool(api, "show_mockup", {
        "title": "The landing page", "frames": frames, "visual_id": None}))
    assert [f["label"] for f in _drawn_frames(fake)] == ["Classic", "Minimal"]
    assert [f["id"] for f in _drawn_frames(fake)] == ["frame-1", "frame-2"]
    assert answer["dropped"] == [{"n": 2, "label": "Bold",
                                  "why": "it has a script tag, and a frame runs no code and loads nothing"}]


def test_when_no_frame_can_be_drawn_nothing_is_sent_and_the_planner_hears_which_and_why():
    fake = FakeApi()
    api, _ = _api(fake)
    with pytest.raises(tools_visual.FramesRefused) as refused:
        _run(tools_visual.run_visual_tool(api, "show_mockup", {
            "title": "x", "frames": [{"label": "Only", "html": "<script>x()</script>"}],
            "visual_id": None}))
    assert fake.requests == []
    assert "frame 1 (Only)" in str(refused.value) and "script tag" in str(refused.value)


def test_the_tools_description_and_the_planner_prompt_say_what_a_frame_may_hold():
    (mockup,) = [s for s in tools_visual.tool_schemas() if s["name"] == "show_mockup"]
    for text in (mockup["description"], prompts.SYSTEM):
        assert tools_visual.FRAME_RULES in text
    for rule in ("<meta charset>", "<link>", "<form>", "@import", "data: URI", "# or #id"):
        assert rule in tools_visual.FRAME_RULES


def test_the_spoken_line_names_a_dropped_frame_and_why():
    result = pl.TurnResult(None, "")
    result.frames_dropped.append({"title": "The landing page", "asked": 3, "drawn": 2, "dropped": [
        {"n": 3, "label": "Minimal", "why": "it has a script tag, and a frame runs no code"}]})
    assert pl.frames_not_drawn(result) == (
        "I drew two of the three versions; the third, Minimal, was refused because it has a "
        "script tag, and a frame runs no code.")
    assert pl.frames_not_drawn(pl.TurnResult(None, "")) == ""
