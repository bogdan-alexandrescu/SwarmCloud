"""The in-memory Firestore refuses a document path real Firestore refuses.

`Client.document("runs")` raises in google-cloud-firestore because a document
path needs an even number of segments; a fake that accepted it would let a test
pass against a path production could never build a reference to.
"""

from __future__ import annotations

import pytest

from .fakes import FakeFirestore


@pytest.mark.parametrize("path", ["runs", "tenants/t1/runs", "", "tenants//runs/r1", "/tenants/t1"])
def test_document_refuses_a_path_that_is_not_a_document(path: str) -> None:
    db = FakeFirestore()
    with pytest.raises(ValueError, match="even number"):
        db.document(path)


def test_document_accepts_an_even_path() -> None:
    db = FakeFirestore()
    ref = db.document("tenants/t1/runs/r1")
    ref.set({"state": "QUEUED"})
    assert db.docs == {"tenants/t1/runs/r1": {"state": "QUEUED"}}
    assert db.document("tenants/t1").id == "t1"
