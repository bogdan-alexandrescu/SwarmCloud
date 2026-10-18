"""swarm-github-hooks, the process (docs/design/graph-service.md §6.2; lane GS-HOOKS).

What it reads, all set by terraform/infra/github_hooks.tf:

  * `SWARM_GITHUB_HOOKS_SECRET` -- the Secret Manager VERSION reference of
    `swarm-github-app-webhook-secret` (`.../versions/latest`). Read at run
    time, NOT at start and not by Cloud Run reference: the service is deployed
    before the owner stores the value, and a delivery signed before then is a
    503 rather than a revision that never starts. Its project is the one the
    Firestore write goes to;
  * `SWARM_API_URL` -- swarm-api's https URL, the one place a verified push
    is forwarded to; `SWARM_API_AUDIENCE`, when set, the audience the ID
    token is minted for, and otherwise that URL (what the rollup sweeper's
    Cloud Scheduler jobs mint for);
  * `FIRESTORE_DATABASE` -- the database github_deliveries lives in;
  * `PORT` -- Cloud Run's, 8080 by default.

THE IDENTITY is the service's own: the rollup sweeper account (owner decision
2026-10-16). Every call is made as it, through the metadata server; there is
no credential in the image, the environment or a file.

`create_receiver()` is the factory: it reads and validates the settings and
wires the receiver, and builds no client -- the first delivery that needs one
builds it -- so importing or constructing this needs no credential. It is the
`create_app()` pattern CLAUDE.md asks of a FastAPI service, under another name
on purpose: scripts/lib/check-contract-parity.sh reads every `create_app(` as a
FastAPI app that modules/cloud_run probes at GET /healthz, and this is neither
(the standard library's server, a TCP startup probe, one POST route).
"""

from __future__ import annotations

import os
import re
import signal
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .clients import ApiForwarder, FirestoreDeliveries, authorized_session, read_secret
from .receiver import Deliveries, Receiver, WebhookSecret, event
from .server import make_server

SECRET_ENV = "SWARM_GITHUB_HOOKS_SECRET"
API_URL_ENV = "SWARM_API_URL"
AUDIENCE_ENV = "SWARM_API_AUDIENCE"
DATABASE_ENV = "FIRESTORE_DATABASE"
PORT_ENV = "PORT"
DEFAULT_PORT = 8080
#: The one secret this service reads (terraform/infra/github_hooks.tf).
SECRET_ID = "swarm-github-app-webhook-secret"

_SECRET_VERSION = re.compile(
    r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/secrets/" + re.escape(SECRET_ID) + r"/versions/[a-z0-9]+")
_API_URL = re.compile(r"https://[a-z0-9.-]+(?::[0-9]{1,5})?/?")
_DATABASE = re.compile(r"[a-z][a-z0-9-]{0,62}")


@dataclass(frozen=True)
class Settings:
    secret_version: str
    project: str
    api_url: str
    audience: str
    database: str
    port: int


def settings(env: Mapping[str, str]) -> Settings:
    """The settings, or RuntimeError naming the variable (never its value)."""
    version = (env.get(SECRET_ENV) or "").strip()
    matched = _SECRET_VERSION.fullmatch(version)
    if matched is None:
        raise RuntimeError(f"{SECRET_ENV} must be projects/<p>/secrets/{SECRET_ID}/versions/<v>")
    api_url = (env.get(API_URL_ENV) or "").strip()
    if not _API_URL.fullmatch(api_url):
        raise RuntimeError(f"{API_URL_ENV} must be swarm-api's https:// base URL, with no path or query")
    audience = (env.get(AUDIENCE_ENV) or "").strip() or api_url
    if not audience.startswith("https://"):
        raise RuntimeError(f"{AUDIENCE_ENV} must be an https:// audience")
    database = (env.get(DATABASE_ENV) or "swarm").strip()
    if not _DATABASE.fullmatch(database):
        raise RuntimeError(f"{DATABASE_ENV} must be a Firestore database id")
    port_text = (env.get(PORT_ENV) or str(DEFAULT_PORT)).strip()
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise RuntimeError(f"{PORT_ENV} must be a port number")
    return Settings(secret_version=version, project=matched.group(1), api_url=api_url.rstrip("/"),
                    audience=audience, database=database, port=int(port_text))


def create_receiver(env: Mapping[str, str] | None = None, *,
               access: Callable[[str], bytes] | None = None,
               deliveries: Deliveries | None = None,
               forward: Callable[[str], int] | None = None,
               session_factory: Callable[[], Any] = authorized_session) -> Receiver:
    """The receiver, wired. Builds no client: each is made on first use."""
    s = settings(os.environ if env is None else env)
    sessions: dict[str, Any] = {}

    def session() -> Any:
        if "s" not in sessions:
            sessions["s"] = session_factory()
        return sessions["s"]

    reader = access or (lambda version: read_secret(version, session()))
    return Receiver(
        secret=WebhookSecret(lambda: reader(s.secret_version)),
        deliveries=deliveries or FirestoreDeliveries(s.project, s.database, session),
        forward=forward or ApiForwarder(s.api_url, s.audience),
    )


def serve(env: Mapping[str, str] = os.environ, stop: threading.Event | None = None) -> None:
    s = settings(env)
    server = make_server(create_receiver(env), "0.0.0.0", s.port)
    stop = stop or threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # not the main thread (a test)
            pass
    threading.Thread(target=server.serve_forever, daemon=True).start()
    event("github_hooks_serving", port=s.port)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        event("github_hooks_stopped")


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


__all__ = [
    "API_URL_ENV", "AUDIENCE_ENV", "DATABASE_ENV", "DEFAULT_PORT", "SECRET_ENV", "SECRET_ID",
    "Settings", "create_receiver", "main", "serve", "settings",
]
