"""The endpoint behind Claude Code's /usage, read for an account's quota windows.

WHY THIS EXISTS
---------------
`Account.windows` and everything computed from it -- `headroom`,
`_binding_remaining`, `next_reset`, and `may_serve`'s assign-floor check -- read
fields that NOTHING WROTE. `AccountStore.record_reading` had no caller anywhere
in the repository, so every account reported full headroom forever and the
scheduler could hand work to an account with nothing left.

This is the missing producer.

WHAT THE ENDPOINT IS
--------------------
`GET https://api.anthropic.com/api/oauth/usage`, undocumented, authenticating
with whatever OAuth token is presented -- so an account can be polled WITHOUT
being made active. The shape was taken from a real captured 200 rather than
guessed:

    {"five_hour":  {"utilization": 34.0, "resets_at": "2026-...+00:00", ...},
     "seven_day":  {"utilization": 29.0, "resets_at": "2026-...+00:00", ...},
     "seven_day_opus": null, "nimbus_quill": {...}, ...}

Two properties of that shape drive the parsing below:

  * `utilization` is a PERCENTAGE, 0-100. `WindowReading.utilization` is
    0.0-1.0. Getting that conversion wrong would report a 34% used account as
    3400% used, or a full one as empty -- and the assign floor would then either
    starve the pool or overfill it.
  * windows are TOP-LEVEL KEYS and several are null, with names that change
    (`seven_day_opus`, `nimbus_quill`). `Account.windows` being a keyed dict
    rather than two fixed columns is what makes that survivable; this keeps that
    property by recording every window it can parse and ignoring the rest.

THE RATE LIMIT IS THE HARD CONSTRAINT
-------------------------------------
Roughly FIVE CALLS PER FIVE MINUTES, answering 429 with `retry-after: 299`.
There are no rate-limit headers on a 200, so the budget cannot be measured, only
exhausted. A sweep that polled every account every tick would spend it and then
learn nothing for five minutes -- and the platform shares the limit with any
human running `claude /usage` or a rotation daemon against the same accounts.

So the caller polls a BOUNDED number of accounts per sweep, oldest reading
first, and a 429 stops the round rather than being retried.

This is an undocumented endpoint behind one adapter, so a shape change fails
loudly in exactly one place.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .accounts import WindowReading

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
#: The endpoint answers 403 to an unrecognised client, so it is presented as the
#: CLI it belongs to rather than as an anonymous script.
USER_AGENT = "claude-cli/2.1.266 (external, cli)"

#: Conservative by construction: the observed budget is ~5 calls / 5 minutes and
#: it is SHARED with anything else polling the same accounts. The sweep runs
#: every five minutes, so three leaves headroom for a human running /usage.
DEFAULT_MAX_POLLS_PER_SWEEP = 3


class UsageUnavailable(RuntimeError):
    """The reading could not be taken. NEVER conflated with 'nothing is used'."""


class RateLimited(UsageUnavailable):
    """The budget is spent. The caller must stop, not retry."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"usage endpoint rate limited; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


def _parse_window(raw: Any) -> WindowReading | None:
    """One window, or None if this key carries no usable reading.

    Returns None rather than a zero reading for a null or malformed window. A
    window reported as 0.0 utilisation means "untouched"; a window that could not
    be read means nothing at all, and recording the first for the second is how
    an exhausted account looks available.
    """
    if not isinstance(raw, dict):
        return None
    pct = raw.get("utilization")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    resets = raw.get("resets_at")
    if not isinstance(resets, str):
        return None
    try:
        # Python 3.11 handles the +00:00 offset these carry.
        resets_at = datetime.fromisoformat(resets)
    except ValueError:
        return None
    if resets_at.tzinfo is None:
        resets_at = resets_at.replace(tzinfo=timezone.utc)
    # 0-100 -> 0.0-1.0, clamped: the endpoint has been observed to report values
    # above 100 on an overage, and a utilisation above 1.0 would make
    # `remaining()` negative and the assign floor behave unpredictably.
    return WindowReading(utilization=max(0.0, min(1.0, float(pct) / 100.0)), resets_at=resets_at)


def parse(payload: dict[str, Any]) -> dict[str, WindowReading]:
    """Every window in the payload that carries a usable reading.

    Unknown keys are kept, not filtered against a list of expected names: the
    endpoint adds windows under names nobody has seen yet, and a reading is
    still a reading. `Account.windows` is keyed for exactly this reason.
    """
    windows: dict[str, WindowReading] = {}
    for key, value in payload.items():
        reading = _parse_window(value)
        if reading is not None:
            windows[key] = reading
    return windows


def fetch(access_token: str, *, timeout_seconds: float = 10.0) -> dict[str, WindowReading]:
    """Read one account's windows. Raises rather than returning an empty dict.

    An empty dict from this function would mean "this account has no windows",
    which is a claim about the account. Every failure to READ raises instead, so
    a caller cannot record a fetch failure as a fresh, healthy reading.
    """
    if not access_token:
        raise UsageUnavailable("no access token for this account")

    request = urllib.request.Request(
        ENDPOINT,
        headers={
            "authorization": f"Bearer {access_token}",
            "anthropic-beta": BETA_HEADER,
            "user-agent": USER_AGENT,
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raw = exc.headers.get("retry-after", "") if exc.headers else ""
            try:
                retry_after = int(raw)
            except (TypeError, ValueError):
                retry_after = 300
            raise RateLimited(retry_after) from exc
        raise UsageUnavailable(f"usage endpoint returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UsageUnavailable(f"usage endpoint unreachable: {type(exc).__name__}") from exc

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise UsageUnavailable("usage endpoint returned a body that is not JSON") from exc
    if not isinstance(payload, dict):
        raise UsageUnavailable("usage endpoint returned JSON that is not an object")

    windows = parse(payload)
    if not windows:
        # The call succeeded and nothing in it parsed. That is a SHAPE CHANGE,
        # not an account with no quota, and recording it as a reading would
        # silently zero every account on the platform.
        raise UsageUnavailable(
            "usage endpoint returned no parseable window; the response shape may have changed"
        )
    return windows
