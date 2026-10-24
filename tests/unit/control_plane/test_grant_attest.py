"""Workspace-secret grant attestation (SEC-GRANT-ATTEST, part of #1347).

Design: docs/design/workspace-secrets.md, "What SEC-GRANT-ATTEST built";
owner decision 2026-10-15. swarm-api MACs every grant it writes under
`swarm-workspace-grant-key`, and the release drops a grant whose MAC does not
verify, so an agent that can write Firestore cannot widen what is released.

Drives the real release route over the in-memory Firestore, exactly as
test_secret_release.py does (its helpers are imported), with grants written by
the store's own `set_grant` and then rewritten the way an agent in the pod can
rewrite them: directly in Firestore.

Every key is built at runtime (`secrets.token_urlsafe`), never a literal.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.grant_attest import (
    ATTESTATION_FIELD,
    INVALID,
    MISSING,
    VALID,
    GrantKeys,
    GrantPolicy,
    canonical,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_api.workspace_secrets import (
    AUDIT_GRANT_ATTESTED,
    GRANTS_COLLECTION,
    WorkspaceSecrets,
)

from .conftest import auth_header, seed_tenant
from .test_child_tasks_api import Attempt, worker_tokens  # noqa: F401  (a fixture)
from .test_secret_release import (
    ROOT,
    FakeSecretManager,
    _settings,
    _tenant,
    fake_value,
    names,
    release,
    running,
)

REPO = Path(__file__).resolve().parents[3]
ATTEST_ROUTE = "/v1/admin/workspace-secrets/attest-grants"


def new_key() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def sm() -> FakeSecretManager:
    return FakeSecretManager()


@pytest.fixture
def keys() -> dict[str, str]:
    return {"one": new_key(), "two": new_key(), "three": new_key()}


def store(db, sm, key: str = "", previous: str = "", required: bool = False) -> WorkspaceSecrets:
    return WorkspaceSecrets(db, sm, now=lambda: datetime.now(timezone.utc),
                            grants=GrantPolicy(GrantKeys(key, previous), required))


def make_client(db, worker_tokens, group_map, objects, sm, **extra: Any) -> TestClient:  # noqa: F811
    ctx = build_context(
        settings=_settings(**extra), db=db, verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
    )
    application = create_app(ctx)
    application.state.workspace_secret_reader = sm
    application.state.workspace_secret_writer = sm
    return TestClient(application, raise_server_exceptions=False)


@pytest.fixture
def world(db, worker_tokens, group_map, objects, sm, keys):  # noqa: F811
    """eng with two secrets, `a-key` and `b-key`, written under key `one`.
    Returns a client factory taking the release's settings."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    writer = store(db, sm, keys["one"])
    for name in ("a-key", "b-key"):
        writer.create(tenant=_tenant(db, "eng"), name=name, provider="other", kind="api_key",
                      payload=fake_value(name[0]).encode(), by=ROOT, via_link=False)

    def client(**extra: Any) -> TestClient:
        return make_client(db, worker_tokens, group_map, objects, sm, **extra)

    return client


def capture(caplog, level: int = logging.DEBUG) -> None:
    """caplog, after `create_app` has replaced the root handlers
    (swarm_common.logging_setup) during this test's call phase."""
    root = logging.getLogger()
    if caplog.handler not in root.handlers:
        root.addHandler(caplog.handler)
    caplog.set_level(level)


def grant_doc(db, secret_id: str, consumer: str) -> dict[str, Any]:
    return db.docs[f"{GRANTS_COLLECTION}/{secret_id}~{consumer}"]


def sid(db, sm, name: str) -> str:
    return store(db, sm).find("eng", name)["secret_id"]


# --------------------------------------------------------------------------
# the canonical form and the ring
# --------------------------------------------------------------------------


def test_the_canonical_form_is_a_versioned_fixed_order_array():
    assert canonical("s", "t", "c", None, True) == b'["v1","s","t","c",null,true]'
    assert canonical("s", "t", "c", "files", False) == b'["v1","s","t","c","files",false]'
    for bad in [(1, "t", "c", None, True), ("s", "t", "c", None, 1), ("s", "", "c", None, True),
                ("s", "t", "c", 7, True), ("s", "t", "c", "", True)]:
        with pytest.raises(ValueError):
            canonical(*bad)


def test_the_previous_ring_key_verifies_and_a_third_does_not(keys):
    grant = {"secret_id": "s", "tenant_id": "eng", "consumer": "profile:claude-code",
             "slot": "files", "enabled": True}
    grant[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(grant)
    assert GrantKeys(keys["one"]).check(grant) == VALID
    assert GrantKeys(keys["two"], keys["one"]).check(grant) == VALID
    assert GrantKeys(keys["three"], keys["two"]).check(grant) == INVALID
    assert GrantKeys(keys["three"]).check(grant) == INVALID
    # No key at all verifies nothing.
    assert GrantKeys().check(grant) == INVALID
    assert GrantKeys(keys["one"]).check({**grant, ATTESTATION_FIELD: None}) == MISSING


@pytest.mark.parametrize("presented", [
    "", "v1", "v1.", "v2.AAAA", "v1.!!!!", "v1." + "A" * 42, "v1." + "A" * 44, 7, ["v1.x"],
    "v1.a.b",
])
def test_a_malformed_attestation_is_invalid_never_missing(keys, presented):
    grant = {"secret_id": "s", "tenant_id": "eng", "consumer": "profile:claude-code",
             "slot": "files", "enabled": True, ATTESTATION_FIELD: presented}
    assert GrantKeys(keys["one"]).check(grant) == INVALID


def test_the_comparison_is_constant_time():
    source = (REPO / "apps/swarm-api/swarm_api/grant_attest.py").read_text()
    assert "hmac.compare_digest" in source
    assert not re.search(r"_mac\([^)]*\)\s*==|==\s*_mac\(", source)


def test_the_canonical_form_is_stated_once():
    """No other module builds the grant's MAC input: no other file names the
    grant key's setting, makes an HMAC over a grant, or spells the form."""
    root = REPO / "apps/swarm-api/swarm_api"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "grant_attest.py":
            continue
        text = path.read_text()
        if re.search(r"workspace_grant_key|FORM_VERSION|canonical(_of)?\([^)]*consumer"
                     r"|\[\s*[\"']v1[\"']\s*,[^\]]*secret_id", text):
            offenders.append(str(path.relative_to(REPO)))
    # settings.py declares the key's fields; it builds nothing from them.
    assert offenders == ["apps/swarm-api/swarm_api/settings.py"], offenders
    settings_text = (root / "settings.py").read_text()
    assert "hmac" not in settings_text and "canonical" not in settings_text


# --------------------------------------------------------------------------
# attest on write
# --------------------------------------------------------------------------


def test_a_grant_written_through_set_grant_carries_an_attestation_that_verifies(db, sm, world,
                                                                                keys):
    writer = store(db, sm, keys["one"])
    writer.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=True, by=ROOT)
    doc = grant_doc(db, sid(db, sm, "a-key"), "profile:claude-code")
    assert doc[ATTESTATION_FIELD].startswith("v1.")
    assert GrantKeys(keys["one"]).check(doc) == VALID
    # Unticked: re-signed for enabled=false, so the earlier MAC is gone.
    before = doc[ATTESTATION_FIELD]
    writer.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=False, by=ROOT)
    doc = grant_doc(db, sid(db, sm, "a-key"), "profile:claude-code")
    assert doc[ATTESTATION_FIELD] != before and GrantKeys(keys["one"]).check(doc) == VALID
    # The API serves no attestation.
    listed = writer.list("eng")
    assert ATTESTATION_FIELD not in json.dumps(listed, default=str)


def test_delete_re_signs_its_disabled_grants(db, sm, world, keys):
    writer = store(db, sm, keys["one"])
    writer.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=True, by=ROOT)
    secret_id = sid(db, sm, "a-key")
    writer.delete(tenant_id="eng", name="a-key", by=ROOT)
    doc = grant_doc(db, secret_id, "profile:claude-code")
    assert doc["enabled"] is False and GrantKeys(keys["one"]).check(doc) == VALID


def test_with_no_key_a_grant_is_written_unattested_and_never_with_a_stale_mac(db, sm, world,
                                                                               keys):
    store(db, sm, keys["one"]).set_grant(tenant_id="eng", name="a-key",
                                         consumer="profile:claude-code", slot=None,
                                         enabled=True, by=ROOT)
    store(db, sm).set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code",
                            slot=None, enabled=False, by=ROOT)
    doc = grant_doc(db, sid(db, sm, "a-key"), "profile:claude-code")
    assert doc[ATTESTATION_FIELD] is None


def test_required_without_a_key_refuses_to_enable_a_grant(db, sm, world):
    from swarm_api.errors import UpstreamUnavailable

    with pytest.raises(UpstreamUnavailable):
        store(db, sm, required=True).set_grant(tenant_id="eng", name="a-key",
                                               consumer="profile:claude-code", slot=None,
                                               enabled=True, by=ROOT)
    assert not [p for p in db.docs if p.startswith(f"{GRANTS_COLLECTION}/")]


# --------------------------------------------------------------------------
# verify on release: what an agent in the pod can write
# --------------------------------------------------------------------------


def _forge(db, sm, keys, how: str) -> None:
    """eng's `a-key` is legitimately granted to profile:claude-code. Then the
    agent writes Firestore directly to get `b-key` released too."""
    writer = store(db, sm, keys["one"])
    writer.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=True, by=ROOT)
    a_id, b_id = sid(db, sm, "a-key"), sid(db, sm, "b-key")
    a_grant = grant_doc(db, a_id, "profile:claude-code")
    forged = {"secret_id": b_id, "tenant_id": "eng", "consumer": "profile:claude-code",
              "slot": "files", "enabled": True}
    path = f"{GRANTS_COLLECTION}/{b_id}~profile:claude-code"
    if how == "no_attestation":
        db.docs[path] = forged
    elif how == "garbage_attestation":
        db.docs[path] = {**forged, ATTESTATION_FIELD: "v1." + "A" * 43}
    elif how == "copied_from_another_grant":
        db.docs[path] = {**forged, ATTESTATION_FIELD: a_grant[ATTESTATION_FIELD]}
    elif how == "enabled_flipped":
        writer.set_grant(tenant_id="eng", name="b-key", consumer="profile:claude-code",
                         slot=None, enabled=False, by=ROOT)
        db.docs[path]["enabled"] = True
    elif how == "consumer_changed":
        # Granted to the review profile, which also takes a `files` slot.
        writer.set_grant(tenant_id="eng", name="b-key", consumer="profile:claude-code-review",
                         slot=None, enabled=True, by=ROOT)
        moved = dict(db.docs[f"{GRANTS_COLLECTION}/{b_id}~profile:claude-code-review"])
        db.docs[path] = {**moved, "consumer": "profile:claude-code"}
    elif how == "secret_id_changed":
        # a-key's own grant document, re-pointed at b-key: neither is released.
        a_grant["secret_id"] = b_id
    else:
        raise AssertionError(how)


FORGERIES = ["no_attestation", "garbage_attestation", "copied_from_another_grant",
             "enabled_flipped", "consumer_changed", "secret_id_changed"]


@pytest.mark.parametrize("how", FORGERIES)
def test_a_forged_grant_is_not_released_when_attestation_is_required(db, sm, world, keys, how,
                                                                     caplog):
    _forge(db, sm, keys, how)
    client = world(workspace_grant_key=keys["one"], grant_attestation_required=True)
    capture(caplog, logging.WARNING)
    released = names(release(client, running(client, db)))
    assert "b-key" not in released
    assert released == ([] if how == "secret_id_changed" else ["a-key"])
    assert any(INVALID in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("how", [h for h in FORGERIES if h != "no_attestation"])
def test_a_wrong_attestation_is_dropped_even_while_not_required(db, sm, world, keys, how):
    _forge(db, sm, keys, how)
    client = world(workspace_grant_key=keys["one"], grant_attestation_required=False)
    released = names(release(client, running(client, db)))
    assert "b-key" not in released


def test_an_unattested_grant_is_released_with_a_warning_while_not_required(db, sm, world, keys,
                                                                           caplog):
    _forge(db, sm, keys, "no_attestation")
    client = world(workspace_grant_key=keys["one"])
    capture(caplog, logging.WARNING)
    assert names(release(client, running(client, db))) == ["a-key", "b-key"]
    [line] = [r.getMessage() for r in caplog.records if MISSING in r.getMessage()]
    assert sid(db, sm, "b-key") in line and "'eng'" in line


def test_the_release_accepts_the_previous_ring_key_and_not_a_third(db, sm, world, keys):
    store(db, sm, keys["one"]).set_grant(tenant_id="eng", name="a-key",
                                         consumer="profile:claude-code", slot=None,
                                         enabled=True, by=ROOT)
    rotated = world(workspace_grant_key=keys["two"], workspace_grant_key_previous=keys["one"],
                    grant_attestation_required=True)
    assert names(release(rotated, running(rotated, db))) == ["a-key"]
    later = world(workspace_grant_key=keys["three"], workspace_grant_key_previous=keys["two"],
                  grant_attestation_required=True)
    second = Attempt(task="task_second", attempt="att_second_1", lease="lease_second")
    assert names(release(later, running(later, db, second))) == []


# --------------------------------------------------------------------------
# no key, no MAC anywhere it should not be
# --------------------------------------------------------------------------


def test_no_log_response_or_document_carries_the_key(db, sm, world, keys, caplog):
    writer = store(db, sm, keys["one"])
    writer.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=True, by=ROOT)
    _forge(db, sm, keys, "copied_from_another_grant")
    client = world(workspace_grant_key=keys["one"], workspace_grant_key_previous=keys["two"])
    capture(caplog)
    answers = [release(client, running(client, db)),
               client.post(ATTEST_ROUTE, headers=auth_header("root")),
               client.get("/v1/workspace/secrets", headers=auth_header("root"))]
    macs = {d[ATTESTATION_FIELD] for p, d in db.docs.items()
            if p.startswith(f"{GRANTS_COLLECTION}/") and d.get(ATTESTATION_FIELD)}
    assert macs
    text = "\n".join(r.getMessage() for r in caplog.records)
    for answer in answers:
        assert answer.status_code == 200, answer.text
        text += "\n" + answer.text
    stored = json.dumps(db.docs, default=str)
    for key in keys.values():
        assert key not in text and key not in stored
    for mac in macs:
        assert mac not in text
        assert mac.split(".")[1] not in text
    # The settings object's repr carries neither ring key.
    settings = _settings(
        workspace_grant_key=keys["one"], workspace_grant_key_previous=keys["two"])
    assert keys["one"] not in repr(settings) and keys["two"] not in repr(settings)


# --------------------------------------------------------------------------
# the migration route
# --------------------------------------------------------------------------


def test_the_attest_route_signs_only_audit_matching_grants_and_lists_the_rest(db, sm, world,
                                                                              keys):
    # Three grants written BEFORE this lane: no key, so unattested, each with
    # set_grant's audit row.
    legacy = store(db, sm)
    legacy.set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code", slot=None,
                     enabled=True, by=ROOT)
    legacy.set_grant(tenant_id="eng", name="b-key", consumer="profile:claude-code-review",
                     slot=None, enabled=True, by=ROOT)
    a_id, b_id = sid(db, sm, "a-key"), sid(db, sm, "b-key")
    # Changed since its audited write: its consumer.
    changed = grant_doc(db, b_id, "profile:claude-code-review")
    changed["consumer"] = "profile:claude-code"
    # Never audited at all: written straight into Firestore.
    db.docs[f"{GRANTS_COLLECTION}/{b_id}~profile:claude-code"] = {
        "secret_id": b_id, "tenant_id": "eng", "consumer": "profile:claude-code",
        "slot": "files", "enabled": True}

    client = world(workspace_grant_key=keys["one"])
    assert client.post(ATTEST_ROUTE, headers=auth_header("alice")).status_code == 403
    answer = client.post(ATTEST_ROUTE, headers=auth_header("root"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["signed"] == [f"{a_id}~profile:claude-code"]
    assert body["unsigned"] == sorted([f"{b_id}~profile:claude-code",
                                       f"{b_id}~profile:claude-code-review"])
    assert body["examined"] == 3 and body["already_attested"] == 0
    assert GrantKeys(keys["one"]).check(grant_doc(db, a_id, "profile:claude-code")) == VALID
    assert ATTESTATION_FIELD not in grant_doc(db, b_id, "profile:claude-code")
    assert any(d.get("action") == AUDIT_GRANT_ATTESTED for p, d in db.docs.items()
               if p.startswith("admin_audit/"))

    # Run again: nothing more to sign.
    again = client.post(ATTEST_ROUTE, headers=auth_header("root")).json()
    assert again["signed"] == [] and again["already_attested"] == 1

    # And once required, only the signed grant is released.
    required = world(workspace_grant_key=keys["one"], grant_attestation_required=True)
    assert names(release(required, running(required, db))) == ["a-key"]


def test_the_attest_route_re_signs_a_previous_key_grant_under_the_current(db, sm, world, keys):
    store(db, sm, keys["one"]).set_grant(tenant_id="eng", name="a-key",
                                         consumer="profile:claude-code", slot=None,
                                         enabled=True, by=ROOT)
    client = world(workspace_grant_key=keys["two"], workspace_grant_key_previous=keys["one"])
    body = client.post(ATTEST_ROUTE, headers=auth_header("root")).json()
    assert body["signed"] == [f"{sid(db, sm, 'a-key')}~profile:claude-code"]
    doc = grant_doc(db, sid(db, sm, "a-key"), "profile:claude-code")
    assert GrantKeys(keys["two"]).check(doc) == VALID


def test_the_attest_route_without_a_key_writes_nothing(db, sm, world):
    store(db, sm).set_grant(tenant_id="eng", name="a-key", consumer="profile:claude-code",
                            slot=None, enabled=True, by=ROOT)
    before = json.dumps(db.docs, default=str, sort_keys=True)
    answer = world().post(ATTEST_ROUTE, headers=auth_header("root"))
    assert answer.status_code == 503, answer.text
    assert json.dumps(db.docs, default=str, sort_keys=True) == before


# --------------------------------------------------------------------------
# SSC-REPO-SCOPE: form "v2" binds a repository secret's grant to its
# repository (docs/design/secrets-scopes.md §2.2, owner decision Q9)
# --------------------------------------------------------------------------

from swarm_api.grant_attest import attested_form  # noqa: E402
from swarm_api.repositories import repo_id_for  # noqa: E402
from swarm_api.workspace_secrets import SECRETS_COLLECTION  # noqa: E402


def test_form_v2_ends_with_the_repository_and_v1_is_unchanged():
    assert canonical("s", "t", "c", None, True, "repo_0123456789abcdef") == \
        b'["v2","s","t","c",null,true,"repo_0123456789abcdef"]'
    assert canonical("s", "t", "c", None, True) == b'["v1","s","t","c",null,true]'
    for bad in ("", 7):
        with pytest.raises(ValueError):
            canonical("s", "t", "c", None, True, bad)


def test_a_v1_grant_still_verifies_and_a_repository_grant_is_signed_v2(keys):
    plain = {"secret_id": "s", "tenant_id": "eng", "consumer": "profile:claude-code",
             "slot": "files", "enabled": True}
    plain[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(plain)
    assert attested_form(plain) == "v1"
    assert GrantKeys(keys["one"]).check(plain) == VALID

    bound = {**plain, "repo_id": "repo_0123456789abcdef"}
    bound[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(bound)
    assert attested_form(bound) == "v2"
    assert GrantKeys(keys["one"]).check(bound) == VALID


@pytest.mark.parametrize("rewrite", ["other_repo", "strip_repo", "relabel_v1", "drop_mac"])
def test_a_repository_grants_binding_cannot_be_rewritten(keys, rewrite):
    grant = {"secret_id": "s", "tenant_id": "eng", "consumer": "profile:claude-code",
             "slot": "files", "enabled": True, "repo_id": "repo_0123456789abcdef"}
    grant[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(grant)
    if rewrite == "other_repo":
        grant["repo_id"] = "repo_fedcba9876543210"
    elif rewrite == "strip_repo":
        del grant["repo_id"]
    elif rewrite == "relabel_v1":
        grant[ATTESTATION_FIELD] = "v1." + grant[ATTESTATION_FIELD].split(".")[1]
    else:
        grant[ATTESTATION_FIELD] = None
    assert GrantKeys(keys["one"]).check(grant) != VALID


def _repo_world(db, sm, keys) -> dict[str, Any]:
    """eng's registration of acme/widgets and its secret `widgets-db`,
    granted to the profile and signed under key `one`."""
    repo_id = repo_id_for("eng", "acme", "widgets")
    registration = {"repo_id": repo_id, "tenant_id": "eng", "owner": "acme",
                    "repo": "widgets", "repository_url": "https://github.com/acme/widgets"}
    db.docs[f"repositories/{repo_id}"] = registration
    writer = store(db, sm, keys["one"])
    writer.create(tenant=_tenant(db, "eng"), name="widgets-db", provider="other",
                  kind="api_key", payload=fake_value("r").encode(), by=ROOT, via_link=False,
                  repository=registration)
    writer.set_grant(tenant_id="eng", name="widgets-db", consumer="profile:claude-code",
                     slot=None, enabled=True, by=ROOT, repo_id=repo_id)
    return registration


def _repo_grant(db, sm, repo_id: str) -> dict[str, Any]:
    secret_id = store(db, sm).find("eng", "widgets-db", repo_id)["secret_id"]
    return grant_doc(db, secret_id, "profile:claude-code")


def test_a_repository_grant_written_by_set_grant_is_bound_and_released(db, sm, world, keys):
    registration = _repo_world(db, sm, keys)
    grant = _repo_grant(db, sm, registration["repo_id"])
    assert grant["repo_id"] == registration["repo_id"]
    assert attested_form(grant) == "v2" and GrantKeys(keys["one"]).check(grant) == VALID
    client = world(workspace_grant_key=keys["one"])
    attempt = running(client, db, repository_url=registration["repository_url"])
    assert names(release(client, attempt)) == ["widgets-db"]


def test_an_unattested_repository_grant_is_dropped_with_attestation_not_required(
        db, sm, world, keys):
    """grant_attestation_required is off, which releases an unattested
    WORKSPACE grant; a repository grant is never released unattested."""
    registration = _repo_world(db, sm, keys)
    _repo_grant(db, sm, registration["repo_id"])[ATTESTATION_FIELD] = None
    client = world(workspace_grant_key=keys["one"], grant_attestation_required=False)
    attempt = running(client, db, repository_url=registration["repository_url"])
    assert names(release(client, attempt)) == []


def test_a_repository_grant_relabelled_v1_is_dropped(db, sm, world, keys):
    registration = _repo_world(db, sm, keys)
    grant = _repo_grant(db, sm, registration["repo_id"])
    # A v1 MAC over the same fields, which omits repo_id: as an agent holding
    # an old v1 grant on this secret could present it.
    grant[ATTESTATION_FIELD] = GrantKeys(keys["one"]).sign(
        {k: v for k, v in grant.items() if k != "repo_id"})
    assert attested_form(grant) == "v1" and GrantKeys(keys["one"]).check(grant) == VALID
    client = world(workspace_grant_key=keys["one"])
    attempt = running(client, db, repository_url=registration["repository_url"])
    assert names(release(client, attempt)) == []


@pytest.mark.parametrize("rewrite", ["to_sibling", "to_workspace", "drop_repo_id"])
def test_a_record_whose_repo_id_was_rewritten_is_dropped(db, sm, world, keys, rewrite):
    """The agent rewrites the record (unattested) to widen the secret to an
    attempt on a sibling repository, or to make it look like a workspace
    secret: the signed grant still names acme/widgets, so nothing is released."""
    registration = _repo_world(db, sm, keys)
    sibling = repo_id_for("eng", "acme", "gadgets")
    db.docs[f"repositories/{sibling}"] = {**registration, "repo_id": sibling, "repo": "gadgets",
                                          "repository_url": "https://github.com/acme/gadgets"}
    secret_id = store(db, sm).find("eng", "widgets-db", registration["repo_id"])["secret_id"]
    record = db.docs[f"{SECRETS_COLLECTION}/{secret_id}"]
    if rewrite == "to_sibling":
        record["repo_id"] = sibling
    elif rewrite == "to_workspace":
        record["scope"] = "workspace"
        record["repo_id"] = None
    else:
        del record["repo_id"]
    client = world(workspace_grant_key=keys["one"])
    attempt = running(client, db, repository_url="https://github.com/acme/gadgets")
    assert names(release(client, attempt)) == []
    if rewrite == "to_workspace":
        # Nor on its own repository: the grant's binding is not the record's.
        again = running(client, db, Attempt(task="task-two", attempt="att-two", lease="lease-two"),
                        repository_url=registration["repository_url"])
        assert names(release(client, again)) == []


def test_a_repository_secret_rewritten_to_look_like_the_workspaces_is_not_released(
        db, sm, world, keys):
    """The review's attack: with grant_attestation_required OFF (which releases
    an unattested workspace grant), an agent strips `scope` and `repo_id` from
    the record and writes itself a fresh unattested grant with no repository.
    The secret's id still says it is a repository secret, so neither the
    release nor any other reader takes it as the workspace's."""
    registration = _repo_world(db, sm, keys)
    secret_id = store(db, sm).find("eng", "widgets-db", registration["repo_id"])["secret_id"]
    record = db.docs[f"{SECRETS_COLLECTION}/{secret_id}"]
    record.pop("repo_id")
    record["scope"] = "workspace"
    record.pop("repository")
    db.docs[f"{GRANTS_COLLECTION}/{secret_id}~profile:claude-code"] = {
        "secret_id": secret_id, "tenant_id": "eng", "consumer": "profile:claude-code",
        "slot": "files", "enabled": True}
    client = world(workspace_grant_key=keys["one"], grant_attestation_required=False)
    for n, url in enumerate(("https://github.com/acme/gadgets", registration["repository_url"])):
        attempt = running(client, db, Attempt(task=f"task-{n}", attempt=f"att-{n}",
                                              lease=f"lease-{n}"), repository_url=url)
        assert names(release(client, attempt)) == []


def test_a_repository_secrets_id_says_what_it_is_and_a_workspace_ones_never_does():
    from swarm_api.workspace_secrets import (
        is_repository_secret_id,
        repository_hex,
        workspace_hex,
    )
    for _ in range(200):
        assert is_repository_secret_id(f"swarm-tenant-eng-wsec-{repository_hex()}")
        assert not is_repository_secret_id(f"swarm-tenant-eng-wsec-{workspace_hex()}")
