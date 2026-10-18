"""The role floor on a human-step answer, and a platform admin's reach to it
(docs/design/workspace-roles.md §4.2.3, §4.8; human-steps.md §4.2; lane
ROLES-GATES-DECIDE, part of #1518).

`POST /v1/workflows/{wf}/steps/{step}/input` checks, in order: a person
(`person_required`); `may_act_in`, fresh, which a member or -- in a GROUP
workspace -- a platform admin passes (`not_a_member` otherwise); then
`member`, read fresh (`role_member_required`, through its switch).

Who is who, in eng: `alice` a member; `vic` in eng's group, narrowed to
`viewer`; `ops` a platform admin in no tenant group, reaching eng through
`platform_admin_reach`; `carol` in no group, with her own `u-carol`.

Offline, with HW-API-ANSWER's fixtures (test_await_input_api.py).
"""

from __future__ import annotations

import logging
import secrets

import pytest
from fastapi.testclient import TestClient

from swarm_api import awaitinput, refusals, roles
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.roles import RoleStore

from .conftest import ADMIN_GROUP, ENG_GROUP, auth_header, seed_tenant
from .test_await_input_api import (  # noqa: F401 -- HW-API-ANSWER's fixtures, used by name
    BOT,
    _person,
    _personal_tenant,
    await_enabled,
    launch,
    make_context,
    park,
    snapshot,
    tokens,
    url,
    verdict,
    writer,
)
from .test_roles_store import grant

MEMBER_SWITCH = refusals.env_name("role_member_required")
VIC = "vic@saga.xyz"
OPS = "ops@saga.xyz"


@pytest.fixture
def group_map(group_map):
    return {**group_map, BOT: (ENG_GROUP,), VIC: (ENG_GROUP,), OPS: (ADMIN_GROUP,)}


@pytest.fixture
def signing_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def api_context(db, tokens, group_map, objects, monkeypatch, signing_key):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    return make_context(db, tokens, objects, StaticGroups(group_map),
                        platform_admin_reach=True, settings_key=signing_key)


@pytest.fixture
def client(api_context, writer) -> TestClient:
    return TestClient(create_app(api_context, input_writer=writer), raise_server_exceptions=False)


@pytest.fixture
def waiting(client, db, await_enabled) -> dict:
    ids = launch(client, db)
    park(db, ids["review"])
    return ids


@pytest.fixture
def vic_is_viewer(api_context):
    grant(api_context.roles, VIC, roles.VIEWER)


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "on")


@pytest.fixture
def switch_off(monkeypatch):
    monkeypatch.setenv(MEMBER_SWITCH, "off")


def _as(user: str, tenant: str | None = None) -> dict[str, str]:
    headers = auth_header(user)
    if tenant:
        headers["X-Swarm-Tenant"] = tenant
    return headers


def test_a_viewer_is_refused_an_answer_and_nothing_is_written(
    client, db, objects, waiting, vic_is_viewer, switch_on,
):
    before = snapshot(db, objects, waiting["review"])
    response = client.post(url(waiting), headers=_as("vic"), json=verdict("r1-vic"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_member_required"
    assert response.json()["detail"]["role"] == roles.VIEWER
    assert snapshot(db, objects, waiting["review"]) == before


def test_with_the_switch_off_a_viewers_answer_is_taken_and_logged(
    client, db, waiting, vic_is_viewer, switch_off, caplog,
):
    with caplog.at_level(logging.WARNING):
        response = client.post(url(waiting), headers=_as("vic"), json=verdict("r1-vic"))
    assert response.status_code == 200, response.text
    assert response.json()["answered_by"] == VIC
    assert "refusal report-only code=role_member_required" in caplog.text


def test_a_member_answers_with_the_switch_on(client, waiting, vic_is_viewer, switch_on):
    response = client.post(url(waiting), headers=_as("alice"), json=verdict())
    assert response.status_code == 200, response.text


def test_the_answer_reads_the_role_fresh(client, db, api_context, waiting, switch_on):
    assert api_context.roles.person_role("eng", VIC).role == roles.MEMBER   # cached
    other = RoleStore(db, api_context.settings, metrics=ApiMetrics(), now=api_context.now)
    grant(other, VIC, roles.VIEWER)
    assert api_context.roles.person_role("eng", VIC).role == roles.MEMBER   # still cached
    response = client.post(url(waiting), headers=_as("vic"), json=verdict("r1-vic"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_member_required"


def test_a_non_member_platform_admin_answers_a_group_workspaces_step(
    client, db, waiting, switch_on,
):
    response = client.post(url(waiting), headers=_as("ops", "eng"), json=verdict("r1-ops"))
    assert response.status_code == 200, response.text
    assert response.json()["answered_by"] == OPS
    acted = [r for r in db.dump("admin_audit/").values()
             if r["action"] == roles.AUDIT_PLATFORM_ADMIN_ACTED and r["by"] == OPS]
    assert acted, "the reach's answer is audited before it runs"


def test_a_platform_admin_still_answers_no_one_elses_personal_workspace(db, api_context):
    _personal_tenant(db)
    for other in ("root@saga.xyz", OPS):
        with pytest.raises(awaitinput.Forbidden) as refused:
            awaitinput.member_refusal(api_context, _person(other, "u-carol"), "u-carol")
        assert refused.value.detail["reason"] == "not_a_member"
    awaitinput.member_refusal(api_context, _person("carol@saga.xyz", "u-carol"), "u-carol")


def test_the_form_is_offered_to_the_reach_and_not_to_an_enforced_viewer(
    api_context, waiting, vic_is_viewer, monkeypatch,
):
    ops = _person(OPS, "eng")
    vic = _person(VIC, "eng")
    assert awaitinput.may_answer(api_context, ops, "eng") is True
    monkeypatch.setenv(MEMBER_SWITCH, "off")
    assert awaitinput.may_answer(api_context, vic, "eng") is True
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    assert awaitinput.may_answer(api_context, vic, "eng") is False
