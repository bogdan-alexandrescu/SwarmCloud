"""The state reads, wired into the worker (docs/design/agent-autonomy.md §3.4;
lane AU4-WIRE, part of #1933).

The relay (`statemcp.relay.StateReads`) lives inside `ChildPath`, beside the
git tools' and the search tool's, so the lifecycle's existing `prepare`,
`has_requests` and `tick` carry it and its fenced exit is theirs. What this
file holds:

  * a key `state/open` registered for the state purpose gets `$SWARM_STATE`
    and the relay, which signs under `swarm-state-req/v1`;
  * a key registered at `/child-key` (child purpose only) gets them too, and
    the relay signs under `swarm-child-req/v1` (BGD, 2026-10-22 08:54Z);
  * an attempt with no key gets neither, and says `no_attempt_key`;
  * a fenced read raises the error the lifecycle exits on (invariant 5) and
    sends nothing but the read: no lease call, no release.

Only swarm-api is faked, at the `ChildApi.call` boundary, with
`swarm_api.childkey`'s own verifier.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_worker import children as children_mod
from agent_worker.gitmcp import server as git_shim
from agent_worker.statemcp import server as shim
from swarm_api import childkey as api_childkey

from worker_seeds import TENANT


class FakeRoutes:
    """swarm-api's worker-only routes: the key registration and the reads."""

    def __init__(self, *, accept: tuple[str, ...] = ("state", "child")) -> None:
        self.accept = accept
        self.calls: list[dict[str, Any]] = []
        self.public_key: str | None = None
        self.fence = False

    def call(self, method, path, *, body=None, headers=None):
        headers = dict(headers or {})
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers})
        if path.endswith("/child-key"):
            self.public_key = json.loads(body).get("public_key")
            return 201, {"registered": "key" if self.public_key else "tombstone"}
        if "/reads/" not in path:
            raise AssertionError(f"unexpected route {method} {path}")
        if self.fence:
            raise children_mod.ChildApiError(409, "attempt_fenced", "superseded",
                                             retryable=False)
        proof = headers.get(children_mod.PROOF_HEADER) or ""
        stamp = headers.get(children_mod.TIMESTAMP_HEADER) or ""
        messages = {"state": api_childkey.state_request_message(method, path, b"", stamp),
                    "child": api_childkey.request_message(method, path, b"", stamp)}
        verified = [purpose for purpose, message in messages.items()
                    if self.public_key and api_childkey.verify_proof(self.public_key, proof,
                                                                     message)]
        if not set(verified) & set(self.accept):
            raise children_mod.ChildApiError(403, "attempt_unproven", "", retryable=False)
        return 200, {"ok": True, "tool": path.split("/reads/")[1].split("?")[0],
                     "result": {"verified_as": verified}}

    def reads(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "/reads/" in c["path"]]


class _Log:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, msg: str, **k: Any) -> None:
        self.lines.append((msg, k))

    warning = error = info


def _cfg(nonce: str) -> SimpleNamespace:
    return SimpleNamespace(
        child_submit_retry_seconds=5, max_child_requests_per_tick=4,
        max_child_request_bytes=262144, tenant_id=TENANT, attempt_id="att_1",
        lease_id="lease_1", generation=1, task_id="task_1", swarm_api_url=None,
        swarm_api_audience=None, child_nonce=nonce, runner_profile="claude-code",
        git_branch_prefix="swarm/", git_author_name="swarmcloud agent",
        git_author_email="swarmcloud-agent@users.noreply.github.com",
        git_clone_timeout_seconds=60,
    )


def _protected():
    from agent_worker.hardening import PROTECTED, MemoryProtection

    return MemoryProtection(PROTECTED, "test")


def child_keyed(routes: FakeRoutes) -> children_mod.ChildPath:
    """Registered at `/child-key`: the child purpose only."""
    path = children_mod.ChildPath(_cfg("nonce-" + secrets.token_hex(4)), logger=_Log(),
                                  memory=_protected(), api=routes)
    path.register(lambda _note: None)
    return path


def state_keyed(routes: FakeRoutes, *, purposes=("child", "state"),
                unprotected: bool = False) -> children_mod.ChildPath:
    """Registered by `state/open`, adopted (`SWARM_STATE_VIA_API`)."""
    path = children_mod.ChildPath(_cfg("nonce-" + secrets.token_hex(4)), logger=_Log(),
                                  memory=_protected(), api=routes)
    key = children_mod.AttemptKey()
    routes.public_key = key.public
    path.adopt(key, {"registered": "key", "purpose": list(purposes),
                     "unprotected": unprotected}, lambda _note: None)
    return path


def unkeyed(routes: FakeRoutes) -> children_mod.ChildPath:
    path = children_mod.ChildPath(_cfg(""), logger=_Log(), memory=_protected(), api=routes)
    path.register(lambda _note: None)
    return path


def _prepare(path, tmp_path: Path) -> tuple[dict[str, str], Path]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return path.prepare(work, task={"id": "task_1"}), work


def _read_once(path, env, work) -> dict[str, Any]:
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "state-1-abc", "agents_list", {"state": "FAILED"})
    # MUTATION: dropping the relay from `ChildPath.has_requests` turns this red.
    assert path.has_requests(work)
    # MUTATION: dropping the relay's tick from `ChildPath.tick` turns this red.
    assert path.tick(work) == 1
    return json.loads((spool / "responses" / "state-1-abc.json").read_text())


def test_a_state_purpose_key_gets_the_spool_and_signs_state(tmp_path):
    routes = FakeRoutes(accept=("state",))
    path = state_keyed(routes)
    env, work = _prepare(path, tmp_path)
    # MUTATION: not composing `_prepare_state_reads` into `prepare` turns this red.
    assert env[shim.SPOOL_ENV] == str(work / shim.SPOOL_DIR_NAME)
    assert path.state_reads_unavailable is None and path.state_signed is True
    answer = _read_once(path, env, work)
    # MUTATION: a relay that signs a state-purpose key's reads under the child
    # purpose is refused by a state-only route and turns this red.
    assert answer["result"]["verified_as"] == ["state"], answer
    (call,) = routes.reads()
    assert call["method"] == "GET" and call["body"] is None


def test_a_child_purpose_key_gets_the_spool_and_signs_child(tmp_path):
    routes = FakeRoutes(accept=("child",))
    path = child_keyed(routes)
    assert path.keyed
    env, work = _prepare(path, tmp_path)
    # MUTATION: offering the reads only to a state-signed key (AU-4's first
    # patch, `no_state_key`) turns this red.
    assert env[shim.SPOOL_ENV] == str(work / shim.SPOOL_DIR_NAME)
    assert path.state_reads_unavailable is None and path.state_signed is False
    answer = _read_once(path, env, work)
    # MUTATION: a relay that signs a child-purpose key's reads under
    # `swarm-state-req/v1`, which swarm-api refuses for such a key, turns this red.
    assert answer["result"]["verified_as"] == ["child"], answer
    (call,) = routes.reads()
    path_only, _, query = call["path"].partition("?")
    assert path_only == "/v1/attempts/att_1/reads/agents_list"
    # MUTATION: a relay built from anything but the worker's own tuple turns this red.
    assert query.startswith(f"tenant_id={TENANT}&task_id=task_1&lease_id=lease_1&generation=1&")


@pytest.mark.parametrize("make", [unkeyed, lambda r: state_keyed(r, purposes=("state",),
                                                                 unprotected=True)],
                         ids=["no_nonce", "unprotected_heap"])
def test_an_attempt_without_a_usable_key_gets_no_spool(tmp_path, make):
    routes = FakeRoutes()
    path = make(routes)
    env, work = _prepare(path, tmp_path)
    # MUTATION: offering the reads without a key the worker holds turns this red.
    assert shim.SPOOL_ENV not in env
    assert not (work / shim.SPOOL_DIR_NAME).exists()
    assert path.state_reads_unavailable == "no_attempt_key"
    assert not path.has_requests(work) and routes.reads() == []


def test_preparing_again_keeps_the_relay(tmp_path):
    routes = FakeRoutes()
    path = child_keyed(routes)
    _prepare(path, tmp_path)
    relay = path._state_reads
    env, _ = _prepare(path, tmp_path)
    # MUTATION: rebuilding the relay on every `prepare` (losing its hourly budget) turns this red.
    assert path._state_reads is relay and shim.SPOOL_ENV in env


@pytest.mark.parametrize("make", [child_keyed, state_keyed], ids=["child", "state"])
def test_a_fenced_read_exits_through_the_fence_without_touching_the_lease(tmp_path, make):
    routes = FakeRoutes()
    path = make(routes)
    env, work = _prepare(path, tmp_path)
    routes.fence = True
    before = len(routes.calls)
    spool = Path(env[shim.SPOOL_ENV])
    git_shim.write_request(spool, "state-2-abc", "agent_view", {"agent_id": "task_failed"})
    # MUTATION: answering the 409 as an ordinary refusal, or catching the
    # fence inside `ChildPath.tick`, turns this red: the lifecycle's fenced
    # exit (`Worker._exit_fenced_mid_run`) is reached only through this error.
    with pytest.raises(children_mod.ChildSubmitFenced):
        path.tick(work)
    answer = json.loads((spool / "responses" / "state-2-abc.json").read_text())
    assert answer["refused"]["code"] == "fenced"
    sent = routes.calls[before:]
    # MUTATION: a fenced read that releases, heartbeats or writes anything turns this red.
    assert [(c["method"], "/reads/" in c["path"]) for c in sent] == [("GET", True)]
