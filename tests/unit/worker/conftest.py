"""Fixtures that assemble a real worker against in-memory infrastructure.

The seeds, builders and constants these fixtures use live in worker_seeds.py,
and test modules import them from there: every directory's conftest.py is the
top-level module `conftest` under pytest's `prepend` import mode, so a test
module that imported `conftest` got whichever directory's was loaded first
(owner decision 2026-10-06, observer P24).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from agent_worker.objectstore import LocalObjectStore

from fakes import FakeFirestore
from worker_seeds import _SIMULATED, BUCKET, build_worker


@pytest.fixture
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "gcs", bucket=BUCKET)


@pytest.fixture
def log_stream() -> io.StringIO:
    return io.StringIO()


@pytest.fixture
def runner_inputs(monkeypatch) -> list[dict[str, Any]]:
    """`work/input.json` as the runner saw it, read at every checkpoint.

    Tests used to read it back out of the final checkpoint's archive. The
    worker leaves it out of the archive since the PR #229 review (the task's
    whole input was served back out of every checkpoint), so this reads the
    file on disk at the moment each checkpoint is taken -- the same moment,
    and the same bytes, the archive held. Newest last.
    """
    import json

    from agent_worker.checkpoint import CheckpointManager

    seen: list[dict[str, Any]] = []
    original = CheckpointManager.create

    def create(self, ws, *args, **kwargs):
        if ws.input_path.exists():
            seen.append(json.loads(ws.input_path.read_text()))
        return original(self, ws, *args, **kwargs)

    monkeypatch.setattr(CheckpointManager, "create", create)
    return seen


@pytest.fixture(autouse=True)
def _the_runner_sees_its_simulation(monkeypatch):
    """Hand the mock runner what its simulated provider does, beside its input.

    THESE ARE NOT CALLER INPUT, AND ARE NEVER STORED AS ONE. The mock reads
    `spend`, `provider`, `credential_revoked_times`, `credential_detail`,
    `quota_detail` and `reset_at` to act out what a real provider does to a
    real runner: report a spend, revoke a credential, name its rate limit. The
    owner withheld them from callers on #142, because each writes a platform
    record; the API refuses them, and since contract request 32 the worker
    refuses a stored input that carries one before anything runs. So a test
    that needs one does not write it into the task document, which no caller
    can do either. It passes it here, and this puts it into `input.json` after
    the worker has checked the stored input and written the file -- the moment
    in production when the runner, not the platform, meets its provider.

    The seam is `Worker._build_child_env`, which the worker calls right after
    writing `input.json` and again on a credential reload; the merge is the
    same both times. A test that seeds nothing here sees no difference.
    """
    import json

    from agent_worker.lifecycle import Worker

    _SIMULATED.clear()
    original = Worker._build_child_env

    def build_child_env(self, *args: Any, **kwargs: Any):
        simulated = _SIMULATED.get(self.cfg.task_id)
        ws = getattr(self, "ws", None)
        if simulated and ws is not None and ws.input_path.exists():
            payload = json.loads(ws.input_path.read_text())
            payload.update(simulated)
            ws.input_path.write_text(json.dumps(payload, indent=2, default=str))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Worker, "_build_child_env", build_child_env)
    yield
    _SIMULATED.clear()


@pytest.fixture
def recheck_bypassed(monkeypatch) -> None:
    """A stored input that reached the runner step past the worker's re-check.

    For the tests of the worker's SECOND line: `input.model`, `staged_inputs`,
    `expected_outputs` and `attempt_count` are each dropped or overwritten by
    the lifecycle as it writes `input.json`, whatever the stored input holds.
    Since contract request 32 the worker refuses a stored input carrying any
    of them before it gets that far (`lifecycle._recheck_runner_input`, held
    by test_stored_input_is_rechecked.py), so that layer is reached only when
    the first is not there. This removes the first, so each test still proves
    what the second does on its own. `raising=False`: on a tree without the
    re-check there is nothing to remove, and the test asserts the same thing.
    """
    from agent_worker import lifecycle

    monkeypatch.setattr(
        lifecycle, "_recheck_runner_input", lambda *_args, **_kwargs: None, raising=False
    )


@pytest.fixture
def worker_factory(db, store, tmp_path, log_stream):
    def _factory(**kwargs: Any):
        return build_worker(db, store, tmp_path, log_stream, **kwargs)

    return _factory
