"""Input from the person in control, into Xvfb through XTEST (live browser L3).

docs/design/live-browser.md §4.2 and §5.6. When, and only when, a person holds
control, the gate injects that one connection's keys, pointer and paste into
the display Chromium draws on. WHETHER an event may be injected is decided in
`livegate.rfb` (`ControlState`); this module only does it.

HOW: THE X11 WIRE PROTOCOL, SPOKEN DIRECTLY. Like `livegate.capture`, which
reads Xvfb's framebuffer file instead of linking an X client, this needs no
Xlib binding and no third-party package. The gate opens Xvfb's unix socket in
its own `/tmp` (`/tmp/.X11-unix/X0`, §3.1 -- the worker container does not
mount it), reads the keyboard map once, and sends `XTestFakeInput` requests.
Not `xdotool`: a subprocess per key would put every keysym in an argv.

KEYSYMS TO KEYCODES. A keysym on index 0 of a keycode is pressed as it is. A
keysym on index 1 (the shifted one) is pressed as it is for a key event --
the viewer sends its own Shift press first, as a keyboard does -- and wrapped
in Shift for typed text (a paste, §5.6), which has no Shift of its own. A
keysym on no key at all (a `€`, an ideograph) is bound to a spare keycode with
`ChangeKeyboardMapping`, on both indexes so Shift does not change it, and
then pressed. Spare keycodes are reused round-robin.

WHAT IS HELD IS RELEASED. Every pressed key and button is tracked, and
`release_all` lets go of each one: control can end mid-chord (preempted, idle,
the hold expired), and a Shift left down in Xvfb would be held for the agent
and for the next person.

NOTHING HERE IS LOGGED BUT COUNTS (§5.3.3). No keysym, keycode, character or
coordinate reaches a log line; an X error is logged by its error code only.

NOT COVERED. X authorisation: Xvfb is started by the gate without `-auth`, on
a socket only the sidecar's uid can reach, so the setup sends no
authorisation data. A client that caches the keyboard map (Chromium does)
learns of a spare keycode's new binding from the `MappingNotify` the server
sends before the next request is processed; the X server orders this
connection's requests, so the binding is in place before the press.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
from typing import Protocol

log = logging.getLogger(__name__)

#: Where Xvfb `:0` listens, inside the sidecar's own /tmp (§3.1).
DEFAULT_DISPLAY_SOCKET = "/tmp/.X11-unix/X0"

_KEY_PRESS, _KEY_RELEASE, _BUTTON_PRESS, _BUTTON_RELEASE, _MOTION = 2, 3, 4, 5, 6
_X_QUERY_EXTENSION = 98
_X_CHANGE_KEYBOARD_MAPPING = 100
_X_GET_KEYBOARD_MAPPING = 101
_X_GET_INPUT_FOCUS = 43
_XTEST_FAKE_INPUT = 2

XK_SHIFT_L = 0xFFE1
_XK_RETURN = 0xFF0D
_XK_TAB = 0xFF09

#: RFB's button mask has 8 bits; X buttons 1-8 (4-7 are the wheel).
_BUTTONS = range(8)

#: A paste longer than this is not a password; it is refused whole (§5.6).
MAX_TYPED_CHARS = 1024


class InjectError(Exception):
    """The display cannot take injected input."""


class Injector(Protocol):
    """What `livegate.rfb` injects through. `XInjector` is the real one."""

    async def key(self, keysym: int, down: bool) -> None: ...

    async def pointer_event(self, buttons: int, x: int, y: int) -> None: ...

    async def type_text(self, text: str) -> bool: ...

    async def release_all(self) -> None: ...


def keysym_for(char: str) -> int | None:
    """The keysym that types `char`, or None for a control character."""
    if char == "\n" or char == "\r":
        return _XK_RETURN
    if char == "\t":
        return _XK_TAB
    cp = ord(char)
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return None
    if cp <= 0xFF:
        return cp  # Latin-1 keysyms are the code point
    return 0x01000000 | cp  # X's Unicode keysym range


def _pad(data: bytes) -> bytes:
    return data + b"\0" * (-len(data) % 4)


class XInjector:
    """One X connection to Xvfb, with the XTEST extension."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 root: int, min_keycode: int, max_keycode: int, xtest_opcode: int,
                 width: int, height: int) -> None:
        self._reader = reader
        self._writer = writer
        self._root = root
        self._min = min_keycode
        self._max = max_keycode
        self._xtest = xtest_opcode
        self.width = width
        self.height = height
        self._per_keycode = 0
        # keysym -> (keycode, index); first binding wins, as XKeysymToKeycode does.
        self._codes: dict[int, tuple[int, int]] = {}
        self._spare: list[int] = []
        self._spare_next = 0
        self._spare_bound: dict[int, int] = {}  # spare keycode -> keysym bound now
        self._keys_down: dict[int, int] = {}  # keysym -> keycode it was pressed on
        self._buttons_down: set[int] = set()
        self._position: tuple[int, int] | None = None
        self._seq = 0
        self._errors = 0
        self._sync_waiters: dict[int, asyncio.Future[None]] = {}
        self._drain_task: asyncio.Task[None] | None = None
        self.sent = 0
        self.unmappable = 0

    # -- connection -------------------------------------------------------

    @classmethod
    async def open(cls, path: str = DEFAULT_DISPLAY_SOCKET, *, width: int, height: int) -> "XInjector":
        try:
            reader, writer = await asyncio.open_unix_connection(path)
        except OSError as exc:
            raise InjectError(f"display unreachable: {exc.strerror or type(exc).__name__}") from None
        try:
            injector = await cls._setup(reader, writer, width=width, height=height)
        except BaseException:
            writer.close()
            raise
        return injector

    @classmethod
    async def _setup(cls, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                     width: int, height: int) -> "XInjector":
        # Little-endian, protocol 11.0, no authorisation.
        writer.write(struct.pack("<BxHHHH2x", ord("l"), 11, 0, 0, 0))
        await writer.drain()
        try:
            status, _, _major, _minor, length = struct.unpack("<BBHHH", await reader.readexactly(8))
            body = await reader.readexactly(length * 4)
        except asyncio.IncompleteReadError:
            raise InjectError("display closed the connection during setup") from None
        if status != 1:
            raise InjectError("display refused the connection")
        (vendor_len, _max_req, _screens, formats, _, _, _, _, min_kc, max_kc) = struct.unpack_from(
            "<HHBBBBBBBB", body, 16)
        screen_at = 32 + vendor_len + (-vendor_len % 4) + 8 * formats
        (root,) = struct.unpack_from("<I", body, screen_at)

        # Requests before the drain loop starts: replies are read inline.
        seq = 0
        name = b"XTEST"
        writer.write(struct.pack("<BxHH2x", _X_QUERY_EXTENSION, 2 + len(_pad(name)) // 4, len(name))
                     + _pad(name))
        seq += 1
        reply = await _read_reply(reader, seq)
        present, opcode = reply[8], reply[9]
        if not present:
            raise InjectError("display has no XTEST extension")

        count = max_kc - min_kc + 1
        writer.write(struct.pack("<BxHBB2x", _X_GET_KEYBOARD_MAPPING, 2, min_kc, count))
        seq += 1
        reply = await _read_reply(reader, seq)
        per = reply[1]
        (n,) = struct.unpack_from("<I", reply, 4)
        syms = struct.unpack_from(f"<{n}I", reply, 32)

        injector = cls(reader, writer, root=root, min_keycode=min_kc, max_keycode=max_kc,
                       xtest_opcode=opcode, width=width, height=height)
        injector._seq = seq
        injector._per_keycode = per
        for i in range(count):
            row = syms[i * per:(i + 1) * per]
            keycode = min_kc + i
            if not any(row):
                injector._spare.append(keycode)
                continue
            for index, sym in enumerate(row[:2]):
                if sym and sym not in injector._codes:
                    injector._codes[sym] = (keycode, index)
        injector._drain_task = asyncio.create_task(injector._drain())
        log.info("live inject: display open keycodes=%d spare=%d", count, len(injector._spare))
        return injector

    async def _drain(self) -> None:
        """Read and discard what the server sends unasked (errors, MappingNotify)."""
        try:
            while True:
                head = await self._reader.readexactly(32)
                kind = head[0]
                if kind == 1:  # a reply: only GetInputFocus, from sync()
                    (extra,) = struct.unpack_from("<I", head, 4)
                    if extra:
                        await self._reader.readexactly(extra * 4)
                    (seq,) = struct.unpack_from("<H", head, 2)
                    waiter = self._sync_waiters.pop(seq, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                elif kind == 0:
                    self._errors += 1
                    log.warning("live inject: X error code=%d", head[1])
        except (asyncio.IncompleteReadError, ConnectionError):
            log.info("live inject: display connection closed")
        finally:
            for waiter in self._sync_waiters.values():
                if not waiter.done():
                    waiter.set_exception(InjectError("display connection closed"))
            self._sync_waiters.clear()

    async def sync(self) -> None:
        """Return once the X server has processed every request sent so far."""
        self._writer.write(struct.pack("<BxH", _X_GET_INPUT_FOCUS, 1))
        self._seq = (self._seq + 1) & 0xFFFF
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sync_waiters[self._seq] = waiter
        await self._writer.drain()
        await waiter

    async def close(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except ConnectionError:
            pass
        log.info("live inject: closed sent=%d unmappable=%d errors=%d",
                 self.sent, self.unmappable, self._errors)

    # -- requests ---------------------------------------------------------

    def _request(self, data: bytes) -> None:
        self._writer.write(data)
        self._seq = (self._seq + 1) & 0xFFFF

    def _fake(self, kind: int, detail: int, root: int = 0, x: int = 0, y: int = 0) -> None:
        # xXTestFakeInputReq: the header, then one xEvent (32 bytes).
        self._request(struct.pack("<BBHBBHIIIIhhIHBB", self._xtest, _XTEST_FAKE_INPUT, 9,
                                  kind, detail, 0, 0, root, 0, 0, x, y, 0, 0, 0, 0))
        self.sent += 1

    def _bind_spare(self, keysym: int) -> int | None:
        if not self._spare:
            return None
        keycode = self._spare[self._spare_next % len(self._spare)]
        self._spare_next += 1
        old = self._spare_bound.pop(keycode, None)
        if old is not None and self._codes.get(old, (None,))[0] == keycode:
            del self._codes[old]
        per = max(self._per_keycode, 2)
        syms = [keysym, keysym] + [0] * (per - 2)
        self._request(struct.pack("<BBHBB2x", _X_CHANGE_KEYBOARD_MAPPING, 1, 2 + per, keycode, per)
                      + struct.pack(f"<{per}I", *syms))
        self._codes[keysym] = (keycode, 0)
        self._spare_bound[keycode] = keysym
        return keycode

    def _resolve(self, keysym: int) -> tuple[int, int] | None:
        found = self._codes.get(keysym)
        if found is None and 0x41 <= keysym <= 0x5A:
            # An upper-case letter on a key listing only its lower case:
            # X's implicit case conversion puts it on the shifted index.
            lower = self._codes.get(keysym + 0x20)
            if lower is not None and lower[1] == 0:
                found = (lower[0], 1)
        if found is None:
            keycode = self._bind_spare(keysym)
            found = (keycode, 0) if keycode is not None else None
        return found

    async def key(self, keysym: int, down: bool) -> None:
        if down:
            if keysym in self._keys_down:
                # Auto-repeat from the viewer: press again on the same key.
                self._fake(_KEY_PRESS, self._keys_down[keysym])
            else:
                found = self._resolve(keysym)
                if found is None:
                    self.unmappable += 1
                    return
                self._keys_down[keysym] = found[0]
                self._fake(_KEY_PRESS, found[0])
        else:
            keycode = self._keys_down.pop(keysym, None)
            if keycode is None:
                return  # never pressed here: a release left over from view mode
            self._fake(_KEY_RELEASE, keycode)
        await self._writer.drain()

    async def pointer_event(self, buttons: int, x: int, y: int) -> None:
        x = min(max(x, 0), self.width - 1)
        y = min(max(y, 0), self.height - 1)
        if self._position != (x, y):
            self._fake(_MOTION, 0, self._root, x, y)
            self._position = (x, y)
        for bit in _BUTTONS:
            button = bit + 1
            pressed = bool(buttons & (1 << bit))
            if pressed and button not in self._buttons_down:
                self._buttons_down.add(button)
                self._fake(_BUTTON_PRESS, button)
            elif not pressed and button in self._buttons_down:
                self._buttons_down.discard(button)
                self._fake(_BUTTON_RELEASE, button)
        await self._writer.drain()

    async def type_text(self, text: str) -> bool:
        """Type `text` as key presses. False, and nothing typed, if it cannot all be."""
        if len(text) > MAX_TYPED_CHARS:
            return False
        keysyms = [keysym_for(c) for c in text]
        if any(k is None for k in keysyms):
            return False
        shift = self._codes.get(XK_SHIFT_L)
        for keysym in keysyms:
            assert keysym is not None
            found = self._resolve(keysym)
            if found is None:
                self.unmappable += 1
                return False
            keycode, index = found
            shifted = index == 1 and shift is not None
            if shifted:
                self._fake(_KEY_PRESS, shift[0])  # type: ignore[index]
            self._fake(_KEY_PRESS, keycode)
            self._fake(_KEY_RELEASE, keycode)
            if shifted:
                self._fake(_KEY_RELEASE, shift[0])  # type: ignore[index]
        await self._writer.drain()
        return True

    async def release_all(self) -> None:
        for keycode in self._keys_down.values():
            self._fake(_KEY_RELEASE, keycode)
        self._keys_down.clear()
        for button in sorted(self._buttons_down):
            self._fake(_BUTTON_RELEASE, button)
        self._buttons_down.clear()
        await self._writer.drain()

    def resize(self, width: int, height: int) -> None:
        """The display changed size (a device preset, §6.1): clamp to the new one."""
        self.width, self.height = width, height


async def _read_reply(reader: asyncio.StreamReader, seq: int) -> bytes:
    """The reply to request `seq`, skipping events; an X error is `InjectError`."""
    while True:
        try:
            head = await reader.readexactly(32)
        except asyncio.IncompleteReadError:
            raise InjectError("display closed the connection") from None
        kind = head[0]
        if kind == 0:
            raise InjectError(f"display answered with X error code={head[1]}")
        if kind != 1:
            continue  # an event
        (got,) = struct.unpack_from("<H", head, 2)
        (extra,) = struct.unpack_from("<I", head, 4)
        body = await reader.readexactly(extra * 4) if extra else b""
        if got == (seq & 0xFFFF):
            return head + body


def display_socket(display: str | None = None) -> str:
    """Xvfb's socket for `display` (`:0` when unset): `/tmp/.X11-unix/X<n>`."""
    value = display if display is not None else os.environ.get("DISPLAY", ":0")
    number = value.rsplit(":", 1)[-1].split(".", 1)[0] or "0"
    if not number.isdigit():
        raise InjectError("DISPLAY is not a local display number")
    return f"/tmp/.X11-unix/X{number}"
