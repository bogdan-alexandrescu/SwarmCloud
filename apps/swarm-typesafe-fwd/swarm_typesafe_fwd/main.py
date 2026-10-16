"""swarm-typesafe-fwd, the process (docs/design/workspace-secrets.md §3.5; lane SEC-INJECT-B).

What it reads, all named by kubernetes/swarm-system/swarm-typesafe-fwd.yaml:

  * `SWARM_TYPESAFE_FWD_KEY_SECRET` -- the Secret Manager VERSION reference of
    `swarm-typesafe-fwd-key` (`.../versions/latest`), the key swarm-api MACs
    tickets with. Read at start, BEFORE the port opens (no key, no start: the
    readiness probe is a TCP check, so a forwarder that can verify nothing
    must never look ready), and re-read every SECRET_POLL_SECONDS; a change
    rotates the ring (keyring.KeyRing keeps the previous key);
  * `SWARM_TYPESAFE_FWD_PROJECT` -- the ONLY project a ticket's secret is read
    from;
  * `SWARM_TYPESAFE_FWD_CONFIG` -- the mounted ConfigMap file (config.py),
    re-read every CONFIG_POLL_SECONDS: the kill switch.

Every secret is read with this pod's own Workload Identity (the KSA
`swarm-typesafe-fwd`), over REST with google-auth, into memory. Never an
environment value, a file or a log line.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import signal
import threading
from typing import Callable, Mapping

from .config import FwdConfig, load_config
from .forwarder import PORT, Forwarder, event
from .keyring import KeyRing, SecretCache
from .server import make_server
from .ticket import mac_key
from .upstream import HttpsUpstream

KEY_ENV = "SWARM_TYPESAFE_FWD_KEY_SECRET"
PROJECT_ENV = "SWARM_TYPESAFE_FWD_PROJECT"
CONFIG_ENV = "SWARM_TYPESAFE_FWD_CONFIG"
CONFIG_POLL_SECONDS = 5.0
SECRET_POLL_SECONDS = 300.0
MIN_KEY_BYTES = 32
DEFAULT_CONFIG = "/etc/swarm-typesafe-fwd/config.json"
_KEY_VERSION = re.compile(r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/swarm-typesafe-fwd-key/versions/[a-z0-9]+")
_PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"


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
        raise RuntimeError("swarm-typesafe-fwd-key is shorter than 32 bytes")
    return key


def settings(env: Mapping[str, str]) -> tuple[str, str, str]:
    """(key version, project, config path), or RuntimeError naming the variable."""
    version = (env.get(KEY_ENV) or "").strip()
    if not _KEY_VERSION.fullmatch(version):
        raise RuntimeError(f"{KEY_ENV} must be projects/<p>/secrets/swarm-typesafe-fwd-key/versions/<v>")
    project = (env.get(PROJECT_ENV) or "").strip()
    if not _PROJECT.fullmatch(project):
        raise RuntimeError(f"{PROJECT_ENV} must be a project id")
    return version, project, (env.get(CONFIG_ENV) or DEFAULT_CONFIG)


class _ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        self.current: FwdConfig = load_config(path)
        event("typesafe_config", cause=self.current.cause)

    def reload(self) -> None:
        new = load_config(self._path)
        if (new.enabled, new.cause) != (self.current.enabled, self.current.cause):
            event("typesafe_config", cause=new.cause)
        self.current = new


def serve(env: Mapping[str, str] = os.environ,
          access: Callable[[str], bytes] = _access_secret,
          stop: threading.Event | None = None, port: int = PORT) -> None:
    version, project, config_path = settings(env)
    keys = KeyRing(read_key(version, access))
    config = _ConfigFile(config_path)
    forwarder = Forwarder(keys=keys, secrets=SecretCache(access, project=project),
                          config=lambda: config.current, upstream=HttpsUpstream())
    stop = stop or threading.Event()

    def watch_config() -> None:
        while not stop.wait(CONFIG_POLL_SECONDS):
            config.reload()

    def watch_key() -> None:
        while not stop.wait(SECRET_POLL_SECONDS):
            try:
                if keys.rotate(read_key(version, access)):
                    event("typesafe_key_rotated")
            except Exception as exc:  # noqa: BLE001 - keep the keys in hand
                event("typesafe_key_unreadable", cause=type(exc).__name__)

    server = make_server(forwarder, "0.0.0.0", port)
    for target in (watch_config, watch_key):
        threading.Thread(target=target, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # not the main thread (a test)
            pass
    threading.Thread(target=server.serve_forever, daemon=True).start()
    event("typesafe_serving", port=port)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        event("typesafe_stopped")


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


__all__ = ["CONFIG_ENV", "KEY_ENV", "PORT", "PROJECT_ENV", "main", "read_key", "serve", "settings"]
