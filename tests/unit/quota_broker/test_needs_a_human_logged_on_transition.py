"""A dead credential's "needs a human" is said on the transition, not every sweep (#1822).

MEASURED: the quota broker logged "subscription credential needs a human; tasks
will park" every five minutes for the same account, 576 times in 48 hours. The
fact had not changed since the first line; the account's own state already said
it. Pinned here:

  N-1  The line is an ERROR on the sweep that finds the credential dead, and is
       NOT repeated on the next sweep.
  N-2  While it stays dead it is repeated at most once an hour.
  N-3  A credential that works again re-arms it: dying again is a transition.
"""

from __future__ import annotations

import itertools
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from quota_broker.credentials import REFRESH_SUFFIX, CredentialRefresher
from quota_broker.oauth import ReauthRequired

NEEDS_A_HUMAN = "subscription credential needs a human; tasks will park"
START = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
BASE = "swarm-tenant-eng-anthropic"

_SEQ = itertools.count()


class _Capture(logging.Handler):
    """A real handler behind a real logging.Logger, as production logs."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _logger() -> tuple[logging.Logger, _Capture]:
    logger = logging.getLogger(f"test.needs_a_human.{next(_SEQ)}")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    capture = _Capture()
    logger.addHandler(capture)
    return logger, capture


class _Store:
    def __init__(self, data: dict[str, str]) -> None:
        self.data = dict(data)

    def access(self, name: str) -> str:
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def add_version(self, name: str, payload: str) -> None:
        self.data[name] = payload


class _Endpoint:
    """Refuses with invalid_grant until `revive()`; then exchanges."""

    def __init__(self) -> None:
        self.dead = True

    def exchange(self, refresh_token: str) -> dict[str, object]:
        if self.dead:
            raise ReauthRequired("invalid_grant")
        return {
            "access_token": secrets.token_hex(16),
            "refresh_token": secrets.token_hex(16),
            "expires_in": 8 * 3600,
        }


def _stored(now: datetime) -> str:
    """A refresh credential inside the refresh window, built at runtime."""
    return json.dumps({
        "accessToken": secrets.token_hex(16),
        "refreshToken": secrets.token_hex(16),
        "expiresAt": int((now + timedelta(minutes=30)).timestamp() * 1000),
    })


class _World:
    def __init__(self) -> None:
        self.now = START
        self.store = _Store({BASE + REFRESH_SUFFIX: _stored(START)})
        self.endpoint = _Endpoint()
        logger, self.capture = _logger()
        self.refresher = CredentialRefresher(
            self.store, self.endpoint, logger=logger, now=lambda: self.now
        )

    def sweep(self, *, at_minutes: float) -> str:
        self.now = START + timedelta(minutes=at_minutes)
        # A refresh that worked wrote a fresh expiry; put it back inside the
        # window so every sweep exchanges, as a dead account's does.
        self.store.data[BASE + REFRESH_SUFFIX] = _stored(self.now)
        return self.refresher.refresh_tenant("eng", "anthropic").reason

    def errors(self) -> list[logging.LogRecord]:
        return [
            r for r in self.capture.records
            if r.getMessage() == NEEDS_A_HUMAN and r.levelno == logging.ERROR
        ]


def test_needs_a_human_is_logged_on_the_transition_and_not_on_the_next_sweep():
    """N-1."""
    world = _World()

    assert world.sweep(at_minutes=0) == "reauth_required"
    # MUTATION: never logging it (deleting the error call) leaves this at 0.
    assert len(world.errors()) == 1

    assert world.sweep(at_minutes=5) == "reauth_required"
    assert world.sweep(at_minutes=10) == "reauth_required"
    # MUTATION: logging on every sweep, as before #1822, makes this 3.
    assert len(world.errors()) == 1, "repeated on the next sweep"


def test_needs_a_human_is_repeated_at_most_once_an_hour_while_it_lasts():
    """N-2."""
    world = _World()
    for minute in range(0, 60, 5):
        world.sweep(at_minutes=minute)
    # MUTATION: a repeat interval shorter than an hour makes this more than 1.
    assert len(world.errors()) == 1

    world.sweep(at_minutes=60)
    world.sweep(at_minutes=65)
    # MUTATION: never repeating it (no hourly reminder) leaves this at 1.
    assert len(world.errors()) == 2
    assert world.errors()[1].__dict__.get("repeated") is True


def test_a_credential_that_works_again_rearms_the_line():
    """N-3."""
    world = _World()
    world.sweep(at_minutes=0)

    world.endpoint.dead = False
    assert world.sweep(at_minutes=5) == "refreshed"
    world.endpoint.dead = True
    assert world.sweep(at_minutes=10) == "reauth_required"
    # MUTATION: never clearing the memory on recovery keeps this at 1 until the
    # hour is up, hiding that the credential died a second time.
    assert len(world.errors()) == 2
    assert world.errors()[1].__dict__.get("repeated") is False
