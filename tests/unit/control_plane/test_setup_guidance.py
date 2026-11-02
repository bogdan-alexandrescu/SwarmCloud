"""Admin › Setup's API sends the person the right way (lane
DAYONE-SETUP-API-4, part of #2688; findings 27, 28 and 29 of the 2026-10-30
audit).

27. The first team's group is checked before anything is written: swarm-api
    must be able to read it and the installer must be in it, else the answer
    names the reason and the Google Admin groups page.
28. A self-check before swarm install's infra pass 3 (`app_id_not_configured`)
    is a wait for that pass, not a failure that suggests creating the App
    again.
29. The App manifest is posted to the account's own "new App" page: a user
    account's (`github.com/settings/apps/new`) or an organisation's.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .conftest import auth_header
from .test_setup_routes import (  # noqa: F401 - fixtures
    ENV,
    ORG,
    ROOT,
    TEAM_GROUP,
    TEAMS_PREFIX,
    create_team,
    directory,
    fake_secrets,
    github,
    group_map,
    pool,
    seed_engine_document,
    setup_app,
    setup_client,
    tokens,
)

GROUPS_PAGE = "https://admin.google.com/ac/groups"
GITHUB_APP_DOC = f"swarm-install/{ENV}/console/github_app"
SELF_CHECK = "/v1/admin/setup/github-app/self-check"
MANIFEST = "/v1/admin/setup/github-app/manifest"
NEW_GROUP = "new-team@example.com"


def _nothing_written(db) -> bool:
    # Team documents live at TEAMS_PREFIX since DAYONE-SETUP-API-7 (PR 2850):
    # the old `console/teams/<id>` was not a valid Firestore path, so a check
    # for it could never fail.
    return not any(k.startswith("workspaces/") or k.startswith(TEAMS_PREFIX) for k in db.docs)


# -- 27: the first team's group ------------------------------------------------


def test_an_unreadable_group_is_refused_with_the_groups_page(setup_client, db,
                                                             directory) -> None:
    directory.unreadable.add(NEW_GROUP)
    response = create_team(setup_client, group=NEW_GROUP)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "group_unreadable"
    assert detail["admin_groups_url"] == GROUPS_PAGE
    assert GROUPS_PAGE in response.json()["message"]
    assert _nothing_written(db)


def test_a_group_the_installer_is_not_in_is_refused_before_any_write(setup_client, db,
                                                                     directory) -> None:
    """The verify runs look for a team the installer is in; a group they are
    not in would pass here and fail there (`lists no team for you yet`)."""
    response = create_team(setup_client, group=NEW_GROUP)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == "installer_not_member"
    assert detail["installer"] == ROOT
    assert detail["admin_groups_url"] == GROUPS_PAGE
    assert detail["add_members_url"] == "https://admin.google.com/ac/groups/0idnewteam/members"
    assert (ROOT, (NEW_GROUP,)) in directory.asked
    assert _nothing_written(db)
    directory.join(ROOT, NEW_GROUP)
    assert create_team(setup_client, group=NEW_GROUP).status_code == 202
    # The control: the same check sees the team the accepted request wrote.
    assert any(k.startswith(TEAMS_PREFIX) for k in db.docs)
    assert not _nothing_written(db)


def test_the_engine_named_installer_is_the_one_checked(setup_client, db, directory) -> None:
    seed_engine_document(db)
    db.docs[f"swarm-install/{ENV}"]["installer"] = "Alice@example.com"
    directory.join("alice@example.com", NEW_GROUP)
    assert create_team(setup_client, group=NEW_GROUP).status_code == 202
    refused = create_team(setup_client, group="other-team@example.com")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["installer"] == "alice@example.com"


def test_a_member_installer_creates_the_team(setup_client, db) -> None:
    response = create_team(setup_client, group=TEAM_GROUP.upper())
    assert response.status_code == 202, response.text


# -- 28: the self-check before infra pass 3 -----------------------------------


class AppCheck:
    def __init__(self, reason: str | None) -> None:
        self.reason = reason

    def check_app_key(self) -> dict[str, Any]:
        return {"ok": self.reason is None, "app_id": None, "slug": None, "reason": self.reason}


def test_a_self_check_before_pass_3_is_a_wait(setup_app, setup_client, db) -> None:
    setup_app.state.forge_app = AppCheck("app_id_not_configured")
    response = setup_client.post(SELF_CHECK, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["state"] == "waiting_for_infra_pass_3"
    assert "waiting for swarm install's infra pass 3" in body["message"]
    assert "again" not in body["message"]
    recorded = db.docs[GITHUB_APP_DOC]["self_check"]
    assert recorded["state"] == "waiting_for_infra_pass_3" and recorded["ok"] is False
    page = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()
    assert page["github_app"]["self_check"]["state"] == "waiting_for_infra_pass_3"


def test_a_passing_and_a_failing_self_check_keep_their_states(setup_app, setup_client,
                                                              db) -> None:
    setup_app.state.forge_app = AppCheck("key_rejected")
    failed = setup_client.post(SELF_CHECK, headers=auth_header("root")).json()
    assert failed["state"] == "failed" and failed["reason"] == "key_rejected"
    setup_app.state.forge_app = AppCheck(None)
    passed = setup_client.post(SELF_CHECK, headers=auth_header("root")).json()
    assert passed["ok"] is True and passed["state"] == "passed"


def test_a_self_check_recorded_before_this_change_reads_with_a_state(setup_client,
                                                                     db) -> None:
    db.docs[GITHUB_APP_DOC] = {"id": 4242, "self_check": {
        "ok": False, "reason": "app_id_not_configured", "at": None}}
    page = setup_client.get("/v1/admin/setup", headers=auth_header("root")).json()
    assert page["github_app"]["self_check"]["state"] == "waiting_for_infra_pass_3"


# -- 29: a personal GitHub account ---------------------------------------------


def _users_requests(github) -> list[dict[str, Any]]:
    return [r for r in github.requests
            if r["url"] == f"https://api.github.com/users/{ORG}"]


def test_a_user_account_posts_to_its_own_new_app_page(setup_client, github) -> None:
    github.account_types[ORG] = "User"
    response = setup_client.post(MANIFEST, json={}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    action = urlsplit(response.json()["action"])
    assert (action.scheme, action.netloc, action.path) == (
        "https", "github.com", "/settings/apps/new")
    assert action.query.startswith("state=")
    asked = _users_requests(github)
    assert [r["method"] for r in asked] == ["GET"]
    assert "Authorization" not in asked[0]["headers"]


def test_an_organisation_posts_to_the_organisations_page(setup_client, github) -> None:
    github.account_types[ORG] = "Organization"
    response = setup_client.post(MANIFEST, json={}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["action"].startswith(
        f"https://github.com/organizations/{ORG}/settings/apps/new?state=")
    assert len(_users_requests(github)) == 1


def test_an_unanswered_account_lookup_starts_nothing(setup_client, db, github) -> None:
    """Neither page is guessed: a wrong one is GitHub's 404."""
    github.users_status = 403
    response = setup_client.post(MANIFEST, json={}, headers=auth_header("root"))
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "github_refused"
    assert GITHUB_APP_DOC not in db.docs
