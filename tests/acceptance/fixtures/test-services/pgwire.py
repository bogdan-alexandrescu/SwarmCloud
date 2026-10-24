"""A PostgreSQL client over the wire protocol, standard library only.

Why not psycopg: the fixture's lock then carries a compiled driver per
platform, and a failure to fetch one would read as the very `failed` this
fixture exists to produce for another reason. The declared service trusts
loopback with no password (docs/design/test-services.md §3.1), so the
startup, a simple query and the rows it returns are all a test here needs.

It reads the address from the variables the pre-publish gate sets when the
declared Postgres answers (`PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`; §5.1)
and defaults to the same address, as a repository's own conftest does. A
refused connection says `could not connect to server at 127.0.0.1:5432`, the
shape the gate reads as an unreachable local service (#1605).
"""

from __future__ import annotations

import os
import socket
import struct

HOST = "127.0.0.1"
PORT = 5432
USER = "postgres"
DATABASE = "swarm_accept"

#: Protocol 3.0.
_PROTOCOL = 196608


class PgError(Exception):
    """An ErrorResponse from the server, or a reply this client cannot read."""


class Connection:
    def __init__(self, host: str, port: int, user: str, database: str,
                 timeout: float = 10.0) -> None:
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise ConnectionError(
                f"could not connect to server at {host}:{port}: {exc}") from exc
        params = b"".join(k.encode() + b"\0" + v.encode() + b"\0"
                          for k, v in (("user", user), ("database", database)))
        body = struct.pack("!i", _PROTOCOL) + params + b"\0"
        self._sock.sendall(struct.pack("!i", len(body) + 4) + body)
        while True:
            kind, payload = self._read()
            if kind == b"R":
                (code,) = struct.unpack("!i", payload[:4])
                if code != 0:
                    raise PgError(f"the server asked for authentication method {code}; "
                                  "the declared service trusts loopback")
            elif kind == b"E":
                raise PgError(_error_text(payload))
            elif kind == b"Z":
                return

    def query(self, sql: str) -> list[tuple[str | None, ...]]:
        """Run `sql` (one simple query) and return its rows as text."""
        body = sql.encode() + b"\0"
        self._sock.sendall(b"Q" + struct.pack("!i", len(body) + 4) + body)
        rows: list[tuple[str | None, ...]] = []
        error = None
        while True:
            kind, payload = self._read()
            if kind == b"D":
                rows.append(_row(payload))
            elif kind == b"E":
                error = _error_text(payload)
            elif kind == b"Z":
                if error is not None:
                    raise PgError(error)
                return rows

    def close(self) -> None:
        try:
            self._sock.sendall(b"X" + struct.pack("!i", 4))
        except OSError:
            pass
        self._sock.close()

    def _read(self) -> tuple[bytes, bytes]:
        head = self._exactly(5)
        (length,) = struct.unpack("!i", head[1:])
        return head[:1], self._exactly(length - 4)

    def _exactly(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise PgError("the server closed the connection")
            out += chunk
        return out


def _row(payload: bytes) -> tuple[str | None, ...]:
    (count,) = struct.unpack("!h", payload[:2])
    at, values = 2, []
    for _ in range(count):
        (size,) = struct.unpack("!i", payload[at:at + 4])
        at += 4
        if size < 0:
            values.append(None)
        else:
            values.append(payload[at:at + size].decode())
            at += size
    return tuple(values)


def _error_text(payload: bytes) -> str:
    fields = {part[:1]: part[1:].decode(errors="replace")
              for part in payload.split(b"\0") if part}
    return f"{fields.get(b'S', 'ERROR')}: {fields.get(b'M', 'unknown error')}"


def connect() -> Connection:
    """A connection to the address the gate names, or to the default one."""
    return Connection(os.environ.get("PGHOST") or HOST,
                      int(os.environ.get("PGPORT") or PORT),
                      os.environ.get("PGUSER") or USER,
                      os.environ.get("PGDATABASE") or DATABASE)
