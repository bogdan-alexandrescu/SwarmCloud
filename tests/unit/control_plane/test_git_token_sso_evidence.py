"""What a token reaches, and why a registration it cannot make is refused
(docs/onboarding.md §0 and §2.3; #780, lane OB0b).

What is held here:

  * the probe records GitHub's `X-GitHub-SSO` answer: `required` with the org
    its URL names, `partial-results` with the org ids it lists -- and never
    the URL's `authorization_request` part;
  * the probe records the orgs the token reaches (`GET /user/orgs`, paged),
    logins only;
  * the probe records an org that refuses classic personal access tokens;
  * all three are stored on the token record, additively, and served;
  * a registration refused 404/403 names the likely cause from that
    evidence: SSO not authorised for the org, the org restricting classic
    tokens, or the token's account not seeing the repository.

The forge is a fake transport; nothing reaches a network. Every
token-shaped value is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.gittokens import (
    COLLECTION,
    MAX_ORG_PAGES,
    GitTokens,
    Scope,
    parse_sso_header,
    probe_token,
    record_for_slot,
    refusal_cause,
    repo_id_for,
    token_id_for,
)

from .conftest import auth_header, seed_tenant
from .repo_fakes import GitHubRepos, TenantTokens, make_client
from .test_git_token_probe import ProbeForge, Slots, classic_value, fine_grained_value

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
OWNER_REPO = "saga-xyz/widgets"
REPO = repo_id_for("eng", OWNER_REPO)

#: GitHub's own sentence for an org that forbids classic tokens.
CLASSIC_MESSAGE = ("`saga-xyz` forbids access via a personal access token (classic). Please use "
                   "a GitHub App, OAuth App, or a personal access token with fine-grained "
                   "permissions.")


def sso_required(org: str) -> tuple[str, str]:
    """The header GitHub sends for an SSO-protected resource, and its request part."""
    request = secrets.token_hex(16)
    return (f"required; url=https://github.com/orgs/{org}/sso?authorization_request={request}",
            request)


def run(value: str, fake: ProbeForge):
    return probe_token(value, [(REPO, OWNER_REPO)], send=fake, now=T0)


# -- the header ------------------------------------------------------------------


def test_the_sso_header_is_parsed_without_its_request_part() -> None:
    header, request = sso_required("saga-xyz")
    parsed = parse_sso_header(header)
    assert parsed == {"mode": "required", "org": "saga-xyz", "organization_ids": []}
    assert request not in json.dumps(parsed)
    assert parse_sso_header("partial-results; organizations=21955855,20582480") == {
        "mode": "partial", "org": None, "organization_ids": [20582480, 21955855]}
    for junk in (None, "", "nonsense", "required; url=https://evil.example/orgs/x/sso"):
        parsed = parse_sso_header(junk)
        assert parsed is None or parsed["org"] is None, junk


# -- the probe records it ----------------------------------------------------------


def test_the_probe_records_an_sso_required_answer_and_never_its_url() -> None:
    header, request = sso_required("saga-xyz")
    fake = ProbeForge(status={"repo": 403}, headers={"repo": {"x-github-sso": header}})
    result = run(classic_value(), fake)
    assert result.sso_required_orgs == ["saga-xyz"]
    assert request not in repr(result)


def test_the_probe_records_partial_sso_org_ids() -> None:
    fake = ProbeForge(orgs=["saga-xyz"],
                      headers={"orgs": {"x-github-sso": "partial-results; organizations=7,3"}})
    result = run(fine_grained_value(), fake)
    assert result.sso_partial_org_ids == [3, 7]
    assert result.sso_required_orgs == []


def test_the_probe_records_the_orgs_the_token_reaches_paged_logins_only() -> None:
    logins = [f"org-{i:03d}" for i in range(103)]
    fake = ProbeForge(orgs=logins)
    result = run(classic_value(), fake)
    assert result.orgs == logins
    assert all(isinstance(login, str) for login in result.orgs)
    assert fake.keys().count("orgs") == 2
    # A cap: a token in more orgs than the pages allow still costs a bounded read.
    many = ProbeForge(orgs=[f"o{i}" for i in range(100 * MAX_ORG_PAGES + 5)])
    capped = run(classic_value(), many)
    assert many.keys().count("orgs") == MAX_ORG_PAGES
    assert len(capped.orgs) == 100 * MAX_ORG_PAGES and capped.orgs_capped


def test_an_unanswered_org_read_is_not_an_empty_list() -> None:
    result = run(classic_value(), ProbeForge(status={"orgs": 503}))
    assert result.orgs is None
    refused = run(classic_value(), ProbeForge(status={"orgs": 403}))
    assert refused.orgs is None


def test_the_probe_records_an_org_that_refuses_classic_tokens() -> None:
    fake = ProbeForge(status={"repo": 403}, body={"repo": {"message": CLASSIC_MESSAGE}})
    result = run(classic_value(), fake)
    assert result.classic_blocked_orgs == ["saga-xyz"]
    plain = run(classic_value(), ProbeForge(status={"repo": 403}))
    assert plain.classic_blocked_orgs == []


# -- stored on the record, additively ---------------------------------------------


@pytest.fixture
def registry(db) -> GitTokens:
    return GitTokens(db, now=lambda: T0)


def test_the_evidence_is_stored_on_the_record_and_served(db, registry) -> None:
    tenant = seed_tenant(db, "eng", credentials=("git",))
    registry.ensure_tenant_default(tenant)
    slots = Slots()
    slots.put("swarm-tenant-eng-git", classic_value())
    header, request = sso_required("saga-xyz")
    fake = ProbeForge(status={"repo": 403}, headers={"repo": {"x-github-sso": header}},
                      orgs=["Saga-Personal", "other-org"])
    token_id = token_id_for("eng", Scope.TENANT, "")
    stored, _ = registry.probe(tenant, "eng", token_id, tokens=slots, send=fake,
                               repository=OWNER_REPO)
    assert stored.sso_required_orgs == ["saga-xyz"]
    assert stored.orgs == ["Saga-Personal", "other-org"]
    doc = db.docs[f"{COLLECTION}/{token_id}"]
    assert doc["sso_required_orgs"] == ["saga-xyz"] and doc["orgs"] == ["Saga-Personal", "other-org"]
    assert request not in json.dumps(doc, default=str)
    served = stored.to_api()
    assert served["access_evidence"]["sso_required_orgs"] == ["saga-xyz"]
    assert served["access_evidence"]["orgs"] == ["Saga-Personal", "other-org"]
    assert served["access_evidence"]["read_at"] == T0.isoformat()
    # A record written before this lane reads with no evidence, not an error.
    old = dict(doc)
    for key in ("sso_required_orgs", "sso_partial_org_ids", "classic_blocked_orgs", "orgs",
                "orgs_capped", "access_evidence_at"):
        old.pop(key)
    assert type(stored).from_firestore(old).orgs is None


def test_a_later_scoped_probe_that_reads_the_org_clears_its_sso_mark(db, registry) -> None:
    tenant = seed_tenant(db, "eng", credentials=("git",))
    registry.ensure_tenant_default(tenant)
    slots = Slots()
    slots.put("swarm-tenant-eng-git", classic_value())
    token_id = token_id_for("eng", Scope.TENANT, "")
    header, _ = sso_required("saga-xyz")
    registry.probe(tenant, "eng", token_id, tokens=slots, send=ProbeForge(
        status={"repo": 403}, headers={"repo": {"x-github-sso": header}}),
        repository=OWNER_REPO)
    # Another org's repository, scoped: saga-xyz's mark stands.
    other = ProbeForge(repo={"full_name": "other/thing", "default_branch": "main",
                             "private": True, "permissions": {"pull": True, "push": True}})
    kept, _ = registry.probe(tenant, "eng", token_id, tokens=slots, send=other,
                             repository="other/thing")
    assert kept.sso_required_orgs == ["saga-xyz"]
    # saga-xyz read with a 200 now: authorised, so the mark is gone.
    cleared, _ = registry.probe(tenant, "eng", token_id, tokens=slots, send=ProbeForge(),
                                repository=OWNER_REPO)
    assert cleared.sso_required_orgs == []


# -- the refusal names the cause ---------------------------------------------------


def test_refusal_cause_is_pure_and_names_each_cause() -> None:
    record = record_for_slot("eng", Scope.TENANT, registered_by="swarm-api", now=T0)
    plain = refusal_cause("saga-xyz", "widgets", record)
    assert plain["code"] == "ACCOUNT_CANNOT_SEE"
    assert "the token's account cannot see this repository" in plain["message"]
    assert refusal_cause("saga-xyz", "widgets", None)["code"] == "ACCOUNT_CANNOT_SEE"
    record.sso_required_orgs = ["Saga-XYZ"]
    sso = refusal_cause("saga-xyz", "widgets", record)
    assert sso["code"] == "SSO_NOT_AUTHORISED"
    assert sso["message"].startswith(
        "SSO not authorised for saga-xyz on this token -- authorise it at "
        "https://github.com/settings/tokens")
    assert "uses SAML single sign-on" in sso["message"]
    record.sso_required_orgs = []
    record.classic_blocked_orgs = ["saga-xyz"]
    classic = refusal_cause("saga-xyz", "widgets", record)
    assert classic["code"] == "CLASSIC_PAT_BLOCKED"
    assert "the org restricts classic tokens" in classic["message"]
    assert "does not accept classic personal access tokens" in classic["message"]


def test_partial_sso_and_the_org_list_shape_the_plain_cause() -> None:
    record = record_for_slot("eng", Scope.TENANT, registered_by="swarm-api", now=T0)
    record.forge_login = "swarm-bot"
    record.orgs = ["other-org"]
    record.sso_partial_org_ids = [3]
    hidden = refusal_cause("saga-xyz", "widgets", record)
    assert hidden["code"] == "ACCOUNT_CANNOT_SEE"
    assert "swarm-bot is not a member of saga-xyz" in hidden["message"]
    assert "single sign-on" in hidden["message"]
    record.orgs = ["Saga-XYZ"]
    record.sso_partial_org_ids = []
    member = refusal_cause("saga-xyz", "widgets", record)
    assert "not a member" not in member["message"]


def _evidence(db, **fields) -> None:
    tenant = seed_tenant(db, "eng", credentials=("git",))
    registry = GitTokens(db, now=lambda: T0)
    registry.ensure_tenant_default(tenant)
    token_id = token_id_for("eng", Scope.TENANT, "")
    doc = db.docs[f"{COLLECTION}/{token_id}"]
    doc.update(fields)
    doc["access_evidence_at"] = T0 - timedelta(hours=1)


@pytest.fixture
def refused(db, tokens, group_map, objects):
    def build(status: int | None = None):
        transport = GitHubRepos(status={"/repos/saga-xyz/widgets": status} if status else None)
        client = make_client(db, tokens, group_map, objects, forge_tokens=TenantTokens(),
                             transport=transport)
        return client.post("/v1/repositories", json={"repository": OWNER_REPO},
                           headers=auth_header("alice"))
    return build


@pytest.mark.parametrize("status", [None, 403])
def test_a_refused_registration_names_sso(db, refused, status) -> None:
    _evidence(db, sso_required_orgs=["saga-xyz"])
    response = refused(status)
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "no_access"
    assert "SSO not authorised for saga-xyz on this token" in body["message"]
    assert body["detail"]["cause"] == "SSO_NOT_AUTHORISED"
    assert body["detail"]["secret_name"] == "swarm-tenant-eng-git"
    assert body["detail"]["evidence_at"] == (T0 - timedelta(hours=1)).isoformat()


def test_a_refused_registration_names_the_classic_token_policy(db, refused) -> None:
    _evidence(db, classic_blocked_orgs=["saga-xyz"])
    body = refused(403).json()
    assert body["detail"]["cause"] == "CLASSIC_PAT_BLOCKED"
    assert "the org restricts classic tokens" in body["message"]


def test_a_refused_registration_without_evidence_names_the_account(db, refused) -> None:
    body = refused().json()
    assert body["detail"]["cause"] == "ACCOUNT_CANNOT_SEE"
    assert "the token's account cannot see this repository" in body["message"]
    assert body["detail"]["evidence_at"] is None
    # The secret is still named, as before.
    assert "swarm-tenant-eng-git" in body["message"]
