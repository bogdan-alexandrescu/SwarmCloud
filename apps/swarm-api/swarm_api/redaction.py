"""Read-time credential redaction for anything this API serves back verbatim.

WHY THIS EXISTS AT ALL, WHEN THE WORKER ALREADY SCRUBS
------------------------------------------------------
It does, and its pass is real -- but it is a pass over REGISTERED LITERAL
VALUES, and that is a much smaller promise than it reads like:

* `agent_worker.logs.StructuredLogger.register_secret` is called from exactly
  four places (`secrets.py` twice, `lifecycle.py` once, `runners/cliagent.py`
  once) and every one of them registers the value of a credential this platform
  itself resolved out of Secret Manager. Nothing else is ever registered.
* `agent_worker.redact.scrub_text` then replaces those exact strings. It matches
  no patterns. A GitHub token the agent minted mid-run, an `Authorization:`
  header a `curl -v` echoed, an `.env` file printed out of a repository the
  agent cloned -- none of those are registered values, so none of them are
  touched.
* Both `_redact_before_upload` and `_publish_live_logs` short-circuit on
  `if not self.log.has_secrets`. A `mock`-profile run registers nothing, so for
  that run the scrubbing pass does not execute at all.

So the honest statement about a log object in the bucket is: a pass MAY have
run over it, and if it did it removed the tenant's provider key and nothing
else. That is not a basis on which to hand bytes to a browser.

Hence: redaction at READ time, unconditionally, on every byte this API serves
out of a log object, regardless of what happened at write time. If we cannot
prove a stream was scrubbed -- and we cannot -- we scrub it again.

RELATIONSHIP TO `redact()` IN scripts/lib/common.sh
---------------------------------------------------
That shell function is the house definition of "credential-shaped". The rules
below are the same families, in the same order, with the same `\\1********`
shape, so an operator reading a redacted log in the terminal and a user reading
one in the browser see the same thing.

They are deliberately NOT identical: `KEY_VALUE` here accepts a prefix on the
key name, so `ANTHROPIC_API_KEY=...` and `GH_TOKEN=...` are caught where the
shell rule -- which anchors the name to the bare word -- lets them through. The
relation this module promises is a SUPERSET: everything the shell filter
redacts, this redacts, and possibly more. `tests/unit/control_plane/
test_log_redaction.py` pins both halves, including a drift check that fails
when a rule is added to the shell filter and not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

#: Same marker the shell filter leaves behind, so redacted output is
#: recognisable wherever it is read.
MASK = "********"


@dataclass(frozen=True)
class Rule:
    """One credential family.

    Group 1 is the identifying prefix and is KEPT -- `sk-abc123********` still
    tells a reader which provider's key leaked, which is the first thing anyone
    needs when rotating it. Everything after group 1 is replaced.
    """

    name: str
    #: The distinctive literal this family is recognised by, in the spelling
    #: `scripts/lib/common.sh` uses. The drift test matches on these.
    shell_marker: str
    pattern: re.Pattern[str]


def _rule(name: str, shell_marker: str, expression: str, flags: int = 0) -> Rule:
    return Rule(name=name, shell_marker=shell_marker, pattern=re.compile(expression, flags))


#: Order matters and mirrors the shell filter's `-e` order: a narrow provider
#: rule runs before the broad key/value rule, so `api_key=sk-live-...` is
#: reduced by the `sk-` rule first and the survivor is masked by the second.
#:
#: `.` never matches a newline in any pattern here (no re.DOTALL), which is what
#: keeps the PRIVATE KEY rule line-scoped exactly as sed's line-at-a-time
#: processing makes it.
RULES: tuple[Rule, ...] = (
    _rule("openai_key", "sk-", r"(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+"),
    _rule("google_oauth_access_token", "ya29", r"(ya29\.)[A-Za-z0-9._-]+"),
    # Any JWT: a Google ID token, an IAP assertion, a session cookie. This is
    # the one most likely to appear in a log this platform serves, because it
    # is what every caller of this very API holds.
    _rule("jwt", "ey", r"(ey[A-Za-z0-9_-]{8})[A-Za-z0-9._-]+"),
    _rule("google_api_key", "AIza", r"(AIza)[A-Za-z0-9_-]{20,}"),
    _rule("github_token", "gh[pousr]_", r"(gh[pousr]_)[A-Za-z0-9]{8,}"),
    _rule("github_pat", "github_pat_", r"(github_pat_)[A-Za-z0-9_]{8,}"),
    _rule("slack_token", "xox[abprs]-", r"(xox[abprs]-)[A-Za-z0-9-]{8,}"),
    _rule("aws_access_key_id", "AKIA", r"((?:AKIA|ASIA)[A-Z0-9]{4})[A-Z0-9]+"),
    _rule(
        "private_key_block",
        "PRIVATE KEY",
        r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*",
    ),
    # `Authorization: Bearer <token>` in any casing, and the `Basic` form,
    # which is a base64 username:password and is no less a credential.
    _rule(
        "http_authorization",
        "[Bb]earer",
        r"((?:[Bb]earer|[Bb]asic)[ \t]+)[A-Za-z0-9._~+/-]{12,}=*",
    ),
    # THE ENVIRONMENT-DUMP RULE, and the one that is deliberately wider than
    # the shell's. `[A-Za-z0-9_.-]*` in front of the name is what turns
    # `api_key=` into `ANTHROPIC_API_KEY=`, `GH_TOKEN=`, `db.password:` and
    # `x-api-key:`. An agent that prints its own environment is the single most
    # likely way a credential reaches a log object, and the shell rule as
    # written does not catch the shape `env` actually produces.
    _rule(
        "key_value_assignment",
        "api_?key",
        r"(\"?[A-Za-z0-9_.-]*"
        r"(?:api_?key|apikey|password|passwd|secret|token|credential|authorization)"
        r"\"?[ \t]*[:=][ \t]*\"?)[^\",\s]+",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class Redacted:
    """Text with every recognised credential masked, and how many were found.

    `count` is served to the caller. It is the difference between "this log is
    clean" and "this log had four credentials in it and you should rotate
    them", and a UI that cannot say the second is hiding an incident.
    """

    text: str
    count: int

    @property
    def any(self) -> bool:
        return self.count > 0


def redact(text: str, *, extra: Iterable[str] = ()) -> Redacted:
    """Mask every credential-shaped run in `text`.

    `extra` takes literal values the caller knows are secret -- it is the same
    idea as the worker's registered-secret set, available here for a caller
    that has one. It is applied FIRST and whole-string, because a literal is
    known to be a credential while a pattern only guesses.
    """
    count = 0
    for literal in sorted({v for v in extra if v and len(v) >= 8}, key=len, reverse=True):
        if literal in text:
            count += text.count(literal)
            text = text.replace(literal, MASK)
    for rule in RULES:
        text, found = rule.pattern.subn(r"\1" + MASK, text)
        count += found
    return Redacted(text=text, count=count)


def redact_detail(message: str, *, limit: int = 400) -> str:
    """Redact and BOUND a message that is about to be shown to a caller.

    Used on every error string this API puts in a response body. A GCS client
    exception can carry the full response body of the failed request, and that
    body has been observed elsewhere in this repository to contain a signed URL
    -- which is a credential with an expiry. The bound matters for the same
    reason: an unbounded upstream error becomes an unbounded response.
    """
    cleaned = redact(str(message)).text.strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned
