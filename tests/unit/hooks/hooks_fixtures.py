"""What the receiver's tests share: a signed delivery, a recording store and a
recording forwarder. Every secret is built at runtime (`secrets.token_hex`),
never written here as a literal."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def new_secret() -> bytes:
    return secrets.token_hex(20).encode()


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def push_payload(*, full_name: str = "Saga-XYZ/Widgets", ref: str = "refs/heads/main",
                 default_branch: str = "main", deleted: Any = False,
                 after: str | None = None) -> dict[str, Any]:
    return {
        "ref": ref,
        "before": "1" * 40,
        "after": after or secrets.token_hex(20),
        "deleted": deleted,
        "created": False,
        "repository": {
            "id": 1,
            "full_name": full_name,
            "default_branch": default_branch,
            "private": True,
        },
        "pusher": {"name": "someone"},
    }


@dataclass
class Delivery:
    """One request as GitHub sends it."""

    secret: bytes
    body: bytes
    event: str = "push"
    delivery: str = field(default_factory=lambda: str(uuid.uuid4()))
    signature: str | None = None
    method: str = "POST"
    path: str = "/github/events"
    length: int | None = None

    def headers(self) -> list[tuple[str, str]]:
        headers = [("Content-Type", "application/json"),
                   ("User-Agent", "GitHub-Hookshot/abc")]
        if self.event:
            headers.append(("X-GitHub-Event", self.event))
        if self.delivery:
            headers.append(("X-GitHub-Delivery", self.delivery))
        signature = self.signature if self.signature is not None else sign(self.secret, self.body)
        if signature:
            headers.append(("X-Hub-Signature-256", signature))
        headers.append(("Content-Length", str(len(self.body) if self.length is None else self.length)))
        return headers


def push(secret: bytes, **payload: Any) -> Delivery:
    return Delivery(secret=secret, body=json.dumps(push_payload(**payload)).encode())


class Store:
    """`github_deliveries`, in memory: create() fails on an existing id."""

    def __init__(self) -> None:
        self.docs: dict[str, datetime] = {}
        self.calls: list[str] = []
        self.fail = False

    def create(self, delivery_id: str, expires_at: datetime) -> bool:
        self.calls.append(delivery_id)
        if self.fail:
            from swarm_github_hooks.receiver import StoreUnavailable

            raise StoreUnavailable("http_503")
        if delivery_id in self.docs:
            return False
        self.docs[delivery_id] = expires_at
        return True


class Forward:
    """swarm-api's push route, recording every call."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append(args + tuple(sorted(kwargs.items())))
        return self.status


class Secret:
    """Secret Manager's `latest` for the webhook secret, recording reads."""

    def __init__(self, value: bytes | None) -> None:
        self.value = value
        self.reads = 0

    def __call__(self) -> bytes:
        self.reads += 1
        if self.value is None:
            raise RuntimeError("Secret Manager answered HTTP 404")
        return self.value
