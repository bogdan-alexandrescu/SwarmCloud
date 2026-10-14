"""The vendor credential and the sticky session (docs/browser-egress.md §2.4, §3.1, §7.1).

THE CREDENTIAL lives in ONE platform secret, `swarm-egress-credential`, whose only
reader is this service's Workload Identity principal (terraform/infra/egress.tf).
Its value is JSON, added by the owner on stdin and never written anywhere
else (§7.1). It is one JSON object with five fields:

  * `vendor` -- `brightdata`;
  * `host`, `port` -- the vendor's proxy endpoint;
  * `tls` -- true when that endpoint is TLS-wrapped (§3.2);
  * `zones` -- a map from a zone name (`residential`, `isp`) to an object with
    that zone's `username`, its secret, under the field name the vendor's
    dashboard uses for it, and `sticky` (below). `_zone` names the fields.

ZONES are the owner's phase-1 trial (§7.1 Q2: trial both ISP and residential).
Each is a Bright Data zone with its own username and password; the platform
configuration picks the zone per tenant (`config.EgressConfig.zone_for`).
`sticky` says what one session id stands for:

  * `attempt` -- rotating residential: one peer for the attempt, which covers a
    sign-in and the agent's use of it afterwards (§2.4);
  * `task` -- ISP: one address for the task across its attempts, so a resumed
    attempt after a park returns from the address the cookies were issued to
    (§2.4, "Across a park").

THE USERNAME is rendered here from one template per vendor, with the exit
country and the session id appended; it is never built in the pod. Bright
Data's form is `<zone username>-country-<cc>-session-<id>`.

Nothing in this module has a repr, a str or an exception message that carries
a username or a password.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

#: One template per vendor. Adding a vendor is a line here and a test; the
#: owner chose Bright Data (§7.1 Q1).
USERNAME_TEMPLATES = {
    "brightdata": "{username}-country-{country}-session-{session}",
}
STICKY = ("attempt", "task")
#: What a session id is derived under, so it is never the bare hash of an id
#: the platform also uses elsewhere.
SESSION_DOMAIN = b"swarm-egress-session/v1\0"
SESSION_CHARS = 24
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_ZONE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_COUNTRY = re.compile(r"[a-z]{2}")
_SESSION = re.compile(r"[a-z0-9]{1,32}")


class CredentialInvalid(Exception):
    """The secret's value cannot be used. The message names the problem, never
    the value."""


@dataclass(frozen=True)
class Zone:
    username: str = field(repr=False)
    password: str = field(repr=False)
    sticky: str = "attempt"

    def __str__(self) -> str:
        return f"Zone(sticky={self.sticky})"


@dataclass(frozen=True)
class VendorCredential:
    vendor: str
    host: str
    port: int
    tls: bool
    zones: Mapping[str, Zone] = field(repr=False)

    def __repr__(self) -> str:
        return (f"VendorCredential(vendor={self.vendor!r}, zones={sorted(self.zones)!r}, "
                f"tls={self.tls!r})")

    __str__ = __repr__

    def username(self, zone: str, *, country: str, session: str) -> str:
        if not _COUNTRY.fullmatch(country) or not _SESSION.fullmatch(session):
            raise CredentialInvalid("the exit country or the session id is malformed")
        return USERNAME_TEMPLATES[self.vendor].format(
            username=self.zones[zone].username, country=country, session=session)

    def proxy_authorization(self, zone: str, *, country: str, session: str) -> str:
        """The `Proxy-Authorization` value for the vendor. Callers put it on
        the wire and nowhere else."""
        user = self.username(zone, country=country, session=session)
        raw = f"{user}:{self.zones[zone].password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


def session_id(tenant_id: str, task_id: str, attempt_id: str, *, sticky: str) -> str:
    """Opaque to the vendor (no tenant or task name leaves the platform), stable
    for what `sticky` names, and never shared by two tenants: the tenant is
    always part of the hash."""
    if sticky not in STICKY:
        raise ValueError(f"sticky is one of {STICKY}")
    parts = [tenant_id, task_id] + ([attempt_id] if sticky == "attempt" else [])
    digest = hashlib.sha256(SESSION_DOMAIN + "\0".join(parts).encode()).hexdigest()
    return digest[:SESSION_CHARS]


def _zone(name: Any, body: Any) -> Zone:
    if not isinstance(name, str) or not _ZONE.fullmatch(name):
        raise CredentialInvalid("a zone name is not [a-z][a-z0-9_-]")
    if not isinstance(body, dict) or set(body) - {"username", "password", "sticky"}:
        raise CredentialInvalid(f"zone {name} has fields other than username, password, sticky")
    username, password = body.get("username"), body.get("password")
    sticky = body.get("sticky", "attempt")
    if not isinstance(username, str) or not username or len(username) > 200 or ":" in username:
        raise CredentialInvalid(f"zone {name} has no usable username")
    if not isinstance(password, str) or not password or len(password) > 200:
        raise CredentialInvalid(f"zone {name} has no usable password")
    if sticky not in STICKY:
        raise CredentialInvalid(f"zone {name}'s sticky is not one of {STICKY}")
    return Zone(username=username, password=password, sticky=sticky)


def parse_credential(raw: bytes) -> VendorCredential:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CredentialInvalid("swarm-egress-credential is not a JSON object") from None
    if not isinstance(body, dict):
        raise CredentialInvalid("swarm-egress-credential is not a JSON object")
    extra = set(body) - {"vendor", "host", "port", "tls", "zones"}
    if extra:
        raise CredentialInvalid(f"swarm-egress-credential has unknown fields {sorted(extra)}")
    vendor = body.get("vendor")
    if vendor not in USERNAME_TEMPLATES:
        raise CredentialInvalid(f"the vendor is not one of {sorted(USERNAME_TEMPLATES)}")
    host = body.get("host")
    if not isinstance(host, str) or not _HOST.fullmatch(host.lower()):
        raise CredentialInvalid("the vendor host is not a hostname")
    port = body.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise CredentialInvalid("the vendor port is not a port")
    tls = body.get("tls", False)
    if not isinstance(tls, bool):
        raise CredentialInvalid("tls is true or false")
    zones = body.get("zones")
    if not isinstance(zones, dict) or not zones:
        raise CredentialInvalid("swarm-egress-credential names no zone")
    return VendorCredential(vendor=vendor, host=host.lower(), port=port, tls=tls,
                            zones={name: _zone(name, z) for name, z in zones.items()})


__all__ = ["CredentialInvalid", "USERNAME_TEMPLATES", "VendorCredential", "Zone",
           "parse_credential", "session_id"]
