"""A Live Browsers thumbnail is not a watcher for the recorder (LIVE-THUMBS-SMALL-JPEG, part of #2688).

Every card on the Live Browsers wall is a viewer link. Before this, each one
raised its session's recording to `WATCHED_FPS` (10), so a wall of twenty
cards recorded twenty sessions at 10 fps that nobody was watching closely. A
card asks the gate for its thumbnail mode (rfb.py `ENCODING_THUMB_WIDTH_0 +
width` in its SetEncodings); the relay reads that from the link's own client
messages and does not count the link as a watcher. A session watched only by
thumbnails records at `IDLE_FPS`; a Browser tab raises it to 10. The caps
count every link, thumbnails included, as before.

MUTATIONS: count every viewer as a watcher (1 fails); read the first byte of
each chunk instead of framing the stream (2 fails); read inside pasted text
(3 fails); drop thumbnails from the caps (4 fails).
"""

from __future__ import annotations

import secrets
import struct

from test_relay import Clock, Endpoint, Encoder, FakeGate, Writer, run, ticket

from swarm_live import recorder as rec
from swarm_live.rendezvous import (
    CLOSE_FULL,
    ENCODING_THUMB_WIDTH_0,
    OP_DATA,
    Refused,
    Rendezvous,
    encode_frame,
)

PREAMBLE = b"RFB 003.008\n" + bytes((1, 1))  # version, security None, ClientInit shared
PIXEL_FORMAT = bytes((0, 0, 0, 0)) + bytes(16)
REQUEST = struct.pack(">BBHHHH", 3, 0, 0, 0, 640, 400)


def set_encodings(*encodings: int) -> bytes:
    return struct.pack(f">BxH{len(encodings)}i", 2, len(encodings), *encodings)


THUMB = set_encodings(7, 0, -223, ENCODING_THUMB_WIDTH_0 + 480)
FULL = set_encodings(7, -223, -239, -254)


def _relay():
    clock = Clock()
    key = secrets.token_bytes(32)
    relay = Rendezvous(key, clock=clock,
                       recorder_factory=lambda s, send: rec.Recorder(
                           tenant_id=s.tenant_id, task_id=s.task_id, attempt_id=s.attempt_id,
                           send=send, writer=Writer(), encoder=Encoder(), clock=clock))
    return key, clock, relay


async def _recording(relay, key, clock):
    gate_end, fake = Endpoint(), FakeGate()
    tunnel = await relay.open_tunnel(ticket(key, clock), gate_end)
    await relay.flush()
    (_op, conn, _), = gate_end.frames()
    for chunk in (fake.hello(), fake.after_version(), fake.after_security(), fake.update((9, 9, 9))):
        await relay.from_tunnel(tunnel, encode_frame(OP_DATA, conn, chunk))
    recorder = relay.recorder(tunnel.session.session_id)
    await recorder.tick()
    return tunnel, recorder


def test_a_session_watched_only_by_thumbnails_records_at_1fps_and_a_viewer_raises_it():
    key, clock, relay = _relay()

    async def go():
        tunnel, recorder = await _recording(relay, key, clock)
        thumbs = []
        for n in range(3):
            v = await relay.open_viewer(ticket(key, clock, "viewer", email=f"t{n}@example.test"),
                                        Endpoint())
            await relay.from_viewer(v, PREAMBLE)
            await relay.from_viewer(v, PIXEL_FORMAT + THUMB + REQUEST)
            thumbs.append(v)
        assert recorder.fps == rec.IDLE_FPS == 1
        tab = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await relay.from_viewer(tab, PREAMBLE + PIXEL_FORMAT + FULL + REQUEST)
        assert recorder.fps == rec.WATCHED_FPS == 10
        await relay.close_viewer(tab)
        assert recorder.fps == rec.IDLE_FPS
        # A card that leaves thumbnail mode (a later SetEncodings) is a watcher.
        await relay.from_viewer(thumbs[0], FULL)
        assert recorder.fps == rec.WATCHED_FPS
        await relay.close_tunnel(tunnel)

    run(go())


def test_the_mode_is_read_from_the_stream_however_it_is_chunked():
    key, clock, relay = _relay()

    async def go():
        tunnel, recorder = await _recording(relay, key, clock)
        v = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        assert recorder.fps == rec.WATCHED_FPS  # nothing read yet: a watcher
        data = PREAMBLE + PIXEL_FORMAT + THUMB + REQUEST
        for at in range(0, len(data), 5):
            await relay.from_viewer(v, data[at:at + 5])
        assert recorder.fps == rec.IDLE_FPS
        await relay.close_tunnel(tunnel)

    run(go())


def test_pasted_text_is_skipped_unread_even_when_it_looks_like_a_set_encodings():
    key, clock, relay = _relay()

    async def go():
        tunnel, recorder = await _recording(relay, key, clock)
        v = await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        await relay.from_viewer(v, PREAMBLE + PIXEL_FORMAT + FULL)
        await relay.from_viewer(v, struct.pack(">BxxxI", 6, len(THUMB)))
        await relay.from_viewer(v, THUMB)  # the pasted bytes, not a message
        assert recorder.fps == rec.WATCHED_FPS
        await relay.from_viewer(v, REQUEST + THUMB)
        assert recorder.fps == rec.IDLE_FPS
        await relay.close_tunnel(tunnel)

    run(go())


def test_thumbnails_still_count_against_the_per_task_cap():
    key, clock, relay = _relay()

    async def go():
        tunnel, _recorder = await _recording(relay, key, clock)
        for n in range(10):
            v = await relay.open_viewer(ticket(key, clock, "viewer", email=f"t{n}@example.test"),
                                        Endpoint())
            await relay.from_viewer(v, PREAMBLE + THUMB)
        assert relay.viewer_count(task_id=tunnel.session.task_id) == 10
        try:
            await relay.open_viewer(ticket(key, clock, "viewer"), Endpoint())
        except Refused as exc:
            assert exc.code == CLOSE_FULL
        else:
            raise AssertionError("an eleventh link was admitted")
        await relay.close_tunnel(tunnel)

    run(go())
