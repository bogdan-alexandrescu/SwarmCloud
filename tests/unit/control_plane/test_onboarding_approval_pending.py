"""ORG_APPROVAL_PENDING is recorded and re-checked (docs/onboarding.md §2.3,
§2.4, §3.1-§3.2; #780).

GitHub does not show a user access token an install request it is waiting
on, so SwarmCloud records one itself: `POST /v1/access/orgs/{owner}/
install-request`, which the console and the CLI call when the person opens
the App's install page for an org they do not own. What is held here, every
case offline against the access API's forge fake:

  * the request writes the person's `forge_orgs` document with
    `install_state: requested` and `requested_at`, in their tenant, and makes
    no GitHub call;
  * while `GET /user/installations` does not list the owner, `GET
    /v1/access/orgs` and `GET /v1/onboarding` both say ORG_APPROVAL_PENDING
    with §2.3's copy, word for word -- even when another owner is enabled,
    which otherwise answers `app_installed` with no GitHub read;
  * a requested owner is not an enabled one: Access does not list it among
    the enabled orgs, and nothing under it can be read;
  * as soon as the installations list the owner, the next read -- and the
    refresh sweep (`POST /v1/admin/forge/refresh`, every 15 minutes, the
    interval the copy promises) -- marks it `installed`, enabled, with its
    installation id, and the code is gone;
  * another member's or another tenant's owners never show the request, and
    no document, answer or log line holds a token value.

Every token-shaped value is built at runtime, never written as a literal.
"""

from __future__ import annotations

import logging
from typing import Any

from swarm_api import access, forgeapp
from swarm_api.onboarding import recovery_copy

from .conftest import auth_header
from .test_access_api import (  # noqa: F401  (fixtures)
    ORG,
    AccessGitHub,
    _connect,
    _enable,
    _grant,
    _installation,
    _no_value_anywhere,
    api,
    clock,
    github,
    issues_transport,
    slots,
    tenant_tokens,
)

PENDING = "other-org"
CODE = "ORG_APPROVAL_PENDING"


def _request(api, owner: str = PENDING, user: str = "alice"):
    return api.post(f"/v1/access/orgs/{owner}/install-request", headers=auth_header(user))


def _doc(db, owner: str = PENDING, tenant: str = "eng",
         email: str = "alice@saga.xyz") -> dict[str, Any] | None:
    return db.docs.get(f"forge_orgs/{access.org_id_for(tenant, email, owner)}")


def _owner_row(api, owner: str = PENDING, user: str = "alice") -> dict[str, Any] | None:
    answer = api.get("/v1/access/orgs", headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return next((r for r in answer.json()["owners"] if r["owner"].lower() == owner), None)


def _orgs_step(api, user: str = "alice") -> dict[str, Any]:
    answer = api.get("/v1/onboarding", headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return next(s for s in answer.json()["steps"] if s["step"] == "orgs_enabled")


def _pending_issues(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [i for i in step["issues"] if i["code"] == CODE]


def test_the_copy_promises_the_sweeps_real_interval():
    # The sweep is the forge-refresh job: every 15 minutes
    # (terraform/modules/scheduler/variables.tf forge_refresh_schedule).
    assert "every 15 minutes" in recovery_copy(CODE, owner=PENDING)


def test_an_install_request_is_recorded_with_no_github_call(api, github, db, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(api, github)
    calls = len(github.calls)
    answer = _request(api)
    assert answer.status_code == 200, answer.text
    org = answer.json()["org"]
    assert org["owner"] == PENDING and org["install_state"] == "requested"
    assert org["enabled"] is False and org["requested_at"]
    assert org["code"] == CODE and org["copy"] == recovery_copy(CODE, owner=PENDING)
    assert len(github.calls) == calls, "recording a request asks GitHub nothing"
    doc = _doc(db)
    assert doc is not None
    assert doc["install_state"] == "requested" and doc["requested_at"] is not None
    assert doc["tenant_id"] == "eng" and doc["user"] == "alice@saga.xyz"
    assert doc["user_hash"] == forgeapp.user_hash("alice@saga.xyz")
    _no_value_anywhere(db, github, [answer.text, caplog.text])


def test_a_request_without_a_connection_says_connect_first(api, db):
    answer = _request(api)
    assert answer.status_code == 409 and answer.json()["code"] == "github_not_connected"
    assert _doc(db) is None


def test_an_owner_that_is_not_a_login_is_refused(api, github, db):
    _connect(api, github)
    answer = _request(api, owner="-not_a_login-")
    assert answer.status_code == 422
    assert not db.dump("forge_orgs/")


def test_a_pending_owner_shows_org_approval_pending_on_access_and_setup(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    row = _owner_row(api)
    assert row is not None
    assert row["install_state"] == "requested" and row["enabled"] is False
    assert row["code"] == CODE and row["copy"] == recovery_copy(CODE, owner=PENDING)
    assert row["requested_at"]
    issues = _pending_issues(_orgs_step(api))
    assert [(i["owner"], i["copy"]) for i in issues] == [
        (PENDING, recovery_copy(CODE, owner=PENDING))]


def test_pending_shows_on_setup_even_while_another_owner_is_enabled(api, github, db):
    # An enabled owner answers app_installed with no GitHub read; a pending
    # request is read at GitHub all the same, or it would never flip.
    _connect(api, github)
    _enable(api)
    assert _request(api).status_code == 200
    step = _orgs_step(api)
    assert [i["owner"] for i in _pending_issues(step)] == [PENDING]
    assert _doc(db)["install_state"] == "requested"


def test_a_requested_owner_is_not_an_enabled_one(api, github, db):
    _connect(api, github)
    _enable(api)
    assert _request(api).status_code == 200
    overview = api.get("/v1/access", headers=auth_header("alice")).json()
    assert [o["owner"] for o in overview["orgs"]] == [ORG]
    assert [r["owner"] for r in overview["requested"]] == [PENDING]
    listing = api.get(f"/v1/access/orgs/{PENDING}/repositories",
                      headers=auth_header("alice"))
    assert listing.status_code in (404, 409), listing.text
    refused = _grant(api, f"{PENDING}/repo-0001")
    assert refused.status_code >= 400, refused.text
    assert refused.json()["detail"]["failure_code"] == "REPO_NOT_INSTALLED"
    assert not db.dump("forge_grants/")


def test_the_next_read_marks_it_installed_once_github_lists_it(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    assert _pending_issues(_orgs_step(api))
    github.installs.append(_installation(13, PENDING))
    step = _orgs_step(api)
    assert _pending_issues(step) == []
    doc = _doc(db)
    assert doc["install_state"] == "installed" and doc["installation_id"] == 13
    assert doc["requested_at"] is not None, "when it was asked is kept"
    row = _owner_row(api)
    assert row["install_state"] == "installed" and row["enabled"] is True
    assert row.get("code") is None
    overview = api.get("/v1/access", headers=auth_header("alice")).json()
    assert PENDING in [o["owner"] for o in overview["orgs"]]
    assert overview["requested"] == []


def test_the_access_owners_read_marks_it_installed_too(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    github.installs.append(_installation(13, PENDING))
    row = _owner_row(api)
    assert row["install_state"] == "installed" and row["enabled"] is True
    assert _doc(db)["install_state"] == "installed"


def test_the_refresh_sweep_rechecks_a_requested_owner(api, github, db, caplog):
    caplog.set_level(logging.DEBUG)
    _connect(api, github)
    assert _request(api).status_code == 200
    app = api.app.state.forge_app
    still = app.sweep()
    assert still.to_api()["installs_rechecked"] == 1
    assert still.to_api()["installs_found"] == 0
    assert _doc(db)["install_state"] == "requested"
    assert _doc(db)["checked_at"] is not None
    github.installs.append(_installation(13, PENDING))
    report = app.sweep().to_api()
    assert report["installs_rechecked"] == 1 and report["installs_found"] == 1
    doc = _doc(db)
    assert doc["install_state"] == "installed" and doc["installation_id"] == 13
    assert _pending_issues(_orgs_step(api)) == []
    # Nothing left to re-check: the next sweep asks GitHub for no installations.
    assert app.sweep().to_api()["installs_rechecked"] == 0
    _no_value_anywhere(db, github, [caplog.text])


def test_the_sweep_route_reports_the_recheck(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    github.installs.append(_installation(13, PENDING))
    answer = api.post("/v1/admin/forge/refresh", headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["installs_found"] == 1
    assert _doc(db)["install_state"] == "installed"


def test_a_sweep_github_did_not_answer_leaves_the_request(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    github.down.add("/user/installations")
    report = api.app.state.forge_app.sweep().to_api()
    assert report["installs_unreachable"] == 1 and report["installs_found"] == 0
    assert _doc(db)["install_state"] == "requested"


def test_a_request_for_an_enabled_owner_changes_nothing(api, github, db):
    _connect(api, github)
    _enable(api)
    before = dict(_doc(db, owner=ORG))
    answer = _request(api, owner=ORG)
    assert answer.status_code == 200, answer.text
    assert answer.json()["org"]["install_state"] == "installed"
    assert answer.json()["org"]["enabled"] is True
    assert _doc(db, owner=ORG) == before


def test_removing_a_requested_owner_withdraws_the_request(api, github, db):
    _connect(api, github)
    assert _request(api).status_code == 200
    gone = api.delete(f"/v1/access/orgs/{PENDING}", headers=auth_header("alice"))
    assert gone.status_code == 200, gone.text
    assert _doc(db) is None
    row = _owner_row(api)
    assert row["install_state"] == "not_installed" and row.get("code") is None


def test_another_members_or_tenants_owners_never_show_the_request(api, github, db):
    _connect(api, github)
    _connect(api, github, user="root")
    assert _request(api).status_code == 200
    row = _owner_row(api, user="root")
    assert row is not None and row["install_state"] == "not_installed"
    assert row.get("code") is None
    assert _pending_issues(_orgs_step(api, user="root")) == []
    assert _doc(db, email="root@saga.xyz") is None
    assert _doc(db, tenant="research") is None
    # A document at alice's id that names another tenant is not hers.
    db.docs[f"forge_orgs/{access.org_id_for('eng', 'root@saga.xyz', 'third-org')}"] = {
        "tenant_id": "research", "user": "root@saga.xyz", "owner": "third-org",
        "user_hash": forgeapp.user_hash("root@saga.xyz"), "install_state": "requested"}
    assert _owner_row(api, owner="third-org", user="root") is None


def test_installed_nowhere_but_asked_for_is_app_installed_in_progress(api, github, db):
    # The person did their part and waits on an org owner: not `todo`, and
    # the rest of the checklist still waits for an installation.
    github.installs = []
    _connect(api, github)
    assert _request(api).status_code == 200
    view = api.get("/v1/onboarding", headers=auth_header("alice")).json()
    steps = {s["step"]: s for s in view["steps"]}
    installed = steps["app_installed"]
    assert installed["state"] == "in_progress" and installed["code"] == CODE
    assert installed["copy"] == recovery_copy(CODE, owner=PENDING)
    assert installed["evidence"]["requested"] == [PENDING]
    assert view["next_step"] == "app_installed"
    assert steps["orgs_enabled"]["evidence"] == {"waiting_for": "app_installed"}
    github.installs.append(_installation(13, PENDING))
    view = api.get("/v1/onboarding", headers=auth_header("alice")).json()
    installed = next(s for s in view["steps"] if s["step"] == "app_installed")
    assert installed["state"] == "done" and installed["code"] is None
