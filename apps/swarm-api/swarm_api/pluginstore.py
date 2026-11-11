"""The managed plugin catalogue's store: installs, versions, intakes and their audit, in Firestore.

docs/design/managed-plugins.md sections 1, 2.3, 9 and 10 (lane 5 of section
17), with the owner's answers of 6 November 2026 where the design still
recommended something else.

THE DOCUMENTS (section 1.1), written by swarm-api alone:

    plugin_installs/{install_id}                       one installed plugin
    plugin_installs/{install_id}/versions/{version_id} one pinned copy, never edited
    plugin_intake/{intake_id}                          one fetch or upload in flight

An install id is `p-<12 hex>` (platform) or `w-<12 hex>` (one workspace),
random, never derived from a name. A version is written once, from the
intake job's report, and an update writes a new version that becomes current
only when an admin applies it after reading its diff (`version_diff`).

EVERY DOCUMENT CARRIES A MAC (contract request 141, P2). A tenant identity
can write any Firestore document, so a forged version could swap the digest
every workspace's steps are signed with. Each record is MAC'd under the
`swarm-settings-key` over `specsign.canonical_plugin_record(kind, fields)`,
which binds its kind, its scope, its tenant and its ids, and this module
acts on a record only when its MAC verifies: an unsigned or forged record is
absent. With no settings key nothing can be signed, so nothing is installed.

THE INTAKE BINDS ITS REPORT (section 2.3). The scope and the tenant a pinned
version is written under come from the MAC'd intake document, never from the
report the intake job wrote: the report is untrusted data, read through a
closed schema (`parse_report`), and its runnable tar's sha256 and size are
recomputed by swarm-api before the copy into the scope's prefix.

NOTHING UPSTREAM IN A REFUSAL OR AN AUDIT ROW (section 2.3). Every refusal
is a fixed code with a fixed sentence; an audit row's detail names ids,
version ids, digests, codes and counts, never a manifest's name, a path or a
hook command, because those are the archive's text.

THE OWNER'S CAPS (Q8): 100 MiB packed, 500 MiB unpacked, 20,000 files. The
intake job enforces all three on the bytes it unpacks; swarm-api enforces
the packed cap on what it fetches and on the signed upload URL, and refuses
a report whose stated limits are not these.

REMOVAL (Q5): a removed install leaves the catalogue and `refuse_removed`
refuses any later submission that names it with the fixed code
`plugin_removed`; a step already submitted keeps the version it was signed
with.

Pure apart from the Firestore client it is handed: no network, no Cloud
Storage, no clock but `now`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.specsign import SpecNotCanonical, canonical_plugin_record, jcs

from .admins import AUDIT_COLLECTION
from .errors import ValidationFailed
from .judgment import MAC_FIELD, MAC_VERSION, SETTINGS_COLLECTION, SettingsMac, block_verifies, key_id

INSTALLS = "plugin_installs"
VERSIONS = "versions"
INTAKES = "plugin_intake"

PLATFORM = "platform"
WORKSPACE = "workspace"

STATE_ACTIVE = "active"
STATE_REMOVED = "removed"

#: An upload's intake waits for its bytes; a fetched one starts at once.
INTAKE_AWAITING_UPLOAD = "awaiting_upload"
INTAKE_RUNNING = "running"
INTAKE_PINNED = "pinned"
INTAKE_REFUSED = "refused"
INTAKE_STATES = (INTAKE_AWAITING_UPLOAD, INTAKE_RUNNING, INTAKE_PINNED, INTAKE_REFUSED)

MIB = 1024 * 1024
#: The owner's answer to Q8, the same numbers apps/plugin-intake's limits.py
#: holds; a report stating other limits is refused (`report_limits_differ`).
MAX_PACKED_BYTES = 100 * MIB
MAX_UNPACKED_BYTES = 500 * MIB
MAX_FILES = 20_000
MAX_EXPANSION_RATIO = 100
LIMITS = {
    "max_packed_bytes": MAX_PACKED_BYTES,
    "max_unpacked_bytes": MAX_UNPACKED_BYTES,
    "max_files": MAX_FILES,
    "max_expansion_ratio": MAX_EXPANSION_RATIO,
}
#: The intake job's report: its schema and the most bytes swarm-api reads.
REPORT_SCHEMA = "swarm.plugin-intake.report/1"
MAX_REPORT_BYTES = 4 * MIB
#: The job's own timeout is ten minutes (terraform/infra/plugins.tf); an
#: intake still running five minutes after that wrote no report and is
#: refused when it is next read, never waited for (invariant 4).
INTAKE_DEADLINE = timedelta(minutes=15)
#: An upload URL lives ten minutes; its intake may wait for the bytes an hour.
UPLOAD_DEADLINE = timedelta(hours=1)
#: The overlay travels in the job execution's environment (64 KiB there).
MAX_OVERLAY_BYTES = 64 * 1024

INSTALL_ID = re.compile(r"^[pw]-[0-9a-f]{12}$")
#: The two bundled installs' reserved ids (section 1.4.1): the ids every
#: Allowed list and default already names, never minted by `new_install_id`.
RESERVED_INSTALL_IDS = ("builtin:gstack", "builtin:security-audit")
#: An install whose versions are built into an image version (section 1.4):
#: its `build` field. Any other install's versions are unpacked per attempt.
BUILD_IMAGE = "image"
VERSION_ID = re.compile(r"^v-[0-9a-f]{12}$")
INTAKE_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
#: A manifest name, as the intake job accepts it.
PLUGIN_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
#: A refusal code the job may report: shape only; its sentence is never kept.
REFUSAL_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# `admin_audit` actions (section 10).
AUDIT_INTAKE_STARTED = "plugin_intake_started"
AUDIT_INTAKE_REFUSED = "plugin_intake_refused"
AUDIT_INSTALLED = "plugin_installed"
AUDIT_UPDATE_CHECKED = "plugin_update_checked"
AUDIT_UPDATED = "plugin_updated"
AUDIT_REMOVED = "plugin_removed"
AUDIT_ACTIONS = (AUDIT_INTAKE_STARTED, AUDIT_INTAKE_REFUSED, AUDIT_INSTALLED,
                 AUDIT_UPDATE_CHECKED, AUDIT_UPDATED, AUDIT_REMOVED)

#: swarm-api's own refusals: code -> fixed sentence, nothing interpolated.
REFUSALS: dict[str, str] = {
    "settings_key_missing": "This deployment has no settings key, so no plugin can be installed.",
    "source_kind_refused": "A plugin comes from a GitHub repository, a marketplace entry or an uploaded archive.",
    "repository_invalid": "The repository is not an owner/name GitHub repository.",
    "ref_invalid": "The ref is not a tag, a branch or a commit name.",
    "subdir_invalid": "The folder is not a relative path inside the repository.",
    "ref_not_found": "The ref does not name a commit in the repository.",
    "source_unreachable": "GitHub could not be read; nothing was installed.",
    "marketplace_missing": "The repository has no readable .claude-plugin/marketplace.json at that commit.",
    "marketplace_plugin_missing": "The marketplace does not list a plugin of that name.",
    "marketplace_source_npm": "The marketplace entry installs from npm, which this release refuses.",
    "marketplace_source_pip": "The marketplace entry installs from pip, which this release refuses.",
    "marketplace_source_git": "The marketplace entry names a bare git URL, which this release refuses.",
    "marketplace_source_unknown": "The marketplace entry's source is not a path or a GitHub repository.",
    "archive_too_large": "The archive is larger than 100 MiB.",
    "upload_name_invalid": "An upload is one .zip, .tar.gz or .tgz file.",
    "upload_missing": "No uploaded archive was found for this intake.",
    "overlay_too_large": "The overlay is larger than 64 KiB.",
    "plugin_name_taken": "An installed plugin in this catalogue already has that name; update it instead.",
    "plugin_removed": "The plugin was removed from the catalogue.",
    "plugin_built_in": "A built-in plugin is updated by an image release and cannot be removed.",
    "intake_start_failed": "The intake job could not be started.",
    "intake_timed_out": "The intake job ended without a report.",
    "intake_unverified": "The intake's record does not verify.",
    "intake_digest_mismatch": "The staged copy's digest or size is not the one its report states.",
    "report_invalid": "The intake job's report is not a valid report.",
    "report_mismatch": "The intake job's report is bound to another intake.",
    "report_limits_differ": "The intake job's report was made under other limits.",
    "pinned_copy_differs": "A different copy is already stored under that digest.",
    "install_not_active": "The plugin is not an active install.",
    "version_not_found": "That version is not one of the plugin's versions.",
    "version_is_current": "That version is already the current one.",
    "diff_changed": "The diff changed since it was shown; read it again before applying.",
    "confirmation_required": "This update changes hooks, servers, hosts or secret fields: type the plugin's name to apply it.",
    "confirmation_mismatch": "The name typed is not the plugin's name.",
    # Bundled installs (section 1.4.3, pluginbundle.py): a version becomes
    # current only when the bundle that carries it is ready.
    "install_bundled": "This plugin is built into an image: applying a version starts a bundle build.",
    "install_not_bundled": "This plugin is not built into an image.",
    "bundle_building": "A bundle build is already running; wait for it to end.",
    "bundle_not_found": "That bundle is not one of this deployment's bundles.",
    "bundle_not_retained": "No retained bundle carries the previous version; apply it to build one.",
    "bundle_build_unconfigured": "This deployment has no bundle build configured.",
    "bundle_grant_missing": "swarm-api may not start a bundle build as the builder account; "
                            "the builder's grants in bootstrap are not applied yet.",
    "bundle_build_not_started": "The bundle build could not be started.",
    "bundle_build_failed": "The bundle build failed.",
    "bundle_check_failed": "The bundle build's output is not the bundle that was asked for.",
    "bundle_base_invalid": "A base image is not this deployment's image by digest.",
}


class PluginRefused(ValidationFailed):
    """A refusal by fixed code: the sentence is `REFUSALS[code]`, and the only
    detail is platform-minted ids and fields, never an upstream string."""

    def __init__(self, code: str, *, status: int = 422, **detail: Any) -> None:
        if code not in REFUSALS:
            raise KeyError(code)
        super().__init__(REFUSALS[code], detail={k: v for k, v in detail.items() if v is not None})
        self.code = code
        self.status_code = status


def new_install_id(scope: str) -> str:
    return ("p-" if scope == PLATFORM else "w-") + secrets.token_hex(6)


def install_id_valid(install_id: Any) -> bool:
    """A minted id, or one of the two reserved bundled ids."""
    return isinstance(install_id, str) and (bool(INSTALL_ID.match(install_id))
                                            or install_id in RESERVED_INSTALL_IDS)


def is_bundled(install: Mapping[str, Any] | None) -> bool:
    """A platform install built into an image version (section 1.4)."""
    return isinstance(install, Mapping) and install.get("build") == BUILD_IMAGE \
        and install.get("scope") == PLATFORM


def new_version_id() -> str:
    return "v-" + secrets.token_hex(6)


def new_intake_id() -> str:
    """128 random bits (section 2.3): the job's staging name nobody can guess."""
    return secrets.token_hex(16)


def scope_prefix(scope: str, tenant_id: str | None) -> str:
    """Where a scope's pinned copies live in the plugins bucket (section 3)."""
    return "platform" if scope == PLATFORM else f"tenants/{tenant_id}"


# --- the MAC (contract request 141) -------------------------------------------


def _ids(kind: str, fields: Mapping[str, Any], ids: Mapping[str, str]) -> dict[str, Any]:
    whole = dict(fields)
    whole.update(ids)
    return whole


def _sig(key: str, kind: str, fields: Mapping[str, Any]) -> bytes:
    return hmac.new(key.encode("utf-8"), canonical_plugin_record(kind, fields), hashlib.sha256).digest()


def record_mac(kind: str, fields: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """The `mac` a writer stores, under the current key. Raises
    `PluginRefused("settings_key_missing")` with no key: a catalogue record
    is never stored unsigned."""
    if not keys:
        raise PluginRefused("settings_key_missing", status=503)
    return {"v": MAC_VERSION, "key": key_id(keys[0]),
            "sig": base64.b64encode(_sig(keys[0], kind, fields)).decode("ascii")}


def record_verifies(kind: str, fields: Mapping[str, Any] | None, keys: tuple[str, ...]) -> bool:
    """`fields` (the stored document plus its path ids) carries a MAC one of
    `keys` made over exactly these bytes."""
    if not isinstance(fields, Mapping) or not keys:
        return False
    mac = fields.get(MAC_FIELD)
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION or not isinstance(mac.get("sig"), str):
        return False
    try:
        given = base64.b64decode(mac["sig"], validate=True)
    except (ValueError, TypeError):
        return False
    for key in keys:
        try:
            want = _sig(key, kind, fields)
        except SpecNotCanonical:
            return False
        if hmac.compare_digest(given, want):
            return True
    return False


def _signed(kind: str, fields: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    stored = {k: v for k, v in fields.items() if k != MAC_FIELD}
    stored[MAC_FIELD] = record_mac(kind, stored, keys)
    return stored


def _iso(when: datetime) -> str:
    return when.isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- the intake job's report, as untrusted data -------------------------------

_REPORT_KEYS = frozenset({
    "schema", "intake_id", "scope", "tenant_id", "runnable_object", "limits", "outcome",
    "refusal", "source_sha256", "source_bytes", "runnable_sha256", "runnable_bytes",
    "runnable_format", "files", "unpacked_bytes", "manifest", "manifest_source",
    "components", "overlay", "requires",
})
_PINNED_REQUIRED = frozenset({
    "source_sha256", "source_bytes", "runnable_sha256", "runnable_bytes", "files",
    "unpacked_bytes", "manifest", "components", "requires",
})
_COMPONENT_KEYS = ("skills", "commands", "agents", "hooks", "mcp_servers")


def _nonneg(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_report(raw: bytes, intake: Mapping[str, Any]) -> dict[str, Any]:
    """The report as a closed schema, bound to `intake` (its MAC'd document).
    Raises `PluginRefused` with `report_invalid`, `report_mismatch` or
    `report_limits_differ`; never echoes a byte of the report."""
    if len(raw) > MAX_REPORT_BYTES:
        raise PluginRefused("report_invalid")
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # RecursionError: nesting deeper than the parser's stack, well inside
        # the size cap. Never a reason to retry: the same bytes fail again.
        raise PluginRefused("report_invalid") from None
    if not isinstance(report, dict) or set(report) - _REPORT_KEYS:
        raise PluginRefused("report_invalid")
    if report.get("schema") != REPORT_SCHEMA:
        raise PluginRefused("report_invalid")
    # The scope and tenant are the intake document's; the report must agree.
    if (report.get("intake_id") != intake.get("intake_id") or report.get("scope") != intake.get("scope")
            or report.get("tenant_id") != intake.get("tenant_id")):
        raise PluginRefused("report_mismatch")
    if report.get("limits") != LIMITS:
        raise PluginRefused("report_limits_differ")
    outcome = report.get("outcome")
    if outcome == "refused":
        refusal = report.get("refusal")
        if not isinstance(refusal, dict) or not isinstance(refusal.get("code"), str) \
                or not REFUSAL_CODE.match(refusal["code"]):
            raise PluginRefused("report_invalid")
        index = refusal.get("entry_index")
        if index is not None and not _nonneg(index):
            raise PluginRefused("report_invalid")
        return {"outcome": "refused", "code": refusal["code"], "entry_index": index}
    if outcome != "pinned" or report.get("refusal") is not None or _PINNED_REQUIRED - set(report):
        raise PluginRefused("report_invalid")
    for name in ("source_sha256", "runnable_sha256"):
        if not isinstance(report[name], str) or not SHA256.match(report[name]):
            raise PluginRefused("report_invalid")
    for name in ("source_bytes", "runnable_bytes", "files", "unpacked_bytes"):
        if not _nonneg(report[name]):
            raise PluginRefused("report_invalid")
    if report["files"] > MAX_FILES or report["unpacked_bytes"] > MAX_UNPACKED_BYTES \
            or report["source_bytes"] > MAX_PACKED_BYTES:
        raise PluginRefused("report_limits_differ")
    manifest = report["manifest"]
    if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str) \
            or not PLUGIN_NAME.match(manifest["name"]):
        raise PluginRefused("report_invalid")
    components = report["components"]
    if not isinstance(components, dict) or set(components) != set(_COMPONENT_KEYS) \
            or not all(isinstance(components[k], list) and all(isinstance(e, dict) for e in components[k])
                       for k in _COMPONENT_KEYS):
        raise PluginRefused("report_invalid")
    requires = report["requires"]
    if not isinstance(requires, dict):
        raise PluginRefused("report_invalid")
    overlay = report.get("overlay")
    if overlay is not None and not isinstance(overlay, dict):
        raise PluginRefused("report_invalid")
    try:
        jcs(report)  # stored under the MAC: it must have a canonical form
    except SpecNotCanonical:
        raise PluginRefused("report_invalid") from None
    return report


# --- the diff an update shows before it applies (section 9.1) -----------------


def _hosts(version: Mapping[str, Any]) -> list[str]:
    requires = version.get("requires") or {}
    found: set[str] = set()
    for part in ("suggested", "declared"):
        block = requires.get(part) if isinstance(requires, Mapping) else None
        if isinstance(block, Mapping):
            found.update(h for h in block.get("hosts") or () if isinstance(h, str))
    return sorted(found)


def _runtimes(version: Mapping[str, Any]) -> list[Any]:
    requires = version.get("requires") or {}
    declared = requires.get("declared") if isinstance(requires, Mapping) else None
    block = declared if isinstance(declared, Mapping) else requires.get("suggested") or {}
    return list(block.get("runtimes") or ()) if isinstance(block, Mapping) else []


def _settings(version: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every settings field: the manifest's userConfig and the overlay's declared settings."""
    fields: dict[str, dict[str, Any]] = {}
    for spec in (version.get("manifest") or {}).get("user_config") or ():
        if isinstance(spec, Mapping) and isinstance(spec.get("key"), str):
            fields[spec["key"]] = {"type": spec.get("type"), "sensitive": bool(spec.get("sensitive"))}
    overlay = version.get("overlay") or {}
    for spec in overlay.get("settings") or () if isinstance(overlay, Mapping) else ():
        if isinstance(spec, Mapping) and isinstance(spec.get("key"), str):
            fields[spec["key"]] = {"type": spec.get("type"), "sensitive": bool(spec.get("sensitive"))}
    return fields


def _components(version: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    found = (version.get("components") or {}).get(name) or ()
    return [entry for entry in found if isinstance(entry, Mapping)]


def _hook_key(hook: Mapping[str, Any]) -> str:
    return json.dumps([hook.get("event"), hook.get("matcher"), hook.get("command")], sort_keys=True)


def _added_removed(old: Iterable[str], new: Iterable[str]) -> dict[str, list[str]]:
    before, after = set(old), set(new)
    return {"added": sorted(after - before), "removed": sorted(before - after)}


_MANIFEST_FIELDS = ("name", "version", "description", "author", "homepage", "repository", "license")


def version_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """What changes from `old` to `new`, from the two stored versions, with no
    fetch. A change to hooks, MCP servers, network hosts or a field's
    sensitivity is `marked`: that is where a malicious update acts, and
    applying it needs the plugin's name typed."""
    old_manifest, new_manifest = old.get("manifest") or {}, new.get("manifest") or {}
    manifest = {f: {"from": old_manifest.get(f), "to": new_manifest.get(f)}
                for f in _MANIFEST_FIELDS if old_manifest.get(f) != new_manifest.get(f)}

    old_hooks = {_hook_key(h): h for h in _components(old, "hooks")}
    new_hooks = {_hook_key(h): h for h in _components(new, "hooks")}
    hooks = {
        "added": [dict(new_hooks[k]) for k in sorted(set(new_hooks) - set(old_hooks))],
        "removed": [dict(old_hooks[k]) for k in sorted(set(old_hooks) - set(new_hooks))],
        "changed": [{"from": dict(old_hooks[k]), "to": dict(new_hooks[k])}
                    for k in sorted(set(old_hooks) & set(new_hooks)) if old_hooks[k] != new_hooks[k]],
    }

    old_servers = {str(s.get("name")): s for s in _components(old, "mcp_servers")}
    new_servers = {str(s.get("name")): s for s in _components(new, "mcp_servers")}
    servers = {
        **_added_removed(old_servers, new_servers),
        "changed": sorted(k for k in set(old_servers) & set(new_servers) if old_servers[k] != new_servers[k]),
    }

    named = {kind: _added_removed((str(e.get("name")) for e in _components(old, kind)),
                                  (str(e.get("name")) for e in _components(new, kind)))
             for kind in ("skills", "commands", "agents")}

    old_settings, new_settings = _settings(old), _settings(new)
    shared = set(old_settings) & set(new_settings)
    settings = {
        **_added_removed(old_settings, new_settings),
        "retyped": sorted(k for k in shared if old_settings[k]["type"] != new_settings[k]["type"]),
        "newly_sensitive": sorted(k for k in shared if new_settings[k]["sensitive"] and not old_settings[k]["sensitive"]),
    }
    added_sensitive = sorted(k for k in settings["added"] if new_settings[k]["sensitive"])

    hosts = _added_removed(_hosts(old), _hosts(new))
    runtimes = {"from": _runtimes(old), "to": _runtimes(new)}

    old_source, new_source = old.get("source") or {}, new.get("source") or {}
    source: dict[str, Any] = {"from_commit": old_source.get("commit"), "to_commit": new_source.get("commit")}
    if (old_source.get("repository") and old_source.get("repository") == new_source.get("repository")
            and COMMIT.match(str(old_source.get("commit") or "")) and COMMIT.match(str(new_source.get("commit") or ""))):
        source["compare"] = (f"https://github.com/{new_source['repository']}/compare/"
                             f"{old_source['commit']}...{new_source['commit']}")

    marked = []
    if any(hooks.values()):
        marked.append("hooks")
    if any(servers.values()):
        marked.append("mcp_servers")
    if any(hosts.values()):
        marked.append("hosts")
    if settings["newly_sensitive"] or added_sensitive:
        marked.append("sensitive")
    return {
        "from_version_id": old.get("version_id"),
        "to_version_id": new.get("version_id"),
        "manifest": manifest,
        "hooks": hooks,
        "mcp_servers": servers,
        **named,
        "settings": settings,
        "hosts": hosts,
        "runtimes": runtimes if runtimes["from"] != runtimes["to"] else None,
        "files": {"from": old.get("files"), "to": new.get("files")},
        "unpacked_bytes": {"from": old.get("unpacked_bytes"), "to": new.get("unpacked_bytes")},
        "runnable_sha256": {"from": old.get("runnable_sha256"), "to": new.get("runnable_sha256")},
        "source": source,
        "marked": marked,
        "needs_confirmation": bool(marked),
    }


def diff_digest(diff: Mapping[str, Any]) -> str:
    """The digest an apply must quote: proof the admin applies the diff they read."""
    return hashlib.sha256(jcs(dict(diff))).hexdigest()


# --- views -------------------------------------------------------------------


def version_view(version: Mapping[str, Any], *, current: bool) -> dict[str, Any]:
    manifest = version.get("manifest") or {}
    return {
        "version_id": version.get("version_id"),
        "current": current,
        "version": manifest.get("version"),
        "source": version.get("source"),
        "source_sha256": version.get("source_sha256"),
        "runnable_sha256": version.get("runnable_sha256"),
        "runnable_bytes": version.get("runnable_bytes"),
        "files": version.get("files"),
        "unpacked_bytes": version.get("unpacked_bytes"),
        "manifest": manifest,
        "components": version.get("components"),
        "requires": version.get("requires"),
        # Shown now; enforcement is a later lane (the owner's answer to Q1).
        "hosts": _hosts(version),
        "created_at": _when(version.get("created_at")),
        "created_by": version.get("created_by"),
    }


def _when(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value if isinstance(value, str) else None


def install_view(install: Mapping[str, Any], current: Mapping[str, Any] | None, *,
                 allowed: bool | None = None) -> dict[str, Any]:
    manifest = (current or {}).get("manifest") or {}
    components = (current or {}).get("components") or {}
    view = {
        "install_id": install.get("install_id"),
        "name": install.get("name"),
        "display_name": install.get("display_name"),
        "scope": install.get("scope"),
        "tenant_id": install.get("tenant_id"),
        "built_in": False,
        "state": install.get("state"),
        "source": install.get("source"),
        "current_version": install.get("current_version"),
        "version": manifest.get("version"),
        "description": manifest.get("description"),
        "runnable_sha256": (current or {}).get("runnable_sha256"),
        "hosts": _hosts(current or {}),
        "components": {k: len(components.get(k) or ()) for k in _COMPONENT_KEYS},
        "removable": install.get("state") == STATE_ACTIVE,
        "updatable": install.get("state") == STATE_ACTIVE,
        "updated_at": _when(install.get("updated_at")),
        "updated_by": install.get("updated_by"),
    }
    if is_bundled(install):
        # Built into an image (section 1.4): no Remove, and an update builds.
        view.update({"build": BUILD_IMAGE, "removable": False})
    if allowed is not None:
        view["allowed"] = allowed
    return view


# --- the store ---------------------------------------------------------------


def _audit_row(action: str, *, by: str, at: datetime, scope: str, tenant_id: str | None,
               detail: Mapping[str, Any]) -> dict[str, Any]:
    return {"action": action, "by": by, "at": at, "target_tenant_id": tenant_id,
            "detail": {"scope": scope, **detail}}


class PluginStore:
    """The catalogue's documents, each MAC'd, each change audited in the same
    transaction as the change itself."""

    def __init__(self, db: Any, *, now: Callable[[], datetime], mac: SettingsMac) -> None:
        self._db = db
        self._now = now
        self._mac = mac

    @property
    def keys(self) -> tuple[str, ...]:
        return self._mac.keys

    def require_key(self) -> None:
        if not self._mac.keys:
            raise PluginRefused("settings_key_missing", status=503)

    # refs
    def _install_ref(self, install_id: str) -> Any:
        return self._db.collection(INSTALLS).document(install_id)

    def _version_ref(self, install_id: str, version_id: str) -> Any:
        return self._install_ref(install_id).collection(VERSIONS).document(version_id)

    def _intake_ref(self, intake_id: str) -> Any:
        return self._db.collection(INTAKES).document(intake_id)

    def _audit_ref(self) -> Any:
        return self._db.collection(AUDIT_COLLECTION).document()

    # verified reads
    def _verified(self, kind: str, snap: Any, ids: Mapping[str, str]) -> dict[str, Any] | None:
        if not snap.exists:
            return None
        whole = _ids(kind, snap.to_dict() or {}, ids)
        return whole if record_verifies(kind, whole, self._mac.keys) else None

    def install(self, install_id: str) -> dict[str, Any] | None:
        if not install_id_valid(install_id):
            return None
        return self._verified("install", self._install_ref(install_id).get(), {"install_id": install_id})

    def version(self, install_id: str, version_id: str) -> dict[str, Any] | None:
        if not install_id_valid(install_id) or not VERSION_ID.match(version_id or ""):
            return None
        return self._verified("version", self._version_ref(install_id, version_id).get(),
                              {"install_id": install_id, "version_id": version_id})

    def versions(self, install_id: str) -> list[dict[str, Any]]:
        found = []
        for snap in self._install_ref(install_id).collection(VERSIONS).stream():
            whole = self._verified("version", snap, {"install_id": install_id, "version_id": snap.id})
            if whole is not None:
                found.append(whole)
        return sorted(found, key=lambda v: (str(_when(v.get("created_at")) or ""), v["version_id"]))

    def intake(self, intake_id: str) -> dict[str, Any] | None:
        if not INTAKE_ID.match(intake_id or ""):
            return None
        return self._verified("intake", self._intake_ref(intake_id).get(), {"intake_id": intake_id})

    def installs(self, scope: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        """Every verified install of `scope` (one workspace's when `tenant_id`), removed ones included."""
        query = self._db.collection(INSTALLS).where(filter=FieldFilter("scope", "==", scope))
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        found = []
        for snap in query.stream():
            whole = self._verified("install", snap, {"install_id": snap.id})
            if whole is not None:
                found.append(whole)
        return sorted(found, key=lambda i: (str(i.get("name")), i["install_id"]))

    def current(self, install: Mapping[str, Any]) -> dict[str, Any] | None:
        version_id = install.get("current_version")
        return self.version(install["install_id"], version_id) if isinstance(version_id, str) else None

    # writes
    def open_intake(self, *, scope: str, tenant_id: str | None, install_id: str | None,
                    source: Mapping[str, Any], subdir: str, overlay: str | None, by: str,
                    awaiting_upload: bool, intake_id: str | None = None) -> dict[str, Any]:
        """Write the MAC'd intake document and its `plugin_intake_started` row."""
        self.require_key()
        intake_id = intake_id or new_intake_id()
        now = self._now()
        state = INTAKE_AWAITING_UPLOAD if awaiting_upload else INTAKE_RUNNING
        fields = {
            "intake_id": intake_id, "scope": scope, "tenant_id": tenant_id, "install_id": install_id,
            "source": dict(source), "subdir": subdir, "overlay": overlay, "state": state,
            "deadline": _iso(now + (UPLOAD_DEADLINE if awaiting_upload else INTAKE_DEADLINE)),
            "refusal": None, "version_id": None, "requested_by": by,
            "created_at": now, "created_by": by,
        }
        stored = _signed("intake", fields, self._mac.keys)
        audit = _audit_row(AUDIT_INTAKE_STARTED, by=by, at=now, scope=scope, tenant_id=tenant_id, detail={
            "intake_id": intake_id, "install_id": install_id, "source_kind": source.get("kind"),
            "commit": source.get("commit") if COMMIT.match(str(source.get("commit") or "")) else None})
        ref, audit_ref = self._intake_ref(intake_id), self._audit_ref()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            txn.set(ref, stored)
            txn.set(audit_ref, audit)

        _write(transaction)
        return stored

    def mark_running(self, intake_id: str) -> dict[str, Any] | None:
        """An upload's bytes arrived: the intake runs from now. None unless it was waiting."""
        ref = self._intake_ref(intake_id)
        keys = self._mac.keys
        now = self._now()
        result: dict[str, Any] = {}
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            whole = self._verified("intake", _snapshot(txn.get(ref)), {"intake_id": intake_id})
            if whole is None or whole.get("state") != INTAKE_AWAITING_UPLOAD:
                return
            whole.update({"state": INTAKE_RUNNING, "deadline": _iso(now + INTAKE_DEADLINE)})
            stored = _signed("intake", whole, keys)
            txn.set(ref, stored)
            result.update(stored)

        _write(transaction)
        return result or None

    def refuse(self, intake_id: str, code: str, *, entry_index: int | None = None,
               by: str | None = None) -> dict[str, Any] | None:
        """End an unfinished intake refused, with its audit row. None when it
        had already ended (a second notification changes nothing)."""
        ref = self._intake_ref(intake_id)
        keys = self._mac.keys
        now = self._now()
        result: dict[str, Any] = {}
        audit_ref = self._audit_ref()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            whole = self._verified("intake", _snapshot(txn.get(ref)), {"intake_id": intake_id})
            if whole is None or whole.get("state") not in (INTAKE_AWAITING_UPLOAD, INTAKE_RUNNING):
                return
            whole.update({"state": INTAKE_REFUSED, "refusal": {"code": code, "entry_index": entry_index}})
            stored = _signed("intake", whole, keys)
            txn.set(ref, stored)
            txn.set(audit_ref, _audit_row(
                AUDIT_INTAKE_REFUSED, by=by or str(whole.get("requested_by") or ""), at=now,
                scope=whole["scope"], tenant_id=whole.get("tenant_id"),
                detail={"intake_id": intake_id, "install_id": whole.get("install_id"),
                        "code": code, "entry_index": entry_index}))
            result.update(stored)

        _write(transaction)
        return result or None

    def name_taken(self, scope: str, tenant_id: str | None, name: str) -> bool:
        return any(i.get("name") == name and i.get("state") == STATE_ACTIVE
                   for i in self.installs(scope, tenant_id))

    def pin(self, intake_id: str, report: Mapping[str, Any], object_name: str) -> dict[str, Any] | None:
        """Write the version the intake pinned (and, for a new plugin, its
        install, current at once), end the intake, and audit it, in one
        transaction. A new install's id is minted here. Returns the version,
        or None when the intake had already ended. Raises `PluginRefused` for
        an update whose install is no longer active."""
        keys = self._mac.keys
        now = self._now()
        ref = self._intake_ref(intake_id)
        audit_ref = self._audit_ref()
        result: dict[str, Any] = {}
        refused: list[str] = []
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            intake = self._verified("intake", _snapshot(txn.get(ref)), {"intake_id": intake_id})
            if intake is None or intake.get("state") != INTAKE_RUNNING:
                return
            scope, tenant_id, by = intake["scope"], intake.get("tenant_id"), str(intake.get("requested_by") or "")
            install_id = intake.get("install_id")
            install = None
            if install_id:
                install = self._verified("install", _snapshot(txn.get(self._install_ref(install_id))),
                                         {"install_id": install_id})
                if install is None or install.get("state") != STATE_ACTIVE \
                        or install.get("scope") != scope or install.get("tenant_id") != tenant_id:
                    refused.append("install_not_active")
                    return
            else:
                install_id = new_install_id(scope)
            version_id = new_version_id()
            version = {
                "install_id": install_id, "version_id": version_id, "scope": scope, "tenant_id": tenant_id,
                "intake_id": intake_id, "source": intake.get("source"),
                "source_sha256": report["source_sha256"], "source_bytes": report["source_bytes"],
                "runnable_sha256": report["runnable_sha256"], "runnable_bytes": report["runnable_bytes"],
                "object": object_name, "files": report["files"], "unpacked_bytes": report["unpacked_bytes"],
                "manifest": report["manifest"], "manifest_source": report.get("manifest_source"),
                "components": report["components"], "overlay": report.get("overlay"),
                # The overlay as the admin wrote it, so an update can send it again.
                "overlay_requested": report.get("overlay_requested"),
                "requires": report["requires"], "created_at": now, "created_by": by,
            }
            stored_version = _signed("version", version, keys)
            txn.set(self._version_ref(install_id, version_id), stored_version)
            if install is None:
                name = report["manifest"]["name"]
                txn.set(self._install_ref(install_id), _signed("install", {
                    "install_id": install_id, "scope": scope, "tenant_id": tenant_id, "name": name,
                    "display_name": name, "source": intake.get("source"), "current_version": version_id,
                    "built_in": False, "state": STATE_ACTIVE, "removed_at": None,
                    "created_at": now, "created_by": by, "updated_at": now, "updated_by": by,
                }, keys))
                action = AUDIT_INSTALLED
            else:
                action = AUDIT_UPDATE_CHECKED
            intake.update({"state": INTAKE_PINNED, "version_id": version_id, "install_id": install_id})
            txn.set(ref, _signed("intake", intake, keys))
            txn.set(audit_ref, _audit_row(action, by=by, at=now, scope=scope, tenant_id=tenant_id, detail={
                "intake_id": intake_id, "install_id": install_id, "version_id": version_id,
                "runnable_sha256": report["runnable_sha256"], "source_sha256": report["source_sha256"],
                "files": report["files"],
                **({"current_version": install.get("current_version")} if install else {})}))
            result.update(stored_version)

        _write(transaction)
        if refused:
            raise PluginRefused(refused[0], status=409)
        return result or None

    def apply(self, install_id: str, version_id: str, *, by: str) -> dict[str, Any]:
        """Make `version_id` current, audited `plugin_updated` with both
        versions and digests. The caller has checked the diff and its digest."""
        keys = self._mac.keys
        now = self._now()
        ref = self._install_ref(install_id)
        audit_ref = self._audit_ref()
        result: dict[str, Any] = {}
        refused: list[str] = []
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            install = self._verified("install", _snapshot(txn.get(ref)), {"install_id": install_id})
            if install is None or install.get("state") != STATE_ACTIVE:
                refused.append("install_not_active")
                return
            if is_bundled(install):
                # Only a ready bundle moves a bundled install's current
                # version (section 1.4.3): `pluginbundle.start_apply` builds
                # it, and `move_current` moves it in the ready transaction.
                refused.append("install_bundled")
                return
            target = self._verified("version", _snapshot(txn.get(self._version_ref(install_id, version_id))),
                                    {"install_id": install_id, "version_id": version_id})
            if target is None:
                refused.append("version_not_found")
                return
            previous_id = install.get("current_version")
            if previous_id == version_id:
                refused.append("version_is_current")
                return
            previous = self._verified("version", _snapshot(txn.get(self._version_ref(install_id, previous_id))),
                                      {"install_id": install_id, "version_id": previous_id}) if previous_id else None
            install.update({"current_version": version_id, "updated_at": now, "updated_by": by})
            stored = _signed("install", install, keys)
            txn.set(ref, stored)
            txn.set(audit_ref, _audit_row(
                AUDIT_UPDATED, by=by, at=now, scope=install["scope"], tenant_id=install.get("tenant_id"),
                detail={"install_id": install_id, "from_version": previous_id, "to_version": version_id,
                        "from_sha256": (previous or {}).get("runnable_sha256"),
                        "to_sha256": target.get("runnable_sha256")}))
            result.update(stored)

        _write(transaction)
        if refused:
            raise PluginRefused(refused[0], status=409 if refused[0] != "version_not_found" else 404)
        return result

    def move_current(self, txn: Any, install: Mapping[str, Any], version_id: str, *, by: str,
                     bundle_id: str) -> dict[str, Any]:
        """Inside the caller's transaction (the one that marks a bundle ready,
        or rolls back to a retained one), make `version_id` a bundled
        install's current version, audited `plugin_updated` with the bundle
        id. `install` is the verified install read in that transaction. The
        one writer of a bundled install's `current_version`."""
        install_id = install["install_id"]
        previous_id = install.get("current_version")
        moved = dict(install)
        moved.update({"current_version": version_id, "updated_at": self._now(), "updated_by": by})
        stored = _signed("install", moved, self._mac.keys)
        txn.set(self._install_ref(install_id), stored)
        txn.set(self._audit_ref(), _audit_row(
            AUDIT_UPDATED, by=by, at=self._now(), scope=install["scope"], tenant_id=install.get("tenant_id"),
            detail={"install_id": install_id, "from_version": previous_id, "to_version": version_id,
                    "bundle_id": bundle_id}))
        return stored

    def remove(self, install_id: str, *, by: str) -> dict[str, Any]:
        """Mark the install removed, audited `plugin_removed`. Its versions
        stay, so a step already signed with one still names a real record."""
        keys = self._mac.keys
        now = self._now()
        ref = self._install_ref(install_id)
        audit_ref = self._audit_ref()
        result: dict[str, Any] = {}
        refused: list[str] = []
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            install = self._verified("install", _snapshot(txn.get(ref)), {"install_id": install_id})
            if install is None or install.get("state") != STATE_ACTIVE:
                refused.append("install_not_active")
                return
            install.update({"state": STATE_REMOVED, "removed_at": _iso(now), "updated_at": now, "updated_by": by})
            stored = _signed("install", install, keys)
            txn.set(ref, stored)
            txn.set(audit_ref, _audit_row(
                AUDIT_REMOVED, by=by, at=now, scope=install["scope"], tenant_id=install.get("tenant_id"),
                detail={"install_id": install_id, "current_version": install.get("current_version")}))
            result.update(stored)

        _write(transaction)
        if refused:
            raise PluginRefused(refused[0], status=409)
        return result

    def timed_out(self, intake: Mapping[str, Any]) -> bool:
        deadline = _parse_iso(intake.get("deadline"))
        return intake.get("state") in (INTAKE_AWAITING_UPLOAD, INTAKE_RUNNING) \
            and deadline is not None and self._now() > deadline


def visible_to(install: Mapping[str, Any], tenant_id: str) -> bool:
    """A platform install is every workspace's to name; a workspace install, its own only."""
    return install.get("scope") == PLATFORM or (
        install.get("scope") == WORKSPACE and install.get("tenant_id") == tenant_id)


def refuse_removed(store: PluginStore, tenant_id: str, names: Iterable[str], *,
                   field: str = "plugins") -> None:
    """Refuse a submission that names a removed install (the owner's answer to
    Q5): 422 `plugin_removed`, its `install_id` as a separate field. Names
    that are not install ids (the built-ins) pass through untouched; an install
    another workspace owns is not this workspace's to learn about, and is left
    to the catalogue check that refuses every unknown name."""
    for name in names:
        if not isinstance(name, str) or not INSTALL_ID.match(name):
            continue
        install = store.install(name)
        if install is not None and visible_to(install, tenant_id) and install.get("state") == STATE_REMOVED:
            raise PluginRefused("plugin_removed", install_id=name, field=field)


# --- who uses an install (section 9.2) ------------------------------------------

#: Each list the usage answer carries is cut at this many rows, and the answer
#: says `truncated: true`: Remove shows who uses a plugin, it is not a report.
USAGE_CAP = 200
#: At most this many documents are read per collection to find those rows; a
#: read that stops here says `truncated: true` too, never "nobody".
USAGE_SCAN = 2000

#: The `plugins` block of `workspace_settings/{tenant}` (plugins.FIELD, its
#: `allowed` and `defaults`), restated: plugins.py imports this module.
_SETTINGS_BLOCK = "plugins"
_TASKS = "tasks"
_UNFINISHED = ("SUBMITTED", "QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING")


def _signed_names(tenant_id: str, doc: Mapping[str, Any] | None, keys: tuple[str, ...]) -> tuple[set[str], set[str]]:
    """The workspace's allowed and every-step names, from its `plugins` block
    only when its MAC verifies: an unsigned list is nobody's choice."""
    block = (doc or {}).get(_SETTINGS_BLOCK) if isinstance(doc, Mapping) else None
    if not isinstance(block, Mapping) or not block_verifies(tenant_id, _SETTINGS_BLOCK, block, keys):
        return set(), set()

    def names(key: str) -> set[str]:
        found = block.get(key)
        return {n for n in found if isinstance(n, str)} if isinstance(found, list) else set()

    return names("allowed"), names("defaults")


def _spec_steps(spec: Any, install_id: str, *, default: bool) -> tuple[list[str], bool]:
    """The step ids of a stored workflow spec that get the install, and whether
    any of them names it itself (else every one gets it from the default)."""
    steps = spec.get("steps") if isinstance(spec, Mapping) else None
    found: list[str] = []
    added = False
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, Mapping) or not isinstance(step.get("step_id"), str):
            continue
        named = isinstance(step.get("plugins"), Mapping) and install_id in step["plugins"]
        off = isinstance(step.get("plugins_off"), list) and install_id in step["plugins_off"]
        if named or (default and not off):
            found.append(step["step_id"])
            added = added or named
    return found, added


def _capped(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> tuple[list[dict[str, Any]], bool]:
    rows.sort(key=key)
    return rows[:USAGE_CAP], len(rows) > USAGE_CAP


def install_usage(db: Any, install: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Who uses `install` (section 9.2), for Remove to show before it removes.

    `workspaces`: those whose signed `plugins` block allows it or makes it an
    every-step default; a workspace install is its own workspace's to use
    with no opt-in, so that workspace is listed as allowing it. `workflows`:
    the stored workflow specs (a `custom-prompt` schedule's `params.spec`,
    kind `schedule-template`) with a step that names it or gets it from the
    workspace's default, and `schedules`: the schedules that run them.
    `steps`: steps not finished whose dispatch block (`metadata.dispatch.
    plugins`, signed with the step) pins it. A platform install is looked for
    in every workspace, a workspace install in its own only. Each list holds
    at most USAGE_CAP rows, and at most USAGE_SCAN documents are read per
    collection; either cut sets `truncated`."""
    # Imported here: the schedule modules import the submission's, which
    # import this module.
    from .schedtypes.custom_prompt import TYPE as CUSTOM_PROMPT
    from .schedules import COLLECTION as SCHEDULES

    install_id = install["install_id"]
    tenant_id = install.get("tenant_id") if install.get("scope") == WORKSPACE else None
    truncated = False

    def scan(collection: str, *filters: FieldFilter, fields: tuple[str, ...]) -> list[Any]:
        nonlocal truncated
        query = db.collection(collection)
        if tenant_id is not None and collection != SETTINGS_COLLECTION:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        for clause in filters:
            query = query.where(filter=clause)
        found = list(query.select(list(fields)).limit(USAGE_SCAN + 1).stream())
        if len(found) > USAGE_SCAN:
            truncated = True
        return found[:USAGE_SCAN]

    if tenant_id is None:
        settings = [(s.id, s.to_dict()) for s in scan(SETTINGS_COLLECTION, fields=(_SETTINGS_BLOCK,))]
    else:
        snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get()
        settings = [(tenant_id, snap.to_dict() if snap.exists else None)]
    workspaces: list[dict[str, Any]] = []
    defaults: set[str] = set()
    for tenant, doc in settings:
        allowed, every_step = _signed_names(tenant, doc, keys)
        allows = install_id in allowed or tenant == tenant_id
        if install_id in every_step:
            defaults.add(tenant)
        if allows or install_id in every_step:
            workspaces.append({"tenant_id": tenant, "allowed": allows, "every_step": install_id in every_step})

    workflows: list[dict[str, Any]] = []
    schedules: list[dict[str, Any]] = []
    for snap in scan(SCHEDULES, FieldFilter("type", "==", CUSTOM_PROMPT),
                     fields=("tenant_id", "name", "params")):
        doc = snap.to_dict() or {}
        owner = doc.get("tenant_id")
        params = doc.get("params")
        if not isinstance(owner, str) or not isinstance(params, Mapping):
            continue
        steps, added = _spec_steps(params.get("spec"), install_id, default=owner in defaults)
        if not steps:
            continue
        name = doc.get("name") if isinstance(doc.get("name"), str) else snap.id
        workflows.append({"tenant_id": owner, "id": snap.id, "name": name, "kind": "schedule-template",
                          "steps": steps, "how": "added" if added else "default"})
        schedules.append({"tenant_id": owner, "schedule_id": snap.id, "name": name})

    steps_now: list[dict[str, Any]] = []
    for snap in scan(_TASKS, FieldFilter("state", "in", list(_UNFINISHED)),
                     fields=("tenant_id", "state", "workflow_id", "step_id", "metadata.dispatch.plugins")):
        doc = snap.to_dict() or {}
        metadata = doc.get("metadata")
        dispatch = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
        pinned = dispatch.get("plugins") if isinstance(dispatch, Mapping) else None
        if not isinstance(pinned, Mapping) or install_id not in pinned:
            continue
        steps_now.append({"tenant_id": doc.get("tenant_id"), "task_id": snap.id,
                          "workflow_id": doc.get("workflow_id"), "step": doc.get("step_id"),
                          "state": doc.get("state")})

    answer: dict[str, Any] = {}
    for name, rows, key in (
            ("workspaces", workspaces, lambda r: r["tenant_id"]),
            ("workflows", workflows, lambda r: (r["tenant_id"], r["name"], r["id"])),
            ("schedules", schedules, lambda r: (r["tenant_id"], r["name"], r["schedule_id"])),
            ("steps", steps_now, lambda r: (str(r["tenant_id"]), str(r["workflow_id"] or ""), r["task_id"]))):
        answer[name], cut = _capped(rows, key)
        truncated = truncated or cut
    answer["truncated"] = truncated
    return answer


__all__ = [
    "AUDIT_ACTIONS",
    "INSTALLS",
    "INTAKES",
    "LIMITS",
    "MAX_PACKED_BYTES",
    "PluginRefused",
    "PluginStore",
    "REFUSALS",
    "VERSIONS",
    "USAGE_CAP",
    "USAGE_SCAN",
    "diff_digest",
    "install_usage",
    "install_view",
    "parse_report",
    "record_mac",
    "record_verifies",
    "refuse_removed",
    "scope_prefix",
    "version_diff",
    "version_view",
    "visible_to",
]
