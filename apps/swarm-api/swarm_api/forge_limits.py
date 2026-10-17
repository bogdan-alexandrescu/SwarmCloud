"""GitHub's rate limit, read once for every module that asks GitHub (#1483).

A RATE-LIMITED ANSWER IS NOT AN ANSWER. GitHub answers a spent primary or
secondary rate limit with 403 -- the status it also uses for "you may not"
-- and sometimes 429, with `X-RateLimit-Remaining: 0`, a `Retry-After`, or
a message that says "rate limit". Read as a refusal, it told the owner on
2026-10-17 to "ask for write access" to a repository they administer, while
GitHub was answering 403 with `X-RateLimit-Remaining: 0` for their user.
So every check that decides MISSING or PERMISSION_MISSING from a 403 asks
`rate_limit` first, and a limited answer is FORGE_UNREACHABLE and `unknown`.

`gittokens` (the token probe), `access` (Work › Access) and `onboarding`
(the checklist, which reads the probe's words) import this one reading; the
classification is not restated anywhere else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

#: GitHub's words for both limits: "API rate limit exceeded for user ID
#: ...", "You have exceeded a secondary rate limit ...".
_MESSAGE = re.compile(r"rate limit", re.IGNORECASE)

#: How much of a refusal's body is read for its message: GitHub's error
#: bodies are a few hundred bytes.
_MESSAGE_BYTES = 65536

#: The words a probe records for a rate-limited read (`gittokens._Forge.get`),
#: and that `onboarding` recognises in a stored probe error. The reset, when
#: GitHub gave one, follows as "until HH:MM UTC".
SPENT = "with the rate limit spent"
_SPENT = re.compile(re.escape(SPENT) + r"(?: until (\d{2}:\d{2} UTC))?")


@dataclass(frozen=True)
class RateLimit:
    """A rate-limited answer: its status and, when GitHub said, when the
    limit resets (UTC)."""

    status: int
    reset: datetime | None


def _headers(answer: Any) -> dict[str, str]:
    raw = getattr(answer, "headers", None) or {}
    try:
        items = dict(raw).items()
    except (TypeError, ValueError):
        return {}
    return {str(k).lower(): str(v).strip() for k, v in items}


def _message(answer: Any) -> str:
    body = getattr(answer, "body", b"") or b""
    try:
        data = json.loads(bytes(body)[:_MESSAGE_BYTES].decode("utf-8"))
    except (TypeError, ValueError):
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return message if isinstance(message, str) else ""


def _reset(headers: dict[str, str], now: datetime) -> datetime | None:
    """`X-RateLimit-Reset` (epoch seconds) when GitHub sent it, else
    `Retry-After` (seconds, or an HTTP date)."""
    epoch = headers.get("x-ratelimit-reset", "")
    if epoch.isdigit():
        try:
            return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    after = headers.get("retry-after", "")
    if after.isdigit():
        try:
            return now + timedelta(seconds=int(after))
        except OverflowError:
            return None
    if after:
        try:
            at = parsedate_to_datetime(after)
        except (TypeError, ValueError, IndexError):
            return None
        return at.astimezone(timezone.utc) if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return None


def rate_limit(answer: Any, *, now: datetime | None = None) -> RateLimit | None:
    """The limit `answer` reports, or None when it is not rate-limited.

    `answer` is anything with `status`, `headers` and `body` -- an
    `forgeapp.HttpAnswer`, or a probe transport's response. Rate-limited is:
    a 429; a 403 with `X-RateLimit-Remaining: 0`; a 403 or 429 with
    `Retry-After`; a 403 whose message says "rate limit" (primary or
    secondary). Any other 403 is GitHub's real refusal and stays one."""
    try:
        status = int(answer.status)
    except (AttributeError, TypeError, ValueError):
        return None
    if status not in (403, 429):
        return None
    headers = _headers(answer)
    limited = (status == 429 or headers.get("x-ratelimit-remaining") == "0"
               or bool(headers.get("retry-after"))
               or bool(_MESSAGE.search(_message(answer))))
    if not limited:
        return None
    return RateLimit(status, _reset(headers, now or datetime.now(timezone.utc)))


def is_rate_limited(answer: Any) -> bool:
    """True when `answer` is GitHub limiting requests, not refusing them."""
    return rate_limit(answer) is not None


def until(limit: RateLimit | None) -> str | None:
    """The reset as people read it, "03:13 UTC", or None when GitHub gave none."""
    if limit is None or limit.reset is None:
        return None
    return limit.reset.astimezone(timezone.utc).strftime("%H:%M UTC")


def spent(limit: RateLimit | None) -> str:
    """A probe's words for a limited read, with the reset when known."""
    when = until(limit)
    return SPENT + (f" until {when}" if when else "")


def limited_copy(when: str | None) -> str:
    """The sentence a person reads for a rate-limited check. It names the
    reset and says nothing about permissions: none was checked."""
    if when:
        return (f"GitHub is limiting requests for this account until {when}; nothing was "
                "checked. Try again after that.")
    return ("GitHub is limiting requests for this account; nothing was checked. Try again "
            "in a few minutes.")


def copy_for(limit: RateLimit | None) -> str:
    return limited_copy(until(limit))


def copy_in(*texts: Any) -> str | None:
    """`limited_copy` for the first probe text that records a rate-limited
    read (`spent`), or None when none does."""
    for text in texts:
        if isinstance(text, str):
            found = _SPENT.search(text)
            if found:
                return limited_copy(found.group(1))
    return None
