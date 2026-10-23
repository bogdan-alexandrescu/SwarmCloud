"""Every route whose request body has a credential-shaped field is in
`main.UNCAPTURED_ROUTES` (lane BYPASS-UNCAPTURED, part of #2058).

`UNCAPTURED_ROUTES` is the list a body capture added to the app later must
skip (design workspace-secrets.md §4.5). It was kept by hand, so a lane that
added a route taking a credential could leave it out: CAPTCHA-BYPASS (PR 2069)
added `PUT /v1/qa-targets/{target_id}/challenge-bypass`, whose body is the
bypass value, with `main.py` outside its territory. This test reads the app's
own routes instead, so the next such route is red the day it is added.

A field is credential-shaped when one of its underscore-separated words is in
`CREDENTIAL_WORDS`, at any depth of the body's models (`list[...]`, `X | None`
and nested models included). The fixture app below shows the scan can come out
the other way: a route it should flag is flagged, and one it should not is not.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from swarm_api.main import UNCAPTURED_ROUTES
from swarm_api.routes.qatargets import BYPASS_ROUTE

#: The words that make a body field credential material. `value` is a
#: secret's or a bypass's value; `key` an API key; `code` an OAuth
#: authorization code, which redeems for a token.
CREDENTIAL_WORDS = frozenset({
    "value", "password", "token", "secret", "secrets", "bypass", "key",
    "credential", "credentials", "code",
})

#: Field names that share a word with a credential and carry none, each with
#: why. An entry here is a decision a reviewer reads, not a silencer: a field
#: named in it on a route that DOES take a credential still leaves that route's
#: other fields to flag it.
NOT_CREDENTIALS = frozenset({
    # `POST /v1/admin/history:purge`: a pagination cursor this API minted.
    "page_token",
    # `POST /v1/qa-targets`, `PATCH .../{target_id}`: the bypass's kind and
    # header or cookie name only; its value is the challenge-bypass PUT's
    # (qatargets.py `TargetBody`).
    "challenge_bypass",
})


def api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every `APIRoute` under `routes`. FastAPI 0.141 wraps each included
    router in an `_IncludedRouter` whose `original_router` holds the routes,
    so filtering `app.routes` alone finds none (test_continuation_scope_is_narrow.py)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (router := getattr(route, "original_router", None)) is not None:
            yield from api_routes(router.routes)
        elif (sub := getattr(route, "routes", None)) is not None:
            yield from api_routes(sub)


def field_names(annotation: Any, seen: set[type]) -> Iterator[str]:
    """Every field name in a body annotation, through models, containers and unions."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return
        seen.add(annotation)
        for name, info in annotation.model_fields.items():
            yield name
            yield from field_names(info.annotation, seen)
    elif typing.get_origin(annotation) is not None or isinstance(annotation, types.UnionType):
        for arg in typing.get_args(annotation):
            yield from field_names(arg, seen)


def credential_fields(route: APIRoute) -> list[str]:
    seen: set[type] = set()
    names = (n for p in route.dependant.body_params for n in field_names(p.field_info.annotation, seen))
    return sorted({n for n in names
                   if n not in NOT_CREDENTIALS and CREDENTIAL_WORDS & set(n.lower().split("_"))})


def credential_routes(app: FastAPI) -> dict[str, list[str]]:
    """Path -> its credential-shaped body fields, for every route that has one."""
    found: dict[str, list[str]] = {}
    for route in api_routes(app.routes):
        if fields := credential_fields(route):
            found.setdefault(route.path, []).extend(fields)
    return found


def missing(app: FastAPI, uncaptured: frozenset[str]) -> dict[str, list[str]]:
    return {path: fields for path, fields in credential_routes(app).items() if path not in uncaptured}


# --------------------------------------------------------------------------
# The scan, on a fixture app built here
# --------------------------------------------------------------------------


class Inner(BaseModel):
    api_key: str


class Plain(BaseModel):
    name: str
    limit: int
    page_token: str | None = None


class Nested(BaseModel):
    name: str
    items: list[Inner] | None = None


class Flat(BaseModel):
    password: str


def fixture_app() -> FastAPI:
    router = APIRouter(prefix="/v1")

    @router.put("/plain")
    def plain(body: Plain) -> None:
        return None

    @router.put("/nested")
    def nested(body: Nested) -> None:
        return None

    @router.put("/flat/{thing_id}")
    def flat(thing_id: str, body: Flat) -> None:
        return None

    app = FastAPI()
    app.include_router(router)
    return app


def test_the_scan_flags_a_credential_field_at_any_depth_and_nothing_else():
    found = credential_routes(fixture_app())
    # MUTATION: stop recursing through list[...] / X | None in `field_names` and this fails.
    # MUTATION: stop unwrapping `original_router` in `api_routes` and this fails (found is {}).
    # MUTATION: drop "password" or "key" from CREDENTIAL_WORDS and this fails.
    # MUTATION: match every field, not only credential-shaped ones, and "/v1/plain" appears.
    # MUTATION: ignore NOT_CREDENTIALS in `credential_fields` and "/v1/plain" appears.
    assert found == {"/v1/nested": ["api_key"], "/v1/flat/{thing_id}": ["password"]}


def test_a_credential_route_missing_from_the_set_is_reported_and_a_named_one_is_not():
    app = fixture_app()
    # MUTATION: make `missing` ignore the set it is given and the second assertion fails.
    assert missing(app, frozenset()) == {"/v1/nested": ["api_key"], "/v1/flat/{thing_id}": ["password"]}
    # MUTATION: make `missing` return every credential route and this fails.
    assert missing(app, frozenset({"/v1/nested", "/v1/flat/{thing_id}"})) == {}


# --------------------------------------------------------------------------
# The real app
# --------------------------------------------------------------------------


def test_every_credential_body_route_of_the_app_is_uncaptured(client):
    found = credential_routes(client.app)
    # The scan ran over the real routes: it saw the bypass PUT's `value` and
    # the QA credential's `password`. An empty scan would pass the last assertion
    # for every route in the world.
    # MUTATION: return {} from `credential_routes` and this fails.
    assert "value" in found.get(BYPASS_ROUTE, [])
    # MUTATION: stop unwrapping `original_router` in `api_routes` and this fails.
    assert "password" in found.get("/v1/qa-targets/{target_id}/credential", [])
    # MUTATION: drop any route this test names in the PR body from main.UNCAPTURED_ROUTES and this fails.
    assert missing(client.app, UNCAPTURED_ROUTES) == {}, (
        "these routes take credential material in their body and are not in "
        "main.UNCAPTURED_ROUTES, so a body capture added later would log it"
    )
