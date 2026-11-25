"""The `outcomes` group: the outcome ledger's choices and limits (part of #72).

The Timeline (`apps/swarm-ui/src/outcomes.ts`) offers the spans, buckets,
kinds and groups `GET /v1/outcomes` accepts, opens on its default span, and
never asks for more than the route's limits allow -- a changed limit must turn
CI red rather than turn a control into a 422. Read from the live constants of
`swarm_api.outcomes`, which the route validates with.
"""

from __future__ import annotations

from typing import Any

INPUTS = (
    "scripts/api_constants/outcomes.py",
    "apps/swarm-api/swarm_api/outcomes.py",
)


def build() -> dict[str, Any]:
    from swarm_api import outcomes

    return {
        # In the order the route declares them, which is the order the page offers them.
        "spans": list(outcomes.SPANS),
        "default_span": outcomes.DEFAULT_SPAN,
        "buckets": list(outcomes.BUCKETS),
        "kinds": list(outcomes.KINDS),
        "groups": list(outcomes.GROUPS),
        "row_outcomes": list(outcomes.ROW_OUTCOMES),
        "max_buckets": outcomes.MAX_BUCKETS,
        "month_min_days": outcomes.MONTH_MIN_DAYS,
        "max_span_days": outcomes.MAX_SPAN_DAYS,
        "outcomes_cache_s": outcomes.OUTCOMES_CACHE_S,
    }
