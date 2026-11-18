"""The five spend fields must survive the round trip out of Firestore.

`control.record_spend` merge-sets input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens and cost_usd onto the
attempt document. They arrived intact. `attempt_from_dict` never read them
back, so the dataclass defaults applied and `attempt_to_api` served None for
every attempt that has ever run — every cost and token figure in the product
was unreachable from the API.

`peak_rss_bytes` is the control: same document, same decoder, and it
round-tripped throughout. Exactly the five spend fields were missing.

WHY NO TEST CAUGHT IT, which is the part worth keeping. The only test that
touched these fields was `test_absent_usage_is_null_not_zero`, asserting they
are None on an attempt that recorded no spend. That assertion passes whether
the decoder reads them or not — it is satisfied by the bug. Nothing asserted
they are PRESENT when the document carries them, so the missing half of the
pair was invisible.

The serialiser's comment made it durable: it said "null until the worker fix
ships in an agent-runtime-base image", which had already shipped. That
explanation was read, believed, and cited in docs/web-ui/redesign.md §6.2 as
the reason AgentDetail.tsx omits the cost columns. A working feature stayed
hidden behind a comment that had stopped being true.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api.codec import attempt_from_dict, attempt_to_api

SPEND = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cost_usd",
)


def _doc(**extra):
    base = {
        "attempt_id": "att_1",
        "task_id": "task_1",
        "tenant_id": "eng",
        "generation": 1,
        "created_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
    }
    base.update(extra)
    return base


def test_recorded_spend_reaches_the_api():
    """The bug, stated directly."""
    api = attempt_to_api(
        attempt_from_dict(
            _doc(
                input_tokens=12345,
                output_tokens=678,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=100,
                cost_usd=0.0642028,
            )
        )
    )
    assert api["input_tokens"] == 12345
    assert api["output_tokens"] == 678
    assert api["cache_read_input_tokens"] == 900
    assert api["cache_creation_input_tokens"] == 100
    assert api["cost_usd"] == pytest.approx(0.0642028)


@pytest.mark.parametrize("field", SPEND)
def test_each_spend_field_independently(field):
    """Parametrised so a decoder that reads four of five still fails."""
    api = attempt_to_api(attempt_from_dict(_doc(**{field: 7})))
    assert api[field] == 7, f"{field} was dropped between Firestore and the API"


def test_peak_rss_is_the_control():
    """If this ever fails the harness is wrong, not the decoder."""
    api = attempt_to_api(attempt_from_dict(_doc(peak_rss_bytes=123456789)))
    assert api["peak_rss_bytes"] == 123456789


@pytest.mark.parametrize("field", SPEND)
def test_absent_stays_none_not_zero(field):
    """The half that was already tested, kept. NULL IS NOT ZERO: an attempt
    with no measurement must render as an em dash, never $0.00, or a run
    nobody measured reads as a free one."""
    api = attempt_to_api(attempt_from_dict(_doc()))
    assert api[field] is None


@pytest.mark.parametrize("field", SPEND)
def test_a_measured_zero_survives(field):
    """The trap in the fix itself. `data.get(k) or None` would turn a real 0
    back into "not measured" — the exact conflation this codebase has spent
    three days removing. A cached-read run genuinely costs 0 output tokens."""
    api = attempt_to_api(attempt_from_dict(_doc(**{field: 0})))
    assert api[field] == 0, f"a measured zero for {field} was turned back into None"
    assert api[field] is not None


def test_the_serialiser_no_longer_blames_the_worker():
    """The comment was cited as fact in a design doc and shaped a UI decision."""
    import inspect

    from swarm_api import codec

    src = inspect.getsource(codec)
    assert "Null until the worker fix ships" not in src, (
        "the serialiser still claims these are null pending a worker fix that shipped; "
        "that comment is why the cost columns were removed from the UI"
    )
