"""`POST /v1/attempts/{attempt_id}/spend-estimate`: swarm-api prices a stopped run (#1481).

A worker whose CLI was stopped before it reported a cost -- a cancel, a
SIGTERM, a fence, a timeout (#627) -- sends the per-model token counts its
stream showed, and gets back what they cost at the platform's list prices
(`modelprices`). A model with no price makes the whole figure None: "cost not
reported", never a guess.

WHO: the attempt proof every worker-only route takes -- the tenant's worker
service account, then the attempt key it registered -- one 403 for every
cause (`attemptcontext._prove`). NO FENCE, on purpose: this is asked as an
attempt is being stopped, when its task is no longer RUNNING under a live
lease, and it reads nothing of any tenant's and writes nothing -- only the
platform's price table. The figure goes back to the worker, which records it
through its own spend path. Nothing here creates demand (1), touches capacity
(2, 3) or the lease (5); the caller names models and counts, never a price (10).

A model priced in tiers is priced per request by its own prompt, from the
`prompt_buckets` the worker sends (`modelprices.estimate`, PRICES-TIER-PROMPT).
The answer's `tier_estimated` says a tiered model was priced by its summed
counts instead -- an older worker's body, with no buckets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .childkey import AttemptTuple
from .children import ChildService, ChildUnauthenticated, ChildUnproven, parse_body
from .errors import ApiError, ValidationFailed
from .modelprices import (BUCKET_COUNTS, CURRENCY, MAX_MODELS, MAX_PROMPT_BUCKETS, estimate,
                          price_for)


#: Room for `prompt_buckets` (PRICES-TIER-PROMPT): a row is about 40 bytes,
#: so a model's 1,024 rows are about 40 KiB. A worker whose body would pass
#: this sends sums alone (`agent_worker.spendprice.MAX_BODY_BYTES`, held
#: equal by a test), priced and marked `tier_estimated`.
MAX_BODY_BYTES = 256 * 1024

#: One `prompt_buckets` row: the bucket, then its four counts (`BUCKET_COUNTS`).
_Row = tuple[Annotated[StrictInt, Field(ge=0)], Annotated[StrictInt, Field(ge=0)],
             Annotated[StrictInt, Field(ge=0)], Annotated[StrictInt, Field(ge=0)],
             Annotated[StrictInt, Field(ge=0)]]
UNPROVEN_MESSAGE = "this request is not proven for this attempt"


class EstimateUnproven(ApiError):
    """The child routes' code for the same attempt proof (`attemptcontext.ContextUnproven`)."""

    status_code = 403
    code = "child_submit_unproven"


class _Counts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: StrictInt = Field(default=0, ge=0)
    output_tokens: StrictInt = Field(default=0, ge=0)
    cache_creation_input_tokens: StrictInt = Field(default=0, ge=0)
    cache_read_input_tokens: StrictInt = Field(default=0, ge=0)
    #: The same counts split by each request's own prompt size
    #: (`modelprices.PROMPT_BUCKET_TOKENS`). Absent from an older worker.
    prompt_buckets: list[_Row] | None = Field(default=None, max_length=MAX_PROMPT_BUCKETS)

    @model_validator(mode="after")
    def _rows_add_up(self) -> "_Counts":
        # Rising and distinct, and summing to the model's own counts: a row
        # set that disagreed with the sums would price a tiered model by one
        # set of numbers and an untiered one by the other.
        rows = self.prompt_buckets
        if rows is None:
            return self
        if any(a[0] >= b[0] for a, b in zip(rows, rows[1:])):
            raise ValueError("prompt_buckets rows are in rising bucket order, one row a bucket")
        for at, key in enumerate(BUCKET_COUNTS, start=1):
            if sum(row[at] for row in rows) != getattr(self, key):
                raise ValueError(f"prompt_buckets' {key} does not add up to the model's")
        return self


class EstimateBody(BaseModel):
    # Strict: the worker names models and counts, never a price (invariant 10).
    # Exactly what `agent_worker.spendprice.SpendPricer.price` sends.
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=1)
    usage_by_model: dict[Annotated[str, Field(min_length=1, max_length=128)], _Counts] = Field(
        min_length=1, max_length=MAX_MODELS)


@dataclass
class SpendEstimate:
    """The route's logic (module docstring). Built per request from the app context."""

    store: Any
    children: ChildService

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any) -> "SpendEstimate":
        children = ChildService(
            settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
            verifier=verifier, limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
        )
        return cls(store=ctx.store, children=children)

    def handle(self, attempt_id: str, raw: bytes, *, authorization: str | None,
               proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        self.children._require_configured()
        if len(raw) > MAX_BODY_BYTES:
            raise ValidationFailed(f"a spend estimate request is at most {MAX_BODY_BYTES} bytes")
        body: EstimateBody = parse_body(EstimateBody, raw)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        try:
            self.children.authenticate_worker(authorization, body.tenant_id)
            if body.attempt_id != attempt_id:
                raise ChildUnproven("the body names another attempt")
            self.children.verify_proof(attempt, method="POST", path=path, body=raw,
                                       proof=proof, timestamp=timestamp)
        except (ChildUnauthenticated, ChildUnproven):
            raise EstimateUnproven(UNPROVEN_MESSAGE) from None
        prices = self.store.get_platform_settings()["model_prices"]
        usage = {model: counts.model_dump(exclude_none=True)
                 for model, counts in body.usage_by_model.items()}
        cost, tier_estimated = estimate(usage, prices)
        unpriced = sorted(m for m in usage if price_for(m, prices) is None)
        return {"cost_usd": cost, "currency": CURRENCY, "unpriced_models": unpriced,
                "tier_estimated": tier_estimated}
