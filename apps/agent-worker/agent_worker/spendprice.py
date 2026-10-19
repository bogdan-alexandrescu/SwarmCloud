"""What a stopped run cost, asked of swarm-api (#1481, owner decision 2026-10-17).

A CLI stopped before it reported a cost -- a cancel, a SIGTERM, a fence, a
timeout (#627) -- leaves only the per-message usage its stream carried. The
worker sums that usage per model (`lifecycle._stream_usage`) and asks
`POST /v1/attempts/{attempt_id}/spend-estimate` (`swarm_api.spendestimate`)
what it cost. THE WORKER HOLDS NO PRICE TABLE: list prices are a platform
setting in Firestore (`model_prices`), and workers are moving off direct
Firestore access (#1457, design B), so swarm-api reads them and prices.

None is "cost not reported", and it is the answer whenever swarm-api did not
give a figure: a model it has no price for, no SWARM_API_URL, no attempt key,
a refusal, an unreachable API. Nothing is priced here instead -- a guessed
figure is a wrong one. Signed with the attempt key the child path registered
(`children.ChildPath.sign_request`), as the forge broker's request is.

A MODEL PRICED IN TIERS by prompt length is priced per request by that
request's own prompt (PRICES-TIER-PROMPT), so each model's counts go with
their `prompt_buckets` (`lifecycle._stream_usage`). A body that would pass
swarm-api's limit with them is sent as the sums alone instead: swarm-api
prices those by the summed rule and answers `tier_estimated: true`, which the
attempt's summary carries. A figure, marked, rather than a refusal and none.

Never raises: this runs on the teardown path, where the lease is about to be
released either way.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import quote

from .children import ChildApiError, ChildPath

#: The largest body `spend-estimate` accepts
#: (`swarm_api.spendestimate.MAX_BODY_BYTES`; a test holds the two equal).
MAX_BODY_BYTES = 256 * 1024


class SpendPricer:
    """One attempt's stopped-run pricing, through swarm-api."""

    def __init__(self, cfg: Any, *, children: ChildPath, logger: Any) -> None:
        self.cfg = cfg
        self._children = children
        self.log = logger

    def _path(self) -> str:
        return f"/v1/attempts/{quote(self.cfg.attempt_id, safe='')}/spend-estimate"

    def price(self, usage_by_model: Mapping[str, Mapping[str, Any]]) -> float | None:
        """USD for `usage_by_model` at swarm-api's list prices, or None."""
        return self.estimate(usage_by_model)[0]

    def _body(self, usage_by_model: Mapping[str, Mapping[str, Any]], *, buckets: bool) -> bytes:
        return json.dumps({
            "tenant_id": self.cfg.tenant_id,
            "task_id": self.cfg.task_id,
            "attempt_id": self.cfg.attempt_id,
            "lease_id": self.cfg.lease_id,
            "generation": self.cfg.generation,
            "usage_by_model": {
                model: {k: v for k, v in counts.items() if buckets or k != "prompt_buckets"}
                for model, counts in usage_by_model.items()
            },
        }).encode("utf-8")

    def estimate(self, usage_by_model: Mapping[str, Mapping[str, Any]]) -> tuple[float | None, bool]:
        """`(USD or None, tier_estimated)` for `usage_by_model` at swarm-api's list prices.

        `tier_estimated` is swarm-api's word that a tiered model's tier was
        picked from summed counts rather than per request."""
        if not usage_by_model:
            return None, False
        api = self._children.api
        if api is None or not self._children.can_sign:
            self.log.warning("stopped run's cost not reported: no way to ask swarm-api for it",
                             reason="no_api" if api is None else "no_attempt_key")
            return None, False
        path = self._path()
        body = self._body(usage_by_model, buckets=True)
        if len(body) > MAX_BODY_BYTES:
            self.log.info("stopped run's usage sent without its prompt buckets: too large",
                          bytes=len(body))
            body = self._body(usage_by_model, buckets=False)

        def ask() -> tuple[int, dict[str, Any]]:
            # Signed per try: the proof carries a timestamp swarm-api bounds.
            headers = self._children.sign_request("POST", path, body)
            return api.call("POST", path, body=body, headers=headers)

        try:
            _, answer = self._children.retrying(ask)
        except (ChildApiError, LookupError) as exc:
            self.log.warning("stopped run's cost not reported: swarm-api did not price it",
                             decision=getattr(exc, "code", type(exc).__name__))
            return None, False
        except Exception as exc:  # pragma: no cover - defensive; teardown path
            self.log.warning("stopped run's cost not reported", error=type(exc).__name__)
            return None, False
        cost = answer.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            unpriced = answer.get("unpriced_models")
            self.log.info("stopped run's cost not reported: no list price",
                          unpriced_models=unpriced if isinstance(unpriced, list) else None)
            return None, False
        return float(cost), answer.get("tier_estimated") is True
