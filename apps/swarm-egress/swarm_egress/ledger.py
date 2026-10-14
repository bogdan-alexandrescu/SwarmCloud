"""swarm-egress's own byte counter, and the caps it enforces (docs/browser-egress.md §3.5, §7.1 Q3).

Every byte relayed in either direction through a tunnel is counted three
ways: for the platform this month, for the tenant this month, and for the
attempt. The vendor bills both directions, so both are counted.

  * the PLATFORM monthly cap is the owner's $150 pilot (Q3), turned into bytes
    by terraform/infra/egress.tf. It answers before the vendor's sub-user
    traffic limit, which is the hard stop if this code is wrong;
  * the TENANT monthly cap stops one tenant without stopping the others;
  * the ATTEMPT cap (1 GB, §3.5) stops a runaway page, a video, a loop.

The month is the UTC calendar month, which is how the vendor bills; the
platform and tenant totals start again at zero on the first.

DURABLE. The counters are written to a file on the StatefulSet's persistent
volume (kubernetes/swarm-system/swarm-egress.yaml) every few seconds and on
shutdown, atomically (write, fsync, rename), so a restart does not hand the
month a fresh budget. A file that exists and cannot be read FAILS CLOSED
(`ledger_unreadable`): a ledger that does not know what was spent cannot say
the cap has room. An operator fixes or removes the file; removing it is a
deliberate "start this month's count again", and the vendor's own limit
still holds.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("swarm_egress.ledger")

#: An attempt's counter is kept this long after its last byte, then dropped.
ATTEMPT_RETENTION_SECONDS = 7 * 86400


def _month(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m")


class Ledger:
    def __init__(self, path: Path | str | None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self.healthy = True
        self._month = _month(clock())
        self._platform = 0
        self._tenants: dict[str, int] = {}
        self._attempts: dict[str, dict[str, float]] = {}
        self._dirty = False
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            body = json.loads(self._path.read_text(encoding="utf-8"))
            month = body["month"]
            platform = body["platform"]
            tenants = body["tenants"]
            attempts = body["attempts"]
            if not (isinstance(month, str) and isinstance(platform, int)
                    and isinstance(tenants, dict) and isinstance(attempts, dict)):
                raise ValueError("shape")
            parsed_tenants = {str(k): int(v) for k, v in tenants.items()}
            parsed_attempts = {str(k): {"bytes": int(v["bytes"]), "seen": float(v["seen"])}
                               for k, v in attempts.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.healthy = False
            log.error(json.dumps({"event": "egress_ledger_unreadable"}))
            return
        self._attempts = parsed_attempts
        if month == self._month:
            self._platform = platform
            self._tenants = parsed_tenants

    def flush(self) -> None:
        """Write the counters if they changed. Never raises; a failed write is
        logged and retried on the next flush."""
        if self._path is None or not self.healthy or not self._dirty:
            return
        self._roll()
        now = self._clock()
        self._attempts = {k: v for k, v in self._attempts.items()
                          if now - v["seen"] < ATTEMPT_RETENTION_SECONDS}
        body = {"month": self._month, "platform": self._platform, "tenants": self._tenants,
                "attempts": self._attempts}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            self._dirty = False
        except OSError as exc:
            log.error(json.dumps({"event": "egress_ledger_write_failed",
                                  "cause": type(exc).__name__}))

    # -- counting --------------------------------------------------------------

    def _roll(self) -> None:
        month = _month(self._clock())
        if month != self._month:
            self._month = month
            self._platform = 0
            self._tenants = {}
            self._dirty = True

    def add(self, tenant_id: str, attempt_id: str, count: int) -> None:
        if count <= 0:
            return
        self._roll()
        self._platform += count
        self._tenants[tenant_id] = self._tenants.get(tenant_id, 0) + count
        entry = self._attempts.setdefault(attempt_id, {"bytes": 0, "seen": 0.0})
        entry["bytes"] = int(entry["bytes"]) + count
        entry["seen"] = self._clock()
        self._dirty = True

    def platform_bytes(self) -> int:
        self._roll()
        return self._platform

    def tenant_bytes(self, tenant_id: str) -> int:
        self._roll()
        return self._tenants.get(tenant_id, 0)

    def attempt_bytes(self, attempt_id: str) -> int:
        return int(self._attempts.get(attempt_id, {}).get("bytes", 0))

    def over_cap(self, tenant_id: str, attempt_id: str, config: Any) -> str | None:
        """Which cap is reached, or None. Platform first: it is the budget."""
        if not self.healthy:
            return "ledger_unreadable"
        if self.platform_bytes() >= config.monthly_cap_bytes:
            return "platform_cap"
        if self.tenant_bytes(tenant_id) >= config.tenant_monthly_cap_bytes:
            return "tenant_cap"
        if self.attempt_bytes(attempt_id) >= config.attempt_cap_bytes:
            return "attempt_cap"
        return None


__all__ = ["Ledger"]
