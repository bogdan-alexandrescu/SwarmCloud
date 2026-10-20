"""QA-FLOWS (part of #1649): flow GIFs and a mockup page for a live-browser agent.

Owner decision, 2026-10-19 ("Flow GIFs and a mockup page"):

* `swarm-handoff record start <name>` / `record stop` turn screenshots of the
  page on screen into `$SWARM_ARTIFACTS_DIR/flows/<name>.gif`, with the frame
  count and the file size capped;
* `swarm-handoff serve <dir>` serves a directory over http on 127.0.0.1 only,
  so the live Chromium can open a mockup beside the app (file:// is blocked by
  chromium-policy.json);
* the agent's instructions name both commands.

The image carries no image library (images/agent-runtime-browser/Dockerfile
installs Playwright and Chromium, nothing that writes a GIF), so the command's
encoder is its own, standard library only. These tests read what it writes
with a decoder of their own, written from the GIF89a specification, not from
the encoder: a GIF that only the encoder's own reader accepts would prove
nothing. No Chromium, no network beyond loopback.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import secrets
import struct
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMAND = REPO / "images" / "agent-runtime-browser" / "swarm-handoff"
POLICY = REPO / "images" / "agent-runtime-browser" / "chromium-policy.json"


def _command():
    loader = importlib.machinery.SourceFileLoader("swarm_handoff_flows", str(COMMAND))
    spec = importlib.util.spec_from_loader("swarm_handoff_flows", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# An independent GIF reader (GIF89a, appendix F's variable-length LZW)
# --------------------------------------------------------------------------


def _lzw_decode(data: bytes, min_size: int, count: int) -> bytes:
    clear, end = 1 << min_size, (1 << min_size) + 1
    size, nxt = min_size + 1, end + 1
    table = {i: bytes([i]) for i in range(clear)}
    out = bytearray()
    prev: bytes | None = None
    acc = bits = pos = 0
    while True:
        while bits < size:
            if pos >= len(data):
                raise AssertionError("the image data ended before its end code")
            acc |= data[pos] << bits
            bits += 8
            pos += 1
        code = acc & ((1 << size) - 1)
        acc >>= size
        bits -= size
        if code == clear:
            size, nxt, prev = min_size + 1, end + 1, None
            table = {i: bytes([i]) for i in range(clear)}
            continue
        if code == end:
            break
        if prev is None:
            entry = table[code]
        else:
            if code in table:
                entry = table[code]
            elif code == nxt:
                entry = prev + prev[:1]
            else:
                raise AssertionError(f"code {code} is past the table ({nxt})")
            if nxt < 4096:
                table[nxt] = prev + entry[:1]
                nxt += 1
                if nxt == (1 << size) and size < 12:
                    size += 1
        out += entry
        prev = entry
    assert len(out) == count, (len(out), count)
    return bytes(out)


def _read_gif(raw: bytes) -> dict:
    assert raw[:6] == b"GIF89a"
    width, height, flags, _bg, _aspect = struct.unpack("<HHBBB", raw[6:13])
    pos = 13
    palette: list[tuple[int, int, int]] = []
    if flags & 0x80:
        n = 2 << (flags & 7)
        palette = [tuple(raw[pos + 3 * i:pos + 3 * i + 3]) for i in range(n)]
        pos += 3 * n
    canvas = bytearray(width * height)
    frames: list[dict] = []
    delay = 0
    while True:
        tag = raw[pos]
        pos += 1
        if tag == 0x3B:
            assert pos == len(raw), "nothing follows the trailer"
            break
        if tag == 0x21:
            label = raw[pos]
            pos += 1
            blocks = bytearray()
            while raw[pos]:
                blocks += raw[pos + 1:pos + 1 + raw[pos]]
                pos += raw[pos] + 1
            pos += 1
            if label == 0xF9:
                delay = struct.unpack("<H", bytes(blocks[1:3]))[0]
            continue
        assert tag == 0x2C, f"unknown block {tag:#x} at {pos - 1}"
        left, top, w, h, iflags = struct.unpack("<HHHHB", raw[pos:pos + 9])
        pos += 9
        assert not iflags & 0x80, "every frame uses the global palette"
        assert left + w <= width and top + h <= height
        min_size = raw[pos]
        pos += 1
        data = bytearray()
        while raw[pos]:
            assert raw[pos] <= 255
            data += raw[pos + 1:pos + 1 + raw[pos]]
            pos += raw[pos] + 1
        pos += 1
        pixels = _lzw_decode(bytes(data), min_size, w * h)
        for y in range(h):
            canvas[(top + y) * width + left:(top + y) * width + left + w] = pixels[y * w:(y + 1) * w]
        frames.append({"rect": (left, top, w, h), "delay": delay, "canvas": bytes(canvas)})
    return {"width": width, "height": height, "palette": palette, "frames": frames}


def _rgb_at(gif: dict, frame: int, x: int, y: int) -> tuple[int, int, int]:
    return gif["palette"][gif["frames"][frame]["canvas"][y * gif["width"] + x]]


# --------------------------------------------------------------------------
# PNGs as Chromium writes them: 8-bit RGB or RGBA, every filter type
# --------------------------------------------------------------------------


def _png(width: int, height: int, pixel, *, alpha: bool = False, filters=(0, 1, 2, 3, 4)) -> bytes:
    channels = 4 if alpha else 3
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes(pixel(x, y)[:channels]) if alpha else bytes(pixel(x, y)[:3])
        rows.append(bytes(row))
    out = bytearray()
    prev = bytes(width * channels)
    for y, row in enumerate(rows):
        kind = filters[y % len(filters)]
        line = bytearray([kind])
        for i, value in enumerate(row):
            a = row[i - channels] if i >= channels else 0
            b = prev[i]
            c = prev[i - channels] if i >= channels else 0
            if kind == 0:
                pred = 0
            elif kind == 1:
                pred = a
            elif kind == 2:
                pred = b
            elif kind == 3:
                pred = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            line.append((value - pred) & 0xFF)
        out += line
        prev = row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")


RED, GREEN, BLUE, WHITE, BLACK = (255, 0, 0), (0, 153, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)


# --------------------------------------------------------------------------
# The GIF
# --------------------------------------------------------------------------


def test_the_png_reader_undoes_every_filter_chromium_may_choose():
    cmd = _command()

    def pixel(x, y):
        return ((x * 37 + y * 11) % 256, (x * 5 + y * 71) % 256, (x * y) % 256, 255)

    for alpha in (False, True):
        width, height, channels, pixels = cmd.decode_png(_png(9, 7, pixel, alpha=alpha))
        assert (width, height, channels) == (9, 7, 4 if alpha else 3)
        for y in range(7):
            for x in range(9):
                at = (y * 9 + x) * channels
                assert tuple(pixels[at:at + 3]) == pixel(x, y)[:3], (alpha, x, y)
    with pytest.raises(ValueError):
        cmd.decode_png(b"GIF89a" + secrets.token_bytes(32))


def test_a_flow_of_three_frames_is_a_gif_of_three_frames(tmp_path):
    """The owner's first test: three frames in, a GIF any reader takes out,
    with each frame's pixels where they were and the time between them."""
    cmd = _command()
    path = tmp_path / "flows" / "sign-up.gif"
    shots = [
        _png(24, 16, lambda x, y: RED + (255,)),
        _png(24, 16, lambda x, y: (BLUE if y >= 8 else RED) + (255,)),
        _png(24, 16, lambda x, y: (GREEN if x < 12 else WHITE) + (255,)),
    ]
    flow = cmd.FlowGif(path)
    for i, shot in enumerate(shots):
        assert flow.add_png(shot, at=10.0 + i * 0.5) == cmd.FRAME_ADDED
    result = flow.close(at=12.0)
    raw = path.read_bytes()
    gif = _read_gif(raw)
    assert (gif["width"], gif["height"]) == (24, 16)
    assert len(gif["frames"]) == 3 == result["frames"]
    assert result["bytes"] == len(raw) and result["path"] == str(path)
    assert _rgb_at(gif, 0, 3, 3) == RED and _rgb_at(gif, 0, 20, 12) == RED
    assert _rgb_at(gif, 1, 3, 3) == RED and _rgb_at(gif, 1, 3, 12) == BLUE
    assert _rgb_at(gif, 2, 3, 3) == GREEN and _rgb_at(gif, 2, 20, 12) == WHITE
    # Half a second apart, in hundredths; the last frame holds for the time
    # to `close`, under the cap a frame is ever shown for.
    assert [f["delay"] for f in gif["frames"]] == [50, 50, min(100, cmd.MAX_FRAME_DELAY_CS)]
    # Only the rows that changed are encoded again: frame 2 is the lower half.
    assert gif["frames"][1]["rect"] == (0, 8, 24, 8)


def test_a_frame_like_the_last_is_not_a_frame_and_the_last_is_held_longer(tmp_path):
    cmd = _command()
    path = tmp_path / "same.gif"
    flow = cmd.FlowGif(path)
    still = _png(8, 8, lambda x, y: BLUE + (255,))
    assert flow.add_png(still, at=0.0) == cmd.FRAME_ADDED
    assert flow.add_png(still, at=0.5) == cmd.FRAME_SAME
    assert flow.add_png(_png(8, 8, lambda x, y: RED + (255,)), at=1.0) == cmd.FRAME_ADDED
    flow.close(at=1.2)
    gif = _read_gif(path.read_bytes())
    assert len(gif["frames"]) == 2
    assert gif["frames"][0]["delay"] == 100


def test_a_long_frame_is_shown_for_at_most_the_cap(tmp_path):
    cmd = _command()
    path = tmp_path / "idle.gif"
    flow = cmd.FlowGif(path)
    flow.add_png(_png(8, 8, lambda x, y: BLUE + (255,)), at=0.0)
    flow.add_png(_png(8, 8, lambda x, y: RED + (255,)), at=600.0)
    flow.close(at=601.0)
    assert _read_gif(path.read_bytes())["frames"][0]["delay"] == cmd.MAX_FRAME_DELAY_CS


def test_the_gif_on_disk_is_whole_after_every_frame(tmp_path):
    """A recorder killed between frames leaves a GIF, not a fragment: the
    trailer is written after every frame and overwritten by the next."""
    cmd = _command()
    path = tmp_path / "partial.gif"
    flow = cmd.FlowGif(path)
    flow.add_png(_png(8, 8, lambda x, y: BLUE + (255,)), at=0.0)
    assert len(_read_gif(path.read_bytes())["frames"]) == 1
    flow.add_png(_png(8, 8, lambda x, y: RED + (255,)), at=1.0)
    assert len(_read_gif(path.read_bytes())["frames"]) == 2


def test_a_wide_page_is_halved_and_a_page_of_another_size_is_fitted(tmp_path):
    cmd = _command()
    path = tmp_path / "wide.gif"
    flow = cmd.FlowGif(path, max_width=16, max_height=16)
    flow.add_png(_png(32, 8, lambda x, y: (RED if x < 16 else BLUE) + (255,)), at=0.0)
    # A device switch mid-flow: a taller page, fitted inside the same canvas.
    flow.add_png(_png(8, 32, lambda x, y: GREEN + (255,)), at=1.0)
    flow.close(at=2.0)
    gif = _read_gif(path.read_bytes())
    assert (gif["width"], gif["height"]) == (16, 4)
    assert _rgb_at(gif, 0, 2, 2) == RED and _rgb_at(gif, 0, 12, 2) == BLUE
    assert _rgb_at(gif, 1, 0, 0) == GREEN and _rgb_at(gif, 1, 15, 3) == WHITE


def test_the_size_cap_holds_and_the_gif_stays_whole(tmp_path):
    """The owner's second test. Noise compresses worst, so every frame is
    near its full size: the flow stops BEFORE the frame that would pass the
    cap, says why, and what is on disk is a GIF under the cap."""
    cmd = _command()
    path = tmp_path / "noisy.gif"
    cap = 6000
    flow = cmd.FlowGif(path, max_bytes=cap)
    outcomes = []
    for i in range(20):
        noise = secrets.token_bytes(32 * 32 * 3)
        shot = _png(32, 32, lambda x, y, n=noise: tuple(n[(y * 32 + x) * 3:(y * 32 + x) * 3 + 3]) + (255,))
        outcomes.append(flow.add_png(shot, at=float(i)))
        if outcomes[-1] != cmd.FRAME_ADDED:
            break
    assert outcomes[-1] == cmd.STOP_SIZE_CAP
    result = flow.close(at=30.0)
    assert result["stopped"] == cmd.STOP_SIZE_CAP
    raw = path.read_bytes()
    assert len(raw) <= cap
    assert 1 <= len(_read_gif(raw)["frames"]) == result["frames"] == outcomes.count(cmd.FRAME_ADDED)
    assert flow.add_png(_png(32, 32, lambda x, y: RED + (255,)), at=40.0) == cmd.STOP_SIZE_CAP


def test_the_frame_cap_holds(tmp_path):
    cmd = _command()
    path = tmp_path / "many.gif"
    flow = cmd.FlowGif(path, max_frames=3)
    colours = [RED, BLUE, GREEN, WHITE, BLACK]
    seen = [flow.add_png(_png(4, 4, lambda x, y, c=c: c + (255,)), at=float(i)) for i, c in enumerate(colours)]
    assert seen == [cmd.FRAME_ADDED] * 3 + [cmd.STOP_FRAME_CAP] * 2
    assert len(_read_gif(path.read_bytes())["frames"]) == 3


def test_the_caps_are_what_the_instructions_and_the_console_can_carry():
    cmd = _command()
    # The console draws an image inline up to 20 MiB (Artifacts.tsx
    # INLINE_IMAGE_MAX_BYTES); a flow GIF is always under it.
    assert cmd.MAX_FLOW_BYTES <= 20 * 1024 * 1024
    assert 0 < cmd.MAX_FLOW_FRAMES <= 300
    assert cmd.MIN_INTERVAL_MS <= cmd.FLOW_INTERVAL_MS <= cmd.MAX_INTERVAL_MS
    assert cmd.MAX_FLOW_SECONDS < 60 * 60


def test_the_recorder_writes_the_gif_from_its_frames_and_stops_on_the_stop_file(tmp_path):
    """The loop `record start` runs in the background, with the browser
    replaced: frames on each tick until the stop file appears."""
    cmd = _command()
    shots = iter([
        _png(8, 8, lambda x, y: RED + (255,)),
        _png(8, 8, lambda x, y: BLUE + (255,)),
        None,  # a tick on which the browser had no page on screen
        _png(8, 8, lambda x, y: GREEN + (255,)),
    ])
    stop = tmp_path / "stop"
    clock = [0.0]

    class Frames:
        closed = False

        def capture(self):
            shot = next(shots, None)
            if shot is None and not stop.exists() and clock[0] > 3:
                stop.write_text("")
            return shot

        def wait(self, seconds):
            clock[0] += seconds

        def close(self):
            self.closed = True

    frames = Frames()
    path = tmp_path / "flows" / "checkout.gif"
    result = cmd.record_loop(frames, cmd.FlowGif(path), stop_file=stop, interval=1.0,
                             clock=lambda: clock[0])
    assert frames.closed
    assert result["frames"] == 3 and result["stopped"] == cmd.STOP_ASKED
    assert len(_read_gif(path.read_bytes())["frames"]) == 3


def test_record_start_refuses_without_a_live_browser_or_with_a_bad_name(tmp_path, monkeypatch, capsys):
    cmd = _command()
    root = tmp_path / "spool"
    root.mkdir()
    monkeypatch.setenv("SWARM_HANDOFF", str(root))
    monkeypatch.setenv("SWARM_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("SWARM_CDP_ENDPOINT", raising=False)
    assert cmd.main(["record", "start", "sign-up"]) == cmd.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "no_live_path"
    monkeypatch.setenv("SWARM_CDP_ENDPOINT", "http://127.0.0.1:9222")
    for name in ("../escape", "Sign Up", "", "a" * 80):
        assert cmd.main(["record", "start", name]) == cmd.EXIT_REFUSED, name
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "name_invalid"
    assert cmd.main(["record", "stop"]) == cmd.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "not_recording"


def test_record_start_launches_one_detached_recorder_and_stop_reports_its_gif(tmp_path, monkeypatch, capsys):
    cmd = _command()
    root = tmp_path / "spool"
    root.mkdir()
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("SWARM_HANDOFF", str(root))
    monkeypatch.setenv("SWARM_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("SWARM_CDP_ENDPOINT", "http://127.0.0.1:9222")
    launched = []

    class Proc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return Proc()

    monkeypatch.setattr(cmd.subprocess, "Popen", fake_popen)
    alive = {4242}
    monkeypatch.setattr(cmd, "_alive", lambda pid: pid in alive)
    assert cmd.main(["record", "start", "sign-up", "--interval-ms", "50"]) == cmd.EXIT_RETURNED
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["outcome"] == "recording" and out["path"] == str(artifacts / "flows" / "sign-up.gif")
    assert out["interval_ms"] == cmd.MIN_INTERVAL_MS, "the interval is bounded"
    (argv, kwargs), = launched
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == cmd.subprocess.DEVNULL
    assert "_run" in argv and "sign-up" in argv
    # One recording at a time.
    assert cmd.main(["record", "start", "other"]) == cmd.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "already_recording"
    # `stop` asks the recorder to stop and reports what it wrote.
    result = {"path": out["path"], "frames": 3, "bytes": 1234, "stopped": cmd.STOP_ASKED}

    def finish(*_a, **_k):
        if (root / cmd.RECORDING_STOP).exists():
            (root / cmd.RECORDING_RESULT).write_text(json.dumps(result))
            alive.clear()

    monkeypatch.setattr(cmd.time, "sleep", finish)
    assert cmd.main(["record", "stop"]) == cmd.EXIT_RETURNED
    stopped = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert stopped["outcome"] == "recorded" and stopped["frames"] == 3 and stopped["path"] == out["path"]
    assert not (root / cmd.RECORDING_NAME).exists()


# --------------------------------------------------------------------------
# The mockup page
# --------------------------------------------------------------------------


def _serve(cmd, root: Path):
    server = cmd.mockup_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_serve_binds_127_0_0_1_only_and_serves_the_directory(tmp_path):
    """The owner's third test: the address is loopback, and nothing a caller
    passes can make it anything else."""
    cmd = _command()
    site = tmp_path / "mockup"
    site.mkdir()
    (site / "index.html").write_text("<h1>mockup</h1>")
    server, thread = _serve(cmd, site)
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1" and port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as reply:
            assert reply.read() == b"<h1>mockup</h1>"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    parser_help = cmd.build_parser().format_help()
    serve_help = [a.option_strings for a in cmd.build_parser()._subparsers._group_actions[0].choices["serve"]._actions]
    flat = {o for opts in serve_help for o in opts}
    assert not flat & {"--host", "--bind", "--address", "-b"}, flat
    assert "serve" in parser_help
    with pytest.raises(ValueError):
        cmd.mockup_server(site, 0, host="0.0.0.0")


def test_serve_answers_only_a_loopback_host_and_never_outside_its_directory(tmp_path):
    """A page on another site in the same browser cannot read the mockup by
    rebinding its name to 127.0.0.1 (the Host check, as the CDP proxy's), and
    a link out of the directory is not followed."""
    cmd = _command()
    site = tmp_path / "mockup"
    site.mkdir()
    (site / "index.html").write_text("ok")
    (tmp_path / "private.txt").write_text("not served")
    (site / "out").symlink_to(tmp_path / "private.txt")
    server, thread = _serve(cmd, site)
    try:
        port = server.server_address[1]
        rebound = urllib.request.Request(f"http://127.0.0.1:{port}/index.html", headers={"Host": "attacker.example.com"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(rebound, timeout=5)
        assert refused.value.code == 403
        local = urllib.request.Request(f"http://127.0.0.1:{port}/index.html", headers={"Host": f"localhost:{port}"})
        with urllib.request.urlopen(local, timeout=5) as reply:
            assert reply.read() == b"ok"
        with pytest.raises(urllib.error.HTTPError) as escaped:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/out", timeout=5)
        assert escaped.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_serve_refuses_a_directory_that_is_not_there(tmp_path, capsys):
    cmd = _command()
    assert cmd.main(["serve", str(tmp_path / "missing")]) == cmd.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["code"] == "not_a_directory"


def test_the_live_chromium_may_open_127_0_0_1():
    """Why `serve` needs no exception: the URL blocklist names no loopback
    address, and the proxied egress's bypass list starts with `<local>` and
    never `<-loopback>` (Chromium sends loopback direct unless told
    otherwise). The display sidecar shares the pod's network namespace with
    the agent, which is how the agent already reaches the gate's CDP proxy on
    127.0.0.1:9222."""
    policy = json.loads(POLICY.read_text())
    for entry in policy["URLBlocklist"]:
        assert "127.0.0.1" not in entry and "localhost" not in entry, entry
        assert entry not in ("*", "http://*", "http:*"), entry
    from agent_worker.livegate import egress

    settings = egress.EgressSettings.from_env({egress.EGRESS_ENV: egress.MODE_PROXY,
                                              egress.FORWARDER_ENV: "swarm-egress.example.com:3128"})
    bypass = settings.bypass_list()
    assert bypass.startswith("<local>") and "<-loopback>" not in bypass


# --------------------------------------------------------------------------
# The instructions
# --------------------------------------------------------------------------


def test_the_instructions_name_both_commands_in_one_line_each():
    """The owner's fourth test."""
    from agent_worker import handoff as handoff_mod
    from agent_worker.runners import claude_code

    note = claude_code.LIVE_BROWSER_NOTE
    assert "swarm-handoff record start <name>" in note and "swarm-handoff record stop" in note
    assert "$SWARM_ARTIFACTS_DIR/flows/<name>.gif" in note
    assert "swarm-handoff serve <dir>" in note and "http://127.0.0.1:<port>/" in note
    guide = handoff_mod.AGENT_GUIDE
    lines = [line for line in guide.splitlines() if "swarm-handoff record start" in line]
    assert len(lines) == 1 and "swarm-handoff record stop" in lines[0]
    assert len([line for line in guide.splitlines() if "swarm-handoff serve <dir>" in line]) == 1


def test_the_caps_the_note_states_are_the_commands():
    from agent_worker.runners import claude_code

    cmd = _command()
    note = claude_code.LIVE_BROWSER_NOTE
    assert f"{cmd.MAX_FLOW_FRAMES} frames" in note
    assert f"{cmd.MAX_FLOW_BYTES // (1024 * 1024)} MiB" in note
    assert f"{cmd.MAX_FLOW_SECONDS // 60} minutes" in note
    assert cmd.FLOW_INTERVAL_MS == 1000 and "every second" in note
