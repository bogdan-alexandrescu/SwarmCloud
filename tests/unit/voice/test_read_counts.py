"""The planner answers "how many agents are running" from one read (voice-it §3.3).

The live finding (12 November 2026): asked "how many agents do I have running
right now", the planner read the running agents (25 rows, no total), the
workflows twice and `search` twice, and the turn ended with no answer. So:

* a "how many agents are running" read is ONE `list_agents` call, and its
  answer carries `total` beside the rows;
* the total is the number of running agents swarm-api serves, past 25 and past
  one page of swarm-api's, by following the route's own `next_page_token`;
* the planner is told to count with a list tool, never with `search`, and
  `search` says it does not count;
* the fast front (claudefront.py) runs the same read itself on
  `Planner.read`, so a count is one haiku-class round, never a delegation
  (the owner's pick on #3954);
* every read, every page of it, is on the same route with the session grant's
  headers and never names a tenant or another person: the grant's tenant is
  swarm-api's to apply (invariant 9), and `submitted_by` takes only `me`.

The fake swarm-api serves two tenants and picks the caller's by the grant, as
swarm-api does; nothing needs a cloud credential or an emulator.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import httpx
import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import prompts, tools_read  # noqa: E402
from swarm_voice.grant import GRANT_HEADER, SESSION_HEADER, SessionGrant  # noqa: E402

API = "http://swarm-api.test"
ME = "person@example.com"
OTHER = "other@example.com"
LIVE = ("LEASED", "DISPATCHED", "STARTING", "RUNNING")


def _grant() -> SessionGrant:
    return SessionGrant(project_id="vp_" + secrets.token_hex(16), session_id="vs_" + secrets.token_hex(16),
                        value=secrets.token_urlsafe(32))


class FakeApi:
    """swarm-api's `GET /v1/tasks` and `GET /v1/workflows`: the grant's tenant
    only, newest first, `limit` capped at 200, keyset pages by `page_token`."""

    MAX_PAGE = 200

    def __init__(self, grant: SessionGrant, *, running: int, mine: int = 0) -> None:
        self.grant = grant
        self.id_token = "idt-" + secrets.token_hex(16)
        self.requests: list[httpx.Request] = []
        rows = [{"task_id": f"t-{i:04d}", "title": f"Agent {i}", "state": "RUNNING",
                 "submitted_by": ME if i < mine else OTHER} for i in range(running)]
        rows += [{"task_id": f"t-done-{i}", "title": f"Done {i}", "state": "SUCCEEDED",
                  "submitted_by": ME} for i in range(30)]
        workflows = [{"workflow_id": f"wf-{i}", "title": f"Workflow {i}", "state": "RUNNING"}
                     for i in range(running)]
        # Another tenant's work, which no read through this grant may see.
        foreign = [{"task_id": f"x-{i}", "title": "Foreign", "state": "RUNNING",
                    "submitted_by": ME} for i in range(500)]
        self.tenants = {"mine": {"tasks": rows, "workflows": workflows},
                        "theirs": {"tasks": foreign, "workflows": []}}
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    async def id_tokens(self) -> str:
        return self.id_token

    def tenant(self, request: httpx.Request) -> str | None:
        if (request.headers.get("authorization") == f"Bearer {self.id_token}"
                and request.headers.get(GRANT_HEADER) == self.grant.headers()[GRANT_HEADER]
                and request.headers.get(SESSION_HEADER)
                == f"{self.grant.project_id}/{self.grant.session_id}"):
            return "mine"
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        tenant = self.tenant(request)
        if tenant is None:
            return httpx.Response(401, json={"detail": "not granted"})
        q = request.url.params
        if "tenant_id" in q:
            tenant = q["tenant_id"]  # what a careless route would do; the tools never ask
        key = {"/v1/tasks": "tasks", "/v1/workflows": "workflows"}.get(request.url.path)
        if request.method != "GET" or key is None:
            return httpx.Response(404)
        rows = list(self.tenants.get(tenant, {}).get(key, []))
        states = {s for v in q.get_list("state") for s in v.split(",") if s}
        if states:
            rows = [r for r in rows if r["state"] in states]
        if q.get("submitted_by"):
            who = ME if q["submitted_by"] == "me" else q["submitted_by"]
            rows = [r for r in rows if r.get("submitted_by") == who]
        limit = min(int(q.get("limit") or 50), self.MAX_PAGE)
        start = int(q.get("page_token") or 0)
        page = rows[start:start + limit]
        nxt = str(start + limit) if start + limit < len(rows) else None
        return httpx.Response(200, json={key: page, "next_page_token": nxt, "view": "summary"})


def _read(api: FakeApi, name: str, args: dict):
    async def main():
        client = tools_read.SwarmApi.for_session(api.http, base_url=API, id_tokens=api.id_tokens,
                                                 grant=api.grant)
        try:
            return await tools_read.run_read_tool(client, name, args)
        finally:
            await api.http.aclose()

    return asyncio.run(main())


def _agents(state=None, submitted_by=None, workflow_id=None) -> dict:
    return {"state": state, "workflow_id": workflow_id, "submitted_by": submitted_by}


def test_how_many_agents_are_running_is_one_list_read_with_its_total():
    api = FakeApi(_grant(), running=7)
    answer = _read(api, "list_agents", _agents(state="RUNNING"))
    assert answer["total"] == 7
    assert answer["total_exact"] is True
    assert len(answer["tasks"]) == 7
    assert [r.url.path for r in api.requests] == ["/v1/tasks"]


@pytest.mark.parametrize("running", [26, 199, 200, 201, 430])
def test_the_total_is_every_running_agent_served_even_past_25_and_past_one_page(running):
    api = FakeApi(_grant(), running=running)
    answer = _read(api, "list_agents", _agents(state="RUNNING"))
    assert answer["total"] == running
    assert answer["total_exact"] is True
    # The rows the model reads stay short; the total is what grew.
    assert len(answer["tasks"]) == tools_read.LIST_LIMIT
    pages = -(-running // FakeApi.MAX_PAGE)
    assert len(api.requests) == pages
    assert {r.url.path for r in api.requests} == {"/v1/tasks"}


def test_several_live_states_are_one_read():
    api = FakeApi(_grant(), running=40)
    answer = _read(api, "list_agents", _agents(state=",".join(LIVE)))
    assert answer["total"] == 40
    assert len(api.requests) == 1
    assert api.requests[0].url.params["state"] == ",".join(LIVE)


def test_a_total_past_the_walk_says_it_is_a_lower_bound():
    running = FakeApi.MAX_PAGE * (tools_read.COUNT_PAGES + 1)
    api = FakeApi(_grant(), running=running)
    answer = _read(api, "list_agents", _agents(state="RUNNING"))
    assert answer["total"] == FakeApi.MAX_PAGE * tools_read.COUNT_PAGES
    assert answer["total_exact"] is False
    assert len(api.requests) == tools_read.COUNT_PAGES


def test_the_persons_own_running_agents_are_counted_with_submitted_by_me():
    api = FakeApi(_grant(), running=60, mine=33)
    answer = _read(api, "list_agents", _agents(state="RUNNING", submitted_by="me"))
    assert answer["total"] == 33
    assert all(r.url.params["submitted_by"] == "me" for r in api.requests)


def test_running_workflows_are_counted_the_same_way():
    api = FakeApi(_grant(), running=230)
    answer = _read(api, "list_workflows", {"state": "RUNNING", "repository": None})
    assert answer["total"] == 230
    assert len(answer["workflows"]) == tools_read.LIST_LIMIT


@pytest.mark.parametrize("who", [OTHER, "admin@example.com", " me@example.com"])
def test_submitted_by_names_only_the_person_never_another(who):
    api = FakeApi(_grant(), running=5)
    with pytest.raises(tools_read.BadInput):
        _read(api, "list_agents", _agents(state="RUNNING", submitted_by=who))
    assert api.requests == []


def test_no_read_or_page_of_it_crosses_the_grants_tenant():
    api = FakeApi(_grant(), running=450, mine=450)
    answer = _read(api, "list_agents", _agents(state="RUNNING", submitted_by="me"))
    assert answer["total"] == 450
    assert not any(r["task_id"].startswith("x-") for r in answer["tasks"])
    assert len(api.requests) > 1
    for request in api.requests:
        assert request.url.path == "/v1/tasks"
        assert "tenant_id" not in request.url.params
        assert api.tenant(request) == "mine"


def test_the_list_tools_schemas_offer_the_filters_and_not_a_tenant():
    schemas = {s["name"]: s for s in tools_read.tool_schemas()}
    agents = schemas["list_agents"]["input_schema"]["properties"]
    assert set(agents) == {"state", "workflow_id", "submitted_by"}
    assert "total" in schemas["list_agents"]["description"]
    assert set(schemas["list_workflows"]["input_schema"]["properties"]) == {"state", "repository"}
    for schema in schemas.values():
        assert "tenant_id" not in schema["input_schema"]["properties"]


def test_search_is_never_the_way_to_count():
    search = next(s for s in tools_read.tool_schemas() if s["name"] == "search")
    assert "never to count" in search["description"]
    assert "never count with `search`" in prompts.SYSTEM
    assert "`total`" in prompts.SYSTEM


def _front_read(api: FakeApi, name: str, args: dict, *, grant: SessionGrant | None = None):
    """`Planner.read`, the fast front's read (claudefront.py): no model, no lease."""
    from swarm_voice import planner as planner_mod

    async def main():
        planner = planner_mod.Planner(api.http, api_base=API, id_tokens=api.id_tokens, accounts=None)
        try:
            return await planner.read(grant=grant or api.grant, name=name, args=args)
        finally:
            await api.http.aclose()

    return asyncio.run(main())


def test_the_fast_front_counts_running_agents_in_one_read_on_the_grant():
    api = FakeApi(_grant(), running=230, mine=230)
    text, is_error = _front_read(api, "list_agents", _agents(state="RUNNING", submitted_by="me"))
    assert not is_error
    assert text.startswith('<data source="list_agents">')
    assert '"total":230' in text and '"total_exact":true' in text
    assert {r.url.path for r in api.requests} == {"/v1/tasks"}
    for request in api.requests:
        assert api.tenant(request) == "mine"
        assert "tenant_id" not in request.url.params
    # Nothing secret reaches the child: no grant value, no ID token.
    assert api.grant.headers()[GRANT_HEADER] not in text and api.id_token not in text


def test_the_fast_front_reads_nothing_but_the_read_tools():
    for name in ("speak", "draw_plan", "answer_question", "show_file", "delegate"):
        api = FakeApi(_grant(), running=3)
        text, is_error = _front_read(api, name, {})
        assert is_error and "not a read tool" in text
        assert api.requests == []


def test_the_fast_front_refuses_another_persons_count_before_any_request():
    api = FakeApi(_grant(), running=5)
    text, is_error = _front_read(api, "list_agents", _agents(state="RUNNING", submitted_by=OTHER))
    assert is_error and "submitted_by" in text
    assert api.requests == []
