"""The planner's read tools and its one way into swarm-api (docs/design/voice-it.md
§3.3, §3.6, §3.9).

EVERY CALL CARRIES THE SESSION GRANT. swarm-voice has no Firestore role and no
storage role: it reads SwarmCloud and writes cards only through swarm-api, as
the session's person, presenting its own Google ID token (audience swarm-api)
and the session grant's two headers (grant.py). swarm-api judges each call on
its own path (swarm_api/voice/grant.py): swarm-voice's identity, the route on
the grant's allowlist, the session live; then the person's own memberships,
roles and tenant scope, and one `voice_audit` row stamped `via: voice-it`. So
per-tenant isolation (invariant 9) is swarm-api's, the same as a click's: a
read here can never see another workspace. A session without a grant gets no
client at all (`SwarmApi.for_session` raises `NoGrant`).

THE READ TOOLS map one to one onto GET routes on the grant's allowlist
(`READ_TOOLS`): agents (tasks), workflows, runs, failure findings, approvals and
repositories, plus `search`, which matches a query against the titles and ids
of the agents, workflows and runs lists. Each is a strict tool schema; a path
parameter must look like an id (no "/", no ".."), so the model cannot steer a
read to another route. They are read-only: nothing here sends anything but GET,
except `write_card`, the catalogue's write path.

A LIST OF AGENTS OR WORKFLOWS CARRIES ITS TOTAL (`COUNTED`). Asked "how many
agents do I have running", the planner on the live deployment (12 November
2026) read 25 running agents with no total, then the workflows twice and
`search` twice, and ended the turn with no answer. So `list_agents` and
`list_workflows` take the filters their routes accept (state, one or several
comma-separated; for agents also `submitted_by=me`, the person's own) and
answer `total` beside at most `LIST_LIMIT` rows: the tool reads swarm-api's
largest page and follows the route's own `next_page_token`, on the same route
with the same filters, for at most `COUNT_PAGES` pages, and says
`total_exact: false` when it stopped short. One tool round answers a count.
Neither route serves a total, so the pages are the count.

NOTHING SECRET LEAVES. An error names the route template and the HTTP status,
never a body (a body may quote the request) and never a header. The grant and
the ID token live only in the request's headers.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .grant import GRANT_HEADER, SessionGrant

#: Mints a Google ID token for swarm-api's audience.
IdTokens = Callable[[], Awaitable[str]]

#: An id the model may put in a path: no "/", no "..", nothing to escape.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

#: How many rows a list tool asks for, and how many `search` scans per list.
LIST_LIMIT = 25
#: A counted list's page: swarm-api's `max_page_size`, so a count of up to 200
#: is one request. A larger ask is served as 200 anyway.
COUNT_PAGE = 200
#: How many pages a counted list follows before it says `total_exact: false`.
#: A filtered count (the running agents, the person's own) is far below
#: 1,000; an unfiltered list reads one page, since its total is the tenant's
#: whole history and a turn is no place to walk it.
COUNT_PAGES = 5
#: The longest `next_page_token` followed; a longer one ends the walk.
_TOKEN_MAX = 1024
SEARCH_SCAN = 100
SEARCH_HITS = 20

CATALOGUE_ROUTE = "/v1/voice/cards/schema"
CARD_WRITE_ROUTE = "/v1/voice/sessions/{session_id}/cards"


class NoGrant(PermissionError):
    """The planner was asked to act for a session that holds no grant."""


class ApiRefused(RuntimeError):
    """swarm-api refused or failed a call. Names the route and status only."""

    def __init__(self, route: str, status: int | None, what: str = "") -> None:
        self.route = route
        self.status = status
        detail = f"HTTP {status}" if status is not None else what or "no answer"
        super().__init__(f"swarm-api {route}: {detail}")


def google_id_tokens(audience: str) -> IdTokens:
    """ID tokens for `audience` from the pod's Workload Identity (the metadata server)."""
    if not audience:
        raise RuntimeError("swarm-api's audience is required to mint an ID token for it")

    def fetch() -> str:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        return id_token.fetch_id_token(google_requests.Request(), audience)

    async def mint() -> str:
        return await asyncio.to_thread(fetch)

    return mint


class SwarmApi:
    """swarm-api, as one session's person, through the session grant."""

    def __init__(self, http: httpx.AsyncClient, *, base_url: str, id_tokens: IdTokens,
                 grant: SessionGrant) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._id_tokens = id_tokens
        self._grant = grant

    @classmethod
    def for_session(cls, http: httpx.AsyncClient, *, base_url: str, id_tokens: IdTokens,
                    grant: SessionGrant | None) -> "SwarmApi":
        if grant is None:
            raise NoGrant("this voice session holds no session grant, so the planner may not "
                          "read or write anything in swarm-api for it")
        return cls(http, base_url=base_url, id_tokens=id_tokens, grant=grant)

    @property
    def session_id(self) -> str:
        return self._grant.session_id

    def known_secrets(self) -> tuple[str, ...]:
        """The grant's value, for the planner to scrub from all the model sees."""
        return (self._grant.headers()[GRANT_HEADER],)

    async def _headers(self) -> dict[str, str]:
        try:
            token = await self._id_tokens()
        except Exception as exc:  # noqa: BLE001 - google-auth's message may carry token content
            raise ApiRefused("identity", None, f"no ID token ({type(exc).__name__})") from None
        return {"Authorization": f"Bearer {token}", **self._grant.headers()}

    async def _call(self, method: str, route: str, path: str, *, params: dict | None = None,
                    json_body: Any = None) -> Any:
        headers = await self._headers()
        try:
            response = await self._http.request(method, self._base + path, params=params,
                                                json=json_body, headers=headers, timeout=30)
        except httpx.HTTPError as exc:
            raise ApiRefused(route, None, type(exc).__name__) from None
        if response.status_code >= 300:
            raise ApiRefused(route, response.status_code)
        try:
            return response.json()
        except ValueError:
            raise ApiRefused(route, response.status_code, "not JSON") from None

    async def get(self, route: str, path: str, params: dict | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        return await self._call("GET", route, path, params=clean or None)

    async def catalogue(self) -> dict[str, Any]:
        """The card catalogue and its strict `draw_<kind>` tools (cards.catalogue)."""
        body = await self.get(CATALOGUE_ROUTE, CATALOGUE_ROUTE)
        if not isinstance(body, dict) or not isinstance(body.get("tools"), list):
            raise ApiRefused(CATALOGUE_ROUTE, 200, "no tools in the catalogue")
        return body

    async def write_card(self, card: dict[str, Any]) -> dict[str, Any]:
        """Write one card through swarm-api's card write path, which validates it
        against the catalogue (cards.write_card) and appends its stream event."""
        path = CARD_WRITE_ROUTE.format(session_id=self._grant.session_id)
        body = await self._call("POST", CARD_WRITE_ROUTE, path, json_body={"card": card})
        return body if isinstance(body, dict) else {}


# ---- the read tools ---------------------------------------------------------


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    #: The swarm-api route template it reads; every one is on the grant's allowlist.
    route: str
    #: Path parameters, in route order, each an id.
    path_params: tuple[str, ...] = ()
    #: Optional query parameters the model may set (all nullable strings).
    query_params: tuple[str, ...] = ()
    #: Fixed query parameters (the summary view, the list limit).
    fixed: tuple[tuple[str, str], ...] = ()
    #: For a counted list, the key of the rows in swarm-api's answer: the tool
    #: answers `total` beside at most `LIST_LIMIT` of them (`_counted`).
    counted: str | None = None

    def schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {p: {"type": "string"} for p in self.path_params}
        props.update({q: {"type": ["string", "null"]} for q in self.query_params})
        return {"name": self.name, "description": self.description, "strict": True,
                "input_schema": {"type": "object", "additionalProperties": False,
                                 "properties": props, "required": list(props)}}


_LIST = (("limit", str(LIST_LIMIT)),)
_SUMMARY = (("view", "summary"), *_LIST)

READ_TOOLS: tuple[ReadTool, ...] = (
    ReadTool("list_agents", "List and count agents (tasks) in the workspace, newest first: the "
             "answer's `total` is how many match, beside at most 25 rows. state filters by one "
             "state or several comma-separated, e.g. RUNNING, FAILED, PARKED; everything in "
             "flight is LEASED,DISPATCHED,STARTING,RUNNING. workflow_id filters by workflow; "
             "submitted_by: me keeps the person's own (null for everyone's).",
             "/v1/tasks", query_params=("state", "workflow_id", "submitted_by"), fixed=_SUMMARY,
             counted="tasks"),
    ReadTool("get_agent", "Read one agent (task) by id.", "/v1/tasks/{task_id}", ("task_id",)),
    ReadTool("agent_events", "Read one agent's event history: states, errors, findings.",
             "/v1/tasks/{task_id}/events", ("task_id",)),
    ReadTool("agent_questions", "Read the questions one agent is waiting on.",
             "/v1/tasks/{task_id}/questions", ("task_id",)),
    ReadTool("open_questions", "List the person's own finished agents whose questions to them are "
             "still unanswered (the last 7 days, newest first).",
             "/v1/tasks/open-questions"),
    ReadTool("list_workflows", "List and count workflows, newest first: the answer's `total` is how "
             "many match, beside at most 25 rows. state filters by one state or several "
             "comma-separated, e.g. RUNNING; repository by owner/name.", "/v1/workflows",
             query_params=("state", "repository"), fixed=_SUMMARY, counted="workflows"),
    ReadTool("get_workflow", "Read one workflow and its steps.", "/v1/workflows/{workflow_id}",
             ("workflow_id",)),
    ReadTool("list_runs", "List issue runs, newest first.", "/v1/runs", fixed=_SUMMARY),
    ReadTool("get_run", "Read one issue run: its issue, plan, steps and holds.", "/v1/runs/{run_id}",
             ("run_id",)),
    ReadTool("list_findings", "List failure findings: why agents and workflows failed.",
             "/v1/failure-findings", fixed=_LIST),
    ReadTool("get_finding", "Read one failure finding.", "/v1/failure-findings/{finding_id}",
             ("finding_id",)),
    ReadTool("list_approvals", "List the inbox: items waiting on a person's approval.",
             "/v1/approvals"),
    ReadTool("list_repositories", "List the workspace's repositories.", "/v1/repositories"),
    ReadTool("list_schedules", "List schedules.", "/v1/schedules"),
)

SEARCH = {
    "name": "search",
    "description": ("Find agents, workflows and runs whose title, name or id contains the query "
                    "(case-insensitive). Returns kind, id, title and state of each hit. It "
                    "matches names and is never to count: list_agents and list_workflows "
                    "answer a total."),
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False,
                     "properties": {"query": {"type": "string"}}, "required": ["query"]},
}

_BY_NAME = {t.name: t for t in READ_TOOLS}

#: The lists `search` scans: (kind, route, the keys of a row's title and id).
_SEARCHED = (
    ("agent", "/v1/tasks", ("title", "name", "prompt_summary"), ("task_id", "id")),
    ("workflow", "/v1/workflows", ("title", "name"), ("workflow_id", "id")),
    ("run", "/v1/runs", ("title", "issue_title"), ("run_id", "id")),
)


def tool_schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in READ_TOOLS] + [SEARCH]


def is_read_tool(name: str) -> bool:
    return name in _BY_NAME or name == SEARCH["name"]


class BadInput(ValueError):
    """A read tool's input is not what its schema allows."""


def _rows(body: Any) -> list[dict]:
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for key in ("items", "tasks", "workflows", "runs", "results", "data"):
            if isinstance(body.get(key), list):
                return [r for r in body[key] if isinstance(r, dict)]
    return []


def _first(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def _search(api: SwarmApi, query: str) -> dict[str, Any]:
    needle = query.strip().lower()
    if not needle:
        raise BadInput("search needs a query")
    hits: list[dict[str, str]] = []
    for kind, route, title_keys, id_keys in _SEARCHED:
        body = await api.get(route, route, {"view": "summary", "limit": str(SEARCH_SCAN)})
        for row in _rows(body):
            title, ident = _first(row, title_keys), _first(row, id_keys)
            if needle in title.lower() or needle in ident.lower():
                hits.append({"kind": kind, "id": ident, "title": title,
                             "state": str(row.get("state") or "")})
    return {"query": query, "hits": hits[:SEARCH_HITS], "more": len(hits) > SEARCH_HITS}


async def _counted(api: SwarmApi, tool: ReadTool, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """A counted list: `total` beside at most `LIST_LIMIT` rows (module docstring).

    Every page is the same route, path and filters; only `page_token` changes,
    and only to the token swarm-api itself served, so the walk reads nothing
    the first page could not."""
    filtered = any(params.get(q) is not None for q in tool.query_params)
    pages = COUNT_PAGES if filtered else 1
    query = {**params, "limit": str(COUNT_PAGE)}
    rows: list[dict] = []
    total = 0
    token: str | None = None
    for _ in range(pages):
        body = await api.get(tool.route, path, {**query, "page_token": token})
        page = _rows(body)
        total += len(page)
        rows.extend(page[:max(0, LIST_LIMIT - len(rows))])
        nxt = body.get("next_page_token") if isinstance(body, dict) else None
        token = nxt if isinstance(nxt, str) and 0 < len(nxt) <= _TOKEN_MAX else None
        if token is None:
            break
    return {tool.counted: rows, "total": total, "total_exact": token is None,
            "shown": len(rows), "view": "summary"}


async def run_read_tool(api: SwarmApi, name: str, args: Any) -> Any:
    """Run read tool `name` with the model's `args`; the swarm-api answer."""
    if not isinstance(args, dict):
        raise BadInput("a tool's input is an object")
    if name == SEARCH["name"]:
        query = args.get("query")
        if not isinstance(query, str):
            raise BadInput("search needs a query")
        return await _search(api, query)
    tool = _BY_NAME.get(name)
    if tool is None:
        raise BadInput(f"{name} is not a read tool")
    path = tool.route
    for param in tool.path_params:
        value = args.get(param)
        if not isinstance(value, str) or not _ID_RE.match(value) or ".." in value:
            raise BadInput(f"{param} is an id")
        path = path.replace("{" + param + "}", value)
    params: dict[str, Any] = dict(tool.fixed)
    for q in tool.query_params:
        value = args.get(q)
        if value is not None:
            if not isinstance(value, str) or len(value) > 200:
                raise BadInput(f"{q} is a short string")
            params[q] = value
    if params.get("submitted_by") not in (None, "me"):
        # The person's own work only: another member's is an admin's view,
        # and a voice session asks for the person, never about someone else.
        raise BadInput("submitted_by is me or null")
    if tool.counted:
        return await _counted(api, tool, path, params)
    return await api.get(tool.route, path, params)
