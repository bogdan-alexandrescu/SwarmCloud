"""`questions.json`: what a remote agent asks the owner instead of guessing.

WHY (owner decision, 2026-10-05). A remote agent that met a decision that was
the owner's to make had one place to put it: its answer. It wrote the question
into prose and shipped `part of #N` (I310, W532), and a workflow row that cuts
that answer to 500 characters could drop the question entirely. So an agent may
write `questions.json` into `$SWARM_ARTIFACTS_DIR`:

    [
      {
        "question": "Per task or per workflow?",
        "options": [{"label": "per task", "description": "..."}, ...],
        "recommended": "per task",
        "context": "why this is the owner's call, and what was found"
      }
    ]

The file is uploaded like any other artifact. This module only decides whether
it is the shape above, and the worker records the outcome in `result_summary`:
`questions: N` when it is, `questions: 0` with `questions_rejected: <reason>`
when it is not, and `questions: 0` alone when there is no file. A rejected
file is still uploaded as an ordinary artifact -- the operator can read what
the agent wrote -- but it is not counted, so no reader surfaces it as
questions. The task's own outcome never depends on it: an invalid file is a
line in the summary, not a failed attempt.

DATA, NEVER A CHANNEL. Nothing in the platform reads these questions to decide
anything: no state, retry, dispatch or merge looks at them. They are text for
the operator, served back through the artifacts route (which redacts it) by
`swarm_result` and the follow outcome. An agent cannot use the file to ask the
platform for anything.

THE SHAPE IS STRICT so the bridge can show it without guessing: a list of at
most `MAX_QUESTIONS` objects, each with exactly the four keys above (the last
two may be null or absent), `options` a list of 1 to `MAX_OPTIONS` objects
with exactly `label` and `description`, labels unique, and `recommended` one
of the labels or null. Every text is bounded, and the whole file is at most
`MAX_BYTES` -- the bridge reads it in one window. A reason never quotes the
file: it names a position and a key, so a secret in the file cannot reach the
summary through the reason.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Collection

#: The file's name, at the top of `$SWARM_ARTIFACTS_DIR`.
NAME = "questions.json"

#: `result_summary` keys: how many questions were taken, and why a file was not.
SUMMARY_KEY = "questions"
REJECTED_KEY = "questions_rejected"

#: The largest file taken. 64 KiB is many pages of questions, and one window of
#: the artifacts route (which serves up to 4 MiB) reads it whole.
MAX_BYTES = 64 * 1024

#: At most this many questions in one file. An agent with more than twenty
#: decisions for the owner has a plan to propose, not questions to ask.
MAX_QUESTIONS = 20

#: At most this many options to one question.
MAX_OPTIONS = 10

#: The longest each text may be, in characters.
MAX_QUESTION_CHARS = 2_000
MAX_LABEL_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_CONTEXT_CHARS = 8_000

QUESTION_KEYS = frozenset({"question", "options", "recommended", "context"})
OPTION_KEYS = frozenset({"label", "description"})


class Rejected(ValueError):
    """The file is not a list of questions; `str(exc)` says why, quoting none of it."""


def _text(value: Any, where: str, limit: int, *, required: bool) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str):
        raise Rejected(f"{where} is not a string")
    if required and not value.strip():
        raise Rejected(f"{where} is empty")
    if len(value) > limit:
        raise Rejected(f"{where} is longer than {limit} characters")


def _keys(entry: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    if not set(entry) <= allowed:
        # The key itself is not quoted: it is the agent's text.
        raise Rejected(f"{where}: a key other than {', '.join(sorted(allowed))}")


def _question(entry: Any, where: str) -> None:
    if not isinstance(entry, dict):
        raise Rejected(f"{where} is not an object")
    _keys(entry, QUESTION_KEYS, where)
    _text(entry.get("question"), f"{where}: `question`", MAX_QUESTION_CHARS, required=True)
    options = entry.get("options")
    if not isinstance(options, list):
        raise Rejected(f"{where}: `options` is not a list")
    if not 1 <= len(options) <= MAX_OPTIONS:
        raise Rejected(f"{where}: `options` holds {len(options)}, not 1 to {MAX_OPTIONS}")
    labels: list[str] = []
    for number, option in enumerate(options, start=1):
        at = f"{where}: option {number}"
        if not isinstance(option, dict):
            raise Rejected(f"{at} is not an object")
        _keys(option, OPTION_KEYS, at)
        _text(option.get("label"), f"{at}: `label`", MAX_LABEL_CHARS, required=True)
        _text(option.get("description"), f"{at}: `description`", MAX_DESCRIPTION_CHARS,
              required=False)
        labels.append(option["label"])
    if len(set(labels)) != len(labels):
        raise Rejected(f"{where}: two options share a label")
    recommended = entry.get("recommended")
    if recommended is not None and recommended not in labels:
        raise Rejected(f"{where}: `recommended` is not one of its options' labels")
    _text(entry.get("context"), f"{where}: `context`", MAX_CONTEXT_CHARS, required=False)


def validate(data: bytes) -> list[dict[str, Any]]:
    """The questions in `data`, or `Rejected` saying why it is not a list of them."""
    if len(data) > MAX_BYTES:
        raise Rejected(f"the file is {len(data)} bytes, larger than {MAX_BYTES}")
    try:
        value = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise Rejected("the file is not UTF-8") from None
    except ValueError:
        raise Rejected("the file is not JSON") from None
    except RecursionError:
        # 64 KiB of `[` is nested deeper than the decoder recurses.
        raise Rejected("the file is not JSON: nested too deeply") from None
    if not isinstance(value, list):
        raise Rejected("the file is not a JSON list of questions")
    if len(value) > MAX_QUESTIONS:
        raise Rejected(f"the file holds {len(value)} questions, more than {MAX_QUESTIONS}")
    for number, entry in enumerate(value, start=1):
        _question(entry, f"question {number}")
    return value


def read(directory: Path | str) -> bytes | None:
    """The file's bytes, None when there is none; never through a link.

    Opened with O_NOFOLLOW and checked to be a regular file on the descriptor
    it read, so a link or a FIFO planted under the name is refused rather than
    followed or waited on. At most `MAX_BYTES + 1` bytes are read, which is
    enough for `validate` to say it is too large.
    """
    path = os.path.join(os.fspath(directory), NAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        # ELOOP is the link O_NOFOLLOW refused; anything else is unreadable.
        if os.path.lexists(path):
            raise Rejected("the file is not a regular file") from None
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Rejected("the file is not a regular file")
        chunks: list[bytes] = []
        left = MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def summarise(
    directory: Path | str, *, uploaded: Collection[str], cause: str | None = None
) -> dict[str, Any]:
    """The `result_summary` keys for this attempt's `questions.json`.

    `uploaded` is the manifest's names: a file the upload did not take is not
    counted, because a reader would then be told of questions it cannot read.
    `cause` is why it was not taken, as a sentence
    (`expected_outputs.cause_text`), when the upload said.
    """
    try:
        data = read(directory)
        if data is None:
            return {SUMMARY_KEY: 0}
        if NAME not in uploaded:
            return {SUMMARY_KEY: 0, REJECTED_KEY: cause or "written but not uploaded"}
        return {SUMMARY_KEY: len(validate(data))}
    except Rejected as exc:
        return {SUMMARY_KEY: 0, REJECTED_KEY: str(exc)}
