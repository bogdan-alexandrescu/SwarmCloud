"""The step's graph context, resolved by swarm-api (lane GRAPH-STAGING, part of #1694).

WHY. `graphstage` stages the promoted graph of the task's repository before a
claude-code or codex agent starts. To find it, a worker on the direct
Firestore path listed the registration's `index_versions`
(`versions.limit(n).stream()`): a query, and the tenant worker's role has
`entities.get` but no `entities.list` (docs/design/tenant-settings-integrity.md
§1.3). Measured 2026-10-19/20: 73 of 74 implement steps logged "no graph
snapshot is staged", 33 of them `staging failed (PermissionDenied)`. So the
agents explored more, and the pre-publish gate could not pick the tests a
change reaches.

Design B (#1464, owner decisions 2026-10-17) removes the worker's direct
database access rather than widening it, so the fix is not a permission.
swarm-api reads instead:

    POST /v1/attempts/{attempt_id}/context/graph
         {tenant_id, task_id, attempt_id, lease_id, generation}
      -> {repo_id, registration, versions, tenant_settings, workspace_settings}

(`swarm_api.attemptcontext.AttemptContext.graph_context`). Every id in the
answer is derived there from the attempt's tuple and the task's SIGNED
`repository_url`; the body names the attempt and nothing else (invariant 10),
and another tenant's registration is never served (invariant 9). The call is
signed with the child path's attempt key, which every keyed attempt
registers, exactly as the in-flight route and the graph ticket are, so it
serves a tenant that is not switched to the state routes. A switched tenant
(`indexrun.state_routes`) keeps `GET state/repository` and `state/context`,
which answer the same documents.

THE SHAPE. `GraphContextRoute.get` answers the two names those state routes
answer, `repository` and `context`, from ONE call, so `graphstage` reads it
exactly as it reads a `statecall.StateCall` (`indexrun.route_repository`,
`indexrun.route_context`).

WHAT AN ANSWER BECOMES. 409 is the fence: `graph_ticket.GraphTicketFenced`,
which `graphstage.prepare` raises for the lifecycle's fenced exit
(invariant 5). Anything else that is not an answer -- no attempt key, a 404
from a swarm-api that predates the route, a 5xx, no answer -- is
`GraphContextUnavailable`, and staging falls back to reading by document id,
which the role allows. Nothing here logs or raises with a header or a body.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .graph_ticket import GraphTicketFenced

#: The route, under this attempt's id.
ROUTE = "/v1/attempts/{attempt_id}/context/graph"
#: The names `get` answers: `indexrun.route_repository`'s and `route_context`'s.
REPOSITORY, CONTEXT = "repository", "context"
_REPOSITORY_FIELDS = ("repo_id", "registration", "versions")
_CONTEXT_FIELDS = ("tenant_settings", "workspace_settings")


class GraphContextUnavailable(RuntimeError):
    """The route gave no answer this attempt can stage from; `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(f"the graph context route was not answered ({code})")
        self.code = code


class GraphContextRoute:
    """One attempt's graph context, asked for at most once."""

    def __init__(self, cfg: Any, *, children: Any,
                 wall: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.children = children
        self._wall = wall
        self._answer: dict[str, Any] | None = None

    def _credentials(self) -> tuple[Any, Any] | None:
        key = getattr(self.children, "_key", None)
        api = getattr(self.children, "_api", None)
        if key is None or api is None or not getattr(self.children, "keyed", False):
            return None
        return key, api

    @property
    def available(self) -> bool:
        """Whether this attempt holds a key to sign the call with."""
        return self._credentials() is not None

    def route(self) -> str:
        return ROUTE.format(attempt_id=quote(self.cfg.attempt_id, safe=""))

    def body(self) -> bytes:
        """The exact bytes sent and signed: the attempt's fencing fields only."""
        return json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
        }, sort_keys=True).encode("utf-8")

    def fetch(self) -> dict[str, Any]:
        """The route's answer, asked for once. Raises GraphTicketFenced on a
        409 and GraphContextUnavailable for every other non-answer."""
        if self._answer is not None:
            return self._answer
        found = self._credentials()
        if found is None:
            raise GraphContextUnavailable("no_attempt_key")
        key, api = found
        route, body = self.route(), self.body()
        try:
            headers = key.headers("POST", route, body, now=self._wall())
            _, payload = api.call("POST", route, body=body, headers=headers)
        except Exception as exc:
            status = getattr(exc, "status", None)
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            if status == 409:
                raise GraphTicketFenced(f"graph context: 409 {code}") from None
            raise GraphContextUnavailable(code) from None
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            raise GraphContextUnavailable("not_an_answer")
        self._answer = dict(payload)
        return self._answer

    def get(self, name: str, *, deadline_seconds: float | None = None) -> dict[str, Any]:
        """`repository` or `context`, as the state routes answer them.

        `deadline_seconds` is the state client's argument, accepted so this is
        read as one; the call is made once, by `fetch`."""
        answer = self.fetch()
        if name == REPOSITORY:
            return {field: answer.get(field) for field in _REPOSITORY_FIELDS}
        if name == CONTEXT:
            return {field: answer.get(field) for field in _CONTEXT_FIELDS}
        raise ValueError(f"the graph context route answers no {name!r}")
