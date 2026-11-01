"""The TypeSafe key for the `jev` MCP server (JEV-RUNNER; part of #1030).

Owner decision 2026-10-14: a live-browser task gets jev-browser's MCP tools
(browser_do, browser_read, ...) beside Playwright MCP and dev-browser when its
submitter or its tenant has a TypeSafe key AND its workspace chose the
TypeSafe judgment backend (an internal one needs no key: §3.2.4). The owner accepts that jev-browser
sends page text and labels to TypeSafe (api.typesafe.ai). This module is the
worker's half: which key, read how, and handed over how.

WHICH KEY is swarm-api's rule, `provider_keys.resolve_provider_key`
(docs/provider-keys.md §2): the submitter's own key, in their CONFIRMED
personal tenant (`swarm-tenant-u-<user>-typesafe`), wins; else the task
tenant's (`swarm-tenant-<tenant>-typesafe`); else none. "Set" is the
`provider_keys/{secret_id}` record swarm-api writes in the same transaction
as the audit entry, the same record the status route reads. Confirmed means
`tenants/u-<user>` is `kind: user` and its principal is the submitter's email:
the frozen slug keeps only the local part, so `dave@partner.com` and
`dave@example.com` both derive `u-dave`, and only the principal says whose it is.
`candidates` restates the order; tests/unit/worker/test_jev_runner.py holds it
to swarm-api's function over every combination.

READ HOW. As this worker's own service account, like every other tenant
credential (`secrets`). A key in the task tenant is readable: swarm-api's
write grants the tenant's worker account on the secret it creates. A
submitter's personal key, on a task in a GROUP tenant, is readable only where
terraform/bootstrap/typesafe_user_keys.tf grants that group's worker account
on that one secret (an owner apply, per listed row). Without the grant the
read is PERMISSION_DENIED, logged as `jev_key_unreadable` by secret id and
error type, and the tenant key is tried next. Nothing here asks swarm-api for
a key: no route serves a credential (test_no_route_serves_credentials.py).

HANDED OVER HOW -- the narrowest working path. The key goes into ONE file,
`private/jev-typesafe.key`, mode 0600, created afresh (O_EXCL, O_NOFOLLOW).
The runner gets the file's PATH (`KEY_FILE_ENV`), never the key, and names it
in the jev server's MCP config entry; `runners/jev_launch.py` reads it and
execs jev-browser-mcp with TYPESAFE_API_KEY in that one process's
environment. So the key is not in the runner's environment, not in the
agent CLI's or its Bash tool's, not in the MCP config file, not in `work/`
(which is checkpointed) and not in any log: it is registered with this
logger for redaction the moment it is read. `private/` is never checkpointed
and never uploaded (workspace.py). The agent runs as the same uid and can read
the MCP config, so it can find and read the file: the same trust level as
the provider key already in the CLI's own environment. That is a limit of
one container, recorded in docs/design/live-browser.md, not something this
module hides.

WITH THE FORWARDER (SEC-MIGRATE-TYPESAFE, lane TS-MIGRATE-WORKER). On GKE
with `SWARM_TYPESAFE_INJECT=on` (`typesafe_shim.switched_on`) none of the
above happens: no key is resolved, read or written, and the runner's
variable names the worker's shim file (`private/jev-typesafe.shim`: the
loopback shim's URL and local token), so the key never enters the pod.
Switched off, this module is exactly what it was.

NEVER FATAL. A key that cannot be resolved costs the attempt the jev tools,
not the attempt: every failure is logged by type and the attempt runs with
Playwright MCP and dev-browser alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Collection, Mapping

from swarm_common.identity import tenant_id_for_user
from swarm_common.models import Tenant, utcnow

from . import indexrun

#: The provider-key provider (swarm_api.validation.TYPESAFE_PROVIDER).
PROVIDER = "typesafe"

#: swarm-api's status records (swarm_api.provider_keys.KEYS_COLLECTION).
KEYS_COLLECTION = "provider_keys"

#: The live browser's address, which the hand-off spool puts in the runner's
#: environment only on a live-browser attempt (handoff.AGENT_PASSTHROUGH_ENV).
CDP_ENDPOINT_ENV = "SWARM_CDP_ENDPOINT"

#: The runner's variable: the PATH of the key file, never the key.
KEY_FILE_ENV = "SWARM_JEV_KEY_FILE"
KEY_FILE_NAME = "jev-typesafe.key"

#: What jev-browser reads (jev-browser 0.2.0, src/jev.mjs KEY_NAMES). Also the
#: one JSON field a JSON payload may carry it under.
KEY_ENV = "TYPESAFE_API_KEY"

#: The workspace's judgment backend (scheduler.judgment, JUDGE-SELECT), passed
#: on to the worker by handoff.AGENT_PASSTHROUGH_ENV. An internal backend
#: needs no TypeSafe key, so this module never resolves or writes one for it.
BACKEND_ENV = "SWARM_JEV_BACKEND"
INTERNAL_BACKENDS = ("internal-jev", "internal-laya")

USER = "user"
TENANT = "tenant"


def secret_id(tenant_id: str) -> str:
    """`swarm-tenant-<tenant>-typesafe`, through the frozen spelling."""
    return Tenant(tenant_id=tenant_id, kind="group", principal="",
                  created_at=utcnow()).secret_name(PROVIDER)


def candidates(task_tenant_id: str, submitter_tenant_id: str | None,
               present: Collection[str]) -> list[tuple[str, str]]:
    """`(scope, secret id)` to try, best first: swarm-api's
    `resolve_provider_key` order, with the tenant key kept as the fallback
    for a personal key this worker cannot read. Pure."""
    out: list[tuple[str, str]] = []
    if submitter_tenant_id:
        own = secret_id(submitter_tenant_id)
        if own in present:
            out.append((USER, own))
    tenant = secret_id(task_tenant_id)
    if tenant in present and all(tenant != s for _, s in out):
        out.append((TENANT, tenant))
    return out


def confirmed_personal_tenant(db: Any, submitted_by: Any, *,
                              call_options: Mapping[str, Any] | None = None) -> str | None:
    """The submitter's personal tenant id, only if its document names them."""
    email = submitted_by.strip().lower() if isinstance(submitted_by, str) else ""
    if "@" not in email:
        return None
    tenant_id = tenant_id_for_user(email)
    snap = db.collection("tenants").document(tenant_id).get(**dict(call_options or {}))
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    principal = data.get("principal")
    if data.get("kind") != "user" or not isinstance(principal, str):
        return None
    return tenant_id if principal.strip().lower() == email else None


def is_set(db: Any, secret: str, *, call_options: Mapping[str, Any] | None = None) -> bool:
    snap = db.collection(KEYS_COLLECTION).document(secret).get(**dict(call_options or {}))
    return bool(snap.exists and (snap.to_dict() or {}).get("set") is True)


def _route_present(state: Any, tenant_id: str, submitted_by: Any) -> tuple[str | None, set[str]]:
    """`(confirmed personal tenant, secret ids set)` from `GET state/context`.

    swarm-api confirms the personal tenant by the rule
    `confirmed_personal_tenant` applies (`kind: user`, the principal the
    signed submitter) and serves the `provider_keys` records of this tenant
    and of that one only. The personal id is still re-derived here from the
    submitter, so an answer naming any other tenant's key is not read.
    """
    context = indexrun.route_context(state)
    email = submitted_by.strip().lower() if isinstance(submitted_by, str) else ""
    served = context.get("personal_tenant_id")
    personal = served if "@" in email and served == tenant_id_for_user(email) else None
    records = context.get("provider_keys")
    records = records if isinstance(records, Mapping) else {}
    ids = {secret_id(tenant_id)}
    if personal:
        ids.add(secret_id(personal))
    present = {s for s in ids
               if isinstance(records.get(s), Mapping) and records[s].get("set") is True}
    return personal, present


def _key_from(payload: str) -> str:
    text = payload.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return ""
        value = parsed.get(KEY_ENV) if isinstance(parsed, dict) else None
        return value.strip() if isinstance(value, str) else ""
    return text


def resolve(*, db: Any, client: Any, tenant_id: str, submitted_by: Any, logger: Any,
            call_options: Mapping[str, Any] | None = None) -> tuple[str, str, str] | None:
    """`(key, scope, secret id)` for this task, or None. The key is registered
    for redaction before it is returned; it is never logged.

    A tenant switched to the state routes passes its attempt's state client
    as `db` (`indexrun.state_routes`): which keys are set, and the confirmed
    personal tenant, come from one `GET state/context`, and no Firestore call
    is made. The key itself is still read from Secret Manager, as this
    worker's own account: no route serves a credential."""
    state = indexrun.state_routes(db)
    if state is not None:
        personal, present = _route_present(state, tenant_id, submitted_by)
    else:
        personal = confirmed_personal_tenant(db, submitted_by, call_options=call_options)
        ids = {secret_id(tenant_id)}
        if personal:
            ids.add(secret_id(personal))
        present = {s for s in ids if is_set(db, s, call_options=call_options)}
    for scope, secret in candidates(tenant_id, personal, present):
        try:
            key = _key_from(client.access(secret))
        except Exception as exc:  # noqa: BLE001 -- said by type, never the value
            logger.warning("jev_key_unreadable", secret=secret, scope=scope,
                           error=type(exc).__name__)
            continue
        if not key:
            logger.warning("jev_key_unreadable", secret=secret, scope=scope, error="empty")
            continue
        logger.register_secret(key)
        return key, scope, secret
    return None


def resolve_workspace(*, db: Any, client: Any, tenant_id: str, logger: Any,
                      call_options: Mapping[str, Any] | None = None) -> str | None:
    """The WORKSPACE's TypeSafe key for a step whose signed workflow switch
    says `jev_judgments: on` (2026-11-01), or None. Resolved exactly as the
    quality gates resolve theirs (`qualitygates.prepare`): `resolve` with no
    submitter, so a person's own key is never spent on a workflow's switch,
    and registered for redaction before it is returned. Never raises."""
    if db is None or client is None:
        return None
    try:
        found = resolve(db=db, client=client, tenant_id=tenant_id, submitted_by=None,
                        logger=logger, call_options=call_options)
    except Exception as exc:  # noqa: BLE001 -- never fatal, said by type
        logger.warning("jev_key_unavailable", use="judgments", error=type(exc).__name__)
        return None
    if found is None:
        logger.info("jev_key_absent", provider=PROVIDER, use="judgments")
        return None
    key, scope, secret = found
    logger.info("jev_key_resolved", scope=scope, secret=secret, use="judgments")
    return key


def _write_private(path: Path, text: str) -> None:
    if path.is_symlink() or path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def prepare(*, endpoint: str | None, db: Any, client: Any, tenant_id: str,
            submitted_by: Any, private_dir: Path, logger: Any,
            call_options: Mapping[str, Any] | None = None,
            backend: str | None = None, inject: bool = False,
            shim_file: Path | None = None) -> dict[str, str]:
    """`{KEY_FILE_ENV: path}` for a live-browser attempt with a key, else {}.

    An internal judgment backend (`backend`, JUDGE-SELECT) never reads or
    writes a key: it is {} before any lookup.

    Only with the live browser (`endpoint`, the gate's loopback CDP address):
    jev attaches to nothing else, and a task without one never reads the key.

    With the forwarder switched on (`inject`, `typesafe_shim.switched_on`;
    SEC-MIGRATE-TYPESAFE) NO KEY IS READ OR WRITTEN: the runner's variable
    names the worker's shim file instead (`shim_file`, the shim's loopback URL
    and local token, `typesafe_shim.SHIM_FILE_NAME`), which `jev_launch.py`
    reads in its place. No shim (the ticket was refused) is no jev tools.
    """
    if inject:
        return _shim_env(endpoint=endpoint, backend=backend, shim_file=shim_file,
                         logger=logger)
    if not endpoint or client is None or db is None:
        return {}
    if (backend or "").strip() in INTERNAL_BACKENDS:
        return {}
    try:
        found = resolve(db=db, client=client, tenant_id=tenant_id, submitted_by=submitted_by,
                        logger=logger, call_options=call_options)
        if found is None:
            logger.info("jev_key_absent", provider=PROVIDER)
            return {}
        key, scope, secret = found
        path = Path(private_dir) / KEY_FILE_NAME
        _write_private(path, key)
    except Exception as exc:  # noqa: BLE001 -- never fatal, said by type
        logger.warning("jev_key_unavailable", error=type(exc).__name__)
        return {}
    logger.info("jev_key_resolved", scope=scope, secret=secret)
    return {KEY_FILE_ENV: str(path)}


def _shim_env(*, endpoint: str | None, backend: str | None, shim_file: Path | None,
              logger: Any) -> dict[str, str]:
    """`prepare` with the forwarder on: the shim file's PATH, or {}. Reads nothing."""
    if not endpoint or (backend or "").strip() in INTERNAL_BACKENDS:
        return {}
    if shim_file is None or not Path(shim_file).is_file():
        logger.info("jev_key_absent", provider=PROVIDER, delivery="inject")
        return {}
    logger.info("jev_key_resolved", delivery="inject")
    return {KEY_FILE_ENV: str(shim_file)}
