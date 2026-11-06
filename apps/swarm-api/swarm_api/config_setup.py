"""The Config setup strip's steps 3 and 4, measured (CONFIG-SETUP-READ, part of #2157).

Design: docs/web-ui/mockups/config-e.html, frame S1 and its notes. Every
Config page opens under "GitHub connected -> Repositories chosen -> Secrets
set -> People invited", each step measured from a live read. Steps 1 and 2
are the onboarding checklist's `github_connected` and `repos_chosen`; this
module measures 3 and 4, and `GET /v1/onboarding` serves them beside the
checklist as `config_setup`, so the console still makes one read.

    secrets   what is NEEDED AND NOT STORED: a secret something registered
              names and nobody has stored. Three namers today --
                mcp_server    an enabled MCP server in `workspace_secret` mode
                              with no store secret granted to `mcp:<name>`
                              (the same test Config › Secrets' MCP row makes,
                              `secrets_overview._mcp_rows`, so the two agree);
                qa_target     a QA target environment entry whose
                              `sign_in.kind` is `own_login` and whose
                              credential is not set -- every entry of every
                              target, as `qatargets.credential_status` reads it;
                provider_key  the workspace's TypeSafe slot, while a registered
                              consumer reads it (`jev`, when the workspace's
                              Jev judges with `typesafe`) and it is not set.
              The step is done when nothing is needed and not stored.
    people    in a group workspace, something to do only while requests to
              join wait on the reader (open, naming the workspace's group,
              and the reader an admin who decides them): then it is not done
              and says how many. Otherwise it does not apply, with the reason
              the Config panel shows: the members come from the Google group,
              a role override (`RoleStore.overrides`, the roster Config ›
              People and roles draws, still served as `holders`/`admins`) is
              optional, so the step had nothing to finish (the spine's Config
              dot that never cleared, 2026-11-01). It does not apply in a
              personal workspace either, so the strip draws three steps
              wherever nothing waits.

NAMES ONLY. An item is `{kind, name, needed_by, where}` (plus the target and
environment, for a QA entry): never a value, a length, a version name or any
part of one. Nothing here reads a Secret Manager payload; every field is
copied from an allow-list, so a field a store adds later is not served.

AN UNREAD SOURCE IS UNKNOWN, NEVER 0 AND NEVER DONE. Each source is read on
its own; one that raises is `{"needed": null, "reason": ...}` and the step's
total and `done` are null, while the sources that were read still list their
items. The reason is a fixed sentence: an exception's text is never served
or logged, only its type, since a store error can quote what it was handed.

THE TENANT IS THE ROUTE'S `tenant_scope`, never the request, so a member reads
their own selected workspace's setup and nothing of any other's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from swarm_common.models import Tenant

from .auth import AuthContext
from .joinrequests import REQUESTED, JoinStore
from .judgment import TYPESAFE_BACKEND, JudgmentSettings, settings_mac
from .provider_keys import TYPESAFE, ProviderKeys
from .qatargets import QaTargets, credential_status
from .roles import ADMIN, RoleStore
from .secrets_overview import SCOPE_WORKSPACE, UNSET, _mcp_rows, offers

log = logging.getLogger(__name__)

MCP_SERVER = "mcp_server"
QA_TARGET = "qa_target"
PROVIDER_KEY = "provider_key"

#: The console addresses (apps/swarm-ui/src/paths.ts) each item is set on.
#: An MCP server's credential is a workspace secret granted to it, set from
#: its own row on Config › MCP servers; a QA entry's credential from Config ›
#: Repositories › QA targets (`QA_LINK_ADDRESS`); the workspace TypeSafe key
#: from Settings › API keys, where an admin sets the tenant's.
MCP_WHERE = "config/mcp-servers"
QA_WHERE = "config/repositories?page=qa-targets"
KEY_WHERE = "settings/keys"

SOURCES = ("mcp_servers", "qa_targets", "provider_key")

UNREAD = {
    "mcp_servers": "the MCP servers and the secrets granted to them could not be read",
    "qa_targets": "the QA targets could not be read",
    "provider_key": "the TypeSafe key's status or the workspace's Jev backend could not be read",
    "roles": "this workspace's roles could not be read; retry shortly",
    "joins": "this workspace's requests to join could not be read; retry shortly",
}
PERSONAL = "a personal workspace has nobody to invite"
#: A group workspace's people come from its directory group: nobody asks to
#: join one through SwarmCloud, so there is no request to be waiting on.
NO_REQUESTS = "this workspace takes no requests to join; its people come from its group"
#: Why `pending` is null for a reader who decides no request to join.
NOT_DECIDER = "only this workspace's admins decide its requests to join"
#: Why a group workspace draws no People invited step while nothing waits on
#: the reader: its members come from its Google group, which swarm-api cannot
#: list without a grant it does not hold, and a role override is optional.
FROM_GROUP = ("this workspace's people come from its Google group; add them there. "
              "Nothing here is waiting on you")


def waiting(n: int) -> str:
    return "1 request to join is waiting for you" if n == 1 \
        else f"{n} requests to join are waiting for you"


def _mcp_items(db: Any, tenant: Tenant, now: Any) -> list[dict[str, Any]]:
    own = [o for o in offers(db, tenant) if o.scope == SCOPE_WORKSPACE]
    return [{"kind": MCP_SERVER, "name": row["name"], "needed_by": row["consumer"],
             "where": MCP_WHERE}
            for row in _mcp_rows(db, tenant.tenant_id, own, may=False, now=now)
            if row["status"] == UNSET and row.get("enabled") is not False]


def _qa_items(db: Any, tenant_id: str, now: Any) -> list[dict[str, Any]]:
    items = []
    for target in QaTargets(db, now=now).list(tenant_id):
        for entry in target.get("environments") or []:
            if (entry.get("sign_in") or {}).get("kind") != "own_login":
                continue
            if credential_status(entry)["set"]:
                continue
            items.append({
                "kind": QA_TARGET, "name": target.get("name"),
                "target_id": target.get("id"), "environment": entry.get("name"),
                "needed_by": f"QA runs of {target.get('name')} ({entry.get('name')})",
                "where": QA_WHERE,
            })
    return items


def _key_items(db: Any, tenant_id: str, *, settings: Any, now: Any,
               consumers: Sequence[str]) -> list[dict[str, Any]]:
    if not consumers:
        return []
    backend = JudgmentSettings(db, now=now, mac=settings_mac(settings)) \
        .read(tenant_id)["judgment_backend"]
    if backend != TYPESAFE_BACKEND:
        return []
    if ProviderKeys(db, now=now).status(tenant_id, TYPESAFE)["set"]:
        return []
    return [{"kind": PROVIDER_KEY, "name": TYPESAFE, "needed_by": ", ".join(consumers),
             "where": KEY_WHERE}]


def secrets_step(db: Any, tenant: Tenant, *, settings: Any, now: Any,
                 key_consumers: Sequence[str]) -> dict[str, Any]:
    reads: dict[str, Callable[[], list[dict[str, Any]]]] = {
        "mcp_servers": lambda: _mcp_items(db, tenant, now),
        "qa_targets": lambda: _qa_items(db, tenant.tenant_id, now),
        "provider_key": lambda: _key_items(db, tenant.tenant_id, settings=settings, now=now,
                                           consumers=key_consumers),
    }
    items: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        try:
            found = reads[source]()
        except Exception as exc:  # noqa: BLE001 -- any failure is "unread"
            log.warning("config setup source unread tenant=%s source=%s (%s)",
                        tenant.tenant_id, source, type(exc).__name__)
            sources[source] = {"needed": None, "reason": UNREAD[source]}
            continue
        sources[source] = {"needed": len(found), "reason": None}
        items += found
    unread = any(s["needed"] is None for s in sources.values())
    needed = None if unread else len(items)
    return {"needed": needed, "done": None if needed is None else needed == 0,
            "items": items, "sources": sources}


def _email(value: Any) -> str:
    return (value if isinstance(value, str) else "").strip().lower()


def _open_requests(joins: JoinStore | None, roles: RoleStore, auth: AuthContext,
                   tenant: Tenant) -> int | None:
    """Open requests to join `tenant` that the reader decides, as Admin ›
    People's queue draws them (`notifications.join_requests_to_decide`): only
    a workspace admin or a platform admin decides, and only a request naming
    the group the workspace has now. None for anyone else: a member cannot
    act on a request, so it is nothing left for them."""
    if joins is None:
        return None
    me = roles.try_resolve(auth)
    if me is None or me.role != ADMIN:
        return None
    group = (auth.tenant_principal or tenant.principal or "").strip().lower()
    return sum(1 for view in joins.open_for([tenant.tenant_id])
               if view.state == REQUESTED and view.request.group == group)


def people_step(roles: RoleStore | None, auth: AuthContext, tenant: Tenant,
                joins: JoinStore | None = None) -> dict[str, Any]:
    if tenant.kind == "user":
        return {"applies": False, "done": None, "holders": None, "admins": None,
                "pending": None, "reason": PERSONAL, "pending_reason": None}
    unread = {"applies": True, "done": None, "holders": None, "admins": None,
              "pending": None, "reason": UNREAD["roles"], "pending_reason": NO_REQUESTS}
    if roles is None:
        return unread
    try:
        roster = roles.overrides(tenant.tenant_id)
    except Exception as exc:  # noqa: BLE001 -- any failure is "unread"
        log.warning("config setup roles unread tenant=%s (%s)", tenant.tenant_id,
                    type(exc).__name__)
        return unread
    try:
        pending = _open_requests(joins, roles, auth, tenant)
    except Exception as exc:  # noqa: BLE001 -- any failure is "unread"
        log.warning("config setup join requests unread tenant=%s (%s)", tenant.tenant_id,
                    type(exc).__name__)
        return dict(unread, reason=UNREAD["joins"], pending_reason=UNREAD["joins"])
    me = _email(auth.email)
    others = [(email, grant) for email, grant in roster if _email(email) != me]
    counts = {"holders": len(others),
              "admins": sum(1 for _, grant in others if grant.role == ADMIN)}
    if pending:
        return {"applies": True, "done": False, **counts, "pending": pending,
                "reason": waiting(pending), "pending_reason": None}
    # Nothing waits on the reader: the group supplies the people and a role
    # override is optional, so there is nothing to invite (the Engineering
    # dot on a group workspace, 2026-11-01).
    return {"applies": False, "done": None, **counts, "pending": pending,
            "reason": FROM_GROUP, "pending_reason": None if pending is not None else NOT_DECIDER}


def read(db: Any, *, auth: AuthContext, tenant: Tenant, roles: RoleStore | None,
         settings: Any, now: Any, key_consumers: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """`config_setup` for the caller's selected workspace `tenant`."""
    return {
        "secrets": secrets_step(db, tenant, settings=settings, now=now,
                                key_consumers=list(key_consumers.get(TYPESAFE, []))),
        "people": people_step(roles, auth, tenant,
                              joins=JoinStore(db, settings, now=now)),
    }
