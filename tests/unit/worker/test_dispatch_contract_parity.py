"""The worker and swarm-api have to mean the same thing by `dispatch`.

`task.metadata["dispatch"]` is a contract between two packages that never
import each other -- deliberately: a worker that imported swarm-api would pull
the whole control plane into the image every agent runs in. The price of that
separation is that the vocabulary is written out twice, and a vocabulary
written out twice drifts.

It had. swarm-api's `DISPATCH_CARRIERS` is `("checkpoints", "branches")` with
`checkpoints` as the default, so `DispatchOptions().to_metadata()` -- the block
every default dispatch actually carries -- says `carrier: "checkpoints"`. The
worker's parser accepted `("patches", "branches")` and fell back to `"patches"`:
a value the API's validator REFUSES at submission and can therefore never send.
The two ends overlapped on exactly one word out of three, and every default
dispatch was read as unrecognised.

Nothing had broken yet only because nothing in the worker branches on the
carrier. That is not a defence -- it is the same shape as every fault this
codebase has shipped: both ends built, the seam never executed. The first
feature to read `_dispatch_carrier()` would have found `"patches"` for a
dispatch that said `"checkpoints"`, and the guard against that is not a comment.

These tests import swarm-api's own constants rather than restating them, so a
change to the accepted set on either side fails here instead of in production.
The PRODUCTION worker still imports nothing from swarm-api; only this test does.
"""

from __future__ import annotations

import pytest

from swarm_api.validation import (
    DEFAULT_CARRIER,
    DEFAULT_STRATEGY,
    DISPATCH_CARRIERS,
    DISPATCH_METADATA_KEY,
    DISPATCH_STRATEGIES,
    DispatchOptions,
)


def _worker(worker_factory, block):
    worker, config, _ = worker_factory()
    worker._task = {"task_id": config.task_id, "metadata": {DISPATCH_METADATA_KEY: block}}
    return worker


@pytest.mark.parametrize("carrier", DISPATCH_CARRIERS)
def test_every_carrier_the_api_accepts_is_read_back_unchanged(worker_factory, carrier):
    """A value the API accepts on submit must survive the trip to the worker.

    `checkpoints` is the one that did not: it is the default, so this covers
    the block almost every real dispatch carries.
    """
    w = _worker(worker_factory, {"strategy": "collect", "carrier": carrier})
    assert w._dispatch_carrier() == carrier


@pytest.mark.parametrize("strategy", DISPATCH_STRATEGIES)
def test_every_strategy_the_api_accepts_is_read_back_unchanged(worker_factory, strategy):
    w = _worker(worker_factory, {"strategy": strategy})
    assert w._dispatch_strategy() == strategy


def test_the_worker_falls_back_to_the_apis_defaults_not_its_own(worker_factory):
    """An absent field has to mean what the API says it means.

    The worker's fallback used to be `"patches"`, which `DISPATCH_CARRIERS` has
    never contained. A default that is not one of the accepted values is not a
    default -- it is a fourth state nobody wrote down.
    """
    w = _worker(worker_factory, {})
    assert w._dispatch_carrier() == DEFAULT_CARRIER
    assert w._dispatch_strategy() == DEFAULT_STRATEGY
    assert DEFAULT_CARRIER in DISPATCH_CARRIERS
    assert DEFAULT_STRATEGY in DISPATCH_STRATEGIES


def test_the_block_a_default_dispatch_really_carries_parses_cleanly(worker_factory):
    """Built by the API's own `to_metadata`, not by hand.

    Hand-written fixtures are how the mismatch survived: every worker test wrote
    the block it expected rather than the block the control plane produces.
    """
    w = _worker(worker_factory, DispatchOptions().to_metadata())
    assert w._dispatch_strategy() == DEFAULT_STRATEGY
    assert w._dispatch_carrier() == DEFAULT_CARRIER


def test_an_integrate_block_from_the_api_parses_in_full(worker_factory):
    """All four fields, as `with_role` assembles them for a real workflow."""
    options = DispatchOptions(strategy="integrate", carrier="branches").with_role(
        "integrator", ["t-a", "t-b"]
    )
    w = _worker(worker_factory, options.to_metadata())
    assert w._dispatch_strategy() == "integrate"
    assert w._dispatch_carrier() == "branches"
    assert w._dispatch_role() == "integrator"
    assert w._dispatch_integrates() == ["t-a", "t-b"]


def test_a_contributor_block_from_the_api_parses_in_full(worker_factory):
    options = DispatchOptions(strategy="integrate", carrier="branches").with_role("contributor")
    w = _worker(worker_factory, options.to_metadata())
    assert w._dispatch_role() == "contributor"
    assert w._dispatch_integrates() == []


def test_the_worker_accepts_no_carrier_the_api_would_have_refused(worker_factory):
    """The other direction of the same parity.

    A worker that accepted a word the validator rejects would be honouring a
    dispatch that cannot legally exist -- and would hide the fact that some
    other producer is writing into `metadata.dispatch`.
    """
    for bogus in ("patches", "patch", "tarballs", "pull-requests"):
        w = _worker(worker_factory, {"carrier": bogus})
        assert w._dispatch_carrier() == DEFAULT_CARRIER, (
            f"the worker honoured carrier {bogus!r}, which swarm-api refuses"
        )


def test_the_metadata_key_is_the_same_one_on_both_sides(worker_factory):
    """`DISPATCH_METADATA_KEY` is one nested key precisely because `metadata`
    is caller-supplied. If the worker read a different key it would read the
    CALLER's data as the platform's integration contract."""
    worker, config, _ = worker_factory()
    worker._task = {
        "task_id": config.task_id,
        "metadata": {DISPATCH_METADATA_KEY: {"strategy": "direct-pr"}},
    }
    assert worker._dispatch_strategy() == "direct-pr"
