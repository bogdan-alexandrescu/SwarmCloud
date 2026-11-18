"""swarm-graph, the process (docs/design/graph-service.md §2.1, §3.2; lane GS-SVC).

What it reads, all to be named by kubernetes/swarm-system/swarm-graph.yaml
(lane GS-INFRA):

  * `SWARM_GRAPH_KEY_SECRET` -- the Secret Manager VERSION reference of
    `swarm-graph-ticket-key` (`.../versions/latest`), the key swarm-api MACs
    graph tickets with. Read at start, BEFORE the port opens (no key, no
    start), and re-read every SECRET_POLL_SECONDS; a change rotates the ring,
    which keeps the previous key for a ticket's life (`ticket.KeyRing`);
  * `SWARM_GRAPH_BUCKET` -- the artifact bucket the graphs live in. Every
    object name inside it is built from a verified ticket (store.py);
  * `SWARM_GRAPH_CONFIG` -- the mounted ConfigMap file `swarm-graph-config`,
    re-read every CONFIG_POLL_SECONDS: the kill switch.

THE KILL SWITCH (§2.1). EXACTLY `{"enabled": <bool>}`, mounted as a directory
(never subPath) so the kubelet refreshes it in place. FAIL CLOSED: a file that
is missing, unreadable, not JSON, lacks the key, carries another key, or holds
a non-boolean is OFF, and then every call is 503 `disabled` and the worker
falls back to staging at its next probe (§8).

Every secret and object is read with this pod's own identity (the KSA
`swarm-graph`, a direct Workload Identity principal; §3.4), over REST with
google-auth, into memory. Never an environment value, a file or a log line.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .server import PORT, GraphService, event, make_server, make_stats_server
from .stats import STATS_PORT
from .store import GcsStore
from .ticket import KeyRing, mac_key

KEY_ENV = "SWARM_GRAPH_KEY_SECRET"
BUCKET_ENV = "SWARM_GRAPH_BUCKET"
CONFIG_ENV = "SWARM_GRAPH_CONFIG"
DEFAULT_CONFIG = "/etc/swarm-graph/config.json"
CONFIG_KEYS = ("enabled",)
CONFIG_POLL_SECONDS = 5.0
SECRET_POLL_SECONDS = 300.0
#: §3.2: the key is at least 32 bytes.
MIN_KEY_BYTES = 32
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"
_KEY_VERSION = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/swarm-graph-ticket-key/versions/[a-z0-9]+")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")


@dataclass(frozen=True)
class GraphConfig:
    #: Off unless the file says otherwise.
    enabled: bool = False
    #: `enabled`, `disabled`, `config_unreadable` or `config_invalid`.
    cause: str = "disabled"


def parse_config(body: Any) -> GraphConfig:
    if not isinstance(body, dict) or set(body) != set(CONFIG_KEYS) \
            or not isinstance(body["enabled"], bool):
        raise ValueError("the configuration is exactly {\"enabled\": true|false}")
    return GraphConfig(enabled=body["enabled"],
                       cause="enabled" if body["enabled"] else "disabled")


def load_config(path: Path | str) -> GraphConfig:
    """The switch at `path`, or OFF with the reason. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return GraphConfig(enabled=False, cause="config_unreadable")
    try:
        return parse_config(json.loads(text))
    except ValueError:
        return GraphConfig(enabled=False, cause="config_invalid")


def _access_secret(version: str) -> bytes:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    response = AuthorizedSession(credentials).get(SECRET_MANAGER + version + ":access", timeout=30)
    if response.status_code != 200:
        # The status only: an error body may quote the request.
        raise RuntimeError(f"Secret Manager answered HTTP {response.status_code}")
    try:
        return base64.b64decode(response.json()["payload"]["data"])
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise RuntimeError("Secret Manager returned no payload") from None


def read_key(version: str, access: Callable[[str], bytes]) -> bytes:
    key = mac_key(access(version))
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError("swarm-graph-ticket-key is shorter than 32 bytes")
    return key


def settings(env: Mapping[str, str]) -> tuple[str, str, str]:
    """(key version, bucket, config path), or RuntimeError naming the variable."""
    version = (env.get(KEY_ENV) or "").strip()
    if not _KEY_VERSION.fullmatch(version):
        raise RuntimeError(
            f"{KEY_ENV} must be projects/<p>/secrets/swarm-graph-ticket-key/versions/<v>")
    bucket = (env.get(BUCKET_ENV) or "").strip()
    if not _BUCKET.fullmatch(bucket):
        raise RuntimeError(f"{BUCKET_ENV} must be a bucket name")
    return version, bucket, (env.get(CONFIG_ENV) or DEFAULT_CONFIG)


class _ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        self.current = load_config(path)
        event("graph_config", cause=self.current.cause)

    def reload(self) -> None:
        new = load_config(self._path)
        if new != self.current:
            event("graph_config", cause=new.cause)
        self.current = new


def serve(env: Mapping[str, str] = os.environ,
          access: Callable[[str], bytes] = _access_secret,
          store: Any = None, stop: threading.Event | None = None, port: int = PORT,
          stats_port: int = STATS_PORT) -> None:
    version, bucket, config_path = settings(env)
    keys = KeyRing(read_key(version, access))
    config = _ConfigFile(config_path)
    service = GraphService(keys=keys, store=store if store is not None else GcsStore(bucket),
                           enabled=lambda: config.current.enabled)
    stop = stop or threading.Event()

    def watch_config() -> None:
        while not stop.wait(CONFIG_POLL_SECONDS):
            config.reload()

    def watch_key() -> None:
        while not stop.wait(SECRET_POLL_SECONDS):
            try:
                if keys.rotate(read_key(version, access)):
                    event("graph_key_rotated")
            except Exception as exc:  # noqa: BLE001 - keep the keys in hand
                event("graph_key_unreadable", cause=type(exc).__name__)

    server = make_server(service, "0.0.0.0", port)
    # The read-only stats routes on their own port, never /mcp
    # (graph-answer-cache.md §5.3).
    stats_server = make_stats_server(service, "0.0.0.0", stats_port)
    for target in (watch_config, watch_key):
        threading.Thread(target=target, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # not the main thread (a test)
            pass
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=stats_server.serve_forever, daemon=True).start()
    event("graph_serving", port=server.server_address[1],
          stats_port=stats_server.server_address[1])
    try:
        stop.wait()
    finally:
        for listener in (server, stats_server):
            listener.shutdown()
            listener.server_close()
        service.warmer.close()
        event("graph_stopped")


def main() -> None:
    import logging

    try:
        from swarm_common.logging_setup import configure_logging

        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    except ImportError:
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    serve()


if __name__ == "__main__":
    main()


__all__ = ["BUCKET_ENV", "CONFIG_ENV", "CONFIG_KEYS", "KEY_ENV", "GraphConfig", "load_config",
           "main", "parse_config", "read_key", "serve", "settings"]
