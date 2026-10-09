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
    """(c): the first character of the key material above an END marker with no BEGIN.

    Lines above are taken no further than `PEM_BLOCK_MAX_CHARS` from the
    marker, the reach a BEGIN has to find its END (wave 2026-09-27): a key's
    body is under 13 KB, so base64 further up is not that key's. The bound is
    also what lets the shell filter (`redact` in scripts/lib/common.sh),
    which streams, hold the lines an END may yet claim -- it cannot hold an
    unbounded run -- and the two filters agree only because both keep it.
    """
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
        if marker_start - above > PEM_BLOCK_MAX_CHARS:
            break
        at = above
        line_end = above - 1
    return at


def _mask_orphan_ends(text: str) -> tuple[str, int]:
    """(c) over every END marker in `text`, which holds no BEGIN.

    AN ORPHAN END IS MASKED AND COUNTED EVEN WITH NOTHING BEFORE IT TO TAKE
    (the security pass, 2026-10-09; #361 box 82). Code that builds a key --
    the BEGIN marker split across two string pieces, the base64 body in a
    variable, `"\\n".join([begin, body, "<END marker>"])` -- leaves an END
    with no key material beside it or above it. That counted 0, and the
    worker's publish guard refuses a diff exactly when this count is not 0,
    so the code was published. An END marker is a key's tail wherever it
    stands: `MASK` goes in front of it, as it does when material is taken,
    and it counts.
    """
    if _PEM_HINT not in text:
        return text, 0
    pieces: list[str] = []
    count = 0
    pos = 0
    for marker in _PEM_END.finditer(text):
        start = _tail_start(text, marker.start(), pos)
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

#: CODE IS NOT A CREDENTIAL (#370). `swarm artifact <task> swarm-work.patch`
#: served a patch that no longer applied: this rule masked the value of
#: `_TOKEN = re.compile(...)`, `_MASK_TOKEN = json.dumps(MASK)`,
#: `token: str | None` and `f(token=token_var)`, so the `-` and context lines
#: of every hunk touching them no longer matched the file. A value that is an
#: EXPRESSION names a credential without holding one, and is now left alone.
#: A LITERAL is still masked: a quoted string, a bare token-shaped value, and
#: every `KEY=value` line of an environment dump or an INI file.
#:
#: What counts as an expression is decided by the SHAPE of the value, case
#: sensitively, never by guessing at the language:
#:
#:   * after `:` -- a builtin or typing name, `null`, a dotted name, an empty
#:     `${NAME:-}`, and except under `password`/`passwd` a PascalCase or
#:     dotted TYPE where a type ends and a closed call (`_COLON_VALUE`). A
#:     YAML `secret: missing_link_42` and a JSON `"secret":true` are literals
#:     and stay masked;
#:   * after `=` -- `_EXPRESSION`, but ONLY when the name holds a lowercase
#:     letter or a space precedes the `=`. `GITHUB_TOKEN=unset_me_now_123` is
#:     an environment dump, where every value is a literal, so an all-capitals
#:     name glued to its `=` gets no exemption at all;
#:   * anywhere -- a shell substitution, `$(cmd ...)`, `${NAME}`, an empty
#:     `${NAME:-}` or `${{ ... }}`, quoted or not.
#:
#: THE THREE WAYS THE FIRST ATTEMPT AT THIS WENT WRONG (review of
#: task_a2068d6586ec441fa6e3), and what stops each:
#:
#:   1. A bare name was exempt whatever it looked like, so `TOKEN = <36 random
#:      letters>`, a dotted random value and an INI `password = <word>` were
#:      served whole. A single bare word is now exempt only when it is a
#:      credential keyword itself (`token=token`, `secrets = _secrets`) or
#:      `None`/`True`/`False`/`null`/`undefined`. A multi-word name must be
#:      CONVENTIONAL -- all-lowercase snake_case, all-capitals SNAKE_CASE,
#:      camelCase or PascalCase, with words of at most 16 characters -- which
#:      a random value almost never is: it mixes cases inside a word and runs
#:      two capitals together. A dotted name must start with `self`/`cls`/
#:      `this` or END in a multi-word, underscored or keyword name, so
#:      `correct.horse.battery` stays masked.
#:   2. Under `password`/`passwd` a value is a human's choice, and
#:      `my_dog_rex` or `myDogRex` is a plausible one. So under those names a
#:      bare multi-word name is NOT exempt (`_EXPRESSION_PASSWORD`), and
#:      neither is a call or a PascalCase "type"; a dotted name, a keyword
#:      (`password=password`) and `None` still are.
#:   3. Typed parameters (`token: str | None = None`) and keyword arguments
#:      (`f(token=token_var)`) are the commonest shapes in this repository;
#:      both are covered above, and the git-apply test changes both.
#:
#: THE #403 SECURITY REVIEW then measured three shapes the first version
#: served and main masked, each fixed below where it is defined: a call
#: (`_CLOSED_CALL`), a PascalCase or dotted type (`_TYPE_END`), and a dotted
#: name starting with one character (`_FIRST_PART`). With them, a random
#: 12-20 character symbol password is served whole under `password:`,
#: `password =`, `db_password =` or `api_key:` in 0.00-0.03% of samples (main
#: 0%, the first version about 6%), and a Vault `s.`/`hvb.` token in none.
#: The #403 re-review then found literals riding inside an exempt call
#: (`api_key = SecretStr('<v>')`); a call now exempts its callee, never an
#: argument that is a secret run (`_SECRET_RUN`). And the owner decided on
#: 2026-09-30 that a multi-word name under a non-password key is code only
#: when one of its words is a credential word (`_CREDENTIAL_NAME`). What is
#: left: a random no-digit value that reads as camelCase inside a call's
#: arguments (about 0.03% of 16-28 character base62 values), a PascalCase
#: "type" before ` | <Type>` or ` = `, and random symbol strings that parse
#: as a closed call inside a keyword argument (under 0.1%).
#:
#: Every group the decision reads is POSSESSIVE (`?+`), so a refused
#: exemption cannot backtrack into a different parse of the name -- dropping
#: `lc` would turn `token=a_b` into an environment line and mask it.
#:
#: THE SHELL FILTER HAS THE SAME EXEMPTIONS (`redact` in scripts/lib/common.sh),
#: as rules that mark the name before the main rules run; the parity fixture
#: holds both to one output.
#:
#: `passphrase` IS A KEY WORD, WITH `password`'s RULES (the security pass,
#: 2026-10-09; #361 box 63): it was not one at all, so `passphrase = <v>`
#: was served by both filters. Its value is a human's choice, as a
#: password's is, so it joins the `pw` group: no call and no bare name is
#: exempt under it.
_KV_WORDS = (
    r"api[-_]?key|apikey|private[-_]?key|password|passwd|passphrase|secret|token|credential"
    r"|authorization"
)
#: A SECRET RUN (the #403 re-review). An exemption below exempts the SHAPE
#: of code -- a callee, an attribute, a name -- never a literal riding inside
#: it: `api_key = SecretStr('<v>')`, `secret_key = Fernet(<v>)` and
#: `token = environ[<v>]` were served whole because the call around `<v>`
#: was exempt. So no exempt span may hold the START of a run of 16 or more
#: of `[A-Za-z0-9+/=_-]` that mixes a digit with a letter, or that has no
#: digit, a lowercase letter, and does not read as a name (`_NAME_RUN`) -- a
#: random base62 or base64 value, a hex digest. `r"[a-z]+"`, `MASK`, `"GH_TOKEN"`, a
#: snake_case or camelCase name are not runs like that. A run starts where
#: the character before it is not one of those; `_NO_SECRET` is checked at
#: every place an exempt span can hold such a start. The shell filter marks
#: these runs with a byte its exemption rules cannot match across.
_RUN = r"A-Za-z0-9+/=_-"
#: A run with no digit is a NAME when every capital in it starts a hump of
#: two lowercase letters (or is `Id`), bar one at its end: `nextPageToken`,
#: `MetadataIdToken`, `promoted_credentials`. A random letters-only value
#: almost never reads that way.
_NAME_RUN = (
    r"(?:[A-Z][a-z][a-z+/=_-]|Id|[a-z+/=_-])*+(?:[A-Z][a-z]?)?(?![" + _RUN + r"])"
)
_SECRET_RUN = (
    r"(?-i:(?<![" + _RUN + r"])(?=[" + _RUN + r"]{16})"
    r"(?:(?=[" + _RUN + r"]*[0-9])(?=[" + _RUN + r"]*[A-Za-z])"
    r"|(?![" + _RUN + r"]*[0-9])(?=[" + _RUN + r"]*[a-z])(?!" + _NAME_RUN + r")))"
)
_NO_SECRET = r"(?!" + _SECRET_RUN + r")"
_DOT = r"\." + _NO_SECRET
#: The end of a bare value: what may follow a name in code.
#: A closing bracket ends a value only when the brackets run out at a
#: separator or the end of the line -- `f(token=None)`, `{"a": page.token},`
#: -- so `<random>)}%Y` is not read as code (the #403 security review).
_END = r"(?=[\s,;]|$|[)\]}]+(?:[\s,;]|$))"
_KEYWORD_WORD = r"_*(?i:" + _KV_WORDS + r")(?i:s)?"
#: A camelCase hump: a capital and two lowercase letters or more, or `Id`.
_HUMP = r"(?:[A-Z][a-z]{2,15}|Id)[0-9]{0,3}"
#: A MULTI-WORD NAME IS CODE ONLY WHEN IT NAMES A CREDENTIAL (owner decision,
#: 2026-09-30, on the #403 re-review). `token=fetch_token`,
#: `secret=secret_name`, `client_secret=env_secret`, `page_token=page_token`
#: and `token = nextPageToken` stay exempt; `api_key = correct_horse_battery`,
#: `TOKEN = a_b`, `--token=a_b_c` and `token = zebraQuokkaTundra` are a
#: passphrase as easily as a name, and are masked. A credential word is
#: one whole snake_case word or camelCase hump: token, secret, key,
#: password, passwd, credential, auth or api (a plural `s` allowed) --
#: `monkey_business` does not name a key.
#:
#: A SNAKE_CASE WORD IS LETTERS AND AT MOST THREE TRAILING DIGITS, or up to
#: three digits alone (the security pass, 2026-10-09; #361 box 62). A word
#: was any run of 16 letters or digits, so `token = token_3fa9c2e1` -- a
#: credential word beside eight hex digits, which is a value, not a name --
#: read as a name and was served. `token_v2`, `key_1`, `sha256_secret` and
#: `API_KEY_V2` are still names. What the owner decided on 2026-09-30 stays:
#: a digit-free lowercase name holding a credential word is code.
_CRED_LC = r"(?:token|secret|key|password|passwd|credential|auth|api)s?"
_CRED_UC = r"(?:TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|AUTH|API)S?"
_CRED_CAP = r"(?:Token|Secret|Key|Password|Passwd|Credential|Auth|Api)s?"
_CREDENTIAL_NAME = (
    r"(?:_*(?:[a-z]{1,16}[0-9]{0,3}_)*" + _CRED_LC + r"(?:_(?:[a-z]{1,16}[0-9]{0,3}|[0-9]{1,3}))*_*"
    r"|_*(?:[A-Z]{1,16}[0-9]{0,3}_)*" + _CRED_UC + r"(?:_(?:[A-Z]{1,16}[0-9]{0,3}|[0-9]{1,3}))*_*"
    r"|_*[a-z]{1,16}[0-9]{0,3}(?:" + _HUMP + r")*" + _CRED_CAP + r"[0-9]{0,3}(?:" + _HUMP + r")*"
    r"|_*" + _CRED_LC + r"[0-9]{0,3}(?:" + _HUMP + r")+"
    r"|_*(?:" + _HUMP + r")*" + _CRED_CAP + r"[0-9]{0,3}(?:" + _HUMP + r")*)"
)
_PART = (
    r"_*(?:[a-z][a-z0-9_]{0,31}|[A-Z][A-Z0-9_]{0,31}"
    r"|[a-z]{1,16}[0-9]{0,3}(?:" + _HUMP + r")+"
    r"|(?:" + _HUMP + r")+)"
)
#: The first part of a dotted name has two characters or more: `s.<base62>`
#: is a legacy Vault token, not an attribute (the #403 security review).
_FIRST_PART = (
    r"_*(?:[a-z][a-z0-9_]{1,31}|[A-Z][A-Z0-9_]{1,31}"
    r"|[a-z]{1,16}[0-9]{0,3}(?:" + _HUMP + r")+"
    r"|(?:" + _HUMP + r")+)"
)
#: A dotted name starts `self`/`cls`/`this`, or ends in a name that names a
#: credential, an underscored name or a credential keyword:
#: `page.next_page_token`, `settings.client_secret`, `self._x`, `args.token`.
_DOTTED = (
    r"(?:(?:self|cls|this)(?:" + _DOT + _PART + r")+"
    r"|" + _FIRST_PART + r"(?:" + _DOT + _PART + r")*" + _DOT + r"(?:" + _CREDENTIAL_NAME
    + r"|_+" + _PART + r"|" + _KEYWORD_WORD + r"))"
)
#: A CALL OR SUBSCRIPT, CLOSED ON ITS LINE, OF A CONVENTIONAL NAME (the #403
#: security review). The first version took any identifier followed by `(`
#: or `[`, which served 5.6-6.4% of random 12-20 character symbol passwords
#: (`password: aB3x(9k...`). Now the callee must be a conventional name, the
#: argument list must close on the same line -- with at most two nested
#: calls inside, each bounded, so the scan stays linear -- no argument may
#: hold a secret run (`_NO_SECRET`), and what follows must end an
#: expression. Under `password` and `passwd` no call is exempt at all:
#: `password = getpass.getpass(` stays masked, a price paid for a human's
#: choice of value.
_CALLEE = _FIRST_PART + r"(?:" + _DOT + _PART + r")*"
#: An argument is made of what code arguments are made of -- names,
#: numbers, quotes, `.,:=*+/%\\-[]` and spaces -- never `!?#@|&<>^~$;{}`,
#: which a random symbol password holds and a call's arguments rarely do.
_ARG = r"(?:" + _NO_SECRET + r"[A-Za-z0-9_.,:=*'\" +/%\\\[\]-])"
#: A subscript is an index, a name or a quoted key -- `tokens[0]`,
#: `os.environ["GH_TOKEN"]`.
_ARGUMENTS = (
    r"(?:\(" + _ARG + r"{0,160}(?:\(" + _ARG + r"{0,160}\)" + _ARG + r"{0,160}){0,2}\)"
    r"|\[" + _NO_SECRET + r"(?:-?[0-9]{1,6}|[A-Za-z_][A-Za-z0-9_]{0,63}"
    r"|\"(?:" + _NO_SECRET + r"[^\"\n]){0,128}\"|'(?:" + _NO_SECRET + r"[^'\n]){0,128}')\])"
)
_CLOSED_CALL = (
    _CALLEE + _ARGUMENTS + r"(?=[\s,;]|$|[)\]}]+(?:[\s,;]|$)|" + _DOT + r"[a-z_])"
)
#: A shell substitution: `$(command ...)`, `${NAME}`, an empty `${NAME:-}` (a
#: default VALUE is a literal: `${TOKEN:-<v>}`), or a
#: GitHub Actions `${{ expression }}` -- never `$` and any character.
_SHELL_SUBSTITUTION = (
    r"\"?\$(?:\(" + _NO_SECRET + r"[a-z][a-z0-9_-]{0,31}[ )]"
    r"|\{" + _NO_SECRET + r"[A-Za-z_][A-Za-z0-9_]{0,63}:?-?\}|\{\{ )"
)
_EXPRESSION_PASSWORD = (
    r"(?-i:={1,2}[ \t]|(?:await|new|not)[ \t]"
    + r"|(?:None|True|False|null|undefined|\{\}|\[\]|\(\)|" + _DOTTED + r"|" + _KEYWORD_WORD + r")"
    + _END + r")"
)
_EXPRESSION = (
    r"(?-i:" + _EXPRESSION_PASSWORD + r"|" + _CLOSED_CALL + r"|" + _CREDENTIAL_NAME + _END + r")"
)
#: A PascalCase or dotted type is a type only where a type ends: before
#: ` = `, or ` | ` and another type (spaced, which no value this rule takes
#: can hold), a `)` that
#: closes a parameter list (then `->`, or a `:` or nothing to the end of the
#: line), or a `[` opening a generic of a name: `_token: _Token | None`,
#: `credential: Credential) -> str:`, `pattern: re.Pattern[str]`. At the end
#: of a line or before a comma it is as likely a YAML value -- `password:
#: MyDogRex`, `vault_token: s.<base62>` -- and stays masked (the #403
#: security review: these were served whatever followed them). The builtin
#: and typing names may end anywhere.
_TYPE_END = (
    r"(?=[ \t]+=[ \t]|[ \t]+\|[ \t]+(?:None|null|undefined|str|bytes|int|float|bool|[A-Z][a-z])"
    r"|\)[ \t]*(?:->|:?$)"
    r"|\[(?:str|bytes|int|float|bool|None|Any|object|[A-Z][a-z]{1,15}(?:[A-Z][a-z]{1,15}){0,3})[\],])"
)
_BUILTIN_TYPE = (
    r"(?-i:(?:str|bytes|int|float|bool|None|Any|object|string|number|boolean|unknown"
    r"|null|undefined|list|dict|tuple|set|frozenset|type|Path|Optional|Union|Sequence"
    r"|Mapping|Iterable|Iterator|Callable|Literal|Record|Array|Promise)"
    r"(?=[\s,;)\]}|=>\[]|$))"
)
_ANNOTATION = (
    r"(?-i:(?:_*(?:" + _HUMP + r")+"
    r"|[a-z_][a-z0-9_]{0,31}" + _DOT + r"(?:" + _HUMP + r")+)"
    + _TYPE_END + r")"
)
#: After `:` -- a type annotation (under `password`/`passwd` only a builtin
#: or typing name: `password: Summer)` is a value), a dotted name (a dict
#: literal's `"next_page_token": page.next_page_token`), the end of a shell
#: `${NAME:-}`, and, except under `password`/`passwd`, a closed call. Never
#: a bare word: `secret: missing_link_42` is YAML.
_COLON_VALUE_PASSWORD = (
    r"(?-i:" + _BUILTIN_TYPE + r"|" + _DOTTED + _END + r"|[-+=?]\}(?=[\"\s]|$))"
)
_COLON_VALUE = r"(?-i:" + _COLON_VALUE_PASSWORD + r"|" + _ANNOTATION + r"|" + _CLOSED_CALL + r")"
#: Not masked when the value starts with one of these (see above), and does
#: not itself start a secret run.
_NOT_A_LITERAL = (
    _NO_SECRET + r"(?:" + _SHELL_SUBSTITUTION
    + r"|(?(colon)(?(pw)" + _COLON_VALUE_PASSWORD + r"|" + _COLON_VALUE + r")"
    + r"|(?(lc)(?(pw)" + _EXPRESSION_PASSWORD + r"|" + _EXPRESSION + r")"
    r"|(?(sp)(?(pw)" + _EXPRESSION_PASSWORD + r"|" + _EXPRESSION + r")|(?!)))))"
)
KEY_VALUE = _rule(
    "key_value_assignment",
    "api[-_]?key",
    r"(?<![A-Za-z0-9_.-])"
    r"((?:\\{0,15}\")?(?P<lc>(?=(?-i:[A-Z0-9_.-]*[a-z])))?+[A-Za-z0-9_.-]*"
    r"(?:(?P<pw>password|passwd|passphrase)|api[-_]?key|apikey|private[-_]?key|secret|token|credential|authorization)"
    r"(?:(?P<plural>s)|(?P<keysuffix>s?[-_](?:access[-_]?key|key|hash)))?"
    r"(?:\\*\")?(?P<sp>[ \t]+)?+(?:(?P<colon>:)|=)[ \t]*(?!" + _NOT_A_LITERAL + r")"
    r"(?:\[[ \t]*)?(?:(?P<esc>\\+\")|\"?))"
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

#: A LITERAL AFTER CODE (the security pass, 2026-10-09; #361 box 64). The
#: exemptions above serve the SHAPE of code -- a call, a subscript, an
#: annotation -- whole, and the key/value rule masks one run, which stops at
#: the first quote or comma. So a literal riding after code on the same line
#: was served whatever its value: `api_key = os.getenv('API_KEY', '<v>')`,
#: `token = x["<v>"]`, `token: str = "<v>"`. This step masks such a literal
#: IN PLACE, before the key/value rule runs, so the code around it is still
#: code (a patch holding it still applies) and the literal is gone.
#:
#: Where: after a credential name and its `=` or `:`, inside the argument
#: list or subscript a name opens there (one nested `(...)` or `[...]`
#: allowed), or right after `<type> =` (an annotated default), or both
#: (`token: SecretStr = SecretStr("<v>")`). Not after a `,` at the top level:
#: in `{"token": null, "other": "v"}` the next key is not an argument.
#:
#: What: a quoted run of 6 to 128 characters with no space, quote,
#: backslash or any of `()[]{}<>|,;%` -- a value never holds a space, so
#: prose (`description="the database password"`) is not one, and a regular
#: expression or a format string (`re.compile(r"[a-z]+")`, `"%Y-%m-%d"`) is
#: code a served patch has to keep (#370). Under six characters a string is
#: as likely code as a value (`"utf-8"`, `"mask"`, `"rb"`), and the patch
#: that holds it has to apply; THAT IS THE RESIDUAL: a literal of five
#: characters or fewer after code is still served. Quoted NAMES are left: an environment
#: variable's name (`"GH_TOKEN"`), a letters-only name holding a credential
#: word (`"access_token"`, `"X-Api-Key"`), the empty string, and `********`.
#: Escaped quotes (`\"`) count as quotes, as a stream-json line writes them,
#: up to fifteen backslashes deep -- bounded, as `KEY_VALUE`'s run is, so a
#: long run of backslashes is not rescanned from each of its positions.
#:
#: HOW, IN BOTH FILTERS ALIKE. sed has no look-around and back-references
#: only `\1` to `\9`, so the step is four moves, and this function makes the
#: same four with the same expressions: (1) the quotes around every quoted
#: NAME become placeholders, so no later expression takes one for a
#: literal; (2) a marker goes after every credential name and its separator;
#: (3) a loop masks the first literal after each marker's code, turning its
#: quotes into placeholders too, until a pass masks nothing; (4) the markers
#: go and the placeholders turn back into quotes. Here the placeholders are
#: private-use characters; in the shell, control bytes it strips from its
#: input first.
#:
#: NOT A MEMBER OF `RULES`. The worker's publish guard walks `RULES` and
#: grades each by its name; this step is read-time masking only, and runs
#: wherever `redact` (and `TaskMasking.name`) apply the rules, immediately
#: before `KEY_VALUE`. The shell filter's copy is in `redact` in
#: scripts/lib/common.sh, held to this one by the parity fixture.
_CODE_SQ, _CODE_DQ, _CODE_MARK = "", "", ""
_CODE_QUOTED_NAME = (
    r"([A-Z_][A-Z0-9_]{0,63}"
    r"|[A-Za-z_-]{0,32}(?i:token|secret|key|passw|passphrase|credential|auth|api)[A-Za-z_-]{0,32}"
    r"|[*]{8})?"
)
_CODE_NAMES = (
    (re.compile(r"(\\{1,15})\"" + _CODE_QUOTED_NAME + r"(\\{1,15})\""), r"\1" + _CODE_DQ + r"\2\3" + _CODE_DQ),
    (re.compile(r"\"" + _CODE_QUOTED_NAME + r"\""), _CODE_DQ + r"\1" + _CODE_DQ),
    (re.compile(r"'" + _CODE_QUOTED_NAME + r"'"), _CODE_SQ + r"\1" + _CODE_SQ),
)
_CODE_NAME_MARK = re.compile(
    r"(^|[^A-Za-z0-9_.-])((\\{0,15}[\"" + _CODE_DQ + r"])?[A-Za-z0-9_.-]*(?i:(" + _KV_WORDS + r"))"
    r"(?i:s|s?[-_](?:access[-_]?key|key|hash))?(\\{0,15}[\"" + _CODE_DQ + r"])?[ \t]*[:=][ \t]*)",
    re.MULTILINE,
)
_CODE_CHAR = r"[A-Za-z0-9_.,:=*+/% \t\\" + _CODE_SQ + _CODE_DQ + r"-]"
#: The bounds are small on purpose: the shell's copy is compiled by sed
#: every time `redact` starts, and `{0,160}` of a group holding `{0,64}` cost
#: 0.25 s of compile per expression (measured 2026-10-09); this costs 0.03.
_CODE_ARG = r"(" + _CODE_CHAR + r"|\(" + _CODE_CHAR + r"{0,16}\)|\[" + _CODE_CHAR + r"{0,16}\])"
_CODE_OPEN = r"[A-Za-z_][A-Za-z0-9_.]{0,128}[(\[]" + _CODE_ARG + r"{0,64}"
_CODE_TYPED = r"[A-Za-z_][A-Za-z0-9_.,\[\]| ]{0,96}[ \t]*=[ \t]*"
_CODE = r"(" + _CODE_TYPED + r"(" + _CODE_OPEN + r")?|" + _CODE_OPEN + r")"
_CODE_VALUE = r"[^'\"\s\\()\[\]{}<>|,;%" + _CODE_SQ + _CODE_DQ + _CODE_MARK + r"]{6,128}"
_CODE_LITERALS = (
    (re.compile(r"(" + _CODE_MARK + _CODE + r")'" + _CODE_VALUE + r"'"),
     r"\1" + _CODE_SQ + MASK + _CODE_SQ),
    (re.compile(r"(" + _CODE_MARK + _CODE + r")\"" + _CODE_VALUE + r"\""),
     r"\1" + _CODE_DQ + MASK + _CODE_DQ),
    (re.compile(r"(" + _CODE_MARK + _CODE + r")(\\{1,15})\"" + _CODE_VALUE + r"(\\{1,15})\""),
     r"\1\6" + _CODE_DQ + MASK + r"\7" + _CODE_DQ),
)


def mask_code_literals(text: str) -> tuple[str, int]:
    """Every literal after code under a credential name, masked in place (box 64).

    Each pass masks the first literal after each name; a masked one is a
    quoted name to the next pass, so the loop ends when a pass finds none.
    """
    if any(c in text for c in (_CODE_SQ, _CODE_DQ, _CODE_MARK)):
        return text, 0  # a placeholder in the input would come out a quote
    if not _CODE_NAME_MARK.search(text):
        return text, 0
    for pattern, replacement in _CODE_NAMES:
        text = pattern.sub(replacement, text)
    text = _CODE_NAME_MARK.sub(r"\1\2" + _CODE_MARK, text)
    count = 0
    while True:
        found = 0
        for pattern, replacement in _CODE_LITERALS:
            text, n = pattern.subn(replacement, text)
            found += n
        if not found:
            break
        count += found
    text = text.replace(_CODE_MARK, "")
    return text.replace(_CODE_SQ, "'").replace(_CODE_DQ, '"'), count


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
#:
#: AN IDENTIFIER IS NEVER CUT MID-WORD (#370). `ey` and eight more characters
#: took `key_withheld` as `k` + a JWT, served `key_withhel********`, and broke
#: the patch holding it; `KeyboardInterrupt` and `KeychainStore` went the same
#: way, and the `sk-` rule cut `task-bbbbbbbb`. So both rules now start only
#: where a word starts -- after a character that is not a letter, digit or
#: `_` -- and a JWT must start `eyJ`, which every JWT does (it is base64 of
#: `{"`). A token GLUED onto a word is still masked when it is long enough
#: that no identifier is: `eyJ` and 37 more characters (dots counted -- a JWT
#: header alone can be 36), `sk-` and 32 more.
#: The shell filter states each as two expressions, since sed has no
#: look-around; tests/unit/control_plane/test_patch_artifact_applies.py holds
#: the four to one output (its values are assembled, so they are not in the
#: fixture file).
RULES: tuple[Rule, ...] = (
    _private_key_rule(),
    _rule(
        "openai_key",
        "sk-",
        r"(?:(?<![A-Za-z0-9_])|(?=sk-[A-Za-z0-9_-]{32}))(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+",
    ),
    _rule("google_oauth_access_token", "ya29", r"(ya29\.)[A-Za-z0-9._-]+"),
    # Any JWT: a Google ID token, an IAP assertion, a session cookie. This is
    # the one most likely to appear in a log this platform serves, because it
    # is what every caller of this very API holds.
    _rule(
        "jwt",
        "ey",
        r"(?:(?<![A-Za-z0-9_])|(?=eyJ[A-Za-z0-9._-]{37}))(eyJ[A-Za-z0-9_-]{7})[A-Za-z0-9._-]+",
    ),
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
        if rule is KEY_VALUE:
            text, found = mask_code_literals(text)
            count += found
        if rule.apply is not None:
            text, found = rule.apply(text, decoded, inside_key)
        else:
            text, found = rule.pattern.subn(r"\1" + MASK, text)
        count += found
    return Redacted(text=text, count=count)
