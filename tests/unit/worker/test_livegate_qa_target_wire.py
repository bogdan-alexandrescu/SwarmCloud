"""The live gate's entrypoint serves the QA target (VQ4-WIRE, part of #1933).

Lane VQ-4 (#1979) built the held sign-in in `livegate/qatarget.py` and the
proxy's half in `cdpproxy.py`, but merged with `__main__.py` unwired: a real
gate answered `qa_sign_in` with `failed` and own-login targets stayed signed
out. BGD decided on 2026-10-22: "Wire it + prompt notice". These tests drive
the real `LiveGate` through `test_livegate_main`'s rig over ctl.sock, the way
the worker does (docs/design/visual-qa-any-app.md §3-§4):

* a `qa_sign_in` reaches `qatarget.handle_ctl`, and after the wipe it is
  refused `wiped`;
* the gate's status carries `qa_target` and never the credential;
* a relaunched Chromium gets the target's rules again (`start_qa_target`).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import qatarget
from agent_worker.livegate.cdpproxy import CdpProxy
from agent_worker.livegate.ctl import CtlClient, CtlRefused

import test_live_browser_relaunch as relaunch_rigs
import test_livegate_main as rigs

ORIGIN = "https://app.example.com"
LOGIN_ORIGIN = "https://auth.example.com"


@pytest.fixture
def rigs_tmp() -> Any:
    """A short directory: a unix socket path is limited to ~108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="qt", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _target_id() -> str:
    return "qat_" + secrets.token_hex(8)


def _password() -> str:
    # Random, built at runtime: never one literal that looks like a credential.
    return "pw-" + secrets.token_hex(12)


def _args(*, own_login: bool, password: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "target_id": _target_id(), "base_url": f"{ORIGIN}/home", "origins": [ORIGIN, LOGIN_ORIGIN],
        "environment": "production", "rate": {"navigation_ms": 1000, "requests_per_s": 10},
        "sign_in": {"kind": "none"},
    }
    if own_login:
        args["sign_in"] = {"kind": "own_login", "login_url": f"{LOGIN_ORIGIN}/login",
                           "signed_in_selector": "#account"}
        args["credential"] = {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                              "password": password or _password()}
    return args


def test_a_qa_sign_in_reaches_the_targets_handler_and_is_refused_after_the_wipe(rigs_tmp, monkeypatch):
    seen: list[dict[str, Any]] = []
    real = qatarget.handle_ctl

    async def spy(target: Any, proxy: Any, args: Any) -> dict[str, Any]:
        seen.append({"target": target, "proxy": proxy, "args": dict(args)})
        return await real(target, proxy, args)

    monkeypatch.setattr(gate_main, "qa_target_ctl", spy, raising=False)

    async def body() -> None:
        rig = rigs.Rig(rigs_tmp)
        stop, task = await rigs._started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        args = _args(own_login=False)
        answer = await asyncio.to_thread(client.call, qatarget.OP, args)
        # MUTATION: drop `if op == QA_SIGN_IN_OP` from `LiveGate.handle_ctl`
        # -> the gate answers `failed` and this call raises.
        assert answer["qa_sign_in"] == "ready" and answer["target_id"] == args["target_id"]
        # MUTATION: call `self.qa_target.handle(args)` instead of
        # `qa_target_ctl(...)` in `LiveGate.qa_sign_in` -> nothing reaches the spy.
        assert len(seen) == 1 and seen[0]["target"] is rig.gate.qa_target
        # MUTATION: pass `None` as the proxy -> the target's guard never starts.
        assert seen[0]["proxy"] is rig.gate.proxy
        assert rig.gate.qa_target.armed

        polled = await asyncio.to_thread(client.call, qatarget.OP, {"poll": True})
        assert polled["qa_sign_in"] == "ready"

        wiped = await asyncio.to_thread(client.call, "end_control", {"cause": "attempt_end", "wipe": True})
        assert wiped == {"ended": True, "wiped": True}
        # MUTATION: drop `self.qa_target.wipe()` from `LiveGate.wipe` -> the
        # target still reads armed and unwiped.
        assert rig.gate.qa_target.wiped
        with pytest.raises(CtlRefused, match="wiped"):
            # MUTATION: drop the `if self.wiped` check in `LiveGate.qa_sign_in`
            # -> `QaTarget.handle` still refuses `wiped` (its own guard); drop
            # both -> this call is accepted and fails.
            await asyncio.to_thread(client.call, qatarget.OP, _args(own_login=False))
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_the_gates_status_carries_the_qa_target_and_never_its_credential(rigs_tmp):
    password = _password()

    async def body() -> None:
        rig = rigs.Rig(rigs_tmp)
        stop, task = await rigs._started(rig)
        client = CtlClient(rig.cfg.ctl_path)
        await asyncio.to_thread(client.register)
        # Armed directly, so a credential is held without a sign-in running
        # against the fake Chromium.
        args = _args(own_login=True, password=password)
        rig.gate.qa_target.handle(args)
        status = await asyncio.to_thread(client.call, "status", {})
        # MUTATION: drop `"qa_target": self.qa_target.status()` from
        # `LiveGate.status` -> KeyError.
        assert status["qa_target"]["qa_sign_in"] == "signing_in"
        assert status["qa_target"]["target_id"] == args["target_id"]
        text = json.dumps(status)
        # MUTATION: report `self.qa_target.credential()` in the status -> fails.
        assert password not in text and args["credential"]["username"] not in text
        assert "password" not in text and "credential" not in text
        client.close()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_the_session_carry_excludes_the_targets_origins(rigs_tmp):
    async def body() -> None:
        rig = rigs.Rig(rigs_tmp)
        stop, task = await rigs._started(rig)
        rig.gate.qa_target.handle(_args(own_login=False))
        # MUTATION: drop `excluded_origins=self.qa_target.excluded_origins`
        # from the SessionCarry -> the carry's default `tuple` gives ().
        assert tuple(rig.gate.carry._excluded_origins()) == (ORIGIN, LOGIN_ORIGIN)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_relaunched_chromium_gets_the_targets_rules_again(tmp_path, monkeypatch):
    started: list[Any] = []
    real = CdpProxy.start_qa_target

    async def spy(self: CdpProxy) -> None:
        started.append(self)
        await real(self)

    monkeypatch.setattr(CdpProxy, "start_qa_target", spy)

    async def body(rig: Any) -> None:
        rig.gate.qa_target.handle(_args(own_login=False))
        first = rig.gate.proxy
        # MUTATION: drop `qa_target=self.qa_target` from the proxy's
        # constructor -> the proxy holds no target.
        assert first._qa_target is rig.gate.qa_target
        await rig.crash("[1:1:ERROR:gpu_init.cc(1)] crashed")
        await relaunch_rigs._until(lambda: rig.gate.status()["browser"] == "running")
        assert rig.gate.proxy is not first
        # MUTATION: drop the `start_qa_target()` call from `_launch` -> the
        # relaunched proxy is never started.
        assert started and started[-1] is rig.gate.proxy
        assert rig.gate.proxy._qa_target is rig.gate.qa_target

    relaunch_rigs._run(body, tmp_path)


