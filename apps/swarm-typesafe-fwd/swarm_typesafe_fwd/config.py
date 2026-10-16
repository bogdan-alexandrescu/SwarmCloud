"""The platform setting the forwarder obeys (docs/design/workspace-secrets.md §3.5).

`config.json` in the ConfigMap `swarm-typesafe-fwd-config`, rendered by
terraform/infra/typesafe_fwd.tf from `var.typesafe_forwarder` and mounted as a
directory (never subPath), so the kubelet refreshes it in place and the
forwarder re-reads it every CONFIG_POLL_SECONDS (main.py): the kill switch is
a `terraform apply`, or in an emergency `kubectl edit configmap`, away, with
no restart.

EXACTLY the five keys CONFIG_KEYS, all required. FAIL CLOSED: a file that is
missing, cannot be read, is not JSON, lacks a key, carries a key this module
does not know, or holds a value of the wrong type is the switch OFF (`cause`
says which), and every request is 503 `disabled`. An unknown key is refused
rather than ignored so a setting renamed on one side cannot silently do
nothing on the other; tests/unit/typesafe_fwd/test_typesafe_fwd_ticket.py
reads typesafe_fwd.tf's keys and holds them equal to CONFIG_KEYS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_KEYS = ("enabled", "tenants", "requests_per_minute", "max_request_bytes",
               "max_response_seconds")


class ConfigInvalid(Exception):
    pass


@dataclass(frozen=True)
class FwdConfig:
    #: The kill switch. Off unless the file says otherwise.
    enabled: bool = False
    #: Why the switch is off, for the log: `disabled` (the setting says so),
    #: `config_unreadable` or `config_invalid`; `enabled` when on.
    cause: str = "disabled"
    #: The tenants served; a ticket for any other is 403 `tenant_off`, so
    #: removing a tenant here stops its running attempts' judgments too.
    tenants: frozenset[str] = frozenset()
    #: Per attempt, over a sliding minute.
    requests_per_minute: int = 1
    #: A body over this is 413 and never read.
    max_request_bytes: int = 1
    #: The upstream timeout; past it the answer is 504 `upstream_timeout`.
    max_response_seconds: int = 1


def _positive(body: dict[str, Any], name: str) -> int:
    value = body[name]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigInvalid(f"{name} is a positive integer")
    return value


def parse_config(body: Any) -> FwdConfig:
    if not isinstance(body, dict):
        raise ConfigInvalid("the configuration is a JSON object")
    if set(body) != set(CONFIG_KEYS):
        raise ConfigInvalid("the configuration has exactly the keys "
                            + ", ".join(CONFIG_KEYS))
    enabled = body["enabled"]
    if not isinstance(enabled, bool):
        raise ConfigInvalid("enabled is true or false")
    tenants = body["tenants"]
    if not isinstance(tenants, list) or not all(isinstance(t, str) and t for t in tenants):
        raise ConfigInvalid("tenants is a list of tenant ids")
    return FwdConfig(
        enabled=enabled,
        cause="enabled" if enabled else "disabled",
        tenants=frozenset(tenants),
        requests_per_minute=_positive(body, "requests_per_minute"),
        max_request_bytes=_positive(body, "max_request_bytes"),
        max_response_seconds=_positive(body, "max_response_seconds"),
    )


def load_config(path: Path | str) -> FwdConfig:
    """The configuration at `path`, or OFF with the reason. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return FwdConfig(enabled=False, cause="config_unreadable")
    try:
        return parse_config(json.loads(text))
    except (ValueError, ConfigInvalid):
        return FwdConfig(enabled=False, cause="config_invalid")


__all__ = ["CONFIG_KEYS", "ConfigInvalid", "FwdConfig", "load_config", "parse_config"]
