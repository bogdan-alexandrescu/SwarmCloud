"""The platform setting swarm-egress obeys (docs/browser-egress.md §3.5, §6.1, §7.1).

Rendered by terraform/infra/egress.tf into the `swarm-egress-config`
ConfigMap and mounted as a file, which this service RE-READS every few
seconds (main.CONFIG_POLL_SECONDS). The kubelet refreshes a mounted ConfigMap
in about a minute, so the kill switch is a `terraform apply` -- or, in an
emergency, `kubectl edit configmap swarm-egress-config` -- away, with no
restart and no image.

FAIL CLOSED. A file that is missing, cannot be read, is not JSON, or carries a
key this module does not know is the switch OFF (`cause` says which). An
unknown key is refused rather than ignored so a setting renamed on one side
cannot silently do nothing on the other; tests/unit/egress/test_egress_manifest.py
holds egress.tf's keys equal to CONFIG_KEYS.

OFF stops every new CONNECT with `egress_disabled` and closes the open tunnels
(owner decision Q7, fail closed): a proxied attempt's pages stop loading, and
it never silently changes IP under a signed-in session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

CONFIG_KEYS = (
    "enabled", "country", "tenants", "default_zone", "tenant_zones", "monthly_cap_bytes",
    "tenant_monthly_cap_bytes", "attempt_cap_bytes", "connects_per_minute",
    "max_tunnels_per_attempt", "bypass_hosts",
)
_COUNTRY = re.compile(r"[a-z]{2}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_HOST = re.compile(r"(?:\*?\.)?[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


class ConfigInvalid(Exception):
    pass


@dataclass(frozen=True)
class EgressConfig:
    #: The platform flag (§3.5 layer 1). Off unless the file says otherwise.
    enabled: bool = False
    #: Why the switch is off, for the log: `disabled` (the setting says so),
    #: `config_unreadable` or `config_invalid`.
    cause: str = "disabled"
    #: The exit country for the whole platform (owner decision Q6: US).
    country: str = "us"
    #: Tenants switched on (§6.1). swarm-api mints only for these; swarm-egress
    #: refuses the rest too, so removing a tenant here stops its running
    #: attempts' proxy as well as its next ones.
    tenants: frozenset[str] = frozenset()
    default_zone: str = "residential"
    tenant_zones: Mapping[str, str] = field(default_factory=dict)
    #: The platform's monthly cap, in bytes, from the owner's $150 (Q3) and a
    #: conservative per-GB price (egress.tf says which). The vendor sub-user's
    #: own traffic limit is the hard stop; this is the one that answers first.
    monthly_cap_bytes: int = 0
    tenant_monthly_cap_bytes: int = 0
    #: Per attempt (§3.5: 1 GB, ten times the planning figure).
    attempt_cap_bytes: int = 0
    connects_per_minute: int = 600
    max_tunnels_per_attempt: int = 128
    bypass_hosts: tuple[str, ...] = ()
    #: HTTPS only (§2.3, §3.2). Not a setting: nothing here should ever carry
    #: plain http through a residential peer.
    allowed_ports: frozenset[int] = frozenset({443})

    def zone_for(self, tenant_id: str) -> str:
        return self.tenant_zones.get(tenant_id, self.default_zone)


def _positive(body: Mapping[str, Any], name: str, default: int) -> int:
    value = body.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigInvalid(f"{name} is a positive integer")
    return value


def _names(value: Any, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigInvalid(f"{what} is a list of strings")
    return value


def parse_config(body: Any) -> EgressConfig:
    if not isinstance(body, dict):
        raise ConfigInvalid("the configuration is a JSON object")
    unknown = set(body) - set(CONFIG_KEYS)
    if unknown:
        raise ConfigInvalid(f"unknown keys {sorted(unknown)}")
    enabled = body.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigInvalid("enabled is true or false")
    country = body.get("country", "us")
    if not isinstance(country, str) or not _COUNTRY.fullmatch(country):
        raise ConfigInvalid("country is a two-letter lower-case code")
    tenants = _names(body.get("tenants", []), "tenants")
    zones = body.get("tenant_zones", {})
    if not isinstance(zones, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and _NAME.fullmatch(v)
            for k, v in zones.items()):
        raise ConfigInvalid("tenant_zones maps a tenant to a zone name")
    default_zone = body.get("default_zone", "residential")
    if not isinstance(default_zone, str) or not _NAME.fullmatch(default_zone):
        raise ConfigInvalid("default_zone is a zone name")
    bypass = [h.strip().lower() for h in _names(body.get("bypass_hosts", []), "bypass_hosts")]
    if not all(_HOST.fullmatch(h) for h in bypass):
        raise ConfigInvalid("bypass_hosts are host names or .suffixes")
    return EgressConfig(
        enabled=enabled,
        cause="enabled" if enabled else "disabled",
        country=country,
        tenants=frozenset(tenants),
        default_zone=default_zone,
        tenant_zones=dict(zones),
        monthly_cap_bytes=_positive(body, "monthly_cap_bytes", 0),
        tenant_monthly_cap_bytes=_positive(body, "tenant_monthly_cap_bytes", 0),
        attempt_cap_bytes=_positive(body, "attempt_cap_bytes", 0),
        connects_per_minute=_positive(body, "connects_per_minute", 600),
        max_tunnels_per_attempt=_positive(body, "max_tunnels_per_attempt", 128),
        bypass_hosts=tuple(bypass),
    )


def load_config(path: Path | str) -> EgressConfig:
    """The configuration at `path`, or OFF with the reason. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return EgressConfig(enabled=False, cause="config_unreadable")
    try:
        return parse_config(json.loads(text))
    except (ValueError, ConfigInvalid):
        return EgressConfig(enabled=False, cause="config_invalid")


__all__ = ["CONFIG_KEYS", "ConfigInvalid", "EgressConfig", "load_config", "parse_config"]
