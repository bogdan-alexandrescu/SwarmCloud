"""The install lease: one CLI runs steps at a time (design §2.3, plan §5.4).

The lease is the object `swarm-install/lock` in the state bucket. It is
created with `ifGenerationMatch=0`, so of two CLIs starting at once exactly one
gets it; it holds the holder's email and an expiry 30 minutes ahead, and is
renewed on every step and, while the CLI waits, every 30 s (the hand-off's
heartbeat, plan §5.5). Another admin may take over an expired lease only after
a typed confirmation: they type the holder's email. `SWARM_ASSUME_YES` is
never read here (CLAUDE.md: anything destructive takes a typed confirmation).

Before the bucket exists (stages 0 and 1) there is nothing to lock, and the
lease is local: `Lease.local()`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .state import ObjectStore, PreconditionFailed

LEASE_OBJECT = "swarm-install/lock"
LEASE_SECONDS = 30 * 60
HEARTBEAT_SECONDS = 30


class LeaseHeld(RuntimeError):
    """Another admin holds a live lease."""

    def __init__(self, holder: str, expires: float) -> None:
        super().__init__(
            f"{holder} is running the install (lease until {expires:.0f}); wait for it to "
            "finish or expire, then run swarm install again"
        )
        self.holder = holder
        self.expires = expires


class LeaseRefused(RuntimeError):
    """An expired lease was not taken over: the typed confirmation was missing."""


class LeaseLost(RuntimeError):
    """The lease changed under its holder (another admin took it over)."""


@dataclass
class Lease:
    holder: str
    store: ObjectStore | None
    clock: Callable[[], float]
    generation: int = 0
    expires: float = 0.0
    heartbeat: float = 0.0

    @classmethod
    def local(cls, holder: str, clock: Callable[[], float]) -> Lease:
        return cls(holder=holder, store=None, clock=clock)

    def _body(self, now: float) -> bytes:
        return json.dumps(
            {"holder": self.holder, "heartbeat": now, "expires": now + LEASE_SECONDS},
            sort_keys=True,
        ).encode()

    def _write(self, match: int) -> None:
        now = self.clock()
        assert self.store is not None
        self.generation = self.store.write(LEASE_OBJECT, self._body(now), if_generation_match=match)
        self.heartbeat = now
        self.expires = now + LEASE_SECONDS

    def acquire(self, confirm: Callable[[str], str] | None = None) -> None:
        """Take the lease, or raise `LeaseHeld` / `LeaseRefused`.

        `confirm(prompt)` returns what the person typed; it is asked only to
        take over an expired lease held by someone else.
        """
        if self.store is None:
            now = self.clock()
            self.heartbeat, self.expires = now, now + LEASE_SECONDS
            return
        try:
            self._write(0)
            return
        except PreconditionFailed:
            pass
        found = self.store.read(LEASE_OBJECT)
        if found is None:
            self._write(0)
            return
        data, generation = found
        current: dict[str, Any] = json.loads(data)
        holder = str(current.get("holder", ""))
        expires = float(current.get("expires", 0))
        if holder == self.holder:
            self._write(generation)
            return
        if expires > self.clock():
            raise LeaseHeld(holder, expires)
        typed = confirm(
            f"{holder}'s lease expired. Type their email to take over the install: "
        ) if confirm is not None else ""
        if typed.strip() != holder:
            raise LeaseRefused(
                f"the lease held by {holder} expired; taking it over needs their email typed"
            )
        try:
            self._write(generation)
        except PreconditionFailed as exc:
            raise LeaseHeld(holder, expires) from exc

    def renew(self) -> None:
        if self.store is None:
            now = self.clock()
            self.heartbeat, self.expires = now, now + LEASE_SECONDS
            return
        try:
            self._write(self.generation)
        except PreconditionFailed as exc:
            raise LeaseLost("the install lease was taken over; stop and re-run swarm install") from exc

    def heartbeat_due(self) -> bool:
        return self.clock() - self.heartbeat >= HEARTBEAT_SECONDS

    def release(self) -> None:
        if self.store is None:
            return
        try:
            self.store.delete(LEASE_OBJECT, if_generation_match=self.generation)
        except PreconditionFailed:
            pass

    def as_record(self) -> dict[str, Any]:
        """The `lease` field of the install record (plan §5.4)."""
        return {"holder": self.holder, "heartbeat": self.heartbeat, "expires": self.expires}
