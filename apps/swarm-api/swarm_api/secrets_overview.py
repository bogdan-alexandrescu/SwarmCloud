"""One read of every scope's secrets, never a value (SSC-OVERVIEW, part of #2158).

Design: docs/design/secrets-scopes.md §4 (what Config › Secrets lists, and
from which store) and §6.2 (`GET /v1/secrets/overview`), with the owner's
answer Q1 of its §11 for shadowing.

NO STORE MIGRATES (§4). Config › Secrets is a view over the stores that
already exist; each row keeps its collection, its Secret Manager name, its
IAM and its writer, and names that writer (`writer`, an API route) or the
console page it is still managed on (`page`). This module only READS, and
only status: what is set, by whom and when, what it is granted to, and which
scope wins a name. It reads no Secret Manager payload and calls no broker and
no forge.

    scope        rows (store)
    workspace    the store's own secrets (`workspace_secrets`), the TypeSafe
                 tenant key (`provider_keys`), the runner LLM keys (the tenant's
                 `credentials`), MCP servers whose credential is a store secret
                 (`mcp_servers`), one line for the subscription accounts, and the
                 tenant's GitHub token (`git_tokens`)
    personal     the caller's own store secrets in their personal workspace
                 (`opted_in` to the selected one), their TypeSafe key, their
                 saved browser logins, and their GitHub user slots
    repository   that repository's secrets for agents, its QA targets' sign-in
                 credential and challenge-bypass status, its `-git-r-` token,
                 and SwarmCloud's record of the Actions secrets it wrote there

NEVER A VALUE, A LENGTH OR ANY CHARACTER OF ONE. Every row is built from an
allow-list of fields, so a field a store adds later is not served by
accident; `git_tokens`' `last4` is never read into a row, nor is any
`version` name. Not listed, as §4 says: an MCP credential in `dispatch` mode
(it lives as long as its agent), QA identity grants and platform secrets
(Admin's).

PERSONAL ROWS ARE ONLY THE CALLER'S. The personal scope reads the caller's
own personal workspace, derived from their verified email and confirmed by
its tenant document (`provider_keys.own_personal_tenant`), never from the
request. Another person appears only as the winner of a name on a
workspace or repository row (§2.4: "shadowed on agents of <person>"), and
only for a secret they opted in to this workspace.

SHADOWING (Q1). The release (`workspace_secrets._resolve_shadowing`) keeps
one item per `(slot, name)`: the person's, then the repository's, then the
workspace's. Here, two secrets of one name shadow where an ENABLED grant of
each delivers into the same slot (`workspace_secrets._deliverable`, the
release's own rule, less the attestation, which is the release's alone).
Each loser carries `shadowed_by` and each winner `shadows`, one entry per
(other secret, slot), with `where` it applies: on that person's agents, or
on agents that clone that repository. Two repositories' secrets of one name
are not compared: which wins depends on which the attempt clones first.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES

from .actions_secrets import SET as ACTIONS_SET
from .actions_secrets import WRITES_COLLECTION as ACTIONS_WRITES
from .auth import AuthContext
from .browser_logins import COLLECTION as BROWSER_LOGINS
from .browser_logins import login_to_api
from .gittokens import GitTokenRecord, GitTokens, Scope
from .grant_attest import REPO_ID_FIELD
from .mcp_servers import MODE_WORKSPACE, McpServers
from .provider_keys import ProviderKeys
from .qatargets import BYPASS_FIELD, QaTargets, bypass_status, credential_status
from .roles import RoleStore
from .validation import PROVIDER_KEY_PROVIDERS, known_providers
from .workspace_secrets import (
    ACTIVE,
    GRANTS_COLLECTION,
    INJECT_ONLY_SLOTS,
    SCOPE_PERSONAL,
    SCOPE_REPOSITORY,
    SCOPE_WORKSPACE,
    SECRETS_COLLECTION,
    _bound_to,
    _deliverable,
    can_manage,
    is_personal,
    is_repository_record,
    record_api,
)
from .workspaces import personal_tenant_id

SCOPES = (SCOPE_WORKSPACE, SCOPE_PERSONAL, SCOPE_REPOSITORY)

#: The console's sections, in the order §6.1 draws them.
AGENTS = "agents"
KEYS = "keys"
BROWSER_SESSIONS = "browser_sessions"
QA_SIGN_IN = "qa_sign_in"
GITHUB = "github"
ACTIONS = "actions"
ACCOUNTS = "accounts"

SET = "set"
UNSET = "unset"

#: Where the rows that are only a link are managed (apps/swarm-ui/src/paths.ts).
ACCOUNTS_PAGE = "/capacity/accounts"
GIT_TOKENS_PAGE = "/repositories/tokens"

#: §4's stores that are deliberately not on Config › Secrets, and why.
NOT_LISTED = (
    {"store": "mcp_dispatch_credentials",
     "why": "an MCP credential in dispatch mode lives as long as its agent and is set in "
            "the submission"},
    {"store": "qa_identity_grants", "why": "a platform admin's, on Admin"},
    {"store": "platform_secrets", "why": "a platform admin's, on Admin › Platform secrets"},
)

NOTE = "status only; no route in this API returns a secret's value"

_PRECEDENCE = {SCOPE_PERSONAL: 0, SCOPE_REPOSITORY: 1, SCOPE_WORKSPACE: 2}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


# ---------------------------------------------------------------------------
# The store's records, and which scope wins a name (Q1)
# ---------------------------------------------------------------------------


@dataclass
class _Offer:
    """One store secret that may reach an agent of the selected workspace."""

    record: dict[str, Any]          # `record_api`'s shape: no value
    scope: str
    label: dict[str, Any]
    slots: frozenset[str]
    shadowed_by: list[dict[str, Any]] = field(default_factory=list)
    shadows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def secret_id(self) -> str:
        return str(self.record.get("secret_id"))

    @property
    def name(self) -> str:
        return str(self.record.get("name"))


def _grants(db: Any, doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The grants on `doc` written in its own tenant: a grant document is
    tenant-writable, so one claiming another tenant's secret counts for none."""
    query = db.collection(GRANTS_COLLECTION).where(
        filter=FieldFilter("secret_id", "==", doc.get("secret_id")))
    return [g for g in (s.to_dict() or {} for s in query.stream())
            if g.get("secret_id") == doc.get("secret_id")
            and g.get("tenant_id") == doc.get("tenant_id")]


def _slots(grants: Iterable[Mapping[str, Any]], kind: Any) -> frozenset[str]:
    """The slots the enabled grants deliver into, as the release decides
    them; an inject-only slot is never released, so it shadows nothing."""
    out = set()
    for grant in grants:
        if grant.get("enabled") is not True:
            continue
        slot = _deliverable(grant, kind)
        if slot is not None and slot not in INJECT_ONLY_SLOTS:
            out.add(slot)
    return frozenset(out)


def _offer(db: Any, doc: Mapping[str, Any], scope: str, label: dict[str, Any]) -> _Offer:
    grants = _grants(db, doc)
    return _Offer(record=record_api(doc, grants), scope=scope, label=label,
                  slots=_slots(grants, doc.get("kind")))


def _active(db: Any, tenant_id: str) -> list[dict[str, Any]]:
    query = db.collection(SECRETS_COLLECTION) \
        .where(filter=FieldFilter("tenant_id", "==", tenant_id)) \
        .where(filter=FieldFilter("state", "==", ACTIVE))
    docs = [s.to_dict() or {} for s in query.stream()]
    return sorted((d for d in docs if d.get("tenant_id") == tenant_id
                   and d.get("state") == ACTIVE and d.get("secret_id")),
                  key=lambda d: str(d.get("name")))


WORKSPACE_LABEL = {"scope": SCOPE_WORKSPACE, "where": "agents of this workspace"}


def _repository_label(doc: Mapping[str, Any]) -> dict[str, Any]:
    repository = doc.get("repository") or doc.get(REPO_ID_FIELD)
    return {"scope": SCOPE_REPOSITORY, "repo_id": doc.get(REPO_ID_FIELD),
            "repository": doc.get("repository"), "where": f"agents that clone {repository}"}


def _person_label(email: str) -> dict[str, Any]:
    return {"scope": SCOPE_PERSONAL, "person": email, "where": f"agents of {email}"}


def _opted_in(db: Any, tenant_id: str) -> list[dict[str, Any]]:
    """Every person's secret opted in to group workspace `tenant_id`, held to
    what the release would take: active, `scope: personal`, not bound to a
    repository, in the personal workspace of the person who created it."""
    query = db.collection(SECRETS_COLLECTION).where(
        filter=FieldFilter("use_in_workspaces", "array_contains", tenant_id))
    out = []
    for doc in (s.to_dict() or {} for s in query.stream()):
        email = str(doc.get("created_by") or "").strip().lower()
        if (doc.get("state") == ACTIVE and doc.get("scope") == SCOPE_PERSONAL and email
                and not is_repository_record(doc)
                and tenant_id in (doc.get("use_in_workspaces") or [])
                and doc.get("tenant_id") == personal_tenant_id(email)
                and doc.get("tenant_id") != tenant_id):
            out.append(doc)
    return sorted(out, key=lambda d: (str(d.get("name")), str(d.get("tenant_id"))))


def offers(db: Any, tenant: Tenant) -> list[_Offer]:
    """Every store secret that may reach an agent of `tenant`, with Q1's
    shadowing between them already worked out."""
    found: list[_Offer] = []
    for doc in _active(db, tenant.tenant_id):
        if _bound_to(doc, None):
            found.append(_offer(db, doc, SCOPE_WORKSPACE, WORKSPACE_LABEL))
        elif _bound_to(doc, doc.get(REPO_ID_FIELD)) and doc.get(REPO_ID_FIELD):
            found.append(_offer(db, doc, SCOPE_REPOSITORY, _repository_label(doc)))
    if not is_personal(tenant):
        for doc in _opted_in(db, tenant.tenant_id):
            email = str(doc.get("created_by")).strip().lower()
            found.append(_offer(db, doc, SCOPE_PERSONAL, _person_label(email)))
    _shadow(found)
    return found


def _shadow(found: list[_Offer]) -> None:
    by_name: dict[str, list[_Offer]] = {}
    for offer in found:
        by_name.setdefault(offer.name, []).append(offer)
    for group in by_name.values():
        for winner in group:
            for loser in group:
                if _PRECEDENCE[winner.scope] >= _PRECEDENCE[loser.scope]:
                    continue
                for slot in sorted(winner.slots & loser.slots):
                    loser.shadowed_by.append({**winner.label, "slot": slot})
                    winner.shadows.append({**loser.label, "slot": slot})
    for offer in found:
        offer.shadowed_by.sort(key=_order)
        offer.shadows.sort(key=_order)


def _order(entry: Mapping[str, Any]) -> tuple[int, str, str]:
    return _PRECEDENCE[entry["scope"]], str(entry["where"]), str(entry["slot"])


def _secret_row(record: Mapping[str, Any], *, may: bool, writer: str,
                offer: _Offer | None) -> dict[str, Any]:
    rotated = record.get("rotated_at") is not None
    return {
        "section": AGENTS,
        "store": "workspace_secrets",
        "name": record.get("name"),
        "scope": record.get("scope"),
        "repo_id": record.get("repo_id"),
        "repository": record.get("repository"),
        "provider": record.get("provider"),
        "kind": record.get("kind"),
        "status": SET,
        "set_at": record.get("rotated_at") if rotated else record.get("created_at"),
        "set_by": record.get("rotated_by") if rotated else record.get("created_by"),
        "consumers": [g["consumer"] for g in record.get("grants") or [] if g.get("enabled")],
        "shadowed_by": list(offer.shadowed_by) if offer else [],
        "shadows": list(offer.shadows) if offer else [],
        "can_manage": may,
        "writer": writer,
    }


# ---------------------------------------------------------------------------
# The other stores, one allow-listed row each
# ---------------------------------------------------------------------------


def _provider_key_rows(keys: ProviderKeys, tenant_id: str, *, may: bool,
                       writer_scope: str) -> list[dict[str, Any]]:
    """TypeSafe and any other `PROVIDER_KEY_PROVIDERS` key, from
    `provider_keys` (§4): the tenant's `credentials` never lists them."""
    rows = []
    for provider in sorted(PROVIDER_KEY_PROVIDERS):
        status = keys.status(tenant_id, provider)
        is_set = bool(status.get("set"))
        rows.append({
            "section": KEYS, "store": "provider_keys", "name": provider,
            "tenant_id": tenant_id, "secret_id": status.get("secret_id"),
            "status": SET if is_set else UNSET,
            "set_at": status.get("set_at") if is_set else None,
            "set_by": status.get("set_by") if is_set else None,
            "consumers": [], "can_manage": may,
            "writer": f"PUT /v1/provider-keys/{provider}/{writer_scope}",
        })
    return rows


def _runner_key_rows(tenant: Tenant, *, may: bool) -> list[dict[str, Any]]:
    """The runner LLM keys (Q7 of workspace-secrets.md: listed, not moved).
    The tenant document records only that one is set."""
    known = set(known_providers())
    return [{
        "section": KEYS, "store": "runner_provider_keys", "name": provider,
        "secret_id": tenant.secret_name(provider), "status": SET,
        "set_at": None, "set_by": None,
        "consumers": [f"profile:{name}" for name, profile in sorted(RUNNER_PROFILES.items())
                      if profile.provider == provider and profile.worker_action is None],
        "can_manage": may, "writer": "POST /v1/tenants/me/credentials",
    } for provider in sorted(set(tenant.credentials or []) & known)]


def _mcp_rows(db: Any, tenant_id: str, own: list[_Offer], *, may: bool,
              now: Any) -> list[dict[str, Any]]:
    """Registered MCP servers whose credential is a store secret granted to
    `mcp:<name>`. A `dispatch` or `none` server holds nothing here."""
    rows = []
    for server in McpServers(db, now=now).list(tenant_id):
        if (server.get("auth") or {}).get("mode") != MODE_WORKSPACE:
            continue
        consumer = server["consumer"]
        names = sorted(o.name for o in own if consumer in
                       [g["consumer"] for g in o.record.get("grants") or [] if g.get("enabled")])
        rows.append({
            "section": KEYS, "store": "mcp_servers", "name": server.get("name"),
            "consumer": consumer, "enabled": server.get("enabled"),
            "secrets": names, "status": SET if names else UNSET,
            "set_at": None, "set_by": None, "consumers": [consumer],
            "can_manage": may, "writer": f"PUT /v1/workspace/secrets/{{name}}/grants/{consumer}",
        })
    return rows


def _git_row(record: GitTokenRecord) -> dict[str, Any]:
    """Kind, state, verified, expires and login (§4). Never `last4`."""
    return {
        "section": GITHUB, "store": "git_tokens", "name": record.secret_name,
        "token_id": record.token_id, "scope": record.scope.value,
        "user": record.user, "repo_ids": list(record.repo_ids),
        "kind": record.kind, "status": record.state.value,
        "forge_login": record.forge_login,
        "verified_at": _iso(record.verified_at), "expires_at": _iso(record.expires_at),
        "set_at": _iso(record.rotated_at or record.registered_at),
        "set_by": record.registered_by,
        "consumers": [], "can_manage": False, "page": GIT_TOKENS_PAGE,
    }


def _accounts_row() -> dict[str, Any]:
    return {"section": ACCOUNTS, "store": "pool_accounts", "name": "subscription accounts",
            "status": "elsewhere", "set_at": None, "set_by": None, "consumers": [],
            "can_manage": False, "page": ACCOUNTS_PAGE}


def _browser_rows(db: Any, personal: Tenant, email: str) -> list[dict[str, Any]]:
    query = db.collection(BROWSER_LOGINS).where(
        filter=FieldFilter("tenant_id", "==", personal.tenant_id))
    docs = [d for d in (s.to_dict() or {} for s in query.stream())
            if d.get("tenant_id") == personal.tenant_id]
    rows = []
    for doc in sorted(docs, key=lambda d: str(d.get("name"))):
        login = login_to_api(doc)
        rows.append({
            "section": BROWSER_SESSIONS, "store": "browser_logins", "name": login["name"],
            "site": login["site"], "status": login["state"],
            "set_at": login["saved_at"], "set_by": email,
            "expires_at": login["expires_at"], "last_used_at": login["last_used_at"],
            "consumers": [], "can_manage": True,
            "writer": f"DELETE /v1/browser-logins/{login['name']}",
        })
    return rows


def _qa_rows(db: Any, tenant_id: str, repo_id: str, *, may: bool,
             now: Any) -> list[dict[str, Any]]:
    """Each QA target of the repository, per environment: the sign-in
    credential's status, and the challenge bypass's where one is configured."""
    rows = []
    for target in QaTargets(db, now=now).list(tenant_id):
        if target.get("repo_id") != repo_id:
            continue
        for entry in target.get("environments") or []:
            fields = [("credential", credential_status(entry),
                       f"PUT /v1/qa-targets/{target.get('id')}/credential")]
            bypass = bypass_status(entry)
            if entry.get("challenge_bypass") or bypass["set"] or entry.get(BYPASS_FIELD):
                fields.append(("challenge_bypass", bypass,
                               f"PUT /v1/qa-targets/{target.get('id')}/challenge-bypass"))
            for name, status, writer in fields:
                rows.append({
                    "section": QA_SIGN_IN, "store": "qa_targets", "name": target.get("name"),
                    "target_id": target.get("id"), "environment": entry.get("name"),
                    "field": name, "status": SET if status["set"] else UNSET,
                    "set_at": status["set_at"], "set_by": status["set_by"],
                    "consumers": [], "can_manage": may, "writer": writer,
                })
    return rows


def _actions_rows(db: Any, tenant_id: str, repo_id: str, *, may: bool) -> list[dict[str, Any]]:
    """SwarmCloud's own record of the Actions secrets it wrote (§3.8): names
    and times. GitHub's live list is `GET /v1/repositories/{repo_id}/actions/secrets`."""
    query = db.collection(ACTIONS_WRITES).where(filter=FieldFilter("repo_id", "==", repo_id))
    docs = [d for d in (s.to_dict() or {} for s in query.stream())
            if d.get("repo_id") == repo_id and d.get("tenant_id") == tenant_id
            and d.get("state") == ACTIONS_SET]
    return [{
        "section": ACTIONS, "store": "actions_secret_writes", "name": doc.get("name"),
        "environment": doc.get("environment"), "status": SET,
        "set_at": _iso(doc.get("set_at")), "set_by": doc.get("set_by"),
        "consumers": [], "can_manage": may, "github_admin_checked_at_write": True,
        "writer": f"PUT /v1/repositories/{repo_id}/actions/secrets/{doc.get('name')}",
    } for doc in sorted(docs, key=lambda d: (str(d.get("environment") or ""),
                                             str(d.get("name"))))]


# ---------------------------------------------------------------------------
# One scope
# ---------------------------------------------------------------------------


def workspace_scope(db: Any, *, auth: AuthContext, tenant: Tenant, roles: RoleStore | None,
                    now: Any) -> dict[str, Any]:
    may = can_manage(auth, tenant, roles)
    found = offers(db, tenant)
    own = [o for o in found if o.scope == SCOPE_WORKSPACE]
    rows = [_secret_row(o.record, may=may, writer="/v1/workspace/secrets", offer=o) for o in own]
    rows += _provider_key_rows(ProviderKeys(db, now=now), tenant.tenant_id, may=may,
                               writer_scope="user" if is_personal(tenant) else "tenant")
    rows += _runner_key_rows(tenant, may=may)
    rows += _mcp_rows(db, tenant.tenant_id, own, may=may, now=now)
    rows.append(_accounts_row())
    rows += [_git_row(r) for r in GitTokens(db, now=now).list(tenant.tenant_id)
             if r.scope is Scope.TENANT]
    return {"can_manage": may, "rows": rows}


def personal_scope(db: Any, *, auth: AuthContext, tenant: Tenant, personal: Tenant | None,
                   now: Any) -> dict[str, Any]:
    """The caller's own: `personal` is their personal workspace, confirmed by
    the route, or None when they have none."""
    email = (auth.email or "").strip().lower()
    rows: list[dict[str, Any]] = []
    if personal is not None:
        found = {o.secret_id: o for o in offers(db, tenant)}
        for doc in _active(db, personal.tenant_id):
            if not _bound_to(doc, None):
                continue
            row = _secret_row(record_api(doc, _grants(db, doc)), may=True,
                              writer="/v1/workspace/secrets", offer=found.get(doc["secret_id"]))
            row["opted_in"] = (None if is_personal(tenant)
                               else tenant.tenant_id in (doc.get("use_in_workspaces") or []))
            rows.append(row)
        rows += _provider_key_rows(ProviderKeys(db, now=now), personal.tenant_id, may=True,
                                   writer_scope="user")
        rows += _browser_rows(db, personal, email)
    seen: set[str] = set()
    for tenant_id in dict.fromkeys(t.tenant_id for t in (tenant, personal) if t is not None):
        for record in GitTokens(db, now=now).list(tenant_id):
            if record.scope is Scope.USER and (record.user or "").strip().lower() == email \
                    and record.token_id not in seen:
                seen.add(record.token_id)
                rows.append(_git_row(record))
    return {"can_manage": personal is not None, "rows": rows}


def repository_scope(db: Any, *, auth: AuthContext, tenant: Tenant, roles: RoleStore | None,
                     registration: Mapping[str, Any], now: Any) -> dict[str, Any]:
    """`registration` is this workspace's, checked by the route (404 else)."""
    repo_id = str(registration["repo_id"])
    may = can_manage(auth, tenant, roles)
    found = [o for o in offers(db, tenant)
             if o.scope == SCOPE_REPOSITORY and o.record.get("repo_id") == repo_id]
    rows = [_secret_row(o.record, may=may, writer=f"/v1/repositories/{repo_id}/secrets",
                        offer=o) for o in found]
    rows += _qa_rows(db, tenant.tenant_id, repo_id, may=may, now=now)
    rows += [_git_row(r) for r in GitTokens(db, now=now).list(tenant.tenant_id)
             if r.scope is Scope.REPOSITORY and repo_id in r.repo_ids]
    rows += _actions_rows(db, tenant.tenant_id, repo_id, may=may)
    return {"can_manage": may, "rows": rows}
