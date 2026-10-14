"""The live-browser acceptance check's offline half (lane LB-L13, part of #1030).

`tests/acceptance/test_live_browser.py` runs against a deployment, so what
can be proven without one is proven here, with no credentials and no network
beyond loopback:

* its RFB viewer speaks to the REAL gate server (`livegate.rfb.RfbSession`):
  the framebuffer it keeps is the gate's frame, and its key events reach the
  injector only once its connection holds control;
* its websocket client upgrades, sends masked frames, reads a server's
  close code, and its frame reader joins fragments;
* its frame judge passes a frame whose field box is the mask's paint and
  FAILS one where a single row of the field's own colour shows -- at device
  scale 1 and 2, exactly (RFB) and with VP8-like noise (the recording);
* the fixture page's layout is the one the judge measures by, and the page
  cannot send the password anywhere;
* the verdict keys, the shell group's check names and the settle states agree.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import importlib.util
import json
import random
import re
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "tests" / "acceptance" / "test_live_browser.py"
FIXTURE = ROOT / "tests" / "acceptance" / "fixtures" / "login.html"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "live-browser.sh"


def _load():
    spec = importlib.util.spec_from_file_location("live_browser_acceptance", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


lb = _load()

GREEN = (0x00, 0x80, 0x00)


# --------------------------------------------------------------------------
# Synthetic frames in the viewer's layout (R, G, B, X)
# --------------------------------------------------------------------------


def _frame(width=1280, height=800, bpp=4):
    pixels = bytearray(bytes([*GREEN, 0][:bpp]) * (width * height))
    return pixels


def _fill(pixels, width, box, rgb, bpp=4):
    x0, y0, x1, y1 = box
    px = bytes([*rgb, 0][:bpp])
    for y in range(y0, y1):
        at = (y * width + x0) * bpp
        pixels[at:at + (x1 - x0) * bpp] = px * (x1 - x0)


def _page(scale=1, top=120, left=40, bpp=4, painted=True, width=1280, height=800):
    """The fixture as it would reach a viewer: page area starting `top` px
    down (the browser's own bar above it), marker and field at the page's CSS
    positions times `scale`, the field painted by the mask (dilated 8 px) or not."""
    pixels = _frame(width, height, bpp)
    mx, my = left + 40 * scale, top + 120 * scale
    _fill(pixels, width, (mx, my, mx + 320 * scale, my + 40 * scale), lb.MARKER_RGB, bpp)
    fx, fy = left + 40 * scale, top + 200 * scale
    field = (fx, fy, fx + 320 * scale, fy + 40 * scale)
    _fill(pixels, width, field, lb.FIELD_RGB, bpp)
    if painted:
        from agent_worker.livegate.mask import MASK_RGB

        d = 8
        _fill(pixels, width, (field[0] - d, field[1] - d, field[2] + d, field[3] + d), MASK_RGB, bpp)
    return pixels, field


def test_the_judge_s_mask_colour_is_the_gate_s():
    from agent_worker.livegate.mask import MASK_RGB

    assert lb.MASK_RGB == MASK_RGB


@pytest.mark.parametrize("scale", [1, 2])
def test_a_frame_whose_field_is_painted_passes(scale):
    pixels, field = _page(scale=scale)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "painted", verdict.describe()
    assert (verdict.field.x0, verdict.field.y0, verdict.field.x1, verdict.field.y1) == field
    assert verdict.field_pixels == 0 and verdict.painted == 1.0


def test_a_frame_showing_the_field_unpainted_is_a_leak():
    pixels, _ = _page(painted=False)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "leak"
    assert verdict.field_pixels == 320 * 40


def test_one_unpainted_row_of_the_field_is_a_leak():
    """MUTATION: the mask paints the field one row short."""
    pixels, field = _page()
    _fill(pixels, 1280, (field[0], field[3] - 3, field[2], field[3] - 2), lb.FIELD_RGB)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "leak", verdict.describe()


def test_a_field_coloured_pixel_anywhere_is_a_leak():
    pixels, _ = _page()
    _fill(pixels, 1280, (900, 700, 901, 701), lb.FIELD_RGB)
    assert lb.judge(bytes(pixels), 1280, 800).kind == "leak"


def test_a_frame_without_the_page_is_not_judged():
    from agent_worker.livegate.mask import MASK_RGB

    blanked = _frame()
    _fill(blanked, 1280, (0, 120, 1280, 800), MASK_RGB)  # the mask blanked the page area
    assert lb.judge(bytes(blanked), 1280, 800).kind == "no_page"
    unavailable = bytearray(bytes([0x55, 0x55, 0x55, 0]) * (1280 * 800))
    assert lb.judge(bytes(unavailable), 1280, 800).kind == "no_page"


def test_the_recording_is_judged_with_a_tolerance_at_the_live_frames_box():
    pixels, field = _page(bpp=3)
    rng = random.Random(7)
    noisy = bytes(max(0, min(255, b + rng.randint(-12, 12))) for b in pixels)
    box = lb.Box(*field)
    assert lb.judge(noisy, 1280, 800, bpp=3, tol=24, field=box).kind == "painted"
    leaked, _ = _page(bpp=3, painted=False)
    assert lb.judge(bytes(leaked), 1280, 800, bpp=3, tol=24, field=box).kind == "leak"


# --------------------------------------------------------------------------
# The fixture page
# --------------------------------------------------------------------------


def _rule(css: str, selector: str) -> dict[str, int]:
    body = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert body, selector
    return {k: int(v) for k, v in re.findall(r"(left|top|width|height):\s*(\d+)px", body.group(1))}


def test_the_fixture_s_layout_is_the_one_the_judge_measures_by():
    html = FIXTURE.read_text()
    marker, field = _rule(html, "#marker"), _rule(html, "#password")
    assert (marker["width"], marker["height"]) == lb.MARKER_CSS
    assert (field["width"], field["height"]) == lb.FIELD_CSS
    assert field["left"] == marker["left"]
    assert field["top"] - marker["top"] == lb.FIELD_OFFSET_CSS
    assert "background: #00ffff" in html and "background: #ff00ff" in html
    assert lb.MARKER_RGB == (0x00, 0xFF, 0xFF) and lb.FIELD_RGB == (0xFF, 0x00, 0xFF)
    # The dilated mask of the field must not reach the marker.
    assert field["top"] - 8 > marker["top"] + marker["height"]


def test_the_fixture_cannot_send_the_password_anywhere():
    html = FIXTURE.read_text()
    field = re.search(r"<input[^>]*id=\"password\"[^>]*>", html).group(0)
    assert 'type="password"' in field
    assert "name=" not in field, "a named field is submitted with the form"
    form = re.search(r"<form[^>]*>", html).group(0)
    assert "action=" not in form and "method=" not in form
    assert "event.preventDefault()" in html
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "sendBeacon" not in html


def test_the_code_is_what_the_page_computes():
    html = FIXTURE.read_text()
    assert "crypto.subtle.digest('SHA-256'" in html and ".slice(0, 12)" in html
    password = lb.new_password()
    assert lb.session_code(password) == hashlib.sha256(password.encode()).hexdigest()[:12]


def test_the_password_is_made_at_run_time_and_typed_unshifted():
    a, b = lb.new_password(), lb.new_password()
    assert a != b and len(a) == lb.PASSWORD_LENGTH
    assert set(a) <= set(lb.PASSWORD_ALPHABET)
    assert lb.keysyms(a) == [ord(c) for c in a]
    with pytest.raises(ValueError):
        lb.keysyms("A")


# --------------------------------------------------------------------------
# The RFB viewer against the gate's own server
# --------------------------------------------------------------------------


class _Injector:
    def __init__(self):
        self.keys = []
        self.pointer = []

    async def key(self, keysym, down):
        self.keys.append((keysym, down))

    async def pointer_event(self, buttons, x, y):
        self.pointer.append((buttons, x, y))

    async def type_text(self, text):
        return True

    async def release_all(self):
        pass


class _Mask:
    def apply(self, frame):
        from agent_worker.livegate.mask import Outcome

        return Outcome.MASKED


def _gate_frame(width=320, height=200):
    from agent_worker.livegate.capture import Frame
    from agent_worker.livegate.mask import MASK_RGB

    frame = Frame.blank(width, height, GREEN)
    frame.fill(10, 20, 130, 40, lb.MARKER_RGB)
    frame.fill(10, 60, 110, 80, MASK_RGB)
    return frame


async def _session(handler):
    from agent_worker.livegate import rfb

    hub = rfb.FrameHub(320, 200)
    hub.publish(rfb.masked(_gate_frame(), _Mask()))
    injector = _Injector()
    control = rfb.ControlState(injector)
    sessions = []

    async def serve(reader, writer):
        session = rfb.RfbSession("conn-1", reader, writer, hub, control, injector)
        sessions.append(session)
        await session.serve()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    client = lb.RfbClient()

    async def pump(until, seconds=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while not until():
            for message in client.take_outbound():
                writer.write(message)
            await writer.drain()
            left = deadline - loop.time()
            if left <= 0:
                raise AssertionError("timed out")
            try:
                data = await asyncio.wait_for(reader.read(65536), min(left, 0.2))
            except asyncio.TimeoutError:
                continue
            if not data:
                raise AssertionError("the gate closed the stream")
            client.feed(data)
        for message in client.take_outbound():
            writer.write(message)
        await writer.drain()

    try:
        await handler(client, pump, hub, control, injector)
    finally:
        writer.close()
        server.close()
        await server.wait_closed()


def test_the_viewer_keeps_the_gate_s_frame_and_types_only_in_control():
    async def handler(client, pump, hub, control, injector):
        await pump(lambda: client.updates >= 1)
        assert (client.width, client.height) == (320, 200)
        expected = _gate_frame()
        for x, y in ((0, 0), (15, 25), (15, 65), (319, 199)):
            at = (y * 320 + x) * 4
            assert tuple(client.fb[at:at + 3]) == expected.rgb_at(x, y)
        verdict = lb.judge(bytes(client.fb), 320, 200)
        assert verdict.marker == lb.Box(10, 20, 130, 40)

        client.key(ord("a"), True)
        client.key(ord("a"), False)
        await pump(lambda: True)
        await asyncio.sleep(0.2)
        assert injector.keys == [], "a viewer's keys reached the display"

        await control.grant("conn-1")
        for keysym in lb.keysyms("ab3"):
            client.key(keysym, True)
            client.key(keysym, False)
        client.pointer(15, 65, 1)
        await pump(lambda: True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while len(injector.keys) < 6 and loop.time() < deadline:
            await asyncio.sleep(0.05)
        assert injector.keys == [(0x61, True), (0x61, False), (0x62, True), (0x62, False),
                                 (0x33, True), (0x33, False)]
        assert injector.pointer == [(1, 15, 65)]

        # A new frame arrives as an update to the viewer's incremental request.
        changed = _gate_frame()
        changed.fill(200, 150, 210, 160, (0xAA, 0xBB, 0xCC))
        before = client.updates
        from agent_worker.livegate import rfb

        hub.publish(rfb.masked(changed, _Mask()))
        # The pointer event above is news too (LB-CURSOR): the gate may already
        # have sent this viewer an update with the pointer painted in, so the
        # first update after `before` need not be the published frame. Wait for
        # the frame itself; pump() fails on its deadline if it never arrives.
        at = (155 * 320 + 205) * 4
        await pump(lambda: tuple(client.fb[at:at + 3]) == (0xAA, 0xBB, 0xCC))
        assert client.updates > before
        assert tuple(client.fb[at:at + 3]) == (0xAA, 0xBB, 0xCC)

    asyncio.run(_session(handler))


def test_the_viewer_refuses_what_the_gate_never_sends():
    client = lb.RfbClient()
    client.feed(b"RFB 003.008\n" + bytes([1, 1]) + struct.pack(">I", 0))
    client.feed(struct.pack(">HH", 4, 4) + bytes(16) + struct.pack(">I", 0))
    assert client.ready
    with pytest.raises(lb.RfbError):
        client.feed(bytes([3, 0, 0, 0, 0, 0, 0, 1]) + b"x")  # ServerCutText


def test_an_update_split_anywhere_is_read_once():
    client = lb.RfbClient()
    client.feed(b"RFB 003.008\n" + bytes([1, 1]) + struct.pack(">I", 0)
                + struct.pack(">HH", 2, 1) + bytes(16) + struct.pack(">I", 0))
    comp = __import__("zlib").compressobj()
    band = bytes([1, 2, 3, 0, 4, 5, 6, 0])
    data = comp.compress(band) + comp.flush(2)  # Z_SYNC_FLUSH
    message = struct.pack(">BxH", 0, 1) + struct.pack(">HHHHi", 0, 0, 2, 1, 6) + struct.pack(">I", len(data)) + data
    for byte in message:
        client.feed(bytes([byte]))
    assert client.updates == 1 and bytes(client.fb) == band


# --------------------------------------------------------------------------
# The websocket client
# --------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 5, 125, 126, 65535, 70000])
def test_a_masked_frame_reads_back(size):
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    reader = lb.FrameReader()
    reader.feed(lb.encode_frame(lb.OP_BINARY, payload))
    assert reader.next() == (lb.OP_BINARY, payload)
    assert reader.next() is None


def test_fragments_are_joined_and_control_frames_pass_between_them():
    reader = lb.FrameReader()
    reader.feed(bytes([0x01, 3]) + b"abc")            # text, not final
    reader.feed(bytes([0x89, 1]) + b"p")              # a ping in between
    reader.feed(bytes([0x80, 2]) + b"de")             # final continuation
    assert reader.next() == (lb.OP_PING, b"p")
    assert reader.next() == (lb.OP_TEXT, b"abcde")


def _server(script):
    """A one-connection websocket server on loopback running `script(conn, headers)`."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    seen = {}

    def run():
        conn, _ = listener.accept()
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        head = request.split(b"\r\n\r\n")[0].decode()
        headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:])
        seen.update(headers, path=head.split(" ")[1])
        accept = base64.b64encode(hashlib.sha1(headers["Sec-WebSocket-Key"].encode()
                                               + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        script(conn, seen)
        conn.close()
        listener.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return listener.getsockname()[1], seen, thread


def test_the_client_sends_its_ticket_first_and_reads_the_refusal_code():
    got = {}

    def script(conn, seen):
        reader = lb.FrameReader()
        while (message := reader.next()) is None:
            reader.feed(conn.recv(4096))
        got["first"] = message
        conn.sendall(bytes([0x88, 2]) + struct.pack(">H", lb.CLOSE_REFUSED))
        conn.recv(4096)

    port, seen, thread = _server(script)
    ws = lb.WebSocket(f"ws://127.0.0.1:{port}/v1/live/ls_1/stream", {"Authorization": "Bearer x"})
    ws.send(lb.OP_TEXT, json.dumps({"ticket": "t"}).encode())
    assert ws.recv(5) is None
    thread.join(5)
    assert ws.closed and ws.close_code == lb.CLOSE_REFUSED
    assert got["first"] == (lb.OP_TEXT, b'{"ticket": "t"}')
    assert seen["path"] == "/v1/live/ls_1/stream", "no ticket, nothing at all, in the URL"
    assert seen["Authorization"] == "Bearer x"


def test_socket_urls_carry_no_query():
    assert lb.socket_url("https://api.example/", "/v1/live/ls_1/stream") == "wss://api.example/v1/live/ls_1/stream"
    assert lb.socket_url("http://127.0.0.1:8080", "/v1/live/a/stream") == "ws://127.0.0.1:8080/v1/live/a/stream"


def test_a_repointed_ticket_names_the_other_tenant_and_keeps_the_mac():
    claims = {"tenant_id": "smoke", "session_id": "ls_1"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    mac = "m" * 43
    forged = lb.repoint(payload + "." + mac, "eng")
    head, _, tail = forged.partition(".")
    assert tail == mac
    assert json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))["tenant_id"] == "eng"


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def test_the_sweep_finds_the_password_raw_and_inside_a_checkpoint_archive():
    needle = lb.new_password().encode()
    assert lb.contains(b"log line " + needle + b"\n", needle)
    archive = gzip.compress(b"tar header" + needle) + gzip.compress(b"second member")
    assert lb.contains(archive, needle)
    assert lb.contains(gzip.compress(b"a") + gzip.compress(needle), needle), "every gzip member is read"
    assert not lb.contains(gzip.compress(b"clean"), needle)


@pytest.mark.parametrize("name,kind", [
    ("tenants/s/tasks/t/attempts/a/logs/agent_stdout.log", "transcript"),
    ("tenants/s/tasks/t/attempts/a/logs/live/agent_stdout.tail.log", "transcript"),
    ("tenants/s/tasks/t/attempts/a/logs/stderr.log", "logs"),
    ("tenants/s/tasks/t/attempts/a/checkpoints/c1/archive.tar.gz", "checkpoints"),
    ("tenants/s/tasks/t/attempts/a/live/rec-1760000000000.live.webm", "recording"),
    ("tenants/s/tasks/t/attempts/a/artifacts/session-code.txt", "outputs"),
])
def test_every_object_is_counted_in_a_category(name, kind):
    assert lb.category(name) == kind


# --------------------------------------------------------------------------
# The verdicts, and how the shell group reads them
# --------------------------------------------------------------------------


def test_the_shell_group_lists_one_check_per_verdict_key_in_order():
    text = GROUP.read_text()
    keys = re.search(r"LIVE_BROWSER_KEYS=\(([^)]*)\)", text).group(1).split()
    assert tuple(keys) == lb.CHECKS
    listed = re.search(r"live_browser_checks\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    assert len(listed.splitlines()) == len(lb.CHECKS)
    assert all(line.startswith("live-browser: ") for line in listed.splitlines())


def test_the_shell_group_never_holds_the_password():
    text = GROUP.read_text()
    assert "new_password" not in text and "password=" not in text.lower()
    assert "tests/acceptance/test_live_browser.py" in text
    assert "--credential-file" in text and "umask 077" in text


@pytest.mark.parametrize("state,verdict", [
    ("PARKED:CREDENTIAL_MISSING", "SKIP"),
    ("HELD:QUEUED:pool-gke MANUAL_PAUSE limit 0", "SKIP"),
    ("RUNNING", "FAIL"),
])
def test_a_task_that_did_not_run_is_skipped_only_when_the_platform_holds_it(state, verdict):
    assert lb.not_run_reason(state)[0] == verdict


def _run(tmp_path, monkeypatch, settled, answers):
    (tmp_path / "settled").write_text(settled)
    results = tmp_path / "results.jsonl"
    cred = tmp_path / "cred"
    cred.write_text("c" * 10)

    def call(self, method, path, body=None, *, tenant=None):
        for suffix, answer in answers.items():
            if path.endswith(suffix):
                return answer
        return 404, None

    monkeypatch.setattr(lb.Api, "call", call)
    monkeypatch.setattr(lb.time, "sleep", lambda s: None)
    lb.main(["--task", "task_1", "--tenant", "smoke", "--api", "http://127.0.0.1:9",
             "--bucket", "b", "--control-dir", str(tmp_path), "--results", str(results),
             "--credential-file", str(cred), "--timeout", "5"])
    assert not cred.exists(), "the credential file is read once and deleted"
    lines = [json.loads(l) for l in results.read_text().splitlines()]
    assert sorted(l["check"] for l in lines) == sorted(lb.CHECKS), "one verdict per check"
    return {l["check"]: l["verdict"] for l in lines}


def test_a_held_task_skips_every_check(tmp_path, monkeypatch):
    got = _run(tmp_path, monkeypatch, "PARKED:CREDENTIAL_MISSING",
               {"/live/session": (200, {"available": False})})
    assert set(got.values()) == {"SKIP"}


def test_a_deployment_with_no_live_path_skips_saying_so(tmp_path, monkeypatch):
    got = _run(tmp_path, monkeypatch, "SUCCEEDED",
               {"/live/session": (200, {"available": False}),
                "/answer": (200, {"status": "ok", "content": "HANDOFF-REFUSED no_live_path"})})
    assert set(got.values()) == {"SKIP"}


def test_a_live_path_that_refused_is_a_failure(tmp_path, monkeypatch):
    got = _run(tmp_path, monkeypatch, "SUCCEEDED",
               {"/live/session": (200, {"available": False}),
                "/answer": (200, {"status": "ok", "content": "HANDOFF-REFUSED gate_unavailable"})})
    assert got["handoff"] == "FAIL"


def test_an_agent_that_never_asked_is_a_failure(tmp_path, monkeypatch):
    got = _run(tmp_path, monkeypatch, "SUCCEEDED",
               {"/live/session": (200, {"available": False}),
                "/answer": (200, {"status": "ok", "content": "done"})})
    assert got["handoff"] == "FAIL"


# --------------------------------------------------------------------------
# Harness defects the scheduled runs showed (lane SPEED-C2, 2026-10-14)
# --------------------------------------------------------------------------

ATTEMPT = "tenants/smoke/tasks/task_1/attempts/"


@pytest.mark.parametrize("name", [
    # The worker's live log tails (agent_worker/runners/streams.py): under the
    # attempt's logs/live/, not the recording's live/. 8 of the 14 failed
    # live-browser runs read 2026-10-12..14 failed `recording` on exactly these
    # two objects ("2 object(s) under live/ are not recording segments").
    ATTEMPT + "att_1/logs/live/agent_stdout.tail.log",
    ATTEMPT + "att_1/logs/live/agent_stderr.tail.log",
    ATTEMPT + "att_1/live/rec-1760000000000.live.webm",
    ATTEMPT + "att_1/live/actions-1760000000000.live.jsonl",
    ATTEMPT + "att_1/artifacts/live/notes.txt",
])
def test_only_the_recording_s_own_live_directory_is_judged_for_strays(name):
    """MUTATION: go back to `"/live/" in name`. The log tails read as strays
    and `recording` fails on every run that streamed its logs."""
    assert lb.stray_live_objects(ATTEMPT, [name]) == []


@pytest.mark.parametrize("name", [
    ATTEMPT + "att_1/live/rec-1760000000000.webm",
    ATTEMPT + "att_1/live/frames/0001.png",
    ATTEMPT + "att_1/live/password.txt",
])
def test_anything_else_in_the_recording_s_live_directory_is_a_stray(name):
    assert lb.stray_live_objects(ATTEMPT, [name]) == [name]


def test_the_swarm_verify_image_carries_gnu_sed_for_redact():
    """`redact` (scripts/lib/common.sh) is a sed -E program with `#`
    delimiters that busybox sed cannot compile ("sed: unmatched '#'"), and the
    swarm-verify image is Alpine. 13 of the 14 failed live-browser logs read
    2026-10-12..14 print that error once per check, and every verdict reason
    the group pipes through redact came out empty. MUTATION: drop `sed` from
    the apk line."""
    dockerfile = (ROOT / "images" / "swarm-verify" / "Dockerfile").read_text()
    (line,) = [l for l in dockerfile.splitlines() if l.startswith("RUN apk add")]
    assert "sed" in line.split(), line
