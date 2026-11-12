"""swarm-api's own route to a workspace's Jev backend (lane JEV-REACH-API;
docs/design/agent-quality/jev-contract.md section 4.1, the owner's answer to
Q1, 3 November 2026).

swarm-api's contract review is a Jev call it makes itself. An internal
backend is reached through swarm-api's Direct VPC egress and the internal
passthrough load balancer `decision-api-internal` in front of the tailnet
egress (terraform/infra/tailscale.tf), at INTERNAL_DECISION_API_ADDRESS, with
SNI and Host for the tailnet name. Held here, with no cloud and no network
beyond the loopback:

* the backend, URL, model and key for one call, per backend;
* no address rendered, a misrendered one, or one that does not answer reads
  `no_route`, and an internal choice never falls back to TypeSafe;
* the transport dials the pinned address and verifies the tailnet name's own
  certificate, end to end;
* the TypeSafe key is the submitter's own, then the workspace's, and an
  unreadable one is no key.
"""

from __future__ import annotations

import datetime as dt
import secrets
import socket
import ssl
import threading
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from swarm_api import judgment
from swarm_api.gittokens import secret_name_for
from swarm_jev.client import JevBackend, urllib_transport

TAILNET = "https://decision-api.example-tailnet.ts.net"
HOST = "decision-api.example-tailnet.ts.net"
ADDRESS = "10.10.0.77"


@pytest.fixture(autouse=True)
def tailnet_name(monkeypatch) -> str:
    """The platform renders the name (SWARM_DECISION_API_TAILNET_NAME)."""
    monkeypatch.setenv(judgment.DECISION_API_TAILNET_NAME_ENV, HOST)
    return HOST


def _internal(backend: str = judgment.INTERNAL_JEV, *, url: str = TAILNET,
              address: str | None = ADDRESS) -> judgment.CallBackend | judgment.NoBackend:
    return judgment.resolve_call_backend(backend, internal_url=url, address=address,
                                         typesafe_key=None)


# --- resolution ----------------------------------------------------------------


@pytest.mark.parametrize("backend, path, model", [
    (judgment.INTERNAL_JEV, "jev", "jev-latest"),
    (judgment.INTERNAL_LAYA, "laya", "laya-latest"),
])
def test_an_internal_backend_is_the_tailnet_url_dialled_at_the_load_balancer(backend, path, model):
    resolved = _internal(backend)
    assert resolved == judgment.CallBackend(
        backend, f"{TAILNET}/{path}/v1/decide", model, stringify_state=True,
        key=None, connect_address=ADDRESS)
    assert isinstance(resolved, JevBackend)
    assert resolved.transport() is not urllib_transport


@pytest.mark.parametrize("address", [None, "", "  ", "decision-api.internal", "8.8.8.8",
                                     "127.0.0.1", "fd00::1", "10.10.0.77:443"])
def test_an_unreachable_backend_with_no_usable_address_reads_no_route(address):
    """No load balancer rendered (INTERNAL_DECISION_API_ADDRESS empty), or a
    value that is not the reserved private IPv4 address: there is no route."""
    resolved = _internal(address=address)
    assert isinstance(resolved, judgment.NoBackend)
    assert (resolved.backend, resolved.code) == (judgment.INTERNAL_JEV, "no_route")
    assert resolved.reason == judgment.NO_ROUTE


@pytest.mark.parametrize("url", ["", "http://decision-api.example-tailnet.ts.net",
                                 "https://decision-api.example-tailnet.ts.net:8443",
                                 "https://decision-api.example-tailnet.ts.net/jev",
                                 "https://user:@decision-api.example-tailnet.ts.net",
                                 "https://decision-api.example.com"])
def test_the_internal_backends_off_or_misnamed_read_no_route(url):
    resolved = _internal(url=url)
    assert isinstance(resolved, judgment.NoBackend)
    assert (resolved.code, resolved.reason) == ("no_route", judgment.NO_EGRESS)


@pytest.mark.parametrize("name", [None, "", "decision-api", TAILNET])
def test_the_internal_backends_on_without_the_tailnet_name_are_refused(monkeypatch, name):
    """No default host: INTERNAL_DECISION_API_URL set and the name unset or
    misshapen is the fixed refusal, never a call to a guessed host."""
    if name is None:
        monkeypatch.delenv(judgment.DECISION_API_TAILNET_NAME_ENV)
    else:
        monkeypatch.setenv(judgment.DECISION_API_TAILNET_NAME_ENV, name)
    resolved = _internal()
    assert isinstance(resolved, judgment.NoBackend)
    assert (resolved.code, resolved.reason) == ("no_route", judgment.NAME_UNSET)


def test_the_url_must_name_the_rendered_host(monkeypatch):
    monkeypatch.setenv(judgment.DECISION_API_TAILNET_NAME_ENV, "judge.example-tailnet.ts.net")
    resolved = _internal()
    assert (resolved.code, resolved.reason) == ("no_route", judgment.NO_EGRESS)
    resolved = _internal(url="https://judge.example-tailnet.ts.net")
    assert resolved.url == "https://judge.example-tailnet.ts.net/jev/v1/decide"


def test_an_internal_choice_never_falls_back_to_typesafe():
    key = secrets.token_hex(16)
    resolved = judgment.resolve_call_backend(judgment.INTERNAL_JEV, internal_url=TAILNET,
                                             address="", typesafe_key=key)
    assert isinstance(resolved, judgment.NoBackend) and resolved.code == "no_route"


def test_typesafe_is_the_public_api_with_the_resolved_key_and_never_logs_it():
    key = secrets.token_hex(16)
    resolved = judgment.resolve_call_backend(judgment.TYPESAFE_BACKEND, internal_url="",
                                             address=None, typesafe_key=key)
    assert isinstance(resolved, judgment.CallBackend)
    assert (resolved.url, resolved.model, resolved.stringify_state, resolved.key,
            resolved.connect_address) == (judgment.TYPESAFE_URL, "jev-latest", False, key, None)
    assert resolved.transport() is urllib_transport
    assert key not in repr(resolved)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_typesafe_without_a_key_reads_no_key(key):
    resolved = judgment.resolve_call_backend(judgment.TYPESAFE_BACKEND, internal_url=TAILNET,
                                             address=ADDRESS, typesafe_key=key)
    assert isinstance(resolved, judgment.NoBackend)
    assert (resolved.code, resolved.reason) == ("no_key", judgment.NO_KEY)


def test_an_unknown_name_is_the_default_backend():
    resolved = _internal("anything-else")
    assert isinstance(resolved, judgment.CallBackend) and resolved.name == judgment.DEFAULT_BACKEND


def test_every_code_is_a_known_unavailable_code():
    assert {judgment.UNAVAILABLE_CODES[judgment.NO_ROUTE],
            judgment.UNAVAILABLE_CODES[judgment.UNREACHABLE]} == {"no_route"}


def test_the_address_is_read_from_the_environment_at_the_call(monkeypatch):
    monkeypatch.delenv("INTERNAL_DECISION_API_ADDRESS", raising=False)
    assert judgment.route_address() is None
    monkeypatch.setenv("INTERNAL_DECISION_API_ADDRESS", f" {ADDRESS} ")
    assert judgment.route_address() == ADDRESS
    assert judgment.INTERNAL_DECISION_API_ADDRESS_ENV == "INTERNAL_DECISION_API_ADDRESS"


def test_terraform_sets_the_variable_swarm_api_reads():
    """The name in terraform/infra/tailscale.tf is the one route_address reads."""
    tf = (Path(__file__).resolve().parents[3] / "terraform" / "infra" / "tailscale.tf").read_text()
    assert f"{judgment.INTERNAL_DECISION_API_ADDRESS_ENV} = local.tailscale_egress_enabled" in tf


# --- an unreachable load balancer ----------------------------------------------


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_load_balancer_that_does_not_answer_reads_no_route():
    """The address is rendered but nothing listens on it: the probe reads
    `no_route`, with the reason a person is shown."""
    backend = judgment.CallBackend(judgment.INTERNAL_JEV, f"https://{HOST}:{_closed_port()}/jev/v1/decide",
                                   "jev-latest", stringify_state=True, connect_address="127.0.0.1")
    found = judgment.probe_route(backend, timeout=1.0)
    assert found == judgment.NoBackend(judgment.INTERNAL_JEV, "no_route", judgment.UNREACHABLE)


def test_the_probe_dials_the_pinned_address_on_443_and_sends_nothing():
    dialled: list[tuple[Any, float]] = []

    class _Socket:
        def close(self) -> None:
            dialled.append(("closed", 0.0))

    def connect(where: Any, timeout: float) -> _Socket:
        dialled.append((where, timeout))
        return _Socket()

    resolved = _internal()
    assert judgment.probe_route(resolved, connect=connect) is None
    assert dialled == [((ADDRESS, 443), judgment.PROBE_SECONDS), ("closed", 0.0)]


def test_the_probe_passes_through_what_resolution_already_decided():
    no_route = _internal(address="")
    assert judgment.probe_route(no_route) == no_route
    no_key = judgment.resolve_call_backend(judgment.TYPESAFE_BACKEND, internal_url="",
                                           address=None, typesafe_key=None)
    assert judgment.probe_route(no_key) is None  # not a route problem
    typesafe = judgment.resolve_call_backend(judgment.TYPESAFE_BACKEND, internal_url="",
                                             address=None, typesafe_key=secrets.token_hex(8))
    assert judgment.probe_route(typesafe, connect=lambda *a: pytest.fail("dialled")) is None


# --- the pinned transport -------------------------------------------------------


def _certificate(tmp: Path, name: str) -> tuple[Path, Path, Path]:
    """A throwaway CA and a server certificate for `name`, made at runtime."""
    now = dt.datetime.now(dt.timezone.utc)

    def build(subject: str, issuer: x509.Name, key: Any, signer: Any, *, ca: bool) -> x509.Certificate:
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
                   .issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - dt.timedelta(minutes=1))
                   .not_valid_after(now + dt.timedelta(hours=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if not ca:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(subject)]),
                                            critical=False)
        return builder.sign(signer, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca = build("test-ca", ca_name, ca_key, ca_key, ca=True)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = build(name, ca_name, server_key, ca_key, ca=False)
    ca_path, cert_path, key_path = tmp / "ca.pem", tmp / "server.pem", tmp / "server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return ca_path, cert_path, key_path


def _serve_once(cert: Path, key: Path, seen: dict[str, Any]) -> tuple[int, threading.Thread]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def sni(_sock: Any, server_name: str | None, _ctx: Any) -> None:
        seen["sni"] = server_name

    context.sni_callback = sni
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def run() -> None:
        with listener:
            raw, _ = listener.accept()
            try:
                with context.wrap_socket(raw, server_side=True) as conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += conn.recv(4096)
                    head, _, body = data.partition(b"\r\n\r\n")
                    lines = head.decode().split("\r\n")
                    headers = dict(line.split(": ", 1) for line in lines[1:])
                    while len(body) < int(headers.get("Content-Length", "0")):
                        body += conn.recv(4096)
                    seen.update(request=lines[0], host=headers.get("Host"), body=body)
                    reply = b'{"answers": {}}'
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: " + str(len(reply)).encode() + b"\r\n\r\n" + reply)
            except (ssl.SSLError, OSError) as exc:
                seen["server_error"] = type(exc).__name__

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


def test_the_transport_dials_the_address_and_verifies_the_tailnet_names_certificate(tmp_path):
    ca, cert, key = _certificate(tmp_path, HOST)
    seen: dict[str, Any] = {}
    port, thread = _serve_once(cert, key, seen)
    client = ssl.create_default_context(cafile=str(ca))
    transport = judgment.pinned_transport("127.0.0.1", context=client)
    status, raw = transport(f"https://{HOST}:{port}/jev/v1/decide", b'{"q": 1}',
                            {"Content-Type": "application/json"}, 5.0)
    thread.join(5)
    assert (status, raw) == (200, b'{"answers": {}}')
    assert seen["sni"] == HOST
    assert seen["host"] == f"{HOST}:{port}"
    assert seen["request"] == "POST /jev/v1/decide HTTP/1.1"
    assert seen["body"] == b'{"q": 1}'


def test_a_certificate_for_another_name_is_refused(tmp_path):
    """TLS is end to end: the load balancer's address is dialled, but only the
    tailnet name's certificate is accepted."""
    ca, cert, key = _certificate(tmp_path, "someone-else.example-tailnet.ts.net")
    seen: dict[str, Any] = {}
    port, thread = _serve_once(cert, key, seen)
    transport = judgment.pinned_transport("127.0.0.1", context=ssl.create_default_context(cafile=str(ca)))
    with pytest.raises(ssl.SSLCertVerificationError):
        transport(f"https://{HOST}:{port}/jev/v1/decide", b"{}", {}, 5.0)
    thread.join(5)
    assert "request" not in seen


def test_the_transport_refuses_anything_but_https():
    transport = judgment.pinned_transport(ADDRESS)
    with pytest.raises(ValueError):
        transport(f"http://{HOST}/jev/v1/decide", b"{}", {}, 1.0)


# --- the TypeSafe key -----------------------------------------------------------


def test_the_submitters_own_key_first_then_the_workspaces():
    own, workspace = secret_name_for("u-ada", "typesafe"), secret_name_for("eng", "typesafe")
    values = {own: secrets.token_hex(12), workspace: secrets.token_hex(12)}
    assert judgment.typesafe_call_key("eng", "u-ada", {own, workspace}, values.get) == values[own]
    assert judgment.typesafe_call_key("eng", "u-ada", {workspace}, values.get) == values[workspace]
    assert judgment.typesafe_call_key("eng", None, {own, workspace}, values.get) == values[workspace]
    assert judgment.typesafe_call_key("eng", "u-ada", set(), values.get) is None


def test_an_unreadable_key_is_no_key():
    workspace = secret_name_for("eng", "typesafe")

    def denied(_secret_id: str) -> str:
        raise PermissionError("403")

    assert judgment.typesafe_call_key("eng", None, {workspace}, denied) is None
    assert judgment.typesafe_call_key("eng", None, {workspace}, lambda _s: "  ") is None
