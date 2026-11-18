"""The platform switches as console settings (docs/design/managed-plugins.md section 11).

`SWARM_WORK_CONTRACTS` (`on`/`off`), `SWARM_CONTRACT_REVIEW`
(`off`/`report`/`enforce`) and `SWARM_PLUGINS` (`on`/`off`, the owner's pick
on PR 3648: a step may name plugins at all) were Terraform-set environment
values only. A platform admin now sets any of them from Admin › Plugins ›
Platform (`routes/platform_switches.py`), and the value is stored in
`platform_settings/switches`:

    {work_contracts, contract_review, plugins, version, set_by, set_at,
     mac, switches_mac}

* **The Terraform value is the default.** A switch whose stored value is
  null, or a document that is absent, reads as the environment (Terraform's
  value), and that as `off` when unset. `workcontract.enabled` and
  `contractreview.mode` keep their environment read, so
  scripts/lib/check-env-parity.sh still sees swarm-api read what Terraform
  sets; they ask `stored` first. The plugins switch's Terraform value reads
  as `on` when unset, so a deployment that sets nothing refuses nothing it
  accepted before (`plugins.environment_enabled`).
* **Signed, twice.** `mac` is HMAC-SHA256 under the `swarm-settings-key` ring
  over `specsign.canonical_platform_settings` (contract request 144, its own
  purpose), stored as `{v, key, sig}` the way a workspace block's is. That
  form covers `work_contracts` and `contract_review` only, and `swarm_common`
  is frozen, so `switches_mac` is a second MAC under the same ring over
  `canonical_switches` below: every switch, `plugins` among them, under a
  purpose of swarm-api's own (`SWITCHES_PURPOSE`), the way
  `issueruns.OWNER_PURPOSE` is. Contract request 150 asks to add `plugins` to
  `PLATFORM_SETTINGS_MAC_FIELDS`; until it is applied, the second MAC is what
  keeps a tenant worker from forging the plugins switch. A document only
  verifies when both MACs do. A document whose MACs fail reads as the
  Terraform default, never as the stored value, and logs
  `platform_switch_unsigned` once per instance. With no key a write is
  refused (409, reason `UNSIGNED_CODE`): a value nobody can verify would never
  be read.
* **Versioned.** Every write names the version it read and bumps it in one
  transaction; a stale version is 409, reason `CONFLICT_CODE`, so two admins never
  overwrite each other unseen. `version` is not covered by the MAC: it
  guards writes and no reader acts on it.
* **Audited.** Each switch whose stored value changes writes one
  `platform_switch_set` row to `admin_audit` in the same transaction:
  who, when, the stored values (null being the Terraform default) and the
  values in force before and after.
* **Cached.** The readers ask this instance's `PlatformSwitches` (installed by
  `create_app`), which reads the document at most once per `CACHE_SECONDS`,
  so a change reaches every instance within half a minute with no redeploy; a
  set through this instance clears its cache at once. A read that fails is
  logged and reads as Terraform's value.

Nothing here takes or returns a credential, an image, a command or a path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Mapping

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_common.specsign import (
    PLATFORM_SETTINGS_MAC_FIELDS,
    SpecNotCanonical,
    canonical_platform_settings,
    jcs,
)

from .admins import AUDIT_COLLECTION
from .errors import Conflict
from .judgment import MAC_FIELD, MAC_VERSION, SettingsMac, key_id

logger = logging.getLogger(__name__)

COLLECTION = "platform_settings"
DOCUMENT = "switches"

WORK_CONTRACTS = "work_contracts"
CONTRACT_REVIEW = "contract_review"
PLUGINS = "plugins"

#: Each switch's values, in the order the console offers them. The keys are
#: the contract's covered fields and then `plugins`, which only
#: `switches_mac` covers until contract request 150 is applied; every key is
#: inside `canonical_switches`, so no switch is outside a MAC.
OPTIONS: Mapping[str, tuple[str, ...]] = {
    WORK_CONTRACTS: ("off", "on"),
    CONTRACT_REVIEW: ("off", "report", "enforce"),
    PLUGINS: ("off", "on"),
}
if tuple(OPTIONS)[:len(PLATFORM_SETTINGS_MAC_FIELDS)] != PLATFORM_SETTINGS_MAC_FIELDS:
    raise RuntimeError("the platform switches do not start with the contract's covered fields")

#: The second MAC's field and the purpose of the bytes it covers: not the
#: contract's (`swarm.platform-settings/v1`) nor a workspace block's, so no
#: other MAC verifies as this one.
SWITCHES_MAC_FIELD = "switches_mac"
SWITCHES_PURPOSE = "swarm.platform-switches/v1"

VERSION = "version"
SET_AT = "set_at"
SET_BY = "set_by"

AUDIT_ACTION = "platform_switch_set"
UNSIGNED_LOG = "platform_switch_unsigned"

#: How long one instance acts on a read before reading again: the design's
#: half a minute, the longest a change waits to reach every instance.
CACHE_SECONDS = 30.0

#: `detail.reason` of the 409s a write is refused with. Both are the
#: established `conflict` code (tests/unit/control_plane/
#: test_new_refusals_ship_off.py): a route that is new refuses nothing a
#: caller relied on, so neither needs a refusal switch of its own.
CONFLICT_CODE = "platform_switches_version_conflict"
UNSIGNED_CODE = "platform_switches_unsigned"


def canonical_switches(doc: Mapping[str, Any]) -> bytes:
    """JCS of {"purpose": SWITCHES_PURPOSE, "fields"}: every switch in
    OPTIONS, an absent one null; nothing else is covered."""
    if not isinstance(doc, Mapping):
        raise SpecNotCanonical("platform switches are not a map")
    return jcs({"purpose": SWITCHES_PURPOSE,
                "fields": {name: doc.get(name) for name in OPTIONS}})


#: Each MAC field and the bytes it covers.
_FORMS: tuple[tuple[str, Callable[[Mapping[str, Any]], bytes]], ...] = (
    (MAC_FIELD, canonical_platform_settings),
    (SWITCHES_MAC_FIELD, canonical_switches),
)


def _sig(key: str, canonical: bytes) -> bytes:
    return hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).digest()


def sign(key: str, doc: Mapping[str, Any]) -> dict[str, Any]:
    """Both MAC fields for `doc` under `key`."""
    return {field: {"v": MAC_VERSION, "key": key_id(key),
                    "sig": base64.b64encode(_sig(key, form(doc))).decode("ascii")}
            for field, form in _FORMS}


def _one_verifies(doc: Mapping[str, Any], field: str,
                  form: Callable[[Mapping[str, Any]], bytes], keys: tuple[str, ...]) -> bool:
    mac = doc.get(field)
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION:
        return False
    sig = mac.get("sig")
    if not isinstance(sig, str):
        return False
    try:
        presented = base64.b64decode(sig, validate=True)
        canonical = form(doc)
        return any(hmac.compare_digest(_sig(k, canonical), presented) for k in keys)
    except (binascii.Error, ValueError, SpecNotCanonical):
        return False


def verifies(doc: Mapping[str, Any] | None, keys: tuple[str, ...]) -> bool:
    """`doc` carries both MACs, each made by one of `keys` over exactly its
    switches."""
    if not isinstance(doc, Mapping) or not keys:
        return False
    return all(_one_verifies(doc, field, form, keys) for field, form in _FORMS)


def _valid(name: str, value: Any) -> str | None:
    return value if isinstance(value, str) and value in OPTIONS[name] else None


def _version(doc: Mapping[str, Any] | None) -> int:
    value = (doc or {}).get(VERSION)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def terraform_value(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The switch's Terraform value: the environment as each reader reads it."""
    if name == WORK_CONTRACTS:
        from .workcontract import environment_enabled

        return "on" if environment_enabled(environ) else "off"
    if name == PLUGINS:
        from .plugins import environment_enabled as plugins_enabled

        return "on" if plugins_enabled(environ) else "off"
    from .contractreview import environment_mode

    return environment_mode(environ)


class PlatformSwitches:
    """`platform_settings/switches`: the cached, verified read and the signed,
    versioned, audited write."""

    def __init__(self, db: Any, *, mac: SettingsMac | None = None,
                 now: Callable[[], datetime] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._db = db
        self._mac = mac if mac is not None else SettingsMac()
        self._now = now or (lambda: datetime.now().astimezone())
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, str | None]] | None = None
        self._warned = False

    def _ref(self) -> Any:
        return self._db.collection(COLLECTION).document(DOCUMENT)

    def document(self) -> dict[str, Any] | None:
        """The document as stored, or None when there is none."""
        if self._db is None:
            return None
        snap = self._ref().get()
        if not getattr(snap, "exists", False):
            return None
        data = snap.to_dict()
        return dict(data) if isinstance(data, Mapping) else None

    def trusted(self, doc: Mapping[str, Any] | None) -> dict[str, str | None]:
        """Each switch's stored value a reader may act on: None for one unset
        or invalid, and for all of them when the document does not verify."""
        if doc is None:
            return {name: None for name in OPTIONS}
        if not verifies(doc, self._mac.keys):
            if not self._warned:
                self._warned = True
                logger.warning("%s: platform_settings/switches does not verify; "
                               "every switch reads as its Terraform value", UNSIGNED_LOG)
            return {name: None for name in OPTIONS}
        return {name: _valid(name, doc.get(name)) for name in OPTIONS}

    def stored(self, name: str) -> str | None:
        """The switch's verified stored value, read at most once per
        CACHE_SECONDS; None means the Terraform value applies."""
        now = self._clock()
        with self._lock:
            cached = self._cached
            if cached is not None and now - cached[0] < CACHE_SECONDS:
                return cached[1].get(name)
        try:
            values = self.trusted(self.document())
        except Exception:  # noqa: BLE001 - a store outage must never decide a switch
            logger.exception("platform_switch_read_failed: every switch reads as its Terraform value")
            values = {n: None for n in OPTIONS}
        with self._lock:
            self._cached = (now, values)
        return values.get(name)

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def view(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        """What GET and PUT answer, read fresh (never from the cache)."""
        doc = self.document()
        values = self.trusted(doc)
        switches = {}
        for name, options in OPTIONS.items():
            default = terraform_value(name, environ)
            stored = values[name]
            switches[name] = {
                "value": stored if stored is not None else default,
                "stored": stored,
                "terraform_default": default,
                "source": "setting" if stored is not None else "terraform",
                "options": list(options),
            }
        set_at = (doc or {}).get(SET_AT)
        set_by = (doc or {}).get(SET_BY)
        return {
            "switches": switches,
            "version": _version(doc),
            "signed": None if doc is None else verifies(doc, self._mac.keys),
            "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
            "set_by": set_by if isinstance(set_by, str) else None,
        }

    def set(self, changes: Mapping[str, str | None], *, version: int, by: str,
            environ: Mapping[str, str] | None = None) -> None:
        """Store `changes` (switch -> value, None resetting it to the Terraform
        default) over the document at `version`, signed, bumping the version,
        with one audit row per switch whose stored value changes, all in one
        transaction. The same values again write nothing.

        A stored value that does not verify is never carried over: the write
        starts from every switch at its default, so a planted value is not
        signed with the one the admin chose."""
        for name, value in changes.items():
            if name not in OPTIONS or (value is not None and _valid(name, value) is None):
                raise ValueError(f"{name} is not a platform switch value")
        keys = self._mac.keys
        if not keys:
            raise Conflict("this deployment has no settings key, so no platform switch can be stored",
                           detail={"reason": UNSIGNED_CODE})
        ref = self._ref()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else None
            current = _version(whole)
            if version != current:
                raise Conflict("the platform switches changed since they were read",
                               detail={"reason": CONFLICT_CODE, "version": current})
            before = self.trusted(whole)
            after = {**before, **changes}
            signed = whole is not None and verifies(whole, keys[:1])
            if after == before and (signed or whole is None):
                return
            doc: dict[str, Any] = {name: after[name] for name in OPTIONS}
            doc.update({VERSION: current + 1, SET_AT: now, SET_BY: by})
            doc.update(sign(keys[0], doc))
            txn.set(ref, doc)
            for name in OPTIONS:
                if after[name] == before[name]:
                    continue
                default = terraform_value(name, environ)
                txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
                    "action": AUDIT_ACTION,
                    "by": by,
                    "at": now,
                    "target": f"{COLLECTION}/{DOCUMENT}",
                    "detail": {
                        "switch": name,
                        "from": before[name],
                        "to": after[name],
                        "from_value": before[name] if before[name] is not None else default,
                        "to_value": after[name] if after[name] is not None else default,
                    },
                })

        _write(transaction)
        self.invalidate()


# -- this instance's reader ------------------------------------------------------

_reader: PlatformSwitches | None = None


def install(reader: PlatformSwitches) -> None:
    """Make `reader` the one `stored` asks: create_app installs its own."""
    global _reader
    _reader = reader


def uninstall() -> None:
    global _reader
    _reader = None


def stored(name: str) -> str | None:
    """The switch's stored, verified value on this instance, or None (the
    Terraform value applies) when none is, or no reader is installed."""
    reader = _reader
    return reader.stored(name) if reader is not None else None


def invalidate() -> None:
    """Clear this instance's cache: a set through it is read at once."""
    reader = _reader
    if reader is not None:
        reader.invalidate()
