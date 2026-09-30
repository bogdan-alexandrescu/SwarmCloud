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
  * a private key written as a LIST of strings is masked from the element
    holding its BEGIN marker through the one holding its END, as
    `JsonMasker` does (`redaction._key_run_end`, restated here as a running
    state because a window is not a decoded list);
  * everything between tokens -- whitespace, commas, brackets -- is copied as
    stored. The text is still the stored document's layout.

`null`, `true` and `false` UNDER A CREDENTIAL'S NAME ARE SERVED AS THEY ARE.
None of them can be a credential, `_masks_whole` has always said so for a
decoded document, and masking one to `"********"` would tell a reader to
rotate a key nobody sent. `redact_lines` ran the text rules over such a line
as a "safety pass" (the PR #229 review), which is exactly what turned
`"api_key":null` into `********`. That pass is not run here: every place a
free-text credential can sit in JSON is a string, and every string is masked
decoded; a number is masked under a name the credential word ends, as the
text rule masks it. The pass could only ever add a mask to a literal.

WHICH WINDOWS COUNT AS JSON.

  * the whole window parses (`json.loads`), or every non-empty line of it
    does (JSONL: an agent's stream-json stdout); or
  * `fragment=True`: the caller KNOWS the window starts at the start of a line
    of the stored object and ends at the end of one (or at the object's end).
    In JSON text a line break never falls inside a string, so a window cut on
    line breaks is cut between tokens, and a page of a pretty-printed
    transcript tokenises cleanly although it does not parse. This is what
    lets `swarm artifact` concatenate 512 KiB pages into a file that parses.

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
their concatenation is still JSON. A value that opened further back than the
context reaches (`PEM_BLOCK_MAX_CHARS`) is not known about; its strings are
still masked by every rule, as they were before.

`inside_key`: the paged route's look-back found an open private key before
the window. Its first strings are masked as that key's lines, up to its END,
and when the window does not scan the text path masks it as it always has.

IMPORTS. `JsonMasker`, `Redacted`, `redact_lines`, `MASK` and
`PEM_BLOCK_MAX_CHARS` are public names of `redaction.py`. `_masks_whole`,
`_open_key`, `_CREDENTIAL_KEY` and `_PEM_END` are private ones, imported
rather than restated: the decision "is this value a credential" must be the
one `/input` and `/logs` make, and a second copy of it is how the two would
drift (the PR description says so too).
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring
from typing import Any, Iterable

from .redaction import (
    MASK,
    PEM_BLOCK_MAX_CHARS,
    JsonMasker,
    Redacted,
    _CREDENTIAL_KEY,
    _PEM_END,
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

    __slots__ = ("kind", "key", "has_str", "has_num", "watch", "run", "run_size")

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


def _structure(text: str, *, strict: bool) -> dict[int, tuple[bool, bool, int]]:
    """Every container under a credential's name: its start to `(has_str, has_num, end)`.

    Also checks the brackets pair up. `strict`: the window must be whole
    documents -- nothing closed that was not opened in it, nothing left open.
    A fragment may close what the text before it opened and leave open what
    the text after it closes; a container left open has no entry.
    """
    spans: dict[int, tuple[bool, bool, int]] = {}
    scanner = _Scanner(text)
    stack = [_Frame("root")]
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
                continue
            if frame.kind != ("{" if kind == "}" else "["):
                raise _NotJson("brackets that do not pair")
            stack.pop()
            if frame.watch >= 0:
                spans[frame.watch] = (frame.has_str, frame.has_num, end)
            parent = stack[-1]
            parent.has_str = parent.has_str or frame.has_str
            parent.has_num = parent.has_num or frame.has_num
        elif kind == "s":
            if scanner.is_key():
                frame.key = value
            else:
                frame.key = None
                if value.strip():
                    frame.has_str = True
        elif kind == "n":
            frame.key = None
            frame.has_num = True
        elif kind == "l":
            frame.key = None
    if strict and len(stack) > 1:
        raise _NotJson("a container left open")
    return spans


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


def _encode(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _walk(
    text: str,
    spans: dict[int, tuple[bool, bool, int]],
    masker: JsonMasker | None,
    *,
    emit_from: int,
    run_at_start: bool,
) -> Any:
    """Walk the tokens of `text`: learn (`masker` None) or mask.

    Learning returns what `JsonMasker` learns its literals from, as a list of
    the strings, keys and credential-named values in document order, so the
    literals are the ones a walk of the decoded document would learn. Masking
    returns the `Redacted` text of `text[emit_from:]`; `text[:emit_from]` is
    the context, walked and never served.
    """
    scanner = _Scanner(text)
    stack = [_Frame("root")]
    stack[0].run = run_at_start
    learned: list[Any] = []
    pieces: list[str] = []
    at = emit_from
    count = 0

    def put(start: int, end: int, new: str, counted: int) -> None:
        nonlocal at, count
        if end <= emit_from:
            return  # all of it is context
        if start >= emit_from:
            pieces.append(text[at:start])
            pieces.append(new)
            count += counted
        else:
            # A value the context opened: the page before this one served
            # its mask and counted it; nothing of it is served here.
            pieces.append(text[at:emit_from])
        at = end

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
            if masker is None:
                learned.append({value: None})
            else:
                name = masker.text(value)
                if name.count:
                    put(start, end, _encode(name.text), name.count)
            continue

        key, frame.key = frame.key, None
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
                if masker is None:
                    learned.append(value)
                else:
                    put(start, end, _MASKED, 0)
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
                    scanner.seek(stop)
                    continue
            stack.append(_Frame(kind))
            continue

        if key is not None and _masks_whole(key, value):
            if masker is None:
                learned.append({key: value})
            else:
                put(start, end, _MASKED, 1)
            continue
        if kind == "s":
            if masker is None:
                learned.append(value)
            else:
                masked = masker.text(value)
                if masked.count:
                    put(start, end, _encode(masked.text), masked.count)
            if frame.kind != "{" and _open_key(value):
                frame.run = True
                frame.run_size = 0

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
            spans = _structure(whole, strict=strict and not prefix)
            learned = _walk(whole, spans, None, emit_from=len(prefix), run_at_start=inside_key)
            masker = JsonMasker(learned, literals=literals)
            return _walk(whole, spans, masker, emit_from=len(prefix), run_at_start=inside_key)
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
