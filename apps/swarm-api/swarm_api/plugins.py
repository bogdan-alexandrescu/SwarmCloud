"""Agent plugins: the closed catalogue, a step's plugins checked at submission, and a workspace's allowed plugins.

The owner (3 November 2026) chose plugins per step, gstack first, baked
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

THE PLATFORM SWITCH. `enabled` is the platform's plugins switch (the owner's
pick on PR 3648, docs/design/managed-plugins.md section 11): a platform
admin's signed console setting when one is stored, else SWARM_PLUGINS, the
Terraform value, `on` unless it says exactly `off`. Off, `check_step` refuses
every step that names a plugin, whichever way it came, before anything else
is checked; on, nothing changes. It ships `on`, so it refuses nothing a
caller relied on until an admin turns it off.

MANAGED PLUGINS AND THE WORKSPACE'S DEFAULTS (docs/design/managed-plugins.md
sections 6.4, 7 and 8, lane 7 of section 17). The same field also names
managed installs, by install id, beside the built-ins: no mode, no skills,
only `overrides` for non-secret fields. `merge_step_plugins` runs first at
submission: it refuses a removed install (`pluginstore.refuse_removed`,
`plugin_removed`, the owner's answer to Q5), then computes the step's set
(`pluginconfig.step_plugins`): the workspace's signed every-step defaults,
plus the installs the step adds, minus its `plugins_off`, each pinned to its
install's current version at this moment, with the workspace's values, the
step's overrides and secret references by name. A default the step cannot
run is skipped and recorded, never a refusal (the owner's answer to Q6). The
managed set goes into `metadata.dispatch.plugins` and the skipped defaults
into `metadata.dispatch.plugins_skipped`, inside the dispatch block the step
spec signs whole; managed plugins need no work contract. A built-in default
joins the step's built-ins in unattended mode with every skill that mode
offers, carried in the work contract as before, so with work contracts off
it is skipped and recorded (`work_contracts_off`).

THE ALLOWED LIST, WRITTEN AGAIN. `PluginsStore.put` names catalogue
built-ins and active platform installs (a platform install serves only the
workspaces that allow it). Its block also carries the defaults and the
settings digest (contract request 140), written by `pluginconfig`: when the
stored block verifies they are carried over and signed again with the new
list; when it does not, they are dropped, never signed with a list the
caller chose (the settings lane's recommended answer; the owner has not
answered yet).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Iterable, Mapping, Sequence

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from . import platformswitches, workcontract
from . import pluginstore as ps
from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import RoleAdminRequired, ValidationFailed
from .judgment import (
    MAC_FIELD,
    SETTINGS_COLLECTION,
    SettingsMac,
    block_mac,
    block_verifies,
    unowned_covered_keys,
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


# --- the platform switch ----------------------------------------------------

#: The Terraform value of the platform's plugins switch (var.plugins_switch).
SWITCH_ENV = "SWARM_PLUGINS"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The switch: the platform admin's console setting when one is stored
    and verifies (`platformswitches`), else the Terraform value in `environ`
    (`environment_enabled`)."""
    stored = platformswitches.stored(platformswitches.PLUGINS)
    if stored is not None:
        return stored == "on"
    return environment_enabled(environ)


def environment_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """The Terraform value: on unless SWARM_PLUGINS is exactly `off`, so a
    deployment that sets nothing accepts the plugins it accepted before. The
    read is written `(os.environ if environ is None else environ).get(...)`
    so that scripts/lib/check-env-parity.sh sees swarm-api read the key
    terraform sets."""
    return (os.environ if environ is None else environ).get(SWITCH_ENV, "").strip() != "off"


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


def _platform_id(name: Any) -> bool:
    """A platform install's id (`p-<12 hex>`): the only install an Allowed
    list names, since a workspace's own installs need no opt-in."""
    return isinstance(name, str) and bool(ps.INSTALL_ID.match(name)) and name.startswith("p-")


def allowed_plugins(tenant_id: str, doc: Mapping[str, Any] | None,
                    mac: SettingsMac) -> tuple[str, ...]:
    """The catalogue plugins and platform installs the workspace allowed,
    from its stored block, but only when that block's MAC verifies under
    `mac.keys`: an unsigned or forged list allows nothing, for every tenant."""
    block = (doc or {}).get(FIELD) if isinstance(doc, Mapping) else None
    if not isinstance(block, Mapping) or not block_verifies(tenant_id, MAC_BLOCK, block, mac.keys):
        return ()
    names = block.get(ALLOWED)
    if not isinstance(names, list):
        return ()
    return tuple(n for n in names if isinstance(n, str) and (n in CATALOGUE or _platform_id(n)))


def signed_defaults(tenant_id: str, doc: Mapping[str, Any] | None, mac: SettingsMac) -> tuple[str, ...]:
    """The every-step defaults of the workspace's stored block when its MAC
    verifies; none otherwise. Only a cheap "is there anything to merge"
    test: `pluginconfig.PluginConfigs.defaults` is what the merge reads."""
    block = (doc or {}).get(FIELD) if isinstance(doc, Mapping) else None
    if not isinstance(block, Mapping) or not block_verifies(tenant_id, MAC_BLOCK, block, mac.keys):
        return ()
    names = block.get(DEFAULTS)
    return tuple(n for n in names if isinstance(n, str)) if isinstance(names, list) else ()


def check_step(plugins: Mapping[str, Mapping[str, Any]], *, tenant_id: str,
               runner_profile: str, settings_doc: Mapping[str, Any] | None,
               mac: SettingsMac, step_id: str | None = None,
               field: str = FIELD_PATH) -> None:
    """Refuse (422 `validation_failed`, `detail.plugins`) a step's plugins the
    platform cannot run for it: a name outside the catalogue, a profile that
    cannot load it, a mode that needs the question bridge, a skill its mode
    does not offer, no skill at all, or a plugin the workspace has not allowed under a signed
    block; and any plugin at all while the platform's plugins switch is off
    (`enabled`). Nothing to check for a step that names none."""
    if plugins and not enabled():
        raise _refuse(
            "plugins are off on this platform: a platform admin turns them on in "
            f"Admin › Plugins › Platform ({SWITCH_ENV})", step_id=step_id, field=field,
            plugins=sorted(plugins))
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


# --- managed plugins and the workspace's defaults, merged at submission ------

#: Where the step's managed plugin set goes, inside the signed dispatch block
#: (`metadata.dispatch.plugins`, docs/design/managed-plugins.md section 8),
#: and the defaults it skipped (section 7, the owner's answer to Q6).
DISPATCH_PLUGINS = "plugins"
DISPATCH_SKIPPED = "plugins_skipped"
#: A built-in default rides in the work contract, in this mode, with every
#: skill the mode offers: a default names no skills of its own.
DEFAULT_MODE = "unattended"
#: A built-in default skipped because work contracts are off (nothing would
#: sign it), or because the caller's own contract names other plugins.
SKIP_CONTRACTS_OFF = "work_contracts_off"
SKIP_CONTRACT_NAMES_PLUGINS = "work_contract_names_plugins"
#: A default skipped because the platform's plugins switch is off (`enabled`).
SKIP_PLATFORM_OFF = "plugins_off_on_platform"


@dataclass(frozen=True)
class StepPlugins:
    """A step's merged plugins: `built_ins` for its `plugins` field (and so
    its work contract), `managed` for `metadata.dispatch.plugins` (install
    id -> the signed entry, pinned to a version), `skipped` defaults."""

    built_ins: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    managed: dict[str, dict[str, Any]] = dataclass_field(default_factory=dict)
    skipped: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def record_on(self, dispatch_block: dict[str, Any]) -> None:
        """Write the set into the dispatch block, only when there is one: a
        step with no managed plugin and nothing skipped is byte for byte what
        it was before managed plugins."""
        if self.managed:
            dispatch_block[DISPATCH_PLUGINS] = {k: self.managed[k] for k in sorted(self.managed)}
        if self.skipped:
            dispatch_block[DISPATCH_SKIPPED] = list(self.skipped)


def _with_step(exc: ValidationFailed, step_id: str | None) -> ValidationFailed:
    """The refusal, pointing at the step's `plugins` field and naming the step."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        detail.setdefault("field", STEP_FIELD)
        if step_id:
            detail.setdefault("step_id", step_id)
    return exc


def merge_step_plugins(chosen: Mapping[str, Mapping[str, Any]], off: Iterable[str], *,
                       tenant_id: str, runner_profile: str, step_id: str | None,
                       settings_doc: Mapping[str, Any] | None, mac: SettingsMac,
                       store: Any, configs: Any, environ: Mapping[str, str],
                       caller_contract_plugins: bool = False) -> StepPlugins:
    """The step's plugins after the workspace's defaults are merged in.

    `chosen` is the step's `plugins` field (name -> `{mode, skills,
    overrides}`), `off` its `plugins_off`. `store` is a
    `pluginstore.PluginStore`, `configs` a `pluginconfig.PluginConfigs`.
    Refuses (422) a removed install the step names (`plugin_removed`, first),
    a managed install named with a mode or skills, a built-in named with
    overrides or without a mode, a name both loaded and turned off, an
    install the step adds and cannot run, and more than eight plugins in all.
    With the platform's plugins switch off (`enabled`) it refuses a step
    naming any plugin and skips every default.
    `caller_contract_plugins`: the caller sent a work contract naming
    plugins, so a built-in default is skipped rather than refused as a
    contract that names different plugins than the field."""
    # Imported here: pluginconfig imports this module's constants.
    from . import pluginconfig as pcfg

    # The platform's plugins switch, before anything else: off, a step naming
    # a plugin either way is refused, as `check_step` refuses it, and the
    # workspace's defaults are skipped and recorded, never a refusal.
    if not enabled():
        if chosen:
            raise _refuse(
                "plugins are off on this platform: a platform admin turns them on in "
                f"Admin › Plugins › Platform ({SWITCH_ENV})", step_id=step_id,
                field=STEP_FIELD, plugins=sorted(chosen))
        return StepPlugins(skipped=[
            {"install_id": name, "code": SKIP_PLATFORM_OFF}
            for name in signed_defaults(tenant_id, settings_doc, mac) if name not in set(off)])
    try:
        ps.refuse_removed(store, tenant_id, list(chosen), field=STEP_FIELD)
    except ValidationFailed as exc:
        raise _with_step(exc, step_id) from None
    turned_off = sorted({n for n in off if isinstance(n, str)})
    both = sorted(set(turned_off) & set(chosen))
    if both:
        raise _refuse(f"{', '.join(both)} is both loaded and turned off for this step",
                      step_id=step_id, field=STEP_FIELD, plugins_off=both)
    built_ins: dict[str, dict[str, Any]] = {}
    added: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(chosen.items()):
        if ps.INSTALL_ID.match(name):
            if entry.get("mode") is not None or entry.get("skills") is not None:
                raise _refuse(f"{name} is a managed plugin: it loads every component of its pinned "
                              "version and takes no mode or skills", step_id=step_id,
                              field=STEP_FIELD, plugin=name)
            added[name] = {"overrides": dict(entry.get("overrides") or {})}
            continue
        if entry.get("overrides"):
            raise _refuse(f"{name} is a built-in plugin and has no settings to override",
                          step_id=step_id, field=STEP_FIELD, plugin=name)
        if entry.get("mode") is None:
            raise _refuse(f"{name} names a mode ({', '.join(SUPPORTED_MODES)}) and the skills it uses",
                          step_id=step_id, field=STEP_FIELD, plugin=name,
                          modes=list(SUPPORTED_MODES))
        built_ins[name] = {"mode": entry.get("mode"), "skills": list(entry.get("skills") or ())}
    if not added and not signed_defaults(tenant_id, settings_doc, mac):
        return StepPlugins(built_ins=built_ins)
    try:
        merged = pcfg.step_plugins(
            store, configs, tenant_id=tenant_id, runner_profile=runner_profile,
            allowed=allowed_plugins(tenant_id, settings_doc, mac), added=added,
            # A built-in the step names itself keeps the step's own mode and
            # skills: never a second, default copy of it.
            off=[*turned_off, *built_ins])
    except ValidationFailed as exc:
        raise _with_step(exc, step_id) from None
    skipped = list(merged["skipped"])
    for name in merged["built_ins"]:
        if caller_contract_plugins:
            skipped.append({"install_id": name, "code": SKIP_CONTRACT_NAMES_PLUGINS})
        elif not workcontract.enabled(environ):
            skipped.append({"install_id": name, "code": SKIP_CONTRACTS_OFF})
        else:
            built_ins[name] = {"mode": DEFAULT_MODE,
                               "skills": list(CATALOGUE[name].skills.get(DEFAULT_MODE, ()))}
    if len(built_ins) + len(merged["plugins"]) > pcfg.MAX_PLUGINS:
        raise _with_step(pcfg.ConfigRefused("too_many_plugins", runner_profile=runner_profile), step_id)
    return StepPlugins(built_ins=built_ins, managed=dict(merged["plugins"]), skipped=skipped)


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
#: The block's keys `pluginconfig` writes (contract request 140): the
#: every-step defaults and the settings digest, both covered by the MAC, and
#: the uncovered map of per-plugin digests the settings digest is made from.
#: Restated here because pluginconfig imports this module; a unit test holds
#: them equal to `pluginconfig.DEFAULTS`, `CONFIGS_DIGEST`, `CONFIG_DIGESTS`.
DEFAULTS = "defaults"
CONFIGS_DIGEST = "configs_digest"
CONFIG_DIGESTS = "config_digests"
#: Carried over when the Allowed list is written again under a block that
#: verifies; dropped with it when it does not.
CARRIED_FIELDS = (DEFAULTS, CONFIGS_DIGEST, CONFIG_DIGESTS)
#: None allowed until the workspace's owner or admin allows one.
DEFAULT_ALLOWED: tuple[str, ...] = ()
#: The longest Allowed list: the built-ins and up to 64 platform installs, a
#: bound on one document's size, far above any catalogue in sight.
MAX_ALLOWED = len(CATALOGUE) + 64


class PluginsPut(StrictModel):
    #: Catalogue names and platform install ids; checked by `normalise`.
    allowed: list[str] = Field(max_length=MAX_ALLOWED)


def normalise(names: Sequence[str], store: Any = None) -> list[str]:
    """Sorted, deduplicated catalogue names and active platform install ids
    (`store`, a `pluginstore.PluginStore`, reads each); 422 for any other
    name. A removed platform install is dropped: removal takes it out of
    every Allowed list, so a list read before the removal still saves."""
    kept: set[str] = set()
    unknown: set[str] = set()
    for name in names:
        if name in CATALOGUE:
            kept.add(name)
            continue
        install = store.install(name) if store is not None and _platform_id(name) else None
        if install is None or install.get("scope") != ps.PLATFORM:
            unknown.add(name)
        elif install.get("state") == ps.STATE_ACTIVE:
            kept.add(name)
    if unknown:
        listed = sorted(unknown)
        raise ValidationFailed(
            f"{', '.join(listed)} is not in the plugin catalogue",
            detail={"field": ALLOWED, "unknown": listed, "catalogue": sorted(CATALOGUE)})
    return sorted(kept)


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
        wanted = normalise(allowed, ps.PluginStore(self._db, now=self._now, mac=self._mac))
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
            # The defaults and the settings digest (contract request 140)
            # are carried over, signed again with the new list, only when
            # the stored block verifies under a key this deployment holds:
            # then pluginconfig wrote them. When it does not, they may be a
            # direct write's, and are dropped, never signed with the list the
            # caller chose. The per-plugin map goes with the digest made
            # from it. A covered key no writer writes is dropped either way.
            verified = bool(keys) and block_verifies(tenant_id, MAC_BLOCK, stored, keys)
            dropped = (*OWNED_FIELDS, *unowned_covered_keys(MAC_BLOCK))
            if not verified or stored.get(CONFIGS_DIGEST) is None:
                dropped = (*dropped, CONFIGS_DIGEST, CONFIG_DIGESTS)
            if not verified:
                dropped = (*dropped, DEFAULTS)
            block = {k: v for k, v in stored.items() if k not in dropped}
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
    "CARRIED_FIELDS",
    "CATALOGUE",
    "CLAUDE_CODE_PROFILES",
    "DISPATCH_PLUGINS",
    "DISPATCH_SKIPPED",
    "FIELD",
    "GSTACK",
    "MAC_BLOCK",
    "MODES",
    "PluginEntry",
    "PluginsPut",
    "PluginsStore",
    "STEP_FIELD",
    "SUPPORTED_MODES",
    "StepPlugins",
    "allowed_plugins",
    "authorize_read",
    "authorize_write",
    "can_edit",
    "catalogue_view",
    "check_step",
    "contract_id_of",
    "enabled",
    "environment_enabled",
    "merge_step_plugins",
    "normalise",
    "offered_skills",
    "signed_defaults",
    "step_contract",
    "view",
    "with_step_plugins",
]
