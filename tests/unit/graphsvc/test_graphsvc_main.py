"""The process's settings and the kill switch (graph-service.md §2.1, §3.2).

The switch fails closed: a missing, unreadable or non-JSON file, a missing or
unknown key, or a non-boolean is OFF, and then every call is 503 `disabled`
and the worker falls back to staging at its next probe (§8). The key must be
`swarm-graph-ticket-key` and at least 32 bytes; nothing else starts.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
from graphsvc_fixtures import new_key

from swarm_graph import main


def _write(tmp_path, body) -> str:
    path = tmp_path / "config.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return str(path)


@pytest.mark.parametrize("body,enabled,cause", [
    ({"enabled": True}, True, "enabled"),
    ({"enabled": False}, False, "disabled"),
    ({"enabled": "yes"}, False, "config_invalid"),
    ({"enabled": True, "tenants": []}, False, "config_invalid"),
    ({}, False, "config_invalid"),
    ("not json", False, "config_invalid"),
    ([True], False, "config_invalid"),
])
def test_the_switch_fails_closed(tmp_path, body, enabled, cause):
    config = main.load_config(_write(tmp_path, body))
    assert (config.enabled, config.cause) == (enabled, cause)


def test_a_missing_switch_is_off(tmp_path):
    config = main.load_config(tmp_path / "absent.json")
    assert (config.enabled, config.cause) == (False, "config_unreadable")


VERSION = "projects/swarm-dev-1/secrets/swarm-graph-ticket-key/versions/latest"


def test_settings_name_the_graph_key_and_a_bucket():
    env = {main.KEY_ENV: VERSION, main.BUCKET_ENV: "swarm-artifacts-dev"}
    assert main.settings(env) == (VERSION, "swarm-artifacts-dev", main.DEFAULT_CONFIG)
    with pytest.raises(RuntimeError):
        main.settings({**env, main.KEY_ENV: VERSION.replace("graph-ticket", "egress")})
    with pytest.raises(RuntimeError):
        main.settings({**env, main.BUCKET_ENV: "a/b"})


def test_a_short_key_never_starts():
    with pytest.raises(RuntimeError):
        main.read_key(VERSION, lambda _v: b"short")
    key = new_key()
    assert main.read_key(VERSION, lambda _v: f"{key}\n".encode()) == key.encode()


def test_serve_answers_health_and_stops(tmp_path):
    env = {main.KEY_ENV: VERSION, main.BUCKET_ENV: "swarm-artifacts-dev",
           main.CONFIG_ENV: _write(tmp_path, {"enabled": True})}
    stop = threading.Event()
    key = new_key()
    ready = threading.Event()
    ports: list[int] = []
    real = main.make_server

    def capture(service, host, port):
        httpd = real(service, "127.0.0.1", 0)
        ports.append(httpd.server_address[1])
        ready.set()
        return httpd

    main.make_server = capture
    try:
        thread = threading.Thread(target=main.serve, kwargs=dict(
            env=env, access=lambda _v: key.encode(), store=object(), stop=stop), daemon=True)
        thread.start()
        assert ready.wait(10)
        deadline = time.time() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{ports[0]}/healthz",
                                            timeout=5) as response:
                    assert response.status == 200
                    break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        stop.set()
        main.make_server = real
    thread.join(10)
    assert not thread.is_alive()
