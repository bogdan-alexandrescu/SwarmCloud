"""Registered MCP servers: a tenant's own MCP servers, by name (MCP-API, part of #1433).

Design: docs/design/registered-mcp-servers.md §4.2 (the registration), §4.11
(the API) and the owner's decisions of 2026-10-17 (Q1 admins register for a
group workspace, Q4 empty `allowed_tools` is every tool, Q5 streamable HTTP
only, Q6 public HTTPS only).

One Firestore document per (tenant, server), `mcp_servers/{server_id}`, kept
out of `store.py` and `codec.py` for the reason `repositories.py` gives: the
shape is not the frozen contract's. `server_id` is `mcp_` and the first 16 hex
of `sha256(tenant_id + "\\0" + name)`, so the id is stable, unique per tenant
and name, and carries no name.

WHAT A REGISTRATION NEVER HOLDS: a credential, a secret's name, or a header
value. A `dispatch` server's key travels beside each submission
(`mcp_credentials.py`); a `workspace_secret` server's is found through the
grant `mcp:<name>` of the workspace secrets store (§4.5).

SWARM-API NEVER CALLS A REGISTERED URL (§4.2): not at registration and not to
"verify" one. swarm-api reaches every tenant, and a URL a tenant typed is
exactly what an SSRF needs. The first request a server sees is a worker's,
through the relay's own address checks.

ONE TENANT NEVER SEES ANOTHER'S. No route takes a tenant: every read and
write acts on `tenant_scope`, the id is derived from it, and the document's
own `tenant_id` is compared again before anything is returned, so another
tenant's server is a 404 whatever its name.

EVERY WRITE IS AUDITED IN ITS OWN TRANSACTION: `mcp_server_registered`,
`_updated`, `_disabled`, `_enabled`, in `admin_audit`, with `{tenant_id,
server_id, name, url, auth_mode, revision}`.

THE FEATURE SHIPS DARK. `mcp_servers_enabled()` reads `SWARM_MCP_SERVERS`
(off unless it is `on`); while it is off a registration write and an attach
are refused `mcp_unavailable`, and MCP-SUBMIT refuses every `mcp_servers`
submission with the same code.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.models import (
    MCP_SERVER_NAME,
    RESERVED_MCP_SERVER_NAMES,
    RESERVED_MCP_SERVER_PREFIX,
    consumer_name,
)
from swarm_common.profiles import RUNNER_PROFILES

from .admins import AUDIT_COLLECTION
from .errors import Conflict, NotFound, UpstreamUnavailable, ValidationFailed
from .redaction import redact

log = logging.getLogger(__name__)

#: One document per (tenant, server). Never deleted: disabling keeps its history.
COLLECTION = "mcp_servers"

#: The environment switch the feature ships behind (design §6). Anything but
#: `on` is off, so an unset, misspelt or empty value keeps the feature dark.
FLAG = "SWARM_MCP_SERVERS"

AUDIT_REGISTERED = "mcp_server_registered"
AUDIT_UPDATED = "mcp_server_updated"
AUDIT_DISABLED = "mcp_server_disabled"
AUDIT_ENABLED = "mcp_server_enabled"

#: Q5: the one transport.
TRANSPORT = "streamable-http"

MODE_NONE = "none"
MODE_DISPATCH = "dispatch"
MODE_WORKSPACE = "workspace_secret"
MODES = (MODE_NONE, MODE_DISPATCH, MODE_WORKSPACE)

DEFAULT_HEADER = "Authorization"
SCHEMES = ("Bearer", "")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
#: Headers the relay owns or the transport needs: a registration that put its
#: credential in one of them would break the request or smuggle a second one.
FORBIDDEN_HEADERS = frozenset(h.lower() for h in (
    "Host", "Cookie", "Content-Length", "Content-Type", "Accept", "Transfer-Encoding",
    "Connection", "Mcp-Session-Id", "MCP-Protocol-Version", "Proxy-Authorization",
))

TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
MAX_TOOLS = 64
MAX_URL = 512
MAX_DESCRIPTION = 280

#: (default, ceiling) per limit (§4.2). The defaults suit a knowledge-base
#: server answering search results; the ceilings bound what one relay may hold
#: in memory and how long one call may keep an attempt waiting.
LIMITS: dict[str, tuple[int, int]] = {
    "max_request_bytes": (1024 * 1024, 8 * 1024 * 1024),
    "max_response_bytes": (4 * 1024 * 1024, 32 * 1024 * 1024),
    "calls_per_minute": (60, 600),
    "call_timeout_seconds": (60, 300),
}

#: Every profile that declares the `mcp_servers` input (contract request 88).
#: Read from the frozen catalogue, so a profile that gains it is offered here.
MCP_PROFILES: tuple[str, ...] = tuple(sorted(
    name for name, profile in RUNNER_PROFILES.items()
    if "mcp_servers" in (getattr(profile, "inputs", None) or {})
))

#: Host suffixes that never name a public server (Q6): loopback, link-local
#: and mDNS names, cluster and metadata names, and the special-use domains.
_RESERVED_HOST_SUFFIXES = (
    "localhost", "local", "localdomain", "internal", "intranet", "lan", "home", "corp",
    "private", "arpa", "test", "invalid", "onion", "svc", "cluster.local",
)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")


class McpUnavailable(UpstreamUnavailable):
    code = "mcp_unavailable"


class McpServerInvalid(ValidationFailed):
    code = "mcp_server_invalid"


class McpServerNotFound(NotFound):
    code = "mcp_server_not_found"


class McpServerExists(Conflict):
    code = "mcp_server_exists"


def mcp_servers_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Is the feature on? Off unless SWARM_MCP_SERVERS is exactly `on`.

    Read from the environment on each call, as `workspaces.WORKSPACE_GATE` is,
    so a test sets it per case and a deployment flips it without a code
    change. `settings.py` is not this lane's file (design §6)."""
    raw = (os.environ if environ is None else environ).get(FLAG, "")
    return raw.strip().lower() == "on"


def require_enabled(environ: Mapping[str, str] | None = None) -> None:
    if not mcp_servers_enabled(environ):
        raise McpUnavailable(
            "registered MCP servers are not enabled in this deployment "
            f"({FLAG} is off); nothing was written")


def server_id(tenant_id: str, name: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{name}".encode("utf-8")).hexdigest()
    return f"mcp_{digest[:16]}"


# ---------------------------------------------------------------------------
# The shape checks: each refusal names the rule, never echoes the value
# ---------------------------------------------------------------------------


def check_server_name(name: str) -> str:
    """Contract request 90's spelling, minus the names the worker registers itself."""
    text = name if isinstance(name, str) else ""
    if not MCP_SERVER_NAME.fullmatch(text):
        raise McpServerInvalid(
            "an MCP server's name is 1-31 characters of a-z, 0-9 and '-', starting "
            "with a letter")
    if text in RESERVED_MCP_SERVER_NAMES or text.startswith(RESERVED_MCP_SERVER_PREFIX):
        raise McpServerInvalid(
            "that name is reserved: the worker registers MCP servers named "
            f"{', '.join(RESERVED_MCP_SERVER_NAMES)} and {RESERVED_MCP_SERVER_PREFIX}* itself")
    return text


def _credential_shaped(text: str) -> bool:
    return bool(text) and redact(text).text != text


def _public_dns_host(host: str) -> bool:
    if not host or len(host) > 253 or host.endswith("."):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    if not _TLD.fullmatch(labels[-1]):
        # An all-numeric or hex last label is an address in disguise
        # (`127.1`, `0x7f.1`), which some resolvers still honour.
        return False
    return not any(host == suffix or host.endswith("." + suffix)
                   for suffix in _RESERVED_HOST_SUFFIXES)


def normalise_url(url: str) -> str:
    """`https://<public dns name>[:443]/<path>`, lower-cased host, no default port.

    Refused: any scheme but https; userinfo; a query or a fragment (a place a
    credential hides); an IP literal in any spelling; a single-label or
    reserved host (Q6: public HTTPS only); any port but 443; whitespace or a
    control character; anything credential-shaped. A refusal never repeats
    the URL, which a person can paste a token into."""
    text = url if isinstance(url, str) else ""
    if not 1 <= len(text) <= MAX_URL or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                                            for c in text):
        raise McpServerInvalid(f"the URL is 1-{MAX_URL} characters with no whitespace")
    if not text.isascii():
        raise McpServerInvalid("the URL is ASCII; spell an international host in punycode")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise McpServerInvalid("the URL does not parse") from None
    if parts.scheme.lower() != "https":
        raise McpServerInvalid("an MCP server's URL is https:// (Q6: public HTTPS only)")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise McpServerInvalid(
            "the URL carries userinfo; register the credential as the server's auth, "
            "never in its URL")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise McpServerInvalid("the URL carries a query or a fragment; register a path only")
    if port not in (None, 443):
        raise McpServerInvalid("an MCP server is reached on port 443 only (Q6)")
    host = (parts.hostname or "").lower()
    if not _public_dns_host(host):
        raise McpServerInvalid(
            "the URL's host must be a public DNS name: not an IP address, not a "
            "single label, not localhost or a reserved or internal domain (Q6)")
    if _credential_shaped(text):
        raise McpServerInvalid("the URL holds something shaped like a credential")
    return f"https://{host}{parts.path}"


def check_description(text: str | None) -> str:
    value = (text or "").strip()
    if len(value) > MAX_DESCRIPTION or any(ord(c) < 32 for c in value):
        raise McpServerInvalid(
            f"the description is 0-{MAX_DESCRIPTION} characters on one line")
    if _credential_shaped(value):
        raise McpServerInvalid("the description holds something shaped like a credential")
    return value


def check_auth(auth: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(auth or {})
    mode = data.get("mode", MODE_NONE)
    if mode not in MODES:
        raise McpServerInvalid(f"auth.mode is one of {', '.join(MODES)}")
    header = data.get("header") or DEFAULT_HEADER
    if not isinstance(header, str) or not HEADER_NAME.fullmatch(header) \
            or header.lower() in FORBIDDEN_HEADERS:
        raise McpServerInvalid(
            "auth.header is 1-64 characters of A-Z, a-z, 0-9 and '-', and not one the "
            "transport or the relay owns (Host, Cookie, Content-*, Accept, Connection, "
            "Mcp-Session-Id, MCP-Protocol-Version, Proxy-Authorization, ...)")
    scheme = data.get("scheme", "Bearer")
    if scheme is None:
        scheme = "Bearer"
    if scheme not in SCHEMES:
        raise McpServerInvalid("auth.scheme is `Bearer` or empty (the value is the whole header)")
    if mode == MODE_NONE:
        return {"mode": mode, "header": None, "scheme": None}
    return {"mode": mode, "header": header, "scheme": scheme}


def check_profiles(profiles: list[str] | None) -> list[str]:
    wanted = list(dict.fromkeys(profiles or []))
    unknown = [p for p in wanted if p not in MCP_PROFILES]
    if unknown:
        raise McpServerInvalid(
            "allowed_profiles names only profiles that take MCP servers",
            detail={"profiles": list(MCP_PROFILES)})
    return sorted(wanted)


def check_tools(tools: list[str] | None) -> list[str]:
    wanted = list(dict.fromkeys(tools or []))
    if len(wanted) > MAX_TOOLS:
        raise McpServerInvalid(f"allowed_tools lists at most {MAX_TOOLS} tools")
    if not all(isinstance(t, str) and TOOL_NAME.fullmatch(t) for t in wanted):
        raise McpServerInvalid(
            "each allowed tool is 1-64 characters of A-Z, a-z, 0-9, '_', '.' and '-'")
    return sorted(wanted)


def check_limits(limits: Mapping[str, Any] | None,
                 current: Mapping[str, Any] | None = None) -> dict[str, int]:
    given = dict(limits or {})
    unknown = sorted(set(given) - set(LIMITS))
    if unknown:
        raise McpServerInvalid("unknown limit", detail={"limits": sorted(LIMITS)})
    out: dict[str, int] = {}
    for key, (default, ceiling) in LIMITS.items():
        value = given.get(key)
        if value is None:
            value = (current or {}).get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise McpServerInvalid(f"limits.{key} is an integer from 1 to {ceiling}")
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def server_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A registration as the API answers it. There is no credential to leave
    out: a registration holds none. `consumer` is the grant name a
    `workspace_secret` server's key is granted to (§4.5)."""
    name = str(doc.get("name"))
    return {
        "server_id": doc.get("server_id"), "tenant_id": doc.get("tenant_id"),
        "name": name, "url": doc.get("url"), "transport": doc.get("transport"),
        "description": doc.get("description") or "",
        "auth": dict(doc.get("auth") or {}),
        "allowed_profiles": list(doc.get("allowed_profiles") or []),
        "allowed_tools": list(doc.get("allowed_tools") or []),
        "limits": dict(doc.get("limits") or {}),
        "enabled": doc.get("enabled") is True,
        "revision": doc.get("revision"),
        "consumer": consumer_name("mcp", name),
        "created_at": _iso(doc.get("created_at")), "created_by": doc.get("created_by"),
        "updated_at": _iso(doc.get("updated_at")), "updated_by": doc.get("updated_by"),
    }


def read_registration(db: Any, tenant_id: str, name: str) -> dict[str, Any] | None:
    """The registration of `name` in `tenant_id`, or None. Bounded by the
    tenant twice: the id is derived from it and the document must carry it.

    For the attach (§4.6), MCP-SUBMIT and the store's `mcp:` grants: a name
    that is not a registrable spelling is never looked up."""
    if not isinstance(name, str) or not MCP_SERVER_NAME.fullmatch(name):
        return None
    snap = db.collection(COLLECTION).document(server_id(tenant_id, name)).get()
    doc = (snap.to_dict() or {}) if snap.exists else {}
    if doc.get("tenant_id") != tenant_id or doc.get("name") != name:
        return None
    return doc


def profile_allowed(doc: Mapping[str, Any], profile: str) -> bool:
    """Empty `allowed_profiles` is every profile that takes MCP servers."""
    allowed = list(doc.get("allowed_profiles") or [])
    return profile in MCP_PROFILES and (not allowed or profile in allowed)


def enabled_names(db: Any, tenant_id: str) -> list[str]:
    """The names of `tenant_id`'s enabled registrations, sorted."""
    query = db.collection(COLLECTION).where(filter=FieldFilter("tenant_id", "==", tenant_id))
    return sorted({str(d.get("name")) for d in (s.to_dict() or {} for s in query.stream())
                   if d.get("tenant_id") == tenant_id and d.get("enabled") is True
                   and isinstance(d.get("name"), str)
                   and MCP_SERVER_NAME.fullmatch(d["name"])})


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class McpServers:
    """`mcp_servers/`, every write with its `admin_audit` row in the same
    Firestore transaction. Who may write is the route's (`require_manager`)."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        query = self._db.collection(COLLECTION).where(filter=FieldFilter("tenant_id", "==", tenant_id))
        docs = [d for d in (s.to_dict() or {} for s in query.stream())
                if d.get("tenant_id") == tenant_id]
        return [server_api(d) for d in sorted(docs, key=lambda d: str(d.get("name")))]

    def get(self, tenant_id: str, name: str) -> dict[str, Any]:
        doc = read_registration(self._db, tenant_id, name)
        if doc is None:
            raise McpServerNotFound("no MCP server by that name in this workspace")
        return server_api(doc)

    def _audit(self, txn: Any, action: str, *, by: str, at: datetime,
               doc: Mapping[str, Any]) -> None:
        txn.set(self._db.collection(AUDIT_COLLECTION).document(), {
            "action": action, "target_server_id": doc.get("server_id"), "by": by, "at": at,
            "detail": {
                "tenant_id": doc.get("tenant_id"), "server_id": doc.get("server_id"),
                "name": doc.get("name"), "url": doc.get("url"),
                "auth_mode": (doc.get("auth") or {}).get("mode"),
                "revision": doc.get("revision"),
            },
        })

    def register(self, *, tenant_id: str, name: str, url: str, description: str | None,
                 auth: Mapping[str, Any] | None, allowed_profiles: list[str] | None,
                 allowed_tools: list[str] | None, limits: Mapping[str, Any] | None,
                 enabled: bool, by: str) -> dict[str, Any]:
        name = check_server_name(name)
        doc_id = server_id(tenant_id, name)
        now = self._now()
        doc = {
            "server_id": doc_id, "tenant_id": tenant_id, "name": name,
            "url": normalise_url(url), "transport": TRANSPORT,
            "description": check_description(description), "auth": check_auth(auth),
            "allowed_profiles": check_profiles(allowed_profiles),
            "allowed_tools": check_tools(allowed_tools), "limits": check_limits(limits),
            "enabled": bool(enabled), "revision": 1,
            "created_at": now, "created_by": by, "updated_at": now, "updated_by": by,
        }
        ref = self._db.collection(COLLECTION).document(doc_id)

        @firestore.transactional
        def _write(txn: Any) -> None:
            if _snapshot(txn.get(ref)).exists:
                raise McpServerExists(
                    "an MCP server by that name is already registered in this workspace; "
                    "change it with PATCH")
            txn.set(ref, doc)
            self._audit(txn, AUDIT_REGISTERED, by=by, at=now, doc=doc)

        _write(self._db.transaction())
        log.info("mcp server registered tenant=%s server=%s name=%s auth=%s",
                 tenant_id, doc_id, name, doc["auth"]["mode"])
        return server_api(doc)

    def update(self, *, tenant_id: str, name: str, changes: Mapping[str, Any],
               by: str) -> dict[str, Any]:
        """Apply the fields `changes` names; `revision` + 1. One audit row:
        `_disabled` or `_enabled` when `enabled` flips, else `_updated`."""
        name = check_server_name(name)
        checked: dict[str, Any] = {}
        if "url" in changes:
            checked["url"] = normalise_url(changes["url"])
        if "description" in changes:
            checked["description"] = check_description(changes["description"])
        if "auth" in changes:
            checked["auth"] = check_auth(changes["auth"])
        if "allowed_profiles" in changes:
            checked["allowed_profiles"] = check_profiles(changes["allowed_profiles"])
        if "allowed_tools" in changes:
            checked["allowed_tools"] = check_tools(changes["allowed_tools"])
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise McpServerInvalid("enabled is true or false")
            checked["enabled"] = changes["enabled"]
        ref = self._db.collection(COLLECTION).document(server_id(tenant_id, name))
        now = self._now()

        @firestore.transactional
        def _write(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else {}
            if doc.get("tenant_id") != tenant_id or doc.get("name") != name:
                raise McpServerNotFound("no MCP server by that name in this workspace")
            if "limits" in changes:
                checked["limits"] = check_limits(changes["limits"], doc.get("limits"))
            before = doc.get("enabled") is True
            doc.update(checked)
            doc["revision"] = int(doc.get("revision") or 0) + 1
            doc["updated_at"], doc["updated_by"] = now, by
            txn.set(ref, doc)
            after = doc.get("enabled") is True
            action = (AUDIT_UPDATED if before == after
                      else AUDIT_ENABLED if after else AUDIT_DISABLED)
            self._audit(txn, action, by=by, at=now, doc=doc)
            return doc

        doc = _write(self._db.transaction())
        log.info("mcp server updated tenant=%s server=%s name=%s revision=%s fields=%s",
                 tenant_id, doc.get("server_id"), name, doc.get("revision"), sorted(checked))
        return server_api(doc)
