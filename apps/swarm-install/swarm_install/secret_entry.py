"""Human-held values, typed into a hidden terminal prompt (design §2.5).

A value a person holds (the Desktop client's secret, plan §6.3) reaches
SwarmCloud one way only: typed at a hidden prompt on the controlling terminal
and handed straight to Secret Manager's `addVersion`. It is never printed,
never put in an event, never written to the install state (which records only
the version number) and never written to disk.

**No terminal, no prompt (review finding 11).** `getpass.getpass` falls back to
reading stdin, with echo on, when `/dev/tty` cannot be opened; under
`/sc:install` that stdin is a pipe Claude writes to, so a value could be piped
through a Claude session. This module never calls `getpass` and never reads
stdin: it opens `/dev/tty` itself, and when that fails the step pauses (exit 4)
with a `step_paused` event whose `reason` is `needs_terminal` and whose
`command` is the line the person types in their own terminal,
`! swarm install --resume`.

The same terminal asks the plain (non-secret) questions of stage 0 (`ask`), so
those are never answered by a pipe either.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, NoReturn, Protocol

#: The `reason` of the `step_paused` event a step emits when it has no terminal.
NEEDS_TERMINAL = "needs_terminal"

#: What the person types, in their own terminal, to go on (`!` runs it outside
#: the Claude session in Claude Code).
RESUME_LINE = "! swarm install --resume"


class NoTerminal(OSError):
    """There is no controlling terminal to prompt on."""


class Terminal(Protocol):
    def write(self, text: str) -> None: ...

    def read_line(self) -> str: ...

    def read_hidden(self) -> bytearray: ...

    def close(self) -> None: ...


class DevTty:
    """The controlling terminal, opened directly; never stdin, never stdout."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def open(cls) -> DevTty:
        try:
            fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            raise NoTerminal(f"no controlling terminal: {exc.strerror or exc}") from None
        if not os.isatty(fd):
            os.close(fd)
            raise NoTerminal("/dev/tty is not a terminal")
        return cls(fd)

    def write(self, text: str) -> None:
        os.write(self._fd, text.encode())

    def _read_bytes(self) -> bytearray:
        line = bytearray()
        while True:
            chunk = os.read(self._fd, 1)
            if not chunk or chunk in (b"\n", b"\r"):
                return line
            line += chunk

    def read_line(self) -> str:
        return self._read_bytes().decode(errors="replace").strip()

    def read_hidden(self) -> bytearray:
        import termios  # POSIX only; imported where it is used

        old = termios.tcgetattr(self._fd)
        new = termios.tcgetattr(self._fd)
        new[3] &= ~termios.ECHO
        try:
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, new)
            return self._read_bytes()
        finally:
            termios.tcsetattr(self._fd, termios.TCSAFLUSH, old)
            os.write(self._fd, b"\n")

    def close(self) -> None:
        os.close(self._fd)


class SecretVersions(Protocol):
    """The one Secret Manager call a typed value goes to."""

    def add_secret_version(self, project: str, secret_id: str, data: bytes) -> int: ...


def needs_terminal(ctx: Any, step_id: str, what: str) -> NoReturn:
    """Pause the run (exit 4): `what` must be typed in the person's own terminal."""
    from .engine import _Stop  # the engine's own way to end a run with an outcome

    message = (
        f"{step_id} needs {what} typed at a hidden prompt in a terminal, never through a "
        f"chat or a pipe. In your own terminal run: {RESUME_LINE}"
    )
    ctx.events.emit(
        "step_paused", step=step_id, reason=NEEDS_TERMINAL, command=RESUME_LINE, message=message
    )
    raise _Stop("paused", step_id, message)


def ask(
    ctx: Any,
    step_id: str,
    question: str,
    *,
    default: str | None = None,
    open_terminal: Callable[[], Terminal] = DevTty.open,
) -> str:
    """A plain (non-secret) question on the terminal; pauses when there is none."""
    try:
        terminal = open_terminal()
    except NoTerminal:
        needs_terminal(ctx, step_id, f"an answer to {question!r}")
    try:
        terminal.write(f"{question}" + (f" [{default}]" if default else "") + ": ")
        answer = terminal.read_line()
    finally:
        terminal.close()
    return answer or (default or "")


def enter_secret(
    ctx: Any,
    *,
    step_id: str,
    project: str,
    secret_id: str,
    prompt: str,
    store: SecretVersions,
    open_terminal: Callable[[], Terminal] = DevTty.open,
) -> int:
    """Prompt for a value with echo off and add it as a version of `secret_id`.

    Returns the version number, the only thing about the value anyone keeps.
    """
    try:
        terminal = open_terminal()
    except NoTerminal:
        needs_terminal(ctx, step_id, f"the value of {secret_id}")
    value = bytearray()
    try:
        terminal.write(f"{prompt} (input hidden): ")
        value = terminal.read_hidden()
        if not value.strip():
            raise ValueError(f"nothing was typed for {secret_id}; nothing was stored")
        return store.add_secret_version(project, secret_id, bytes(value.strip()))
    finally:
        for index in range(len(value)):
            value[index] = 0
        terminal.close()
