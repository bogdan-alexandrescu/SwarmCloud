"""The hosted voice's turn latency, swarm-voice's half
(docs/design/voice-it-own-speech.md §12.6; the store is swarm-api's
voice/speech_telemetry.py).

WHAT. One record per turn: from the person's end of speech to the first audio
of the voice's reply, `span_ms`, taken from two events the sideband already
receives (live.py reads both for the pause): the person's voice detection
ending (`input_audio_buffer.speech_stopped`, `session.input_audio.speech_stopped`:
any type ending `.speech_stopped` that is not the output's) and the voice's
playback starting (`session.output_audio.started`).

ONE CLOCK. Both are stamped on arrival at this process's monotonic clock, and
both arrive on the same sideband socket, so the span is taken on one side
only and no two clocks are compared. It is the span as the sideband sees it:
the socket's own transit is common to both events and cancels; the WebRTC leg
to the browser is not on this socket and is not in the span.

NOT MEASURED, NEVER GUESSED. When a reply comes (the audio starting, or the
voice's transcript final) after words of the person's but one of the two
events never came, the turn is recorded with `span_ms` None. Each is
recognised only when the session has never seen that event at all: a missing
`session.output_audio.started` closes the turn at the voice's final; a missing
`*.speech_stopped` closes it at the reply. A reply with nothing of the
person's before it (a planner update spoken while the person is quiet) is not
a turn. The person speaking again before the reply moves the end of speech.

NOTHING BUT NUMBERS. A record is `engine`, `turn_id`, `session_id` and
`span_ms`: never a word, never audio.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable

ENGINE = "gpt-live"

#: The events the span is taken between (live.py names them for the pause).
OUTPUT_AUDIO_STARTED = "session.output_audio.started"
SPEECH_STARTED_SUFFIX = ".speech_started"
SPEECH_STOPPED_SUFFIX = ".speech_stopped"
#: Transcript families, as live.py recognises them (`final_speaker`, `delta_speaker`).
FINAL_SUFFIXES = ("transcript.done", "transcription.completed", "transcript.completed")
DELTA_SUFFIXES = ("transcript.delta", "transcription.delta")


def _side(kind: str) -> str | None:
    """`person`, `voice` or None, by the event type's family."""
    if kind.startswith(("session.input", "input_audio", "conversation.item.input")):
        return "person"
    if kind.startswith(("session.output", "response.")):
        return "voice"
    return None


def _person(kind: str, suffix: str) -> bool:
    return kind.endswith(suffix) and not kind.startswith("session.output")


class TurnLatency:
    """One hosted session's turns; `note` each sideband event, in order."""

    def __init__(self, session_id: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.session_id = session_id
        self._clock = clock
        #: The person's last end of speech since the last reply, on `clock`.
        self._ended_at: float | None = None
        #: Words of the person's (or their voice detection) since the last reply.
        self._heard = False
        self._stops_seen = False
        self._audio_seen = False

    def _record(self, span_s: float | None) -> dict[str, Any]:
        self._ended_at = None
        self._heard = False
        return {"engine": ENGINE, "turn_id": "turn_" + secrets.token_hex(6),
                "session_id": self.session_id,
                "span_ms": None if span_s is None else round(max(0.0, span_s) * 1000.0, 1)}

    def note(self, kind: str, event: dict) -> dict[str, Any] | None:
        """The finished turn's record when `event` ends one, else None."""
        if _person(kind, SPEECH_STOPPED_SUFFIX):
            self._stops_seen = True
            self._heard = True
            self._ended_at = self._clock()
            return None
        if _person(kind, SPEECH_STARTED_SUFFIX):
            self._heard = True
            self._ended_at = None
            return None
        if kind == OUTPUT_AUDIO_STARTED:
            self._audio_seen = True
            if self._ended_at is not None:
                return self._record(self._clock() - self._ended_at)
            if self._heard and not self._stops_seen:
                return self._record(None)
            return None
        side = _side(kind)
        if side == "person" and kind.endswith(FINAL_SUFFIXES + DELTA_SUFFIXES):
            self._heard = True
            return None
        if side == "voice" and kind.endswith(FINAL_SUFFIXES) and not self._audio_seen \
                and (self._ended_at is not None or (self._heard and not self._stops_seen)):
            return self._record(None)
        return None


__all__ = ["ENGINE", "TurnLatency"]
