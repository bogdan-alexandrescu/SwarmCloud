"""Which backend a workspace's Jev judgments go to (JUDGE-SELECT).

Owner decisions 2026-10-14. Jev (jev-browser, on the live browser) asks typed
questions about page state. Its answers can come from three backends, chosen
per workspace:

  * `internal-jev` -- THE DEFAULT. Your own internal decision API on the
    tailnet (`/jev`), reached through the ONE Tailscale egress Service the
    operator runs in swarm-system (JUDGE-NET). Page text stays inside your network,
    and no TypeSafe key is needed or sent.
  * `internal-laya` -- the same API's `/laya` path.
  * `typesafe` -- api.typesafe.ai, jev-browser's own default. Page text goes
    to TypeSafe, and a TypeSafe key must resolve (`provider_keys`).

WHERE THE CHOICE LIVES. `workspace_settings/{tenant_id}`, field
`judgment_backend`, beside nothing else yet: no per-workspace settings store
existed on main when this was written (the brief named TRAIN-WS and the
merge settings; neither stores per-workspace documents here), and the frozen
`Tenant` has no field for it. A document of its own rather than a field
written into `tenants/` keeps every reader of the frozen type untouched.
Absent means the default. Every change writes an `admin_audit` entry in the
same transaction.

WHO MAY CHANGE IT. The owner of a personal workspace (`kind: user`, its
principal the caller's verified email); in a group workspace its `admin`
(docs/design/workspace-roles.md §4.1, Q13), a workspace admin or a platform
admin, exactly as for a tenant TypeSafe key. The gate is
`routes/judgment.py::put_judgment_backend`.

AVAILABILITY never changes the choice and never falls back. An internal
backend is available when the platform renders INTERNAL_DECISION_API_URL
(the tailnet egress is enabled); TypeSafe when a TypeSafe key resolves for
the caller in that workspace. A chosen backend that is unavailable means Jev
is unavailable -- the dispatcher and the jev wrapper refuse rather than send
page text to TypeSafe in place of an internal backend.

`scheduler.judgment` restates the backend names, the collection and the
service address for the dispatcher (the scheduler does not import swarm-api);
tests/unit/control_plane/test_dispatch_judgment.py holds the two equal.

THE SETTINGS MAC (#1457; docs/design/tenant-settings-integrity.md §3.4 and
§4.5, owner decision Q2 2026-10-17). Every tenant worker can write any
`workspace_settings/<tenant>` document whose id it can name, so a value the
control plane acts on must carry proof that swarm-api wrote it. Each writer
(`JudgmentSettings.set` here, `mergesettings`, `codeintel`) stores, beside the
block it wrote, `mac: {"v": 1, "key": <key id>, "sig": <base64>}`, where `sig`
is HMAC-SHA256 under the `swarm-settings-key` secret over
`swarm_common.specsign.canonical_settings_block(tenant_id, block, fields)`.
The block is "top" (this module's `judgment_backend`, so the MAC is the
document's top-level `mac`), "merge" or "code_intelligence" (the map's own
`mac` key). The key reaches swarm-api as SWARM_SETTINGS_KEY, the one it
replaced as SWARM_SETTINGS_KEY_PREVIOUS while a rotation re-signs, both read
into `ApiSettings` at start like the rest of its configuration and handed to
each reader as a `SettingsMac`; with no key configured nothing is signed,
which is what every write did before this.

A reader acts on a block only when its MAC verifies, for the tenants listed
in SETTINGS_MAC_TENANTS (comma-separated, or `*`) -- the per-tenant cutover,
dev's tenants first. It ships EMPTY, so every reader behaves as it did before
this lane until `scripts/resign-workspace-settings.sh` has signed the stored
documents and a tenant is switched: switching one on before that reads its
unsigned settings as unset, which for `judgment_backend` is `internal-jev`
(fail closed, as `stored_backend` already is for anything unknown).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_common.specsign import SETTINGS_MAC_FIELDS, canonical_settings_block

from .admins import AUDIT_COLLECTION
from .errors import ValidationFailed

INTERNAL_JEV = "internal-jev"
INTERNAL_LAYA = "internal-laya"
TYPESAFE_BACKEND = "typesafe"
#: In the order the console offers them.
BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA, TYPESAFE_BACKEND)
#: Owner decision 2026-10-14: page text stays inside your network unless a workspace
#: chooses otherwise.
DEFAULT_BACKEND = INTERNAL_JEV
INTERNAL_BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA)

#: What the Submit picker and the browser strip show.
LABELS = {
    INTERNAL_JEV: "Jev (Internal)",
    INTERNAL_LAYA: "Jev (Internal Laya)",
    TYPESAFE_BACKEND: "Jev (TypeSafe)",
}

#: One document per workspace, keyed by tenant id.
SETTINGS_COLLECTION = "workspace_settings"
SETTING_FIELD = "judgment_backend"
AUDIT_JUDGMENT_SET = "judgment_backend_set"

#: The internal decision API by its TAILNET NAME, over https, as a judging pod calls
#: it: the pod's hostAliases entry resolves the name to JUDGE-NET's egress
#: Service, which passes TLS through, so the API's own certificate verifies
#: (owner decision 2026-10-15, #1380). The only value INTERNAL_DECISION_API_URL
#: may hold besides "" (off): the jev wrapper accepts exactly this host, so a
#: second spelling here would be one the wrapper refuses. Restates
#: `scheduler.judgment.INTERNAL_DECISION_ORIGIN`;
#: tests/unit/control_plane/test_dispatch_judgment.py holds them equal.
INTERNAL_DECISION_ORIGIN = "https://decision-api-staging.taile0bda9.ts.net"

NO_EGRESS = ("the tailnet egress to the decision API is not enabled on this platform "
             "(INTERNAL_DECISION_API_URL is unset)")
NO_KEY = ("no TypeSafe key resolves for you in this workspace: set your own key, "
          "or ask an admin to set a tenant key")


#: Each reason sentence above as the code the live read's `browser.jev.reason`
#: carries (`live.JEV_REASONS`, the worker's `browser_tools.JEV_REASONS`):
#: `no_route` is what the runner reports when the dispatcher sent no
#: JEV_API_URL to an internal backend, `no_key` when TypeSafe has no key.
UNAVAILABLE_CODES = {NO_EGRESS: "no_route", NO_KEY: "no_key"}


#: The MAC beside each signed block (§4.5): the document's own top level for
#: "top", the map's own key for "merge" and "code_intelligence".
MAC_FIELD = "mac"
MAC_VERSION = 1
#: The block names of `swarm_common.specsign.SETTINGS_MAC_FIELDS`.
TOP_BLOCK = "top"
#: The Secret Manager secret (§5 request B), created by
#: `scripts/create-secrets.sh --settings-mac-key`, and the variables it and
#: the one it replaced reach swarm-api as (`ApiSettings.settings_key`,
#: `settings_key_previous`).
SETTINGS_KEY_SECRET_ID = "swarm-settings-key"
SETTINGS_KEY_ENV = "SWARM_SETTINGS_KEY"
SETTINGS_KEY_PREVIOUS_ENV = "SWARM_SETTINGS_KEY_PREVIOUS"
#: The per-tenant cutover (owner Q7): who has their settings verified
#: (`ApiSettings.settings_mac_tenants`). Empty by default, so nothing is
#: verified until an operator lists a tenant.
SETTINGS_MAC_TENANTS_ENV = "SETTINGS_MAC_TENANTS"
#: The key id's own domain, so the id is never a MAC of anything a reader acts on.
_KEY_ID_DOMAIN = b"swarm-settings-key-id/v1\0"


def settings_keys(current: str, previous: str = "") -> tuple[str, ...]:
    """The key ring, current first: the keys a stored MAC may verify under.
    A blank key is no key."""
    keys = ((current or "").strip(), (previous or "").strip())
    return tuple(k for k in keys if k)


@dataclass(frozen=True)
class SettingsMac:
    """What one reader signs and verifies under: the key ring, current first,
    and the tenants cut over. The default -- no key, nobody cut over -- is
    every reader as it was before ISO-SETTINGS-MAC. The keys are excluded from
    the repr, as `ApiSettings` excludes them, so a logged reader cannot carry
    them."""

    keys: tuple[str, ...] = field(default=(), repr=False)
    tenants: tuple[str, ...] = ()


def settings_mac(settings: Any) -> SettingsMac:
    """The readers' `SettingsMac` from `ApiSettings` (settings_key,
    settings_key_previous, settings_mac_tenants). Read with a default, as the
    services read every setting, so a stand-in settings object without them
    is a deployment with no key and nobody cut over."""
    return SettingsMac(
        keys=settings_keys(getattr(settings, "settings_key", ""),
                           getattr(settings, "settings_key_previous", "")),
        tenants=tuple(getattr(settings, "settings_mac_tenants", ()) or ()),
    )


def key_id(key: str) -> str:
    """Which key made a MAC, without saying anything about the key: 16 hex
    characters of a SHA-256 under its own domain. The re-sign job reads it to
    find MACs made under a key that has since been rotated out."""
    return hashlib.sha256(_KEY_ID_DOMAIN + key.encode("utf-8")).hexdigest()[:16]


def _sig(key: str, tenant_id: str, block: str, fields: Mapping[str, Any]) -> bytes:
    return hmac.new(key.encode("utf-8"),
                    canonical_settings_block(tenant_id=tenant_id, block=block, fields=fields),
                    hashlib.sha256).digest()


def sign_block(key: str, *, tenant_id: str, block: str,
               fields: Mapping[str, Any]) -> dict[str, Any]:
    """The `mac` value for `fields` as block `block` of `tenant_id`'s document.
    Only the keys SETTINGS_MAC_FIELDS lists for the block are covered."""
    return {"v": MAC_VERSION, "key": key_id(key),
            "sig": base64.b64encode(_sig(key, tenant_id, block, fields)).decode("ascii")}


def block_mac(tenant_id: str, block: str, fields: Mapping[str, Any],
              keys: tuple[str, ...]) -> dict[str, Any] | None:
    """The `mac` a writer stores with `fields`, under the current key (the
    ring's first); None when this deployment has no key, so the write is what
    it was before."""
    return sign_block(keys[0], tenant_id=tenant_id, block=block, fields=fields) if keys else None


def block_verifies(tenant_id: str, block: str, fields: Mapping[str, Any] | None,
                   keys: tuple[str, ...]) -> bool:
    """`fields` (the block as stored, its `mac` included) carries a MAC one of
    `keys` made over exactly its covered values, for this tenant and block."""
    if not isinstance(fields, Mapping) or not keys:
        return False
    mac = fields.get(MAC_FIELD)
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION:
        return False
    sig = mac.get("sig")
    if not isinstance(sig, str):
        return False
    try:
        presented = base64.b64decode(sig, validate=True)
    except (binascii.Error, ValueError):
        return False
    return any(hmac.compare_digest(_sig(k, tenant_id, block, fields), presented) for k in keys)


def mac_enforced(tenant_id: str, tenants: Iterable[str]) -> bool:
    """`tenant_id` is cut over: its readers act only on signed settings.
    `tenants` is the cutover list, names or `*` for everyone."""
    listed = {t.strip() for t in tenants if t.strip()}
    return "*" in listed or tenant_id in listed


def trusted_block(tenant_id: str, block: str, fields: Mapping[str, Any] | None,
                  mac: SettingsMac) -> dict[str, Any]:
    """The block a reader may act on: as stored for a tenant not yet cut over
    (today's behaviour); for one that is, as stored when its MAC verifies
    under `mac.keys` and empty -- every setting its default -- when it does not."""
    stored = dict(fields) if isinstance(fields, Mapping) else {}
    if not mac_enforced(tenant_id, mac.tenants):
        return stored
    return stored if block_verifies(tenant_id, block, stored, mac.keys) else {}


#: The covered keys each block's swarm-api writer writes: this module's
#: `set`, `mergesettings.MergeSettings.as_dict`, `codeintel.OWNED_FIELDS`,
#: `budget.COVERED` (tests/unit/control_plane/test_settings_mac.py holds them equal). A covered
#: key outside its writer's list -- today only `code_intelligence.
#: judgment_backend` -- is written by no swarm-api path, so it is present only
#: through a direct Firestore write.
WRITER_FIELDS: Mapping[str, tuple[str, ...]] = {
    TOP_BLOCK: ("judgment_backend",),
    "merge": ("mode", "train_size", "max_train_age_minutes"),
    "code_intelligence": ("jev", "context", "context_ab", "graph_delivery"),
    # Contract request 119 (#2459): `budget.BudgetStore.put` writes both.
    "budget": ("unit", "amount"),
    # Contract request 128: `qualitygates.QualityGatesStore.put` writes it.
    "quality_gates": ("jev",),
}


def unowned_covered_keys(block: str) -> tuple[str, ...]:
    """The keys the contract covers in `block` that its writer does not write.
    A writer that carried one over would sign a value it never chose -- the
    one a forger planted -- so it drops them instead."""
    return tuple(k for k in SETTINGS_MAC_FIELDS[block] if k not in WRITER_FIELDS[block])


# -- the re-sign pass (scripts/resign-workspace-settings.sh) --------------------

#: What the pass says of one block. Only UNSIGNED and STALE are written.
RESIGN_SIGNED = "signed"        # verifies under the current key: nothing to do
RESIGN_STALE = "stale"          # verifies only under another key: re-signed
RESIGN_UNSIGNED = "unsigned"    # no MAC, or one that verifies under no key: signed
RESIGN_HELD = "held"            # carries a covered key no writer writes: never signed


def _blocks(doc: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(block, stored fields) for every block of `doc` that holds a covered key."""
    out: list[tuple[str, dict[str, Any]]] = []
    for block, covered in SETTINGS_MAC_FIELDS.items():
        fields = doc if block == TOP_BLOCK else doc.get(block)
        if isinstance(fields, Mapping) and any(k in fields for k in covered):
            out.append((block, dict(fields)))
    return out


def _resign_status(tenant_id: str, block: str, fields: Mapping[str, Any], key: str) -> str:
    if any(k in fields for k in unowned_covered_keys(block)):
        return RESIGN_HELD
    if block_verifies(tenant_id, block, fields, (key,)):
        mac = fields.get(MAC_FIELD)
        return RESIGN_SIGNED if isinstance(mac, Mapping) and mac.get("key") == key_id(key) \
            else RESIGN_STALE
    return RESIGN_UNSIGNED


def resign_workspace_settings(db: Any, key: str, *, apply: bool,
                              tenant_id: str | None = None) -> dict[str, Any]:
    """Sign every stored `workspace_settings` block that does not verify under
    `key`, or plan to (`apply` False). Idempotent: a second run finds every
    block it wrote `signed` and writes nothing.

    It writes ONLY each block's `mac`, computed over the covered values
    (SETTINGS_MAC_FIELDS) as they are stored: no setting is changed, and a
    key outside the lists is neither covered nor touched. The tenant is the
    DOCUMENT ID, never its `tenant_id` field, which a worker can rewrite. A
    block carrying a covered key no writer writes is `held`, never signed:
    signing it would sign a value only a forger can have put there. Each
    document is re-read and written in its own transaction."""
    if not key:
        raise ValueError("no settings key: nothing can be signed")
    collection = db.collection(SETTINGS_COLLECTION)
    if tenant_id is not None:
        snaps = [collection.document(tenant_id).get()]
    else:
        snaps = list(collection.stream())
    rows: list[dict[str, Any]] = []
    written = 0
    for snap in snaps:
        if not snap.exists:
            continue
        tid = snap.id
        plan = [(block, fields, _resign_status(tid, block, fields, key))
                for block, fields in _blocks(snap.to_dict() or {})]
        for block, fields, status in plan:
            rows.append({"tenant_id": tid, "block": block, "status": status,
                         "covered": {k: fields.get(k) for k in SETTINGS_MAC_FIELDS[block]}})
        if not apply or not any(st in (RESIGN_STALE, RESIGN_UNSIGNED) for _, _, st in plan):
            continue
        written += _resign_one(db, collection.document(tid), tid, key)
    return {"documents": sum(1 for s in snaps if s.exists), "blocks": rows,
            "written": written, "key": key_id(key), "applied": apply}


def _resign_one(db: Any, ref: Any, tenant_id: str, key: str) -> int:
    """Sign the document's blocks again from a fresh read, in one transaction;
    how many blocks were written."""
    transaction = db.transaction()

    @firestore.transactional
    def _write(txn: Any) -> int:
        snap = _snapshot(txn.get(ref))
        if not snap.exists:
            return 0
        update: dict[str, Any] = {}
        for block, fields in _blocks(snap.to_dict() or {}):
            if _resign_status(tenant_id, block, fields, key) not in (RESIGN_STALE, RESIGN_UNSIGNED):
                continue
            mac = sign_block(key, tenant_id=tenant_id, block=block, fields=fields)
            if block == TOP_BLOCK:
                update[MAC_FIELD] = mac
            else:
                update[block] = {**fields, MAC_FIELD: mac}
        if update:
            txn.update(ref, update)
        return len(update)

    return _write(transaction)


def check_backend(value: str) -> str:
    name = (value or "").strip().lower()
    if name not in BACKENDS:
        raise ValidationFailed(f"unknown judgment backend {value!r}",
                               detail={"judgment_backends": list(BACKENDS)})
    return name


def stored_backend(doc: Mapping[str, Any] | None) -> str:
    """The stored choice, or the default for nothing or anything unknown."""
    value = (doc or {}).get(SETTING_FIELD)
    return value if isinstance(value, str) and value in BACKENDS else DEFAULT_BACKEND


def availability(*, internal_enabled: bool,
                 typesafe_key: bool) -> dict[str, tuple[bool, str | None]]:
    """Each backend -> (available, why not). Pure, so the matrix is a test."""
    internal = (True, None) if internal_enabled else (False, NO_EGRESS)
    return {
        INTERNAL_JEV: internal,
        INTERNAL_LAYA: internal,
        TYPESAFE_BACKEND: (True, None) if typesafe_key else (False, NO_KEY),
    }


def unavailable_code(backend: str, *, internal_enabled: bool, typesafe_key: bool) -> str | None:
    """Why `backend` cannot run jev, as a live-read reason code; None when it can.

    Read off `availability`, the one table that restates the dispatcher's
    condition (`scheduler.judgment.jev_dispatch` sends JEV_API_URL to an
    internal backend only when INTERNAL_DECISION_API_URL is set), so the
    console and the dispatcher cannot disagree on it (JEV-OFF, #1345)."""
    available, reason = availability(internal_enabled=internal_enabled,
                                     typesafe_key=typesafe_key)[backend]
    return None if available else UNAVAILABLE_CODES[str(reason)]


def jev_status(backend: str, *, internal_enabled: bool, typesafe_key: bool,
               set_at: Any = None, set_by: str | None = None) -> dict[str, Any]:
    """The `jev` object: the chosen backend, its label, whether it can run
    and why not, the default, who chose it, and every option.

    `backend`, `label`, `available` and `reason` are the keys the shared
    `browser.jev` object (BSTATUS-API) carries for the picker and the strip.
    """
    table = availability(internal_enabled=internal_enabled, typesafe_key=typesafe_key)
    available, reason = table[backend]
    return {
        "backend": backend,
        "label": LABELS[backend],
        "available": available,
        "reason": reason,
        "default": DEFAULT_BACKEND,
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else set_at,
        "set_by": set_by,
        "options": [
            {"backend": b, "label": LABELS[b], "available": table[b][0], "reason": table[b][1]}
            for b in BACKENDS
        ],
    }


class JudgmentSettings:
    """`workspace_settings/{tenant}.judgment_backend`, read and audited write."""

    def __init__(self, db: Any, *, now: Any, mac: SettingsMac) -> None:
        self._db = db
        self._now = now
        self._mac = mac

    def read(self, tenant_id: str) -> dict[str, Any]:
        snap = self._db.collection(SETTINGS_COLLECTION).document(tenant_id).get()
        doc = trusted_block(tenant_id, TOP_BLOCK, (snap.to_dict() or {}) if snap.exists else {},
                            self._mac)
        return {
            "judgment_backend": stored_backend(doc),
            "set_at": doc.get("judgment_backend_set_at"),
            "set_by": doc.get("judgment_backend_set_by"),
        }

    def set(self, tenant_id: str, backend: str, *, by: str) -> dict[str, Any]:
        """Store `backend` with its `admin_audit` entry, in one transaction."""
        backend = check_backend(backend)
        ref = self._db.collection(SETTINGS_COLLECTION).document(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            previous = stored_backend(trusted_block(
                tenant_id, TOP_BLOCK, snap.to_dict() if snap.exists else None, self._mac))
            written: dict[str, Any] = {
                "tenant_id": tenant_id,
                SETTING_FIELD: backend,
                "judgment_backend_set_at": now,
                "judgment_backend_set_by": by,
            }
            mac = block_mac(tenant_id, TOP_BLOCK, written, self._mac.keys)
            if mac is not None:
                written[MAC_FIELD] = mac
            txn.set(ref, written, merge=True)
            txn.set(audit, {
                "action": AUDIT_JUDGMENT_SET,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "judgment_backend": backend,
                           "previous": previous},
            })

        _write(transaction)
        return {"judgment_backend": backend, "set_at": now, "set_by": by}
