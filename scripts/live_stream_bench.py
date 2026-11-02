"""Measure the live view end to end: gate -> relay -> viewer (LIVE-STREAM-SPEED, part of #2688).

    scripts/live-stream-bench.sh [--seconds 600] [--jpeg 0|1-95] [--json out.json]

WHY THIS EXISTS. On 2026-11-02 the owner watched browser agents in Chrome and
saw the live views take a long time to connect, reconnect every 5-17 s and
move at under one frame a second. `agent_worker.livegate.bench` times the
gate's own encode on one socket pair; this runs the WHOLE wire the picture
takes, over real sockets, so one command gives the three numbers the owner's
targets are stated in:

* time to first frame: from asking for the viewer ticket to the first full
  update decoded (target under 2 s);
* frames a second, and bytes a second, reaching the viewer while a
  1440x987 page moves (target at least 10 frames a second);
* reconnects over the run (target zero in 10 minutes on a healthy relay).

`--link-mbps` holds the viewer's side to a link of that speed, as the hosted
path between the pod and a person's browser is: the gate answers only what
the viewer asks for, so a picture larger than the link carries in a frame's
time is a lower frame rate, not a queue.

WHAT RUNS. The relay is the real `swarm_live.main.create_app` over the real
`Rendezvous`, served by uvicorn on 127.0.0.1. The gate is the real
`livegate.tunnel.Tunnel` and `StreamMux` dialling it with a tunnel ticket,
serving one real `livegate.rfb.RfbSession` per stream from a `FrameHub`.
The viewer speaks RFB as noVNC 1.4.0 does (its encodings, the console's
`qualityLevel` 9 and `compressionLevel` 1), over a websocket to the relay's
`/v1/view` with a viewer ticket, and asks for the next update as soon as one
has arrived. On a close it mints a new ticket and reconnects, counted.

THE PAGE. `--source animate` (the default) is `livegate.bench.Page`, a tall
document of text, a photo and a form field, standing still but for a
720x400 box whose content slides diagonally every frame, as a carousel or a
video does (no CopyRect can follow it); `--source scroll` scrolls the whole
page 24 px a frame instead. Either is captured at the gate's watch rate (`WATCH_FPS`, 10 a second) or, with
`--control`, its control rate (`CONTROL_FPS`). `--source xvfb:PATH` captures
a real Xvfb screen file instead (`Xvfb :99 -screen 0 1440x987x24 -fbdir DIR`,
PATH being DIR/Xvfb_screen0) with Chromium drawing an animated page on it;
the shell wrapper starts both when they are installed (the browser image has
them) and `--chromium` is passed.

WHAT IT CANNOT SEE. swarm-api's proxy between the console and the relay, and
the network and Cloud Run hops between pod, relay, API and browser: here every
hop is loopback. The numbers are what the gate, the relay and the encoding
set; the hosted path adds its own latency on top.

NOTHING HERE IS A SECRET: the relay key and every ticket are made at runtime
(`secrets.token_bytes`) and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import statistics
import struct
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for app in ("apps/swarm-live", "apps/agent-worker"):
    if str(ROOT / app) not in sys.path:
        sys.path.insert(0, str(ROOT / app))

from agent_worker.livegate import __main__ as gate_main  # noqa: E402
from agent_worker.livegate import bench as gate_bench  # noqa: E402
from agent_worker.livegate import rfb  # noqa: E402
from agent_worker.livegate.capture import Frame, XvfbFramebuffer  # noqa: E402
from agent_worker.livegate.tunnel import StreamMux, Tunnel  # noqa: E402

WIDTH, HEIGHT = 1440, 987
#: noVNC 1.4.0's encodings with the console's settings (LiveBrowser.tsx):
#: quality 9 (-23) and compression 1 (-255).
CONSOLE_ENCODINGS = tuple(-32 + 9 if -32 <= e <= -23 else -256 + 1 if -256 <= e <= -247 else e
                          for e in gate_bench.NOVNC_ENCODINGS)
SESSION, TENANT, TASK, ATTEMPT, GENERATION = "ls_bench", "bench", "task_bench", "att_bench", 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Source:
    """The page the gate captures, one frame per call."""

    #: The animated box (`animate`): a carousel or a video, half the page wide.
    BOX = (360, 300, 1080, 700)

    def __init__(self, spec: str) -> None:
        self._xvfb: XvfbFramebuffer | None = None
        self._kind = spec
        if spec.startswith("xvfb:"):
            self._xvfb = XvfbFramebuffer(spec[len("xvfb:"):])
            return
        if spec not in ("animate", "scroll"):
            raise SystemExit(f"--source: animate, scroll or xvfb:PATH, not {spec!r}")
        self._page = gate_bench.Page(WIDTH, HEIGHT)
        body = HEIGHT - self._page.chrome
        self._rows = len(self._page.doc) // self._page.row
        self._span = max(1, self._rows - body)
        self._offset = 0
        self._still = self._page.screen(0)

    def grab(self) -> Frame:
        if self._xvfb is not None:
            return self._xvfb.grab()
        self._offset += gate_bench.SCROLL_PX
        if self._kind == "scroll":
            return Frame(WIDTH, HEIGHT, WIDTH * 4, self._page.screen(self._offset % self._span))
        # A still page with a box whose content slides diagonally every frame:
        # no CopyRect can follow it, as none can follow a video.
        out = bytearray(self._still)
        x0, y0, x1, y1 = self.BOX
        row, doc = self._page.row, self._page.doc
        shift = (self._offset * 3) % (WIDTH - (x1 - x0))
        for y in range(y0, y1):
            at = ((y + self._offset) % self._rows) * row + shift * 4
            out[y * row + x0 * 4:y * row + x1 * 4] = doc[at:at + (x1 - x0) * 4]
        return Frame(WIDTH, HEIGHT, row, out)


class WsStream:
    """A websocket as the `asyncio` reader and writer `livegate.bench.Client` reads RFB from."""

    def __init__(self, ws: Any, link_mbps: float = 0.0) -> None:
        self.ws = ws
        #: The link's speed, megabits a second; 0 is loopback's. A message is
        #: handed to the reader only after it would have crossed the link.
        self._seconds_per_byte = 8 / (link_mbps * 1e6) if link_mbps > 0 else 0.0
        self.reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        self._out: asyncio.Queue[bytes] = asyncio.Queue()
        self.bytes_in = 0
        self._tasks = [asyncio.create_task(self._pump_in()), asyncio.create_task(self._pump_out())]

    async def _pump_in(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    if self._seconds_per_byte:
                        await asyncio.sleep(len(message) * self._seconds_per_byte)
                    self.bytes_in += len(message)
                    self.reader.feed_data(message)
        except Exception:  # noqa: BLE001 -- a closed link is read as EOF
            pass
        self.reader.feed_eof()

    async def _pump_out(self) -> None:
        while True:
            await self.ws.send(await self._out.get())

    def write(self, data: bytes) -> None:
        self._out.put_nowait(bytes(data))

    async def drain(self) -> None:
        while not self._out.empty():
            await asyncio.sleep(0)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.ws.close()


async def _gate(url: str, ticket: str, hub: rfb.FrameHub, jpeg: int) -> asyncio.Task[Any]:
    injector = gate_bench._Injector()
    control = rfb.ControlState(injector)

    async def serve(conn_id: str, reader: asyncio.StreamReader, writer: Any) -> None:
        await rfb.RfbSession(conn_id, reader, writer, hub, control, injector, jpeg_quality=jpeg).serve()

    async def on_control(*_args: Any) -> None:
        return None

    tunnel = Tunnel(url)
    await tunnel.open(ticket)
    mux = StreamMux(tunnel, serve, on_control)
    return asyncio.create_task(tunnel.run(mux.on_frame))


async def _capture(hub: rfb.FrameHub, source: Source, fps: float, stop: asyncio.Event) -> None:
    gap = 1.0 / fps
    next_at = time.perf_counter()
    while not stop.is_set():
        hub.publish(source.grab())
        next_at += gap
        await asyncio.sleep(max(0.0, next_at - time.perf_counter()))


async def _watch(url: str, mint: Any, seconds: float, link_mbps: float) -> dict[str, Any]:
    """The viewer: connect, read updates for `seconds`, reconnect on a close."""
    from websockets.asyncio.client import connect

    deadline = time.perf_counter() + seconds
    first_frames: list[float] = []
    updates: list[float] = []
    total_bytes = 0
    reconnects = -1
    while time.perf_counter() < deadline:
        reconnects += 1
        asked = time.perf_counter()
        ws = await connect(url, max_size=64 * 1024 * 1024, compression=None)
        await ws.send(mint())
        stream = WsStream(ws, link_mbps)
        client = gate_bench.Client(stream.reader, stream)  # type: ignore[arg-type]
        try:
            await client.handshake(CONSOLE_ENCODINGS)
            client.request(False)
            await client.read_update()
            first_frames.append(time.perf_counter() - asked)
            while time.perf_counter() < deadline:
                client.request(True)
                update = await asyncio.wait_for(client.read_update(), max(0.1, deadline - time.perf_counter()))
                updates.append(update.received)
                total_bytes += update.nbytes
        except (asyncio.IncompleteReadError, ConnectionError, AssertionError):
            continue  # the link closed under the viewer: a reconnect
        except asyncio.TimeoutError:
            pass
        finally:
            await stream.close()
    gaps = [b - a for a, b in zip(updates, updates[1:])]
    span = (updates[-1] - updates[0]) if len(updates) > 1 else 0.0
    return {
        "seconds": seconds,
        "first_frame_s": round(first_frames[0], 3) if first_frames else None,
        "first_frame_max_s": round(max(first_frames), 3) if first_frames else None,
        "frames_per_s": round((len(updates) - 1) / span, 2) if span else 0.0,
        "frame_gap_p95_ms": round(statistics.quantiles(gaps, n=20)[-1] * 1000, 1) if len(gaps) >= 20 else None,
        "bytes_per_s": int(total_bytes / span) if span else 0,
        "reconnects": reconnects,
    }


async def run(*, seconds: float, jpeg: int, source: str, fps: float, link_mbps: float = 0.0) -> dict[str, Any]:
    import uvicorn
    from swarm_live.main import create_app
    from swarm_live.rendezvous import Rendezvous, mint_ticket

    key = secrets.token_bytes(32)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(Rendezvous(key)), host="127.0.0.1", port=port,
                                           log_level="warning", ws_max_size=32 * 1024 * 1024))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    def ticket(role: str) -> str:
        return mint_ticket(key, session_id=SESSION, tenant_id=TENANT, task_id=TASK, attempt_id=ATTEMPT,
                           generation=GENERATION, role=role,
                           # The gate's ticket names nobody; a viewer's names the person (§5.2).
                           email="" if role == "tunnel" else "viewer@bench.invalid", now=time.time())

    hub = rfb.FrameHub(WIDTH, HEIGHT)
    src = Source(source)
    hub.publish(src.grab())
    stop = asyncio.Event()
    capture = asyncio.create_task(_capture(hub, src, fps, stop))
    gate = await _gate(f"ws://127.0.0.1:{port}/v1/tunnel", ticket("tunnel"), hub, jpeg)
    try:
        result = await _watch(f"ws://127.0.0.1:{port}/v1/view", lambda: ticket("viewer"), seconds, link_mbps)
    finally:
        stop.set()
        gate.cancel()
        capture.cancel()
        await asyncio.gather(gate, capture, return_exceptions=True)
        server.should_exit = True
        await serving
    result.update({"jpeg_quality": jpeg, "capture_fps": fps, "source": source.split(":")[0],
                   "size": f"{WIDTH}x{HEIGHT}", "link_mbps": link_mbps or None})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=600.0, help="how long the viewer watches")
    parser.add_argument("--jpeg", type=int, default=rfb.jpeg_quality_from_env(),
                        help="the gate's adaptive JPEG quality; 0 is today's lossless-only")
    parser.add_argument("--control", action="store_true", help="capture at the gate's control rate")
    parser.add_argument("--source", default="animate",
                        help="animate (a moving box on a still page), scroll, or xvfb:PATH to a screen file")
    parser.add_argument("--link-mbps", type=float, default=0.0,
                        help="the viewer's link, megabits a second (0: loopback)")
    parser.add_argument("--json", type=Path, help="also write the result here")
    args = parser.parse_args(argv)
    fps = float(gate_main.CONTROL_FPS if args.control else gate_main.WATCH_FPS)
    result = asyncio.run(run(seconds=args.seconds, jpeg=max(0, min(95, args.jpeg)), source=args.source, fps=fps,
                             link_mbps=args.link_mbps))
    print(json.dumps(result, indent=2))
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
