"""Execution limits for the process a runner starts.

A runner's child -- the agent CLI, the build command -- gets a wall clock, a
SIGTERM grace period and an output cap. Those are execution parameters, and
invariant 10 says a caller never supplies one: `input.timeout_seconds` used to
be read straight out of the task's `input` with no ceiling, so
`max_stdout_bytes: 10**12` removed the cap entirely and a task could fill the
ephemeral disk the resource class sizes until the kubelet evicted the pod
mid-attempt.

The rule here is the one the API already applies to `timeout_seconds`: **a
caller may lower a limit, never raise one.** The ceiling comes from the
platform -- the worker exports it from `WorkerConfig`, which is built from the
frozen catalogue -- and an absolute hard maximum in this file bounds even that,
so a doctored worker environment cannot widen the cap either.

Every clamp is reported rather than silently applied: `ChildLimits.clamped`
names the fields that were reduced, and the runners log it, because "my timeout
was ignored" is otherwise a mystery from the outside.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

#: Absolute ceilings. Nothing -- not a caller, not the worker's environment --
#: produces a limit above these. They exist so that a mistake in the layer above
#: is bounded rather than unbounded.
HARD_MAX_TIMEOUT_SECONDS = 24 * 3600
HARD_MAX_GRACE_SECONDS = 300.0
HARD_MAX_STDOUT_BYTES = 64 * 1024 * 1024
HARD_MAX_STDERR_BYTES = 16 * 1024 * 1024

#: Used when the worker exported nothing, which happens only for a runner
#: started by hand outside the worker (a local repro).
DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_GRACE_SECONDS = 20.0
DEFAULT_STDOUT_BYTES = 32 * 1024 * 1024
DEFAULT_STDERR_BYTES = 8 * 1024 * 1024

#: Environment names the WORKER sets from its own config. A runner reads them;
#: nothing a caller sends ever reaches them, because the child environment is
#: built from an allowlist in `workspace.child_env`.
TIMEOUT_ENV = "SWARM_CHILD_TIMEOUT_SECONDS"
GRACE_ENV = "SWARM_CHILD_GRACE_SECONDS"
STDOUT_ENV = "SWARM_CHILD_MAX_STDOUT_BYTES"
STDERR_ENV = "SWARM_CHILD_MAX_STDERR_BYTES"


@dataclass(frozen=True)
class ChildLimits:
    timeout_seconds: float
    grace_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    #: Fields whose requested value was above the ceiling and was reduced.
    clamped: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "grace_seconds": self.grace_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "clamped": list(self.clamped),
        }


def _env_number(env: Mapping[str, str], name: str, default: float, hard_max: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return min(value, hard_max)


def platform_ceilings(env: Mapping[str, str] | None = None) -> ChildLimits:
    """The largest limits this attempt may use, from the platform's own config."""
    env = env if env is not None else os.environ
    return ChildLimits(
        timeout_seconds=_env_number(
            env, TIMEOUT_ENV, DEFAULT_TIMEOUT_SECONDS, HARD_MAX_TIMEOUT_SECONDS
        ),
        grace_seconds=_env_number(env, GRACE_ENV, DEFAULT_GRACE_SECONDS, HARD_MAX_GRACE_SECONDS),
        max_stdout_bytes=int(
            _env_number(env, STDOUT_ENV, DEFAULT_STDOUT_BYTES, HARD_MAX_STDOUT_BYTES)
        ),
        max_stderr_bytes=int(
            _env_number(env, STDERR_ENV, DEFAULT_STDERR_BYTES, HARD_MAX_STDERR_BYTES)
        ),
    )


def _requested(payload: Mapping[str, Any], key: str) -> float | None:
    """A caller's value for `key`, or None when absent or unusable.

    An unparseable or non-positive value is treated as absent rather than as an
    error: the platform ceiling is always a safe answer, and failing the attempt
    over a malformed optional field would be a worse outcome than ignoring it.
    """
    if key not in payload:
        return None
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_limits(
    payload: Mapping[str, Any],
    ceilings: ChildLimits | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ChildLimits:
    """Combine the platform ceiling with what the caller asked for.

    The result is never larger than the ceiling in any dimension, whatever the
    payload says.
    """
    ceilings = ceilings or platform_ceilings(env)
    clamped: list[str] = []
    values: dict[str, float] = {
        "timeout_seconds": ceilings.timeout_seconds,
        "grace_seconds": ceilings.grace_seconds,
        "max_stdout_bytes": float(ceilings.max_stdout_bytes),
        "max_stderr_bytes": float(ceilings.max_stderr_bytes),
    }
    for key, ceiling in list(values.items()):
        asked = _requested(payload, key)
        if asked is None:
            continue
        if asked > ceiling:
            clamped.append(key)
            continue
        values[key] = asked
    return ChildLimits(
        timeout_seconds=values["timeout_seconds"],
        grace_seconds=values["grace_seconds"],
        max_stdout_bytes=int(values["max_stdout_bytes"]),
        max_stderr_bytes=int(values["max_stderr_bytes"]),
        clamped=tuple(clamped),
    )
