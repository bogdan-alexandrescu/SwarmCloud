"""The live browser's proxied egress, the gate's half (EGRESS-GATE; part of #1030).

docs/browser-egress.md §2.2-§2.5 and the owner's decisions of 2026-10-13: the
credential lives only in swarm-egress, and the browser FAILS CLOSED. These
tests hold:

* the launch: with egress off Chromium's arguments are exactly what they were;
  with it on they carry `--proxy-server` at the gate's loopback forwarder and
  the design's bypass list, and with no listening forwarder there is no way to
  render them at all;
* the ticket: the forwarder presents the worker's ticket to swarm-egress as a
  bearer proxy authorization on every CONNECT, and nothing else;
* fail closed: with no ticket, a failed mint, an expired ticket or swarm-egress
  down, every CONNECT is refused and nothing is dialled but swarm-egress; a
  forwarder that cannot listen means Chromium is never started;
* the agent's uid cannot use the forwarder;
* no ticket or CONNECT host appears in a record or a log line.

Offline: the "swarm-egress" here is a loopback server that records what it
was sent. Every ticket is built at run time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sys
import urllib.parse
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import __main__ as gate_main
from agent_worker.livegate import ctl as ctl_mod
from agent_worker.livegate import egress
from agent_worker.livegate.ctl import CtlClient, CtlRefused

from test_livegate_main import Rig, _started, _until, short_tmp  # noqa: F401, F811 - the fixture

REPO = Path(__file__).resolve().parents[3]
TARGET = "www.example-search.test:443"
PROXY_ENV = {egress.EGRESS_ENV: "proxy"}


def _ticket() -> str:
    # Shaped like swarm-api's (payload.mac, base64url), built at run time.
    return secrets.token_urlsafe(48) + "." + secrets.token_urlsafe(32)


class Records(list):
    def __call__(self, event: str, fields: Any) -> None:
        self.append((event, {str(k): str(v) for k, v in dict(fields).items()}))

    def events(self) -> list[str]:
        return [e for e, _ in self]


# --------------------------------------------------------------------------
# Configuration and Chromium's arguments
# --------------------------------------------------------------------------


def test_egress_is_off_unless_the_sidecar_says_proxy():
    assert egress.EgressSettings.from_env({}).proxy is False
    assert egress.EgressSettings.from_env({egress.EGRESS_ENV: ""}).proxy is False
    assert egress.EgressSettings.from_env({egress.EGRESS_ENV: "direct"}).proxy is False
    on = egress.EgressSettings.from_env(PROXY_ENV)
    assert on.proxy is True
    assert (on.forwarder_host, on.forwarder_port) == ("swarm-egress.swarm-system.svc.cluster.local", 3128)


def test_a_value_nobody_meant_is_a_fatal_setup_error_not_direct():
    with pytest.raises(egress.EgressConfigError):
        egress.EgressSettings.from_env({egress.EGRESS_ENV: "prxy"})
    with pytest.raises(egress.EgressConfigError):
        egress.EgressSettings.from_env({**PROXY_ENV, egress.FORWARDER_ENV: "no-port"})
    with pytest.raises(egress.EgressConfigError):
        egress.EgressSettings.from_env({**PROXY_ENV, egress.BYPASS_ENV: "bad host!"})


def test_the_gate_reads_egress_from_its_environment(tmp_path):
    env = {"SWARM_GATE_CTL": "/run/swarm-gate/ctl.sock", "SWARM_BROWSER_PROFILE_DIR": "/p",
           "SWARM_BROWSER_DOWNLOADS": "/d", "SWARM_BROWSER_UPLOADS": "/u",
           "SWARM_CDP_LISTEN": "127.0.0.1:9222"}
    assert gate_main.GateConfig.from_env(env).egress.proxy is False
    assert gate_main.GateConfig.from_env({**env, **PROXY_ENV}).egress.proxy is True
    with pytest.raises(gate_main.SetupError):
        gate_main.GateConfig.from_env({**env, egress.EGRESS_ENV: "sometimes"})


def _cfg(tmp: Path, **changes: Any) -> gate_main.GateConfig:
    from dataclasses import replace

    from test_livegate_main import _config

    return replace(_config(tmp), **changes)


def test_with_egress_off_chromiums_arguments_carry_no_proxy(tmp_path):
    args = gate_main.chromium_args(_cfg(tmp_path))
    assert not [a for a in args if "proxy" in a or "webrtc" in a]
    assert args[-1] == "about:blank"


def test_with_egress_on_chromium_is_pointed_at_the_loopback_forwarder(tmp_path):
    settings = egress.EgressSettings.from_env({**PROXY_ENV, egress.BYPASS_ENV: "console.example.test"})
    args = gate_main.chromium_args(_cfg(tmp_path, egress=settings, egress_port=40123))
    assert "--proxy-server=http://127.0.0.1:40123" in args
    (bypass,) = [a for a in args if a.startswith("--proxy-bypass-list=")]
    assert bypass == ("--proxy-bypass-list=<local>;github.com;*.github.com;*.githubusercontent.com;"
                      "console.example.test")
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert args[-1] == "about:blank", "the proxy flags come before the first page"


def test_with_egress_on_and_no_forwarder_there_are_no_arguments_to_launch_with(tmp_path):
    settings = egress.EgressSettings.from_env(PROXY_ENV)
    with pytest.raises(gate_main.SetupError):
        gate_main.chromium_args(_cfg(tmp_path, egress=settings))
    with pytest.raises(egress.EgressConfigError):
        egress.chromium_proxy_args(settings, None)


def _egress_policy() -> Any:
    path = REPO / "apps" / "swarm-egress" / "swarm_egress" / "policy.py"
    spec = spec_from_file_location("_swarm_egress_policy_for_the_gate", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_bypass_list_is_rendered_as_swarm_egress_renders_it():
    policy = _egress_policy()
    settings = egress.EgressSettings.from_env({**PROXY_ENV, egress.BYPASS_ENV: "a.example.test;.b.example.test"})
    assert settings.bypass_list() == policy.chromium_bypass_list(settings.bypass_hosts)
    # Every bypassed host is one swarm-egress refuses as `bypass`: a CONNECT for
    # it reaching swarm-egress means this list and terraform's disagree.
    for host in ("github.com", "api.github.com", "raw.githubusercontent.com", "a.example.test"):
        with pytest.raises(policy.TargetRefused) as refused:
            policy.check_target(f"{host}:443", bypass_hosts=settings.bypass_hosts, allowed_ports={443})
        assert refused.value.cause == "bypass"


def test_the_default_bypass_hosts_are_terraforms():
    tf = (REPO / "terraform" / "infra" / "egress.tf").read_text()
    match = re.search(r"egress_bypass_hosts\s*=\s*distinct\(concat\(\s*\[([^\]]*)\]", tf)
    assert match, "egress.tf no longer renders egress_bypass_hosts as this test reads it"
    assert tuple(json.loads(f"[{match.group(1)}]")) == egress.DEFAULT_BYPASS


def test_the_default_forwarder_is_swarm_egress_service_and_port():
    manifest = (REPO / "kubernetes" / "swarm-system" / "swarm-egress.yaml").read_text()
    assert "kind: Service" in manifest and "name: swarm-egress" in manifest
    main = (REPO / "apps" / "swarm-egress" / "swarm_egress" / "main.py").read_text()
    port = int(re.search(r"^PORT = (\d+)", main, re.M).group(1))
    assert egress.DEFAULT_FORWARDER == f"swarm-egress.swarm-system.svc.cluster.local:{port}"
    netpol = (REPO / "kubernetes" / "network-policies" / "allow-egress-proxy.yaml").read_text()
    assert f"port: {port}" in netpol, "a labelled browser pod may reach swarm-egress on its port"


# --------------------------------------------------------------------------
# The peer's uid, from /proc/net/tcp
# --------------------------------------------------------------------------


def test_the_peer_socket_owner_is_read_from_the_namespaces_table():
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    # 127.0.0.1:51000 -> 127.0.0.1:40123, owned by uid 1001 (the agent), and
    # the server's accepted side, owned by 1002 (the gate).
    rows = [
        "   0: 0100007F:C738 0100007F:9CBB 01 00000000:00000000 00:00000000 00000000  1001        0 1 1\n",
        "   1: 0100007F:9CBB 0100007F:C738 01 00000000:00000000 00:00000000 00000000  1002        0 2 1\n",
    ]
    tables = {"/proc/net/tcp": header + "".join(rows), "/proc/net/tcp6": header}
    owner = egress.socket_owner(("127.0.0.1", 51000), ("127.0.0.1", 40123),
                                read=lambda path: tables[path])
    assert owner == 1001
    assert egress.socket_owner(("127.0.0.1", 51001), ("127.0.0.1", 40123),
                               read=lambda path: tables[path]) is None


def test_a_real_loopback_peer_is_this_process_uid():
    import os

    async def body() -> int | None:
        seen: list[int | None] = []

        async def serve(reader, writer):
            peer = writer.get_extra_info("peername")
            local = writer.get_extra_info("sockname")
            seen.append(egress.socket_owner((peer[0], peer[1]), (local[0], local[1])))
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        await _until(lambda: seen)
        writer.close()
        server.close()
        return seen[0]

    if not Path("/proc/net/tcp").exists():
        pytest.skip("no /proc/net/tcp here")
    assert asyncio.run(body()) == os.getuid()


# --------------------------------------------------------------------------
# The forwarder, against a recording "swarm-egress"
# --------------------------------------------------------------------------


class Upstream:
    """swarm-egress as the forwarder sees it: records each CONNECT head, answers
    `status` (with `cause` in X-Swarm-Egress-Refused), then echoes."""

    def __init__(self, status: int = 200, cause: str = "") -> None:
        self.status = status
        self.cause = cause
        self.heads: list[str] = []
        self.accepted = 0
        self.server: Any = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer) -> None:
        self.accepted += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.heads.append(head.decode("latin-1"))
        if self.status != 200:
            writer.write(f"HTTP/1.1 {self.status} No\r\nX-Swarm-Egress-Refused: {self.cause}\r\n"
                         "Content-Length: 0\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        try:
            while data := await reader.read(65536):
                writer.write(data.upper())
                await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()


class World:
    def __init__(self, upstream: Upstream | None, *, uid: int = 1000, peer_uid: int | None = 1000,
                 clock: list[float] | None = None) -> None:
        self.upstream = upstream
        self.records = Records()
        self.unavailable: list[str] = []
        self.dialled: list[tuple[str, int]] = []
        self.clock = clock or [1_000_000.0]
        self.settings = egress.EgressSettings.from_env(PROXY_ENV)
        self.forwarder = egress.LocalForwarder(
            self.settings, record=self.records, uid=uid,
            peer_uid=lambda peer, local: peer_uid, dial=self._dial,
            clock=lambda: self.clock[0], on_unavailable=self.unavailable.append)

    async def _dial(self, host: str, port: int):
        self.dialled.append((host, port))
        if self.upstream is None:
            raise ConnectionRefusedError("swarm-egress is down")
        return await asyncio.open_connection("127.0.0.1", self.upstream.port)

    async def connect(self, head: str | None = None) -> tuple[int, Any, Any]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.forwarder.port)
        writer.write((head or f"CONNECT {TARGET} HTTP/1.1\r\nHost: {TARGET}\r\n"
                      "Proxy-Connection: keep-alive\r\n\r\n").encode())
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return int(answer.split(b" ", 2)[1]), reader, writer


def run(scenario, upstream: Upstream | None = None, **kw: Any) -> World:
    world = World(upstream, **kw)

    async def main() -> None:
        if upstream is not None:
            await upstream.start()
        await world.forwarder.start()
        try:
            await asyncio.wait_for(scenario(world), 20)
        finally:
            await world.forwarder.close()
            if upstream is not None:
                await upstream.stop()

    asyncio.run(main())
    return world


def test_with_no_ticket_every_connect_is_refused_and_nothing_is_dialled():
    async def scenario(world: World) -> None:
        status, _, writer = await world.connect()
        assert status == 503
        writer.close()

    world = run(scenario, Upstream())
    assert world.dialled == [] and world.upstream.accepted == 0
    assert ("egress_refused", {"cause": "no_ticket"}) in world.records


def test_the_ticket_is_presented_to_swarm_egress_as_a_bearer_on_every_connect():
    ticket = _ticket()

    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(ticket, 600)
        for _ in range(2):
            status, reader, writer = await world.connect()
            assert status == 200
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"HELLO"
            writer.close()
        await _until(lambda: world.forwarder.bytes_down == 10)
        stats.update(world.forwarder.stats())

    stats: dict[str, Any] = {}
    world = run(scenario, Upstream())
    assert world.dialled == [("swarm-egress.swarm-system.svc.cluster.local", 3128)] * 2
    for head in world.upstream.heads:
        lines = head.split("\r\n")
        assert lines[0] == f"CONNECT {TARGET} HTTP/1.1"
        assert f"Proxy-Authorization: Bearer {ticket}" in lines
        # Chromium's own headers are not passed on.
        assert not any(line.lower().startswith("proxy-connection") for line in lines)
    assert stats["state"] == "ready" and stats["connects"] == 2
    assert stats["bytes_up"] == 10 and stats["bytes_down"] == 10
    assert ticket not in json.dumps(stats)


def test_a_failed_mint_refuses_every_connect_and_says_why():
    async def scenario(world: World) -> None:
        world.forwarder.fail("egress_off")
        status, _, writer = await world.connect()
        assert status == 503
        writer.close()

    world = run(scenario, Upstream())
    assert world.dialled == []
    assert world.unavailable == ["egress_off"]
    assert world.forwarder.stats()["state"] == "failed"
    assert ("egress_failed", {"cause": "egress_off"}) in world.records


def test_an_expired_ticket_is_refused_until_the_worker_renews_it():
    ticket = _ticket()

    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(ticket, 60)
        world.clock[0] += 61
        status, _, writer = await world.connect()
        assert status == 503
        writer.close()
        assert world.forwarder.stats()["state"] == "expired"
        world.forwarder.set_ticket(_ticket(), 600)
        status, _, writer = await world.connect()
        assert status == 200
        writer.close()

    world = run(scenario, Upstream())
    assert len(world.dialled) == 1, "the expired ticket was never presented"


def test_swarm_egress_down_is_refused_and_never_dials_the_site():
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        assert await world.forwarder.probe() is False
        status, _, writer = await world.connect()
        assert status == 502
        writer.close()

    world = run(scenario, None)
    # The only address ever dialled is swarm-egress's: never the CONNECT target.
    assert set(world.dialled) == {("swarm-egress.swarm-system.svc.cluster.local", 3128)}
    assert "forwarder_unreachable" in world.unavailable


@pytest.mark.parametrize("status,cause", [(407, "expired"), (403, "egress_disabled"), (502, "vendor_refused")])
def test_a_refusal_from_swarm_egress_is_a_502_never_a_407(status, cause):
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        got, _, writer = await world.connect()
        assert got == 502, "a 407 would make Chromium ask for a proxy password"
        writer.close()

    world = run(scenario, Upstream(status=status, cause=cause))
    assert ("egress_refused", {"cause": cause}) in world.records


def test_the_agents_uid_cannot_use_the_forwarder():
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        status, _, writer = await world.connect()
        assert status == 403
        writer.close()

    world = run(scenario, Upstream(), uid=1000, peer_uid=1001)
    assert world.dialled == []
    assert ("egress_refused", {"cause": "peer_uid"}) in world.records


def test_an_owner_that_cannot_be_read_is_not_ours():
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        status, _, writer = await world.connect()
        assert status == 403
        writer.close()

    world = run(scenario, Upstream(), peer_uid=None)
    assert world.dialled == []


def test_only_connect_is_served():
    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(_ticket(), 600)
        status, _, writer = await world.connect(
            "GET http://plain.example.test/ HTTP/1.1\r\nHost: plain.example.test\r\n\r\n")
        assert status == 405
        writer.close()

    world = run(scenario, Upstream())
    assert world.dialled == []


def test_a_ticket_with_another_shape_is_not_taken():
    world = World(Upstream())
    for bad in ("", "a b", "x" * 5000, None, 7):
        with pytest.raises(ValueError):
            world.forwarder.set_ticket(bad, 600)
    with pytest.raises(ValueError):
        world.forwarder.set_ticket(_ticket(), 0)
    with pytest.raises(ValueError):
        world.forwarder.set_ticket(_ticket(), 10_000)


def test_no_record_or_log_line_carries_the_ticket_or_the_host(caplog):
    ticket = _ticket()
    caplog.set_level(logging.DEBUG)

    async def scenario(world: World) -> None:
        world.forwarder.set_ticket(ticket, 600)
        status, _, writer = await world.connect()
        writer.close()
        world.forwarder.fail("mint_refused")
        status, _, writer = await world.connect()
        writer.close()

    world = run(scenario, Upstream(status=403, cause="tenant_off"))
    text = json.dumps(world.records) + caplog.text
    for secret in (ticket, ticket.split(".")[0], "example-search"):
        assert secret not in text


def test_the_notice_page_loads_without_a_network_and_names_the_cause():
    url = egress.notice_url("forwarder_unreachable")
    assert url.startswith("data:text/html")
    page = urllib.parse.unquote(url.split(",", 1)[1])
    assert "forwarder_unreachable" in page and "not" in page and "direct" in page
    assert "<script" not in egress.notice_url("<script>alert(1)</script>")


# --------------------------------------------------------------------------
# The gate process: order, fail closed, the ctl operation
# --------------------------------------------------------------------------


class EgressRig(Rig):
    def __init__(self, tmp: Path, *, upstream: Upstream | None = None, fail_listen: bool = False) -> None:
        super().__init__(tmp)
        settings = egress.EgressSettings.from_env(PROXY_ENV)
        self.cfg = _cfg(tmp, egress=settings)
        self.upstream = upstream
        self.fail_listen = fail_listen
        self.chromium_args: list[list[str]] = []
        self.deps.start_egress = self.start_egress
        self.gate = gate_main.LiveGate(self.cfg, self.deps)
        self.forwarder: egress.LocalForwarder | None = None

    async def start_egress(self, cfg, record, on_unavailable):
        self._note("egress.start")
        if self.fail_listen:
            raise gate_main.SetupError("the egress forwarder cannot listen: Address in use")

        async def dial(host, port):
            if self.upstream is None:
                raise ConnectionRefusedError("down")
            return await asyncio.open_connection("127.0.0.1", self.upstream.port)

        self.forwarder = egress.LocalForwarder(cfg.egress, record=record, dial=dial,
                                               on_unavailable=on_unavailable)
        await self.forwarder.start()
        return self.forwarder

    async def start_chromium(self, cfg):
        self.chromium_args.append(gate_main.chromium_args(cfg))
        return await super().start_chromium(cfg)


def _client(rig: Rig) -> CtlClient:
    client = CtlClient(rig.cfg.ctl_path)
    client.register()
    return client


def test_the_forwarder_listens_before_chromium_starts_and_chromium_is_proxied(short_tmp):
    async def body() -> None:
        rig = EgressRig(short_tmp)
        stop, task = await _started(rig)
        names = rig.calls.names()
        assert names.index("egress.start") < names.index("chromium.start")
        (args,) = rig.chromium_args
        assert f"--proxy-server=http://127.0.0.1:{rig.forwarder.port}" in args
        status = await asyncio.to_thread(lambda: _client(rig).call("status", {}))
        assert status["egress"]["route"] == "proxy" and status["egress"]["state"] == "waiting"
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_a_forwarder_that_cannot_listen_means_no_chromium_at_all(short_tmp):
    async def body() -> None:
        rig = EgressRig(short_tmp, fail_listen=True)
        code = await gate_main.run(rig.cfg, stop=asyncio.Event(), gate=rig.gate)
        assert code == 1
        assert "chromium.start" not in rig.calls.names()

    asyncio.run(body())


def test_a_gate_with_egress_on_and_no_forwarder_refuses_to_launch(short_tmp):
    """The launch's own check, should anything ever skip `start_egress`."""
    async def body() -> None:
        rig = EgressRig(short_tmp)
        await rig.gate.start_display()
        with pytest.raises(gate_main.SetupError):
            await rig.gate.start_browser()
        assert "chromium.start" not in rig.calls.names()
        await rig.gate.stop()

    asyncio.run(body())


def test_the_workers_ticket_arms_the_forwarder_over_ctl_sock(short_tmp):
    ticket = _ticket()

    async def body() -> None:
        upstream = Upstream()
        await upstream.start()
        rig = EgressRig(short_tmp, upstream=upstream)
        stop, task = await _started(rig)
        client = await asyncio.to_thread(_client, rig)
        answer = await asyncio.to_thread(client.call, "egress_ticket", {"ticket": ticket, "expires_in": 600})
        assert answer == {"egress": "ready"}
        status = await asyncio.to_thread(client.call, "status", {})
        assert status["egress"]["state"] == "ready"
        assert ticket not in json.dumps(status)
        reader, writer = await asyncio.open_connection("127.0.0.1", rig.forwarder.port)
        writer.write(f"CONNECT {TARGET} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.close()
        assert f"Proxy-Authorization: Bearer {ticket}" in upstream.heads[0]
        stop.set()
        assert await task == 0
        await upstream.stop()

    asyncio.run(body())


def test_a_failed_mint_or_a_down_forwarder_puts_the_reason_on_screen(short_tmp):
    async def body() -> None:
        rig = EgressRig(short_tmp, upstream=None)
        stop, task = await _started(rig)
        client = await asyncio.to_thread(_client, rig)
        answer = await asyncio.to_thread(client.call, "egress_ticket", {"failed": "egress_off"})
        assert answer == {"egress": "failed", "cause": "egress_off"}
        answer = await asyncio.to_thread(client.call, "egress_ticket", {"ticket": _ticket(), "expires_in": 600})
        assert answer == {"egress": "failed", "cause": "forwarder_unreachable"}
        # One page per cause, each a data: URL saying why (egress.notice_url).
        await _until(lambda: rig.browser.transport.methods.count("Target.createTarget") >= 2)
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_with_egress_off_the_operation_is_refused_and_status_says_direct(short_tmp):
    async def body() -> None:
        rig = Rig(short_tmp)
        stop, task = await _started(rig)
        client = await asyncio.to_thread(_client, rig)
        status = await asyncio.to_thread(client.call, "status", {})
        assert status["egress"] == {"route": "direct"}
        with pytest.raises(CtlRefused):
            await asyncio.to_thread(client.call, "egress_ticket", {"ticket": _ticket(), "expires_in": 600})
        assert "egress.start" not in rig.calls.names()
        stop.set()
        assert await task == 0

    asyncio.run(body())


def test_egress_ticket_is_an_operation_ctl_sock_accepts():
    assert "egress_ticket" in ctl_mod.OPS
