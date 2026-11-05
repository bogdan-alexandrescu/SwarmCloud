"""The worker's two JC2 hooks (part of #2688; jev-contract.md section 5).

The runner's end-of-step judgment (`cliagent._contract_post_step`) reads the
step's signed work contract from two variables the worker sets, and its
verdict reaches swarm-api's `state/contract-verdict` only through the worker,
after the publish. One test per property:

1. the runner is started with SWARM_WORK_CONTRACT_FILE, naming a file in the
   attempt's work/ that holds `metadata.work_contract`, and
   SWARM_WORK_CONTRACT_DIGEST, the signed `metadata.dispatch.work_contract
   .digest`; a step with no signed reference gets neither;
2. after the publish the runner report's `contract_judgment` is posted once,
   about the pushed head, with a request id of its own;
3. a stale generation (409 `attempt_fenced`) stands down: no further write,
   no terminal state, the exit is GENERATION_FENCED;
4. a send-back ends the attempt where the route ended it (READY);
5. the switch (`workcontract.JC2_AT_END_OF_STEP`) is on.

MUTATIONS: drop `base.update(self._work_contract_env)` from `_build_child_env`
(test 1); drop the `_deliver_contract_verdict` call from `_record_finalised`
(test 2); catch the FencedError without `_stand_down` (test 3); drop the
`sent_back` branch (test 4); turn the switch off (test 5).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from agent_worker.errors import ExitCode, FencedWriteRefused
from agent_worker.judge import poststeps
from swarm_api import workcontract as wc
from swarm_common.states import TaskState

from worker_seeds import seed_attempt

CONTRACT = {"contract_id": "wc-hooks", "revision": 1, "requirements": [
    {"id": "REQ-1", "text": "Clear copy.", "verification": ["judgment"]}]}


def _digest() -> str:
    return secrets.token_hex(32)


def _head() -> str:
    return secrets.token_hex(20)


def _seed(db: Any, metadata: dict[str, Any]) -> None:
    seed_attempt(db, task_input={"prompt": "do the work", "steps": 1, "sleep_seconds": 0.01})
    db.doc("tasks/task_1")["metadata"] = metadata


def _runner_envs(worker: Any) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []
    build = worker._build_child_env

    def recorded() -> dict[str, str]:
        env = build()
        seen.append(dict(env))
        return env

    worker._build_child_env = recorded  # type: ignore[method-assign]
    return seen


# ---------------------------------------------------------------------------
# 1. the runner gets both variables
# ---------------------------------------------------------------------------


def test_the_runner_is_started_with_the_signed_contract_and_its_digest(db, worker_factory):
    digest = _digest()
    _seed(db, {"dispatch": {"work_contract": {"digest": digest}}, "work_contract": CONTRACT})
    worker, _, _ = worker_factory()
    seen = _runner_envs(worker)

    worker.run()

    assert seen, "the runner was never started"
    env = seen[0]
    assert env[poststeps.CONTRACT_DIGEST_ENV] == digest
    path = Path(env[poststeps.CONTRACT_FILE_ENV])
    assert path.parent == worker.ws.work
    assert json.loads(path.read_text()) == CONTRACT
    assert poststeps.read_contract(env) == (CONTRACT, digest)


def test_a_step_without_a_signed_reference_gets_neither_variable(db, worker_factory):
    # A body with no signed reference names nothing (SWARM_WORK_CONTRACTS off).
    _seed(db, {"dispatch": {}, "work_contract": CONTRACT})
    worker, _, _ = worker_factory()
    seen = _runner_envs(worker)

    worker.run()

    assert seen
    assert poststeps.CONTRACT_DIGEST_ENV not in seen[0]
    assert poststeps.CONTRACT_FILE_ENV not in seen[0]
    assert poststeps.read_contract(seen[0]) == (None, None)


def test_a_signed_reference_without_its_body_still_names_the_digest(db, worker_factory):
    # JC2 then posts `not_run` (contract_unreadable), never nothing.
    digest = _digest()
    _seed(db, {"dispatch": {"work_contract": {"digest": digest}}})
    worker, _, _ = worker_factory()
    seen = _runner_envs(worker)

    worker.run()

    assert seen[0][poststeps.CONTRACT_DIGEST_ENV] == digest
    assert poststeps.CONTRACT_FILE_ENV not in seen[0]


# ---------------------------------------------------------------------------
# 2-4. the verdict after the publish
# ---------------------------------------------------------------------------


class _Posts:
    """`RemoteControlPlane._post`, recorded; `answer` or `raises` per call."""

    def __init__(self, answer: dict[str, Any] | None = None,
                 raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.answer = answer or {"ok": True, "outcome": "met", "waiting_on": None,
                                 "sent_back": False}
        self.raises = raises

    def __call__(self, op: str, fields: Any, *, call: Any) -> dict[str, Any]:
        self.calls.append((op, dict(fields), call))
        if self.raises is not None:
            raise self.raises
        return dict(self.answer)


def _verdict(digest: str, commit: str) -> dict[str, Any]:
    return {"digest": digest, "commit": commit, "base": None, "outcome": "judged",
            "reason": None, "call_id": "call_1", "state_sha256": "c" * 64,
            "changed": ["a.py"], "mechanical": [],
            "verdicts": [{"requirement_id": "REQ-1", "verdict": "met"}]}


def _published(head: str) -> dict[str, Any]:
    return {"git": {"published": True, "branch": "swarm/x", "pushed_head": head}}


def _remote(worker: Any, monkeypatch: Any, posts: _Posts) -> None:
    monkeypatch.setattr(worker.control, "state_via_api", True, raising=False)
    monkeypatch.setattr(worker.control, "_post", posts, raising=False)


def test_the_verdict_is_posted_after_a_publish_about_the_pushed_head(worker_factory,
                                                                     monkeypatch):
    worker, _, _ = worker_factory()
    posts = _Posts()
    _remote(worker, monkeypatch, posts)
    digest, head = _digest(), _head()
    summary = _published(head)

    out = worker._deliver_contract_verdict(
        summary, {"output": {"contract_judgment": _verdict(digest, head)}})

    assert out is None
    ((op, fields, _call),) = posts.calls
    assert op == poststeps.VERDICT_OP == "contract-verdict"
    assert fields["commit"] == head and fields["digest"] == digest
    assert fields["outcome"] == "judged"
    assert isinstance(fields["request_id"], str) and fields["request_id"]
    assert summary["contract_verdict"]["delivery"] == "posted"


def test_a_verdict_on_another_commit_is_posted_not_run_about_the_pushed_head(
        worker_factory, monkeypatch):
    worker, _, _ = worker_factory()
    posts = _Posts()
    _remote(worker, monkeypatch, posts)
    head = _head()

    worker._deliver_contract_verdict(
        _published(head), {"output": {"contract_judgment": _verdict(_digest(), _head())}})

    ((_op, fields, _call),) = posts.calls
    assert fields["commit"] == head
    assert fields["outcome"] == "not_run" and fields["reason"] == "commit_moved"


def test_each_post_carries_a_request_id_of_its_own(worker_factory, monkeypatch):
    worker, _, _ = worker_factory()
    posts = _Posts()
    _remote(worker, monkeypatch, posts)
    head = _head()
    report = {"output": {"contract_judgment": _verdict(_digest(), head)}}

    worker._deliver_contract_verdict(_published(head), report)
    worker._deliver_contract_verdict(_published(head), report)

    first, second = (fields["request_id"] for _op, fields, _call in posts.calls)
    assert first != second


def test_nothing_is_posted_without_a_judgment_or_without_a_publish(worker_factory, monkeypatch):
    worker, _, _ = worker_factory()
    posts = _Posts()
    _remote(worker, monkeypatch, posts)
    head = _head()

    assert worker._deliver_contract_verdict(_published(head), {"output": {}}) is None
    unpublished = {"git": {"published": False, "pushed_head": head}}
    assert worker._deliver_contract_verdict(
        unpublished, {"output": {"contract_judgment": _verdict(_digest(), head)}}) is None
    assert unpublished["contract_verdict"]["delivery"] == "not_published"
    assert posts.calls == []


def test_a_stale_generation_stands_down_without_posting_anything_else(db, worker_factory,
                                                                      monkeypatch):
    seed_attempt(db, task_input={"prompt": "do the work", "steps": 1, "sleep_seconds": 0.01})
    worker, _, _ = worker_factory()
    posts = _Posts(raises=FencedWriteRefused(1, -1, "swarm-api: attempt_fenced",
                                             write="contract-verdict"))
    _remote(worker, monkeypatch, posts)
    terminal: list[Any] = []
    monkeypatch.setattr(worker, "_finish_terminal", lambda **kw: terminal.append(kw))
    head = _head()
    before = dict(db.doc("tasks/task_1"))

    out = worker._deliver_contract_verdict(
        _published(head), {"output": {"contract_judgment": _verdict(_digest(), head)}})

    assert out is not None and out.exit_code == ExitCode.GENERATION_FENCED
    assert out.detail == {"fenced": True}
    assert len(posts.calls) == 1, "nothing is posted after the fence"
    assert terminal == []
    task = db.doc("tasks/task_1")
    assert task["state"] == before["state"]
    assert task.get("current_lease_id") == before.get("current_lease_id")


def test_a_send_back_ends_the_attempt_where_the_route_ended_it(worker_factory, monkeypatch):
    worker, _, _ = worker_factory()
    posts = _Posts(answer={"ok": True, "outcome": "sent_back", "waiting_on": None,
                           "sent_back": True})
    _remote(worker, monkeypatch, posts)
    head = _head()

    out = worker._deliver_contract_verdict(
        _published(head), {"output": {"contract_judgment": _verdict(_digest(), head)}})

    assert out is not None
    assert out.state == TaskState.READY and out.exit_code == ExitCode.FAILED


def test_the_direct_path_records_that_it_had_no_route(worker_factory):
    worker, _, _ = worker_factory()
    head = _head()
    summary = _published(head)

    out = worker._deliver_contract_verdict(
        summary, {"output": {"contract_judgment": _verdict(_digest(), head)}})

    assert out is None
    assert summary["contract_verdict"] == {"delivery": "no_state_route"}


def test_the_success_path_delivers_after_the_published_mark():
    """Wired between `_mark_publish` (the mark the route checks the commit
    against) and the success."""
    import inspect

    from agent_worker import lifecycle

    source = inspect.getsource(lifecycle.Worker._record_finalised)
    mark = source.index("self._mark_publish(summary)\n")
    deliver = source.index("self._deliver_contract_verdict(summary, runner_result)")
    success = source.index("state=TaskState.SUCCEEDED")
    assert mark < deliver < success


# ---------------------------------------------------------------------------
# 5. the switch
# ---------------------------------------------------------------------------


def test_the_step_end_judgment_switch_is_on():
    assert wc.JC2_AT_END_OF_STEP is True
