"""The platform's closed agent plugin catalogue: a runtime capability manifest entry per plugin.

docs/design/agent-plugins.md, "A closed plugin catalogue"; the owner merged
plugins into the agent quality program on 3 November 2026, so the catalogue
is a runtime capability manifest entry and a step's plugins ride in its work
contract (`capabilities.plugins`). Per plugin: its pinned version, the
read-only path the agent images install it at, the runner profiles that can
load it, and per mode the skills it may offer. A caller names plugins, modes
and skills from here by name only, never a path, command, URL or version
(invariant 10). `workcontract.PLUGIN_CATALOGUE` decodes contracts against it;
`plugins.check_step` checks a step against it at submission;
`agent_worker.plugins` restates it (the worker imports nothing from swarm-api)
and tests/unit/worker/test_plugins_start.py holds the two equal.

THE BUILT-INS SWITCH (docs/design/managed-plugins.md sections 1.4.1 and
1.4.5, lane 17 of section 17.1). `SWARM_BUILTINS_MANAGED`, exactly `on`,
moves gstack and security-audit onto the managed path: their two platform
installs, `builtin:gstack` and `builtin:security-audit` (`build: "image"`),
are seeded from `BUNDLED` (`seed`), and a step naming them resolves to the
install's current version as a managed entry with its mode and skills
(pluginconfig.step_plugins), never `capabilities.plugins`. Anything else,
unset included, is off: every step resolves exactly as before, and the
code entries below stay one release so a step signed through
`capabilities.plugins` before the switch still runs (section 1.4.1).

Pure apart from `seed`, which writes through the Firestore client it is
handed: no network, no clock but the `now` it is handed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from google.cloud import firestore

from swarm_common.admission import _snapshot

from . import pluginstore as ps
from .admins import AUDIT_COLLECTION
from .judgment import MAC_FIELD


@dataclass(frozen=True)
class PluginEntry:
    """One catalogue plugin: a runtime capability manifest entry."""

    name: str
    version: str
    install_path: str
    runner_profiles: tuple[str, ...]
    #: Per mode, the skills a step may name.
    skills: Mapping[str, tuple[str, ...]] = field(hash=False)
    #: Where the plugin's skill folders sit under `install_path`, for a
    #: plugin whose SKILL.md reads its sibling files by relative path: the
    #: worker links each step skill's folder whole. None is gstack's shape
    #: (one `<skill>/SKILL.md` per skill, the plugin root linked by name).
    skill_folders: str | None = None


#: The modes a step may name, in `swarm_common.workcontract`'s vocabulary.
MODES = ("unattended", "planner", "designer")
#: The modes a step may run today. Planner and designer interview a person,
#: which needs the question bridge (docs/design/agent-plugins.md, "The question
#: bridge"); until it lands they are refused with `BRIDGE_REASON`.
SUPPORTED_MODES = ("unattended",)
BRIDGE_REASON = (
    "runs an interview with a person, which needs the question bridge; until it "
    "lands only unattended mode runs"
)

#: The six claude-code profiles (`swarm_common.profiles`), the only runners
#: that load the catalogue's skills. codex comes later.
CLAUDE_CODE_PROFILES = (
    "claude-code",
    "claude-code-large",
    "claude-code-medium",
    "claude-code-review",
    "claude-code-browser",
    "claude-code-lightpanda",
)

#: gstack at the release the packaging spike measured (v1.92.55.0, the build
#: mirror's pinned archive), at the path its build stage renders the skills for.
GSTACK = PluginEntry(
    name="gstack",
    version="1.92.55.0",
    install_path="/opt/swarm/plugins/gstack",
    runner_profiles=CLAUDE_CODE_PROFILES,
    skills={
        # No `cso`: left out of the first catalogue (owner, 3 November 2026).
        "unattended": (
            "review", "investigate", "health", "qa-only", "qa", "design-review",
            "document-generate", "document-release", "ship", "plan-eng-review",
            "plan-design-review", "autoplan",
        ),
        "planner": (
            "office-hours", "plan-ceo-review", "plan-eng-review",
            "plan-devex-review", "autoplan", "spec",
        ),
        "designer": (
            "design-consultation", "plan-design-review", "design-review", "design-html",
        ),
    },
)

#: Cloudflare's security audit skill (github.com/cloudflare/security-audit-skill,
#: MIT), the second plugin (owner, 4 November 2026), at the commit the build
#: mirror pins (14 September 2026). One skill folder, skills/security-audit/,
#: whose SKILL.md reads its method files, report schema and two Node
#: validators beside it. Guidance mode answers a focused question; full audit
#: mode writes its reports to an output folder outside the checkout. No new
#: runner profile and no new image: the base image carries it, so every
#: claude-code profile's image does (docs/design/agent-plugins-packaging.md,
#: "The second plugin").
SECURITY_AUDIT = PluginEntry(
    name="security-audit",
    version="c1c8a8c1471069fb0e188eeaff69b8e8db6564a8",
    install_path="/opt/swarm/plugins/security-audit",
    runner_profiles=CLAUDE_CODE_PROFILES,
    skills={"unattended": ("security-audit",)},
    skill_folders="skills",
)

CATALOGUE: Mapping[str, PluginEntry] = {GSTACK.name: GSTACK, SECURITY_AUDIT.name: SECURITY_AUDIT}


#: A built-in's id in the managed catalogue's data view
#: (docs/design/managed-plugins.md section 1.4): never an install id
#: (`p-`/`w-`), so no install can collide with one.
BUILT_IN_PREFIX = "builtin:"


def builtin_entries() -> list[dict[str, object]]:
    """The built-ins as entries of the managed catalogue's view, beside the
    Firestore installs (swarm_api.pluginstore.install_view's shape): image-baked,
    version as the image pins it, no Remove and no Update (an image release
    updates one). The install path is the image's and is not served."""
    return [
        {
            "install_id": BUILT_IN_PREFIX + entry.name,
            "name": entry.name,
            "display_name": entry.name,
            "scope": "platform",
            "tenant_id": None,
            "built_in": True,
            "state": "active",
            "source": {"kind": "image"},
            "current_version": None,
            "version": entry.version,
            "runner_profiles": list(entry.runner_profiles),
            "skills": {mode: list(skills) for mode, skills in entry.skills.items()},
            "hosts": [],
            "removable": False,
            "updatable": False,
        }
        for entry in CATALOGUE.values()
    ]


# --- the built-ins switch and the seed (section 1.4.1, 1.4.5) -------------------

#: swarm-api's switch (Terraform `var.builtins_managed`): on only when it is
#: exactly "on". Off by default on every deployment; nothing here turns it on.
SWITCH_ENV = "SWARM_BUILTINS_MANAGED"


def builtins_managed(environ: Mapping[str, str] | None = None) -> bool:
    """The built-ins switch. Written `(os.environ if environ is None else
    environ).get(...)` so scripts/lib/check-env-parity.sh sees the read."""
    return (os.environ if environ is None else environ).get(SWITCH_ENV, "").strip() == "on"


@dataclass(frozen=True)
class BundledPin:
    """A bundled plugin's first version: what the release image carries,
    as images/agent-runtime-base/bundled-plugins.json states it (a unit test
    holds the two equal). No upstream location: the repository an update
    reads is named by the admin who asks for it (generic content)."""

    entry: PluginEntry
    source_sha256: str
    commit: str

    @property
    def install_id(self) -> str:
        return BUILT_IN_PREFIX + self.entry.name

    @property
    def version_id(self) -> str:
        """Deterministic, so two instances seeding at once write the same
        documents: `v-` and 12 hex of the install id and the source digest."""
        digest = hashlib.sha256(f"{self.install_id}:{self.source_sha256}".encode()).hexdigest()
        return "v-" + digest[:12]


#: The two bundled built-ins, at the pins of the release image.
BUNDLED: tuple[BundledPin, ...] = (
    BundledPin(GSTACK, source_sha256="63c118f13ccf0d72d06d7bff56231dc08ce3da29ce405ed567224dd303cf5bd2",
               commit="2512dd7624a474fee036326989ea0c6bb8470e69"),
    BundledPin(SECURITY_AUDIT, source_sha256="53e708ebf770dfe35b57df0586a5058707081a870cd149ce30a2d57c51ed29b6",
               commit=SECURITY_AUDIT.version),
)
BUNDLED_BY_ID: Mapping[str, BundledPin] = {pin.install_id: pin for pin in BUNDLED}
#: Who the seeded records name as their writer.
SEED_BY = "swarm-api"


def bundled_id(name: str) -> str | None:
    """A bundled plugin's reserved install id for its bare name or its id."""
    install_id = name if name.startswith(BUILT_IN_PREFIX) else BUILT_IN_PREFIX + name
    return install_id if install_id in BUNDLED_BY_ID else None


def seed_records(pin: BundledPin, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """The install and its first version, unsigned (section 1.4.1): platform
    scope, `built_in`, `build: "image"`, current at once; the version's
    per-mode skills in its overlay as `modes`, its runner profiles as its
    declared `requires`. Nothing is unpacked per attempt, so no object."""
    entry = pin.entry
    source = {"kind": "image", "commit": pin.commit, "sha256": pin.source_sha256}
    version = {
        "install_id": pin.install_id, "version_id": pin.version_id, "scope": ps.PLATFORM, "tenant_id": None,
        "intake_id": None, "source": source, "source_sha256": pin.source_sha256, "source_bytes": None,
        "runnable_sha256": None, "runnable_bytes": None, "object": None, "files": None, "unpacked_bytes": None,
        "manifest": {"name": entry.name, "version": entry.version, "description": None, "user_config": []},
        "manifest_source": "image",
        "components": {"skills": [{"name": s} for s in sorted({s for m in entry.skills.values() for s in m})],
                       "commands": [], "agents": [], "hooks": [], "mcp_servers": []},
        "overlay": {"modes": {mode: list(skills) for mode, skills in entry.skills.items()}},
        "overlay_requested": None,
        "requires": {"declared": {"profiles": list(entry.runner_profiles), "hosts": [], "runtimes": []},
                     "suggested": None},
        "created_at": now, "created_by": SEED_BY,
    }
    install = {
        "install_id": pin.install_id, "scope": ps.PLATFORM, "tenant_id": None, "name": entry.name,
        "display_name": entry.name, "source": source, "current_version": pin.version_id,
        "built_in": True, "build": ps.BUILD_IMAGE, "state": ps.STATE_ACTIVE, "removed_at": None,
        "created_at": now, "created_by": SEED_BY, "updated_at": now, "updated_by": SEED_BY,
    }
    return install, version


def _signed(kind: str, fields: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    stored = {k: v for k, v in fields.items() if k != MAC_FIELD}
    stored[MAC_FIELD] = ps.record_mac(kind, stored, keys)
    return stored


def seed(db: Any, keys: tuple[str, ...], now: datetime) -> list[str]:
    """Write the bundled installs and their first versions that are not
    there yet, MAC'd, in one transaction, audited `plugin_installed`; the ids
    written. An install document already there, in any state, is left alone:
    a second instance's identical write, or a seed after a removal or an
    update, writes nothing. Raises `PluginRefused("settings_key_missing")`
    with no key: a catalogue record is never stored unsigned."""
    if not keys:
        raise ps.PluginRefused("settings_key_missing", status=503)
    refs = {pin.install_id: db.collection(ps.INSTALLS).document(pin.install_id) for pin in BUNDLED}
    if all(ref.get().exists for ref in refs.values()):
        return []
    written: list[str] = []
    transaction = db.transaction()

    @firestore.transactional
    def _write(txn: Any) -> None:
        written.clear()
        missing = [pin for pin in BUNDLED if not _snapshot(txn.get(refs[pin.install_id])).exists]
        for pin in missing:
            install, version = seed_records(pin, now)
            txn.set(refs[pin.install_id].collection(ps.VERSIONS).document(pin.version_id),
                    _signed("version", version, keys))
            txn.set(refs[pin.install_id], _signed("install", install, keys))
            # pluginstore's audit row shape, so the catalogue's audit reads it.
            txn.set(db.collection(AUDIT_COLLECTION).document(), {
                "action": ps.AUDIT_INSTALLED, "by": SEED_BY, "at": now, "target_tenant_id": None,
                "detail": {"scope": ps.PLATFORM, "install_id": pin.install_id, "version_id": pin.version_id,
                           "source_sha256": pin.source_sha256, "seeded": True}})
            written.append(pin.install_id)

    _write(transaction)
    return list(written)


def ensure_seeded(store: "ps.PluginStore") -> list[str]:
    """`seed` through the catalogue's store, when either bundled install
    does not read back verified: the first catalogue read or submission
    with the switch on (section 1.4.1). The ids written."""
    if all(store.install(pin.install_id) is not None for pin in BUNDLED):
        return []
    # The store's own client and clock, so the seed is the catalogue's write.
    return seed(store._db, store.keys, store._now())  # noqa: SLF001


def offered_skills() -> dict[str, dict[str, tuple[str, ...]]]:
    """Per plugin, per mode, the skills it may offer: the shape
    `workcontract.PLUGIN_CATALOGUE` decodes a contract against."""
    return {name: dict(entry.skills) for name, entry in CATALOGUE.items()}
