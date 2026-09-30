"""The credential PATTERNS: what "credential-shaped" means on this platform.

WHY THIS IS ITS OWN PACKAGE (owner decision 4, 2026-09-28, PR #259). These
rules lived in `swarm_api.redaction`, where only swarm-api could use them. The
worker needs them too: before it pushes an agent's commits one by one, it
scans each commit's diff for the task's registered secrets AND for these
patterns, and a hit folds the history into one commit of the clean final
tree (`agent_worker.lifecycle.replay_agent_commits`). A registered secret is
only a value this platform resolved itself; a key the agent minted, or an
`.env` it committed, is caught only by a pattern. A second copy of the rules
in the worker would be a mirrored value, and every mirrored value here has
drifted, so both import this one.

It is NOT under `apps/common/swarm_common/`, which CONTRACT.md freezes. It
imports nothing but the standard library, so installing it costs an image
nothing, and `swarm_api.redaction` re-exports every name that moved, so no
import of the old path changed.

RELATIONSHIP TO `redact()` IN scripts/lib/common.sh
---------------------------------------------------
That shell function is the house definition of "credential-shaped". The rules
below are the same families, in the same order, with the same `\\1********`
shape, so an operator reading a redacted log in the terminal and a user reading
one in the browser see the same thing.

THEY GIVE THE SAME OUTPUT, held by ONE FIXTURE SET (wave 2026-09-27):
`tests/fixtures/redaction-parity.json` is run through both filters by
`tests/unit/control_plane/test_log_redaction.py` and by section 12 of
`scripts/lib/check-contract-parity.sh`, and each must give every case's
`expected` text exactly. This used to be described as a SUPERSET -- this
module masking everything the shell's did "and possibly more" -- and the gap
it allowed was measured three times: a plain-text private key the terminal
printed after its BEGIN line (#206), `X-API-KEY:` (#227), and names like
`AWS_SECRET_ACCESS_KEY=` that both let through (#224). The drift check that
fails when a rule is added to the shell filter and not here stays too.

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

import re
import string
from bisect import bisect_left
from dataclasses import dataclass
from typing import Callable, Iterable

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
# after that line (#206; the shell filter now masks the same blocks, with an
# awk stage that holds lines back -- `redact` in scripts/lib/common.sh). On a raw NDJSON line that is the whole key: JSON writes the
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


#: THE ENVIRONMENT-DUMP RULE. `[A-Za-z0-9_.-]*` in front of the name is what
#: turns `api_key=` into `ANTHROPIC_API_KEY=`, `GH_TOKEN=` and `db.password:`,
#: and `api[-_]?key` is what catches `x-api-key:`. An agent that prints its own
#: environment is the single most likely way a credential reaches a log object.
#: The shell filter reaches the same names by not anchoring at all, and since
#: wave 2026-09-27 takes `api[-_]?key` too: it spelled it `api_?key`, so it
#: printed `X-API-KEY: <v>` and `api-key=<v>` that this rule masked (#227).
#:
#: NAMES THE KEYWORD DOES NOT END (#224). The rule served `AWS_SECRET_ACCESS_KEY=`,
#: `private_key:`, `password_hash:` and plural names such as `secrets:` in clear,
#: because the keyword had to END the name. In free text the name's end IS
#: visible -- it is the separator -- but "the keyword anywhere in the name"
#: would take `secret_name:`, `token_count=` and `password_file:` too, so the
#: rule names what may FOLLOW the keyword instead: `private[-_]?key` joins the
#: keywords, and after any keyword may come a plural `s` and one of the
#: suffixes `key`, `access_key` and `hash` (group `wide`). That is
#: `SECRET_KEY`, `AWS_SECRET_ACCESS_KEY`, `password_hash`, `api_keys`, `tokens`.
#:
#: A COUNT UNDER A WIDER NAME IS A COUNT. `max_tokens=4096`, `"input_tokens":
#: 10` and `"output_tokens":5}` are usage figures in every agent's stream-json
#: log. So under a `wide` name the value must hold a character that is not a
#: digit, a `.` or a closing bracket; under a name the keyword ENDS
#: (`password: 12345678`) a number is still masked. That is the line
#: `JsonMasker` draws for a JSON key (`_masks_whole`: a number only when the
#: credential word ends the key), drawn in text.
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
#:
#: A PLURAL NAME IS A COUNT, A KEY-SUFFIXED NAME IS A CREDENTIAL (#260). The
#: wide group used to be one alternative, `s` or `s?[-_](access-key|key|hash)`,
#: and both took the same lenient value class -- a run of digits alone is a
#: count, not a credential (`max_tokens=4096`, `"output_tokens":5}`), so it was
#: left alone. That is right for a bare plural (`tokens`, `secrets`, `keys`
#: counted): `secrets: 3` and `max_tokens=4096` must stay counts. It is wrong
#: for a name that ends in `_key`, `_access_key` or `_hash` -- `SECRET_KEY`,
#: `API_ACCESS_KEY`, `PASSWORD_HASH` -- which name a credential exactly as a
#: singular one does, digits and all: `SECRET_KEY=1234567` is a leak. `plural`
#: and `keysuffix` are now two named groups, not one; a key-suffixed name falls
#: through to the same (always-mask) branch a singular name already used.
#:
#: A PLURAL OR WIDE NAME FOLLOWED BY `: ` DOES NOT TAKE A CAPITALISED ENGLISH
#: WORD OR A STATUS WORD AS ITS VALUE (#260). gcloud's `...your current auth
#: tokens: Reauthentication required.` masked `Reauthentication` -- a wide name
#: happened to precede an ordinary sentence, not a credential -- and `Found 3
#: secrets: none leaked` masked `none`. Before a value is taken under a wide
#: name reached by `: ` (never `=`, where a capitalised word is at least as
#: likely to be a real credential), a negative lookahead refuses a status word
#: (`not set`, `unset`, `set`, `none`, `(none)`, `missing`) or a capitalised
#: word (`[A-Z][a-z]+`) that runs to a boundary. Singular names are untouched:
#: the shell filter alone passes a status word through for those (see above),
#: and that gap is unaffected here.
_STATUS_OR_CAPITALISED_WORD = (
    r"(?:not set|unset|missing|\(none\)|none|set|[A-Z][a-z]+)(?=[\"\\,\s]|$)"
)
KEY_VALUE = _rule(
    "key_value_assignment",
    "api[-_]?key",
    r"(?<![A-Za-z0-9_.-])"
    r"((?:\\{0,15}\")?[A-Za-z0-9_.-]*"
    r"(?:api[-_]?key|apikey|private[-_]?key|password|passwd|secret|token|credential|authorization)"
    r"(?:(?P<plural>s)|(?P<keysuffix>s?[-_](?:access[-_]?key|key|hash)))?"
    r"(?:\\*\")?[ \t]*(?:(?P<colon>:)|=)[ \t]*(?:\[[ \t]*)?(?:(?P<esc>\\+\")|\"?))"
    r"(?(esc)"
    r"(?(colon)(?(plural)(?!" + _STATUS_OR_CAPITALISED_WORD + r")"
    r"|(?(keysuffix)(?!" + _STATUS_OR_CAPITALISED_WORD + r")|))|)"
    r"(?(plural)[0-9.)\]}]*[^0-9.)\]}\"\\,\s][^\"\\,\s]*|[^\"\\,\s]+)"
    r"|"
    r"(?(colon)(?(plural)(?!" + _STATUS_OR_CAPITALISED_WORD + r")"
    r"|(?(keysuffix)(?!" + _STATUS_OR_CAPITALISED_WORD + r")|))|)"
    r"(?(plural)(?:\\+[^\",\s\\]|[0-9.)\]}]*[^0-9.)\]}\",\s\\])|(?:\\+[^\",\s\\]|[^\",\s\\]))"
    r"[^\",\s]*)",
    re.IGNORECASE,
)

#: Order matters and mirrors the shell filter's `-e` order: a narrow provider
#: rule runs before the broad key/value rule, so `api_key=sk-live-...` is
#: reduced by the `sk-` rule first and the survivor is masked by the second.
#:
#: The private-key block runs FIRST, in both filters (the shell's is an awk
#: stage in front of its sed, since #206). A key's base64 body is full of
#: runs other rules match by chance (`ey` and eight more characters is a JWT
#: to the JWT rule), and masking pieces of a body before the block rule sees
#: it only adds counts for one leak -- and, in the shell, would change the
#: body lines the block is recognised by.
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
    # `Authorization: token <x>` -- GitHub's own scheme word -- served <x> in
    # both filters (epic #227): the rule above knows only `Bearer` and `Basic`,
    # and the key/value rule masks the first word after `Authorization:`,
    # which is the scheme. So after a header NAMED `authorization`, the value
    # after a known scheme word is masked, whatever its length. Only after
    # that name: `token` followed by a word is ordinary prose anywhere else.
    # A value that starts with `*` is one the rule above already masked. The
    # key/value rule then masks the scheme word, as it always has `Bearer`.
    #
    # THE PROVIDER PREFIX SURVIVES, IN BOTH FILTERS (#260). `Authorization:
    # Bearer ya29.a0...` already has its value masked to `ya29.********` by
    # `google_oauth_access_token` above -- this rule ran anyway, took the
    # WHOLE masked value (its class does not exclude `*`) and served a second,
    # provider-blind `********`; the key/value rule then masked the scheme
    # word too, and the line read `Authorization: ******** ********`, not the
    # house style's `Authorization: ******** ya29.********`. A value already
    # holding `********` is left alone: a negative lookahead refuses to take
    # this rule's value at all when the mask marker is somewhere in the run
    # ahead, so the scheme word is still masked by the key/value rule and the
    # provider-tagged value it left behind is not touched a second time.
    _rule(
        "http_authorization_scheme",
        "(token|bearer|basic|digest",
        r"(authorization(?:\\*\")?[ \t]*[:=][ \t]*(?:\\*\")?"
        r"(?:token|bearer|basic|digest|negotiate|oauth|api[-_]?key|key|ssws)[ \t]+)"
        r"(?![^\"\\,\s]*\*\*\*\*\*\*\*\*)"
        r"[^*\"\\,\s][^\"\\,\s]*",
        re.IGNORECASE,
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
