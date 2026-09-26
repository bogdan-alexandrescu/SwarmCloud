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


#: Bracket a string's stand-in in pass 2 of `redact_json`. Private-use code
#: points: no rule matches them and `\s` does not include them, so the
#: key/value rule's value class takes a stand-in whole or not at all.
_STAND_IN_OPEN = ""
_STAND_IN_CLOSE = ""


def redact_json(value: Any, *, cache: dict[str, Redacted] | None = None) -> Redacted:
    """A JSON value as the pretty-printed text a screen draws, masked, with the count.

    The text is `json.dumps(value, indent=2, ensure_ascii=False)`, as
    `JSON.stringify(value, null, 2)` draws it, with every credential masked.

    WHY NOT ONE `redact()` OVER THAT TEXT (the PR #210 review). JSON text
    writes every quote inside a string as `\\"`, and the key/value rule reads
    a quote as a quote. A string holding `{"api_key": "<bare>"}` did not match
    at all, and one holding `PASSWORD="<bare>"` had its backslash masked and
    its value served. The same string masked DECODED came out masked, so
    `/input` masked a prompt in its `prompt` block and served it in clear in
    its `full` block, under a count that said nothing was found.

    WHY NOT EACH STRING ON ITS OWN EITHER: the key/value rule catches
    `"api_token": "<bare>"` only with the key beside its value, and a key is
    not inside any string.

    So two passes, of the one set of `RULES`:

      1. every string in the value, DECODED (`redact(decoded=True)`), the way
         `/answer` and `/transcript` mask the strings they decode;
      2. the JSON text of the value with every string replaced by an inert
         stand-in, through `redact()`. This pass sees what only the
         document's shape shows: a credential's name beside its value, a
         credential in a key, a number under `password`. When pass 2 masks a
         string's stand-in, the whole string is masked. The key/value rule
         would have masked its first word.

    Pass 2 never reads what pass 1 masked, so nothing is masked or counted
    twice. The count is pass 1's over every string plus pass 2's. Keys are
    read by pass 2 only, in their JSON spelling. A value that is not JSON (a
    Firestore timestamp in a hand-written document) is masked as its text.

    `cache` maps a string to its pass-1 result. A caller that draws one string
    in several blocks, like the prompt alone and inside the whole input,
    masks it once and counts it the same way in each block.
    """
    memo: dict[str, Redacted] = {} if cache is None else cache
    leaves: list[Redacted] = []
    # A nonce, so no key in the document can spell a stand-in. The served
    # text never holds one: each is replaced below or was masked by pass 2.
    tag = secrets.token_hex(8)

    def stand_in(node: Any) -> Any:
        if isinstance(node, str):
            found = memo.get(node)
            if found is None:
                found = memo[node] = redact(node, decoded=True)
            leaves.append(found)
            return f"{_STAND_IN_OPEN}{tag}.{len(leaves) - 1}{_STAND_IN_CLOSE}"
        if isinstance(node, dict):
            return {key: stand_in(item) for key, item in node.items()}
        if isinstance(node, (list, tuple)):
            return [stand_in(item) for item in node]
        if node is None or isinstance(node, (bool, int, float)):
            return node
        return stand_in(str(node))

    shaped = redact(json.dumps(stand_in(value), indent=2, ensure_ascii=False))
    placed = re.compile(
        '"' + re.escape(f"{_STAND_IN_OPEN}{tag}.") + r"(\d+)" + re.escape(_STAND_IN_CLOSE) + '"'
    )
    text = placed.sub(
        lambda m: json.dumps(leaves[int(m.group(1))].text, ensure_ascii=False), shaped.text
    )
    return Redacted(text=text, count=sum(leaf.count for leaf in leaves) + shaped.count)


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
