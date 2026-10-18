"""Where a live-browser pod's Jev judgments go (JUDGE-SELECT).

Owner decisions 2026-10-14. A workspace chooses `internal-jev` (the default),
`internal-laya` or `typesafe` (swarm-api `swarm_api.judgment`, which owns the
setting, its audit and the console's view of it). The dispatcher reads the
choice at dispatch -- `workspace_settings/{tenant}.judgment_backend` -- and
turns it into what the pod carries:

  * SWARM_JEV_BACKEND, always, on a live-browser pod: the choice itself, so
    the jev wrapper can refuse an internal backend it has no URL for instead
    of falling to jev-browser's TypeSafe default;
  * JEV_API_URL, for an internal backend with the tailnet egress on
    (`internal_decision_api_url`): the decision API's `/jev/v1/decide` or
    `/laya/v1/decide` BY ITS TAILNET NAME, over https;
  * the pod label `swarm-judge: internal`, with that URL and only then: what
    JUDGE-NET's NetworkPolicy keys the decision API's egress on, so a pod
    that does not judge internally cannot reach it;
  * with the label and only with it, a `hostAliases` entry pinning the
    tailnet name to the egress Service's fixed clusterIP. The cluster's DNS
    does not know the tailnet name; the Service passes raw TLS through to the
    tailnet, so the pod verifies the API's real certificate, issued for that
    name (owner decision 2026-10-15, #1380). The in-cluster name it replaced
    had no port 80 for http and the wrong certificate for https.

TypeSafe gets none of the three: jev-browser's own default URL
and whatever TypeSafe key the runner provides (JEV-RUNNER). Every value is
the platform's -- the scheduler's settings and a document only a workspace
owner or an admin writes through swarm-api -- never a caller's (invariant 10).

A read that fails, or a stored value this code does not know, is the
DEFAULT, internal-jev: it is the choice that sends nothing outside Saga.

The names below restate swarm-api's; tests/unit/control_plane/
test_dispatch_judgment.py holds them equal.

A VALUE COUNTS ONLY WHEN SWARM-API SIGNED IT, once the switch is on
(docs/design/tenant-settings-integrity.md §4.5, interim A; #1457). Every
tenant's worker can write `workspace_settings/<any tenant>` directly today, so
a stored `judgment_backend` proves nothing about who chose it. swarm-api MACs
each block it writes, `mac: {v: 1, key: <key version>, sig: <base64>}` beside
the fields it covers, over `specsign.canonical_settings_block` under the
`swarm-settings-key` secret that only swarm-api and this service hold.
`trusted_settings` drops every covered field of a block whose MAC is missing
or wrong, so a forged choice reads exactly as no choice: the default. Behind
`SchedulerSettings.settings_mac_required` (SETTINGS_MAC_REQUIRED), OFF until
lane ISO-SETTINGS-MAC's re-sign job has signed the documents that exist, since
on an unsigned document it reads every workspace's choice as the default.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping
from typing import Any

from swarm_common.specsign import SETTINGS_MAC_FIELDS, SpecNotCanonical, canonical_settings_block

log = logging.getLogger(__name__)

INTERNAL_JEV = "internal-jev"
INTERNAL_LAYA = "internal-laya"
TYPESAFE_BACKEND = "typesafe"
BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA, TYPESAFE_BACKEND)
DEFAULT_BACKEND = INTERNAL_JEV

SETTINGS_COLLECTION = "workspace_settings"
SETTING_FIELD = "judgment_backend"

#: Saga's decision API's tailnet name: what its certificate is issued for, and
#: what the egress forwarder dials (terraform/infra/tailscale.tf
#: `tailscale_egress_target`, whose default a test holds equal to this).
DECISION_API_TAILNET_NAME = "decision-api-staging.taile0bda9.ts.net"
#: The decision API's origin as a judging pod calls it: the tailnet name over
#: https, resolved by the pod's `hostAliases` entry to the egress Service.
#: The only value INTERNAL_DECISION_API_URL may hold besides "" (off).
INTERNAL_DECISION_ORIGIN = f"https://{DECISION_API_TAILNET_NAME}"
#: The egress Service `decision-api`'s clusterIP. FIXED, because a judging
#: pod pins the tailnet name to it in /etc/hosts: a Service recreated with a
#: new address would leave every such pod dialling nothing. Stated once, as
#: terraform/infra/tailscale.tf `local.decision_api_cluster_ip`, which renders
#: it into the manifest; tests/unit/control_plane/test_dispatch_judgment.py
#: holds this copy equal to it.
DECISION_API_CLUSTER_IP = "10.48.6.120"
#: The decision API's path per internal backend.
INTERNAL_PATHS = {INTERNAL_JEV: "jev", INTERNAL_LAYA: "laya"}

JEV_BACKEND_ENV = "SWARM_JEV_BACKEND"
JEV_API_URL_ENV = "JEV_API_URL"
#: What JUDGE-NET's NetworkPolicy selects to open the decision API.
JUDGE_POD_LABEL = ("swarm-judge", "internal")


def check_internal_decision_api_url(value: str) -> None:
    """"" (the egress is off) or the decision API's tailnet origin, nothing
    else: the jev wrapper accepts that host alone, and the pod's hostAliases
    entry resolves that name alone, so any other value would light the
    internal backends up in the console and fail in every pod."""
    if value and value.rstrip("/") != INTERNAL_DECISION_ORIGIN:
        raise ValueError(
            f"INTERNAL_DECISION_API_URL must be empty or {INTERNAL_DECISION_ORIGIN!r} "
            f"(the decision API by its tailnet name), not {value!r}"
        )


def jev_dispatch(backend: str, internal_url: str
                 ) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    """(env, pod labels, pod hostAliases) for a live-browser pod whose
    workspace chose `backend`. The URL, the label and the hostAliases entry
    come together or not at all."""
    if backend not in BACKENDS:
        backend = DEFAULT_BACKEND
    env = {JEV_BACKEND_ENV: backend}
    labels: dict[str, str] = {}
    host_aliases: list[dict[str, Any]] = []
    base = (internal_url or "").strip().rstrip("/")
    if backend in INTERNAL_PATHS and base:
        env[JEV_API_URL_ENV] = f"{base}/{INTERNAL_PATHS[backend]}/v1/decide"
        labels[JUDGE_POD_LABEL[0]] = JUDGE_POD_LABEL[1]
        host_aliases.append({"ip": DECISION_API_CLUSTER_IP,
                             "hostnames": [DECISION_API_TAILNET_NAME]})
    return env, labels, host_aliases


#: Where a block's MAC sits: beside the fields it covers, in the block itself
#: (the document for "top"). Its shape, `{v, key, sig}`, is §4.5's.
MAC_FIELD = "mac"
MAC_VERSION = 1
#: The nested blocks this service reads: `code_intelligence`, for its
#: `judgment_backend` (`dispatch.judge_backend`). `merge` is swarm-api's alone,
#: so it is neither verified nor logged here.
NESTED_BLOCKS = ("code_intelligence",)
#: The log event for a block whose MAC is missing or wrong, once per tenant
#: and block per process per hour.
SETTINGS_MAC_REFUSED = "settings_mac_refused"
SETTINGS_MAC_LOG_INTERVAL_S = 3600.0
_mac_refusal_logged: dict[str, float] = {}


def settings_mac(key: str, *, tenant_id: str, block: str, fields: Mapping[str, Any]) -> str:
    """HMAC-SHA256(key, canonical_settings_block(...)), standard base64: the
    `sig` swarm-api stores. The key is the secret's text as UTF-8, as the
    child key is (`children.registration_nonce`)."""
    message = canonical_settings_block(tenant_id=tenant_id, block=block, fields=fields)
    digest = hmac.new(key.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def block_verifies(key: str, *, tenant_id: str, block: str, fields: Any) -> bool:
    """Whether `fields`, one stored block, carries a MAC `key` made for this
    tenant and this block. False for no key, no MAC, another version, a sig
    that is not base64, or one made over other values, another tenant or
    another block. `mac.key` names the key version and is not checked: a sig
    that verifies under the key this service holds was made with it."""
    if not key or not isinstance(fields, Mapping):
        return False
    mac = fields.get(MAC_FIELD)
    if not isinstance(mac, Mapping) or mac.get("v") != MAC_VERSION:
        return False
    sig = mac.get("sig")
    if not isinstance(sig, str):
        return False
    try:
        presented = base64.b64decode(sig, validate=True)
        expected = base64.b64decode(
            settings_mac(key, tenant_id=tenant_id, block=block, fields=fields))
    except (binascii.Error, ValueError, SpecNotCanonical):
        return False
    return hmac.compare_digest(presented, expected)


def _say_refused(tenant_id: str, block: str, now: float) -> None:
    due_key = f"{tenant_id}\n{block}"
    last = _mac_refusal_logged.get(due_key)
    if last is not None and now - last < SETTINGS_MAC_LOG_INTERVAL_S:
        return
    _mac_refusal_logged[due_key] = now
    # Which fields were set, never their values: a forged value is the
    # forger's text.
    log.error(
        "%s tenant=%s block=%s: its MAC is missing or does not verify; its fields "
        "read as unset", SETTINGS_MAC_REFUSED, tenant_id, block,
        extra={"event": SETTINGS_MAC_REFUSED, "tenant_id": tenant_id, "block": block},
    )


def trusted_settings(doc: Any, *, tenant_id: str, key: str | None) -> dict[str, Any]:
    """`doc`, a `workspace_settings` document, with what is not swarm-api's removed.

    `key` None is the switch off: the document as stored, today's reading.
    Otherwise every field SETTINGS_MAC_FIELDS lists for a block whose MAC does
    not verify is dropped -- the top level's from the document, a nested
    block's from that block -- so a reader falls back to its default exactly
    as for a workspace that never chose. A field outside the list is left
    alone: no reader here acts on one.
    """
    doc = dict(doc) if isinstance(doc, Mapping) else {}
    if key is None:
        return doc
    if any(doc.get(name) is not None for name in SETTINGS_MAC_FIELDS["top"]) \
            and not block_verifies(key, tenant_id=tenant_id, block="top", fields=doc):
        _say_refused(tenant_id, "top", time.monotonic())
        for name in SETTINGS_MAC_FIELDS["top"]:
            doc.pop(name, None)
    for block in NESTED_BLOCKS:
        fields = doc.get(block)
        if not isinstance(fields, Mapping):
            continue
        if any(fields.get(name) is not None for name in SETTINGS_MAC_FIELDS[block]) \
                and not block_verifies(key, tenant_id=tenant_id, block=block, fields=fields):
            _say_refused(tenant_id, block, time.monotonic())
            doc[block] = {k: v for k, v in fields.items() if k not in SETTINGS_MAC_FIELDS[block]}
    return doc


def settings_mac_key(settings: Any) -> str | None:
    """The key `trusted_settings` verifies with; None while the switch is off.

    On with no key is refused when the settings are built
    (`SchedulerSettings.__post_init__`); a hand-built object that got here
    anyway verifies with "" and so trusts nothing, which fails closed."""
    if not getattr(settings, "settings_mac_required", False):
        return None
    return str(getattr(settings, "settings_mac_key", "") or "")


def read_settings(db: Any, tenant_id: str, *, mac_key: str | None = None) -> dict[str, Any]:
    """The tenant's `workspace_settings` document, through `trusted_settings`.

    Raises what the read raises; each caller decides what unreadable means."""
    snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    return trusted_settings(doc, tenant_id=tenant_id, key=mac_key)


def read_backend(db: Any, tenant_id: str, *, mac_key: str | None = None) -> str:
    """The tenant's stored choice; the default when none, unknown, unreadable
    or, with `mac_key` (`settings_mac_key`), not signed by swarm-api."""
    try:
        doc = read_settings(db, tenant_id, mac_key=mac_key)
    except Exception as exc:  # noqa: BLE001 - a dispatch never fails on this read
        log.warning("judgment backend unreadable for tenant=%s, using %s: %s",
                    tenant_id, DEFAULT_BACKEND, type(exc).__name__)
        return DEFAULT_BACKEND
    value = doc.get(SETTING_FIELD)
    return value if isinstance(value, str) and value in BACKENDS else DEFAULT_BACKEND
