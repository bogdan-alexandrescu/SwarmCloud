"""Telling the platform's time-limit line (#2139) apart from the rest of a prompt.

Every claude-code and codex prompt carries one line built by
`cliagent.time_limit_line`, naming the step's limit and its end time. A test
that holds a whole prompt holds that line through the function, never by
restating its text; and since the end time moves with the clock, the helper
reads the minutes and the end time back out of the line and checks it is
exactly what `time_limit_line` builds for them.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from agent_worker.runners.cliagent import time_limit_line

_END = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_MINUTES = re.compile(r"\b(\d+) minutes?\b")


def is_limit_line(text: str) -> bool:
    """True when `text` is one time_limit_line output, whole."""
    ends, minutes = _END.findall(text), _MINUTES.findall(text)
    if len(ends) != 1 or len(minutes) != 1:
        return False
    end = datetime.strptime(ends[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    seconds = int(minutes[0]) * 60
    return text == time_limit_line(seconds, now=end - timedelta(seconds=seconds))


def without_limit_line(prompt: str) -> str:
    """`prompt` less its last paragraph, which must be the limit line."""
    body, _, last = prompt.rstrip().rpartition("\n\n")
    assert is_limit_line(last), prompt[-800:]
    return body
