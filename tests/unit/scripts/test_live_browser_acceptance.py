"""The live-browser acceptance check's offline half (lane LB-L13, part of #1030).

`tests/acceptance/test_live_browser.py` runs against a deployment, so what
can be proven without one is proven here, with no credentials and no network
beyond loopback:

* its RFB viewer speaks to the REAL gate server (`livegate.rfb.RfbSession`):
  the framebuffer it keeps is the gate's frame, and its key events reach the
  injector only once its connection holds control;
* its websocket client upgrades, sends masked frames, reads a server's
  close code, and its frame reader joins fragments;
* its frame judge passes a frame whose field box is the field as drawn (its
  own magenta; BGD 2026-10-25: the live view paints nothing) and FAILS one
  where the field is painted over, even partly -- at device scale 1 and 2,
  exactly (RFB) and with VP8-like noise (the recording);
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
import shutil
import socket
import struct
import subprocess
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


#: What the gate's mask painted a field with until LIVE-NOMASK (#2298).
OLD_MASK_RGB = (0x22, 0x22, 0x22)


def _page(scale=1, top=120, left=40, bpp=4, painted=False, width=1280, height=800):
    """The fixture as it would reach a viewer: page area starting `top` px
    down (the browser's own bar above it), marker and field at the page's CSS
    positions times `scale`, the field as drawn, or painted over as the old
    mask did (dilated 8 px)."""
    pixels = _frame(width, height, bpp)
    mx, my = left + 40 * scale, top + 120 * scale
    _fill(pixels, width, (mx, my, mx + 320 * scale, my + 40 * scale), lb.MARKER_RGB, bpp)
    fx, fy = left + 40 * scale, top + 200 * scale
    field = (fx, fy, fx + 320 * scale, fy + 40 * scale)
    _fill(pixels, width, field, lb.FIELD_RGB, bpp)
    if painted:
        d = 8
        _fill(pixels, width, (field[0] - d, field[1] - d, field[2] + d, field[3] + d), OLD_MASK_RGB, bpp)
    return pixels, field


def test_the_judge_s_mask_colour_is_the_one_the_gate_used_to_paint_and_the_gate_paints_none():
    from agent_worker.livegate import mask

    assert lb.MASK_RGB == OLD_MASK_RGB
    assert not hasattr(mask, "MASK_RGB")


@pytest.mark.parametrize("scale", [1, 2])
def test_a_frame_whose_field_is_shown_as_drawn_passes(scale):
    pixels, field = _page(scale=scale)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "shown", verdict.describe()
    assert (verdict.field.x0, verdict.field.y0, verdict.field.x1, verdict.field.y1) == field
    assert verdict.shown == 1.0 and verdict.painted == 0.0


def test_a_frame_whose_field_is_painted_over_is_covered():
    pixels, _ = _page(painted=True)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "covered"
    assert verdict.painted == 1.0 and verdict.shown == 0.0


def test_one_painted_row_of_the_field_is_covered():
    """MUTATION: a box painted over part of the field (rows of it) still fails."""
    pixels, field = _page()
    _fill(pixels, 1280, (field[0], field[1] + 10, field[2], field[1] + 12), OLD_MASK_RGB)
    verdict = lb.judge(bytes(pixels), 1280, 800)
    assert verdict.kind == "covered", verdict.describe()


def test_the_pointers_arrow_on_the_field_is_still_shown():
    """A viewer with no local cursor is sent the arrow painted on its copy (rfb.py)."""
    pixels, field = _page()
    x, y = (field[0] + field[2]) // 2, (field[1] + field[3]) // 2
    _fill(pixels, 1280, (x, y, x + 12, y + 19), (0, 0, 0))
    assert lb.judge(bytes(pixels), 1280, 800).kind == "shown"


def test_a_frame_without_the_page_is_not_judged():
    blanked = _frame()
    _fill(blanked, 1280, (0, 120, 1280, 800), OLD_MASK_RGB)  # a page area painted out
    assert lb.judge(bytes(blanked), 1280, 800).kind == "no_page"
    unavailable = bytearray(bytes([0x55, 0x55, 0x55, 0]) * (1280 * 800))
    assert lb.judge(bytes(unavailable), 1280, 800).kind == "no_page"


def test_the_recording_is_judged_with_a_tolerance_at_the_live_frames_box():
    pixels, field = _page(bpp=3)
    rng = random.Random(7)
    noisy = bytes(max(0, min(255, b + rng.randint(-12, 12))) for b in pixels)
    box = lb.Box(*field)
    assert lb.judge(noisy, 1280, 800, bpp=3, tol=24, field=box).kind == "shown"
    covered, _ = _page(bpp=3, painted=True)
    assert lb.judge(bytes(covered), 1280, 800, bpp=3, tol=24, field=box).kind == "covered"


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
    # The field and the marker do not touch, so the judge finds each alone.
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


def _gate_frame(width=320, height=200):
    from agent_worker.livegate.capture import Frame

    frame = Frame.blank(width, height, GREEN)
    frame.fill(10, 20, 130, 40, lb.MARKER_RGB)
    frame.fill(10, 60, 110, 80, lb.FIELD_RGB)
    return frame


async def _session(handler):
    from agent_worker.livegate import rfb

    hub = rfb.FrameHub(320, 200)
    hub.publish(_gate_frame())
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

        hub.publish(changed)
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


def _qa_listed(text: str) -> list[str]:
    return re.search(r"live_browser_qa_checks\(\) \{\n  cat <<'EOF2'\n(.*?)\nEOF2", text, re.S).group(1).splitlines()


def test_the_shell_group_lists_one_qa_identity_check_per_verdict_key_in_order():
    """QI-ACCEPT (docs/design/qa-identity.md §8, part of #1649): the group's QA
    identity case reports the four verdicts `--qa-identity` writes, in its
    order. MUTATION: drop qa_no_leak from LIVE_BROWSER_QA_KEYS."""
    text = GROUP.read_text()
    keys = re.search(r"LIVE_BROWSER_QA_KEYS=\(([^)]*)\)", text).group(1).split()
    assert tuple(keys) == lb.QA_IDENTITY_CHECKS
    listed = _qa_listed(text)
    assert len(listed) == len(lb.QA_IDENTITY_CHECKS)
    assert all(line.startswith("live-browser: ") for line in listed)
    labels = dict(zip(lb.QA_IDENTITY_CHECKS, listed))
    assert lb.QA_HOST_REFUSAL in labels["qa_handoff_refused"]
    assert lb.QA_NOT_GRANTED in labels["qa_ungranted_refused"]


def test_the_qa_identity_case_runs_after_the_group_s_own_checks():
    """The case was drafted and never called (TRIAGE-B02, PR 2530): until the
    group calls it, `--qa-identity` runs nowhere. MUTATION: remove the call."""
    text = GROUP.read_text()
    body = re.search(r"^run_live_browser\(\) \{\n(.*?)\n\}\n", text, re.S | re.M).group(1)
    assert body.rstrip().endswith("run_live_browser_qa")
    qa = re.search(r"^run_live_browser_qa\(\) \{\n(.*?)\n\}\n", text, re.S | re.M).group(1)
    assert "--qa-identity" in qa and "qa-identity-submission" in qa
    assert 'SWARM_API_TENANT="${SWARM_ACCEPTANCE_QA_TENANT}"' in qa
    assert qa.rstrip().endswith('SWARM_API_TENANT="${saved}"'), "the suite's own tenant comes back"


#: Stand-ins for what run.sh sources (common.sh, testlib.sh, lib.sh): each
#: prints what it was asked, and anything that would reach a deployment says so.
_QA_STUBS = """
step() { printf 'STEP %s\\n' "$1"; }
die() { printf 'DIE %s\\n' "$*"; exit 1; }
acc_check() { printf 'CHECK %s\\n' "$1"; }
acc_skip() { printf 'SKIP %s\\n' "$1"; }
acc_pass() { printf 'PASS %s\\n' "$1"; }
acc_fail() { printf 'FAIL %s\\n' "$1"; }
acc_submit() { printf 'SUBMITTED\\n'; return 1; }
python3() { printf 'PYTHON\\n'; return 1; }
api_credential() { printf 'CREDENTIAL\\n'; }
"""


@pytest.mark.parametrize("tenant,console", [
    ("", ""),
    ("smoke", ""),
    ("", "https://console.example.test"),
])
def test_the_qa_identity_case_skips_every_check_unless_both_settings_are_set(tmp_path, tenant, console):
    """Without a granted tenant and the console's URL the case cannot be
    measured: every check is a SKIP that names both settings, and nothing is
    submitted. MUTATION: test only SWARM_ACCEPTANCE_QA_TENANT."""
    if shutil.which("bash") is None:
        pytest.skip("bash is required")
    env = {"PATH": "/usr/bin:/bin", "SWARM_API_TENANT": "smoke", "ACC_WORK": str(tmp_path)}
    # An unset setting is absent from the environment, as on a job that never declared it.
    for name, value in (("SWARM_ACCEPTANCE_QA_TENANT", tenant), ("SWARM_ACCEPTANCE_CONSOLE_URL", console)):
        if value:
            env[name] = value
    proc = subprocess.run(
        ["bash", "-c", _QA_STUBS + 'source "$1"; run_live_browser_qa; printf "TENANT %s\\n" "${SWARM_API_TENANT}"',
         "_", str(GROUP)],
        capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    checks = [l[len("CHECK "):] for l in lines if l.startswith("CHECK ")]
    skips = [l[len("SKIP "):] for l in lines if l.startswith("SKIP ")]
    assert checks == _qa_listed(GROUP.read_text())
    assert len(skips) == len(checks) == len(lb.QA_IDENTITY_CHECKS)
    for reason in skips:
        assert reason.startswith("not measured")
        assert "SWARM_ACCEPTANCE_QA_TENANT" in reason and "SWARM_ACCEPTANCE_CONSOLE_URL" in reason
    assert not [l for l in lines if l in ("SUBMITTED", "PYTHON", "CREDENTIAL") or l.startswith(("PASS", "FAIL", "DIE"))]
    assert lines[-1] == "TENANT smoke"


def test_the_frames_and_recording_labels_say_the_field_is_as_drawn():
    """The checks pass a field shown as Chromium drew it and fail one painted
    over (BGD, 2026-10-25: the live view paints nothing), so a label saying
    "painted" names the opposite of what is checked (LIVE-NOMASK-TIDY)."""
    text = GROUP.read_text()
    listed = re.search(r"live_browser_checks\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF", text, re.S).group(1)
    labels = dict(zip(lb.CHECKS, listed.splitlines()))
    assert not [line for line in labels.values() if "painted" in line]
    assert "as drawn" in labels["frames"] and "as drawn" in labels["recording"]


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
    every apk line. The image has more than one apk line (the hourly doctor's
    py3-yaml/py3-pydantic, DAYONE-DOCTOR-1), so any of them may carry it."""
    dockerfile = (ROOT / "images" / "swarm-verify" / "Dockerfile").read_text()
    lines = [l for l in dockerfile.splitlines() if l.startswith("RUN apk add")]
    assert lines, dockerfile
    assert any("sed" in line.split() for line in lines), lines


# --------------------------------------------------------------------------
# The scheduled runs that ended PARKED (lane LIVEBROWSER-SCHED, part of #1333)
# --------------------------------------------------------------------------
#
# 2026-10-15..19, 17 scheduled runs: `returned` FAILED "the task did not
# finish in time (last state PARKED)" after `handoff` PASSED, the job ending
# ~17 minutes in (the 900 s settle and its setup). The task had run and asked;
# PARKED after an attempt is the worker's park, and the one a hand-off makes
# is HUMAN_REQUIRED, which only a person promotes (swarm_api.handoff).


def test_the_viewer_opens_the_live_view_before_it_probes_isolation(tmp_path, monkeypatch):
    """swarm-api counts a person as watching from the viewer socket's join
    (`LiveService.watched`), and a hold nobody watches ends after
    `human_unwatched_seconds` (60). The isolation probes are six API calls
    and a socket that may wait 20 s for its refusal; run first, they spent
    the person's minute, the worker parked the hold unwatched, and the task
    sat PARKED(HUMAN_REQUIRED). A person opens the view first; so does this.
    MUTATION: call check_isolation before drive_viewer again."""
    order: list[str] = []

    class Socket:
        closed = False
        close_code = None

        def __init__(self, url, headers, timeout=30.0):
            order.append("socket")

        def send(self, opcode, payload):
            pass

        def recv(self, timeout):
            return None

        def close(self):
            self.closed = True

    def call(self, method, path, body=None, *, tenant=None):
        if path.endswith("/live/ticket"):
            return 200, {"ticket": "t" * 8, "session_id": "ls_1"}
        if path.endswith("/live/control"):
            order.append("control")
            return 409, {"error": {"code": "stop_here"}}
        return 404, None

    session = {"session_id": "ls_1", "state": "open", "human_wait": {"reason": "sign in"}}
    monkeypatch.setattr(lb, "wait_for_handoff", lambda api, cfg: (session, None))
    monkeypatch.setattr(lb, "check_isolation",
                        lambda api, cfg, s, results: order.append("isolation"))
    monkeypatch.setattr(lb, "WebSocket", Socket)
    monkeypatch.setattr(lb.Viewer, "pump", lambda self, seconds, until=None: True)
    monkeypatch.setattr(lb.Api, "call", call)
    monkeypatch.setattr(lb, "wait_settled", lambda cfg, seconds: "PARKED:HUMAN_REQUIRED")
    monkeypatch.setattr(lb, "sweep", lambda *a: {})
    monkeypatch.setattr(lb, "check_recording", lambda *a: None)
    cfg = lb.Config(task="task_1", tenant="smoke", api="http://127.0.0.1:9", bucket="b",
                    control_dir=str(tmp_path), results=str(tmp_path / "r.jsonl"), timeout=5,
                    other_tenant=None, credential="c" * 10, gcs_token=None)
    lb.run(cfg)
    assert order == ["socket", "isolation", "control"], order


def test_isolation_is_still_judged_when_the_viewer_never_got_a_frame(tmp_path, monkeypatch):
    """The probes moved behind the first frame; a viewer that fails before it
    must not leave `isolation` unjudged ("not judged" is a FAIL)."""
    seen: list[str] = []

    def call(self, method, path, body=None, *, tenant=None):
        return 503, None  # the ticket cannot be minted: drive_viewer stops at once

    session = {"session_id": "ls_1", "state": "open", "human_wait": {"reason": "sign in"}}
    monkeypatch.setattr(lb, "wait_for_handoff", lambda api, cfg: (session, None))
    monkeypatch.setattr(lb, "check_isolation",
                        lambda api, cfg, s, results: (seen.append("isolation"),
                                                      results.put("isolation", "PASS", "probed")))
    monkeypatch.setattr(lb.Api, "call", call)
    monkeypatch.setattr(lb, "wait_settled", lambda cfg, seconds: "SUCCEEDED")
    monkeypatch.setattr(lb, "sweep", lambda *a: {})
    monkeypatch.setattr(lb, "check_recording", lambda *a: None)
    monkeypatch.setattr(lb, "check_returned", lambda *a: None)
    results = tmp_path / "r.jsonl"
    cfg = lb.Config(task="task_1", tenant="smoke", api="http://127.0.0.1:9", bucket="b",
                    control_dir=str(tmp_path), results=str(results), timeout=5,
                    other_tenant=None, credential="c" * 10, gcs_token=None)
    lb.run(cfg)
    verdicts = {json.loads(l)["check"]: json.loads(l)["verdict"] for l in results.read_text().splitlines()}
    assert seen == ["isolation"] and verdicts["isolation"] == "PASS"


@pytest.mark.parametrize("state,words", [
    ("PARKED:HUMAN_REQUIRED", ["PARKED", "HUMAN_REQUIRED", "nobody in control"]),
    ("PARKED:PROVIDER_COOLDOWN:eligible again at 2026-10-19T05:40:00Z",
     ["PARKED", "PROVIDER_COOLDOWN", "eligible again at 2026-10-19T05:40:00Z"]),
    ("PARKED", ["PARKED", "no park_reason"]),
    ("RUNNING", ["RUNNING"]),
])
def test_a_task_still_waiting_names_its_state_and_park_reason(state, words):
    """MUTATION: return the old "did not finish in time (last state ...)"."""
    verdict, reason = lb.not_run_reason(state)
    assert verdict == "FAIL"
    assert "did not finish in time" not in reason
    for word in words:
        assert word in reason, (word, reason)


def _stuck(doc: dict) -> str:
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; live_browser_stuck "$2"', "_", str(GROUP), json.dumps(doc)],
        capture_output=True, text=True, check=True, timeout=30)
    return proc.stdout


@pytest.mark.parametrize("doc,want", [
    ({"state": "PARKED", "park_reason": "HUMAN_REQUIRED", "attempt_count": 1}, "PARKED:HUMAN_REQUIRED"),
    ({"state": "PARKED", "park_reason": "PROVIDER_COOLDOWN", "next_eligible_at": "2026-10-19T05:40:00Z"},
     "PARKED:PROVIDER_COOLDOWN:eligible again at 2026-10-19T05:40:00Z"),
    ({"state": "PARKED"}, "PARKED"),
    ({"state": "RUNNING", "park_reason": "HUMAN_REQUIRED"}, "RUNNING"),
    ({}, "MISSING"),
])
def test_the_group_hands_the_viewer_the_park_reason_it_stopped_on(doc, want):
    """The settle printed only `PARKED`, so the verdict could not say which
    park. The group reads the task once more and writes the reason, in the
    `PARKED:<reason>` shape acc_settle already uses for CREDENTIAL_MISSING."""
    assert _stuck(doc) == want


def test_the_group_reads_the_park_reason_when_the_settle_runs_out():
    text = GROUP.read_text()
    assert 'live_browser_stuck "${doc}"' in text


# --------------------------------------------------------------------------
# The viewer's watch for the whole hold (lane LIVEBROWSER-WATCH, part of #1333)
# --------------------------------------------------------------------------
#
# Still PARKED(HUMAN_REQUIRED) after LIVEBROWSER-SCHED (scheduled run
# 39264477271, 2026-10-19 13:56Z). Three defects end that way and the verdict
# could not tell them apart: the join not counted as watching, the viewer's
# socket gone or unserved before control, or the worker parking a hold that
# was being watched.


class _QuietSocket:
    """A console socket that stays open and says nothing; it notes each read."""

    closed = False
    close_code = None

    def __init__(self, url, headers, timeout=30.0):
        self.read = threading.Event()

    def send(self, opcode, payload):
        pass

    def recv(self, timeout):
        self.read.set()
        return None

    def close(self):
        self.closed = True


def _cfg(tmp_path):
    return lb.Config(task="task_1", tenant="smoke", api="http://127.0.0.1:9", bucket="b",
                     control_dir=str(tmp_path), results=str(tmp_path / "r.jsonl"), timeout=5,
                     other_tenant=None, credential="c" * 10, gcs_token=None)


def test_the_viewer_keeps_reading_its_socket_while_the_probes_run(tmp_path, monkeypatch):
    """A person with the view open keeps receiving it. Inline, the probes
    (six API calls and a socket held up to 20 s) left the viewer's socket
    unread for as long as they took. MUTATION: call `while_watching()` inline
    in drive_viewer again; the probe sees no read and this fails."""
    sockets: list[_QuietSocket] = []
    seen: list[bool] = []

    def socket_(url, headers, timeout=30.0):
        sockets.append(_QuietSocket(url, headers, timeout))
        return sockets[-1]

    def call(self, method, path, body=None, *, tenant=None):
        if path.endswith("/live/ticket"):
            return 200, {"ticket": "t" * 8, "session_id": "ls_1"}
        return 409, {"error": {"code": "stop_here"}}  # take control: stop there

    real_pump = lb.Viewer.pump
    frames = {"waited": False}

    def pump(self, seconds, until=None):
        if not frames["waited"]:  # the first frame: on screen at once
            frames["waited"] = True
            return True
        return real_pump(self, seconds, until)

    def probe():
        sockets[0].read.clear()
        seen.append(sockets[0].read.wait(3.0))

    monkeypatch.setattr(lb, "WebSocket", socket_)
    monkeypatch.setattr(lb.Viewer, "pump", pump)
    monkeypatch.setattr(lb.Api, "call", call)
    api = lb.Api("http://127.0.0.1:9", "c" * 10, "smoke")
    lb.drive_viewer(api, _cfg(tmp_path), {"session_id": "ls_1"}, "p" * 8, lb.Results(str(tmp_path / "r")),
                    while_watching=probe)
    assert seen == [True]


def test_what_a_probe_raises_is_raised_once_the_viewer_has_read(monkeypatch):
    class Viewer:
        pumped = 0

        def pump(self, seconds, until=None):
            Viewer.pumped += 1
            while until is not None and not until():
                pass
            return True

    def boom():
        raise RuntimeError("probe")

    with pytest.raises(RuntimeError, match="probe"):
        lb.watch_while(Viewer(), boom, seconds=5)
    assert Viewer.pumped == 1


OPENED = "2026-10-19T13:56:40+00:00"


@pytest.mark.parametrize("joined,words,absent", [
    # The join counted: the deadline moved past the one the request opened.
    ("2026-10-19T13:57:05+00:00", ["moved the hold's deadline", OPENED, "13:57:05"], ["NOT"]),
    # Not counted: the deadline is the opening one.
    (OPENED, ["did NOT move", "LiveService.watched"], ["moved the hold's deadline from"]),
    (None, ["not read after this viewer's join"], ["NOT"]),
])
def test_the_hold_says_whether_the_join_was_counted(joined, words, absent):
    hold = lb.Hold(opened=OPENED, joined=joined)
    said = hold.describe()
    for word in words:
        assert word in said, (word, said)
    for word in absent:
        assert word not in said, (word, said)


def _event(kind, detail, attempt="att_1"):
    return {"type": kind, "attempt_id": attempt, "detail": detail}


def test_a_human_park_names_its_cause_from_the_tasks_events():
    """MUTATION: drop the parked event's `cause` from the explanation."""
    events = [
        _event("human_handoff_requested", {"request_id": "h-1"}),
        _event("human_handoff_resolved", {"outcome": "expired", "held_seconds": 61,
                                          "funnel": {"first_viewer_s": 4, "control_taken_s": None}}),
        _event("parked", {"park_phase": "human_wait", "cause": "gate_lost"}),
    ]
    said = lb.explain_human_park(events, lb.Hold(opened=OPENED, joined="2026-10-19T13:57:05+00:00"))
    for word in ("asked for help 1 time", "resolved expired after 61 s", "a viewer counted at 4 s",
                 "parked on gate_lost", "moved the hold's deadline"):
        assert word in said, (word, said)


def test_a_return_that_re_queued_the_task_is_named():
    """The return landed after the park: READY, a second attempt, a second
    request nobody watched (LB-RETURN)."""
    events = [
        _event("human_handoff_requested", {}),
        _event("human_handoff_resolved", {"outcome": "expired", "held_seconds": 60,
                                          "funnel": {"first_viewer_s": None}}),
        _event("parked", {"park_phase": "human_wait", "cause": "hold_expired"}),
        _event("ready", {"reason": "human_returned"}),
        _event("human_handoff_requested", {}, attempt="att_2"),
    ]
    said = lb.explain_human_park(events, None)
    assert "asked for help 2 time" in said
    assert "with no viewer counted" in said
    assert "re-queued the task" in said


def test_a_parked_verdict_carries_the_holds_readings_and_the_events(tmp_path, monkeypatch):
    """The scheduled run's `returned` FAIL said only "the hold ran out with
    nobody in control". It now says what the viewer read of the hold and how
    the task's hand-offs ended. MUTATION: put not_run_reason's reason alone."""
    session = {"session_id": "ls_1", "state": "open",
               "human_wait": {"reason": "sign in", "deadline": OPENED}}

    def call(self, method, path, body=None, *, tenant=None):
        if path.endswith("/live/session"):  # the join was not counted
            return 200, {"session": {"human_wait": {"deadline": OPENED}}}
        if "/events" in path:
            return 200, {"events": [_event("parked", {"park_phase": "human_wait",
                                                      "cause": "hold_expired"})]}
        return 404, None

    def drive(api, cfg, s, password, results, while_watching=None):
        while_watching()
        return None

    monkeypatch.setattr(lb, "wait_for_handoff", lambda api, cfg: (session, None))
    monkeypatch.setattr(lb, "check_isolation", lambda *a: None)
    monkeypatch.setattr(lb, "drive_viewer", drive)
    monkeypatch.setattr(lb.Api, "call", call)
    monkeypatch.setattr(lb, "wait_settled", lambda cfg, seconds: "PARKED:HUMAN_REQUIRED")
    monkeypatch.setattr(lb, "sweep", lambda *a: {})
    monkeypatch.setattr(lb, "check_recording", lambda *a: None)
    cfg = _cfg(tmp_path)
    lb.run(cfg)
    lines = [json.loads(l) for l in (tmp_path / "r.jsonl").read_text().splitlines()]
    (returned,) = [l for l in lines if l["check"] == "returned"]
    assert returned["verdict"] == "FAIL"
    for word in ("HUMAN_REQUIRED", "parked on hold_expired", "did NOT move"):
        assert word in returned["reason"], (word, returned["reason"])
