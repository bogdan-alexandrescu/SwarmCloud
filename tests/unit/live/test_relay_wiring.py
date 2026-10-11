"""The relay as it is wired (LB-L8d; docs/design/live-browser.md §3.3, owner decisions 2026-10-11).

  (a) the relay has no Firestore client: it trusts swarm-api's ticket and
      nothing else, and swarm-api closes a session on `POST /internal/close`;
  (b) it serves plain ws on port 8080, with no certificate anywhere;
  (c) it reads swarm-live-key from Secret Manager by the version reference in
      SWARM_LIVE_KEY_SECRET.

And the two ends agree: a ticket and a close request minted by swarm-api's code
verify here. That is the test that fails when either side's format drifts.

Every key and ticket is built at runtime; nothing here is a credential. No
Secret Manager, Firestore or network is reached: the secret read is a fake
passed to `build_rendezvous`, and Firestore is made unimportable.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-live")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_live import main as relay_main  # noqa: E402
from swarm_live import rendezvous as rz  # noqa: E402
from swarm_live.rendezvous import (  # noqa: E402
    CLOSE_ENDED,
    CLOSE_REFUSED,
    CLOSE_SIGNATURE_HEADER,
    OP_OPEN,
    Refused,
    Rendezvous,
    decode_frame,
    mint_ticket,
    sign_close,
    verify_close,
    verify_ticket,
)

TENANT, TASK, ATTEMPT, SESSION, GEN = "eng", "task_a1", "att_a1", "live_att_a1", 3
KEY_REF = "projects/swarm-test/secrets/swarm-live-key/versions/latest"


def ticket(key: bytes, role: str = "tunnel", **overrides) -> str:
    fields = dict(session_id=SESSION, tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                  generation=GEN, role=role)
    if role == "viewer":
        fields["email"] = "viewer@example.test"
    fields.update(overrides)
    return mint_ticket(key, **fields)


def close_body(session_id: str = SESSION, *, exp: int | None = None, cause: str = "cancelled") -> bytes:
    return json.dumps({"session_id": session_id, "cause": cause,
                       "exp": int(time.time()) + 30 if exp is None else exp,
                       "nonce": secrets.token_hex(8)}, sort_keys=True).encode()


# ---------------------------------------------------------------- (a) + (b): it starts bare


def test_the_relay_starts_and_serves_with_no_firestore_client_and_no_certificate(monkeypatch):
    key = secrets.token_bytes(32)
    asked: list[str] = []

    def access(name: str) -> bytes:
        asked.append(name)
        return key + b"\n"

    # Any attempt to import Firestore fails the start.
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", None)
    for name in ("SWARM_LIVE_TLS_CERT", "SWARM_LIVE_TLS_KEY", "SWARM_LIVE_KEY_FILE"):
        monkeypatch.delenv(name, raising=False)
    rendezvous = relay_main.build_rendezvous(
        {"SWARM_LIVE_KEY_SECRET": KEY_REF, "SWARM_LIVE_RECORD": "false"}, access)
    assert asked == [KEY_REF]
    with TestClient(relay_main.create_app(rendezvous)) as client:
        assert client.get("/healthz").json() == {"ok": True, "tunnels": 0, "viewers": 0}
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
            with client.websocket_connect("/v1/view") as viewer:
                viewer.send_text(ticket(key, "viewer"))
                assert json.loads(viewer.receive_text()) == {"type": "ready"}
                assert decode_frame(gate.receive_bytes())[0] == OP_OPEN


def test_main_serves_plain_ws_on_8080_with_no_tls(monkeypatch):
    import uvicorn
    from swarm_common import logging_setup

    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(logging_setup, "configure_logging", lambda *a, **kw: None)
    for name in ("SWARM_LIVE_TLS_CERT", "SWARM_LIVE_TLS_KEY"):
        monkeypatch.delenv(name, raising=False)
    relay_main.main()
    assert relay_main.PORT == 8080
    assert seen["port"] == 8080 and seen["factory"] is True
    assert not [k for k in seen if k.startswith("ssl")]


def test_the_relay_package_names_no_firestore_and_no_certificate():
    package = Path(_APP) / "swarm_live"
    code = "\n".join(p.read_text() for p in sorted(package.glob("*.py")))
    for gone in ("from google.cloud import firestore", "firestore.Client", "FirestoreSessionStore",
                 "ssl_certfile", "SWARM_LIVE_TLS_CERT", "SWARM_LIVE_KEY_FILE", "live_sessions\")"):
        assert gone not in code, gone
    pyproject = (Path(_APP) / "pyproject.toml").read_text()
    assert "google-cloud-firestore" not in pyproject
    assert "google-cloud-secret-manager" in pyproject


# ---------------------------------------------------------------- (c): the key


def test_the_key_is_read_by_its_secret_version_reference_only():
    key = secrets.token_bytes(32)
    assert relay_main.read_key(KEY_REF, lambda name: key) == key
    for ref in (None, "", "/var/run/secrets/swarm-live-key", "projects/p/secrets/swarm-live-key"):
        with pytest.raises(RuntimeError):
            relay_main.read_key(ref, lambda name: key)
    with pytest.raises(RuntimeError, match="shorter"):
        relay_main.read_key(KEY_REF, lambda name: b"x" * 8)


# ---------------------------------------------------------------- the close route


def test_a_close_request_drops_a_connected_viewer_and_a_forged_one_is_refused():
    key = secrets.token_bytes(32)
    rendezvous = Rendezvous(key)
    with TestClient(relay_main.create_app(rendezvous)) as client:
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key))
            assert json.loads(gate.receive_text()) == {"type": "ready"}
            with client.websocket_connect("/v1/view") as viewer:
                viewer.send_text(ticket(key, "viewer"))
                assert json.loads(viewer.receive_text()) == {"type": "ready"}
                gate.receive_bytes()  # the viewer's OPEN
                # Refused alike: unsigned, signed with another key, expired, or
                # with the body changed after signing. None drops anything.
                body = close_body()
                stale = close_body(exp=int(time.time()) - 1)
                for sent, signature in (
                    (body, None),
                    (body, sign_close(secrets.token_bytes(32), body)),
                    (stale, sign_close(key, stale)),
                    (close_body(), sign_close(key, body)),
                ):
                    headers = {} if signature is None else {CLOSE_SIGNATURE_HEADER: signature}
                    response = client.post("/internal/close", content=sent, headers=headers)
                    assert response.status_code == 403
                assert rendezvous.viewer_count() == 1
                good = client.post("/internal/close", content=body,
                                   headers={CLOSE_SIGNATURE_HEADER: sign_close(key, body)})
                assert good.status_code == 204
                with pytest.raises(WebSocketDisconnect) as closed:
                    viewer.receive_bytes()
                assert closed.value.code == CLOSE_ENDED
                # The same request again is a replay.
                again = client.post("/internal/close", content=body,
                                    headers={CLOSE_SIGNATURE_HEADER: sign_close(key, body)})
                assert again.status_code == 403
        assert rendezvous.tunnel_count() == 0 and rendezvous.viewer_count() == 0
        # And the closed session takes no fresh ticket inside its lifetime.
        with client.websocket_connect("/v1/tunnel") as gate:
            gate.send_text(ticket(key))
            with pytest.raises(WebSocketDisconnect) as closed:
                gate.receive_text()
            assert closed.value.code == CLOSE_REFUSED


# ---------------------------------------------------------------- the two ends agree


def test_tickets_swarm_api_mints_verify_at_the_relay():
    from swarm_api.live import mint_ticket as api_mint

    key_text = "-".join(("live", "parity", "key")) + "-" + secrets.token_hex(16)
    key = key_text.encode()
    now = int(time.time())
    base = {"session_id": SESSION, "tenant_id": TENANT, "task_id": TASK,
            "attempt_id": ATTEMPT, "generation": GEN, "exp": now + 60,
            "nonce": secrets.token_urlsafe(16)}
    tunnel = verify_ticket(key, api_mint(key_text, {**base, "role": "tunnel"}), now=now)
    assert (tunnel.role, tunnel.session_id, tunnel.generation, tunnel.email) == (
        "tunnel", SESSION, GEN, "")
    viewer = verify_ticket(key, api_mint(key_text, {**base, "role": "viewer",
                                                    "email": "a@example.test",
                                                    "nonce": secrets.token_urlsafe(16)}), now=now)
    assert (viewer.role, viewer.email, viewer.tenant_id) == ("viewer", "a@example.test", TENANT)
    # And the relay's test minter is swarm-api's format.
    from swarm_api.live import verify_ticket as api_verify

    claims = api_verify((key_text,), mint_ticket(key, session_id=SESSION, tenant_id=TENANT,
                                                 task_id=TASK, attempt_id=ATTEMPT,
                                                 generation=GEN, role="tunnel"), now=now)
    assert claims["session_id"] == SESSION
    # A 61-second ticket from swarm-api's minter is refused here.
    with pytest.raises(Refused) as e:
        verify_ticket(key, api_mint(key_text, {**base, "role": "tunnel", "exp": now + 3600}), now=now)
    assert e.value.reason == "lifetime"


def test_close_requests_swarm_api_signs_verify_at_the_relay():
    from swarm_api.live import CLOSE_SIGNATURE_HEADER as API_HEADER
    from swarm_api.live import relay_close_request

    key_text = "-".join(("live", "parity", "key")) + "-" + secrets.token_hex(16)
    body, signature = relay_close_request(key_text, SESSION, "attempt_ended", now=time.time())
    request = verify_close(key_text.encode(), body, signature, now=time.time())
    assert (request.session_id, request.cause) == (SESSION, "attempt_ended")
    assert API_HEADER.lower() == CLOSE_SIGNATURE_HEADER
    # A ticket's MAC is never a close request's, and the other way round.
    raw = mint_ticket(key_text.encode(), session_id=SESSION, tenant_id=TENANT, task_id=TASK,
                      attempt_id=ATTEMPT, generation=GEN, role="tunnel")
    with pytest.raises(Refused):
        verify_close(key_text.encode(), rz._b64decode(raw.split(".")[1]), raw.split(".")[2],
                     now=time.time())
