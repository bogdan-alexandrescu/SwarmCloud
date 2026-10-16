"""The receiver: what one GitHub delivery gets (docs/design/graph-service.md §6.2).

This is the platform's one unauthenticated door, so it does exactly the
design's four things, in an order that spends nothing on a stranger:

  1. only `POST /github/events`, with a Content-Length of at most 1 MiB --
     refused from the header, before a byte of the body is read;
  2. a valid `X-Hub-Signature-256`: HMAC-SHA256 of the RAW body under the
     App's webhook secret, compared in constant time. An unsigned or badly
     signed request is a 401 that never reaches Firestore or swarm-api, and
     never learns which event it claimed. `ping` is then a 204, and any event
     but `push` a 204 that is dropped;
  3. only a non-deleting push to `refs/heads/<repository.default_branch>`
     (`deleted` exactly false), deduped on `X-GitHub-Delivery` with a
     Firestore `create()` of github_deliveries/{id} that expires in 7 days.
     A redelivery is a 200 and nothing more. The filter runs before the
     dedup, so a dropped push costs no write;
  4. swarm-api is told the repository's `full_name` and nothing else. The
     payload's `after` sha is never read: swarm-api re-reads the head with
     each workspace's own credential (`RepoIndex.push`).

THE SECRET is read lazily and cached. On a mismatch it is re-read at most once
a minute, so a rotation is picked up at the first delivery signed with the new
value, and a stranger's bad signatures cannot become a stream of Secret Manager
reads. Unreadable (stored after the service was deployed), a signed request is
a 503 and the next read waits the same minute.

ONE LOG LINE per delivery: the outcome, the status and the delivery id once it
has been validated. Never the payload, the repository, a ref or another
header's value.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from swarm_redaction import redact

log = logging.getLogger("swarm_github_hooks")

PATH = "/github/events"
#: §6.2: "a body of at most 1 MiB". GitHub caps a payload at 25 MB, and a push
#: payload past 1 MiB (thousands of commits) is one the poll handles anyway.
MAX_BODY_BYTES = 1024 * 1024
#: §6.2: "expires_at = now + 7 days", under the Firestore TTL policy
#: terraform/infra/github_hooks.tf sets on github_deliveries.expires_at.
DELIVERY_TTL = timedelta(days=7)
#: How often a mismatch may re-read the secret (owner brief: "at most once a minute").
SECRET_REFRESH_SECONDS = 60.0
COLLECTION = "github_deliveries"

#: GitHub's delivery id is a UUID. Anything this pattern refuses never becomes
#: a Firestore document name or a log field.
_DELIVERY = re.compile(r"[A-Za-z0-9-]{8,64}")
#: GitHub's own spelling of `owner/repo` (the shape swarm-api's
#: `repositories.parse_repository` accepts; it re-checks).
_FULL_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
_SIGNATURE = re.compile(r"sha256=([0-9a-f]{64})")

BodyReader = Callable[[int], bytes]


class StoreUnavailable(Exception):
    """Firestore did not answer created-or-exists. The message is a cause
    word, never its answer body."""


class Deliveries(Protocol):
    def create(self, delivery_id: str, expires_at: datetime) -> bool:
        """True when created; False when the id was already there."""


def event(name: str, **fields: object) -> None:
    """One JSON log line, redacted, with only the fields a caller passes."""
    log.info(redact(json.dumps({"event": name, **fields}, sort_keys=True)).text)


class WebhookSecret:
    """The App's webhook secret, read through `read` (Secret Manager's
    `latest`) the first time it is needed and cached. `refresh()` re-reads it
    at most once every SECRET_REFRESH_SECONDS. No repr carries the value."""

    def __init__(self, read: Callable[[], bytes], *,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._read = read
        self._monotonic = monotonic
        self._value: bytes | None = None
        self._read_at: float | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "WebhookSecret(<hidden>)"

    def _load(self) -> None:
        self._read_at = self._monotonic()
        try:
            value = self._read()
        except Exception as exc:  # noqa: BLE001 - keep what is in hand
            event("github_secret_unreadable", cause=type(exc).__name__)
            return
        if value:
            self._value = value

    def current(self) -> bytes | None:
        with self._lock:
            if self._value is None and self._due():
                self._load()
            return self._value

    def refresh(self) -> bytes | None:
        """The value after a re-read, or None when the minute has not passed
        (or the read failed and nothing new is known)."""
        with self._lock:
            if not self._due():
                return None
            before = self._value
            self._load()
            return self._value if self._value != before else None

    def _due(self) -> bool:
        return self._read_at is None or self._monotonic() - self._read_at >= SECRET_REFRESH_SECONDS


@dataclass(frozen=True)
class Reply:
    status: int
    outcome: str

    @property
    def body(self) -> bytes:
        if self.status == 204:
            return b""
        return json.dumps({"outcome": self.outcome}).encode()


_STATUS = {
    "not_found": 404,
    "length_required": 411,
    "too_large": 413,
    "short_body": 400,
    "unsigned": 401,
    "bad_signature": 401,
    "secret_unavailable": 503,
    "ping": 204,
    "ignored_event": 204,
    "bad_payload": 400,
    "not_default_branch": 204,
    "deleted": 204,
    "bad_delivery": 400,
    "store_failed": 503,
    "duplicate": 200,
    "forwarded": 200,
    "forward_failed": 502,
}


def _header(headers: Iterable[tuple[str, str]], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers:
        if key.lower() == wanted:
            return value
    return None


class Receiver:
    def __init__(self, *, secret: WebhookSecret, deliveries: Deliveries,
                 forward: Callable[[str], int],
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._secret = secret
        self._deliveries = deliveries
        self._forward = forward
        self._clock = clock

    def handle(self, method: str, path: str, headers: Iterable[tuple[str, str]] | Mapping[str, str],
               read: BodyReader) -> Reply:
        pairs = list(headers.items()) if isinstance(headers, Mapping) else list(headers)
        outcome, delivery = self._judge(method, path, pairs, read)
        reply = Reply(_STATUS[outcome], outcome)
        event("github_delivery", delivery=delivery, outcome=outcome, status=reply.status)
        return reply

    def _judge(self, method: str, path: str, headers: list[tuple[str, str]],
               read: BodyReader) -> tuple[str, str]:
        if method != "POST" or path != PATH:
            return "not_found", ""
        length_text = (_header(headers, "Content-Length") or "").strip()
        if not length_text.isdigit():
            return "length_required", ""
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            return "too_large", ""
        body = read(length) if length else b""
        if len(body) != length:
            return "short_body", ""

        signature = (_header(headers, "X-Hub-Signature-256") or "").strip()
        if not signature:
            return "unsigned", ""
        verdict = self._verify(signature, body)
        if verdict != "ok":
            return verdict, ""

        # Signed by the App from here on: the delivery id may be logged once
        # it has the shape GitHub gives it.
        raw_delivery = (_header(headers, "X-GitHub-Delivery") or "").strip()
        delivery = raw_delivery if _DELIVERY.fullmatch(raw_delivery) else ""

        kind = _header(headers, "X-GitHub-Event")
        if kind == "ping":
            return "ping", delivery
        if kind != "push":
            return "ignored_event", delivery

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return "bad_payload", delivery
        verdict, full_name = _push_target(payload)
        if verdict != "keep":
            return verdict, delivery

        if not delivery:
            return "bad_delivery", ""
        try:
            created = self._deliveries.create(delivery, self._clock() + DELIVERY_TTL)
        except Exception as exc:  # noqa: BLE001 - never a traceback quoting the request
            event("github_delivery_store", delivery=delivery, cause=type(exc).__name__)
            return "store_failed", delivery
        if not created:
            return "duplicate", delivery

        try:
            status = self._forward(full_name)
        except Exception as exc:  # noqa: BLE001 - the poll is the fallback
            event("github_delivery_forward", delivery=delivery, cause=type(exc).__name__)
            return "forward_failed", delivery
        if not 200 <= status < 300:
            event("github_delivery_forward", delivery=delivery, cause=f"http_{status}")
            return "forward_failed", delivery
        return "forwarded", delivery

    def _verify(self, signature: str, body: bytes) -> str:
        matched = _SIGNATURE.fullmatch(signature)
        if matched is None:
            return "bad_signature"
        given = matched.group(1)
        secret = self._secret.current()
        if secret is None:
            secret = self._secret.refresh()
            if secret is None:
                return "secret_unavailable"
        if _matches(secret, body, given):
            return "ok"
        # A rotation: the App signs with the new value before this instance
        # has read it. At most one re-read a minute (WebhookSecret.refresh).
        fresh = self._secret.refresh()
        if fresh is not None and _matches(fresh, body, given):
            return "ok"
        return "bad_signature"


def _matches(secret: bytes, body: bytes, given: str) -> bool:
    computed = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, given)


def _push_target(payload: Any) -> tuple[str, str]:
    """("keep", full_name) for a non-deleting push to the default branch, or
    the outcome that drops it."""
    if not isinstance(payload, dict):
        return "bad_payload", ""
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return "bad_payload", ""
    full_name = repository.get("full_name")
    default_branch = repository.get("default_branch")
    ref = payload.get("ref")
    if not isinstance(full_name, str) or not _FULL_NAME.fullmatch(full_name):
        return "bad_payload", ""
    if not isinstance(default_branch, str) or not default_branch or not isinstance(ref, str):
        return "bad_payload", ""
    if ref != "refs/heads/" + default_branch:
        return "not_default_branch", ""
    # Exactly false: a deletion, a missing flag or a stringly one is dropped.
    if payload.get("deleted") is not False:
        return "deleted", ""
    return "keep", full_name


__all__ = [
    "COLLECTION", "DELIVERY_TTL", "MAX_BODY_BYTES", "PATH", "SECRET_REFRESH_SECONDS",
    "Deliveries", "Receiver", "Reply", "StoreUnavailable", "WebhookSecret", "event",
]
