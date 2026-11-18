"""Where a person's sign-in is kept: the platform's credential store, keyed by context.

TWO THINGS LIVE HERE, per context, and nothing else:

    <context>/refresh-token         the person's own, from `sc login`
    <context>/oauth-client-secret   the deployment's Desktop OAuth client secret

Neither goes in the config file (`config.py` writes only a URL and a client
id), and neither is ever printed: `sc context list` says WHETHER a context is
signed in, never with what.

THREE BACKENDS, chosen in this order, and the one in use is always stated --
`sc login` and `sc whoami` print `describe()` -- because "where is my refresh
token" must have an answer a person can check:

  * macOS: the login Keychain, through `/usr/bin/security`. The secret goes to
    `security -i` on STDIN, never in argv, because argv is what `ps` shows.
    `security -i` reads one command per LINE, so a value is refused unless it
    is made only of the characters Google's tokens and client secrets are
    made of -- a newline in a value would otherwise be a second command, run
    with the keychain's authority.
  * Linux with a Secret Service (GNOME Keyring, KWallet): `secret-tool`, which
    reads the secret from stdin by design.
  * Anywhere else: a 0600 JSON file in the config directory, in a 0700
    directory. That is weaker than a keyring -- anything running as this user
    can read it -- and `describe()` says so rather than letting it pass as one.

`SWARM_CREDENTIAL_STORE=keychain|secret-service|file` forces a choice; the
test suite forces `file` so no test ever touches a real keychain.

NO THIRD-PARTY DEPENDENCY, for the reason `pyproject.toml` gives for the whole
package: `keyring` would be the obvious import, and it would mean installing
something before a person can sign in to their own platform. The two system
tools do the same job and are already there.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from .client import SwarmError

SERVICE = "swarmcloud"
REFRESH_TOKEN = "refresh-token"
CLIENT_SECRET = "oauth-client-secret"

ENV_STORE = "SWARM_CREDENTIAL_STORE"

#: What a value handed to `security -i` may contain. Google refresh tokens are
#: `1//` plus base64url; Desktop client secrets are `GOCSPX-` plus base64url.
#: Anything outside this set -- a quote, a backslash, whitespace, a newline --
#: could end the quoted argument or the command, so it is refused, not escaped.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_SAFE_ACCOUNT = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")


def key(context: str, kind: str) -> str:
    return f"{context}/{kind}"


class Store:
    """The interface every backend keeps. `get` returns None for absent."""

    name = "store"

    def describe(self) -> str:  # pragma: no cover - overridden
        return self.name

    def get(self, key: str) -> str | None:  # pragma: no cover - overridden
        raise SwarmError(f"{self.name} cannot read")

    def set(self, key: str, value: str) -> None:  # pragma: no cover - overridden
        raise SwarmError(f"{self.name} cannot write")

    def delete(self, key: str) -> bool:  # pragma: no cover - overridden
        raise SwarmError(f"{self.name} cannot delete")


def _run(argv: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError as exc:
        raise SwarmError(f"{argv[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        # A locked keychain can sit behind a GUI prompt nobody is looking at.
        raise SwarmError(
            f"{argv[0]} did not answer within 30s -- is the keychain locked behind "
            "a prompt? Unlock it, or set SWARM_CREDENTIAL_STORE=file"
        ) from exc


class KeychainStore(Store):
    """The macOS login Keychain, as generic passwords under service `swarmcloud`."""

    name = "keychain"

    def describe(self) -> str:
        return f"the macOS Keychain (service {SERVICE!r})"

    @staticmethod
    def _account(key: str) -> str:
        if not _SAFE_ACCOUNT.match(key):
            raise SwarmError(f"{key!r} cannot name a keychain item")
        return key

    def get(self, key: str) -> str | None:
        done = _run(["security", "find-generic-password", "-s", SERVICE, "-a", self._account(key), "-w"])
        if done.returncode == 44:  # errSecItemNotFound, as `security` exits
            return None
        if done.returncode != 0:
            raise SwarmError(f"the Keychain refused a read: {done.stderr.strip()[:200]}")
        return done.stdout.rstrip("\n") or None

    def set(self, key: str, value: str) -> None:
        account = self._account(key)
        if not _SAFE_VALUE.match(value or ""):
            raise SwarmError(
                "refusing to store a value with characters outside [A-Za-z0-9._~+/=-]: "
                "`security -i` reads one command per line, and such a value could "
                "end this one. Google's refresh tokens and client secrets never "
                "contain them -- check what was pasted"
            )
        # -U updates an item that already exists instead of failing on it.
        command = f'add-generic-password -U -s "{SERVICE}" -a "{account}" -w "{value}"\n'
        done = _run(["security", "-i"], stdin=command)
        if done.returncode != 0:
            raise SwarmError(f"the Keychain refused a write: {done.stderr.strip()[:200]}")

    def delete(self, key: str) -> bool:
        done = _run(["security", "delete-generic-password", "-s", SERVICE, "-a", self._account(key)])
        return done.returncode == 0


class SecretServiceStore(Store):
    """The freedesktop Secret Service (GNOME Keyring, KWallet), via `secret-tool`."""

    name = "secret-service"

    def describe(self) -> str:
        return f"the Secret Service keyring (service {SERVICE!r})"

    def get(self, key: str) -> str | None:
        done = _run(["secret-tool", "lookup", "service", SERVICE, "account", key])
        if done.returncode != 0:
            # secret-tool exits 1 for "not found" and for most failures alike;
            # an empty stdout is the only portable "absent".
            if done.stderr.strip():
                raise SwarmError(f"the Secret Service refused a read: {done.stderr.strip()[:200]}")
            return None
        return done.stdout.rstrip("\n") or None

    def set(self, key: str, value: str) -> None:
        done = _run(
            ["secret-tool", "store", f"--label=SwarmCloud {key}", "service", SERVICE, "account", key],
            stdin=value,
        )
        if done.returncode != 0:
            raise SwarmError(
                f"the Secret Service refused a write: {done.stderr.strip()[:200]}. "
                "Set SWARM_CREDENTIAL_STORE=file to use a 0600 file instead"
            )

    def delete(self, key: str) -> bool:
        return _run(["secret-tool", "clear", "service", SERVICE, "account", key]).returncode == 0


class FileStore(Store):
    """A 0600 JSON file. The fallback, and said to be one."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def describe(self) -> str:
        return (
            f"the file {self.path} (0600; readable by anything running as you -- "
            "no system keyring was available)"
        )

    def _load(self) -> dict[str, str]:
        try:
            body = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise SwarmError(f"could not read {self.path}: {exc}") from exc
        return {k: v for k, v in body.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, items: dict[str, str]) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(items, handle, indent=2, sort_keys=True)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        items = self._load()
        items[key] = value
        self._save(items)

    def delete(self, key: str) -> bool:
        items = self._load()
        if key not in items:
            return False
        del items[key]
        self._save(items)
        return True


def store(environ: Mapping[str, str] | None = None) -> Store:
    """The credential store for this machine: forced, or the best one present."""
    from .config import config_dir

    environ = os.environ if environ is None else environ
    forced = environ.get(ENV_STORE, "").strip().lower()
    file_store = FileStore(config_dir(environ) / "credentials.json")
    if forced == "file":
        return file_store
    if forced == "keychain":
        return KeychainStore()
    if forced == "secret-service":
        return SecretServiceStore()
    if forced:
        raise SwarmError(f"{ENV_STORE}={forced!r}: expected keychain, secret-service or file")
    if sys.platform == "darwin" and shutil.which("security"):
        return KeychainStore()
    if shutil.which("secret-tool") and environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return SecretServiceStore()
    return file_store
