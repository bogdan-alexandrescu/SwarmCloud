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

THE KEY/VALUE RULE IS ONE RULE IN BOTH PLACES FOR JSON TEXT (#221, owner
decision 2026-09-26). A quote inside a JSON string is written `\\"`, so a
stream-json log line holds an agent's `export DB_PASSWORD="<v>"` as
`DB_PASSWORD=\\"<v>\\"`, and `"api_key": "<v>"` inside a tool's input as
`\\"api_key\\": \\"<v>\\"`. Both rules now take an escaped quote around the
key and before the value, and stop a value opened by one at the next
backslash, which in JSON text always begins an escape. The shell filter does
it in two expressions, because sed has no conditional group; the test file
runs both filters over the same lines and holds their output EQUAL on every
key/value case, not merely both masked.

AT ANY DEPTH OF ESCAPING (the PR #229 review). A command that itself quotes
its quotes -- `bash -c "export DB_PASSWORD=\\"<v>\\""`, `curl -d
"{\\"api_key\\": ...}"` -- sits in a stream-json line one level deeper, as
`\\\\\\"`: three backslashes, then the quote. The first version took exactly
one backslash, so it masked the three and served `<v>` under a count of 1,
and both filters agreed, so the parity check was green over the leak. Both
now take a RUN of backslashes wherever they took one.

`/logs` DOES NOT RELY ON THIS RULE FOR A JSON LINE. A log line that parses as
a JSON document is masked by its structure (`redact_lines`, `JsonMasker`), so
its strings are masked decoded, a list or an object under a credential's name
is masked whole, and a value holding a backslash or a space is masked to its
end. The rule over the text is what the terminal has (sed cannot decode JSON),
and what `/logs` falls back to for a line that is not a document.

The PRIVATE KEY family is the other place it is wider: a key is masked as a
BLOCK, not as the rest of one line -- see `mask_private_keys`.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Callable, Iterable

#: Same marker the shell filter leaves behind, so redacted output is
#: recognisable wherever it is read.
MASK = "********"


@dataclass(frozen=True)
class Rule:
    """One credential family.

    Group 1 is the identifying prefix and is KEPT -- `sk-abc123********` still
    tells a reader which provider's key leaked, which is the first thing anyone
    needs when rotating it. Everything after group 1 is replaced.

    `apply`, when set, masks the family INSTEAD of one substitution of
    `pattern`: a family whose extent is not one match of one pattern (a key
    spans lines) is masked by a function of `(text, decoded, inside_key)`
    returning `(text, count)`, and `pattern` then only records the marker the
    family starts from.
    """

    name: str
    #: The distinctive literal this family is recognised by, in the spelling
    #: `scripts/lib/common.sh` uses. The drift test matches on these.
    shell_marker: str
    pattern: re.Pattern[str]
    apply: Callable[[str, bool, bool], tuple[str, int]] | None = None


def _rule(name: str, shell_marker: str, expression: str, flags: int = 0) -> Rule:
    return Rule(name=name, shell_marker=shell_marker, pattern=re.compile(expression, flags))


# --------------------------------------------------------------------------
# Private keys: a BLOCK, not a line
# --------------------------------------------------------------------------
#
# THE HOLE THIS CLOSES (#188 review). The shell filter's rule masks the BEGIN
# marker's line from the marker on, and sed is line-at-a-time, so nothing
# after that line. On a raw NDJSON line that is the whole key: JSON writes the
# key's newlines as the two characters `\n`, so the key IS one line. Once
# DECODED they are real newlines, and the same rule masked the BEGIN line and
# served the base64 body in clear: `/transcript` and `/answer`, which redact
# after `json.loads`, were weaker than `/logs` over the very same bytes. A
# plain-text log or artifact holding a key (`cat id_rsa`, a generated fixture)
# had the same hole on every route.
#
# So a key is masked as a BLOCK, in four shapes:
#
#   (a) BEGIN ... END within `PEM_BLOCK_MAX_CHARS`: from the BEGIN marker
#       through the END marker and the rest of its line -- which is exactly
#       the line rule's reach on a key written as one line;
#   (b) a BEGIN with no END near it -- a window, a tool, or a field that cut
#       the key short. In a string DECODED from JSON: to the end of the
#       string, which is what the line rule masked of the raw line holding it.
#       In text: the rest of the BEGIN line, then every following line shaped
#       like a key's body (base64, the RFC 1421 headers before it, blank lines
#       between), stopping at the first line that is not;
#   (c) an END with no BEGIN -- text that starts inside a key: the key
#       material before the marker on its line, and the base64 lines above;
#   (d) text a paged reader KNOWS begins inside a key (`inside_key`: its own
#       look-back found a BEGIN before the window and no END): the body lines
#       at its head, and the END line if it follows them. Without this, the
#       pages in the middle of a key longer than the page hold neither marker.
#
# The BEGIN marker is kept, like every rule's identifying prefix, so a reader
# still sees WHAT leaked. Lines numbered by the tool that printed them (`cat
# -n`, an agent's file-read tool: `   12<TAB>MIIE...`) count as body lines.
#
# BY HAND, NOT AS ONE REGULAR EXPRESSION. A lazy BEGIN-to-END pattern retried
# at every BEGIN is quadratic on text of many BEGIN markers and no END, and a
# "body lines, then END" pattern retried at every line start is quadratic on
# a window of base64 -- and both are shapes an agent's output can take. Every
# scan below is linear in the text.

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_PEM_BEGIN_BYTES = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PEM_END_BYTES = re.compile(rb"-----END [A-Z ]*PRIVATE KEY-----")
#: A cheap test before any of the work: both markers end in it.
_PEM_HINT = "PRIVATE KEY-----"

#: How far after a BEGIN marker its END is looked for, and how far back a
#: paged reader looks for a BEGIN. The largest private key anyone generates
#: (RSA 16384) is under 13 KB of PEM, a little more once JSON escapes its
#: newlines; an END further away than this belongs to something else, and
#: masking everything between would hide ordinary output.
PEM_BLOCK_MAX_CHARS = 64 * 1024

#: The longest a marker is: `-----BEGIN ENCRYPTED PRIVATE KEY-----` is 37.
_MARKER_MAX = 64

#: A line number a tool printed in front of a line: digits, a tab or an arrow.
_NUMBERED = r"(?:\d{1,9}(?:\t|\u2192)[ \t]*)?"
#: One line of a key's body. `*` is there because a literal secret masked
#: first (`redact(extra=...)`) must not end the block early.
_PEM_BASE64_LINE = re.compile(_NUMBERED + r"[A-Za-z0-9+/=*]+")
#: `Proc-Type: 4,ENCRYPTED`, `DEK-Info: AES-128-CBC,...` -- only before the base64.
_PEM_HEADER_LINE = re.compile(_NUMBERED + r"[A-Za-z][A-Za-z0-9-]*:.*")
#: What may precede key material or an END marker on its own line.
_PEM_LINE_PREFIX = re.compile(r"[ \t\r]*" + _NUMBERED)
#: Key material on the END marker's own line, written JSON-escaped (`\n` as two
#: characters, `\/` for a slash).
_PEM_INLINE_BODY = frozenset(string.ascii_letters + string.digits + "+/=*\\")


def _body_line(line: str, *, headers_allowed: bool) -> str | None:
    """`"blank"`, `"base64"` or `"header"` for a line of a key's body, or None."""
    stripped = line.strip(" \t\r")
    if not stripped:
        return "blank"
    if _PEM_BASE64_LINE.fullmatch(stripped):
        return "base64"
    if headers_allowed and _PEM_HEADER_LINE.fullmatch(stripped):
        return "header"
    return None


def _body_run_end(text: str, pos: int) -> int:
    """Where the key-body lines after position `pos` end.

    `pos` is the newline that ends the line BEFORE the first candidate, or -1
    for the start of `text`. Returns the end (exclusive) of the last body line
    -- blank lines are only taken when more of the body follows them -- or
    `pos` when the first line is not one.
    """
    stop = pos
    seen_base64 = False
    size = len(text)
    while pos < size:
        following = text.find("\n", pos + 1)
        end = size if following < 0 else following
        shape = _body_line(text[pos + 1 : end], headers_allowed=not seen_base64)
        if shape is None:
            break
        if shape != "blank":
            stop = end
            seen_base64 = seen_base64 or shape == "base64"
        pos = end
    return stop


def _line_end(text: str, at: int) -> int:
    newline = text.find("\n", at)
    return len(text) if newline < 0 else newline


def _block_end(
    text: str, head_end: int, end_starts: list[int], end_ends: list[int], *, to_end: bool
) -> int:
    """Where the masked span of the key whose BEGIN marker ends at `head_end` stops."""
    at = bisect_left(end_starts, head_end)
    if at < len(end_starts) and end_starts[at] - head_end <= PEM_BLOCK_MAX_CHARS:
        return _line_end(text, end_ends[at])  # (a)
    if to_end:
        return len(text)  # (b), decoded
    line_end = text.find("\n", head_end)
    if line_end < 0:
        return len(text)
    return _body_run_end(text, line_end)  # (b), text


def _continuation_end(text: str) -> int:
    """(d): where the key that `text` begins inside ends. 0 when nothing of it is here."""
    run = _body_run_end(text, -1)
    line_start = min(run + 1, len(text))
    prefix = _PEM_LINE_PREFIX.match(text, line_start)
    marker = _PEM_END.match(text, prefix.end() if prefix else line_start)
    if marker is not None:
        return _line_end(text, marker.end())
    return max(run, 0)


#: How far back a line's start is looked for from inside it. A key's lines are
#: 64 or 70 characters, a numbered one a few more; a line longer than this is
#: not one, and bounding the search keeps text with many END markers on one
#: long line linear.
_LINE_SCAN_MAX = 4096


def _start_of_line(text: str, at: int, floor: int) -> int | None:
    """The start of the line holding `at`, not before `floor`; None if further than the scan."""
    low = max(floor, at - _LINE_SCAN_MAX)
    newline = text.rfind("\n", low, at)
    if newline >= 0:
        return newline + 1
    return floor if low == floor else None


def _tail_start(text: str, marker_start: int, floor: int) -> int:
    """(c): the first character of the key material above an END marker with no BEGIN."""
    at = marker_start
    while at > floor and text[at - 1] in _PEM_INLINE_BODY:
        at -= 1
    line_start = _start_of_line(text, at, floor)
    if line_start is None or not _PEM_LINE_PREFIX.fullmatch(text, line_start, at):
        return at  # other text precedes the key material on this line
    line_end = line_start - 1  # the newline ending the line above
    while line_end >= floor:
        above = _start_of_line(text, line_end, floor)
        if above is None or _body_line(text[above:line_end], headers_allowed=False) != "base64":
            break
        at = above
        line_end = above - 1
    return at


def _mask_orphan_ends(text: str) -> tuple[str, int]:
    """(c) over every END marker in `text`, which holds no BEGIN."""
    if _PEM_HINT not in text:
        return text, 0
    pieces: list[str] = []
    count = 0
    pos = 0
    for marker in _PEM_END.finditer(text):
        start = _tail_start(text, marker.start(), pos)
        if start < marker.start():
            pieces.append(text[pos:start])
            pieces.append(MASK)
            count += 1
            pos = marker.start()
    pieces.append(text[pos:])
    return "".join(pieces), count


def mask_private_keys(
    text: str, decoded: bool = False, inside_key: bool = False
) -> tuple[str, int]:
    """Every private key in `text` masked as a block: shapes (a) to (d) above.

    `decoded`: `text` is ONE string decoded out of a JSON document, so a key
    with no END is masked to the end of it. `inside_key`: the caller's
    look-back found `text` begins inside a key (`open_key_start`).
    """
    if not inside_key and _PEM_HINT not in text:
        return text, 0
    ends = [(m.start(), m.end()) for m in _PEM_END.finditer(text)]
    end_starts = [start for start, _ in ends]
    end_ends = [end for _, end in ends]
    pieces: list[str] = []
    count = 0
    pos = 0
    if inside_key:
        stop = _continuation_end(text)
        if stop > 0:
            pieces.append(MASK)
            count += 1
            pos = stop
    search = pos
    while True:
        begin = _PEM_BEGIN.search(text, search)
        if begin is None:
            break
        outside, found = _mask_orphan_ends(text[pos : begin.start()])
        pieces.append(outside)
        count += found
        stop = _block_end(text, begin.end(), end_starts, end_ends, to_end=decoded)
        pieces.append(begin.group(0))
        pieces.append(MASK)
        count += 1
        pos = stop
        search = max(stop, begin.end())
    outside, found = _mask_orphan_ends(text[pos:])
    pieces.append(outside)
    return "".join(pieces), count + found


def open_key_start(data: bytes, at: int) -> int | None:
    """Where the BEGIN marker of a key still open at byte `at` of `data` starts, or None.

    Open: the marker STARTS before `at` -- it may run past it, when `at` falls
    inside the marker itself -- and no END marker lies between the two, and
    the BEGIN is within `PEM_BLOCK_MAX_CHARS`. Used two ways: a paged reader
    asks whether its window begins inside a key (shape (d)), and a streamed
    download asks whether the window it is about to cut ends inside one, so
    it can carry the key whole into the next.
    """
    floor = max(0, at - PEM_BLOCK_MAX_CHARS)
    last = None
    for match in _PEM_BEGIN_BYTES.finditer(data, floor, min(len(data), at + _MARKER_MAX)):
        if match.start() >= at:
            break
        last = match
    if last is None:
        return None
    if last.end() >= at:
        return last.start()
    if _PEM_END_BYTES.search(data, last.end(), at) is not None:
        return None
    return last.start()


def _private_key_rule() -> Rule:
    return Rule(
        name="private_key_block",
        shell_marker="PRIVATE KEY",
        # The marker the family starts from; `apply` does the masking.
        pattern=_PEM_BEGIN,
        apply=mask_private_keys,
    )


#: THE ENVIRONMENT-DUMP RULE, and the one that is deliberately wider than the
#: shell's. `[A-Za-z0-9_.-]*` in front of the name is what turns `api_key=`
#: into `ANTHROPIC_API_KEY=`, `GH_TOKEN=` and `db.password:`, and `api[-_]?key`
#: is what catches `x-api-key:` (the shell rule's `api_?key` does not take a
#: hyphen, so its filter lets that header through; the superset relation this
#: module promises still holds). An agent that prints its own environment is
#: the single most likely way a credential reaches a log object, and the shell
#: rule as written does not catch the shape `env` actually produces.
#:
#: WHAT IT STILL DOES NOT CATCH, because the keyword must END the name:
#: `AWS_SECRET_ACCESS_KEY=`, `private_key:`, `password_hash:` and plurals such
#: as `secrets:`. A JSON document's keys are read by `JsonMasker`, which knows
#: where a key ends and matches the keyword anywhere in it; in free text the
#: rule would need a boundary it cannot see.
#:
#: ANCHORED TO THE START OF THE NAME (PR #210 re-review). Unanchored, the name
#: prefix was rescanned from every position of a long run of name characters:
#: a 40,000-character run took 57 s to 127 s, and `/v1/tasks/{id}/input` feeds
#: this rule a caller's input of up to 256 KiB, inside `re`, holding the GIL of
#: an instance every tenant shares. `(?<![A-Za-z0-9_.-])` lets a match start
#: only where a run starts. The output is unchanged: a match that could start
#: inside a run can also start at the run's first character, because the
#: prefix takes any name characters.
#: `tests/unit/control_plane/test_task_input_route.py` bounds it on 256 KiB.
#:
#: ESCAPED QUOTES (#221, owner decision 2026-09-26). In JSON TEXT a quote
#: inside a string is `\"`, and the rule read a quote as a quote: over a raw
#: stream-json line, `DB_PASSWORD=\"<v>\"` had its backslash masked and `<v>`
#: served under a count of 1, and `\"api_key\": \"<v>\"` did not match at all.
#: `/logs` serves exactly those lines, and there is no decoded document to walk
#: there, so the rule itself now takes `\"` where it took `"` -- around the key
#: and before the value -- and a value OPENED by `\"` stops at the next
#: backslash (group 2 records that it was), because in JSON text a backslash
#: always begins an escape: `\"` closing the value, or a `\n` after it. A value
#: NOT opened by one keeps its old class, backslashes included, so a plain
#: `password=ab\cd` is still masked whole; it may not START with `\"`, so an
#: escaped empty value is left alone rather than having its backslash masked.
#:
#: A LIST'S FIRST ELEMENT (the reach limit in PR #210's comment 5841681051).
#: `{"password": ["<v>"]}` masked the `[` and served `<v>`. An optional `[`
#: after the separator now lets the value start at the first element. Only the
#: first: `["a", "b"]` still serves `b` in free text, and an object under a
#: credential's name still serves its strings. A JSON document the API can
#: DECODE goes through `JsonMasker`, which masks either whole.
#:
#: ANY NUMBER OF BACKSLASHES (the PR #229 review). One level of JSON escaping
#: is `\"`; a command that quotes its own quotes, logged as JSON, is `\\\"`,
#: and each level more doubles the run and adds one. So wherever the rule took
#: one backslash before a quote it takes a run:
#:
#:   * before the key, `\\{0,15}` -- BOUNDED, because that group is tried at
#:     every position of the text, and an unbounded run there would rescan a
#:     long run of backslashes from each of its positions (quadratic, on a
#:     shared instance, over text an agent chose). The bound loses nothing: a
#:     longer run still matches, from a later start, and the backslashes
#:     before that start are left exactly where the match would have kept
#:     them. `test_log_redaction.py` times 256 KiB of backslashes;
#:   * after the key and before the value, `\\*` and `\\+` -- unbounded, since
#:     both are only reached after a credential's name has matched;
#:   * a value NOT opened by an escaped quote may not start with a run of
#:     backslashes and a quote either: it starts with a character that is not
#:     a backslash, or a run of backslashes and one that is not a quote. That
#:     is the shell expression's own wording, so the two cannot differ on a
#:     value made only of backslashes.
#:
#: THE SHELL FILTER HAS THE SAME RULE (`scripts/lib/common.sh` `redact`), in two
#: expressions -- the escaped form, then the plain one -- because sed has no
#: conditional group. `tests/unit/control_plane/test_log_redaction.py` runs both
#: over the same lines and holds their output equal.
KEY_VALUE = _rule(
    "key_value_assignment",
    "api_?key",
    r"(?<![A-Za-z0-9_.-])"
    r"((?:\\{0,15}\")?[A-Za-z0-9_.-]*"
    r"(?:api[-_]?key|apikey|password|passwd|secret|token|credential|authorization)"
    r"(?:\\*\")?[ \t]*[:=][ \t]*(?:\[[ \t]*)?(?:(\\+\")|\"?))"
    r"(?(2)[^\"\\,\s]+|(?:\\+[^\",\s\\]|[^\",\s\\])[^\",\s]*)",
    re.IGNORECASE,
)

#: Order matters and mirrors the shell filter's `-e` order: a narrow provider
#: rule runs before the broad key/value rule, so `api_key=sk-live-...` is
#: reduced by the `sk-` rule first and the survivor is masked by the second.
#:
#: ONE EXCEPTION: the private-key block runs FIRST here. A key's base64 body
#: is full of runs other rules match by chance (`ey` and eight more characters
#: is a JWT to the JWT rule), and masking pieces of a body before the block
#: rule sees it only adds counts for one leak.
#:
#: `.` never matches a newline in any pattern here (no re.DOTALL).
RULES: tuple[Rule, ...] = (
    _private_key_rule(),
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
    # `Authorization: Bearer <token>` in any casing, and the `Basic` form,
    # which is a base64 username:password and is no less a credential.
    _rule(
        "http_authorization",
        "[Bb]earer",
        r"((?:[Bb]earer|[Bb]asic)[ \t]+)[A-Za-z0-9._~+/-]{12,}=*",
    ),
    KEY_VALUE,
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


def redact(
    text: str,
    *,
    extra: Iterable[str] = (),
    decoded: bool = False,
    inside_key: bool = False,
) -> Redacted:
    """Mask every credential-shaped run in `text`.

    `extra` takes literal values the caller knows are secret -- it is the same
    idea as the worker's registered-secret set, available here for a caller
    that has one. It is applied FIRST and whole-string, because a literal is
    known to be a credential while a pattern only guesses.

    `decoded`: `text` is ONE string decoded out of a JSON document (a step of
    a transcript, an answer). Every such string must be masked at least as far
    as its raw line would have been, and the raw line rule masks everything
    after a key's BEGIN marker on that line -- so a key with no END is masked
    to the end of the string.

    `inside_key`: a paged reader's look-back found `text` begins inside a
    private key (`open_key_start`); the key material at its head is masked.
    """
    count = 0
    for literal in sorted({v for v in extra if v and len(v) >= 8}, key=len, reverse=True):
        if literal in text:
            count += text.count(literal)
            text = text.replace(literal, MASK)
    for rule in RULES:
        if rule.apply is not None:
            text, found = rule.apply(text, decoded, inside_key)
        else:
            text, found = rule.pattern.subn(r"\1" + MASK, text)
        count += found
    return Redacted(text=text, count=count)


# --------------------------------------------------------------------------
# A JSON document: masked by its structure, not by its text
# --------------------------------------------------------------------------
#
# WHY NOT ONE `redact()` OVER THE JSON TEXT (the PR #210 review). JSON text
# writes every quote inside a string as `\"`, and the key/value rule reads a
# quote as a quote. A string holding `{"api_key": "<bare>"}` did not match at
# all, and one holding `PASSWORD="<bare>"` had its backslash masked and its
# value served.
#
# WHY NOT A SECOND PASS OVER THE JSON TEXT EITHER (the PR #210 re-review).
# The fix-up masked every string, then ran the rules over the JSON text with
# every string stood in, so the key/value rule still saw `"api_token": "..."`
# with its key beside it. But that rule's value class takes the first run of
# characters after the key, and under a key the document's shape decides what
# that run is: `[` or `{` for a list or an object, whose strings were then
# served in clear under `masked 1`; `null` or `true`, counted as a credential;
# and the text stopped being JSON. A key that itself held `=` pushed the mask
# onto the `:` after it. Every one of those is a question about the document's
# STRUCTURE, answered here by reading the structure:
#
#   * every string, and every key, is masked as one DECODED string
#     (`redact(decoded=True)`), the way `/answer` and `/transcript` mask the
#     strings they decode out of JSON;
#   * a value under a key that NAMES a credential (`_CREDENTIAL_KEY`) is masked
#     WHOLE, as one leaf, counted once -- a string, a list, an object, and a
#     number when the credential word ends the key (`_masks_whole` says which);
#   * a private key written as a LIST of lines is masked from the element with
#     its BEGIN marker through the one with its END (`_key_run_end`);
#   * a literal any of those masked -- a value under a credential's name, or a
#     value the key/value rule found beside `NAME=` -- is masked wherever else
#     it appears in the document, the prompt included (`_learned_literals`).
#
# The text is `json.dumps(indent=2, ensure_ascii=False)` of the masked value,
# so it is always the input's JSON, with every string still a string.

#: A key that names a credential: the keyword ANYWHERE in the key, in any case,
#: with `-` or `_` allowed inside the two-word names. Wider than the key/value
#: rule, which needs the keyword to END the name because in free text it cannot
#: see where a name ends: here a key is a whole string, so `AWS_SECRET_ACCESS_KEY`,
#: `private_key`, `password_hash`, `secrets`, `api_keys` and `x-api-key` are all
#: credentials' names.
_CREDENTIAL_WORD = r"api[-_]?key|passw(?:or)?d|secret|token|credential|private[-_]?key|authorization"
_CREDENTIAL_KEY = re.compile(_CREDENTIAL_WORD, re.IGNORECASE)
#: The keyword ENDS the key: `password`, `DB_PASSWORD`, `github_token`. The key
#: part of the key/value rule, with `api-key` and `private_key` added.
_CREDENTIAL_KEY_AT_END = re.compile(r"(?:" + _CREDENTIAL_WORD + r")\Z", re.IGNORECASE)

#: At most this many literals are carried from one place in a document to the
#: others. Each is looked for in every string, so the work is this bound times
#: the document's size -- 256 KiB at most for a task's input -- and a document
#: built to hold thousands of `NAME=value` pairs must not buy that many passes
#: over itself on a shared instance. An input with more named credentials than
#: this still has each one masked where it is named; only the copies elsewhere
#: past the first 256 are not looked for.
MAX_LITERALS = 256

#: Brackets a key's stand-in (`JsonMasker.json`). Private-use code points: no
#: key holds one by accident, and the nonce beside them means none can spell one
#: on purpose. The served text never holds one; each is replaced below.
_STAND_IN_OPEN = ""
_STAND_IN_CLOSE = ""


def _key_text(key: Any) -> str:
    """A dict key as `json.dumps` writes it."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key)
    return str(key)


def _holds_value(node: Any, *, numbers: bool) -> bool:
    """Whether a list or object holds a non-blank string -- or a number, when `numbers`."""
    if isinstance(node, str):
        return node.strip() != ""
    if isinstance(node, bool) or node is None:
        return False
    if isinstance(node, (int, float)):
        return numbers
    if isinstance(node, dict):
        return any(_holds_value(item, numbers=numbers) for item in node.values())
    if isinstance(node, (list, tuple)):
        return any(_holds_value(item, numbers=numbers) for item in node)
    return str(node).strip() != ""


def _masks_whole(key: str, value: Any) -> bool:
    """Whether `value`, under `key`, is masked whole as one credential.

    Only under a key that names a credential. Then:

      * a string that is not blank. An empty or whitespace-only string is drawn
        as it was sent: a `masked 1` over `""` tells the reader to rotate a
        credential nobody sent;
      * a list or an object holding such a string, masked as one leaf, so a
        token split into lines, or `{"value": "..."}`, is not served piece by
        piece under a count that says it was masked;
      * a NUMBER only when the credential word ENDS the key: a PIN under
        `password` is one, `input_tokens: 10` and `credential_revoked_times: 2`
        -- keys this platform's own mock runner takes -- are counts;
      * never `null`, `true`/`false`, `[]` or `{}`: none of them can be a
        credential, and each counted one on the screen.
    """
    if _CREDENTIAL_KEY.search(key) is None:
        return False
    if value is None or isinstance(value, bool):
        return False
    numbers = _CREDENTIAL_KEY_AT_END.search(key) is not None
    if isinstance(value, (int, float)):
        return numbers
    return _holds_value(value, numbers=numbers)


def _leaf_texts(node: Any) -> list[str]:
    """Every string, and every number as JSON writes it, inside `node`."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, bool) or node is None:
        return []
    if isinstance(node, (int, float)):
        return [json.dumps(node)]
    if isinstance(node, dict):
        return [text for item in node.values() for text in _leaf_texts(item)]
    if isinstance(node, (list, tuple)):
        return [text for item in node for text in _leaf_texts(item)]
    return [str(node)]


def _assigned_values(text: str) -> list[str]:
    """The values the key/value rule finds beside a credential's name in `text`, unmasked."""
    return [m.group(0)[len(m.group(1)) :] for m in KEY_VALUE.pattern.finditer(text)]


def _carried(literal: str) -> bool:
    """Whether a literal masked in one place is looked for in every other.

    Eight characters or more, as `redact(extra=...)` requires. Not one the rules
    already mask wherever it appears (`sk-...`, a JWT), nor a private key's
    marker, which the rules keep on purpose so a reader sees what leaked. And not
    a run of mask characters, which would find every mask.
    """
    if len(literal.strip()) < 8 or literal.strip("*").strip() == "":
        return False
    if _PEM_HINT in literal:
        return False
    return redact(literal, decoded=True).count == 0


def _learned_literals(document: Any) -> tuple[str, ...]:
    """What `document` says is secret, to be masked wherever else it appears.

    Two sources: every value masked whole under a credential's name (a string,
    or the strings and numbers inside it), and every value the key/value rule
    finds beside `NAME=` in a string or a key. Named values first, because a
    name is better evidence than a pattern. Longest first, so a literal that
    contains another is masked as itself.
    """
    named: list[str] = []
    assigned: list[str] = []

    def learn(node: Any) -> None:
        if isinstance(node, str):
            assigned.extend(_assigned_values(node))
        elif isinstance(node, dict):
            for key, item in node.items():
                name = _key_text(key)
                assigned.extend(_assigned_values(name))
                if _masks_whole(name, item):
                    named.extend(_leaf_texts(item))
                else:
                    learn(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                learn(item)
        elif node is not None and not isinstance(node, (bool, int, float)):
            learn(str(node))

    learn(document)
    chosen: list[str] = []
    seen: set[str] = set()
    for candidate in named + assigned:
        for literal in (candidate, candidate.strip()):
            if literal in seen:
                continue
            seen.add(literal)
            if len(chosen) < MAX_LITERALS and _carried(literal):
                chosen.append(literal)
    return tuple(sorted(chosen, key=len, reverse=True))


def _open_key(text: str) -> bool:
    """Whether `text` holds a private key's BEGIN marker with no END after the last one."""
    if _PEM_HINT not in text:
        return False
    last = None
    for last in _PEM_BEGIN.finditer(text):
        pass
    return last is not None and _PEM_END.search(text, last.end()) is None


def _key_run_end(items: list[Any] | tuple[Any, ...], start: int) -> int:
    """The last element of the key that element `start` of a list leaves open.

    A private key stored as an ARRAY OF LINES -- `["-----BEGIN ...", "MIIE...",
    ..., "-----END ..."]` -- holds its BEGIN in one string and its body in the
    next ones, none of which carries a marker, so masking each string on its own
    masked the BEGIN element to its end and served every body line and the END
    (the PR #210 re-review). The same rule as a key in one string
    (`mask_private_keys`, decoded): through the element holding the END marker
    when it is within `PEM_BLOCK_MAX_CHARS`, else to the end of the run of
    strings. A non-string element ends the run. `start` itself when nothing
    follows it.
    """
    size = 0
    last = start
    for at in range(start + 1, len(items)):
        item = items[at]
        if not isinstance(item, str):
            break
        if size <= PEM_BLOCK_MAX_CHARS and _PEM_END.search(item) is not None:
            return at
        size += len(item) + 1
        last = at
    return last


class JsonMasker:
    """A JSON document's values as a screen may draw them, masked, with counts.

    Built over the WHOLE document, so what one place says is secret is masked in
    every block drawn from it: `/v1/tasks/{id}/input` draws the prompt, the rest
    of the input and the whole of it from one masker, and a password named in
    the rest is masked inside the prompt too, where the prompt block would
    otherwise draw it in clear under `masked 0` beside `"password": "********"`
    (the PR #210 re-review).

    `text()` masks one string and `json()` one value as the pretty-printed text
    `JSON.stringify(value, null, 2)` draws. Each string's result is kept, so a
    string drawn in several blocks is masked and counted the same in each, and
    a block's count is the sum of what its strings, keys and whole values
    contributed: `json(whole)` counts what `text(prompt)` and `json(rest)` do.

    `value()` is the same masking as a JSON VALUE rather than its text (the
    owner's "masked everywhere", 2026-09-26): `GET /v1/tasks/{id}` serves a
    task's `input` and `metadata` as objects a client reads keys out of, so the
    masked copy has to stay an object. It counts exactly what `json()` counts
    for the same value, and `json.dumps` of it is `json()`'s text, except where
    two keys of one object mask to the same text: an object cannot hold one key
    twice, so the second is served as `<masked key> (2)`, and so on.
    """

    def __init__(self, document: Any, *, literals: Iterable[str] = ()) -> None:
        """`literals`: values another document already named as secret.

        A task's masker (`task_input.TaskMasking`) hands what it learned from
        the task's input and metadata to a log line's masker, so a value the
        caller named as a credential is masked in the agent's output too.
        """
        learned = _learned_literals(document)
        extra = tuple(v for v in literals if v not in learned)
        self._literals = tuple(sorted(learned + extra, key=len, reverse=True))
        self._memo: dict[str, Redacted] = {}
        # A nonce, so no key in the document can spell a stand-in.
        self._tag = secrets.token_hex(8)

    @property
    def literals(self) -> tuple[str, ...]:
        """What this document named as secret, longest first (`_learned_literals`)."""
        return self._literals

    def text(self, value: str, *, remember: bool = True) -> Redacted:
        """One string, masked as a decoded string, then for the document's literals.

        The literals go AFTER the rules, not first as `redact(extra=...)` puts
        them: a literal that happened to hold a marker a rule starts from would
        otherwise take the marker away from the rule. Each is looked for in what
        the rules left, so a literal the rules already masked is not counted
        again.

        `remember=False` masks without keeping the result: for a string that is
        not part of the document (a task's `last_error`, an event's detail),
        masked with this document's literals by a masker that outlives the
        request (`task_input.masking_for`), whose memory must stay the
        document's size.
        """
        found = self._memo.get(value)
        if found is None:
            first = redact(value, decoded=True)
            text, count = _mask_literals(first.text, self._literals)
            found = Redacted(text=text, count=first.count + count)
            if remember:
                self._memo[value] = found
        return found

    def _walk(self, value: Any, *, label_for: Callable[[dict[str, Any], str, str], str]) -> tuple[Any, int]:
        """`value` with every string, key and credential masked, and the count.

        `label_for(out, raw_key, masked_key)` names the entry a key becomes in
        the object being built: `json()` stands a changed or colliding key in
        and restores it in the text, `value()` numbers a collision.
        """
        total = 0

        def walk(node: Any) -> Any:
            nonlocal total
            if isinstance(node, str):
                masked = self.text(node)
                total += masked.count
                return masked.text
            if isinstance(node, dict):
                out: dict[str, Any] = {}
                for key, item in node.items():
                    raw = _key_text(key)
                    name = self.text(raw)
                    total += name.count
                    label = label_for(out, raw, name.text)
                    if _masks_whole(raw, item):
                        total += 1
                        out[label] = MASK
                    else:
                        out[label] = walk(item)
                return out
            if isinstance(node, (list, tuple)):
                items = list(node)
                shaped: list[Any] = []
                through = -1
                for at, item in enumerate(items):
                    if at <= through:
                        # Key material of the block the element above opened:
                        # its one mask was counted there.
                        shaped.append(MASK)
                        continue
                    shaped.append(walk(item))
                    if isinstance(item, str) and _open_key(item):
                        through = _key_run_end(items, at)
                return shaped
            if node is None or isinstance(node, (bool, int, float)):
                return node
            # Not JSON: a Firestore timestamp in a hand-written document.
            return walk(str(node))

        return walk(value), total

    def json(
        self,
        value: Any,
        *,
        indent: int | None = 2,
        separators: tuple[str, str] | None = None,
    ) -> Redacted:
        """`value` as `json.dumps(value, indent=indent, ensure_ascii=False)`, masked.

        `indent=None` is the one-line text `json.dumps` writes by default, which
        is what a transcript step's `raw` record has always been. `separators`
        is `json.dumps`'s: a log line re-written compactly passes `(",", ":")`.
        """
        keys: list[str] = []

        def stand_in(out: dict[str, Any], raw: str, masked: str) -> str:
            # A key is stood in when masking changed it, or when it now spells
            # another key: two keys that mask to the same text are two
            # entries, not one.
            if masked != raw or masked in out:
                keys.append(masked)
                return f"{_STAND_IN_OPEN}{self._tag}.{len(keys) - 1}{_STAND_IN_CLOSE}"
            return masked

        shaped, total = self._walk(value, label_for=stand_in)
        text = json.dumps(shaped, indent=indent, ensure_ascii=False, separators=separators)
        if keys:
            placed = re.compile(
                '"' + re.escape(f"{_STAND_IN_OPEN}{self._tag}.") + r"(\d+)" + re.escape(_STAND_IN_CLOSE) + '"'
            )
            text = placed.sub(lambda m: json.dumps(keys[int(m.group(1))], ensure_ascii=False), text)
        return Redacted(text=text, count=total)

    def value(self, value: Any) -> tuple[Any, int]:
        """`value` as a JSON value, masked exactly as `json()` masks it, and the count."""

        def numbered(out: dict[str, Any], _raw: str, masked: str) -> str:
            if masked not in out:
                return masked
            n = 2
            while f"{masked} ({n})" in out:
                n += 1
            return f"{masked} ({n})"

        return self._walk(value, label_for=numbered)


def redact_json(value: Any, *, indent: int | None = 2) -> Redacted:
    """One JSON value, masked by its structure as `JsonMasker` does, with the count."""
    return JsonMasker(value).json(value, indent=indent)


def _mask_literals(text: str, literals: Iterable[str]) -> tuple[str, int]:
    """Every occurrence of each literal, longest first, masked; and how many."""
    count = 0
    for literal in literals:
        if literal and literal in text:
            count += text.count(literal)
            text = text.replace(literal, MASK)
    return text, count


#: A log line longer than this is masked as text, never decoded: `json.loads`
#: and a walk of its strings are not worth a shared instance's time on a line
#: no agent CLI writes. Every window `/logs` serves is at most a few MiB.
JSON_LINE_MAX_CHARS = 1024 * 1024


def _json_document(line: str) -> Any | None:
    """The object or list `line` holds, when the whole line is one; else None."""
    body = line.strip()
    if len(body) < 2 or len(body) > JSON_LINE_MAX_CHARS:
        return None
    if not ((body[0] == "{" and body[-1] == "}") or (body[0] == "[" and body[-1] == "]")):
        return None
    try:
        document = json.loads(body)
    except ValueError:
        return None
    return document if isinstance(document, (dict, list)) else None


def redact_lines(
    text: str, *, inside_key: bool = False, literals: Iterable[str] = ()
) -> Redacted:
    """A log window, masked line by line: a JSON document by its structure, text by the rules.

    WHY (the PR #229 review of #221). `/logs` served every line through the
    rules over its TEXT. On a stream-json line that is JSON text, and the
    key/value rule can only guess where a value ends inside it: a list or an
    object under a credential's name served every element after the first, a
    value holding a space or a backslash served the rest, and a command that
    quoted its own quotes sat one escape deeper than the rule looked. A line
    that IS a JSON document is now decoded and masked by `JsonMasker`, which
    answers each of those from the document's structure, and every string in
    it is masked decoded, as `/transcript` masks the same event.

      * A line whose masking changed nothing is served BYTE FOR BYTE as stored.
        A line that was masked is written back as compact JSON (`","`, `":"`,
        what an agent CLI writes), so it stays one line and one document.
      * Everything that is not a whole document -- plain text, a line a window
        cut, a document over `JSON_LINE_MAX_CHARS` -- is masked by `redact`,
        in runs, so a private key printed over many lines is still one block.
        `inside_key` applies to the window's first run only, which is where a
        paged reader's look-back found the key open.
      * `literals`: values the task named as secret (`TaskMasking.literals`),
        masked after the rules wherever they appear, in both halves.
    """
    literals = tuple(literals)
    pieces: list[str] = []
    count = 0
    run: list[str] = []
    first_run = True

    def flush() -> None:
        nonlocal count, first_run
        if not run:
            return
        body = "".join(run)
        scrubbed = redact(body, inside_key=inside_key and first_run)
        masked, found = _mask_literals(scrubbed.text, literals)
        pieces.append(masked)
        count += scrubbed.count + found
        run.clear()
        first_run = False

    # Split on "\n" only. `str.splitlines` also splits on U+2028 and U+2029,
    # which JSON allows unescaped INSIDE a string, and a line cut there parses
    # as nothing and would silently fall back to the text rule.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    for index, line in enumerate(lines):
        document = None if (inside_key and index == 0) else _json_document(line)
        if document is None:
            run.append(line)
            continue
        flush()
        first_run = False
        masked = JsonMasker(document, literals=literals).json(
            document, indent=None, separators=(",", ":")
        )
        if masked.count:
            ending = line[len(line.rstrip("\r\n")) :]
            pieces.append(masked.text + ending)
            count += masked.count
        else:
            pieces.append(line)
    flush()
    return Redacted(text="".join(pieces), count=count)


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
