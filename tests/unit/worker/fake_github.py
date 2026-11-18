"""GitHub at the transport seam of `forge.PinnedForgeClient`, for the worker actions' tests.

Nothing here reaches a network. A route is `(METHOD, path)` -> a callable
`(request) -> (status, headers, body)` or a fixed answer. Every request is
recorded with its method, its full URL and its headers, so a test can assert
where the token went (only the Authorization header) and what was asked.

The App key and every token are made at runtime (an RSA key generated per
process, `secrets.token_hex`), never written as a literal: the worker refuses
to publish a diff whose added lines look like a credential.
"""

from __future__ import annotations

import json
import secrets
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def app_key_pem() -> str:
    return _KEY.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def app_secret_payload(app_id: int) -> str:
    """What `create-secrets.sh --stdin` stores for a merge or review App."""
    return json.dumps({"app_id": app_id, "private_key": app_key_pem()})


def fresh_token() -> str:
    return secrets.token_hex(20)


@dataclass
class Seen:
    method: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.url).query)


Answer = tuple[int, dict[str, str], Any]


@dataclass
class FakeGitHub:
    routes: dict[tuple[str, str], Any] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    installation_id: int = 7
    token: str = field(default_factory=fresh_token)

    def __post_init__(self) -> None:
        self.routes.setdefault(("GET", "/repos/acme/widgets/installation"),
                               (200, {}, {"id": self.installation_id}))
        self.routes.setdefault(
            ("POST", f"/app/installations/{self.installation_id}/access_tokens"),
            lambda _req: (201, {}, {"token": self.token, "expires_at": "2026-10-02T13:00:00Z"}),
        )
        self.routes.setdefault(("DELETE", "/installation/token"), (204, {}, None))

    def route(self, method: str, path: str, answer: Any) -> None:
        self.routes[(method, path)] = answer

    def calls(self, method: str, path: str) -> list[Seen]:
        return [s for s in self.seen if s.method == method and s.path == path]

    def __call__(self, request: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
        body = json.loads(request.data.decode()) if request.data else None
        seen = Seen(request.get_method(), request.full_url, dict(request.header_items()), body)
        self.seen.append(seen)
        answer = self.routes.get((seen.method, seen.path))
        if answer is None:
            return 404, {}, json.dumps({"message": f"no fake route {seen.method} {seen.path}"}).encode()
        if callable(answer):
            answer = answer(seen)
        status, headers, data = answer
        raw = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        return status, headers, raw
