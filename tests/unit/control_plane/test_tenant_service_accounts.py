"""A tenant may list service accounts that resolve to it (contract request 30).

WHAT THIS PINS. `.github/workflows/ci-fix.yml` (#273) runs as
`swarm-ci-fix@saga-agents-staging.iam.gserviceaccount.com` and has to land in
tenant `eng` to continue `eng`'s red pull requests. A service account is not a
Workspace principal, and adding it to `eng@saga.xyz` would hand it every grant
that group holds across the company. So `eng` LISTS it in terraform, and
swarm-api resolves it by an exact match on the email AND the unique id (`sub`)
its verified token carries -- before any group is consulted, and with rights
narrowed to continuation only (`member_scope="continuation"`).

The route-by-route narrowing is swept in test_continuation_scope_is_narrow.py;
this file is the identity half: who resolves, to what, on which path, and what
a misconfiguration is refused as.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_common.identity import TenantMember
from swarm_common.states import EventType

import swarm_api.auth as auth_module
from swarm_api.auth import (
    Authenticator,
    IapAssertionVerifier,
    StaticTokenVerifier,
)
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context, current_auth
from swarm_api.errors import Forbidden, Unauthenticated
from swarm_api.groups import GroupLookupError, StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.schemas import TaskCreate
from swarm_api.settings import ApiSettings
from swarm_api.waker import NullWaker

from .conftest import ENG_GROUP, PROJECT, api_settings, auth_header, seed_task, seed_tenant

FIXER = f"swarm-ci-fix@{PROJECT}.iam.gserviceaccount.com"
FIXER_UID = "104857600000000000001"
UNLISTED_SA = f"swarm-other@{PROJECT}.iam.gserviceaccount.com"

LISTING = TenantMember(email=FIXER, kind="group", principal=ENG_GROUP, uid=FIXER_UID)


def _claims(email: str = FIXER, sub: str = FIXER_UID, **extra: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {"email": email, "sub": sub, "email_verified": True}
    claims.update(extra)
    return claims


class NoLookups:
    """A directory that fails every question -- and records that it was asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    def groups_for(self, email: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
        self.asked.append((email, tuple(candidates)))
        raise GroupLookupError("Cloud Identity did not answer")


def _authenticator(
    tokens: dict[str, dict[str, Any]],
    *,
    listing: tuple[TenantMember, ...] = (LISTING,),
    groups: Any = None,
    iap: Any = None,
    **overrides: Any,
) -> Authenticator:
    settings = api_settings(tenant_service_accounts=listing, **overrides)
    return Authenticator(
        settings,
        StaticTokenVerifier(tokens),
        groups if groups is not None else StaticGroups({"alice@saga.xyz": (ENG_GROUP,)}),
        iap=iap,
    )


def _bearer(token: str) -> str:
    return f"Bearer {token}"


# ---------------------------------------------------------------------------
# Who resolves, and to what
# ---------------------------------------------------------------------------

def test_a_listed_account_resolves_to_its_tenant_with_continuation_scope():
    ctx = _authenticator({"t": _claims()}).authenticate(_bearer("t"))
    assert ctx.tenant_id == "eng"
    assert ctx.tenant_principal == ENG_GROUP
    assert ctx.is_admin is False
    assert ctx.is_pool_admin is False
    assert ctx.admin_unresolved is False
    assert ctx.tenant_member == FIXER
    assert ctx.member_scope == "continuation"
    assert ctx.email == FIXER


def test_an_ordinary_member_has_no_scope_and_no_listing():
    ctx = _authenticator({"t": _claims("alice@saga.xyz", "sub-alice")}).authenticate(
        _bearer("t")
    )
    assert ctx.tenant_id == "eng"
    assert ctx.member_scope == ""
    assert ctx.tenant_member == ""


def test_admitted_without_allowed_users_while_an_unlisted_account_is_still_refused():
    auth = _authenticator(
        {"listed": _claims(), "unlisted": _claims(UNLISTED_SA, "2")},
        allowed_users=(),
    )
    assert auth.authenticate(_bearer("listed")).tenant_id == "eng"
    with pytest.raises(Forbidden):
        auth.authenticate(_bearer("unlisted"))


def test_a_listed_account_makes_no_directory_lookup_and_is_not_an_admin():
    directory = NoLookups()
    ctx = _authenticator({"t": _claims()}, groups=directory).authenticate(_bearer("t"))
    assert directory.asked == [], "a listed account must not consult Cloud Identity"
    assert ctx.tenant_id == "eng"
    assert ctx.is_admin is False
    assert ctx.admin_unresolved is False


def test_a_recreated_account_under_the_same_email_is_not_listed():
    """Same address, new unique id: it falls through to the domain check, which
    a service-account address fails."""
    with pytest.raises(Forbidden):
        _authenticator({"t": _claims(sub="999")}).authenticate(_bearer("t"))


# ---------------------------------------------------------------------------
# email_verified
# ---------------------------------------------------------------------------

def test_an_unverified_email_is_refused():
    with pytest.raises(Unauthenticated):
        _authenticator({"t": _claims(email_verified=False)}).authenticate(_bearer("t"))


def test_an_absent_email_verified_is_refused_on_the_listed_bearer_path_only():
    listed = _claims()
    del listed["email_verified"]
    human = _claims("alice@saga.xyz", "sub-alice")
    del human["email_verified"]
    auth = _authenticator({"listed": listed, "human": human})
    with pytest.raises(Unauthenticated):
        auth.authenticate(_bearer("listed"))
    # The general rule on this path is "not False", so the human still gets in:
    # the two paths are not accidentally the same code.
    assert auth.authenticate(_bearer("human")).tenant_id == "eng"


def _real_iap(monkeypatch, claims: dict[str, Any]) -> IapAssertionVerifier:
    """The shipped IAP verifier, with only Google's signature check replaced."""
    from google.oauth2 import id_token as google_id_token

    monkeypatch.setattr(
        google_id_token, "verify_token", lambda *a, **k: dict(claims), raising=True
    )
    iap = IapAssertionVerifier(("/projects/1/global/backendServices/2",))
    monkeypatch.setattr(iap, "_transport", lambda: None)
    return iap


def test_on_the_iap_path_an_absent_email_verified_still_authenticates(monkeypatch):
    """Not a gap: IapAssertionVerifier.verify defaults the claim to True before
    `_from_claims` runs, so the listed-path check never sees it absent here."""
    raw = {"email": FIXER, "sub": f"accounts.google.com:{FIXER_UID}"}
    ctx = _authenticator({}, iap=_real_iap(monkeypatch, raw)).authenticate(None, "assertion")
    assert ctx.tenant_id == "eng"
    assert ctx.member_scope == "continuation"


def test_iap_and_bearer_resolve_the_same_context_from_differently_shaped_subs(monkeypatch):
    iap_claims = {
        "email": FIXER,
        "sub": f"accounts.google.com:{FIXER_UID}",
        "email_verified": True,
    }
    auth = _authenticator({"t": _claims(sub=FIXER_UID)}, iap=_real_iap(monkeypatch, iap_claims))
    via_bearer = auth.authenticate(_bearer("t"))
    via_iap = auth.authenticate(None, "assertion")
    assert via_bearer == via_iap
    assert via_iap.member_scope == "continuation"
    assert via_iap.principal.subject == FIXER_UID


def test_a_duplicate_listing_is_a_401_not_a_500_on_both_paths(monkeypatch):
    twice = (
        LISTING,
        TenantMember(email=FIXER, kind="group", principal="research@saga.xyz", uid=FIXER_UID),
    )
    iap = _real_iap(monkeypatch, _claims(sub=f"accounts.google.com:{FIXER_UID}"))
    auth = _authenticator({"t": _claims()}, listing=twice, iap=iap)
    with pytest.raises(Unauthenticated):
        auth.authenticate(_bearer("t"))
    with pytest.raises(Unauthenticated):
        auth.authenticate(None, "assertion")


# ---------------------------------------------------------------------------
# A whitespace-bearing email claim (contract request 30, owner decision
# 2026-09-29, replacing the deleted `test_a_trailing_newline_is_not_a_match`)
#
# Entry 30's prose credited `tenant_member_for`'s `re.fullmatch` with refusing
# a trailing newline, but the ACCEPTED diff normalises with `.strip().lower()`
# BEFORE that `fullmatch` runs, so the frozen function itself accepts one (see
# tests/unit/control_plane/test_group_resolution.py, where the old test was
# removed with the reason recorded in place). `identity.py` is frozen and is
# not changed here. Instead `_from_claims` (auth.py, not frozen) now refuses a
# token whose email claim is not exactly its own `.strip()`, BEFORE the
# listing lookup or anything else runs -- restoring the property the entry's
# text describes, at the layer that is allowed to change.
# ---------------------------------------------------------------------------

def test_a_trailing_newline_or_leading_space_email_never_reaches_the_listing(monkeypatch):
    calls: list[str] = []
    real = auth_module.tenant_member_for

    def _tracking(email: str, subject: str, members: Any) -> Any:
        calls.append(email)
        return real(email, subject, members)

    monkeypatch.setattr(auth_module, "tenant_member_for", _tracking)

    auth = _authenticator(
        {
            "trailing": _claims(FIXER + "\n"),
            "leading": _claims(" " + FIXER),
            "clean": _claims(FIXER),
        }
    )

    with pytest.raises(Unauthenticated):
        auth.authenticate(_bearer("trailing"))
    with pytest.raises(Unauthenticated):
        auth.authenticate(_bearer("leading"))

    # The control: a clean email is unaffected, resolves as before, and IS the
    # one call that reaches the listing -- proving the refusal above is about
    # the whitespace, not a false positive that would also block a real caller.
    ctx = auth.authenticate(_bearer("clean"))
    assert ctx.tenant_id == "eng"
    assert ctx.member_scope == "continuation"

    assert calls == [FIXER], (
        f"tenant_member_for was reached for a whitespace-bearing email claim: {calls}"
    )


def test_a_trailing_newline_email_is_refused_on_the_iap_path_too(monkeypatch):
    iap = _real_iap(
        monkeypatch,
        {"email": FIXER + "\n", "sub": f"accounts.google.com:{FIXER_UID}"},
    )
    with pytest.raises(Unauthenticated):
        _authenticator({}, iap=iap).authenticate(None, "assertion")


# ---------------------------------------------------------------------------
# Settings: TENANT_SERVICE_ACCOUNTS, refused at startup
# ---------------------------------------------------------------------------

def _entry(email: str = FIXER, **overrides: Any) -> dict[str, Any]:
    entry = {"email": email, "kind": "group", "principal": ENG_GROUP, "uid": FIXER_UID}
    entry.update(overrides)
    return entry


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    for name in ("TENANT_SERVICE_ACCOUNTS", "ADMIN_USERS", "ADMIN_POOL_USERS",
                 "SECRET_ADMIN_PRINCIPALS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _listing(env, *entries: dict[str, Any]) -> None:
    env.setenv("TENANT_SERVICE_ACCOUNTS", json.dumps(list(entries)))


@pytest.mark.parametrize("raw", [None, "", "[]"])
def test_no_listing_is_an_empty_tuple(env, raw):
    if raw is not None:
        env.setenv("TENANT_SERVICE_ACCOUNTS", raw)
    assert ApiSettings.from_env().tenant_service_accounts == ()


def test_a_listing_parses_as_a_list_of_members(env):
    _listing(env, _entry(email=FIXER.upper(), principal="ENG@saga.xyz", uid=f" {FIXER_UID} "))
    assert ApiSettings.from_env().tenant_service_accounts == (LISTING,)


def test_an_object_instead_of_a_list_is_refused(env):
    env.setenv("TENANT_SERVICE_ACCOUNTS", json.dumps({FIXER: _entry()}))
    with pytest.raises(ValueError):
        ApiSettings.from_env()


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(_entry(email="bogdan@saga.xyz"), id="human"),
        # A Google-managed service agent whose `gcp-sa-*` half happens to look
        # like a project id: the frozen regex accepts the shape, so it is the
        # pin to THIS project that refuses it (see the test after this one).
        pytest.param(
            _entry(email="service-123456@gcp-sa-cloudbuild.iam.gserviceaccount.com"),
            id="google-managed-service-agent",
        ),
        pytest.param(
            _entry(email="swarm-ci-fix@other-project.iam.gserviceaccount.com"),
            id="other-project",
        ),
        pytest.param(
            _entry(email="123456789012-compute@developer.gserviceaccount.com"),
            id="compute-default",
        ),
        pytest.param(_entry(uid=""), id="empty-uid"),
        pytest.param(_entry(uid="   "), id="blank-uid"),
        pytest.param({k: v for k, v in _entry().items() if k != "uid"}, id="missing-uid"),
        pytest.param(_entry(kind="workspace"), id="unknown-kind"),
        pytest.param(_entry(principal=""), id="empty-principal"),
    ],
)
def test_a_bad_entry_is_refused_at_startup(env, entry):
    _listing(env, entry)
    with pytest.raises(ValueError):
        ApiSettings.from_env()


def test_the_project_pin_refuses_what_the_shape_alone_accepts(env):
    """A `service-<n>@gcp-sa-<x>` service agent fits the frozen regex when its
    second half looks like a project id; only the pin to THIS project refuses
    it. `identity.py` has no project id to pin against, which is why the pin
    lives in settings.py (and in variables.tf) rather than in the regex."""
    from swarm_common.identity import SERVICE_ACCOUNT_EMAIL

    agent = "service-123456@gcp-sa-foobar.iam.gserviceaccount.com"
    assert SERVICE_ACCOUNT_EMAIL.fullmatch(agent), "precondition: the shape alone accepts it"
    _listing(env, _entry(email=agent))
    with pytest.raises(ValueError):
        ApiSettings.from_env()


@pytest.mark.parametrize("second_principal", [ENG_GROUP, "research@saga.xyz"])
def test_a_duplicate_email_is_refused_under_the_same_or_another_tenant(env, second_principal):
    _listing(env, _entry(), _entry(email=FIXER.upper(), principal=second_principal))
    with pytest.raises(ValueError, match="(?i)more than once|duplicate|twice"):
        ApiSettings.from_env()


@pytest.mark.parametrize("variable", ["ADMIN_USERS", "ADMIN_POOL_USERS", "SECRET_ADMIN_PRINCIPALS"])
def test_an_account_that_is_also_an_admin_is_refused(env, variable):
    env.setenv(variable, FIXER.upper())
    _listing(env, _entry())
    with pytest.raises(ValueError):
        ApiSettings.from_env()


# ---------------------------------------------------------------------------
# Audit: submit and cancel record that the tenant came from a listing
# ---------------------------------------------------------------------------

@pytest.fixture
def listed_context(db, tokens, group_map, objects):
    tokens = dict(tokens)
    tokens["token-fixer"] = _claims()
    return build_context(
        settings=api_settings(tenant_service_accounts=(LISTING,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )


def _events(ctx, task_id: str, kind: EventType) -> list[Any]:
    page = ctx.store.list_events("eng", task_id, limit=50)
    return [e for e in page.items if e.type is kind]


def _listed_auth(ctx, scope: str):
    listed = ctx.authenticator.authenticate(_bearer("token-fixer"))
    # `member_scope` is "continuation" for every listing today; a future scope
    # that permits submit or cancel must not have to re-wire the audit trail.
    from dataclasses import replace

    return replace(listed, member_scope=scope)


def test_a_listed_submission_records_the_account_and_the_listing(listed_context, db):
    seed_tenant(db, "eng")
    auth = _listed_auth(listed_context, "")
    result = listed_context.submissions.submit_tasks(
        auth, [TaskCreate(runner_profile="mock", input={"prompt": "fix it"})]
    )
    task = result.tasks[0]
    assert task.submitted_by == FIXER
    (event,) = _events(listed_context, task.id, EventType.SUBMITTED)
    assert event.detail["submitted_by"] == FIXER
    assert event.detail["tenant_member"] == "service_account"


def test_an_ordinary_submission_has_no_tenant_member_key(client, api_context, db):
    seed_tenant(db, "eng")
    created = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "hi"}},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["task"]["id"]
    (event,) = _events(api_context, task_id, EventType.SUBMITTED)
    assert event.detail["submitted_by"] == "alice@saga.xyz"
    assert "tenant_member" not in event.detail


def test_a_cancel_through_a_listing_records_the_listing(listed_context, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_c", tenant_id="eng", state="QUEUED")
    app = create_app(listed_context)
    auth = _listed_auth(listed_context, "")
    app.dependency_overrides[current_auth] = lambda: auth
    response = TestClient(app, raise_server_exceptions=False).post("/v1/tasks/task_c/cancel")
    assert response.status_code == 200, response.text
    (event,) = _events(listed_context, "task_c", EventType.CANCELLED)
    assert event.detail["requested_by"] == FIXER
    assert event.detail["tenant_member"] == "service_account"


def test_an_ordinary_cancel_has_no_tenant_member_key(client, api_context, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_c", tenant_id="eng", state="QUEUED")
    response = client.post("/v1/tasks/task_c/cancel", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    (event,) = _events(api_context, "task_c", EventType.CANCELLED)
    assert "tenant_member" not in event.detail


def test_the_listed_account_itself_cannot_submit_or_cancel(listed_context, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_c", tenant_id="eng", state="QUEUED")
    client = TestClient(create_app(listed_context), raise_server_exceptions=False)
    headers = {"Authorization": "Bearer token-fixer"}
    body = {"runner_profile": "mock", "input": {"prompt": "hi"}}
    assert client.post("/v1/tasks", headers=headers, json=body).status_code == 403
    assert client.post("/v1/tasks/task_c/cancel", headers=headers).status_code == 403
