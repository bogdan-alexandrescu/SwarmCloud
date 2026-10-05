"""Let the reconciler's tests reach the fixtures the worker suite already builds.

The in-memory Firestore, the Cloud Run fakes and the seeded running task live
once, in `tests/unit/worker`, and the reconciler tests there import them by
module name. `tests/unit/worker` is not a package, so pytest puts it on
`sys.path` only when it collects something from it; running
`pytest tests/unit/reconciler` on its own would not. So it is put there
explicitly, rather than copying a second statement of what a task, a lease or
a Cloud Run execution looks like.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKER = str(Path(__file__).resolve().parents[1] / "worker")
if _WORKER not in sys.path:
    sys.path.insert(0, _WORKER)

from fakes import FakeFirestore  # noqa: E402


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()
