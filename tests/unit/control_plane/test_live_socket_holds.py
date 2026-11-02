"""A viewer's socket outlives a failed session read (LIVE-STREAM-SPEED, part of #2688).

MEASURED 2026-11-02 about 04:00Z in the owner's Chrome: on an idle browser
agent the Browser tab minted a new viewer ticket every 5-17 s and said "The
live connection closed. Reconnecting with a new ticket", while the API's reads
were slow (p95 16.67 s). swarm-api's socket proxy reads the session every
`live_session_poll_seconds` (`_Proxy._tick`) and caught only `ApiError` from
that read: any other failure -- a Firestore transaction aborted under
contention, a deadline -- ended the tick task, and with it the socket. Each
close cost the viewer a ticket, a new relay link, a full first frame and the
picture.

A read that fails is a read that failed: the socket stays open, the frames
keep flowing, and the next tick reads again. A failure that persists past
`STATUS_FAILURES_MAX` ticks in a row still closes the socket (the
session can no longer be checked; the viewer reconnects and is checked
afresh), so the fencing generation and the tenant scope are still enforced on
the next read that succeeds.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from swarm_api import live as api
from swarm_api.routes import live as live_routes

from .test_live_api import (  # noqa: F401  (fixtures)
    READY,
    SOCKET,
    Attempt,
    FakeLink,
    audit,
    FakeRelay,
    bearer,
    clock,
    groups,
    ldb,
    live,
    make_client,
    open_session,
    people,
    seed_running,
    ticket_for,
    wait_until,
)


class _LateFrameRelay(FakeRelay):
    """A relay whose link sends one binary frame when the test says so."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.frame = bytes(range(16))

    async def connect(self, url: str) -> FakeLink:
        release, frame = self.release, self.frame

        class Link(FakeLink):
            async def recv(self) -> Any:
                if self._frames:
                    return self._frames.pop(0)
                while not release.is_set() and not self.closed:
                    await asyncio.sleep(0.01)
                if self.closed:
                    return None
                release.clear()
                return frame

        link = Link(url, [READY])
        self.links.append(link)
        return link


def _failing_status(monkeypatch, fail_from: int, fail_count: int) -> list[int]:
    """`LiveService.status` raising a non-API error on reads fail_from..fail_from+fail_count-1."""
    real = api.LiveService.status
    calls: list[int] = []

    def status(self, admission):
        calls.append(1)
        n = len(calls)
        if fail_from <= n < fail_from + fail_count:
            raise RuntimeError("409 Aborted: too much contention on these documents")
        return real(self, admission)

    monkeypatch.setattr(api.LiveService, "status", status)
    return calls


def test_a_failed_session_read_does_not_close_the_viewer(ldb, people, groups, objects, clock,
                                                         monkeypatch):
    relay = _LateFrameRelay()
    client = make_client(ldb, people, groups, objects, clock, relay,
                         live_session_poll_seconds=0.02,
                         live_membership_recheck_seconds=300.0)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    assert open_session(client, attempt).status_code == 201
    # The read at join succeeds; the next three, on the ticker, fail.
    calls = _failing_status(monkeypatch, fail_from=2, fail_count=3)
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        # Generous: under a loaded parallel run the 0.02 s ticker's reads go
        # through a busy threadpool, and 3 s saw only four of them.
        try:
            assert wait_until(lambda: len(calls) >= 6, seconds=30.0)
        finally:
            relay.release.set()
        # Still the same socket: the relay's frame reaches the viewer.
        assert ws.receive_bytes() == relay.frame
        # Inside the session: leaving the `with` cancels the app's task, and
        # under load that cancelled `_Proxy.run`'s teardown mid-await
        # (CancelledError at this `with`, as test_live_api's socket-hold test
        # records). Its last write is the `viewer_left` row: wait for it.
        ws.close(1000)
        assert wait_until(lambda: len(audit(ldb, "viewer_left")) == 1, seconds=30.0)
    assert len(relay.links) == 1


def test_a_session_that_cannot_be_read_for_long_still_closes(ldb, people, groups, objects, clock,
                                                             monkeypatch):
    relay = _LateFrameRelay()
    client = make_client(ldb, people, groups, objects, clock, relay,
                         live_session_poll_seconds=0.02,
                         live_membership_recheck_seconds=300.0)
    monkeypatch.setattr(live_routes, "STATUS_FAILURES_MAX", 3)
    attempt = Attempt()
    seed_running(client, ldb, attempt)
    assert open_session(client, attempt).status_code == 201
    _failing_status(monkeypatch, fail_from=2, fail_count=1000)
    ticket = ticket_for(client, "alice")
    with client.websocket_connect(SOCKET, headers=bearer("alice")) as ws:
        ws.send_text(json.dumps({"ticket": ticket}))
        assert ws.receive_json()["open"] is True
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
        # The app writes `viewer_left` after the close; wait for it before
        # leaving the `with` cancels the app's task (as above).
        assert wait_until(lambda: len(audit(ldb, "viewer_left")) == 1, seconds=30.0)
