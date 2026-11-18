"""The CONFIGURED request limits, as `/v1/stats` and `/v1/version` serve them (U25).

ONE function both routes call, so the two payloads cannot drift: a test holds
`limits` on `/v1/version` equal to `limits` on `/v1/stats`.

THESE ARE CONFIGURATION, NEVER A REMAINING BUDGET. No route serves a
remaining-request or headroom figure, and none may (owner decision,
2026-10-01). The limiter (`ratelimit.TokenBucketLimiter`) is an in-process
token bucket per principal PER INSTANCE, built once per process in
`deps.build_context`. With N Cloud Run instances the effective ceiling is about
N x `requests_per_second_per_instance`, and a caller's next request is routed
to whichever instance the load balancer picks -- so a remaining count read from
the instance that answered this request says nothing true about the next one.
The configured rate and burst are the only figures every instance agrees on.
"""

from __future__ import annotations

from typing import Any


def configured_limits(settings: Any) -> dict[str, int]:
    """The configured limits, from `ApiSettings`. Nothing here is measured."""
    core = settings.core
    return {
        "max_batch_size": core.max_batch_size,
        "max_input_bytes": core.max_input_bytes,
        "max_workflow_steps": core.max_workflow_steps,
        "requests_per_second_per_instance": core.requests_per_second,
        # The bucket's size, per principal per instance (deps.build_context).
        "rate_limit_burst_per_instance": settings.rate_limit_burst,
    }
