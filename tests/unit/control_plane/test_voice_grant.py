"""swarm-api accepts a Voice it session grant only from swarm-voice's
identity, only on the allowlisted routes, and stamps `via: voice-it` on the
audit row (docs/design/voice-it.md §3.6, §10 phase 2; swarm_api/voice/grant.py).

A session is started through the real route with a fake swarm-voice that
records the grant swarm-api hands it; every grant request then presents that
grant beside swarm-voice's own token. The route table is walked from the app
itself: every authenticated route off the allowlist is refused with a valid
grant, and the allowlist holds only reads that take no body. No network, no
cloud credential; tokens are opaque strings built at runtime.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from swarm_api import deps
from swarm_api import main as api_main
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context, current_auth
from swarm_api.errors import ApiError
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.settings import ApiSettings
from swarm_api.voice import grant as vg
from swarm_api.voice import sessions as vs
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, OfflineSecretMetadata, api_settings, auth_header, seed_task
from .test_voice_sessions import OFFER, FakeVoice, gpt_live_settings

VOICE_TOKEN = "token-swarm-voice-" + secrets.token_hex(4)
OTHER_SA_TOKEN = "token-other-sa-" + secrets.token_hex(4)
AUDIENCE = "https://swarm-api.internal.example"


def _voice_email(settings: ApiSettings) -> str:
    return vg.voice_account(settings)


def _build(db, tokens, group_map, objects, voice, *, aud: str | None = AUDIENCE,
           **overrides) -> TestClient:
    overrides.setdefault("voice_url", "http://voice.internal:8080")
    overrides.setdefault("voice_people", ("alice@example.com", "root@example.com"))
    settings = api_settings(core_overrides={"api_audience": AUDIENCE}, **overrides)
    claims = {"email_verified": True, "sub": "sub-voice"}
    if aud is not None:
        claims["aud"] = aud
    tokens = {
        **tokens,
        VOICE_TOKEN: {"email": _voice_email(settings), **claims},
        OTHER_SA_TOKEN: {"email": "swarm-api@" + settings.project_id + ".iam.gserviceaccount.com",
                         **claims},
    }
    ctx = build_context(
        settings=settings, db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
        forge_tokens=NoForgeTokens(),
    )
    app = create_app(ctx)
    app.state.secret_metadata = OfflineSecretMetadata()
    app.state.voice_client = voice
    gpt_live_settings(db)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def voice() -> FakeVoice:
    return FakeVoice()


@pytest.fixture
def client(db, tokens, group_map, objects, voice) -> TestClient:
    return _build(db, tokens, group_map, objects, voice)


def _start(client: TestClient, voice: FakeVoice, user: str = "alice") -> vs.StartCall:
    response = client.post("/v1/voice/sessions", json={"offer_sdp": OFFER},
                           headers=auth_header(user))
    assert response.status_code == 200, response.text
    return voice.starts[-1]


def _grant_headers(call: vs.StartCall, *, token: str = VOICE_TOKEN,
                   grant: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}",
            vg.SESSION_HEADER: f"{call.project_id}/{call.session_id}",
            vg.GRANT_HEADER: call.grant if grant is None else grant}


def _audit(db) -> list[dict[str, Any]]:
    return [row for path, row in db.docs.items()
            if path.startswith(vs.AUDIT + "/") and row.get("action") == vg.AUDIT_GRANT_USED]


# --------------------------------------------------------------------------
# Who may present a grant, and when
# --------------------------------------------------------------------------


def test_a_grant_from_swarm_voice_reads_as_the_sessions_person_in_its_workspace(client, db, voice):
    seed_task(db, task_id="t-eng", tenant_id="eng")
    seed_task(db, task_id="t-research", tenant_id="research")
    call = _start(client, voice)

    response = client.get("/v1/tasks", headers=_grant_headers(call))

    assert response.status_code == 200, response.text
    ids = {task["id"] for task in response.json()["tasks"]}
    assert ids == {"t-eng"}


def test_a_grant_from_any_other_identity_is_401(client, db, voice):
    call = _start(client, voice)

    for token in ("token-alice", "token-root", OTHER_SA_TOKEN, "token-nobody"):
        response = client.get("/v1/tasks", headers=_grant_headers(call, token=token))
        assert response.status_code == 401, (token, response.text)
        assert response.json()["code"] == "unauthenticated"
    no_bearer = {k: v for k, v in _grant_headers(call).items() if k != "Authorization"}
    assert client.get("/v1/tasks", headers=no_bearer).status_code == 401
    assert _audit(db) == []


def test_swarm_voices_token_for_another_audience_is_401(db, tokens, group_map, objects, voice):
    client = _build(db, tokens, group_map, objects, voice, aud="https://elsewhere.example")
    call = _start(client, voice)

    response = client.get("/v1/tasks", headers=_grant_headers(call))

    assert response.status_code == 401


def test_an_iap_assertion_beside_a_grant_is_401(client, db, voice):
    call = _start(client, voice)
    headers = {**_grant_headers(call), "X-Goog-IAP-JWT-Assertion": secrets.token_hex(24)}

    assert client.get("/v1/tasks", headers=headers).status_code == 401


def test_an_ended_sessions_grant_is_401_and_its_hash_is_gone(client, db, voice):
    call = _start(client, voice)
    assert client.get("/v1/tasks", headers=_grant_headers(call)).status_code == 200
    ended = client.post(f"/v1/voice/sessions/{call.session_id}/end", headers=auth_header("alice"))
    assert ended.status_code == 200, ended.text

    response = client.get("/v1/tasks", headers=_grant_headers(call))

    assert response.status_code == 401, response.text
    session = db.docs[f"{vs.PROJECTS}/{call.project_id}/{vs.SESSIONS}/{call.session_id}"]
    assert session["state"] == vs.ENDED and not session.get("grant_hash")


def test_a_wrong_grant_or_session_is_401(client, db, voice):
    call = _start(client, voice)
    wrong = secrets.token_urlsafe(32)
    other_session = vs.StartCall(session_id="vs_" + secrets.token_hex(16),
                                 project_id=call.project_id, tenant_id=call.tenant_id,
                                 person=call.person, offer=OFFER,
                                 grant=call.grant)

    for headers in (_grant_headers(call, grant=wrong),
                    _grant_headers(call, grant=call.grant[:-1]),
                    _grant_headers(call, grant=""),
                    _grant_headers(other_session),
                    {**_grant_headers(call), vg.SESSION_HEADER: call.session_id}):
        response = client.get("/v1/tasks", headers=headers)
        assert response.status_code == 401, (headers.get(vg.SESSION_HEADER), response.text)


def test_one_sessions_grant_never_opens_another_session(client, db, voice):
    alice = _start(client, voice, "alice")
    root = _start(client, voice, "root")

    mixed = {**_grant_headers(root), vg.GRANT_HEADER: alice.grant}

    assert client.get("/v1/tasks", headers=mixed).status_code == 401


def test_a_route_off_the_allowlist_is_403_before_any_session_is_read(client, db, voice):
    call = _start(client, voice)

    for method, path in (("POST", "/v1/tasks"), ("GET", "/v1/admin/settings"),
                         ("POST", "/v1/voice/sessions"),
                         ("POST", f"/v1/voice/sessions/{call.session_id}/end")):
        response = client.request(method, path, headers=_grant_headers(call))
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["code"] == "forbidden"
    # The allowlist is checked before the session: an unknown grant on an
    # off-list route is still the 403, and reads nothing to find that out.
    response = client.post("/v1/tasks", headers=_grant_headers(call, grant=secrets.token_urlsafe(32)))
    assert response.status_code == 403
    assert len(voice.starts) == 1 and voice.ends == []


def test_a_grant_cannot_end_its_session_as_a_closed_tab(client, db, voice):
    # Only the person's own route names `tab_closed`; a grant acting for them is refused.
    call = _start(client, voice)

    response = client.post(f"/v1/voice/sessions/{call.session_id}/end",
                           json={"cause": "tab_closed"}, headers=_grant_headers(call))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert voice.ends == []
    sessions = [data for path, data in db.docs.items() if path.endswith(call.session_id)]
    assert sessions and all(data["state"] != vs.ENDED for data in sessions)


def test_a_grant_acts_only_in_its_sessions_workspace(client, db, voice):
    call = _start(client, voice)

    response = client.get("/v1/tasks", headers={**_grant_headers(call), "X-Swarm-Tenant": "research"})

    assert response.status_code == 403


def test_a_person_no_longer_allowed_voice_it_is_refused(db, tokens, group_map, objects, voice):
    client = _build(db, tokens, group_map, objects, voice)
    call = _start(client, voice)
    narrowed = _build(db, tokens, group_map, objects, voice, voice_people=("root@example.com",))

    response = narrowed.get("/v1/tasks", headers=_grant_headers(call))

    assert response.status_code == 403


def test_a_grant_carries_none_of_the_persons_platform_admin_power(client, db, voice):
    call = _start(client, voice, "root")
    ctx = client.app.state.ctx
    session = vg.GrantSession(tenant_id=call.tenant_id, person=call.person,
                              project_id=call.project_id, session_id=call.session_id)

    auth = ctx.authenticator.authenticate_voice_grant(session)

    assert auth.email == "root@example.com" and auth.tenant_id == call.tenant_id
    assert auth.is_admin is False and auth.is_owner is False and auth.is_pool_admin is False
    assert auth.via == vg.VIA and auth.voice_session == call.session_id
    assert client.get("/v1/admin/settings", headers=_grant_headers(call)).status_code == 403


# --------------------------------------------------------------------------
# The audit row, and the grant as a credential
# --------------------------------------------------------------------------


def test_a_request_under_a_grant_writes_an_audit_row_with_via_voice_it(client, db, voice):
    seed_task(db, task_id="t-eng", tenant_id="eng")
    call = _start(client, voice)

    assert client.get("/v1/tasks/t-eng", headers=_grant_headers(call)).status_code == 200

    (row,) = _audit(db)
    assert row["via"] == "voice-it" and row["session_id"] == call.session_id
    assert row["project_id"] == call.project_id
    assert row["tenant_id"] == "eng" and row["by"] == "alice@example.com"
    assert row["method"] == "GET" and row["route"] == "/v1/tasks/{task_id}"
    assert row["path_params"] == {"task_id": "t-eng"}


def test_no_audit_row_no_route(client, db, voice, monkeypatch):
    call = _start(client, voice)

    def refuse(_db, _row):
        raise vg.GrantAuditUnavailable("the Voice it audit row could not be written; nothing was done")

    monkeypatch.setattr(vg, "write_audit", refuse)
    response = client.get("/v1/tasks", headers=_grant_headers(call))

    assert response.status_code == 503


def test_the_grant_never_reaches_a_log_line_a_response_or_a_document(client, db, voice, caplog):
    caplog.set_level(logging.DEBUG)
    call = _start(client, voice)
    texts = []
    for headers, method, path in (
            (_grant_headers(call), "GET", "/v1/tasks"),
            (_grant_headers(call), "POST", "/v1/tasks"),
            (_grant_headers(call, token="token-alice"), "GET", "/v1/tasks"),
            ({**_grant_headers(call), vg.SESSION_HEADER: "vp_x/vs_y"}, "GET", "/v1/tasks"),
            (_grant_headers(call), "GET", "/v1/tasks?limit=nope")):
        texts.append(client.request(method, path, headers=headers).text)
    client.post(f"/v1/voice/sessions/{call.session_id}/end", headers=auth_header("alice"))
    texts.append(client.get("/v1/tasks", headers=_grant_headers(call)).text)

    logged = "\n".join(r.getMessage() + " " + str(r.args) + " " + (r.exc_text or "")
                       for r in caplog.records)
    assert "voice grant refused" in logged, "no refusal was logged, so the check proves nothing"
    assert call.grant not in logged
    assert all(call.grant not in text for text in texts)
    assert call.grant not in json.dumps(db.docs, default=str)


def test_an_error_page_under_a_grant_scrubs_the_grant(client, db, voice, monkeypatch):
    """A route that echoes what it was sent: the error handlers scrub the
    grant because `current_auth` added it to the scrubbed values."""
    assert deps._SCRUB_STATE == api_main.SCRUB_STATE
    call = _start(client, voice)
    app = client.app

    def echo(auth=deps.Depends(current_auth)):
        raise ApiError(f"echo {call.grant}")

    app.add_api_route("/v1/echo-grant", echo, methods=["GET"])
    monkeypatch.setattr(vg, "ALLOWED_ROUTES", vg.ALLOWED_ROUTES | {("GET", "/v1/echo-grant")})

    response = client.get("/v1/echo-grant", headers=_grant_headers(call))

    assert response.status_code == 400, response.text
    assert call.grant not in response.text and "[redacted]" in response.text


# --------------------------------------------------------------------------
# The route table walk
# --------------------------------------------------------------------------


def _iter_api_routes(routes: Any) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif (router := getattr(route, "original_router", None)) is not None:
            yield from _iter_api_routes(router.routes)
        elif (sub := getattr(route, "routes", None)) is not None:
            yield from _iter_api_routes(sub)


def _calls(dependant: Any) -> Iterator[Any]:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def _authenticated(app) -> list[tuple[str, str, APIRoute]]:
    found = {}
    for route in _iter_api_routes(app.routes):
        if any(call is current_auth for call in _calls(route.dependant)):
            for method in route.methods:
                found[(method, route.path)] = route
    return [(m, p, r) for (m, p), r in sorted(found.items())]


#: A body field that is a typed confirmation or a secret value.
_SENSITIVE_FIELD = re.compile(r"confirm|typed|secret|value|token|password|private|key", re.I)
#: A path that names what §3.6 keeps off the allowlist.
_SENSITIVE_PATH = re.compile(
    r"admin|secret|credential|iam|budget|purge|destroy|merge|approve|confirm|token|key|"
    r"shared|platform|setup|install|limit", re.I)


def _body_fields(route: APIRoute) -> set[str]:
    body = route.body_field
    if body is None:
        return set()
    model = getattr(body, "type_", None) or getattr(getattr(body, "field_info", None), "annotation", None)
    fields = getattr(model, "model_fields", None) or {}
    return set(fields) | {body.name}


def test_the_allowlist_holds_only_existing_reads_that_take_no_body(client):
    table = {(m, p): r for m, p, r in _authenticated(client.app)}
    assert len(table) > 100, "the walk found too few routes to prove anything"

    for method, path in sorted(vg.ALLOWED_ROUTES):
        assert (method, path) in table, f"{method} {path} is not a route"
        assert method == "GET", f"{method} {path} is not a read"
        assert table[(method, path)].body_field is None, f"{method} {path} takes a body"
        assert not _SENSITIVE_PATH.search(path), f"{method} {path} names what §3.6 forbids"


def test_no_typed_confirmation_or_secret_value_route_is_on_the_allowlist(client):
    sensitive = [(m, p) for m, p, r in _authenticated(client.app)
                 if any(_SENSITIVE_FIELD.search(f) for f in _body_fields(r))
                 or re.search(r"secret|credential", p, re.I)]
    assert sensitive, "the walk found no typed-confirmation or secret-value route"
    assert not set(sensitive) & (vg.ALLOWED_ROUTES | vg.WRITE_ROUTES)


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_route_off_the_allowlist_refuses_a_live_grant(client, db, voice):
    call = _start(client, voice)
    headers = _grant_headers(call)
    walked, reachable = 0, []
    for method, path, _route in _authenticated(client.app):
        if (method, path) in vg.ALLOWED_ROUTES or (method, path) in vg.WRITE_ROUTES:
            continue
        walked += 1
        response = client.request(method, _fill(path), headers=headers)
        if response.status_code != 403:
            reachable.append(f"{method} {path} -> {response.status_code}")
    assert walked > 100, f"only {walked} routes were walked"
    assert reachable == []
    # And the grant was live throughout: an allowlisted read still answers.
    assert client.get("/v1/tasks", headers=headers).status_code == 200
