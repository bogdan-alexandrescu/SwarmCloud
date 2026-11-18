"""The plugins bucket, through the Cloud Storage JSON API: the job's one channel out.

The job's account may get objects under `quarantine/` and create objects under
`staging/`, and nothing else: no list, no delete (terraform/infra/plugins.tf;
docs/design/managed-plugins.md §2.2-§2.3). So this module makes exactly those
two calls, one object GET of `quarantine/<intake_id>` and object creates under
`staging/<intake_id>/` with `ifGenerationMatch=0` (create-only, as the grant
is), plus the execution's access token from the metadata server. No mount: a
Cloud Storage FUSE volume looks directories up by listing them, which the
account must not do.

The job runs with all traffic through its own network, which has no NAT and
reaches only Google's APIs through Private Google Access, so these two hosts
are the only ones a socket could reach anyway. Inside the program it is
narrower still: `nonet.install()` replaces every socket function the standard
library offers, and this module connects through the originals it captured at
import, to `metadata.google.internal` and `storage.googleapis.com` only. Any
other code path, and any other host, is refused.

The access token lives in this object for the execution's life; it is never
logged, never written and never part of an exception's text.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from urllib.parse import quote

# The originals, before `nonet.install()` replaces the module's attributes.
_SOCKET = socket.socket
_GETADDRINFO = socket.getaddrinfo

METADATA_HOST = "metadata.google.internal"
STORAGE_HOST = "storage.googleapis.com"
_ALLOWED = {(METADATA_HOST, 80), (STORAGE_HOST, 443)}
_TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"
_TIMEOUT = 60.0
_CHUNK = 1024 * 1024


class BucketError(Exception):
    """A Cloud Storage or metadata call failed. Its class is the only thing logged."""


class ObjectNotFound(BucketError):
    """404: the quarantine object is not there."""


class AccessDenied(BucketError):
    """401 or 403: the account may not make this call."""


class ObjectExists(BucketError):
    """412: a create met an object already at that name (create-only)."""


class StorageUnavailable(BucketError):
    """Any other status, or a response that is not what the API documents."""


class HostRefused(BucketError):
    """A connection to a host other than the two this module talks to."""


def _raise_for(status: int) -> None:
    if status == 404:
        raise ObjectNotFound()
    if status in (401, 403):
        raise AccessDenied()
    if status == 412:
        raise ObjectExists()
    raise StorageUnavailable()


def _connect(address: tuple[str, int], timeout: float | None = None, source_address: object = None) -> socket.socket:
    """`socket.create_connection` for the two allowed hosts, from the captured originals."""
    if (address[0], address[1]) not in _ALLOWED:
        raise HostRefused()
    last: OSError | None = None
    for family, kind, proto, _name, sockaddr in _GETADDRINFO(address[0], address[1], 0, socket.SOCK_STREAM):
        sock = _SOCKET(family, kind, proto)
        try:
            sock.settimeout(timeout if timeout is not None else _TIMEOUT)
            sock.connect(sockaddr)
            return sock
        except OSError as error:
            last = error
            sock.close()
    raise last if last is not None else StorageUnavailable()


def _connection(host: str) -> http.client.HTTPConnection:
    if host == STORAGE_HOST:
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, 443, timeout=_TIMEOUT, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, 80, timeout=_TIMEOUT)
    # http.client reads `socket.create_connection` when the connection is made,
    # which `nonet` has replaced by then: this module's own connect instead.
    conn._create_connection = _connect  # type: ignore[attr-defined]
    return conn


class Bucket:
    """One bucket, read and written as the execution's service account."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._token: str | None = None

    def _authorization(self) -> str:
        if self._token is None:
            conn = _connection(METADATA_HOST)
            try:
                conn.request("GET", _TOKEN_PATH, headers={"Metadata-Flavor": "Google"})
                response = conn.getresponse()
                body = response.read(64 * 1024)
                if response.status != 200:
                    _raise_for(response.status)
            finally:
                conn.close()
            try:
                token = json.loads(body)["access_token"]
            except (ValueError, KeyError, TypeError):
                raise StorageUnavailable() from None
            if not isinstance(token, str) or not token:
                raise StorageUnavailable()
            self._token = token
        return f"Bearer {self._token}"

    def read(self, name: str, cap: int) -> tuple[bytes | None, int]:
        """One GET of the object's bytes: `(data, size)`, or `(None, size)` past `cap`.

        Never reads more than `cap + 1` bytes: an object over the cap is
        refused from its stored length, before its bytes are read.
        """
        path = f"/storage/v1/b/{quote(self.name, safe='')}/o/{quote(name, safe='')}?alt=media"
        conn = _connection(STORAGE_HOST)
        try:
            # `identity`: the stored bytes, never a transcoded copy.
            conn.request("GET", path, headers={"Authorization": self._authorization(), "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status != 200:
                _raise_for(response.status)
            stored = response.getheader("x-goog-stored-content-length") or response.getheader("Content-Length")
            size = int(stored) if stored and stored.isdigit() else -1
            if size > cap:
                return None, size
            chunks: list[bytes] = []
            total = 0
            while total <= cap:
                chunk = response.read(min(_CHUNK, cap + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > cap:
                return None, total
            return b"".join(chunks), total
        finally:
            conn.close()

    def create(self, name: str, data: bytes, content_type: str) -> None:
        """Create the object, never overwriting one (`ifGenerationMatch=0`)."""
        path = f"/upload/storage/v1/b/{quote(self.name, safe='')}/o?uploadType=media&ifGenerationMatch=0&name={quote(name, safe='')}"
        conn = _connection(STORAGE_HOST)
        try:
            headers = {"Authorization": self._authorization(), "Content-Type": content_type, "Content-Length": str(len(data))}
            conn.request("POST", path, body=data, headers=headers)
            response = conn.getresponse()
            response.read(64 * 1024)
            if response.status != 200:
                _raise_for(response.status)
        finally:
            conn.close()
