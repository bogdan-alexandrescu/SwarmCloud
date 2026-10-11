"""Read-time masking of a JSON artifact that leaves it JSON (#327).

WHAT BROKE. `/v1/tasks/{id}/artifacts/content` and `/artifacts/raw` masked a
text artifact with `redaction.redact_lines`, which decodes a line only when
the LINE is a whole JSON document. The worker writes `claude-transcript.json`
with `json.dumps(indent=2)`, so no line of it is one, and every line went
through the text rules as raw JSON text. The #323 measurement found 10 of 47
served transcripts no longer parsed. The chains that took #327 measured four
ways it happens:

  1. a value closed by an escaped quote. In `echo \\"PASSWORD=<v>\\"` the
     key/value rule's value class (a value NOT opened by `\\"` keeps its
     backslashes) takes `<v>\\` and leaves a bare `"` inside the string;
  2. a non-string value under a credential's name. `"password": 1234...`
     became `"password": ********`, and `"api_key": null` bare `********`,
     neither of them JSON; a list lost its `[`;
  3. a private key held in ONE string, its newlines written `\\n`. The block
     mask ran from the BEGIN marker across the string's closing quote, with or
     without an END marker;
  4. A LEAK, not only a corruption: a value the task named as secret (a
     learned literal, `task_input.masking_for(task).literals`) that holds a
     quote, a newline or a non-ASCII character is written ESCAPED in JSON
     text (`\\"`, `\\n`, `\\u00e9`), so the text path's plain substring search
     never found it and served it in clear.

WHAT THIS DOES INSTEAD. A window that is JSON is masked by its TOKENS:

  * every string -- value or key -- is DECODED, masked by the same rules and
    the same learned literals `JsonMasker` masks a decoded document with
    (`redact(decoded=True)` then the literals), and RE-ENCODED only when that
    changed it. A string nothing matched keeps its exact bytes, escapes and
    all, so a clean transcript is served byte for byte, `redacted: false`;
  * a value under a key that names a credential is replaced WHOLE by the JSON
    string `"********"` -- a string, a number, a list or an object alike, by
    `redaction._masks_whole`, the rule `JsonMasker` uses, so a list or an
    object is one mask and one count, never served element by element;
  * a NUMBER that holds one of the learned literals -- the number itself,
    inside a longer one, `N.0`, `Ne0` -- is replaced by `"********"` (the
    PR #378 review): a PIN the task named, echoed by the agent as a number,
    was served in clear because only strings were compared. The rule is
    `JsonMasker.number`, the one every other route masks a decoded number
    with (#387), not a copy of it here;
  * a private key split over several strings is masked from the string
    holding its BEGIN marker through the one holding its END, whatever
    containers those strings sit in -- a flat list, an object's values, a
    list with a number in it, nested lists, a list of objects (the PR #378
    review), an object KEY, a value masked whole under a credential's name
    (the re-review) -- when the END is within `PEM_BLOCK_MAX_CHARS` of string
    content, as the text path masks a block; a KEY inside that span is masked
    too (#385: a PEM pasted into a .properties file stores its body lines as
    keys). With no END in reach -- missing, or beyond the page the reader
    chose (#385) -- a key written as a flat LIST of strings is masked to the
    end of that run of strings, as `JsonMasker` does (`redaction._key_run_end`,
    restated here as a running state because a window is not a decoded list),
    and the key is ALSO followed in document order across containers by
    `JsonMasker`'s own rule (`redaction._KeyRun`, imported): every
    body-shaped string value and key after the BEGIN is masked, counted 0;
    blank strings, keys that are not body-shaped, numbers and literals
    neither join nor end it; an END string ends it, masked; the first string
    value that is not body-shaped (`"after the key"`, `"kept"`) ends it and is
    served, as the text path stops at the first line not shaped like a key's
    body; and `PEM_BLOCK_MAX_CHARS` of string content ends it. Body-shaped is
    `redaction._key_body_string`, with its `KEY_BODY_MIN_CHARS` floor and the
    residual its docstring states (a final unpadded line under the floor is
    served);
  * everything between tokens -- whitespace, commas, brackets -- is copied as
    stored. The text is still the stored document's layout.

`null`, `true` and `false` UNDER A CREDENTIAL'S NAME ARE SERVED AS THEY ARE.
None of them can be a credential, `_masks_whole` has always said so for a
decoded document, and masking one to `"********"` would tell a reader to
rotate a key nobody sent (owner decision, 2026-09-30). `redact_lines` ran
the text rules over such a line as a "safety pass" (the PR #229 review),
which is exactly what turned `"api_key":null` into `********`. That pass is
not run here, and what it caught is caught instead by the tokens: every
place a free-text credential can sit in JSON is a string, and every string
is masked decoded; a number is masked under a name the credential word
ends or is followed only by `_key`, `_access_key` or `_hash` (`SECRET_KEY`,
`PASSWORD_HASH` -- `_masks_whole`, widened for every route at once rather
than here alone); and a number holding a learned literal is masked as
above. The PR #378 review found the last two missing, which is what the
pass had been catching besides literals; an earlier version of this
paragraph said the pass could only ever add a mask to a literal, and was
wrong.

WHICH WINDOWS COUNT AS JSON.

  * the whole window parses (`json.loads`), or every non-empty line of it
    does (JSONL: an agent's stream-json stdout); or
  * `fragment=True`: the caller KNOWS the window starts at the start of a line
    of the stored object and ends at the end of one (or at the object's end).
    In JSON text a line break never falls inside a string, so a window cut on
    line breaks is cut between tokens, and a page of a pretty-printed
    transcript tokenises cleanly although it does not parse. This is what
    lets `swarm artifact` concatenate 512 KiB pages into a file that parses.

A FRAGMENT'S GRAMMAR IS CHECKED, not only its tokens (the PR #378 review). A
string is a key only where a key may stand -- right after `{` or `,` inside
an object, or, in a fragment, at the top level where the object that holds
it opened before the window -- and it must be followed by `:`; a `:`
anywhere else is not JSON. Without that, `"password": "<v>": 1` read `<v>`
as a key and served it. Such a window goes to `redact_lines`.

Anything else -- plain text, a Markdown file, a window cut mid-line, a window
whose tokens do not scan, one over `JSON_WINDOW_MAX_CHARS` -- goes through
`redact_lines` exactly as before.

A PAGE KNOWS WHAT CAME BEFORE IT (`context`). A value under a credential's
name, or a private key written as a list, can open on one page and close on
the next. The page that sees it open masks it to its own end (a container
that crosses the window's edge is masked whole whenever its key names a
credential -- it cannot see the rest, so it assumes the worst). The next page
is handed the text before it, from a line start (the look-back the paged
route already reads for `_enter_key`, and the raw download's previous
window), walks that too, and so knows it begins INSIDE such a value: it
serves nothing of it up to its close, masks the rest of a key's lines, and
learns the literals the text before it named. The two pages' edges agree, so
their concatenation is still JSON. EACH page counts what it hid: a page
that serves nothing of a value its context opened reports it masked, once,
rather than `redacted: false` over text it withheld (the PR #378 review),
so the pages' counts sum to more than a whole read's. A value that opened
further back than the context reaches (`PEM_BLOCK_MAX_CHARS`) is not known
about; its strings are still masked by every rule, as they were before.

`inside_key`: the paged route's look-back found an open private key before
the window. Its first strings are masked as that key's lines, up to its END,
and when the window does not scan the text path masks it as it always has.
It opens the no-END run at the start of what is walked, so a page wholly
inside a key in an object or a list of objects masks its body lines whether
or not the context reaches the BEGIN; when it does, the context's walk opens
the run there itself (#385). Either way the page counts the key once, for
the lines it withheld, never once per line and never over clear text.

A LONE SURROGATE (`\\ud800` written escaped in the stored text) decodes to a
code point UTF-8 cannot encode. A string that held one and was masked is
re-encoded with it escaped again, never raw, so serving it cannot raise.

IMPORTS. `JsonMasker`, `Redacted`, `redact_lines`, `MASK` and
`PEM_BLOCK_MAX_CHARS` are public names of `redaction.py`. `_masks_whole`,
`_open_key`, `_CREDENTIAL_KEY`, `_PEM_END`, `_PEM_HINT` and `_KeyRun` (which
holds `_key_body_string` and `KEY_BODY_MIN_CHARS`) are private ones, imported
rather than restated: the decision "is this value a credential" must be the
one `/input` and `/logs` make, and a second copy of it is how the two would
drift (the PR description says so too). A number holding a learned
literal is decided by `JsonMasker.number` for the same reason: this module
kept its own copy of that rule until #387 found `/input` and `/logs` serving
in clear what the copy masked here.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from json.decoder import scanstring
from typing import Any, Iterable

from .redaction import (
    MASK,
    PEM_BLOCK_MAX_CHARS,
    JsonMasker,
    Redacted,
    _CREDENTIAL_KEY,
    _PEM_END,
    _PEM_HINT,
    _KeyRun,
    _masks_whole,
    _open_key,
    redact_lines,
)

#: The largest window, context included, masked by its tokens. Above it the
#: text path runs. The content route serves at most 4 MiB and the raw
#: download reads 1 MiB windows (a whitespace-free run may carry to 4 MiB),
#: so every window either route serves today is under it; the bound is what
#: stops one request buying a `json.loads` and three scans of an unbounded
#: text on an instance every tenant shares.
JSON_WINDOW_MAX_CHARS = 8 * 1024 * 1024

#: How much text before a window is walked as its context: the reach a
#: private key's BEGIN has to its END, which is also how far back the paged
#: route already reads (`inspect.KEY_LOOKBACK_BYTES`).
JSON_CONTEXT_MAX_BYTES = PEM_BLOCK_MAX_CHARS

#: Nesting deeper than this is not scanned: the text path masks it. No agent
#: CLI writes it, and the frame stack is the one allocation that grows with
#: it.
_MAX_DEPTH = 512

_MASKED = json.dumps(MASK)
_WHITESPACE = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_WORDS = (("true", True), ("false", False), ("null", None))
#: What may follow a number or a literal: `12ab` or `nullx` is not JSON.
_AFTER_SCALAR = frozenset(" \t\r\n,:]}")
#: A surrogate code point: a lone one decodes out of a `\\ud800` escape and
#: cannot be written as UTF-8, so a re-encoded string escapes it again.
_SURROGATE = re.compile("[\ud800-\udfff]")


class _NotJson(Exception):
    """The window does not scan as JSON tokens: the text path masks it."""


def _token(text: str, pos: int) -> tuple[str, int, int, Any] | None:
    """The token at or after `pos` as `(kind, start, end, value)`, or None at the end.

    `kind` is the punctuation character itself, `s` for a string (its value
    decoded), `n` for a number (its value), `l` for true/false/null.
    """
    pos = _WHITESPACE.match(text, pos).end()
    if pos >= len(text):
        return None
    char = text[pos]
    if char in "{}[],:":
        return char, pos, pos + 1, None
    if char == '"':
        try:
            value, end = scanstring(text, pos + 1, True)
        except ValueError:
            raise _NotJson("an unterminated or invalid string") from None
        return "s", pos, end, value
    found = _NUMBER.match(text, pos)
    if found is not None:
        end = found.end()
        if end < len(text) and text[end] not in _AFTER_SCALAR:
            raise _NotJson("a number followed by a letter")
        try:
            number = json.loads(found.group(0))
        except ValueError:  # an integer past Python's digit limit
            raise _NotJson("a number Python will not read") from None
        return "n", pos, end, number
    for word, value in _WORDS:
        if text.startswith(word, pos):
            end = pos + len(word)
            if end < len(text) and text[end] not in _AFTER_SCALAR:
                raise _NotJson("a word that is not a JSON literal")
            return "l", pos, end, value
    raise _NotJson("a character no JSON token starts with")


class _Scanner:
    """`_token` in order, with one token of look-ahead and a jump."""

    __slots__ = ("text", "pos", "_ahead")

    def __init__(self, text: str, pos: int = 0) -> None:
        self.text = text
        self.pos = pos
        self._ahead: tuple[str, int, int, Any] | None = None

    def next(self) -> tuple[str, int, int, Any] | None:
        token = self._ahead if self._ahead is not None else _token(self.text, self.pos)
        self._ahead = None
        if token is not None:
            self.pos = token[2]
        return token

    def is_key(self) -> bool:
        """Whether the string just read is a key: the next token is `:`."""
        if self._ahead is None:
            self._ahead = _token(self.text, self.pos)
        return self._ahead is not None and self._ahead[0] == ":"

    def seek(self, pos: int) -> None:
        self.pos = pos
        self._ahead = None


class _Frame:
    """One open container while scanning: `root` for the window's top level."""

    __slots__ = ("kind", "key", "has_str", "has_num", "watch", "run", "run_size", "run_open")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        #: The key the next value sits under, in an object.
        self.key: str | None = None
        #: Whether the container holds a non-blank string VALUE, or a number,
        #: at any depth: what `_masks_whole` asks of a list or an object.
        self.has_str = False
        self.has_num = False
        #: Where the container starts, when its key names a credential.
        self.watch = -1
        #: Inside a private key written as a list of strings (`_key_run_end`).
        self.run = False
        self.run_size = 0
        #: Where the string that opened `run` starts (-1: before the window).
        self.run_open = -1


#: What `_structure` finds: every container under a credential's name, its
#: start to `(has_str, has_num, end)`; and every string that opens a private
#: key whose END another string holds within reach, its start to that
#: string's start.
_Spans = dict[int, tuple[bool, bool, int]]
_Runs = dict[int, int]


def _structure(text: str, *, strict: bool) -> tuple[_Spans, _Runs]:
    """The containers under a credential's name, and the private keys split over strings.

    Also checks the window is JSON: the brackets pair up, and a string is a
    key only where a key may stand -- right after `{` or `,` in an object,
    or, in a fragment, at the top level, where an object may have opened
    before the window -- and is then followed by `:`, and a `:` follows
    nothing else (the PR #378 review: `"password": "<v>": 1` read `<v>` as a
    key and served it). `strict`: the window must be whole documents --
    nothing closed that was not opened in it, nothing left open, no key at
    the top level. A fragment may close what the text before it opened and
    leave open what the text after it closes; a container left open has no
    entry.

    A KEY SPLIT OVER STRINGS (the PR #378 review and re-review). A string
    -- a value OR an object key -- that leaves a private key open
    (`_open_key`) runs to the next string, value or key, holding an END
    marker, whatever containers either sits in -- `{"l1": BEGIN, "l2":
    body}`, `[BEGIN, 1, body, END]`, `[[BEGIN], [body]]`, `[{"line": BEGIN},
    ...]`, `{BEGIN: body, ...}`, and a BEGIN inside a value masked whole
    under a credential's name (`{"secret": [BEGIN], "x": body, ...}`) --
    when the string VALUE content between them is within
    `PEM_BLOCK_MAX_CHARS`, the reach the text path gives a block. One pass:
    every open waits in `pending` for the next END, so the work is linear
    however many BEGIN markers a window holds.

    An open with no END in reach has no entry: `_walk` follows it itself
    (#385), and a string `runs` covers is not also a no-END run's material,
    so no line is counted by both.
    """
    spans: _Spans = {}
    runs: _Runs = {}
    pending: list[tuple[int, int]] = []  # (the open string's start, `size` after it)
    size = 0  # string VALUE content read so far, each plus one, as `_key_run_end` counts
    scanner = _Scanner(text)
    stack = [_Frame("root")]
    before: str | None = None  # the previous token's kind; `k` for a key
    while True:
        token = scanner.next()
        if token is None:
            break
        kind, start, end, value = token
        frame = stack[-1]
        if kind in ("{", "["):
            key, frame.key = frame.key, None
            child = _Frame(kind)
            if key is not None and _CREDENTIAL_KEY.search(key) is not None:
                child.watch = start
            stack.append(child)
            if len(stack) > _MAX_DEPTH:
                raise _NotJson("nested deeper than the scan goes")
        elif kind in ("}", "]"):
            frame.key = None
            if len(stack) == 1:
                if strict:
                    raise _NotJson("a close with no open")
            else:
                if frame.kind != ("{" if kind == "}" else "["):
                    raise _NotJson("brackets that do not pair")
                stack.pop()
                if frame.watch >= 0:
                    spans[frame.watch] = (frame.has_str, frame.has_num, end)
                parent = stack[-1]
                parent.has_str = parent.has_str or frame.has_str
                parent.has_num = parent.has_num or frame.has_num
        elif kind == ":":
            if before != "k":
                raise _NotJson("a colon after something that is not a key")
        elif kind == "s":
            follows = scanner.is_key()
            member = frame.kind == "{" or (frame.kind == "root" and not strict)
            where_a_key_stands = member and before in ("{", ",", None)
            if follows and not where_a_key_stands:
                raise _NotJson("a value followed by a colon")
            if frame.kind == "{" and before in ("{", ",") and not follows:
                raise _NotJson("an object member with no key")
            if pending and _PEM_HINT in value and _PEM_END.search(value) is not None:
                for opened, after in pending:
                    if size - after <= PEM_BLOCK_MAX_CHARS:
                        runs[opened] = start
                pending = []
            if follows:
                frame.key = value
                kind = "k"
            else:
                frame.key = None
                if value.strip():
                    frame.has_str = True
                size += len(value) + 1
            if _open_key(value):
                pending.append((start, size))
        elif kind == "n":
            frame.key = None
            frame.has_num = True
        elif kind == "l":
            frame.key = None
        before = kind
    if strict and len(stack) > 1:
        raise _NotJson("a container left open")
    return spans, runs


def _leaves(text: str, start: int, stop: int) -> list[Any]:
    """Every string value and number between `start` and `stop`: `_leaf_texts`, from tokens."""
    scanner = _Scanner(text, start)
    found: list[Any] = []
    while True:
        token = scanner.next()
        if token is None or token[1] >= stop:
            return found
        if token[0] == "s" and not scanner.is_key():
            found.append(token[3])
        elif token[0] == "n":
            found.append(token[3])


def _scan_masked(run: _KeyRun, text: str, start: int, stop: int) -> bool:
    """`run.scan` over the value masked whole at `text[start:stop]`; whether a key opens in it.

    A fragment page is read by the tolerant `_Scanner`, so the value need not
    be JSON (`[1, 2, ]`): `json.loads` raised there and the page answered 500
    where it had been served masked (the #385 review). It is then rebuilt
    from the scanner's own tokens, so `scan` reads it as it reads a decoded
    value, flat lists included.
    """
    try:
        inner = json.loads(text[start:stop])
    except ValueError:
        inner = _tolerant_value(text, start, stop)
    run.scan(inner)
    return any(_open_key(string) for string in _strings_in(inner))


def _tolerant_value(text: str, start: int, stop: int) -> Any:
    """The container at `text[start:stop]` built from `_Scanner` tokens.

    `_structure` has already paired its brackets; a stray or missing comma is
    what `json.loads` refused, and it is skipped here. So is a missing KEY:
    `_structure` lets a member with no key of its own pass in an object
    (`{1}`, `{"a": 1, 2}`, `{"a": "x" "y"}`). Filed under `None`, the scan
    read `None` as a string and the page answered 500; filed under the last
    key, it erased that key's value, and a BEGIN with it. Each is filed under
    a stand-in key of its own (`_keyless`), which no rule reads as anything.
    """
    scanner = _Scanner(text, start)
    stack: list[Any] = []
    key: list[str | None] = []
    root: Any = None
    while True:
        token = scanner.next()
        if token is None or token[1] >= stop:
            return root
        kind = token[0]
        if kind in ",:":
            continue
        if kind in "}]":
            stack.pop()
            key.pop()
            continue
        if kind == "s" and stack and isinstance(stack[-1], dict) and scanner.is_key():
            key[-1] = token[3]
            continue
        value: Any = {} if kind == "{" else [] if kind == "[" else token[3]
        if not stack:
            root = value
        elif isinstance(stack[-1], dict):
            stack[-1][_keyless(stack[-1]) if key[-1] is None else key[-1]] = value
            key[-1] = None
        else:
            stack[-1].append(value)
        if kind in "{[":
            stack.append(value)
            key.append(None)


def _keyless(members: dict[str, Any]) -> str:
    """A key for an object member written with none, unused in `members`.

    A NUL then a number: never base64, an RFC 1421 header, a marker or a
    credential's name, so the no-END run steps over it as over any key that
    is not body-shaped, and nothing is masked or counted for it.
    """
    at = len(members)
    while f"\x00{at}" in members:
        at += 1
    return f"\x00{at}"


def _strings_in(node: Any) -> Iterable[str]:
    """Every string and key under a decoded `node`."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, item in node.items():
            yield key
            yield from _strings_in(item)
    elif isinstance(node, list):
        for item in node:
            yield from _strings_in(item)


def _encode(value: str) -> str:
    """`value` as a JSON string, non-ASCII kept as stored, a lone surrogate escaped.

    `ensure_ascii=False` writes a lone surrogate (decoded from a `\\ud800`
    escape) raw, and no route can encode that as UTF-8: the content route
    answered 500 (the PR #378 review). It is written as the escape it was.
    """
    encoded = json.dumps(value, ensure_ascii=False)
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", encoded)


def _walk(
    text: str,
    spans: _Spans,
    runs: _Runs,
    masker: JsonMasker | None,
    *,
    emit_from: int,
    run_at_start: bool,
) -> Any:
    """Walk the tokens of `text`: learn (`masker` None) or mask.

    Learning returns what `JsonMasker` learns its literals from, as a list of
    the strings, keys and credential-named values in document order, so the
    literals are the ones a walk of the decoded document would learn. A
    string or key a key run masks is learned as it is stored, as
    `_learned_literals` reads it. Masking returns the `Redacted` text of
    `text[emit_from:]`; `text[:emit_from]` is the context, walked and never
    served.

    THREE RUNS mask a private key split over strings, checked in this order
    for each string, so one string is masked by one of them and counted
    once: the END-in-reach span from `_structure` (`run_stop`), the flat
    list (`_Frame.run`), and the no-END run across containers and keys
    (`shape_run`, #385). The first two feed the third (`_KeyRun.through`),
    so it carries on past a flat list that ended without its END and
    closes at an END either of them reached. `run_at_start` (`inside_key`)
    opens the root's flat-list run and the no-END run before the first
    token.
    """
    scanner = _Scanner(text)
    stack = [_Frame("root")]
    stack[0].run = run_at_start
    learned: list[Any] = []
    pieces: list[str] = []
    at = emit_from
    count = 0
    # The key split over strings being masked (`runs`): through the string
    # starting at `run_stop`, opened by the one starting at `run_open`.
    run_stop = -1
    run_open = -1
    # Values opened before the window that this page has already counted.
    charged: set[int] = set()
    # Every string that opens a run, in order: for a BEGIN a jump skips.
    opens = sorted(runs)
    # A key with no END in reach (#385), followed in document order across
    # containers and keys by the rule `JsonMasker` follows it with
    # (`redaction._KeyRun`), opened by the string starting at `shape_open`
    # (-1: before the look-back, `run_at_start`).
    shape_run = _KeyRun()
    shape_open = -1
    if run_at_start:
        shape_run.open = True

    def shaped(value: str, start: int, *, key: bool = False) -> bool:
        """`_KeyRun.step`: whether `value` is the open key's material."""
        nonlocal shape_open
        hit = shape_run.step(value, key=key)
        if _open_key(value):
            shape_open = start
        return hit

    def through(value: str, start: int) -> None:
        """A string the END-in-reach or flat-list rule masked: `_KeyRun.through`."""
        nonlocal shape_open
        shape_run.through(value)
        if _open_key(value):
            shape_open = start

    def put(start: int, end: int, new: str, counted: int, opened: int | None = None) -> None:
        nonlocal at, count
        if end <= emit_from:
            return  # all of it is context
        if start >= emit_from:
            pieces.append(text[at:start])
            pieces.append(new)
            count += counted
            if not counted and opened is not None and opened < emit_from and opened not in charged:
                # Key material of a key opened before the window (or, at -1,
                # before the look-back): the page that showed its BEGIN
                # counted it there, and THIS page, which masks its lines, says
                # so too rather than `redacted: false` (the PR #378 review).
                charged.add(opened)
                count += 1
        else:
            # A value the context opened: the page before this one served
            # its mask and counted it. Nothing of it is served here, and this
            # page counts it too: it withheld text (the PR #378 review).
            pieces.append(text[at:emit_from])
            count += counted
        at = end

    def open_run(value: str, start: int, frame: _Frame) -> None:
        """A string value that leaves a private key open starts a run."""
        nonlocal run_stop, run_open
        if not _open_key(value):
            return
        stop = runs.get(start)
        if stop is not None:
            run_stop, run_open = stop, start
        elif frame.kind != "{":
            # No END in reach: a key written as a flat list runs to the end
            # of its run of strings, as `JsonMasker` masks it. Past that run,
            # and in an object, `shape_run` carries the key on (#385).
            frame.run = True
            frame.run_size = 0
            frame.run_open = start

    while True:
        token = scanner.next()
        if token is None:
            break
        kind, start, end, value = token
        frame = stack[-1]
        if kind in (",", ":"):
            continue
        if kind in ("}", "]"):
            frame.key = None
            if len(stack) > 1:
                stack.pop()
            else:
                frame.run = False
            continue
        if kind == "s" and scanner.is_key():
            frame.run = False  # an object: not a list of a key's lines
            frame.key = value
            # A body line stored as a KEY (`{"PRIVATE_KEY": BEGIN, "<b64>": "",
            # ..., END: ""}`, #385): masked like a value when it lies within
            # a run whose END is in reach, or is body-shaped inside a run
            # whose END is not. A colliding key is still JSON: the stored
            # text never promised unique keys.
            if start <= run_stop:
                through(value, start)
                opened: int | None = run_open
            else:
                opened = shape_open if shaped(value, start, key=True) else None
            if masker is None:
                learned.append({value: None})
            elif opened is not None:
                put(start, end, _MASKED, 0, opened)
            else:
                name = masker.text(value)
                if name.count:
                    put(start, end, _encode(name.text), name.count)
            # A key holding a BEGIN marker (`{BEGIN: body, ...}`): its body
            # is in the values after it (the PR #378 re-review).
            if _open_key(value) and start in runs:
                run_stop, run_open = max(run_stop, runs[start]), start
            continue

        key, frame.key = frame.key, None
        if kind == "s" and start <= run_stop:
            # Key material of the key a string above opened, in whatever
            # container: its one mask was counted where its BEGIN was.
            through(value, start)
            if masker is None:
                learned.append(value)
            else:
                put(start, end, _MASKED, 0, run_open)
            continue
        if frame.run:
            if kind != "s":
                frame.run = False
            else:
                # Key material of the block an element above opened: its one
                # mask was counted there (`JsonMasker._walk`).
                if frame.run_size <= PEM_BLOCK_MAX_CHARS and _PEM_END.search(value) is not None:
                    frame.run = False
                else:
                    frame.run_size += len(value) + 1
                through(value, start)
                if masker is None:
                    learned.append(value)
                else:
                    put(start, end, _MASKED, 0, frame.run_open)
                continue

        if kind in ("{", "["):
            span = spans.get(start)
            close = span[2] if span is not None else None
            if key is not None:
                if close is None or start < emit_from < close:
                    # It crosses the window's edge, so the rest of it is not
                    # visible here: masked whole whenever its key names a
                    # credential, on both sides of the edge.
                    whole = _masks_whole(key, ["x"])
                else:
                    has_str, has_num = span[0], span[1]
                    whole = _masks_whole(key, ["x"] if has_str else ([0] if has_num else []))
                if whole:
                    stop = len(text) if close is None else close
                    if masker is None:
                        learned.append({key: _leaves(text, start, stop)})
                    else:
                        put(start, stop, _MASKED, 1)
                    # A BEGIN inside the masked value whose END lies after it
                    # (`{"secret": [BEGIN], "x": body, ...}`): the jump would
                    # skip the string that starts the run, so the run is
                    # taken from the LAST opening string inside -- the one
                    # whose END is furthest, since each open runs to the next
                    # END after it (the PR #378 re-review).
                    inside = bisect_left(opens, stop) - 1
                    if inside >= 0 and opens[inside] >= start and runs[opens[inside]] > run_stop:
                        run_stop, run_open = runs[opens[inside]], opens[inside]
                    if close is not None:
                        # Unread, but its strings still move the no-END run,
                        # as `JsonMasker` scans a value it masks whole: a
                        # BEGIN inside opens it, and the body after it is
                        # masked. One past the window's end is never read.
                        if _scan_masked(shape_run, text, start, close):
                            shape_open = start
                    scanner.seek(stop)
                    continue
            stack.append(_Frame(kind))
            continue

        if key is not None and _masks_whole(key, value):
            if masker is None:
                learned.append({key: value})
            else:
                put(start, end, _MASKED, 1)
            if kind == "s":
                shaped(value, start)
                open_run(value, start, frame)
            continue
        if kind == "s":
            if shaped(value, start):
                # A body-shaped string after a key with no END in reach, in
                # whatever container (#385): its one mask was counted at the
                # BEGIN, and a page that only sees the body counts it once.
                if masker is None:
                    learned.append(value)
                else:
                    put(start, end, _MASKED, 0, shape_open)
                continue
            if masker is None:
                learned.append(value)
            else:
                masked = masker.text(value)
                if masked.count:
                    put(start, end, _encode(masked.text), masked.count)
            open_run(value, start, frame)
        elif kind == "n" and masker is not None:
            # A learned literal made of digits, written as a NUMBER -- as
            # itself, inside a longer number, `N.0`, `Ne0` -- is masked as a
            # string is (the PR #378 review); the number becomes the JSON
            # string `"********"`, as a number under a credential's name does.
            # `JsonMasker.number` is the rule every route uses (#387).
            found = masker.number(text[start:end])
            if found:
                put(start, end, _MASKED, found)

    if masker is None:
        return learned
    pieces.append(text[at:])
    return Redacted(text="".join(pieces), count=count)


def _parses(text: str) -> bool:
    """Whether `text` is one JSON document, or JSONL: every non-empty line one."""
    try:
        json.loads(text)
        return True
    except (ValueError, RecursionError):
        pass
    lines = [line for line in text.split("\n") if line.strip()]
    if len(lines) < 2:
        return False
    for line in lines:
        try:
            json.loads(line)
        except (ValueError, RecursionError):
            return False
    return True


def _token_mask(
    text: str, literals: tuple[str, ...], *, strict: bool, inside_key: bool, context: str
) -> Redacted | None:
    """`text` masked by its tokens, with `context` walked first; None when it does not scan."""
    for prefix in (context, "") if context else ("",):
        whole = prefix + text
        try:
            spans, runs = _structure(whole, strict=strict and not prefix)
            learned = _walk(whole, spans, runs, None, emit_from=len(prefix), run_at_start=inside_key)
            masker = JsonMasker(learned, literals=literals)
            return _walk(whole, spans, runs, masker, emit_from=len(prefix), run_at_start=inside_key)
        except (_NotJson, RecursionError):
            # A context that does not scan is dropped, and the window tried
            # alone; a window that does not scan is the text path's.
            continue
    return None


def redact_json_window(
    text: str,
    *,
    literals: Iterable[str] = (),
    inside_key: bool = False,
    fragment: bool = False,
    context: str = "",
) -> Redacted:
    """One window of an artifact, masked so that JSON stays JSON; other text as before.

    `literals`: values the task named as secret (`TaskMasking.literals`).
    `inside_key`: the look-back found an open private key before the window.
    `fragment`: the window starts and ends on a line boundary of the stored
    object, so it may be scanned as a page of a JSON document. `context`: the
    text before the window, from a line start (at most
    `JSON_CONTEXT_MAX_BYTES`), only read when `fragment`. The module's
    docstring says why each exists.
    """
    literals = tuple(literals)
    if text.strip() and len(context) + len(text) <= JSON_WINDOW_MAX_CHARS:
        strict = _parses(text)
        if strict or fragment:
            masked = _token_mask(
                text,
                literals,
                strict=strict,
                inside_key=inside_key,
                context=context if fragment else "",
            )
            if masked is not None:
                return masked
    return redact_lines(text, inside_key=inside_key, literals=literals)


def context_before(data: bytes, *, starts_on_line: bool) -> bytes:
    """The last `JSON_CONTEXT_MAX_BYTES` of `data`, from the start of a line.

    `data` is what precedes a window. `starts_on_line`: `data[0]` begins a
    line of the stored object (the object's first byte, or the byte after a
    line break). Otherwise, and whenever `data` is cut to the bound, the
    context starts after the first line break in what is kept: a context that
    began mid-line could begin inside a string, and would be scanned out of
    step with the tokens.
    """
    kept = data[-JSON_CONTEXT_MAX_BYTES:] if JSON_CONTEXT_MAX_BYTES else b""
    if starts_on_line and len(kept) == len(data):
        return kept
    newline = kept.find(b"\n")
    return b"" if newline < 0 else kept[newline + 1 :]
