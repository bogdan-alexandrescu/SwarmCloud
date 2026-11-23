"""The read path offers no tool that acts (voice-it.md §3.9: the defence against
injection is structural; the improvement plan of 2026-11-23).

A read-only turn -- the fast front's (claudefront.front_tools, run on
`Planner.read`) and the read tool list itself -- is handed no name the act
module (tools_act.py) defines, and the read path refuses an act name when the
model calls one anyway, before any request is made. Both sets of names are
read from the modules, never written here; nothing needs the network.
"""

from __future__ import annotations

import asyncio
import secrets
from types import ModuleType

import httpx

import injection_reader  # noqa: F401 - puts swarm-voice on the path
from swarm_voice import claudefront, tools_act, tools_read
from swarm_voice import planner as planner_mod
from swarm_voice.grant import SessionGrant

API = "http://swarm-api.test"


def _schema_names(module: ModuleType) -> set[str]:
    return {s["name"] for s in module.tool_schemas()}


def _act_names() -> set[str]:
    """Every tool name tools_act.py defines: its schemas and its name list."""
    names = _schema_names(tools_act) | set(tools_act.NAMES)
    assert names, "tools_act.py defines no act tool: this test reads the wrong module"
    return names


class TestReadPathOffersNoAct:
    """Property: no tool set handed to a read-only turn holds an act tool's name,
    and the read path refuses an act name without sending anything."""

    def test_act_module_lists_each_tool_once(self):
        """Every act schema is on the act list and is an act tool, so the names
        read here are the whole act surface."""
        assert _schema_names(tools_act) == set(tools_act.NAMES)
        assert all(tools_act.is_act_tool(n) for n in _act_names())

    def test_read_tools_hold_no_act_name(self):
        reads = _schema_names(tools_read)
        assert reads and not reads & _act_names()
        assert not any(tools_read.is_read_tool(n) for n in _act_names())
        assert not any(tools_act.is_act_tool(n) for n in reads)

    def test_front_tools_hold_no_act_name(self):
        front = {t["name"] for t in claudefront.front_tools()}
        assert _schema_names(tools_read) <= front
        assert not front & _act_names()

    def test_read_path_refuses_an_act_name_unsent(self):
        sent: list[httpx.Request] = []
        http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={})))

        async def id_tokens() -> str:
            return "idt-" + secrets.token_hex(8)

        planner = planner_mod.Planner(http, api_base=API, id_tokens=id_tokens, accounts=None)
        grant = SessionGrant(project_id="vp_" + secrets.token_hex(16),
                             session_id="vs_" + secrets.token_hex(16), value=secrets.token_urlsafe(32))

        async def go() -> list[tuple[str, bool]]:
            out = []
            for name in sorted(_act_names()):
                schema = next(s for s in tools_act.tool_schemas() if s["name"] == name)
                args = {k: None for k in schema["input_schema"]["properties"]}
                out.append(await planner.read(grant=grant, name=name, args=args))
            await http.aclose()
            return out

        for text, is_error in asyncio.run(go()):
            assert is_error, text
        assert sent == []

    def test_every_read_tool_sends_only_get(self):
        methods: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json={"items": []})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handle))

        async def id_tokens() -> str:
            return "idt-" + secrets.token_hex(8)

        grant = SessionGrant(project_id="vp_" + secrets.token_hex(16),
                             session_id="vs_" + secrets.token_hex(16), value=secrets.token_urlsafe(32))
        api = tools_read.SwarmApi(http, base_url=API, id_tokens=id_tokens, grant=grant)
        value = "r" + secrets.token_hex(6)

        async def go() -> None:
            for schema in tools_read.tool_schemas():
                props = schema["input_schema"]["properties"]
                args = {k: (value if p.get("type") == "string" else None) for k, p in props.items()}
                await tools_read.run_read_tool(api, schema["name"], args)
            await http.aclose()

        asyncio.run(go())
        assert methods and set(methods) == {"GET"}
