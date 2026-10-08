"""`GET /v1/onboarding`: the resumable checklist, DERIVED from today's records
(docs/onboarding.md §2.1-§2.3; #780, lane OB1).

What is held here:

  * each of the seven steps' state -- todo, in_progress, done, failed, stale --
    from fixture records: the token record the caller acts through (their
    own user slot, else the tenant token) and its probe, OB0b's SSO and org
    evidence, the tenant's registrations, and the token x repository checks;
  * every failure code serves §2.3's recovery copy WORD FOR WORD, read from
    docs/onboarding.md itself, with {owner}, {repo}, {url} and {login} filled;
  * a read that did not come back is FORGE_UNREACHABLE, never a failure;
  * `app_installed` (#780, 2026-10-08): an App connection installed nowhere
    is `todo` with the install page and the rest waits for it, an unread
    installation list is in progress, never "installed nowhere", and a
    token connection needs no installation;
  * tenant isolation: a caller sees only their own tenant's records, and
    never another member's user slot;
  * the route is read-only: a GET writes nothing.

Nothing reaches a network or a cloud; every token-shaped value is absent,
because no record holds one.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api import onboarding
from swarm_api.gittokens import (
    APP_UNKNOWN,
    CAPABILITIES,
    CHECKS_COLLECTION,
    COLLECTION,
    FINE_GRAINED_UNKNOWN,
    GitTokenRecord,
    Scope,
    TokenState,
    provider_suffix,
    record_for_slot,
)
from swarm_api.onboarding import Caller, derive

from .conftest import auth_header, seed_tenant

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
ALICE = Caller(email="alice@saga.xyz", tenant_id="eng")
DOC = Path(__file__).resolve().parents[3] / "docs" / "onboarding.md"


# -- fixtures ----------------------------------------------------------------------


def tenant_token(**fields) -> GitTokenRecord:
    record = record_for_slot("eng", Scope.TENANT, registered_by="swarm-api", now=T0 - timedelta(days=9))
    record.kind = "classic_pat"
    record.forge_login = "swarm-bot"
    record.state = TokenState.ACTIVE
    record.verified_at = T0 - timedelta(hours=2)
    record.probe_attempted_at = T0 - timedelta(hours=2)
    record.probe_complete = True
    record.orgs = ["example-org"]
    record.access_evidence_at = T0 - timedelta(hours=2)
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def user_token(email: str = "alice@saga.xyz", **fields) -> GitTokenRecord:
    record = record_for_slot("eng", Scope.USER, user=email, registered_by=email,
                             now=T0 - timedelta(days=3))
    record.kind = "fine_grained_pat"
    record.forge_login = email.split("@")[0] + "-gh"
    record.state = TokenState.ACTIVE
    record.verified_at = T0 - timedelta(hours=1)
    record.probe_attempted_at = T0 - timedelta(hours=1)
    record.probe_complete = True
    record.orgs = []
    record.access_evidence_at = T0 - timedelta(hours=1)
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def registration(repository: str, *, can_push: bool = True, archived: bool = False,
                 tenant_id: str = "eng") -> dict:
    owner, repo = repository.split("/")
    return {
        "repo_id": onboarding.repo_id_for(tenant_id, owner, repo),
        "tenant_id": tenant_id,
        "owner": owner,
        "repo": repo,
        "archived": archived,
        "access": {"token_scope": "tenant", "can_read": True, "can_push": can_push,
                   "read_at": T0 - timedelta(days=1)},
        "created_at": T0 - timedelta(days=1),
        "updated_at": T0 - timedelta(days=1),
    }


def checks(record: GitTokenRecord, reg: dict, *, at: datetime = T0 - timedelta(hours=2),
           complete: bool = True, error: str | None = None, **cells) -> dict:
    caps = {cap: {"state": "ok", "reason": "measured", "evidence": "", "verified_at": at}
            for cap in CAPABILITIES}
    for cap, cell in cells.items():
        state, reason = cell
        caps[cap] = {"state": state, "reason": reason, "evidence": "", "verified_at": at}
    return {
        "token_id": record.token_id,
        "tenant_id": record.tenant_id,
        "repo_id": reg["repo_id"],
        "repository": f"{reg['owner']}/{reg['repo']}",
        "capabilities": caps,
        "verified_at": at if complete else None,
        "attempted_at": at,
        "complete": complete,
        "error": error,
    }


def run(records=(), pairs=(), regs=(), *, caller: Caller = ALICE, tenant_lists_git=True,
        installations=None) -> dict:
    return derive(caller, records=list(records), pair_docs=list(pairs),
                  registrations=list(regs), tenant_lists_git=tenant_lists_git, now=T0,
                  installations=installations)


def step(view: dict, name: str) -> dict:
    found = [s for s in view["steps"] if s["step"] == name]
    assert len(found) == 1, name
    return found[0]


def states(view: dict) -> dict[str, str]:
    return {s["step"]: s["state"] for s in view["steps"]}


# -- the copy is §2.3's, word for word --------------------------------------------


def _doc_copy() -> dict[str, str]:
    """§2.3's table as the doc states it: code -> the quoted copy."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("### 2.3", 1)[1].split("### 2.4", 1)[0]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"^\| `([A-Z_]+)` \| .* \| “(.*)” \|$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def test_every_code_serves_the_docs_copy_word_for_word() -> None:
    doc = _doc_copy()
    assert len(doc) == 11, sorted(doc)
    assert onboarding.COPY == doc


def test_the_copy_is_filled_and_never_leaves_a_placeholder() -> None:
    for code in onboarding.COPY:
        line = onboarding.recovery_copy(code, owner="example-org", repo="example-org/api",
                                        url="https://github.com/settings/tokens",
                                        login="example-user")
        assert "{" not in line and "}" not in line, code


# -- the steps and the next step ----------------------------------------------------


def test_the_steps_are_section_2_1s_with_the_workspace_steps_after_signed_in() -> None:
    # docs/workspaces.md §6.1: `workspace` second, `claude_account` right after.
    view = run()
    assert [s["step"] for s in view["steps"]] == [
        "signed_in", "workspace", "claude_account", "github_connected", "app_installed",
        "orgs_enabled", "repos_chosen", "access_verified", "ready"]
    assert set(onboarding.STATES) == {"todo", "in_progress", "done", "failed", "stale"}


def test_a_tenant_with_no_git_token_is_signed_in_and_nothing_else() -> None:
    view = run(tenant_lists_git=False)
    assert states(view) == {
        "signed_in": "done", "workspace": "todo", "claude_account": "todo",
        "github_connected": "todo", "app_installed": "todo", "orgs_enabled": "todo",
        "repos_chosen": "todo", "access_verified": "todo", "ready": "todo"}
    assert view["next_step"] == "github_connected"
    assert view["complete"] is False
    signed = step(view, "signed_in")
    assert signed["evidence"]["email"] == "alice@saga.xyz"
    assert signed["evidence"]["tenant_id"] == "eng"
    connect = step(view, "github_connected")
    # Today's working route to a user slot: the stdin store command, by name.
    suffix = provider_suffix(Scope.USER, user="alice@saga.xyz")
    assert connect["evidence"]["store_command"] == (
        f"scripts/create-secrets.sh --tenant eng --provider {suffix} --stdin")
    assert view["user_hash"] == suffix.removeprefix("git-u-")


def test_everything_measured_ok_is_ready() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    docs = registration("swarm-bot/notes", can_push=False)
    view = run([record], [checks(record, api), checks(record, docs)], [api, docs])
    # The workspace steps are not required with WORKSPACE_GATE off (as it
    # ships), so they hold nothing back: test_onboarding_workspace_steps.py.
    held = {k: v for k, v in states(view).items() if k not in ("workspace", "claude_account")}
    assert set(held.values()) == {"done"}, states(view)
    assert view["next_step"] is None and view["complete"] is True
    connect = step(view, "github_connected")
    assert connect["evidence"]["via"] == "tenant"
    assert connect["evidence"]["forge_login"] == "swarm-bot"
    orgs = step(view, "orgs_enabled")["evidence"]["owners"]
    assert {o["owner"]: o["owner_type"] for o in orgs} == {
        "swarm-bot": "User", "example-org": "Organization"}
    chosen = {r["repository"]: r["mode"] for r in step(view, "repos_chosen")["evidence"]["repositories"]}
    assert chosen == {"example-org/api": "write", "swarm-bot/notes": "read"}
    verified = {r["repository"]: r["result"]
                for r in step(view, "access_verified")["evidence"]["repositories"]}
    assert verified == {"example-org/api": "passed", "swarm-bot/notes": "passed"}
    assert step(view, "ready")["evidence"]["first_repository"] in {"example-org/api",
                                                                    "swarm-bot/notes"}


def test_the_first_step_not_done_is_next() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    view = run([record], [], [api])
    assert states(view)["access_verified"] == "in_progress"
    assert view["next_step"] == "access_verified"


# -- github_connected ---------------------------------------------------------------


def test_a_listed_tenant_slot_with_no_record_yet_is_in_progress() -> None:
    view = run([], tenant_lists_git=True)
    connect = step(view, "github_connected")
    assert connect["state"] == "in_progress"
    assert connect["code"] is None
    assert connect["evidence"]["via"] == "tenant"
    assert connect["evidence"]["secret_name"] == "swarm-tenant-eng-git"


def test_a_slot_with_no_value_stored_is_in_progress_with_its_store_command() -> None:
    record = tenant_token(state=TokenState.UNVERIFIED, verified_at=None, forge_login=None,
                          probe_complete=False,
                          probe_error="no value stored in swarm-tenant-eng-git yet: store one "
                                      "with scripts/create-secrets.sh --tenant eng --provider "
                                      "git --stdin")
    connect = step(run([record]), "github_connected")
    assert connect["state"] == "in_progress"
    assert connect["code"] is None
    assert "no value stored" in connect["evidence"]["probe_error"]


def test_an_expired_token_is_refresh_failed_with_its_copy() -> None:
    record = tenant_token(state=TokenState.EXPIRED, expires_at=T0 - timedelta(days=1))
    view = run([record])
    connect = step(view, "github_connected")
    assert connect["state"] == "failed"
    assert connect["code"] == "REFRESH_FAILED"
    assert connect["copy"] == onboarding.COPY["REFRESH_FAILED"].replace("{login}", "swarm-bot")
    # The later steps wait on it rather than claim anything.
    assert states(view)["orgs_enabled"] == "todo"
    assert view["next_step"] == "github_connected"


def test_an_expiry_passed_since_the_last_probe_is_refresh_failed() -> None:
    record = tenant_token(expires_at=T0 - timedelta(minutes=1))
    assert step(run([record]), "github_connected")["code"] == "REFRESH_FAILED"


def test_a_token_github_refused_is_refresh_failed() -> None:
    # A complete probe that left the record unverified: /user answered 401.
    record = tenant_token(state=TokenState.UNVERIFIED, verified_at=None)
    connect = step(run([record]), "github_connected")
    assert (connect["state"], connect["code"]) == ("failed", "REFRESH_FAILED")


def test_an_unanswered_probe_leaves_the_step_as_it_was_and_says_so() -> None:
    record = tenant_token(probe_complete=False, probe_attempted_at=T0 - timedelta(minutes=5),
                          probe_error="/user: GitHub answered HTTP 503")
    connect = step(run([record]), "github_connected")
    assert connect["state"] == "done"
    assert connect["code"] == "FORGE_UNREACHABLE"
    assert connect["copy"] == onboarding.COPY["FORGE_UNREACHABLE"]
    never = tenant_token(state=TokenState.UNVERIFIED, verified_at=None, forge_login=None,
                         probe_complete=False, probe_error="/user: the read failed (TimeoutError)")
    pending = step(run([never]), "github_connected")
    assert (pending["state"], pending["code"]) == ("in_progress", "FORGE_UNREACHABLE")


def test_evidence_older_than_the_reverification_interval_is_stale() -> None:
    record = tenant_token(verified_at=T0 - timedelta(days=3))
    view = run([record])
    assert step(view, "github_connected")["state"] == "stale"
    assert view["next_step"] == "github_connected"


def test_a_revoked_record_is_not_a_connection() -> None:
    record = tenant_token(state=TokenState.REVOKED, revoked_at=T0 - timedelta(days=1))
    connect = step(run([record]), "github_connected")
    assert connect["state"] == "todo"


def test_the_callers_own_user_slot_is_preferred_to_the_tenant_token() -> None:
    mine = user_token()
    view = run([tenant_token(), mine])
    connect = step(view, "github_connected")
    assert connect["evidence"]["via"] == "user"
    assert connect["evidence"]["token_id"] == mine.token_id
    assert connect["evidence"]["forge_login"] == "alice-gh"
    revoked = user_token(state=TokenState.REVOKED)
    assert step(run([tenant_token(), revoked]), "github_connected")["evidence"]["via"] == "tenant"


def test_another_members_user_slot_is_never_used_or_served() -> None:
    root = user_token("root@saga.xyz")
    view = run([root], tenant_lists_git=False)
    assert step(view, "github_connected")["state"] == "todo"
    dumped = json.dumps(view)
    assert "root" not in dumped and root.token_id not in dumped


# -- orgs_enabled -------------------------------------------------------------------


def test_an_org_whose_sso_the_token_is_not_authorised_for_fails_with_its_copy() -> None:
    record = tenant_token(sso_required_orgs=["example-org"])
    orgs = step(run([record]), "orgs_enabled")
    assert (orgs["state"], orgs["code"]) == ("failed", "SSO_NOT_AUTHORISED")
    # A classic token's SSO is authorised on the token's settings page.
    assert orgs["copy"] == (onboarding.COPY["SSO_NOT_AUTHORISED"]
                            .replace("{owner}", "example-org")
                            .replace("{url}", "https://github.com/settings/tokens"))
    issue = orgs["issues"][0]
    assert issue["owner"] == "example-org" and issue["code"] == "SSO_NOT_AUTHORISED"
    owners = {o["owner"]: o["reach"] for o in orgs["evidence"]["owners"]}
    assert owners == {"swarm-bot": "reachable", "example-org": "sso_required"}


def test_an_org_that_refuses_classic_tokens_fails_with_its_copy() -> None:
    record = tenant_token(classic_blocked_orgs=["example-org"])
    orgs = step(run([record]), "orgs_enabled")
    assert (orgs["state"], orgs["code"]) == ("failed", "CLASSIC_PAT_BLOCKED")
    assert orgs["copy"] == onboarding.COPY["CLASSIC_PAT_BLOCKED"].replace("{owner}", "example-org")
    # The route that works today, beside the copy: the stdin store command.
    assert orgs["issues"][0]["store_command"].endswith("--stdin")


def test_orgs_hidden_behind_sso_are_counted_not_failed() -> None:
    record = tenant_token(sso_partial_org_ids=[3, 7])
    orgs = step(run([record]), "orgs_enabled")
    assert orgs["state"] == "done"
    assert orgs["evidence"]["sso_hidden_orgs"] == 2


def test_no_reach_read_yet_is_in_progress() -> None:
    record = tenant_token(forge_login=None, orgs=None, access_evidence_at=None)
    assert step(run([record]), "orgs_enabled")["state"] == "in_progress"


# -- repos_chosen -------------------------------------------------------------------


def test_no_registration_is_todo() -> None:
    view = run([tenant_token()])
    assert step(view, "repos_chosen")["state"] == "todo"
    assert step(view, "access_verified")["state"] == "todo"
    assert view["next_step"] == "repos_chosen"


def test_an_archived_write_registration_fails_with_its_copy() -> None:
    record = tenant_token()
    old = registration("example-org/old", archived=True)
    chosen = step(run([record], [checks(record, old)], [old]), "repos_chosen")
    assert (chosen["state"], chosen["code"]) == ("failed", "REPO_ARCHIVED")
    assert chosen["copy"] == onboarding.COPY["REPO_ARCHIVED"].replace("{repo}", "example-org/old")
    # Archived and read-only is a read grant: nothing to push, nothing wrong.
    ro = registration("example-org/old", archived=True, can_push=False)
    assert step(run([record], [checks(record, ro)], [ro]), "repos_chosen")["state"] == "done"


def test_a_registration_in_an_sso_blocked_org_fails_per_repository() -> None:
    record = tenant_token(sso_required_orgs=["example-org"])
    api = registration("example-org/api")
    chosen = step(run([record], [], [api]), "repos_chosen")
    assert chosen["code"] == "SSO_NOT_AUTHORISED"
    assert chosen["issues"][0]["repository"] == "example-org/api"


def test_another_tenants_registration_is_dropped() -> None:
    record = tenant_token()
    theirs = registration("example-org/api", tenant_id="research")
    assert step(run([record], [], [theirs]), "repos_chosen")["state"] == "todo"


# -- access_verified ----------------------------------------------------------------


def test_a_write_registration_whose_push_is_missing_fails_with_its_copy() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    pair = checks(record, api, push=("missing", "the actor's role does not allow push"),
                  open_pull_requests=("missing", "needs push"))
    verified = step(run([record], [pair], [api]), "access_verified")
    assert (verified["state"], verified["code"]) == ("failed", "PERMISSION_MISSING")
    assert verified["copy"] == (onboarding.COPY["PERMISSION_MISSING"]
                                .replace("{repo}", "example-org/api")
                                .replace("{login}", "swarm-bot"))
    row = verified["evidence"]["repositories"][0]
    assert row["checks"]["push"] == "missing" and row["result"] == "failed"


def test_a_read_registration_needs_only_clone() -> None:
    record = tenant_token()
    notes = registration("swarm-bot/notes", can_push=False)
    pair = checks(record, notes, push=("missing", "role"))
    verified = step(run([record], [pair], [notes]), "access_verified")
    assert verified["state"] == "done"
    assert set(verified["evidence"]["repositories"][0]["checks"]) == {"clone"}


def test_a_clone_refusal_names_its_cause() -> None:
    record = tenant_token(sso_required_orgs=["example-org"])
    api = registration("example-org/api")
    pair = checks(record, api, clone=("missing", "not visible"))
    verified = step(run([record], [pair], [api]), "access_verified")
    assert verified["code"] == "SSO_NOT_AUTHORISED"
    plain = tenant_token()
    pair = checks(plain, api, clone=("missing", "not visible"))
    verified = step(run([plain], [pair], [api]), "access_verified")
    assert verified["code"] == "ACCOUNT_CANNOT_SEE"
    assert "cannot see this repository" in verified["copy"]


def test_a_pull_request_grant_github_does_not_expose_passes_on_the_role() -> None:
    record = user_token()
    api = registration("example-org/api")
    for reason in (FINE_GRAINED_UNKNOWN, APP_UNKNOWN):
        pair = checks(record, api, open_pull_requests=("unknown", reason))
        assert step(run([record], [pair], [api]), "access_verified")["state"] == "done"


def test_an_unanswered_check_is_forge_unreachable_not_failed() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    pair = checks(record, api, complete=False, error="example-org/api: GitHub answered HTTP 502",
                  push=("unknown", "not measured yet: GitHub answered HTTP 502"))
    verified = step(run([record], [pair], [api]), "access_verified")
    assert (verified["state"], verified["code"]) == ("in_progress", "FORGE_UNREACHABLE")
    assert verified["copy"] == onboarding.COPY["FORGE_UNREACHABLE"]


def test_checks_older_than_the_interval_are_stale() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    pair = checks(record, api, at=T0 - timedelta(days=4))
    assert step(run([record], [pair], [api]), "access_verified")["state"] == "stale"


def test_checks_of_another_token_or_tenant_are_not_this_callers() -> None:
    record = tenant_token()
    api = registration("example-org/api")
    other = checks(user_token("root@saga.xyz"), api)
    foreign = checks(record, api)
    foreign["tenant_id"] = "research"
    verified = step(run([record], [other, foreign], [api]), "access_verified")
    assert verified["state"] == "in_progress"
    assert verified["evidence"]["repositories"][0]["result"] == "pending"


# -- app_installed ------------------------------------------------------------------

INSTALL_URL = "https://github.com/apps/swarmcloud-saga/installations/new"


def app_token(**fields) -> GitTokenRecord:
    return user_token(kind="app_user", forge_login="example-user", **fields)


def test_a_token_connection_needs_no_installation() -> None:
    installed = step(run([tenant_token()]), "app_installed")
    assert installed["state"] == "done"
    assert installed["evidence"]["needed"] is False
    assert step(run([user_token()]), "app_installed")["evidence"]["needed"] is False


def test_an_app_connection_installed_nowhere_is_todo_with_the_install_page() -> None:
    found = {"read": True, "source": "github", "installed": [],
             "not_installed": ["example-user"], "install_url": INSTALL_URL}
    view = run([app_token()], installations=found)
    installed = step(view, "app_installed")
    assert installed["state"] == "todo" and installed["code"] is None
    assert installed["evidence"]["install_url"] == INSTALL_URL
    assert installed["evidence"]["login"] == "example-user"
    assert installed["evidence"]["not_installed"] == ["example-user"]
    assert view["next_step"] == "app_installed"
    # Nothing can be enabled or chosen before an installation exists.
    for name in ("orgs_enabled", "repos_chosen", "access_verified"):
        assert step(view, name)["evidence"] == {"waiting_for": "app_installed"}, name
    assert step(view, "ready")["evidence"] == {"waiting_for": "app_installed"}


def test_an_app_connection_installed_somewhere_is_done() -> None:
    found = {"read": True, "source": "github", "installed": ["example-user"],
             "not_installed": ["example-org"], "install_url": INSTALL_URL}
    view = run([app_token()], installations=found)
    assert states(view)["app_installed"] == "done"
    assert step(view, "orgs_enabled")["evidence"].get("waiting_for") is None
    assert view["next_step"] != "app_installed"


def test_an_unread_installation_list_is_in_progress_never_installed_nowhere() -> None:
    view = run([app_token()], installations=None)
    assert states(view)["app_installed"] == "in_progress"
    assert step(view, "orgs_enabled")["evidence"].get("waiting_for") is None
    unreachable = {"read": False, "unreachable": True, "install_url": INSTALL_URL}
    installed = step(run([app_token()], installations=unreachable), "app_installed")
    assert installed["state"] == "in_progress"
    assert installed["code"] == "FORGE_UNREACHABLE"
    assert installed["copy"] == onboarding.COPY["FORGE_UNREACHABLE"]
    busy = {"read": False, "unreachable": False, "error": "conflict"}
    assert step(run([app_token()], installations=busy), "app_installed")["code"] is None


def test_the_read_asks_for_installations_only_for_an_active_app_connection(db) -> None:
    seed_tenant(db, "eng", credentials=("git",))
    asked: list[int] = []

    def ask() -> dict:
        asked.append(1)
        return {"read": True, "installed": [], "install_url": INSTALL_URL}

    plain = tenant_token()
    db.docs[f"{COLLECTION}/{plain.token_id}"] = plain.to_firestore()
    tenant = onboarding.Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz",
                               credentials=["git"], created_at=T0)
    view = onboarding.read(db, ALICE, tenant=tenant, now=T0, installations=ask)
    assert asked == [] and states(view)["app_installed"] == "done"

    mine = app_token()
    db.docs[f"{COLLECTION}/{mine.token_id}"] = mine.to_firestore()
    view = onboarding.read(db, ALICE, tenant=tenant, now=T0, installations=ask)
    assert asked == [1] and states(view)["app_installed"] == "todo"


# -- the route ----------------------------------------------------------------------


def _seed_eng(db) -> GitTokenRecord:
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research")
    record = tenant_token(last4="Zq9w")
    db.docs[f"{COLLECTION}/{record.token_id}"] = record.to_firestore()
    root = user_token("root@saga.xyz")
    db.docs[f"{COLLECTION}/{root.token_id}"] = root.to_firestore()
    api = registration("example-org/api")
    db.docs[f"repositories/{api['repo_id']}"] = api
    pair = checks(record, api)
    db.docs[f"{CHECKS_COLLECTION}/{record.token_id}_{api['repo_id']}"] = pair
    return record


def test_the_route_serves_the_callers_checklist(db, client) -> None:
    _seed_eng(db)
    response = client.get("/v1/onboarding", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "eng" and body["user"] == "alice@saga.xyz"
    assert step(body, "github_connected")["evidence"]["via"] == "tenant"
    assert step(body, "repos_chosen")["state"] == "done"
    assert "root@saga.xyz" not in response.text
    # No field of the response is a value, or a part of one.
    assert "Zq9w" not in response.text


def test_the_route_is_read_only(db, client) -> None:
    _seed_eng(db)
    seed_tenant(db, "eng", credentials=("git",))
    before = copy.deepcopy(db.docs)
    assert client.get("/v1/onboarding", headers=auth_header("alice")).status_code == 200
    assert db.docs == before


def test_a_caller_sees_only_their_own_tenant(db, client) -> None:
    eng = _seed_eng(db)
    response = client.get("/v1/onboarding", headers=auth_header("bob"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenant_id"] == "research"
    assert states(body)["github_connected"] == "todo"
    assert states(body)["repos_chosen"] == "todo"
    assert eng.token_id not in response.text
    assert "example-org" not in response.text and "swarm-bot" not in response.text


def test_the_route_needs_a_signed_in_caller(client) -> None:
    assert client.get("/v1/onboarding").status_code == 401


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_nothing_but_a_read_is_served(db, client, method) -> None:
    _seed_eng(db)
    response = getattr(client, method)("/v1/onboarding", headers=auth_header("alice"))
    assert response.status_code == 405
