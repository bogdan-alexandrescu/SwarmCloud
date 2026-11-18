"""The act ledger: what each act tool reserves before it calls swarm-api, and
what came of it (docs/design/voice-it.md §3.9: "act tools, each after the
ledger reserves it").

One ledger per voice session, held by the planner (planner.py) for the
session's life and dropped with it (`Planner.forget`). Turns run one at a time
per session (§3.8), so nothing here needs a lock beyond that turn lock.

AN ACT IS READ BACK, THEN CONFIRMED, THEN RESERVED, THEN RECORDED.

  1. `read_back` holds what the planner is about to do, as the person will
     hear it, in the turn it was asked for. Nothing is sent.
  2. In a LATER turn, and only when the person has said something since (the
     person's lines in the turn's transcript are not the ones the read-back
     saw), `reserve` takes exactly that act. A different act, an act from
     this same turn, one with nothing said since, or one older than
     `READBACK_SECONDS` is refused: the planner reads it back again. So the planner cannot read back and act in
     one breath, and cannot act on something other than what it read back.
  3. `record` closes the reservation with its outcome: `done`, or
     `failed` (which frees the act to be tried again, read back afresh).

ONE ANSWER PER QUESTION PER SESSION. A question this session already answered
is refused at `read_back` and again at `reserve`, unless the person said to
change it: the act tool passes `change` and the person's newest words, and
only a newest line that says "change" (change, changed, changing) counts
(`says_change`). The planner's own word is never enough. The route keeps one
answer per question anyway (a re-answer replaces it and keeps the replaced one
in its history); this rule is the session's, so a misheard repeat or a
planner loop does not silently replace what the person answered.

A RESTART LOSES THE LEDGER. swarm-voice holds no store of its own; after a
restart the next session starts with an empty ledger and the route's own
answer history is the record. Nothing here holds a credential: keys are task
ids and question indexes, and an answer's text is what the person said, after
the planner's scrub.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: How long a read-back stays good for its confirmation. Long enough for the
#: voice to say it and the person to answer; short enough that a "yes" the
#: next morning confirms nothing.
READBACK_SECONDS = 300.0

ANSWER = "answer"

READ_BACK = "read_back"
RESERVED = "reserved"
DONE = "done"
FAILED = "failed"

_CHANGE = re.compile(r"\bchang(?:e|ed|es|ing)\b", re.IGNORECASE)


class ActRefused(PermissionError):
    """The ledger refused an act. The message says why, for the planner."""


def _heard(transcript: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(text for who, text in transcript
                 if who == "person" and isinstance(text, str) and text.strip())


def says_change(transcript: Iterable[tuple[str, str]]) -> bool:
    """Whether the person's newest line in `transcript` says to change it."""
    newest = ""
    for who, text in transcript:
        if who == "person" and isinstance(text, str) and text.strip():
            newest = text
    return bool(_CHANGE.search(newest))


@dataclass
class Act:
    """One act this session read back, reserved or did."""

    kind: str
    #: What the act is on, e.g. ("task_1", "0") for an answer.
    key: tuple[str, ...]
    #: What it does there, compared exactly between read-back and reserve.
    what: tuple[Any, ...]
    state: str
    turn: int
    at: float
    #: The person said "change" when it was read back (`says_change`).
    change: bool = False
    #: The person's lines the read-back's turn saw (`_heard`).
    heard: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def view(self) -> dict[str, Any]:
        return {"act": self.kind, "key": list(self.key), "state": self.state, **self.detail}


class ActLedger:
    """One voice session's acts (module docstring)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 readback_seconds: float = READBACK_SECONDS) -> None:
        self._clock = clock
        self._readback_seconds = readback_seconds
        self._turn = 0
        #: The pending read-back per (kind, key); at most one each.
        self._pending: dict[tuple[str, tuple[str, ...]], Act] = {}
        #: Every reservation, in order, with its outcome.
        self._acts: list[Act] = []

    @property
    def turn(self) -> int:
        return self._turn

    def begin_turn(self) -> int:
        """A new planner turn: the person has spoken since the last one."""
        self._turn += 1
        return self._turn

    def history(self) -> list[dict[str, Any]]:
        return [a.view() for a in self._acts]

    def _done(self, kind: str, key: tuple[str, ...]) -> Act | None:
        for act in reversed(self._acts):
            if act.kind == kind and act.key == key and act.state in (DONE, RESERVED):
                return act
        return None

    def _check_once(self, kind: str, key: tuple[str, ...], *, change: bool) -> None:
        done = self._done(kind, key)
        if done is None:
            return
        if done.state == RESERVED:
            raise ActRefused("that act is already in flight in this session; nothing was sent")
        if not change:
            raise ActRefused("this session already answered that question; it is changed only "
                             "when the person says to change it")

    def read_back(self, kind: str, key: tuple[str, ...], what: tuple[Any, ...], *,
                  change: bool, transcript: Iterable[tuple[str, str]],
                  detail: dict[str, Any] | None = None) -> Act:
        """Hold `what` as read back this turn; replaces an earlier read-back of `key`."""
        lines = list(transcript)
        said_change = change and says_change(lines)
        self._check_once(kind, key, change=said_change)
        act = Act(kind=kind, key=key, what=what, state=READ_BACK, turn=self._turn,
                  at=self._clock(), change=said_change, heard=_heard(lines),
                  detail=dict(detail or {}))
        self._pending[(kind, key)] = act
        return act

    def confirmable(self, kind: str, key: tuple[str, ...], what: tuple[Any, ...],
                    transcript: Iterable[tuple[str, str]]) -> bool:
        """Whether `reserve` would take this act now (a read-back of exactly
        `what`, from an earlier turn, the person heard since, not expired)."""
        held = self._pending.get((kind, key))
        return (held is not None and held.what == what and held.turn < self._turn
                and _heard(transcript) != held.heard
                and self._clock() - held.at <= self._readback_seconds)

    def reserve(self, kind: str, key: tuple[str, ...], what: tuple[Any, ...],
                transcript: Iterable[tuple[str, str]]) -> Act:
        """Reserve the act read back in an earlier turn; `ActRefused` otherwise."""
        held = self._pending.get((kind, key))
        if held is None or held.what != what:
            raise ActRefused("read it back to the person first; nothing was sent")
        if held.turn >= self._turn or _heard(transcript) == held.heard:
            raise ActRefused("wait for the person to confirm the read-back; nothing was sent")
        if self._clock() - held.at > self._readback_seconds:
            del self._pending[(kind, key)]
            raise ActRefused("the read-back expired; read it back again; nothing was sent")
        self._check_once(kind, key, change=held.change)
        del self._pending[(kind, key)]
        act = Act(kind=kind, key=key, what=what, state=RESERVED, turn=self._turn,
                  at=self._clock(), change=held.change, detail=dict(held.detail))
        self._acts.append(act)
        return act

    def record(self, act: Act, *, ok: bool, detail: dict[str, Any] | None = None) -> Act:
        """Close a reservation with its outcome."""
        if act.state != RESERVED:
            raise ActRefused("only a reserved act is recorded")
        act.state = DONE if ok else FAILED
        act.detail.update(detail or {})
        return act
