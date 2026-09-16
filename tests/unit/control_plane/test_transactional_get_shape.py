"""A transactional get returns a generator, not a snapshot.

`Transaction.get()` in google-cloud-firestore yields results, because the same
method accepts a Query as well as a DocumentReference. Every in-memory test
double in this repository returned a snapshot directly, so the whole admission
path passed its unit tests and then failed on the first real wake with

    AttributeError: 'generator' object has no attribute 'exists'

returning 503 to Pub/Sub. Nothing alerted: the subscription retried quietly and
tasks simply stayed READY for ever.
"""

from __future__ import annotations

import pytest

from swarm_common.admission import _snapshot


class _Snap:
    def __init__(self, exists: bool = True) -> None:
        self.exists = exists

    def to_dict(self) -> dict:
        return {"state": "READY"}


def test_accepts_a_bare_snapshot():
    """The shape every in-memory double produces."""
    snap = _Snap()
    assert _snapshot(snap) is snap


def test_accepts_a_generator_which_is_what_firestore_really_returns():
    snap = _Snap()
    gen = (s for s in [snap])
    assert _snapshot(gen) is snap


def test_accepts_a_list():
    snap = _Snap()
    assert _snapshot([snap]) is snap


def test_a_missing_document_still_yields_a_snapshot_reporting_absence():
    """Firestore yields a snapshot with exists=False, not an empty generator."""
    snap = _Snap(exists=False)
    assert _snapshot((s for s in [snap])).exists is False


def test_an_empty_result_raises_rather_than_returning_none():
    """Returning None would surface later as a confusing AttributeError."""
    with pytest.raises(RuntimeError):
        _snapshot(iter([]))
