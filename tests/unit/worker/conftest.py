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


# The four variables git reads for a commit's identity. Git puts each above
# `-c user.name=... -c user.email=...`, so where the shell exports them -- every
# SwarmCloud container does -- a test's own identity never reached its commits.
GIT_IDENTITY_VARIABLES = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)


@pytest.fixture(autouse=True)
def _commits_carry_the_tests_identity(monkeypatch):
    """No worker test inherits a git identity from its environment (HERMETIC-GIT-ENV, part of #1822).

    The push check refuses any commit the worker did not write, and the tests
    of it commit as a named identity with `git -c user.name=... -c
    user.email=...`. Inside a SwarmCloud container the exported GIT_AUTHOR_*
    and GIT_COMMITTER_* outranked that, so six tests in test_harvest.py and
    test_publish_as_the_person.py saw the container's author and failed there
    while passing in CI, which exports none. A test that needs an identity in
    the environment sets its own (`env=` or `monkeypatch.setenv`), after this
    has run. test_git_identity_is_hermetic.py holds it.
    """
    for name in GIT_IDENTITY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_agent_tool_spools_from_the_environment(monkeypatch):
    """No worker test inherits an agent tool's spool from its environment.

    The claude-code runner registers the git, search and state MCP servers
    whenever their spool variables (`SWARM_GIT`, `SWARM_SEARCH`,
    `SWARM_STATE`: each server's `SPOOL_ENV`) are set, and a SwarmCloud
    container exports them for its own agent. There, 17 tests that compare the
    runner's MCP servers or argv exactly (test_browser_tool_status.py,
    test_runners.py, test_search_tool_wiring.py,
    test_runner_graph_shim_config.py) saw extra servers and failed, on main
    too, while passing in CI, which exports none (measured 2026-10-22,
    VQ4-WIRE). A test of one of those tools sets its own, after this.
    """
    from agent_worker.gitmcp.server import SPOOL_ENV as GIT_SPOOL_ENV
    from agent_worker.searchmcp.server import SPOOL_ENV as SEARCH_SPOOL_ENV
    from agent_worker.statemcp.server import SPOOL_ENV as STATE_SPOOL_ENV

    for name in (GIT_SPOOL_ENV, SEARCH_SPOOL_ENV, STATE_SPOOL_ENV):
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture(autouse=True)
def _the_relay_name_resolves(monkeypatch):
    """No unit test asks a real resolver for the live relay's name (LB-DNS, part of #1366).

    `HandoffPath`'s first tunnel open asks the pod's resolver for the relay's
    address when the gate refuses it, and retries with real sleeps while the
    name does not resolve. Outside a cluster it never resolves, so without
    this every test whose gate refuses `tunnel_ticket` would retry for 30 s
    of wall time. A test of the retry passes `resolve=` to `HandoffPath`.
    """
    from agent_worker import handoff

    monkeypatch.setattr(handoff, "_resolve_relay", lambda host, port: [("stand-in", host, port)])


@pytest.fixture(autouse=True)
def _the_cdp_endpoint_answers(monkeypatch):
    """No unit test asks a real CDP proxy whether it answers (BROWSER-START, part of #1366).

    `HandoffPath.setup` probes SWARM_CDP_ENDPOINT's `/json/version` and waits,
    with real sleeps, for up to 20 s while it does not answer. Outside a pod
    nothing listens there. A test of the wait sets its own stand-in.
    """
    from agent_worker import handoff

    monkeypatch.setattr(handoff, "_probe_cdp", lambda endpoint, timeout: True, raising=False)
