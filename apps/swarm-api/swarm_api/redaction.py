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

THE RULES THEMSELVES LIVE IN `swarm_redaction` (owner decision 4, 2026-09-28)
---------------------------------------------------------------------------
`MASK`, `Rule`, `RULES`, `KEY_VALUE`, the private-key block and `redact()`
moved to `apps/redaction/swarm_redaction/rules.py`, so the worker's
per-commit secret scan matches the same patterns this API masks. They are
re-exported below, so every `from swarm_api.redaction import ...` still works.
Their relationship to `redact()` in `scripts/lib/common.sh` is documented
there. What stays here is what only an API serving a document needs: masking a
JSON document by its structure (`JsonMasker`), a log by its lines
(`redact_lines`), and an error's detail (`redact_detail`).
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any, Callable, Iterable

# Re-exported: the names that moved. `_PEM_*` are read by the JSON masker.
from swarm_redaction.rules import (  # noqa: F401 - re-exported
    KEY_VALUE,
    MASK,
    PEM_BLOCK_MAX_CHARS,
    RULES,
    Redacted,
    Rule,
    _NUMBERED,
    _PEM_BEGIN,
    _PEM_END,
    _PEM_HINT,
    _body_line,
    mask_private_keys,
    open_key_start,
    redact,
)


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
#   * a private key whose END is not in that list -- missing, or the body split
#     across object members, a list of objects, nested lists, or the object's
#     KEYS (#385) -- is followed in DOCUMENT ORDER across containers
#     (`_KeyRun`): from the string or key that leaves a key open, every
#     body-shaped string value and key (`_key_body_string`) is masked and
#     counted 0, the BEGIN carrying the key's one count; blank strings, keys
#     that are not body-shaped, numbers, booleans and null neither join nor end
#     it; an END string ends it, masked; the first string VALUE that is not
#     body-shaped ends it and is served as the other rules mask it, as the
#     text path stops at the first line not shaped like a key's body; and
#     `PEM_BLOCK_MAX_CHARS` of string content after the BEGIN ends it, the
#     bound a block of text has. What the run masks over the WHOLE document is
#     masked in every block drawn from it (`JsonMasker._key_lines`);
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
#: The keyword ENDS the key -- `password`, `DB_PASSWORD`, `github_token` -- or
#: is followed only by a key suffix: `SECRET_KEY`, `PASSWORD_HASH`,
#: `AWS_SECRET_ACCESS_KEY`. The key part of the key/value rule, with `api-key`
#: and `private_key` added, and its `keysuffix` group (#260,
#: `swarm_redaction.rules.KEY_VALUE`): a name ending `_key`, `_access_key` or
#: `_hash` names a credential exactly as a singular one does, digits and all,
#: so a number under it is masked on `/input`, `/logs` and an artifact alike
#: (the PR #378 review; owner decision 2026-09-30). A bare plural (`secrets`,
#: `max_tokens`) is still a count.
_CREDENTIAL_KEY_AT_END = re.compile(
    r"(?:" + _CREDENTIAL_WORD + r")(?:s?[-_](?:access[-_]?key|key|hash))?\Z", re.IGNORECASE
)

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
      * a NUMBER only when the credential word ENDS the key, or is followed
        only by `_key`, `_access_key` or `_hash` (`_CREDENTIAL_KEY_AT_END`): a
        PIN under `password` or `SECRET_KEY` is one, `input_tokens: 10` and
        `credential_revoked_times: 2` -- keys this platform's own mock runner
        takes -- are counts;
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


#: A string shorter than this, in base64 characters, is body only when it ends
#: in `=` padding. WHY: after a truncated key the next string is as likely an
#: ordinary word (`"kept"`, `"done"`, `"ok"`) as a body line, and every one of
#: those is base64-shaped; a key's body lines are 64 characters (76 in some
#: tools) but its LAST line can be anything from 1 to 63. Sixteen keeps every
#: short word in clear and masks every full line. The residual (#385): a final
#: body line under 16 characters with no padding is served -- at most 11 bytes
#: of a key's trailing DER, which ends in the key's last integer, not its
#: modulus. A padded short line (`"AB=="`) is masked.
KEY_BODY_MIN_CHARS = 16


def _key_body_shape(value: str, *, headers_allowed: bool) -> str | None:
    """`"base64"` or `"header"` for a string shaped like a key's body lines, else None.

    The text path's line test (`swarm_redaction.rules._body_line`) over every
    line of the stripped string: blank, base64 (`*` from an earlier mask and a
    tool's line numbers included), or an RFC 1421 header before any base64.
    `"base64"` only when the base64 is at least `KEY_BODY_MIN_CHARS` long or
    ends in `=` padding; `"header"` for headers and blank lines alone.
    """
    seen_header = False
    chars = 0
    last = ""
    for line in value.strip().split("\n"):
        shape = _body_line(line, headers_allowed=headers_allowed and not chars)
        if shape == "base64":
            stripped = line.strip(" \t\r")
            last = stripped[_NUMBERED_PREFIX.match(stripped).end() :]  # type: ignore[union-attr]
            chars += len(last)
        elif shape == "header":
            seen_header = True
        elif shape is None:
            return None
    if chars:
        return "base64" if chars >= KEY_BODY_MIN_CHARS or last.endswith("=") else None
    return "header" if seen_header else None


def _key_body_string(value: str) -> bool:
    """Whether `value` reads as a private key's body lines (`_key_body_shape`, base64)."""
    return _key_body_shape(value, headers_allowed=True) == "base64"


_NUMBERED_PREFIX = re.compile(_NUMBERED)


class _KeyRun:
    """A private key with no END in reach, followed across JSON containers (#385).

    `step()` is fed every string -- value or key -- in document order and says
    whether it is the open key's material, to be masked and counted 0. The rule
    is in the comment block above `JsonMasker`; `PEM_BLOCK_MAX_CHARS` is the
    bound `swarm_redaction.rules` gives a block of text.
    """

    def __init__(self) -> None:
        self.open = False
        self.size = 0
        self.seen_base64 = False

    def step(self, text: str, *, key: bool = False) -> bool:
        if self.open and self.size > PEM_BLOCK_MAX_CHARS:
            self.open = False
        hit = False
        if self.open:
            self.size += len(text) + 1
            if _PEM_END.search(text) is not None:
                hit = True
                self.open = False
            elif text.strip():
                shape = _key_body_shape(text, headers_allowed=not self.seen_base64)
                if shape is not None:
                    hit = True
                    self.seen_base64 = self.seen_base64 or shape == "base64"
                elif not key:
                    self.open = False
        if _open_key(text):
            self.open, self.size, self.seen_base64 = True, 0, False
        return hit

    def through(self, text: str) -> None:
        """An element the flat-list rule (`_key_run_end`) already masked: counted
        toward the bound, and an END in it ends the run; nothing else does."""
        self.size += len(text) + 1
        if _PEM_END.search(text) is not None:
            self.open = False
        if _open_key(text):
            self.open, self.size, self.seen_base64 = True, 0, False

    def scan(self, node: Any, hits: set[str] | None = None) -> None:
        """Every string under `node`, in the order `JsonMasker._walk` reads them."""
        if isinstance(node, str):
            if self.step(node) and hits is not None:
                hits.add(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                raw = _key_text(key)
                if self.step(raw, key=True) and hits is not None:
                    hits.add(raw)
                self.scan(item, None if _masks_whole(raw, item) else hits)
        elif isinstance(node, (list, tuple)):
            items = list(node)
            through = -1
            for at, item in enumerate(items):
                if at <= through:
                    self.through(item)
                    continue
                self.scan(item, hits)
                if isinstance(item, str) and _open_key(item):
                    through = _key_run_end(items, at)
        elif node is not None and not isinstance(node, (bool, int, float)):
            self.scan(str(node), hits)


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

    A NUMBER is masked when it holds one of the document's literals (`number()`,
    #387): it becomes the JSON string `"********"`, as a number under a
    credential's name does. Every other number, `true`, `false` and `null` is
    served as it is.
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
        # The strings and keys a no-END key run masks over the WHOLE document
        # (#385), so a block drawn without the key's BEGIN -- `/input`'s `rest`
        # beside a prompt that opens the key -- masks them too.
        self._key_lines: set[str] = set()
        _KeyRun().scan(document, self._key_lines)
        # A nonce, so no key in the document can spell a stand-in.
        self._tag = secrets.token_hex(8)

    @property
    def literals(self) -> tuple[str, ...]:
        """What this document named as secret, longest first (`_learned_literals`)."""
        return self._literals

    def number(self, text: str) -> int:
        """How many of this masker's literals a number's JSON text holds.

        THE rule for a number that holds a learned literal, used by `_walk`
        (every route that masks through this class: `/input`, `GET
        /v1/tasks/{id}`, `/transcript`, `/logs`) and by `json_masking._walk`
        (artifacts), so the routes cannot disagree on it. WHY: a PIN the task
        named as a string of digits under a credential's name, echoed by the
        agent as a number, was served in clear, because only strings were
        compared (the PR #378 review for artifacts, #387 for every other
        route). The digits are looked for anywhere in the text: as the
        number, inside a longer one, or in `N.0` and `Ne0`.
        """
        return _mask_literals(text, self._literals)[1]

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

    def _walk(
        self,
        value: Any,
        *,
        label_for: Callable[[dict[str, Any], str, str], str],
        served: Callable[[str], str] = lambda text: text,
    ) -> tuple[Any, int]:
        """`value` with every string, key and credential masked, and the count.

        `label_for(out, raw_key, masked_key)` names the entry a key becomes in
        the object being built: `json()` stands a changed or colliding key in
        and restores it in the text, `value()` numbers a collision.

        `served(text)` is applied to every masked string leaf and key before
        it is placed: `value()` escapes lone surrogates there, `json()` does it
        to its whole text instead.
        """
        total = 0
        run = _KeyRun()

        def walk(node: Any) -> Any:
            nonlocal total
            if isinstance(node, str):
                if run.step(node) or node in self._key_lines:
                    # Key material of the key a string before it opened: its
                    # one mask was counted at the BEGIN.
                    return MASK
                masked = self.text(node)
                total += masked.count
                return served(masked.text)
            if isinstance(node, dict):
                out: dict[str, Any] = {}
                for key, item in node.items():
                    raw = _key_text(key)
                    if run.step(raw, key=True) or raw in self._key_lines:
                        # A body line stored as a KEY (#385): masked like a value.
                        label = label_for(out, raw, MASK)
                    else:
                        name = self.text(raw)
                        total += name.count
                        label = label_for(out, raw, served(name.text))
                    if _masks_whole(raw, item):
                        total += 1
                        out[label] = MASK
                        # Unread, but a BEGIN inside it still opens the run.
                        run.scan(item)
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
                        run.through(item)
                        shaped.append(MASK)
                        continue
                    shaped.append(walk(item))
                    if isinstance(item, str) and _open_key(item):
                        through = _key_run_end(items, at)
                return shaped
            # bool before int: `True` is an int in Python, and never a literal.
            if node is None or isinstance(node, bool):
                return node
            if isinstance(node, (int, float)):
                # A number decoded from a log line is looked for in the token
                # as the line wrote it (`_StoredFloat`), exactly where the
                # artifact path looks: its `json.dumps` text is not that token
                # once the exponent leaves positional form -- `Ne9` re-encodes
                # as `4.8261937e+16`, `Ne-3` as `48261.937` -- and the digits
                # were served (#387 review). A number with no stored token (a
                # Firestore value) is looked for in its `json.dumps` text.
                found = self.number(getattr(node, "token", None) or json.dumps(node))
                if found:
                    total += found
                    return MASK
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
        # A lone surrogate (decoded from a `\\ud800` escape) cannot be written as UTF-8: escape it again (PR #378).
        text = _escape_surrogates(text)
        return Redacted(text=text, count=total)

    def value(self, value: Any) -> tuple[Any, int]:
        """`value` as a JSON value, masked exactly as `json()` masks it, and the count.

        A lone surrogate in a string or key is served as the six characters
        `\\ud800`, as `json()` writes it: the response is encoded as UTF-8, which
        cannot hold one, so a document stored before submission refused them
        (validation.py) made `GET /v1/tasks/{id}` a 500 (#361 box 60). It is
        escaped before a collision is numbered, so a key that escapes to
        another key's text is served as `<key> (2)`, not written over it.
        """

        def numbered(out: dict[str, Any], _raw: str, masked: str) -> str:
            if masked not in out:
                return masked
            n = 2
            while f"{masked} ({n})" in out:
                n += 1
            return f"{masked} ({n})"

        return self._walk(value, label_for=numbered, served=_escape_surrogates)


def _escape_surrogates(text: str) -> str:
    """Every surrogate code point in `text` as its `\\uXXXX` escape (PR #378, #361 box 60)."""
    return re.sub("[\ud800-\udfff]", lambda m: f"\\u{ord(m.group(0)):04x}", text)


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


class _DuplicateKey(ValueError):
    """Raised out of `_reject_duplicate_keys` and caught where `json.loads` is."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`dict(pairs)`, but first check no key repeats at this object level.

    Plain `json.loads` keeps only the LAST value of a repeated key -- so
    `{"note":"export PASSWORD=hunter2","note":"ok"}` decodes to `{"note":
    "ok"}` and `JsonMasker` never sees the secret half at all: the document
    parses cleanly, the walk finds nothing to mask, and the line was about to
    be served byte for byte (PR #229 review). A key that repeats at ANY level
    of the document makes the whole line untrustworthy to decode, so the line
    falls back to the text rule instead of being trusted as JSON.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen.add(key)
    return dict(pairs)


class _StoredFloat(float):
    """A JSON float that keeps the token it was decoded from.

    `json.dumps` writes it as any float (`float.__repr__`), so a line served
    re-encoded is unchanged by it; `JsonMasker._walk` reads `token` to decide
    whether the number holds a learned literal as WRITTEN, the way the
    artifact path (`json_masking`) reads the stored token (#387). An int needs
    no such type: `json.dumps` of an int is its token.
    """

    token: str

    def __new__(cls, token: str) -> "_StoredFloat":
        number = super().__new__(cls, token)
        number.token = token
        return number


def _json_document(line: str) -> Any | None:
    """The object or list `line` holds, when the whole line is one; else None.

    `object_pairs_hook=_reject_duplicate_keys`: a line whose JSON has a
    repeated key anywhere is treated as not-a-document, so it is masked by
    the text rule instead of being decoded down to the surviving key and
    served as if that were the whole truth. `parse_float=_StoredFloat`: a
    float keeps the token the line wrote it as (#387).
    """
    body = line.strip()
    if len(body) < 2 or len(body) > JSON_LINE_MAX_CHARS:
        return None
    if not ((body[0] == "{" and body[-1] == "}") or (body[0] == "[" and body[-1] == "]")):
        return None
    try:
        document = json.loads(body, object_pairs_hook=_reject_duplicate_keys, parse_float=_StoredFloat)
    except (ValueError, _DuplicateKey):
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

      * A line whose masking changed nothing is served BYTE FOR BYTE as stored
        -- but only once the TEXT rule has also had a look at it and found
        nothing either (the PR #229 review): the structural walk masks
        string leaves, credential-named keys' whole values and numbers that
        hold a learned literal, and nothing else, so a non-string scalar the
        walk cannot touch is still caught by the text rule before the line is
        trusted as clean.
      * A line the walk masked something in is NOT given that text pass
        (owner decision, 2026-09-30, `json_masking`'s docstring says why). A
        number holding a literal the task named is masked by the walk itself
        (`JsonMasker.number`), so the skipped pass no longer decides whether
        such a number is served (#387): before, `{"note": "export
        PASSWORD=<v>", "n": <v as digits>}` served the number in clear.
      * A line whose JSON has the SAME KEY TWICE, at any level, is not
        decoded at all: plain `json.loads` keeps only the last value of a
        repeated key, so `{"note":"export PASSWORD=hunter2","note":"ok"}`
        would parse to `{"note": "ok"}`, the walk would find nothing to mask
        in "ok", and the secret half would never have been decoded in the
        first place (the PR #229 review). Such a line falls back to the text
        rule below instead, over the ORIGINAL bytes, where the secret still is.
      * A line that was masked (structurally) is written back as compact JSON
        (`","`, `":"`, what an agent CLI writes), so it stays one line and one
        document.
      * Everything that is not a whole document -- plain text, a line a window
        cut, a duplicate-keyed line, a document over `JSON_LINE_MAX_CHARS` --
        is masked by `redact`, in runs, so a private key printed over many
        lines is still one block. `inside_key` applies to the window's first
        run only, which is where a paged reader's look-back found the key open.
      * `literals`: values the task named as secret (`TaskMasking.literals`),
        masked after the rules wherever they appear, in every case above.
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
            # The structural walk found nothing to mask -- but it only masks
            # STRING leaves, credential-named keys' whole values and numbers
            # holding a learned literal (#387, `JsonMasker.number`, so the
            # number case never depends on this pass); a scalar that is not
            # a string (`"api_key": null`) is none of those, and the
            # text rule may still find a credential shape the walk cannot see
            # at all. Run it over the stored line before trusting "nothing
            # here" enough to serve the line byte for byte (the PR #229 review).
            scrubbed = redact(line)
            safety, found = _mask_literals(scrubbed.text, literals)
            if scrubbed.count or found:
                pieces.append(safety)
                count += scrubbed.count + found
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
