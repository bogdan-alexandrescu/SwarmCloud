"""Agent plugins: the closed catalogue, a step's plugins checked at submission, and a workspace's allowed plugins.

The owner (BGD, 3 November 2026) chose plugins per step, gstack first, baked
into the agent images at a pinned version, and merged them into the agent
quality program: a step's plugins ride in its work contract's capabilities
section (`capabilities.plugins`, contract request 136), and the catalogue is a
runtime capability manifest entry. docs/design/agent-plugins.md is the plan;
docs/design/agent-plugins-packaging.md measured what the image carries.

THE CATALOGUE. `CATALOGUE` is the platform's closed list: per plugin its
pinned version, the read-only path the images install it at, the runner
profiles that can load it and, per mode, the skills it may offer. A caller
names a plugin, a mode and skills from it by name only, never a path, a
command, a URL or a version (invariant 10). gstack's `cso` (security review)
is left out of the first catalogue (the owner's pick on the packaging
spike: as packaged it cannot run). The worker restates the same entry in
`agent_worker.plugins` (it imports nothing from swarm-api); a unit test holds
the two equal.

UNATTENDED ONLY, FOR NOW. Planner and designer modes interview a person, and
that needs the question bridge, which has not landed: a step naming either is
refused here with that reason (`SUPPORTED_MODES`).

WHO MAY USE ONE. `workspace_settings/{tenant}.plugins.allowed`, the list of
catalogue plugins a workspace's steps may name, written only by its owner or
admin (or a platform admin) through `PluginsStore.put`, audited, and signed
under the `swarm-settings-key` (block "plugins", `swarm_common.specsign.
SETTINGS_MAC_FIELDS`). A tenant worker can write that document directly, so a
step's plugins are accepted only when the stored block's MAC verifies, for
every tenant, whatever SETTINGS_MAC_TENANTS says: an unsigned "allowed" is
nobody's choice. Default: none allowed.

SIGNED WITH THE STEP. The contract document is stored in the step's metadata
and its digest rides in the signed dispatch block
(`metadata.dispatch.work_contract`), so the worker trusts the plugins it reads
only after it has digested the stored document and found the signed digest.

A FIELD OF THE STEP (the owner's pick on PR #3264, 5 November 2026). A caller
names a step's plugins in its own `plugins` field (`schemas.StepPluginCreate`),
never by writing a work contract. `with_step_plugins` checks them here, in
`check_step`'s words pointing at `plugins`, and puts them in the step's work
contract: the caller's own, when it sent one (it may name none or the same
plugins, never others), or else the smallest contract `workcontract.decode`
accepts (`step_contract`): the step's runner profile as its only limit, the
submitter as its owner and the plugins, every component one the worker
enforces, so it is strict and never degrades to advisory. That contract is
what the existing digest in the signed dispatch block binds, so no second
signed field exists. Work contracts are behind SWARM_WORK_CONTRACTS: with it
off a step naming plugins is refused, never stored to run without them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from . import workcontract
from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import RoleAdminRequired, ValidationFailed
from .judgment import (
    MAC_FIELD,
    SETTINGS_COLLECTION,
    SettingsMac,
    block_mac,
    block_verifies,
)
from .plugincatalogue import (
    BRIDGE_REASON,
    CATALOGUE,
    CLAUDE_CODE_PROFILES,
    GSTACK,
    MODES,
    SUPPORTED_MODES,
    PluginEntry,
    offered_skills,
)
from .qasettings import authorize_read, can_edit
from .roles import ADMIN, RoleStore
from .schemas import StrictModel


# --- a step's plugins, at submission ---------------------------------------

#: Where a refusal points, in the caller's document.
FIELD_PATH = "metadata.work_contract.capabilities.plugins"


#: Where a refusal points when the plugins came in the step's own field.
STEP_FIELD = "plugins"


def _refuse(message: str, *, step_id: str | None, field: str = FIELD_PATH,
            **detail: Any) -> ValidationFailed:
    return ValidationFailed(
        message,
        detail={"field": field, "plugins": "plugin_refused", **detail,
                **({"step_id": step_id} if step_id else {})},
    )


def allowed_plugins(tenant_id: str, doc: Mapping[str, Any] | None,
                    mac: SettingsMac) -> tuple[str, ...]:
    """The catalogue plugins the workspace allowed, from its stored block, but
    only when that block's MAC verifies under `mac.keys`: an unsigned or
    forged list allows nothing, for every tenant."""
    block = (doc or {}).get(FIELD) if isinstance(doc, Mapping) else None
    if not isinstance(block, Mapping) or not block_verifies(tenant_id, MAC_BLOCK, block, mac.keys):
        return ()
    names = block.get(ALLOWED)
    if not isinstance(names, list):
        return ()
    return tuple(n for n in names if isinstance(n, str) and n in CATALOGUE)


def check_step(plugins: Mapping[str, Mapping[str, Any]], *, tenant_id: str,
               runner_profile: str, settings_doc: Mapping[str, Any] | None,
               mac: SettingsMac, step_id: str | None = None,
               field: str = FIELD_PATH) -> None:
    """Refuse (422 `validation_failed`, `detail.plugins`) a step's plugins the
    platform cannot run for it: a name outside the catalogue, a profile that
    cannot load it, a mode that needs the question bridge, a skill its mode
    does not offer, no skill at all, or a plugin the workspace has not allowed under a signed
    block. Nothing to check for a step that names none."""
    allowed = allowed_plugins(tenant_id, settings_doc, mac) if plugins else ()
    for name, chosen in sorted(plugins.items()):
        entry = CATALOGUE.get(name)
        if entry is None:
            raise _refuse(f"{name!r} is not in the plugin catalogue", step_id=step_id,
                          field=field, plugin=name, catalogue=sorted(CATALOGUE))
        if runner_profile not in entry.runner_profiles:
            raise _refuse(
                f"{name} runs only on the profiles {', '.join(entry.runner_profiles)}, "
                f"not {runner_profile}", step_id=step_id, field=field, plugin=name,
                runner_profile=runner_profile, profiles=list(entry.runner_profiles))
        mode = chosen.get("mode")
        if mode not in SUPPORTED_MODES:
            raise _refuse(f"{name} in {mode} mode {BRIDGE_REASON}", step_id=step_id,
                          field=field, plugin=name, mode=mode, modes=list(SUPPORTED_MODES))
        offered = entry.skills.get(mode, ())
        extra = sorted(set(chosen.get("skills") or ()) - set(offered))
        if extra:
            raise _refuse(f"{name} in {mode} mode does not offer {', '.join(extra)}",
                          step_id=step_id, field=field, plugin=name, skills=extra,
                          offered=list(offered))
        if not chosen.get("skills"):
            raise _refuse(f"{name} names at least one skill its mode offers", step_id=step_id,
                          field=field, plugin=name, offered=list(offered))
        if name not in allowed:
            raise _refuse(
                f"this workspace has not allowed {name}: its owner or admin allows it "
                "in the workspace's plugins setting", step_id=step_id, field=field, plugin=name,
                tenant_id=tenant_id)


def contract_id_of(step_id: str | None) -> str:
    """`wc-<lowercase name>`: the step id folded into the contract id's alphabet."""
    slug = re.sub(r"[^a-z0-9-]+", "-", (step_id or "task").lower()).lstrip("-")
    if len(slug) < 3:
        slug = f"step-{slug}".rstrip("-")
    return f"wc-{slug[:63]}"


def step_contract(plugins: Mapping[str, Mapping[str, Any]], *, tenant_id: str,
                  step_id: str | None, runner_profile: str, owner: str) -> dict[str, Any]:
    """The smallest work contract `workcontract.decode` accepts that carries
    a step's plugins: the step's runner profile as its one limit, so
    `workcontract.prepare` finds no mismatch, and strict, because every
    component it sets is one the worker enforces."""
    named = "; ".join(f"{n} in {c['mode']} mode, using {', '.join(c['skills'])}"
                      for n, c in sorted(plugins.items()))
    return {
        "schema": workcontract.SCHEMA_VERSION,
        "contract_id": contract_id_of(step_id),
        "tenant_id": tenant_id,
        "revision": 1,
        "previous_digest": None,
        "strictness": workcontract.STRICT,
        "requirements": [{"id": "REQ-1", "text": f"Step {step_id or 'task'} runs with {named}.",
                          "verification": []}],
        "repositories": [],
        "scope": {"read": [], "write": []},
        "protected": {"paths": [], "interfaces": []},
        "capabilities": {"runtime": [],
                         "plugins": {n: {"mode": c["mode"], "skills": list(c["skills"])}
                                     for n, c in sorted(plugins.items())}},
        "limits": {"runner_profile": runner_profile, "model_class": None, "budget": None,
                   "max_turns": None, "timeout_seconds": None},
        "artifacts": [],
        "ownership": {"owner": owner, "depends_on": [], "withdrawn": []},
    }


def _as_set(plugins: Any) -> Any:
    """Plugins compared as `decode` reads them: skills in any order."""
    if not isinstance(plugins, Mapping):
        return plugins
    return {n: (c.get("mode"), frozenset(c.get("skills") or ())) if isinstance(c, Mapping) else c
            for n, c in plugins.items()}


def with_step_plugins(metadata: Mapping[str, Any], plugins: Mapping[str, Mapping[str, Any]], *,
                      tenant_id: str, step_id: str | None, runner_profile: str, owner: str,
                      settings_doc: Mapping[str, Any] | None, mac: SettingsMac,
                      environ: Mapping[str, str]) -> dict[str, Any]:
    """The step's metadata with its `plugins` field in its work contract.

    Refuses (422 `validation_failed`, `detail.plugins`, pointing at
    `plugins`) what `check_step` refuses, a step naming plugins while work
    contracts are off (nothing would sign them, and the worker loads only
    signed plugins), and a contract the caller sent that names other
    plugins than the field. A caller's contract naming none gets them added
    to its capabilities before it is decoded and digested."""
    chosen = {n: {"mode": c.get("mode"), "skills": list(c.get("skills") or ())}
              for n, c in plugins.items()}
    check_step(chosen, tenant_id=tenant_id, runner_profile=runner_profile,
               settings_doc=settings_doc, mac=mac, step_id=step_id, field=STEP_FIELD)
    if not workcontract.enabled(environ):
        raise _refuse(
            "a step's plugins are signed with it in its work contract, and work "
            f"contracts are off on this platform ({workcontract.SWITCH_ENV})",
            step_id=step_id, field=STEP_FIELD)
    out = dict(metadata)
    document = out.get(workcontract.METADATA_KEY)
    if document is None:
        out[workcontract.METADATA_KEY] = step_contract(
            chosen, tenant_id=tenant_id, step_id=step_id,
            runner_profile=runner_profile, owner=owner)
        return out
    capabilities = document.get("capabilities", {}) if isinstance(document, Mapping) else None
    if not isinstance(capabilities, Mapping):
        # Not a contract `decode` accepts: it refuses it in its own words.
        return out
    named = capabilities.get("plugins")
    if named:
        if _as_set(named) != _as_set(chosen):
            raise _refuse(
                "the step's plugins and its work contract's capabilities.plugins "
                "name different plugins; send them in the step's plugins field only",
                step_id=step_id, field=STEP_FIELD)
        return out
    out[workcontract.METADATA_KEY] = {**document,
                                      "capabilities": {**capabilities, "plugins": chosen}}
    return out


# --- the workspace's allowed plugins ----------------------------------------

COLLECTION = SETTINGS_COLLECTION
FIELD = "plugins"
ALLOWED = "allowed"
SET_AT = "set_at"
SET_BY = "set_by"
AUDIT_ACTION = "workspace_plugins_set"
#: The block's name in `swarm_common.specsign.SETTINGS_MAC_FIELDS`.
MAC_BLOCK = FIELD
OWNED_FIELDS = (ALLOWED, SET_AT, SET_BY, MAC_FIELD)
#: None allowed until the workspace's owner or admin allows one.
DEFAULT_ALLOWED: tuple[str, ...] = ()


class PluginsPut(StrictModel):
    #: Catalogue names only; checked against `CATALOGUE` by `normalise`.
    allowed: list[str] = Field(max_length=len(CATALOGUE) + 8)


def normalise(names: Sequence[str]) -> list[str]:
    """Sorted, deduplicated catalogue names; 422 for any other name."""
    unknown = sorted({n for n in names if n not in CATALOGUE})
    if unknown:
        raise ValidationFailed(
            f"{', '.join(unknown)} is not in the plugin catalogue",
            detail={"field": ALLOWED, "unknown": unknown, "catalogue": sorted(CATALOGUE)})
    return sorted(set(names))


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its plugins: its owner, the
    group's admin (read fresh) or a platform admin. 404 for one they are not
    in, 403 for a member who is not its admin."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its plugins. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return tenant


class PluginsStore:
    """`workspace_settings/{tenant}.plugins`, read and audited, signed write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 mac: SettingsMac | None = None) -> None:
        self._db = db
        self._now = now
        self._mac = mac if mac is not None else SettingsMac()

    @property
    def mac(self) -> SettingsMac:
        return self._mac

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def document(self, tenant_id: str) -> dict[str, Any]:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def put(self, tenant_id: str, allowed: Sequence[str], *, by: str) -> None:
        """Store `allowed`, signed, and its audit in one transaction. The same
        list again writes nothing, unless the stored block does not verify
        under the current key: then it is re-signed, with no audit row.

        With no settings key a list is stored unsigned, and `allowed_plugins`
        reads it as none allowed: no step can use a plugin on a deployment
        that cannot sign the choice."""
        wanted = normalise(allowed)
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()
        keys = self._mac.keys

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else None
            previous = list(allowed_plugins(tenant_id, whole, self._mac))
            stored = (whole or {}).get(FIELD)
            stored = dict(stored) if isinstance(stored, Mapping) else {}
            signed = bool(keys) and block_verifies(tenant_id, MAC_BLOCK, stored, keys[:1])
            if previous == wanted and (signed or not keys) and stored.get(ALLOWED) == wanted:
                return
            block = {k: v for k, v in stored.items() if k not in OWNED_FIELDS}
            block.update({ALLOWED: wanted, SET_AT: now, SET_BY: by})
            mac = block_mac(tenant_id, MAC_BLOCK, block, keys)
            if mac is not None:
                block[MAC_FIELD] = mac
            if whole is None:
                txn.set(ref, {"tenant_id": tenant_id, FIELD: block})
            else:
                # The whole map, so a MAC made over other values never stays
                # beside the new ones.
                txn.update(ref, {FIELD: block})
            if previous == wanted:
                return
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "setting": f"{FIELD}.{ALLOWED}",
                           "from": previous, "to": wanted},
            })

        _write(transaction)


def catalogue_view() -> list[dict[str, Any]]:
    """The catalogue as GET answers it: names, versions, profiles and skills.
    The install path is the image's, not a caller's to choose, and is not served."""
    return [
        {"name": e.name, "version": e.version, "runner_profiles": list(e.runner_profiles),
         "modes": {m: {"skills": list(s), "available": m in SUPPORTED_MODES,
                       **({} if m in SUPPORTED_MODES else {"reason": BRIDGE_REASON})}
                   for m, s in e.skills.items()}}
        for e in CATALOGUE.values()
    ]


def view(tenant_id: str, doc: Mapping[str, Any], *, editable: bool,
         mac: SettingsMac) -> dict[str, Any]:
    """What GET and PUT answer: the plugins in force (signed ones only), the
    default, who set them, whether the caller may change them, the catalogue."""
    block = doc.get(FIELD) if isinstance(doc.get(FIELD), Mapping) else {}
    set_at = block.get(SET_AT)
    set_by = block.get(SET_BY)
    return {
        "tenant_id": tenant_id,
        FIELD: {ALLOWED: list(allowed_plugins(tenant_id, doc, mac))},
        "defaults": {FIELD: {ALLOWED: list(DEFAULT_ALLOWED)}},
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
        "catalogue": catalogue_view(),
    }


__all__ = [
    "ALLOWED",
    "AUDIT_ACTION",
    "BRIDGE_REASON",
    "CATALOGUE",
    "CLAUDE_CODE_PROFILES",
    "FIELD",
    "GSTACK",
    "MAC_BLOCK",
    "MODES",
    "PluginEntry",
    "PluginsPut",
    "PluginsStore",
    "STEP_FIELD",
    "SUPPORTED_MODES",
    "allowed_plugins",
    "authorize_read",
    "authorize_write",
    "can_edit",
    "catalogue_view",
    "check_step",
    "contract_id_of",
    "normalise",
    "offered_skills",
    "step_contract",
    "view",
    "with_step_plugins",
]
