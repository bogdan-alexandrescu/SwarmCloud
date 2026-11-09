"""A managed plugin's settings and secrets, per workspace, and the workspace's defaults.

docs/design/managed-plugins.md sections 4.4, 6 and 7 (lane 6 of section 17),
with the owner's answers of 6 November 2026: plugin secrets are per workspace
only (Q2: no platform-held secret a platform plugin shares across
workspaces), and a default a step's runner profile cannot run is skipped
for that step and recorded, never a refusal (Q6).

THE FORM (section 6.1). A version's fields are its manifest's `userConfig`
(the intake job's `manifest.user_config`) followed by the overlay's declared
settings (`overlay.settings`, section 6.3); an overlay adds fields, never
replaces one the manifest declares. Each field has a kind:

    value    a string, a number, a boolean (or a list of them, `multiple`)
    path     a `directory` or `file` path inside the checkout: relative, no `..`
    secret   `sensitive: true`: write-only, entered through a one-time link, or a
             binding to a workspace key
    file     an overlay setting whose target is a file: its content is
             write-only too, released to the step like a secret (section 8)

and the variable the launcher sets for it (`swarm-plugin-exec`, section
4.4): `CLAUDE_PLUGIN_OPTION_<KEY>` for a `userConfig` field, the declared
name for an overlay setting, never one on section 6.3's deny list.

WHERE VALUES LIVE (section 6.2). `workspace_settings/{tenant}/plugin_configs/
{install_id}` holds, per field, a non-secret `value`, `secret: {kind:
"plugin", ...}`, `binding: {kind: "provider_key", provider}` or `file:
{sha256, bytes, ...}`: never a secret's value, and a file's digest only (a
rules file, not a key). A tenant identity can write that document, so each
document's canonical bytes are digested into `config_digests` beside the
workspace's signed `plugins` block, and the sorted map's digest is the
block's `configs_digest`, which the block's MAC covers (contract request
140, P1). A document whose digest is not the signed one is read as no
values. A save names the `revision` it read: anything else is a conflict.

SECRET VALUES live in Secret Manager under `Tenant.plugin_secret_name
(install_id, field)` (contract request 142, P3): derived from the
workspace, the plugin and the field, never from anything a caller types.
They are written here, never read here: no function of this module returns
a value, and the only reader is the attempt release (routes/attempts_secrets.py),
for an attempt whose signed plugin set names the plugin.

DEFAULTS (section 7). `plugins.defaults` in the signed block: install ids
(or a built-in's name) the workspace runs on every step. `step_plugins`
resolves a step's set at submission: the defaults, plus what the step adds,
minus what it turns off, each at its install's current version, with the
workspace's values, the step's non-secret overrides and the secret
references. A default the step cannot run is skipped and recorded; a plugin
the step itself names and cannot run is refused.

Pure apart from the Firestore client and the Secret Manager writer it is
handed: no network, no clock but `now`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Protocol

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant, consumer_name
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.specsign import SpecNotCanonical, jcs

from . import pluginstore as ps
from .admins import AUDIT_COLLECTION
from .codec import console_origin
from .errors import UpstreamUnavailable, ValidationFailed
from .judgment import MAC_FIELD, SettingsMac, block_mac, block_verifies
from .plugincatalogue import BUILT_IN_PREFIX, builtin_entries
from .plugins import ALLOWED, COLLECTION, FIELD

log = logging.getLogger(__name__)

#: `workspace_settings/{tenant}/plugin_configs/{install_id}`.
CONFIGS = "plugin_configs"
#: One document per minted secret link, keyed by sha256(token). Never the token.
LINKS = "plugin_secret_links"

#: The signed block's keys this module writes (contract request 140), and
#: the uncovered map whose digest is `configs_digest`.
DEFAULTS = "defaults"
CONFIGS_DIGEST = "configs_digest"
CONFIG_DIGESTS = "config_digests"

#: Ten minutes, one use, as every secret form link (secrets-scopes.md section 6.3).
LINK_TTL = timedelta(minutes=10)

#: Section 13, invariant 7: at most 8 plugins in one step.
MAX_PLUGINS = 8
MAX_VALUE_CHARS = 4096
MAX_LIST_ITEMS = 64
MAX_PATH_CHARS = 1024
#: A secret is at most 8 KiB (the store's bound); a file's content at most
#: 64 KiB, Secret Manager's payload limit.
MAX_SECRET_BYTES = 8 * 1024
MAX_FILE_BYTES = 64 * 1024

KIND_VALUE = "value"
KIND_PATH = "path"
KIND_SECRET = "secret"
KIND_FILE = "file"

#: The keys a workspace may bind a secret field to (section 6.2): a closed
#: list, so a binding never names an arbitrary secret.
BINDABLE = ("typesafe",)
BINDING_KIND = "provider_key"

#: What the intake job puts before every hook and server command (section 4.4).
LAUNCHER = "/opt/swarm/bin/swarm-plugin-exec"
OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"

AUDIT_CONFIG_SET = "plugin_config_set"
AUDIT_SECRET_SET = "plugin_secret_set"
AUDIT_SECRET_REMOVED = "plugin_secret_removed"
AUDIT_SECRET_BOUND = "plugin_secret_bound"
AUDIT_DEFAULTS_SET = "plugin_defaults_set"
AUDIT_ACTIONS = (AUDIT_CONFIG_SET, AUDIT_SECRET_SET, AUDIT_SECRET_REMOVED, AUDIT_SECRET_BOUND,
                 AUDIT_DEFAULTS_SET)

_KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_USER_CONFIG_TYPES = frozenset({"string", "number", "boolean", "directory", "file"})
_OVERLAY_TYPES = frozenset({"string", "number", "boolean", "file"})

#: Section 6.3's deny list, checked at every settings write and override as
#: the intake job checks it at install (apps/plugin-intake overlay.py,
#: `variable_denied`, which swarm-api cannot import): a setting cannot
#: redirect the runtime that runs the hook.
_DENIED_EXACT = frozenset({"PATH", "HOME", "NODE_OPTIONS"})
_DENIED_PREFIXES = ("LD_", "PYTHON", "SWARM_", "GOOGLE_", "GIT_")

REFUSALS: dict[str, str] = {
    "settings_key_missing": "This deployment has no settings key, so no plugin setting can be saved.",
    "plugin_not_found": "The plugin is not in this workspace's catalogue.",
    "plugin_has_no_settings": "A built-in plugin has no settings.",
    "form_changed": "The plugin's current version changed since its form was read; read it again.",
    "config_conflict": "The settings changed since they were read; read them again before saving.",
    "plugins_block_unverified": "The workspace's signed plugin settings do not verify; set its Allowed list again first.",
    "field_unknown": "The plugin declares no setting of that name.",
    "field_write_only": "A secret or file setting is set through its one-time link, never with the other values.",
    "field_not_write_only": "That setting is not a secret or file setting.",
    "field_not_bindable": "Only a secret setting can use a workspace key.",
    "value_invalid": "The value is not of the setting's type.",
    "path_invalid": "A path setting is a relative path inside the checkout, with no '..'.",
    "variable_denied": "The setting's variable is on the deny list.",
    "secret_size": "A secret is 1 byte to 8 KiB, a file 1 byte to 64 KiB.",
    "secret_link_invalid": "This link is unknown, used, expired, or was minted for someone else or another setting.",
    "secret_not_set": "That setting holds no secret.",
    "binding_provider_refused": "That key cannot be bound to a plugin.",
    "binding_needs_node": "A plugin bound to the workspace's TypeSafe key runs only node hooks and servers.",
    "default_not_allowed": "A default must be a plugin this workspace allows or installed.",
    "too_many_plugins": "A step runs at most 8 plugins.",
    "plugin_removed": "The plugin was removed from the catalogue.",
    "plugin_not_allowed": "The workspace does not allow that plugin.",
    "profile_cannot_load_plugins": "The step's runner profile cannot load Claude Code plugins.",
    "plugin_runtime_missing": "The step's runner profile lacks a runtime the plugin needs.",
    "plugin_profile_unsupported": "The plugin does not support the step's runner profile.",
    "plugin_setting_missing": "A required setting of the plugin has no value in this workspace.",
    "override_refused": "A step overrides only a plugin's non-secret values.",
}


class ConfigRefused(ValidationFailed):
    """A refusal by fixed code: the sentence is `REFUSALS[code]`, and the only
    detail is platform-minted ids and field names, never a value."""

    def __init__(self, code: str, *, status: int = 422, **detail: Any) -> None:
        if code not in REFUSALS:
            raise KeyError(code)
        super().__init__(REFUSALS[code], detail={k: v for k, v in detail.items() if v is not None})
        self.code = code
        self.status_code = status


def variable_denied(name: str) -> bool:
    if name in _DENIED_EXACT or name.startswith(_DENIED_PREFIXES):
        return True
    return name.startswith("CLAUDE_") and not name.startswith(OPTION_PREFIX)


def _iso(when: datetime) -> str:
    return when.isoformat()


# --- the form (sections 6.1 and 6.3) ------------------------------------------


def _option_variable(key: str) -> str:
    return OPTION_PREFIX + re.sub(r"[^A-Z0-9_]", "_", key.upper())


def form_fields(version: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every settings field of `version`, in the form's order: `userConfig`
    first, then the overlay's declared settings it does not already name.
    A field whose shape the intake job would not have written is left out."""
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    manifest = version.get("manifest") or {}
    for spec in manifest.get("user_config") or () if isinstance(manifest, Mapping) else ():
        if not isinstance(spec, Mapping):
            continue
        key, kind_of = spec.get("key"), spec.get("type")
        if not isinstance(key, str) or not _KEY.match(key) or key in seen or kind_of not in _USER_CONFIG_TYPES:
            continue
        sensitive = spec.get("sensitive") is True
        kind = KIND_SECRET if sensitive else KIND_PATH if kind_of in ("directory", "file") else KIND_VALUE
        fields.append({
            "key": key, "source": "user_config", "type": kind_of, "kind": kind,
            "title": spec.get("title") if isinstance(spec.get("title"), str) else None,
            "description": spec.get("description") if isinstance(spec.get("description"), str) else None,
            "required": spec.get("required") is True, "multiple": spec.get("multiple") is True,
            "sensitive": sensitive, "default": None if sensitive else spec.get("default"),
            "target": "env", "variable": _option_variable(key),
            "bindable": list(BINDABLE) if sensitive else [],
        })
        seen.add(key)
    overlay = version.get("overlay") or {}
    for spec in overlay.get("settings") or () if isinstance(overlay, Mapping) else ():
        if not isinstance(spec, Mapping):
            continue
        key, kind_of, variable = spec.get("key"), spec.get("type"), spec.get("variable")
        target = spec.get("target", "env")
        if (not isinstance(key, str) or not _KEY.match(key) or key in seen or kind_of not in _OVERLAY_TYPES
                or target not in ("env", "file") or not isinstance(variable, str)
                or not _VARIABLE.match(variable) or variable_denied(variable)):
            continue
        sensitive = spec.get("sensitive") is True
        kind = (KIND_SECRET if sensitive else KIND_FILE if target == "file"
                else KIND_PATH if kind_of == "file" else KIND_VALUE)
        fields.append({
            "key": key, "source": "overlay", "type": kind_of, "kind": kind, "title": None,
            "description": spec.get("description") if isinstance(spec.get("description"), str) else None,
            # A declared setting the admin wrote is required when it is a secret
            # or a file: the plugin reads it, and has no default to fall back on.
            "required": kind in (KIND_SECRET, KIND_FILE), "multiple": False,
            "sensitive": sensitive, "default": None, "target": target, "variable": variable,
            "bindable": list(BINDABLE) if sensitive else [],
        })
        seen.add(key)
    return fields


def form(install: Mapping[str, Any], version: Mapping[str, Any]) -> dict[str, Any]:
    """The form a console draws for `install` at `version`."""
    fields = form_fields(version)
    sources = sorted({f["source"] for f in fields})
    return {
        "install_id": install.get("install_id"),
        "version_id": version.get("version_id"),
        "fields": fields,
        "source": sources[0] if len(sources) == 1 else "both" if sources else "none",
    }


def _fields_by_key(version: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["key"]: f for f in form_fields(version)}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _scalar_ok(field: Mapping[str, Any], value: Any) -> bool:
    kind_of = field["type"]
    if kind_of == "boolean":
        return isinstance(value, bool)
    if kind_of == "number":
        return _finite(value)
    return isinstance(value, str) and len(value) <= MAX_VALUE_CHARS and "\x00" not in value


def _path_ok(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        return False
    if "\x00" in value or "\\" in value or value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", value):
        return False
    return ".." not in value.split("/")


def check_value(field: Mapping[str, Any], value: Any) -> Any:
    """`value` for a non-secret `field`, checked; refused by fixed code, the
    field's key the only detail (the value is never repeated)."""
    key = field["key"]
    if field["kind"] in (KIND_SECRET, KIND_FILE):
        raise ConfigRefused("field_write_only", field=key)
    if variable_denied(str(field.get("variable") or "")):
        raise ConfigRefused("variable_denied", field=key)
    items = value if field.get("multiple") else [value]
    if field.get("multiple") and (not isinstance(value, list) or len(value) > MAX_LIST_ITEMS):
        raise ConfigRefused("value_invalid", field=key)
    for item in items:
        if field["kind"] == KIND_PATH:
            if not _path_ok(item):
                raise ConfigRefused("path_invalid", field=key)
        elif not _scalar_ok(field, item):
            raise ConfigRefused("value_invalid", field=key)
    return list(value) if field.get("multiple") else value


def env_text(value: Any) -> str:
    """A value as the plugin's environment carries it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return str(value)


# --- the digests the signed block covers (contract request 140) ----------------

_DIGESTED = ("install_id", "tenant_id", "version_id", "revision", "fields")


def config_digest(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(jcs({k: doc.get(k) for k in _DIGESTED})).hexdigest()


def configs_digest(digests: Mapping[str, str]) -> str | None:
    """The block's `configs_digest`: None for a workspace with no settings,
    so a block stored before this lane still verifies."""
    if not digests:
        return None
    return hashlib.sha256(jcs(dict(sorted(digests.items())))).hexdigest()


# --- Secret Manager: write-only ------------------------------------------------


class PluginSecretWriter(Protocol):
    def create_secret(self, tenant_id: str, install_id: str, secret_id: str) -> None: ...

    def add_version(self, secret_id: str, payload: bytes) -> str: ...

    def destroy_versions(self, secret_id: str) -> int: ...


class SecretManagerPluginSecrets:
    """A plugin secret's Secret Manager writes, and nothing else.

    NO READ PATH and NO IAM WRITE: the tenant's worker account holds no grant
    on a `-plg-` secret (section 6.2), so a step that does not load the
    plugin cannot read it even in the same workspace; swarm-api's release is
    the one reader. Labelled `managed-by=swarm-api`, `tenant`, `plugin`.
    Builds no client until the first call, so `create_app()` needs no
    credentials."""

    def __init__(self, project_id: str, *, client: Any | None = None) -> None:
        if not project_id:
            raise ValueError("project_id is required")
        self._project_id = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self._project_id}"

    def create_secret(self, tenant_id: str, install_id: str, secret_id: str) -> None:
        from google.api_core import exceptions as gexc

        try:
            self._secret_client().create_secret(request={
                "parent": self.parent, "secret_id": secret_id,
                "secret": {"replication": {"automatic": {}},
                           "labels": {"managed-by": "swarm-api", "tenant": tenant_id,
                                      "plugin": install_id, "purpose": "plugin-secret"}},
            })
        except gexc.AlreadyExists:
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not create the plugin secret ({type(exc).__name__})") from None

    def add_version(self, secret_id: str, payload: bytes) -> str:
        from google.api_core import exceptions as gexc
        from google.cloud import secretmanager

        name = f"{self.parent}/secrets/{secret_id}"
        try:
            version = self._secret_client().add_secret_version(request={
                "parent": name, "payload": secretmanager.SecretPayload(data=payload)})
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not store the value ({type(exc).__name__})") from None
        return str(getattr(version, "name", "") or f"{name}/versions/latest")

    def destroy_versions(self, secret_id: str) -> int:
        from google.api_core import exceptions as gexc

        client = self._secret_client()
        name = f"{self.parent}/secrets/{secret_id}"
        destroyed = 0
        try:
            for version in list(client.list_secret_versions(
                    request={"parent": name, "filter": "state:ENABLED OR state:DISABLED"})):
                client.destroy_secret_version(request={"name": version.name})
                destroyed += 1
        except gexc.NotFound:
            pass
        except gexc.GoogleAPICallError as exc:
            raise UpstreamUnavailable(f"could not destroy the plugin secret ({type(exc).__name__})") from None
        return destroyed


def secret_name(tenant_id: str, install_id: str, field: str) -> str:
    """The frozen contract's name for a plugin secret (contract request 142)."""
    return Tenant(tenant_id=tenant_id, kind="group", principal="", created_at=datetime.min
                  ).plugin_secret_name(install_id, field)


def plugin_consumer(install_id: str) -> str:
    """`plugin:<install_id>`, the consumer a released plugin secret is recorded for."""
    return consumer_name("plugin", install_id)


# --- bindings (section 6.2) ---------------------------------------------------


def _runs_node(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    words = command.split()
    if words[0] == LAUNCHER:
        words = words[words.index("--") + 1:] if "--" in words else []
    return bool(words) and words[0].rsplit("/", 1)[-1] == "node"


def node_only(version: Mapping[str, Any]) -> bool:
    """Every hook and MCP server command of `version` is a `node` command:
    the Jev adapter wraps only those (section 6.2, "A binding brings its adapter")."""
    components = version.get("components") or {}
    overlay_hooks = ((version.get("overlay") or {}).get("hooks") or {})
    commands: list[Any] = [h.get("command") for h in components.get("hooks") or () if isinstance(h, Mapping)]
    commands += [s.get("command") for s in components.get("mcp_servers") or ()
                 if isinstance(s, Mapping) and s.get("command")]
    for groups in (overlay_hooks.get("hooks") or {}).values() if isinstance(overlay_hooks, Mapping) else ():
        for group in groups or ():
            for hook in (group or {}).get("hooks") or ():
                if isinstance(hook, Mapping):
                    commands.append(hook.get("command"))
    return all(_runs_node(c) for c in commands)


# --- the store ---------------------------------------------------------------


def _audit_row(action: str, *, by: str, at: datetime, tenant_id: str,
               detail: Mapping[str, Any]) -> dict[str, Any]:
    return {"action": action, "by": by, "at": at, "target_tenant_id": tenant_id,
            "detail": {"tenant_id": tenant_id, **detail}}


@dataclass(frozen=True)
class Block:
    """The workspace's `plugins` block as a writer may carry it: `verified`
    when its MAC verifies (or nothing is stored), else its values are not
    this module's to sign again."""

    stored: dict[str, Any]
    exists: bool
    verified: bool

    @property
    def digests(self) -> dict[str, str]:
        found = self.stored.get(CONFIG_DIGESTS)
        if not isinstance(found, Mapping):
            return {}
        return {k: v for k, v in found.items() if isinstance(k, str) and isinstance(v, str)}

    @property
    def defaults(self) -> list[str]:
        found = self.stored.get(DEFAULTS)
        return [n for n in found if isinstance(n, str)] if isinstance(found, list) else []

    @property
    def allowed(self) -> list[str]:
        found = self.stored.get(ALLOWED)
        return [n for n in found if isinstance(n, str)] if isinstance(found, list) else []


def _covered_empty(block: Mapping[str, Any]) -> bool:
    return all(block.get(k) in (None, [], {}) for k in (ALLOWED, DEFAULTS, CONFIGS_DIGEST))


class PluginConfigs:
    """Settings values, secret records, bindings, links and defaults, each
    write audited in the transaction that makes it."""

    def __init__(self, db: Any, *, now: Callable[[], datetime], mac: SettingsMac) -> None:
        self._db = db
        self._now = now
        self._mac = mac

    def require_key(self) -> None:
        if not self._mac.keys:
            raise ConfigRefused("settings_key_missing", status=503)

    # refs
    def _settings_ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def _config_ref(self, tenant_id: str, install_id: str) -> Any:
        return self._settings_ref(tenant_id).collection(CONFIGS).document(install_id)

    def _link_ref(self, token: str) -> Any:
        return self._db.collection(LINKS).document(hashlib.sha256(token.encode("utf-8")).hexdigest())

    def _audit_ref(self) -> Any:
        return self._db.collection(AUDIT_COLLECTION).document()

    # reads
    def _block_of(self, tenant_id: str, snap: Any) -> Block:
        whole = (snap.to_dict() or {}) if snap.exists else {}
        stored = whole.get(FIELD)
        if not isinstance(stored, Mapping):
            return Block({}, snap.exists, True)
        stored = dict(stored)
        if MAC_FIELD not in stored and _covered_empty(stored) and not stored.get(CONFIG_DIGESTS):
            return Block(stored, snap.exists, True)
        signed = block_verifies(tenant_id, FIELD, stored, self._mac.keys)
        if signed and stored.get(CONFIGS_DIGEST) is None:
            # Signed with no settings (the Allowed list's writer re-signs
            # without them): the uncovered map is stale, so every document
            # reads as none and the next save starts again from revision 0.
            stored.pop(CONFIG_DIGESTS, None)
            return Block(stored, snap.exists, True)
        return Block(stored, snap.exists, signed
                     and stored.get(CONFIGS_DIGEST) == configs_digest(Block(stored, True, True).digests))

    def block(self, tenant_id: str) -> Block:
        return self._block_of(tenant_id, self._settings_ref(tenant_id).get())

    def _config_of(self, tenant_id: str, install_id: str, block: Block, snap: Any) -> dict[str, Any] | None:
        """The verified config document; an empty one when nothing is stored;
        None when what is stored is not what the signed block names."""
        want = block.digests.get(install_id)
        if not snap.exists:
            return _empty_config(tenant_id, install_id) if want is None else None
        doc = snap.to_dict() or {}
        if want is None or not block.verified:
            return None
        try:
            ok = config_digest(doc) == want
        except SpecNotCanonical:
            ok = False
        if not ok or doc.get("install_id") != install_id or doc.get("tenant_id") != tenant_id:
            return None
        return doc

    def config(self, tenant_id: str, install_id: str) -> dict[str, Any]:
        """The workspace's settings for `install_id`; no values when what is
        stored does not verify (logged by id, never by value)."""
        block = self.block(tenant_id)
        found = self._config_of(tenant_id, install_id, block, self._config_ref(tenant_id, install_id).get())
        if found is None:
            log.warning("plugin settings unverified tenant=%s install=%s: read as none", tenant_id, install_id)
            return _empty_config(tenant_id, install_id)
        return found

    def defaults(self, tenant_id: str) -> list[str]:
        block = self.block(tenant_id)
        return block.defaults if block.verified else []

    # writes
    def _write_block(self, txn: Any, tenant_id: str, block: Block, *, digests: Mapping[str, str] | None = None,
                     defaults: list[str] | None = None) -> None:
        stored = {k: v for k, v in block.stored.items() if k != MAC_FIELD}
        if digests is not None:
            stored[CONFIG_DIGESTS] = dict(sorted(digests.items()))
            stored[CONFIGS_DIGEST] = configs_digest(digests)
        if defaults is not None:
            stored[DEFAULTS] = defaults
        stored[MAC_FIELD] = block_mac(tenant_id, FIELD, stored, self._mac.keys)
        ref = self._settings_ref(tenant_id)
        if block.exists:
            txn.update(ref, {FIELD: stored})
        else:
            txn.set(ref, {"tenant_id": tenant_id, FIELD: stored})

    def _mutate(self, tenant_id: str, install_id: str, *, version_id: str, by: str,
                change: Callable[[dict[str, Any]], dict[str, Any] | None], action: str,
                revision: int | None = None) -> dict[str, Any]:
        """Read the block and the config, check, `change` the fields (None:
        nothing to write), re-digest, re-sign and audit, in one transaction."""
        self.require_key()
        now = self._now()
        settings_ref, config_ref, audit_ref = (self._settings_ref(tenant_id),
                                               self._config_ref(tenant_id, install_id), self._audit_ref())
        result: dict[str, Any] = {}
        refused: list[str] = []
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            block = self._block_of(tenant_id, _snapshot(txn.get(settings_ref)))
            if not block.verified:
                refused.append("plugins_block_unverified")
                return
            current = self._config_of(tenant_id, install_id, block, _snapshot(txn.get(config_ref)))
            if current is None:
                # A planted document is not carried over: it is replaced, from revision 0.
                current = _empty_config(tenant_id, install_id)
            if revision is not None and int(current.get("revision") or 0) != revision:
                refused.append("config_conflict")
                return
            fields = {k: dict(v) for k, v in (current.get("fields") or {}).items() if isinstance(v, Mapping)}
            detail = change(fields)
            if detail is None:
                result.update(current)
                return
            doc = {"install_id": install_id, "tenant_id": tenant_id, "version_id": version_id,
                   "revision": int(current.get("revision") or 0) + 1, "fields": fields,
                   "set_at": _iso(now), "set_by": by}
            digests = block.digests
            digests[install_id] = config_digest(doc)
            txn.set(config_ref, doc)
            self._write_block(txn, tenant_id, block, digests=digests)
            txn.set(audit_ref, _audit_row(action, by=by, at=now, tenant_id=tenant_id,
                                          detail={"install_id": install_id, "version_id": version_id, **detail}))
            result.update(doc)

        _write(transaction)
        if refused:
            raise ConfigRefused(refused[0], status=409, install_id=install_id)
        return result

    def save_values(self, tenant_id: str, install_id: str, version: Mapping[str, Any], *,
                    values: Mapping[str, Any], revision: int, by: str) -> dict[str, Any]:
        """Set (or, with None, clear) non-secret values, checked against
        `version`'s form; refused `config_conflict` unless `revision` is the
        stored one. Audited `plugin_config_set` with the field names."""
        fields_of = _fields_by_key(version)
        checked: dict[str, Any] = {}
        for key, value in values.items():
            field = fields_of.get(key)
            if field is None:
                raise ConfigRefused("field_unknown", field=key if _KEY.match(str(key)) else None)
            checked[key] = None if value is None else check_value(field, value)

        def change(fields: dict[str, Any]) -> dict[str, Any] | None:
            for key, value in checked.items():
                if value is None:
                    if "value" in fields.get(key, {}):
                        fields.pop(key)
                else:
                    fields[key] = {"value": value}
            return {"fields": sorted(checked)}

        return self._mutate(tenant_id, install_id, version_id=str(version.get("version_id")), by=by,
                            change=change, action=AUDIT_CONFIG_SET, revision=revision)

    def record_secret(self, tenant_id: str, install_id: str, version_id: str, field: Mapping[str, Any], *,
                      by: str, sha256: str | None = None, size: int | None = None) -> dict[str, Any]:
        """A secret (or a file's content) was stored: record when and by
        whom, a file's digest and size, never a value."""
        now = _iso(self._now())
        key = field["key"]

        def change(fields: dict[str, Any]) -> dict[str, Any]:
            if field["kind"] == KIND_FILE:
                fields[key] = {"file": {"sha256": sha256, "bytes": size, "set_at": now, "set_by": by}}
            else:
                fields[key] = {"secret": {"kind": "plugin", "set_at": now, "set_by": by}}
            return {"field": key}

        return self._mutate(tenant_id, install_id, version_id=version_id, by=by, change=change,
                            action=AUDIT_SECRET_SET)

    def record_removed(self, tenant_id: str, install_id: str, version_id: str, key: str, *,
                       by: str) -> dict[str, Any]:
        def change(fields: dict[str, Any]) -> dict[str, Any]:
            fields.pop(key, None)
            return {"field": key}

        return self._mutate(tenant_id, install_id, version_id=version_id, by=by, change=change,
                            action=AUDIT_SECRET_REMOVED)

    def record_binding(self, tenant_id: str, install_id: str, version_id: str, key: str, provider: str, *,
                       by: str) -> dict[str, Any]:
        def change(fields: dict[str, Any]) -> dict[str, Any]:
            fields[key] = {"binding": {"kind": BINDING_KIND, "provider": provider}}
            return {"field": key, "provider": provider}

        return self._mutate(tenant_id, install_id, version_id=version_id, by=by, change=change,
                            action=AUDIT_SECRET_BOUND)

    def set_defaults(self, tenant_id: str, wanted: list[str], *, by: str) -> list[str]:
        """Store the workspace's every-step plugins, signed, audited
        `plugin_defaults_set` with both lists. The caller checked each name."""
        self.require_key()
        now = self._now()
        settings_ref, audit_ref = self._settings_ref(tenant_id), self._audit_ref()
        refused: list[str] = []
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            block = self._block_of(tenant_id, _snapshot(txn.get(settings_ref)))
            if not block.verified:
                refused.append("plugins_block_unverified")
                return
            previous = block.defaults
            if previous == wanted and MAC_FIELD in block.stored:
                return
            self._write_block(txn, tenant_id, block, defaults=wanted)
            txn.set(audit_ref, _audit_row(AUDIT_DEFAULTS_SET, by=by, at=now, tenant_id=tenant_id,
                                          detail={"from": previous, "to": wanted}))

        _write(transaction)
        if refused:
            raise ConfigRefused(refused[0], status=409)
        return wanted

    # one-time links (secrets-scopes.md section 6.3)
    def mint_link(self, *, email: str, tenant_id: str, install_id: str, field: str,
                  console: str | None) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires_at = now + LINK_TTL
        self._link_ref(token).set({
            "email": email, "tenant_id": tenant_id, "install_id": install_id, "field": field,
            "created_at": now, "expires_at": expires_at, "used_at": None,
        })
        path = f"/config/plugins/{install_id}?field={field}&t={token}"
        base = console_origin(console)
        log.info("minted a plugin secret link tenant=%s install=%s field=%s", tenant_id, install_id, field)
        return {"url": f"{base}{path}" if base else None, "path": path, "field": field,
                "expires_at": expires_at.isoformat()}

    def consume_link(self, token: str | None, *, email: str, tenant_id: str, install_id: str,
                     field: str) -> None:
        """Burn the link if it is this caller's, for this workspace, plugin and
        field, unused and unexpired; else 403 `secret_link_invalid`, and a
        link someone else presents is not burnt. Never echoes the token."""
        if not token or len(token) > 256:
            raise ConfigRefused("secret_link_invalid", status=403)
        ref = self._link_ref(token)
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            link = (snap.to_dict() or {}) if snap.exists else None
            if not link or link.get("used_at") is not None:
                return False
            expires_at = link.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                return False
            if (link.get("email"), link.get("tenant_id"), link.get("install_id"),
                    link.get("field")) != (email, tenant_id, install_id, field):
                return False
            txn.update(ref, {"used_at": now})
            return True

        if not _consume(transaction):
            raise ConfigRefused("secret_link_invalid", status=403)


def _empty_config(tenant_id: str, install_id: str) -> dict[str, Any]:
    return {"install_id": install_id, "tenant_id": tenant_id, "version_id": None, "revision": 0,
            "fields": {}, "set_at": None, "set_by": None}


def values_view(version: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """What a console shows of the settings: non-secret values, and for a
    secret or a file only whether it is set, by whom and when, or its
    binding. Never a secret's value; a file's content is never stored here."""
    stored = config.get("fields") or {}
    view: dict[str, Any] = {}
    missing: list[str] = []
    for field in form_fields(version):
        key = field["key"]
        entry = stored.get(key) if isinstance(stored.get(key), Mapping) else {}
        if field["kind"] in (KIND_VALUE, KIND_PATH):
            shown: dict[str, Any] = {"set": "value" in entry}
            if "value" in entry:
                shown["value"] = entry["value"]
        elif "binding" in entry and field["kind"] == KIND_SECRET:
            shown = {"set": True, "binding": dict(entry["binding"])}
        else:
            record = entry.get("secret") if field["kind"] == KIND_SECRET else entry.get("file")
            shown = {"set": isinstance(record, Mapping)}
            if isinstance(record, Mapping):
                shown.update({"set_at": record.get("set_at"), "set_by": record.get("set_by")})
                if field["kind"] == KIND_FILE:
                    shown.update({"sha256": record.get("sha256"), "bytes": record.get("bytes")})
        if field["required"] and not shown["set"] and field.get("default") is None:
            missing.append(key)
        view[key] = shown
    return {"revision": int(config.get("revision") or 0), "saved_version_id": config.get("version_id"),
            "values": view, "missing": missing, "set_at": config.get("set_at"), "set_by": config.get("set_by")}


# --- defaults and a step's plugin set (section 7) ----------------------------


def _requires(version: Mapping[str, Any]) -> dict[str, Any]:
    requires = version.get("requires") or {}
    if not isinstance(requires, Mapping):
        return {}
    declared = requires.get("declared")
    block = declared if isinstance(declared, Mapping) else requires.get("suggested")
    return dict(block) if isinstance(block, Mapping) else {}


def profile_refusal(version: Mapping[str, Any], profile: str) -> tuple[str, str | None] | None:
    """Why a step on `profile` cannot run `version`: (code, runtime), or None."""
    spec = RUNNER_PROFILES.get(profile)
    capabilities = tuple(getattr(spec, "runtime_capabilities", ()) or ())
    if "claude_plugins" not in capabilities:
        return "profile_cannot_load_plugins", None
    requires = _requires(version)
    profiles = [p for p in requires.get("profiles") or () if isinstance(p, str)]
    if profiles and profile not in profiles:
        return "plugin_profile_unsupported", None
    for runtime in requires.get("runtimes") or ():
        name = runtime.get("name") if isinstance(runtime, Mapping) else runtime
        if isinstance(name, str) and name not in capabilities:
            return "plugin_runtime_missing", name
    return None


def builtin_profiles() -> dict[str, list[str]]:
    return {str(e["name"]): list(e.get("runner_profiles") or ()) for e in builtin_entries()}  # type: ignore[call-overload]


def step_entry(install: Mapping[str, Any], version: Mapping[str, Any], config: Mapping[str, Any], *,
               overrides: Mapping[str, Any] | None = None) -> tuple[dict[str, Any] | None, str | None]:
    """The signed set's entry for one plugin (section 8), or (None, the key
    of a required setting with no value). Overrides are checked against the
    same fields: a non-secret, non-file value only (section 6.4)."""
    fields_of = _fields_by_key(version)
    stored = config.get("fields") or {}
    for key, value in (overrides or {}).items():
        field = fields_of.get(key)
        if field is None:
            raise ConfigRefused("field_unknown", field=key if _KEY.match(str(key)) else None,
                                install_id=install.get("install_id"))
        if field["kind"] not in (KIND_VALUE, KIND_PATH):
            raise ConfigRefused("override_refused", field=key, install_id=install.get("install_id"))
        check_value(field, value)
    env: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    secret_refs: dict[str, dict[str, Any]] = {}
    for key, field in fields_of.items():
        entry = stored.get(key) if isinstance(stored.get(key), Mapping) else {}
        variable = field["variable"]
        if field["kind"] in (KIND_VALUE, KIND_PATH):
            if overrides and key in overrides:
                value = overrides[key]
            elif "value" in entry:
                value = entry["value"]
            else:
                value = field.get("default")
            if value is None:
                if field["required"]:
                    return None, key
                continue
            try:
                value = check_value(field, value)
            except ConfigRefused:
                # A value saved against an older form that this version retyped.
                if field["required"]:
                    return None, key
                continue
            env[variable] = env_text(value)
        elif field["kind"] == KIND_FILE:
            record = entry.get("file")
            if not isinstance(record, Mapping) or not isinstance(record.get("sha256"), str):
                if field["required"]:
                    return None, key
                continue
            files[variable] = {"field": key, "sha256": record["sha256"]}
        else:
            binding = entry.get("binding")
            if isinstance(binding, Mapping) and binding.get("provider") in BINDABLE:
                secret_refs[variable] = {"field": key, "target": field["target"],
                                         "binding": f"{BINDING_KIND}:{binding['provider']}"}
            elif isinstance(entry.get("secret"), Mapping):
                secret_refs[variable] = {"field": key, "target": field["target"], "secret": "plugin"}
            elif field["required"]:
                return None, key
    return {
        "name": install.get("name"), "scope": install.get("scope"), "version_id": version.get("version_id"),
        "runnable_sha256": version.get("runnable_sha256"), "object": version.get("object"),
        "env": env, "files": files, "secrets": secret_refs, "skills": None, "disabled_skills": [],
    }, None


def step_plugins(store: ps.PluginStore, configs: PluginConfigs, *, tenant_id: str, runner_profile: str,
                 allowed: Iterable[str], added: Mapping[str, Mapping[str, Any] | None] | None = None,
                 off: Iterable[str] = ()) -> dict[str, Any]:
    """A step's plugin set at submission (section 7): `plugins` (managed
    installs, for `metadata.dispatch.plugins`), `built_ins` (names, for the
    work contract as today) and `skipped`: each default the step cannot run,
    with its fixed code (the owner's answer to Q6). A plugin the step itself
    adds (`added`: install id -> `{"overrides": {...}}`) and cannot run is
    refused by the same code. `allowed` is the workspace's signed list."""
    allowed_set = set(allowed)
    turned_off = set(off)
    added = dict(added or {})
    plugins: dict[str, dict[str, Any]] = {}
    built_ins: list[str] = []
    skipped: list[dict[str, Any]] = []
    builtins = builtin_profiles()
    order = [n for n in configs.defaults(tenant_id) if n not in turned_off and n not in added]
    wanted = [(n, True) for n in order] + [(n, False) for n in sorted(added)]

    def refuse_or_skip(name: str, is_default: bool, code: str, **detail: Any) -> None:
        if not is_default:
            raise ConfigRefused(code, install_id=name, runner_profile=runner_profile, **detail)
        skipped.append({"install_id": name, "code": code,
                        **{k: v for k, v in detail.items() if v is not None}})

    for name, is_default in wanted:
        if name in builtins or name.startswith(BUILT_IN_PREFIX):
            bare = name[len(BUILT_IN_PREFIX):] if name.startswith(BUILT_IN_PREFIX) else name
            if bare not in builtins or bare not in allowed_set:
                refuse_or_skip(name, is_default, "plugin_not_allowed")
            elif runner_profile not in builtins[bare]:
                refuse_or_skip(name, is_default, "profile_cannot_load_plugins")
            elif bare not in built_ins:
                built_ins.append(bare)
            continue
        install = store.install(name)
        if install is None or not ps.visible_to(install, tenant_id):
            refuse_or_skip(name, is_default, "plugin_not_found")
            continue
        if install.get("state") != ps.STATE_ACTIVE:
            refuse_or_skip(name, is_default, "plugin_removed")
            continue
        if install.get("scope") == ps.PLATFORM and name not in allowed_set:
            refuse_or_skip(name, is_default, "plugin_not_allowed")
            continue
        version = store.current(install)
        if version is None:
            refuse_or_skip(name, is_default, "plugin_not_found")
            continue
        why = profile_refusal(version, runner_profile)
        if why is not None:
            refuse_or_skip(name, is_default, why[0], runtime=why[1])
            continue
        entry, missing = step_entry(install, version, configs.config(tenant_id, name),
                                    overrides=(added.get(name) or {}).get("overrides"))
        if entry is None:
            refuse_or_skip(name, is_default, "plugin_setting_missing", field=missing)
            continue
        plugins[name] = entry
    if len(plugins) + len(built_ins) > MAX_PLUGINS:
        raise ConfigRefused("too_many_plugins", runner_profile=runner_profile)
    return {"plugins": plugins, "built_ins": built_ins, "skipped": skipped}


def check_defaults(store: ps.PluginStore, tenant_id: str, names: Iterable[str],
                   allowed: Iterable[str]) -> list[str]:
    """Sorted, deduplicated defaults, each a plugin the workspace allows (a
    built-in by name, a platform install by id) or one of its own active
    installs; else 422 `default_not_allowed` naming it."""
    allowed_set = set(allowed)
    builtins = builtin_profiles()
    wanted = sorted(set(names))
    if len(wanted) > MAX_PLUGINS:
        raise ConfigRefused("too_many_plugins")
    for name in wanted:
        if not isinstance(name, str):
            raise ConfigRefused("default_not_allowed")
        if name in builtins:
            if name not in allowed_set:
                raise ConfigRefused("default_not_allowed", install_id=name)
            continue
        install = store.install(name) if ps.INSTALL_ID.match(name) else None
        if install is None or install.get("state") != ps.STATE_ACTIVE or not ps.visible_to(install, tenant_id):
            raise ConfigRefused("default_not_allowed", install_id=name if ps.INSTALL_ID.match(name) else None)
        if install.get("scope") == ps.PLATFORM and name not in allowed_set:
            raise ConfigRefused("default_not_allowed", install_id=name)
    return wanted


__all__ = [
    "AUDIT_ACTIONS",
    "BINDABLE",
    "CONFIGS",
    "ConfigRefused",
    "KIND_FILE",
    "KIND_PATH",
    "KIND_SECRET",
    "KIND_VALUE",
    "LINKS",
    "PluginConfigs",
    "PluginSecretWriter",
    "REFUSALS",
    "SecretManagerPluginSecrets",
    "check_defaults",
    "check_value",
    "config_digest",
    "configs_digest",
    "form",
    "form_fields",
    "node_only",
    "plugin_consumer",
    "profile_refusal",
    "secret_name",
    "step_entry",
    "step_plugins",
    "values_view",
]
