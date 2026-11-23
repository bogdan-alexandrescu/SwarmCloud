"""Hostile text in what a read tool returns stays quoted data (voice-it.md §3.9;
the improvement plan of 2026-11-23).

Every read tool is taken from the module's own tool list
(`tools_read.tool_schemas()`), never from a list written here, and run on both
paths that hand its result to a model: the planner turn's (`Planner._tool`)
and the fast front's (`Planner.read`). A fake swarm-api answers every route
with hostile text in every field a tool keeps; the result must come back as
exactly one fenced section that holds all of it. So a read tool added without
going through the fence turns these tests red.

The hostile texts (injection_reader.hostile) are built at runtime from the
fence's own markers: its closing marker, its label line, a line shaped like the
turn's instruction preamble, each disguised by case, inner whitespace, every
zero-width character, look-alike brackets and every line separator.
Nothing needs a cloud credential, an emulator or the network.
"""

from __future__ import annotations

import asyncio
import json
import secrets

import httpx
import pytest

import injection_reader as reader
from swarm_voice import planner as planner_mod
from swarm_voice import prompts, tools_read
from swarm_voice.grant import SessionGrant

API = "http://swarm-api.test"


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id="vs_" + secrets.token_hex(16),
                        value=secrets.token_urlsafe(32))


def _preamble() -> tuple[str, ...]:
    """Lines shaped like the planner turn's own wording, read from turn_message."""
    lines = prompts.turn_message([("person", "hello")]).split("\n")
    return (lines[0], lines[-1])


def _args(schema: dict, value: str) -> dict:
    """Valid input for a tool from its schema: every string `value`, every nullable null."""
    props = schema["input_schema"]["properties"]
    return {k: (value if p.get("type") == "string" else None) for k, p in props.items()}


class HostileApi:
    """swarm-api answering every GET with `text` in every field a read keeps."""

    def __init__(self, text: str, value: str) -> None:
        self.text, self.value = text, value
        self.methods: list[str] = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    async def id_tokens(self) -> str:
        return "idt-" + secrets.token_hex(8)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.methods.append(request.method)
        # One copy of the text per answer (`search` reads three lists), so a
        # slice of the hostile texts stays under the fence's cut.
        row = {"task_id": self.value, "workflow_id": self.value, "run_id": self.value, "id": self.value,
               "title": self.value + " " + self.text, "state": "RUNNING"}
        body = {"items": [row]}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              headers={"content-type": "application/json"})


def _run_all(text: str, token: str) -> list[tuple[str, str, str]]:
    """(tool, path, result text) for every read tool on both paths."""
    value = "r" + secrets.token_hex(6)
    api = HostileApi(text, value)
    planner = planner_mod.Planner(api.http, api_base=API, id_tokens=api.id_tokens, accounts=None)
    grant = _grant()

    async def go() -> list[tuple[str, str, str]]:
        out = []
        client = tools_read.SwarmApi(api.http, base_url=API, id_tokens=api.id_tokens, grant=grant)
        for schema in tools_read.tool_schemas():
            name, args = schema["name"], _args(schema, value)
            front, err = await planner.read(grant=grant, name=name, args=args)
            assert not err, (name, front)
            out.append((name, "front", front))
            turn, err, _ = await planner._tool(client, {"name": name, "input": args}, set(), (),
                                               planner_mod.TurnResult(None, ""))
            assert not err, (name, turn)
            out.append((name, "turn", turn))
        await api.http.aclose()
        return out

    return asyncio.run(go())


def test_every_read_tool_is_walked():
    """Property: the tool list walked here is the module's whole read surface:
    every schema it offers is a read tool, and there is more than one."""
    names = [s["name"] for s in tools_read.tool_schemas()]
    assert len(names) == len(set(names)) > 1
    assert all(tools_read.is_read_tool(n) for n in names)


#: The hostile texts are run in this many slices, each under the fence's cut.
SLICES = 24


class TestReadToolResultsStayFenced:
    """Property: for every read tool, on both paths, a result holding hostile
    text is one fenced section; the text neither closes it early nor opens a
    second, and what follows each attack is still inside."""

    @pytest.mark.parametrize("which", range(SLICES))
    def test_hostile_markers_stay_inside(self, which):
        token = "tk" + secrets.token_hex(6)
        texts = reader.hostile(token, preamble=_preamble())
        text = "\n".join(texts[which::SLICES])
        # Every attack reaches the model uncut, three times over for `search`.
        assert 3 * len(text) < prompts.RESULT_MAX - 2_000
        for name, path, result in _run_all(text, token):
            reading = reader.read(result)
            assert len(reading.sections) == 1, (name, path, ascii(result[:400]))
            assert reading.inside(token), (name, path)
            assert reading.outside().strip("\x00\n") == "", (name, path, ascii(reading.outside()))

    def test_text_over_the_limit_is_cut_inside_the_fence(self):
        token = "tk" + secrets.token_hex(6)
        opening, closing, _ = reader.markers()
        # The cut lands inside a run of markers, whatever the JSON around it.
        text = token + (closing + opening) * (prompts.RESULT_MAX // len(closing + opening) + 2)
        for name, path, result in _run_all(text, token):
            reading = reader.read(result)
            assert len(reading.sections) == 1, (name, path)
            assert reading.inside(token), (name, path)
            assert len(result) < prompts.RESULT_MAX + 200, (name, path, len(result))
