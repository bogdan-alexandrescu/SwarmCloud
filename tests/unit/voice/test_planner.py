"""swarm-voice turns each delegation into cards and a spoken summary with Claude Opus,
run as headless Claude Code on a Claude subscription account leased from the
quota broker's pool for one turn (docs/design/voice-it.md §3.6; section 10,
phase two, the planner row).

Against a fake quota broker and a fake swarm-api, both on one
`httpx.MockTransport`, and a fake `claude` executable that speaks MCP over
stdio to the planner's real MCP server (swarm_voice.mcp_server), this holds that:

* one delegation leases one account, gives one card written through swarm-api's
  card write path with the session grant and at most two sentences for the
  voice, and releases the account;
* the account is released even when the child fails, and a full pool is a
  spoken refusal that says so, with nothing run;
* the child's environment holds the leased token in CLAUDE_CODE_OAUTH_TOKEN,
  no API key and none of swarm-voice's own settings; built-in tools are off,
  only the planner's MCP server is loaded, nonessential traffic is off, and the
  private HOME is gone after the turn;
* the MCP server lists exactly the planner's tools: the catalogue's
  `draw_<kind>` tools, the read tools, the visual tools, the answer act tool
  and `speak`;
* an unknown kind is refused and never reaches swarm-api; a turn without a
  grant is refused before anything is leased or read;
* no secret value -- the leased token, the grant, the ID token -- appears in
  any log line, the transcript, the spoken summary or a card;
* a turn's usage is marked as a subscription's, for the ledger;
* the live session hands a `session.delegation.created` to the planner and
  speaks its result as `session.commentary.append` for that delegation.

Every token and grant is built at runtime with `secrets`; nothing needs a cloud
credential or an emulator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
import sys
from pathlib import Path

import httpx
import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_api.errors import ValidationFailed  # noqa: E402
from swarm_api.voice import cards as vcards  # noqa: E402
from swarm_api.voice import visuals as vvis  # noqa: E402

from swarm_voice import accountlease, claudeturn, mcp_server, plannerlease  # noqa: E402
from swarm_voice import planner as pl  # noqa: E402
from swarm_voice import prompts, tools_act, tools_read, tools_visual  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant  # noqa: E402

PERSON = "person@example.com"
API = "http://swarm-api.test"
BROKER = "http://broker.test"

#: The fake `claude`: records its argv, environment and HOME, starts the MCP
#: server from --mcp-config exactly as Claude Code does, lists its tools, calls
#: the planned ones and prints stream-json. It echoes the token in one event, as
#: a leaky CLI might, so the scrubbing is tested on the real captured stream.
FAKE_CLAUDE = r'''#!{python}
import json, os, subprocess, sys, time
PLAN = json.load(open({plan!r}))
argv = sys.argv[1:]
prompt = sys.stdin.read()
config = json.load(open(argv[argv.index("--mcp-config") + 1]))
server = config["mcpServers"]["planner"]
env = dict(os.environ); env.update(server.get("env") or {{}})
proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, env=env, text=True)
seq = [0]
def rpc(method, params=None):
    seq[0] += 1
    proc.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": seq[0], "method": method, "params": params or {{}}}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())
def out(event):
    print(json.dumps(event), flush=True)
init = rpc("initialize", {{"protocolVersion": "2025-06-18", "capabilities": {{}},
                          "clientInfo": {{"name": "fake", "version": "0"}}}})
proc.stdin.write(json.dumps({{"jsonrpc": "2.0", "method": "notifications/initialized"}}) + "\n")
listed = rpc("tools/list")["result"]["tools"]
def dump(value, path, call=None):
    # Whole or not at all: the planner can stop this process at any moment, so
    # each file is written under a sibling name and renamed onto its own, and a
    # reader sees the old contents or the new, never a truncated file.
    part = path + ".part"
    with open(part, "w") as fh:
        if call is not None and call == PLAN.get("die_writing_results"):
            os._exit(9)
        json.dump(value, fh)
    os.replace(part, path)
dump({{"argv": argv, "env": dict(os.environ), "cwd": os.getcwd(), "prompt": prompt,
      "tools": listed, "init": init, "home": os.environ.get("HOME")}}, {dump!r})
out({{"type": "system", "subtype": "init", "note": "token " + os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")}})
results = []
for i, (name, args) in enumerate(PLAN["calls"]):
    out({{"type": "assistant", "message": {{"id": "msg_%d" % i, "usage": {{"input_tokens": 100, "output_tokens": 20}},
         "content": [{{"type": "tool_use", "id": "t%d" % i, "name": "mcp__planner__" + name, "input": args}}]}}}})
    answer = rpc("tools/call", {{"name": name, "arguments": args}})
    results.append(answer)
    dump(results, {dump!r} + ".results", call=i)
    out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t%d" % i,
         "content": answer["result"]["content"]}}]}}}})
if PLAN.get("hang"):
    time.sleep(60)
if PLAN.get("exit"):
    sys.exit(PLAN["exit"])
out({{"type": "result", "subtype": "success", "is_error": False, "result": PLAN.get("final", ""),
     "usage": {{"input_tokens": 300, "output_tokens": 60}}, "total_cost_usd": 0.5}})
'''


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id="vs_" + secrets.token_hex(16),
                        value=secrets.token_urlsafe(32))


def _refs() -> dict:
    return {name: None for name in vcards.REFERENCES}


def _details_card(question: str = "Which repository should the fix land in?") -> dict:
    return {"card_id": None, "state": "waiting", "title": "Which repository?",
            "summary": "Pick the repository for the fix.", "references": _refs(),
            "body": {"question": question, "options": ["api", "console"], "free_text": True,
                     "answer": None, "task_id": None}}


class World:
    """The fake quota broker, the fake swarm-api and the fake `claude`."""

    def __init__(self, grant: SessionGrant, tmp: Path) -> None:
        self.grant = grant
        self.tmp = tmp
        self.token = "oat-" + secrets.token_hex(20)
        self.id_token = "idt-" + secrets.token_hex(16)
        self.broker_token = "bt-" + secrets.token_hex(16)
        #: The broker's answer to an assign: a lease, or a refusal reason.
        self.refuse: str | None = None
        self.broker_calls: list[tuple[str, dict]] = []
        self.api_requests: list[httpx.Request] = []
        self.cards: list[dict] = []
        self.visuals: list[dict] = []
        #: The visual each write landed on, in order.
        self.visual_ids: list[str] = []
        self.tasks = [{"task_id": "t-" + secrets.token_hex(4), "title": "Fix the login redirect",
                       "state": "FAILED"}]
        self.plan: dict = {"calls": []}
        self.dump = tmp / "claude-dump.json"
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    async def id_tokens(self) -> str:
        return self.id_token

    async def broker_id_token(self, audience: str) -> str:
        return self.broker_token

    # -- the wire
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "broker.test":
            return self.broker(request)
        if request.url.host == "swarm-api.test":
            return self.swarm_api(request)
        return httpx.Response(404)

    def broker(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {self.broker_token}"
        body = json.loads(request.content)
        self.broker_calls.append((request.url.path, body))
        if request.url.path == "/v1/accounts/assign":
            if self.refuse:
                return httpx.Response(200, json={"account_id": None, "assignment_id": None, "credential": None,
                                                 "reason": self.refuse, "next_reset_at": None})
            return httpx.Response(200, json={"account_id": "acct-1", "assignment_id": "as-" + secrets.token_hex(4),
                                             "credential": {"access_token": self.token}, "reason": "",
                                             "next_reset_at": None})
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json={"released": True})
        if request.url.path.endswith("/renew"):
            return httpx.Response(200, json={"expires_at": "2026-11-03T10:00:00+00:00"})
        return httpx.Response(404)

    def swarm_api(self, request: httpx.Request) -> httpx.Response:
        self.api_requests.append(request)
        if (request.headers.get("authorization") != f"Bearer {self.id_token}"
                or request.headers.get(GRANT_HEADER) != self.grant.headers()[GRANT_HEADER]
                or request.headers.get(SESSION_HEADER) != f"{self.grant.project_id}/{self.grant.session_id}"):
            return httpx.Response(401, json={"detail": "not granted"})
        path = request.url.path
        if request.method == "GET" and path == "/v1/voice/cards/schema":
            return httpx.Response(200, json=vcards.catalogue())
        if request.method == "GET" and path == "/v1/tasks":
            return httpx.Response(200, json={"items": self.tasks})
        if request.method == "POST" and path == f"/v1/voice/sessions/{self.grant.session_id}/cards":
            try:
                checked = vcards.validate(json.loads(request.content)["card"])
            except ValidationFailed as exc:
                return httpx.Response(422, json={"detail": str(exc)})
            # A card's id writes its next version, as swarm-api stores it.
            card_id = checked.get("card_id") or "vc_" + secrets.token_hex(16)
            version = sum(1 for c in self.cards if c["card_id"] == card_id) + 1
            self.cards.append({**checked, "card_id": card_id})
            return httpx.Response(201, json={"card": {**checked, "card_id": card_id, "version": version}})
        if request.method == "POST" and path == f"/v1/voice/sessions/{self.grant.session_id}/visuals":
            # swarm-api's own frame check, so a refused mock-up is refused here too.
            visual = json.loads(request.content)["visual"]
            try:
                _, parts = vvis.check_frames(visual["body"])
            except ValidationFailed as exc:
                return httpx.Response(422, json={"detail": str(exc)})
            self.visuals.append(visual)
            # A visual_id is the next version of that visual, as swarm-api stores it.
            visual_id = visual.get("visual_id") or "vv_" + secrets.token_hex(16)
            version = sum(1 for v in self.visual_ids if v == visual_id) + 1
            self.visual_ids.append(visual_id)
            return httpx.Response(200, json={"visual": {
                "visual_id": visual_id, "kind": "mockup", "version": version,
                "parts": parts, "lines": None}})
        return httpx.Response(404)

    def claude_binary(self) -> str:
        plan = self.tmp / "plan.json"
        plan.write_text(json.dumps(self.plan))
        script = self.tmp / "claude"
        script.write_text(FAKE_CLAUDE.format(python=sys.executable, plan=str(plan), dump=str(self.dump)))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(script)

    def planner(self) -> pl.Planner:
        broker = accountlease.VoiceBroker(self.http, BROKER, token_fetcher=self.broker_id_token)
        # The private HOME under the default temporary directory: a unix socket path
        # must stay under 108 bytes, which pytest's own tmp_path can pass.
        claude = claudeturn.ClaudeCode(binary=self.claude_binary())
        return pl.Planner(self.http, api_base=API, id_tokens=self.id_tokens,
                          accounts=plannerlease.PlannerAccounts(broker), claude=claude)

    def child(self) -> dict:
        return json.loads(self.dump.read_text())

    def secret_values(self) -> list[str]:
        return [self.token, self.grant.headers()[GRANT_HEADER], self.id_token]

    def paths(self) -> list[str]:
        return [p for p, _ in self.broker_calls]


def _delegate(world: World, transcript=None, grant="same") -> pl.TurnResult:
    async def main():
        try:
            return await world.planner().delegate(
                person=PERSON, grant=world.grant if grant == "same" else grant,
                transcript=transcript or [("person", "Ask me which repository the fix goes in.")],
                delegation_id="item_" + secrets.token_hex(6))
        finally:
            await world.http.aclose()

    return asyncio.run(main())


def _sentences(text: str) -> int:
    return len([p for p in prompts._SENTENCE.findall(text) if p.strip()])


@pytest.fixture
def world(tmp_path) -> World:
    return World(_grant(), tmp_path)


# ---------------------------------------------------------------- tests


def test_a_turn_leases_one_account_draws_one_card_speaks_two_sentences_and_releases(world):
    world.plan = {"calls": [
        ["draw_details_input", _details_card()],
        ["speak", {"summary": "The question is on screen. Pick a repository. Then I will plan the lanes."}],
    ]}
    result = _delegate(world)
    assert result.error is None, result.error
    assert [c["kind"] for c in result.cards] == ["details_input"] and len(world.cards) == 1
    # The planner's two sentences, then what the turn drew, counted (planner.drew).
    assert result.spoken == "The question is on screen. Pick a repository. I drew 1 card on screen."
    assert world.paths() == ["/v1/accounts/assign", "/v1/voice/lease/acct-1/release"]
    assign = world.broker_calls[0][1]
    assert assign == {"provider": "anthropic", "holder": "voice-planner", "person": PERSON,
                      "voice_session_id": world.grant.session_id, "key_tier": True}
    release = world.broker_calls[1][1]
    assert release["voice_session_id"] == world.grant.session_id and release["assignment_id"].startswith("as-")


def test_a_turn_that_drew_nothing_says_so_whatever_the_model_said(world):
    world.plan = {"calls": [["speak", {"summary": "The plan is on screen."}]]}
    result = _delegate(world)
    assert result.error is None and result.cards == [] and world.cards == []
    # MUTATION: speak the model's words alone and the voice hears a card that is not there.
    assert result.spoken == "The plan is on screen. " + pl.NOTHING_DRAWN


def test_the_account_is_released_even_when_the_child_fails(world):
    world.plan = {"calls": [], "exit": 3}
    result = _delegate(world)
    assert result.spoken == pl.SAY_FAILED and "exited 3" in result.error
    assert world.paths()[-1] == "/v1/voice/lease/acct-1/release"


def test_the_account_is_released_when_the_turn_runs_out_of_time(world):
    world.plan = {"calls": [], "hang": True}

    async def main():
        planner = world.planner()
        planner._turn_seconds = 3.0  # noqa: SLF001
        try:
            return await planner.delegate(person=PERSON, grant=world.grant, transcript=[("person", "hi")])
        finally:
            await world.http.aclose()

    result = asyncio.run(main())
    assert result.spoken == pl.SAY_FAILED and "out of time" in result.error
    assert world.paths()[-1] == "/v1/voice/lease/acct-1/release"


@pytest.mark.parametrize("reason,say", [
    (plannerlease.NO_ACCOUNT_AVAILABLE, pl.SAY_POOL_FULL),
    (plannerlease.NO_ACCOUNTS_REGISTERED, pl.SAY_NO_POOL),
])
def test_a_full_pool_is_a_spoken_refusal_and_nothing_runs(world, reason, say):
    world.refuse = reason
    world.plan = {"calls": [["speak", {"summary": "never"}]]}
    result = _delegate(world)
    assert result.spoken == say and reason in result.error
    assert world.paths() == ["/v1/accounts/assign"]
    assert not world.dump.exists() and world.cards == []


def test_the_child_holds_the_token_and_no_api_key_and_only_the_planners_tools(world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-" + secrets.token_hex(8))
    monkeypatch.setenv("SWARM_API_URL", API)
    world.plan = {"calls": [["speak", {"summary": "Nothing to draw."}]]}
    _delegate(world)
    child = world.child()
    env = child["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == world.token
    assert "ANTHROPIC_API_KEY" not in env and "SWARM_API_URL" not in env
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1" and env["DISABLE_TELEMETRY"] == "1"
    argv = child["argv"]
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__planner"
    assert "--no-session-persistence" in argv and "--strict-mcp-config" in argv
    assert world.grant.headers()[GRANT_HEADER] not in json.dumps(child)
    # The private HOME was the child's cwd and is gone after the turn.
    assert child["home"] == child["cwd"] and not os.path.exists(child["home"])
    # The MCP server lists exactly the planner's tools.
    names = {t["name"] for t in child["tools"]}
    draws = {t["name"] for t in vcards.catalogue()["tools"] if t["name"].startswith("draw_")}
    reads = {t["name"] for t in tools_read.tool_schemas()}
    visuals = {t["name"] for t in tools_visual.tool_schemas()}
    acts = {t["name"] for t in tools_act.tool_schemas()}
    assert names == draws | reads | visuals | acts | {prompts.SPEAK_TOOL}
    assert all("inputSchema" in t for t in child["tools"])
    assert child["init"]["result"]["serverInfo"]["name"] == "planner"


def test_a_read_tool_reads_swarm_api_with_the_grant_and_its_answer_is_fenced(world):
    world.plan = {"calls": [["list_agents", {"state": None, "workflow_id": None}], ["speak", {"summary": "One task failed."}]]}
    result = _delegate(world)
    assert result.reads == ["list_agents"]
    answers = json.loads(Path(str(world.dump) + ".results").read_text())
    text = answers[0]["result"]["content"][0]["text"]
    assert "Fix the login redirect" in text and not answers[0]["result"]["isError"]


def test_an_unknown_kind_is_refused_and_never_written(world):
    world.plan = {"calls": [["draw_invented_kind", {"title": "x"}], ["speak", {"summary": "No."}]]}
    result = _delegate(world)
    assert world.cards == [] and result.cards == []
    answers = json.loads(Path(str(world.dump) + ".results").read_text())
    assert answers[0]["result"]["isError"] is True
    assert not any(r.method == "POST" for r in world.api_requests)


def _page(name: str) -> str:
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>' + name + '</title><style>'
            'body{font-family:system-ui;margin:0}.hero{padding:64px}</style></head><body>'
            '<section class="hero"><h1>' + name + '</h1><a href="#start">Start</a></section>'
            '</body></html>')


def test_three_versions_asked_for_are_three_frames_drawn(world):
    frames = [{"label": n, "html": _page(n)} for n in ("Classic", "Bold", "Minimal")]
    world.plan = {"calls": [["show_mockup", {"title": "The landing page", "frames": frames,
                                             "visual_id": None}],
                            ["speak", {"summary": "Three versions are on screen."}]]}
    result = _delegate(world)
    # MUTATION: post each page's <meta charset> as written and swarm-api refuses the mock-up.
    assert result.frames_dropped == []
    assert [f["label"] for f in json.loads(world.visuals[-1]["body"])["frames"]] == [
        "Classic", "Bold", "Minimal"]
    assert result.spoken == "Three versions are on screen. I drew 1 mock-up on screen."


def _labels(visual: dict) -> list[str]:
    return [f["label"] for f in json.loads(visual["body"])["frames"]]


def test_a_mockup_of_three_frames_is_written_frame_by_frame_as_one_visual(world):
    frames = [{"label": n, "html": _page(n)} for n in ("Classic", "Bold", "Minimal")]
    world.plan = {"calls": [["show_mockup", {"title": "The landing page", "frames": frames,
                                             "visual_id": None}],
                            ["speak", {"summary": "Three versions are on screen."}]]}
    result = _delegate(world)
    # MUTATION: write all three frames in one call and the page shows nothing until the last.
    assert [_labels(v) for v in world.visuals] == [
        ["Classic"], ["Classic", "Bold"], ["Classic", "Bold", "Minimal"]]
    # Three updates of ONE visual: the first write names none, the next two name its id.
    assert world.visuals[0]["visual_id"] is None
    assert len(set(world.visual_ids)) == 1
    assert [v["visual_id"] for v in world.visuals[1:]] == world.visual_ids[:2]
    # Drawn once, not three times: the turn says one mock-up and traces its three frames.
    assert result.drawn == [{"kind": "mockup", "title": "The landing page",
                             "frames": ["Classic", "Bold", "Minimal"]}]
    assert len(result.visuals) == 1 and result.visuals[0]["visual_id"] == world.visual_ids[0]
    answers = json.loads(Path(str(world.dump) + ".results").read_text())
    answer = answers[0]["result"]["content"][0]["text"]
    assert world.visual_ids[0] in answer and '"version":3' in answer and '"asked":3' in answer


def _mockup_card(names: tuple[str, ...]) -> dict:
    return {"card_id": None, "state": "proposed", "title": "The landing page",
            "summary": "Three versions of the landing page.", "references": _refs(),
            "body": {"drawn_by": "planner", "viewport": "desktop", "height": None,
                     "frames": [{"name": n, "html": f"<h1>{n}</h1>", "blob_key": None} for n in names]}}


def test_a_mockup_card_of_three_frames_is_three_updates_of_one_card(world):
    world.plan = {"calls": [["draw_mockup", _mockup_card(("Classic", "Bold", "Minimal"))],
                            ["speak", {"summary": "Three versions are on screen."}]]}
    result = _delegate(world)
    # MUTATION: write the card with all three frames at once and the console waits for the last.
    assert [[f["name"] for f in c["body"]["frames"]] for c in world.cards] == [
        ["Classic"], ["Classic", "Bold"], ["Classic", "Bold", "Minimal"]]
    # One card: the first write draws it, the next two name its id.
    assert world.cards[0]["card_id"] == world.cards[1]["card_id"] == world.cards[2]["card_id"]
    assert [c["kind"] for c in result.cards] == ["mockup"] and len(result.drawn) == 1
    assert result.spoken == "Three versions are on screen. I drew 1 card on screen."
    answers = json.loads(Path(str(world.dump) + ".results").read_text())
    answer = answers[0]["result"]["content"][0]["text"]
    assert '"version":3' in answer and not answers[0]["result"]["isError"]


def test_a_card_without_frames_is_written_once(world):
    world.plan = {"calls": [["draw_details_input", _details_card()],
                            ["speak", {"summary": "The question is on screen."}]]}
    _delegate(world)
    assert len(world.cards) == 1


def test_a_frame_left_out_of_a_mockup_is_said_with_its_reason(world):
    frames = [{"label": "Classic", "html": _page("Classic")},
              {"label": "Bold", "html": _page("Bold")},
              {"label": "Minimal", "html": "<script>draw()</script>"}]
    world.plan = {"calls": [["show_mockup", {"title": "The landing page", "frames": frames,
                                             "visual_id": None}],
                            ["speak", {"summary": "The versions are on screen."}]]}
    result = _delegate(world)
    # Frame by frame, and the refused third frame writes no version of its own.
    assert [_labels(v) for v in world.visuals] == [["Classic"], ["Classic", "Bold"]]
    # MUTATION: drop the frame without a word and the person hears of no missing version.
    assert result.spoken.endswith(
        "I drew 1 mock-up on screen. I drew two of the three versions; the third, Minimal, was "
        "refused because it has a script tag, and a frame runs no code and loads nothing.")
    assert result.frames_dropped[0]["dropped"][0]["n"] == 3
    assert result.drawn == [{"kind": "mockup", "title": "The landing page",
                             "frames": ["Classic", "Bold"]}]


def test_a_dropped_frame_redrawn_in_the_same_turn_is_not_said_missing(world):
    frames = [{"label": "Classic", "html": _page("Classic")},
              {"label": "Bold", "html": _page("Bold")},
              {"label": "Minimal", "html": "<script>draw()</script>"}]
    redrawn = [{"label": n, "html": _page(n)} for n in ("Classic", "Bold", "Minimal")]
    world.plan = {"calls": [["show_mockup", {"title": "The landing page", "frames": frames,
                                             "visual_id": None}],
                            ["show_mockup", {"title": "The landing page", "frames": redrawn,
                                             "visual_id": None}],
                            ["speak", {"summary": "Three versions are on screen."}]]}
    result = _delegate(world)
    assert [f["label"] for f in json.loads(world.visuals[-1]["body"])["frames"]] == [
        "Classic", "Bold", "Minimal"]
    # MUTATION: keep the first call's dropped frame and the voice says a drawn version is missing.
    assert result.frames_dropped == []
    assert "refused" not in result.spoken and "of the three" not in result.spoken


def test_a_redraw_still_refused_leaves_the_missing_version_said_once(world):
    frames = [{"label": "Classic", "html": _page("Classic")},
              {"label": "Bold", "html": _page("Bold")},
              {"label": "Minimal", "html": "<script>draw()</script>"}]
    world.plan = {"calls": [["show_mockup", {"title": "The landing page", "frames": frames,
                                             "visual_id": None}],
                            ["show_mockup", {"title": "The landing page", "frames": frames[2:],
                                             "visual_id": None}],
                            ["speak", {"summary": "The versions are on screen."}]]}
    result = _delegate(world)
    assert [_labels(v) for v in world.visuals] == [["Classic"], ["Classic", "Bold"]]
    assert len(result.frames_dropped) == 1
    assert result.spoken.count("was refused because") == 1
    assert "I drew two of the three versions; the third, Minimal," in result.spoken


def test_a_child_stopped_while_writing_its_results_leaves_the_last_whole_results(world):
    # The planner stops the child once `speak` is answered, so the stop can land
    # while the fake is writing that call's results (a merge train read an empty
    # file, 5 November 2026). Here the child dies inside its second write: a
    # reader must see the first call's results whole, never a truncated file.
    reads = {"state": None, "workflow_id": None}
    world.plan = {"calls": [["list_agents", reads], ["list_agents", reads]], "die_writing_results": 1}
    result = _delegate(world)
    assert "exited 9" in result.error
    answers = json.loads(Path(str(world.dump) + ".results").read_text())
    assert len(answers) == 1 and not answers[0]["result"]["isError"]
    assert world.child()["home"]


def test_a_planner_call_without_the_grant_is_refused_before_anything_is_leased(world):
    result = _delegate(world, grant=None)
    assert result.spoken == pl.SAY_NO_GRANT and result.error
    assert world.broker_calls == [] and world.api_requests == [] and not world.dump.exists()


def test_no_secret_reaches_a_log_the_transcript_the_summary_a_card_or_the_prompt(world, caplog):
    caplog.set_level(logging.DEBUG)
    leak = world.token
    world.plan = {"calls": [
        ["draw_details_input", _details_card(question=f"Is {leak} the right key?")],
        ["speak", {"summary": f"Saved {leak}. Done."}],
    ]}
    result = _delegate(world, transcript=[("person", f"my token is {leak} and that's it")])
    child = world.child()
    for value in world.secret_values():
        assert value not in result.spoken
        assert value not in json.dumps(result.transcript)
        assert value not in json.dumps(world.cards)
        assert value not in caplog.text
        assert value not in child["prompt"]
    assert any(prompts.MASK in json.dumps(e) for e in result.transcript)


def test_a_turns_usage_is_a_subscriptions_never_a_price(world):
    world.plan = {"calls": [["speak", {"summary": "Done."}]]}
    result = _delegate(world)
    assert result.usage["billing"] == "subscription" and result.usage["model"] == "claude-fable-5-1"
    assert result.usage["input_tokens"] >= 100 and "cost_usd" not in result.usage


def test_the_token_never_shows_in_a_repr(world):
    lease = plannerlease.PlannerLease(account_id="acct-1", assignment_id="as-1", voice_session_id="vs_x",
                                      bearer=accountlease.Bearer(world.token))
    assert world.token not in repr(lease) and world.token not in str(lease)
    asyncio.run(world.http.aclose())


def test_the_mcp_server_answers_only_its_tools_methods():
    class Bridge:
        def ask(self, request):
            return {"tools": [{"name": "speak"}]} if request["op"] == "list" else {"text": "ok"}

    assert mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, Bridge())[2]["code"] == -32601
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, Bridge())[1] == {
        "tools": [{"name": "speak"}]}
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, Bridge())[0] is False


def test_the_bridge_refuses_a_wrong_nonce_and_a_tool_it_does_not_offer():
    async def handler(name, args):
        return "ran", False

    bridge = claudeturn.ToolBridge([prompts.speak_tool()], handler, "n" + secrets.token_hex(8))
    assert asyncio.run(bridge.answer({"op": "list", "nonce": "wrong"}))["is_error"] is True
    nonce = bridge._nonce  # noqa: SLF001
    assert asyncio.run(bridge.answer({"op": "call", "name": "Bash", "arguments": {}, "nonce": nonce}))[
        "is_error"] is True
    assert asyncio.run(bridge.answer({"op": "call", "name": "speak", "arguments": {}, "nonce": nonce})) == {
        "text": "ran", "is_error": False}


@pytest.mark.parametrize("text,expected", [
    ("One. Two. Three.", "One. Two."),
    ("Only one sentence", "Only one sentence"),
    ("Is it on screen? Yes! And more.", "Is it on screen? Yes!"),
])
def test_the_spoken_summary_keeps_two_sentences(text, expected):
    assert prompts.spoken(text) == expected


# ---------------------------------------------------------------- the live hook


def test_the_live_session_delegates_and_speaks_the_planners_result():
    from test_live_session import Clock, FakeBroker, FakeOpenAI, Harness, _start_body
    from websockets.asyncio.server import serve

    from swarm_voice import live
    from swarm_voice.main import create_app

    delegation_id = "item_" + secrets.token_hex(8)

    class DelegatingOpenAI(FakeOpenAI):
        async def sideband(self, conn) -> None:
            live_id = conn.request.path.strip("/").split("/")[3]
            self.attaches.append(live_id)
            await conn.send(json.dumps({"type": "session.updated", "session": {"id": live_id}}))
            await conn.send(json.dumps({"type": "session.input_transcript.done",
                                        "transcript": "Ask me which repository."}))
            await conn.send(json.dumps({"type": "session.delegation.created", "offset_ms": 1000,
                                        "delegation": {"id": delegation_id, "type": "delegation",
                                                       "target": "client"}}))
            async for raw in conn:
                event = json.loads(raw)
                self.received.append(event)
                if event.get("type") == "session.close":
                    await conn.send(json.dumps({"type": "session.closed", "reason": "client_close",
                                                "usage": {"seconds": 3}}))
                    await conn.close()
                    return

    class FakePlanner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def delegate(self, **kwargs):
            self.calls.append(kwargs)
            return pl.TurnResult(kwargs["delegation_id"], "The question is on screen.")

        def forget(self, session_id: str) -> None:
            pass

    async def main():
        openai = DelegatingOpenAI()
        planner = FakePlanner()
        grant = _grant()
        async with serve(openai.sideband, "127.0.0.1", 0) as server:
            h = Harness(openai, FakeBroker(openai), Clock(), server)
            h.sessions = live.Sessions(h.api, leases=h.leases, close_grace=2.0, planner=planner)
            h.app = create_app(sessions=h.sessions, verify_caller=lambda t: _ok(t))
            h.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://voice")
            try:
                body = {**_start_body(voice_id=grant.session_id), "project_id": grant.project_id,
                        "grant": grant.headers()[GRANT_HEADER], "planner_model": "claude-opus-5-5"}
                r = await h.post("/internal/sessions", body)
                assert r.status_code == 201, r.text
                create = openai.creates[0]["session"]
                assert create["delegation"] == {"type": "client"}
                assert create["instructions"] == (live.INSTRUCTIONS + live.DELEGATE_INSTRUCTIONS
                                                  + live.TURN_INSTRUCTIONS)
                for _ in range(200):
                    if any(e.get("type") == "session.commentary.append" for e in openai.received):
                        break
                    await asyncio.sleep(0.01)
                said = [e for e in openai.received if e.get("type") == "session.commentary.append"]
                assert said == [{"type": "session.commentary.append", "event_id": f"planner_{delegation_id}",
                                 "delegation_id": delegation_id, "content": "The question is on screen."}]
                call = planner.calls[0]
                assert call["person"] == "owner@example.com" and call["grant"] is not None
                assert call["grant"].session_id == grant.session_id
                assert call["transcript"] == [("person", "Ask me which repository.")]
                # The live engine plans on the model the start read from Settings too.
                assert call["model"] == "claude-opus-5-5"
            finally:
                await h.sessions.end_all()
                await h.close()

    async def _ok(token: str) -> str:
        if token != "caller-token":
            raise PermissionError("not swarm-api")
        return "swarm-api@example-project.iam.gserviceaccount.com"

    asyncio.run(asyncio.wait_for(main(), 20))


def test_a_sessions_planner_model_runs_the_child_on_it_and_none_keeps_the_pin(world, monkeypatch):
    """The Voice it setting's model, read at the session's start, is the
    child's `--model` for that turn alone; without one the planner keeps its
    pin (VOICE_PLANNER_MODEL, the planning class)."""
    monkeypatch.delenv(claudeturn.PLANNER_MODEL_ENV, raising=False)
    world.plan = {"calls": [["speak", {"summary": "Done."}]]}

    async def main():
        planner = world.planner()
        try:
            picked = await planner.delegate(person=PERSON, grant=world.grant, model="claude-opus-5-5",
                                            transcript=[("person", "Plan it.")])
            picked_argv = world.child()["argv"]
            pinned = await planner.delegate(person=PERSON, grant=world.grant,
                                            transcript=[("person", "Plan it.")])
            return picked, picked_argv, pinned, world.child()["argv"]
        finally:
            await world.http.aclose()

    picked, picked_argv, pinned, pinned_argv = asyncio.run(main())
    assert picked.error is None and pinned.error is None
    assert picked_argv[picked_argv.index("--model") + 1] == "claude-opus-5-5"
    assert picked.usage["model"] == "claude-opus-5-5"
    assert pinned_argv[pinned_argv.index("--model") + 1] == claudeturn.MODEL
    assert pinned.usage["model"] == claudeturn.MODEL


# ------------------------------------------- the fallback order (§5.3)


class SpentOnce:
    """A Claude Code stand-in: the first account it runs on is spent (the CLI's
    error result, as `claudeturn.note_result` reads it); the next one speaks."""

    model = claudeturn.MODEL

    def __init__(self, *, spent_every_time: bool = False) -> None:
        self.tokens: list[str] = []
        self.spent_every_time = spent_every_time

    def with_model(self, model: str) -> "SpentOnce":
        return self

    async def run(self, *, prompt, system, token, tools, handler, known, done) -> claudeturn.ChildOutcome:
        self.tokens.append(token)
        if len(self.tokens) == 1 or self.spent_every_time:
            outcome = claudeturn.ChildOutcome(exit_code=1)
            claudeturn.note_result(outcome, {"is_error": True, "result": "Credit balance is too low"})
            return outcome
        await handler(prompts.SPEAK_TOOL, {"summary": "Done.", "point_at": None})
        return claudeturn.ChildOutcome(exit_code=0, stopped=True, usage={"input_tokens": 10})


def _chain(world: World, tiers: list[str]):
    """The broker walks `tiers`, one account each, passing over `exclude`."""
    world.keys = {f"acct-{i}": "k-" + secrets.token_hex(12) for i in range(len(tiers))}

    def broker(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        world.broker_calls.append((request.url.path, body))
        if request.url.path == "/v1/accounts/assign":
            for i, tier in enumerate(tiers):
                if f"acct-{i}" not in body.get("exclude", []):
                    return httpx.Response(200, json={
                        "account_id": f"acct-{i}", "assignment_id": f"as-{i}", "tier": tier, "reason": "",
                        "credential": {"access_token": world.keys[f"acct-{i}"]}, "next_reset_at": None})
            return httpx.Response(200, json={"account_id": None, "assignment_id": None, "credential": None,
                                             "reason": plannerlease.NO_ACCOUNT_AVAILABLE, "next_reset_at": None})
        return httpx.Response(200, json={"released": True})

    world.broker = broker


def _delegate_on(world: World, claude: SpentOnce) -> pl.TurnResult:
    async def main():
        planner = world.planner()
        planner._claude = claude  # noqa: SLF001
        try:
            return await planner.delegate(person=PERSON, grant=world.grant,
                                          transcript=[("person", "Plan it.")])
        finally:
            await world.http.aclose()

    return asyncio.run(main())


def test_a_turn_on_a_spent_plan_moves_to_the_key_tier_and_runs_again(world, caplog):
    _chain(world, [accountlease.TIER_PLAN, accountlease.TIER_KEY])
    claude = SpentOnce()
    with caplog.at_level(logging.DEBUG):
        result = _delegate_on(world, claude)
    assert result.error is None and result.spoken.startswith("Done.")
    # The plan's token first, then the key's, handed over as a key.
    assert claude.tokens == [world.keys["acct-0"], world.keys["acct-1"]]
    assert not isinstance(claude.tokens[0], claudeturn.ApiKey) and isinstance(claude.tokens[1], claudeturn.ApiKey)
    # The spent plan given back, then the assign with it excluded; the key released at the end.
    assert world.paths() == ["/v1/accounts/assign", "/v1/voice/lease/acct-0/release", "/v1/accounts/assign",
                             "/v1/voice/lease/acct-1/release"]
    assert world.broker_calls[2][1]["exclude"] == ["acct-0"]
    assert result.tier == accountlease.TIER_KEY and result.usage["tier"] == accountlease.TIER_KEY
    # A key is billed per token, so the ledger prices it.
    assert result.usage["billing"] == pl.BILLING_KEY
    for key in world.keys.values():
        assert key not in caplog.text and key not in json.dumps(result.usage) and key not in result.spoken


def test_a_turn_on_a_plan_keeps_the_subscription_billing_and_names_its_tier(world):
    _chain(world, [accountlease.TIER_PLAN])
    claude = SpentOnce()
    claude.tokens.append("already-ran")  # the stand-in speaks on its first account
    result = _delegate_on(world, claude)
    assert result.error is None and result.tier == accountlease.TIER_PLAN
    assert result.usage["billing"] == pl.BILLING and result.usage["tier"] == accountlease.TIER_PLAN
    assert world.paths() == ["/v1/accounts/assign", "/v1/voice/lease/acct-0/release"]


def test_with_both_tiers_spent_the_turn_says_the_pool_is_full_and_holds_nothing(world, caplog):
    """The planner's chain has no tier 3 (Q2): a spent key is the end."""
    _chain(world, [accountlease.TIER_PLAN, accountlease.TIER_KEY])
    claude = SpentOnce(spent_every_time=True)
    with caplog.at_level(logging.DEBUG):
        result = _delegate_on(world, claude)
    assert len(claude.tokens) == 2
    assert result.spoken == pl.SAY_POOL_FULL and "no tier is left" in (result.error or "")
    assert world.broker_calls[-1][0] == "/v1/accounts/assign"
    assert world.broker_calls[-1][1]["exclude"] == ["acct-0", "acct-1"]
    released = [p for p in world.paths() if p.endswith("/release")]
    assert released == ["/v1/voice/lease/acct-0/release", "/v1/voice/lease/acct-1/release"]
    for key in world.keys.values():
        assert key not in caplog.text
