"""Lightpanda reaches the step's own loopback servers and nothing else private
(owner's pick of 2 November 2026, "Five groups now, audit loopback"; part of
#2688; docs/design/lightpanda-runtime.md §6.2).

Both Lightpanda commands keep `--block-private-networks` and exempt exactly
127.0.0.1 and ::1 from it, so the metadata server, 10/8, 172.16/12,
192.168/16, 100.64/10 and the pod and service ranges stay refused. The
platform's own loopback listeners are refused by port: Lightpanda's own CDP
(9223), test-services Postgres (5432) and every port that already listens on
loopback when the runner starts Lightpanda (the TypeSafe shim, the MCP relay:
they start before the runner). A refused port
that is a scheme's default port (80, 443) cannot be refused by URL, so then no
loopback exemption is given at all.

The last test runs the real binary when the image has it (a
claude-code-lightpanda pod): a page served on a loopback port is read, a
refused port and the metadata server are not. Elsewhere it is skipped; CI's
unit tests hold the argv.

MUTATION: drop the `-::1/128` exemption, add `-169.254.0.0/16`, drop the
/proc snapshot from the runner, or keep the exemption when port 80 is refused,
and a test below fails.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import os
import secrets
import subprocess
import threading
from pathlib import Path

import pytest

from agent_worker import lightpanda

#: What must stay refused (the brief, and CONTRACT.md invariant 9).
REFUSED = (
    "169.254.169.254/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10", "127.0.0.2/32", "0.0.0.0/32", "fd00::/8", "fe80::/10",
)
METADATA_URLS = ("http://169.254.169.254/computeMetadata/v1/",
                 "http://metadata.google.internal/computeMetadata/v1/")


def _pairs(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == flag]


def _exempted(args: list[str]) -> list[str]:
    return [value[1:] for value in _pairs(args, "--block-cidrs") if value.startswith("-")]


def _commands() -> dict[str, list[str]]:
    env = lightpanda.child_env({}, Path("/c"))
    return {"serve": lightpanda.serve_argv(env), "mcp": lightpanda.mcp_server(env)["args"]}


@pytest.mark.parametrize("name", ["serve", "mcp"])
def test_loopback_is_exempted_and_nothing_else(name):
    args = _commands()[name]
    assert "--block-private-networks" in args
    assert sorted(_exempted(args)) == ["127.0.0.1/32", "::1/128"]


@pytest.mark.parametrize("name", ["serve", "mcp"])
def test_the_metadata_server_and_every_private_range_stay_refused(name):
    args = _commands()[name]
    assert "--block-private-networks" in args
    for exempt in _exempted(args):
        allowed = ipaddress.ip_network(exempt)
        for refused in REFUSED:
            net = ipaddress.ip_network(refused)
            assert net.version != allowed.version or not net.overlaps(allowed), (exempt, refused)
    joined = " ".join(args)
    assert "metadata" not in joined and "169.254" not in joined


@pytest.mark.parametrize("name", ["serve", "mcp"])
def test_the_platforms_fixed_loopback_ports_are_refused(name):
    args = _commands()[name]
    for port in (9223, 5432):
        assert f"*:{port}/*" in _pairs(args, "--block-urls")


def test_a_listener_present_at_start_is_refused_and_named_once():
    env = lightpanda.child_env({}, Path("/c"))
    args = lightpanda.serve_argv(env, refused_ports=[41234, 9223, 41234])
    urls = _pairs(args, "--block-urls")
    assert urls.count("*:41234/*") == 1 and urls.count("*:9223/*") == 1
    assert sorted(_exempted(args)) == ["127.0.0.1/32", "::1/128"]


@pytest.mark.parametrize("port", [80, 443])
def test_a_default_port_listener_keeps_loopback_refused(port):
    """A URL on a scheme's default port carries no `:<port>`: the port cannot be
    refused by URL, so loopback is not exempted at all."""
    env = lightpanda.child_env({}, Path("/c"))
    for args in (lightpanda.serve_argv(env, refused_ports=[port]),
                 lightpanda.mcp_server(env, refused_ports=[port])["args"]):
        assert "--block-private-networks" in args
        assert _exempted(args) == []


def _tcp_line(slot: int, local: str, state: str) -> str:
    return (f"  {slot}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 "
            "00000000  1000        0 1 1 0000000000000000 100 0 0 10 0")


def test_loopback_listeners_reads_only_listening_loopback_and_wildcard_sockets(tmp_path):
    net = tmp_path / "net"
    net.mkdir()
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid"
    (net / "tcp").write_text("\n".join([
        head,
        _tcp_line(0, "0100007F:A0B2", "0A"),   # 127.0.0.1:41138, listening
        _tcp_line(1, "00000000:1538", "0A"),   # 0.0.0.0:5432, listening
        _tcp_line(2, "622C2C0A:1F90", "0A"),   # 10.44.44.98:8080, the pod IP only
        _tcp_line(3, "0100007F:C350", "01"),   # 127.0.0.1:50000, established
        _tcp_line(4, "0200007F:2710", "0A"),   # 127.0.0.2:10000: not exempted, refused anyway
    ]) + "\n")
    (net / "tcp6").write_text("\n".join([
        head,
        _tcp_line(0, "00000000000000000000000001000000:9C40", "0A"),  # [::1]:40000
        _tcp_line(1, "00000000000000000000000000000000:9C41", "0A"),  # [::]:40001
        _tcp_line(2, "0000000000000000FFFF00000100007F:9C42", "0A"),  # [::ffff:127.0.0.1]
        _tcp_line(3, "000080FE00000000FF00000001000000:9C43", "0A"),  # fe80::, not loopback
    ]) + "\n")
    assert lightpanda.loopback_listeners(net) == [5432, 40000, 40001, 40002, 41138]


def test_loopback_listeners_without_proc_is_empty(tmp_path):
    assert lightpanda.loopback_listeners(tmp_path / "absent") == []


# --------------------------------------------------------------------------- #
# Live, in a claude-code-lightpanda pod                                       #
# --------------------------------------------------------------------------- #

_LIVE = Path(lightpanda.BINARY)


class _Page(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802 -- http.server's name
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_a):
        return


def _serve(body: bytes) -> http.server.HTTPServer:
    handler = type("Page", (_Page,), {"body": body})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _fetch(url: str, refused_ports: list[int]) -> str:
    env = lightpanda.child_env(os.environ, Path(os.environ.get("TMPDIR", "/tmp")))
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    network = list(lightpanda.network_args(refused_ports))
    done = subprocess.run(
        [lightpanda.ENV_BIN, "-i", *(f"{k}={env[k]}" for k in lightpanda.CHILD_ENV_NAMES),
         str(_LIVE), "fetch", *network, "--dump", "markdown", url],
        capture_output=True, text=True, timeout=60, check=False)
    return done.stdout + done.stderr


@pytest.mark.skipif(not _LIVE.is_file(), reason="needs the Lightpanda binary of agent-runtime-lightpanda")
def test_live_a_loopback_page_is_read_and_the_metadata_server_is_refused():
    marker = secrets.token_hex(8)
    own = _serve(f"<html><body><h1>own-{marker}</h1></body></html>".encode())
    platform = _serve(f"<html><body><h1>platform-{marker}</h1></body></html>".encode())
    try:
        own_port, platform_port = own.server_address[1], platform.server_address[1]
        refused = [*lightpanda.PLATFORM_PORTS, platform_port]
        results = {
            "own": _fetch(f"http://127.0.0.1:{own_port}/", refused),
            "own_localhost": _fetch(f"http://localhost:{own_port}/", refused),
            "platform": _fetch(f"http://127.0.0.1:{platform_port}/", refused),
            "platform_localhost": _fetch(f"http://localhost:{platform_port}/", refused),
            **{url: _fetch(url, refused) for url in METADATA_URLS},
        }
    finally:
        own.shutdown()
        platform.shutdown()
    artifacts = os.environ.get("SWARM_ARTIFACTS_DIR")
    if artifacts and Path(artifacts).is_dir():
        Path(artifacts, "loopback-live.json").write_text(json.dumps(results, indent=2))
    assert f"own\\-{marker}" in results["own"] or f"own-{marker}" in results["own"]
    assert marker in results["own_localhost"]
    for key in ("platform", "platform_localhost"):
        assert marker not in results[key] and "UrlBlocked" in results[key]
    for url in METADATA_URLS:
        assert "Navigation failed" in results[url] and "Metadata-Flavor" not in results[url]
