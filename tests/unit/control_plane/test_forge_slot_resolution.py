"""Every server-side forge read resolves its slot the way a task's would
(INDEX-USER-SLOT; part of #1041).

Owner report 2026-10-13: "Index now" in the personal tenant u-bogdan was
refused with "swarm-api may not read swarm-tenant-u-bogdan-git: its
per-secret accessor grant ... is missing". Measured the same day: no
`swarm-tenant-u-bogdan-git` exists; the tenant's only GitHub credential is
its owner's connection slot, `swarm-tenant-u-bogdan-git-u-<hex>`. swarm-api
read the tenant-wide `-git` slot unconditionally, and, holding no
project-wide `secrets.get`, got PermissionDenied for the missing secret, so
the message blamed a grant.

Held here, against the real `SecretManagerForgeTokens` and
`FirestoreSlotIndex` over the in-memory Firestore, with a fake Secret
Manager client that records every name it is asked for:

  * a personal tenant with only its owner's user slot reads THAT slot;
  * a tenant with its `-git` slot reads it, unchanged;
  * a tenant with neither is told to connect GitHub, not to fix a grant;
  * a person's own slot in the tenant is preferred when it exists;
  * "Index now" reads with the caller's slot, through the route;
  * no module outside the resolver reads the `-git` slot by name.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_api import forge
from swarm_api.gittokens import Scope, TokenState, provider_suffix, record_for_slot
from swarm_common.models import Tenant

from .conftest import auth_header
from .repo_fakes import make_client
from .repo_index_fakes import REPOSITORY, IndexGitHub, sha

PROJECT = "p"
OWNER = "bogdan@example.com"
NOW = datetime(2026, 10, 13, 12, 40, tzinfo=timezone.utc)


def _token() -> str:
    return "ghu_" + secrets.token_hex(18)


class _Payload:
    def __init__(self, data: bytes) -> None:
        self.data = data


class _Version:
    def __init__(self, name: str, data: bytes) -> None:
        self.name = name.replace("/latest", "/1")
        self.payload = _Payload(data)


class SecretsByName:
    """Secret Manager as swarm-api sees it: a secret it holds a value for
    answers; every other name answers PermissionDenied, because swarm-api has
    no project-wide `secrets.get` and cannot tell absent from not-granted."""

    def __init__(self, held: dict[str, str] | None = None) -> None:
        self.held = dict(held or {})
        self.names: list[str] = []

    def secret(self, name: str) -> str:
        return f"projects/{PROJECT}/secrets/{name}/versions/latest"

    def access_secret_version(self, request):
        from google.api_core import exceptions as gexc

        name = request["name"]
        self.names.append(name)
        for secret_id, value in self.held.items():
            if name == self.secret(secret_id):
                return _Version(name, value.encode())
        raise gexc.PermissionDenied("denied")


def _personal(tenant_id: str = "u-bogdan", owner: str = OWNER) -> Tenant:
    return Tenant(tenant_id=tenant_id, kind="user", principal=owner, created_at=NOW)


def _group(tenant_id: str = "eng", *, git: bool) -> Tenant:
    return Tenant(tenant_id=tenant_id, kind="group", principal=f"{tenant_id}@example.com",
                  created_at=NOW, credentials=["git"] if git else [])


def _user_record(db, tenant_id: str, email: str, *, state: TokenState = TokenState.ACTIVE):
    record = record_for_slot(tenant_id, Scope.USER, user=email, registered_by=email, now=NOW)
    doc = record.to_firestore()
    doc["state"] = state.value
    db.collection("git_tokens").document(record.token_id).set(doc)
    return record


def _reader(db, held: dict[str, str]) -> tuple[forge.SecretManagerForgeTokens, SecretsByName]:
    client = SecretsByName(held)
    return (forge.SecretManagerForgeTokens(PROJECT, client=client,
                                           index=forge.FirestoreSlotIndex(db)), client)


def _user_secret(tenant_id: str, email: str) -> str:
    return f"swarm-tenant-{tenant_id}-{provider_suffix(Scope.USER, user=email)}"


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------

def test_a_personal_tenant_with_only_its_owners_user_slot_reads_that_slot(db) -> None:
    from swarm_api.repoindex import read_head

    _user_record(db, "u-bogdan", OWNER)
    value = _token()
    reader, client = _reader(db, {_user_secret("u-bogdan", OWNER): value})
    github = IndexGitHub(heads={"main": sha("one")})
    record = {"owner": "example-org", "repo": "widgets", "default_branch": "main"}

    head = read_head(record, _personal(), tokens=reader, forge=forge.GitHubIssues(send=github))

    assert head == sha("one")
    assert client.names == [client.secret(_user_secret("u-bogdan", OWNER))]
    assert reader.resolve(_personal()).source == "owner"


def test_a_tenant_with_its_git_slot_reads_it_unchanged(db) -> None:
    value = _token()
    reader, client = _reader(db, {"swarm-tenant-eng-git": value})
    assert reader.token_for(_group(git=True), person="carol@example.com") == value
    assert client.names == [client.secret("swarm-tenant-eng-git")]


def test_a_tenant_whose_default_record_exists_reads_its_git_slot(db) -> None:
    record = record_for_slot("eng", Scope.TENANT, registered_by="swarm-api", now=NOW)
    db.collection("git_tokens").document(record.token_id).set(record.to_firestore())
    reader, _client = _reader(db, {})
    slot = reader.resolve(_group(git=False))
    assert (slot.provider, slot.known) == ("git", True)


def test_neither_slot_says_connect_github_not_a_missing_grant(db) -> None:
    reader, client = _reader(db, {})
    with pytest.raises(forge.NoForgeCredential) as raised:
        reader.token_for(_personal(), person=OWNER)
    text = str(raised.value)
    assert raised.value.code == "no_forge_credential"
    assert text.startswith("No GitHub credential for u-bogdan: connect GitHub under "
                           "Setup › Git tokens")
    assert "swarm-tenant-u-bogdan-git" in text
    assert "grant" not in text and "terraform" not in text
    assert client.names == [client.secret("swarm-tenant-u-bogdan-git")]


def test_a_known_git_slot_that_is_refused_still_names_its_grant(db) -> None:
    reader, _client = _reader(db, {})
    with pytest.raises(forge.IssueReadFailed) as raised:
        reader.token_for(_group(git=True))
    assert "terraform/modules/secret_manager" in str(raised.value)


def test_a_persons_own_slot_is_preferred_when_present(db) -> None:
    _user_record(db, "eng", "alice@example.com")
    mine, tenants = _token(), _token()
    reader, client = _reader(db, {_user_secret("eng", "alice@example.com"): mine,
                                  "swarm-tenant-eng-git": tenants})
    assert reader.token_for(_group(git=True), person="alice@example.com") == mine
    assert reader.token_for(_group(git=True), person="carol@example.com") == tenants
    assert reader.token_for(_group(git=True)) == tenants
    assert client.names == [client.secret(_user_secret("eng", "alice@example.com")),
                            client.secret("swarm-tenant-eng-git"),
                            client.secret("swarm-tenant-eng-git")]


def test_a_revoked_user_slot_is_not_resolved(db) -> None:
    _user_record(db, "u-bogdan", OWNER, state=TokenState.REVOKED)
    reader, _client = _reader(db, {})
    slot = reader.resolve(_personal(), person=OWNER)
    assert (slot.provider, slot.known) == ("git", False)


def test_another_tenants_record_does_not_resolve_a_slot_here(db) -> None:
    # The record's id is a hash of ITS tenant; a document under this
    # tenant's id that says another tenant is no record (invariant 9).
    record = _user_record(db, "u-bogdan", OWNER)
    other = record_for_slot("u-mallory", Scope.USER, user=OWNER, registered_by=OWNER, now=NOW)
    doc = dict(db.docs[f"git_tokens/{record.token_id}"])
    doc["tenant_id"] = "u-bogdan"
    db.collection("git_tokens").document(other.token_id).set(doc)
    reader, _client = _reader(db, {})
    assert reader.resolve(_personal("u-mallory")).known is False


# --------------------------------------------------------------------------
# through the route
# --------------------------------------------------------------------------

def test_index_now_reads_with_the_callers_own_slot(db, tokens, group_map, objects) -> None:
    _user_record(db, "eng", "alice@example.com")
    reader, client_secrets = _reader(db, {
        "swarm-tenant-eng-git": _token(),
        _user_secret("eng", "alice@example.com"): _token(),
    })
    client = make_client(db, tokens, group_map, objects, forge_tokens=reader,
                         transport=IndexGitHub(heads={"main": sha("one")}))
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    client_secrets.names.clear()

    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))

    assert started.status_code == 202, started.text
    assert client_secrets.secret(_user_secret("eng", "alice@example.com")) in client_secrets.names
    assert client_secrets.secret("swarm-tenant-eng-git") not in client_secrets.names


# --------------------------------------------------------------------------
# one resolver
# --------------------------------------------------------------------------

API = Path(__file__).resolve().parents[3] / "apps" / "swarm-api" / "swarm_api"

#: A slot read that names the tenant `-git` slot itself, bypassing the resolver.
_DIRECT_GIT_READ = re.compile(
    r"read_slot\(\s*[^,()]+,\s*(?:_?forge\.)?(?:GIT_PROVIDER|[\"']git[\"'])\s*[,)]")


def test_no_module_reads_the_tenant_git_slot_outside_the_resolver() -> None:
    # The control: the pattern does find the read this lane removed.
    assert _DIRECT_GIT_READ.search("self.read_slot(tenant, GIT_PROVIDER).value")
    assert _DIRECT_GIT_READ.search('tokens.read_slot(tenant, "git")')
    files = sorted(API.rglob("*.py"))
    assert len(files) > 50, f"found only {len(files)} modules under {API}"
    offenders = [
        f"{path.relative_to(API)}:{text[:match.start()].count(chr(10)) + 1}"
        for path in files
        for text in [path.read_text(encoding="utf-8")]
        for match in _DIRECT_GIT_READ.finditer(text)
    ]
    assert offenders == []


def test_the_production_reader_has_a_slot_index() -> None:
    text = (API / "deps.py").read_text(encoding="utf-8")
    assert re.search(r"SecretManagerForgeTokens\(\s*settings\.project_id,\s*"
                     r"index=FirestoreSlotIndex\(db\)\)", text)


@pytest.mark.parametrize("module, needle", [
    ("routes/issues.py", "person=auth.email"),
    ("routes/runs.py", "person=auth.email"),
    ("routes/repositories.py", "person=auth.email"),
    ("repoindex.py", "person=auth.email"),
    ("service.py", "person=ctx.email"),
])
def test_every_request_read_names_who_asked(module: str, needle: str) -> None:
    assert needle in (API / module).read_text(encoding="utf-8")
