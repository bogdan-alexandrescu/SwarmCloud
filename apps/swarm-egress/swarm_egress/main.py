"""swarm-egress, the process (docs/browser-egress.md §2.2, §3.1, §3.5; lane EGRESS-SVC).

What it reads, all named by kubernetes/swarm-system/swarm-egress.yaml:

  * `SWARM_EGRESS_KEY_SECRET` -- the Secret Manager VERSION reference of
    `swarm-egress-key`, the key swarm-api MACs attempt tickets with. Read at
    start (no key, no start: a forwarder that can verify nothing must not look
    ready) and re-read every SECRET_POLL_SECONDS, so a rotation needs no
    restart (KeyRing keeps the previous value for a ticket's life);
  * `SWARM_EGRESS_CREDENTIAL_SECRET` -- the version reference of
    `swarm-egress-credential`, the vendor credential (vendor.py). Read the same
    way, but its absence is not fatal: until the owner has stored it, every
    CONNECT is refused 503 `no_credential` and the log says so;
  * `SWARM_EGRESS_CONFIG` -- the mounted ConfigMap file (config.py), re-read
    every CONFIG_POLL_SECONDS: the kill switch;
  * `SWARM_EGRESS_LEDGER` -- the byte ledger on the persistent volume
    (ledger.py).

Both secrets are read with this pod's own Workload Identity (its KSA is the
only accessor of the credential, terraform/infra/egress.tf), over REST with
google-auth, into memory. Never an environment value, a file or a log line.

Plain TCP on PORT inside the VPC: the hop from the gate carries only the
attempt ticket, and the NetworkPolicies admit only labelled browser pods. The
vendor hop uses TLS when the credential says the vendor offers it (§3.2).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import os
import re
import signal
from typing import Callable, Mapping

from .config import EgressConfig, load_config
from .ledger import Ledger
from .server import EgressServer, KeyRing, event
from .vendor import CredentialInvalid, VendorCredential, parse_credential

#: The forwarder's port; the Service, both NetworkPolicies and the gate's
#: upstream address name the same one (tests hold them together).
PORT = 3128
CONFIG_POLL_SECONDS = 5.0
SECRET_POLL_SECONDS = 300.0
CREDENTIAL_RETRY_SECONDS = 60.0
SUPERVISE_SECONDS = 1.0
MIN_KEY_BYTES = 32
DEFAULT_CONFIG = "/etc/swarm-egress/config.json"
DEFAULT_LEDGER = "/var/lib/swarm-egress/usage.json"
SECRET_VERSION = re.compile(r"projects/[^/\s]+/secrets/[^/\s]+/versions/[^/\s]+")
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


def _version(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not SECRET_VERSION.fullmatch(value):
        raise RuntimeError(f"{name} must be a Secret Manager version reference, "
                           "projects/<p>/secrets/<name>/versions/<v>")
    return value


def read_key(version: str, access: Callable[[str], bytes]) -> bytes:
    key = access(version).strip()
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError("swarm-egress-key is shorter than 32 bytes")
    return key


def read_credential(version: str, access: Callable[[str], bytes]) -> VendorCredential | None:
    try:
        return parse_credential(access(version))
    except CredentialInvalid as exc:
        event("egress_credential_invalid", cause=str(exc))
    except Exception as exc:  # noqa: BLE001 - absent until the owner stores it
        event("egress_credential_unreadable", cause=type(exc).__name__)
    return None


class _ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        self.current: EgressConfig = load_config(path)
        event("egress_config", enabled=self.current.enabled, reason=self.current.cause)

    def reload(self) -> None:
        new = load_config(self._path)
        if (new.enabled, new.cause) != (self.current.enabled, self.current.cause):
            event("egress_config", enabled=new.enabled, reason=new.cause)
        self.current = new


async def serve(env: Mapping[str, str] = os.environ,
                access: Callable[[str], bytes] = _access_secret) -> None:
    key_version = _version(env, "SWARM_EGRESS_KEY_SECRET")
    credential_version = _version(env, "SWARM_EGRESS_CREDENTIAL_SECRET")
    keys = KeyRing(read_key(key_version, access))
    holder: dict[str, VendorCredential | None] = {
        "credential": read_credential(credential_version, access)}
    config = _ConfigFile(env.get("SWARM_EGRESS_CONFIG") or DEFAULT_CONFIG)
    ledger = Ledger(env.get("SWARM_EGRESS_LEDGER") or DEFAULT_LEDGER)
    server = EgressServer(keys=keys, credential=lambda: holder["credential"],
                          config=lambda: config.current, ledger=ledger)

    async def watch_config() -> None:
        while True:
            await asyncio.sleep(CONFIG_POLL_SECONDS)
            config.reload()

    async def watch_secrets() -> None:
        while True:
            wait = SECRET_POLL_SECONDS if holder["credential"] else CREDENTIAL_RETRY_SECONDS
            await asyncio.sleep(wait)
            try:
                if keys.rotate(await asyncio.to_thread(read_key, key_version, access)):
                    event("egress_key_rotated")
            except Exception as exc:  # noqa: BLE001 - keep the key in hand
                event("egress_key_unreadable", cause=type(exc).__name__)
            fresh = await asyncio.to_thread(read_credential, credential_version, access)
            if fresh is not None:
                holder["credential"] = fresh

    listener = await asyncio.start_server(server.handle, "0.0.0.0", PORT)
    event("egress_serving", port=PORT, credential=holder["credential"] is not None)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(watch_config()), asyncio.create_task(watch_secrets()),
             asyncio.create_task(server.supervise(SUPERVISE_SECONDS))]
    try:
        await stop.wait()
    finally:
        listener.close()
        await server.close_all("shutdown")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event("egress_stopped")


def main() -> None:
    import logging

    try:
        from swarm_common.logging_setup import configure_logging

        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    except ImportError:
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(serve())


if __name__ == "__main__":
    main()


__all__ = ["PORT", "main", "read_credential", "read_key", "serve"]
