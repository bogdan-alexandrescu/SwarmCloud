"""What guards a publish: the agent's commits replayed onto the clean publish
repository, cleaned of attribution, and scanned for a credential before
anything is pushed; with the two checks the attempt runs around it -- the
runner profile's input re-checked before the agent starts, and an error chain
read for a service that was unavailable.

Moved out of `lifecycle.py` unchanged (issue 68, splitting that file by
concern). `lifecycle` binds only the names it, another module or a test still
reads through it (issue 193): a patch of one the worker calls is made there,
and of any other name here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from swarm_common.profiles import RUNNER_PROFILES, InputRefused, check_inputs
from swarm_redaction import MASK as CREDENTIAL_MASK
from swarm_redaction import credentials as _credentials
from swarm_redaction import redact as redact_text

from . import territory as territory_mod
from .errors import ConfigError
from .gitops import (
    ATTRIBUTION_MARKERS,
    EMPTY_CLONE_BASE,
    GitError,
    merged_head_kept,
)
from .gitops import (
    _NO_HOOKS,
    _SHA_RE,
    _git_stream,
    _git_text,
    _git_text_full,
    _worker_identity,
)

CredentialHit = _credentials.CredentialHit


#: The subjects of the worker's own commits -- the one holding what the agent
#: left uncommitted, and the one a fold makes -- when the agent wrote no usable
#: `pr-title.txt`. With one, that title is the subject instead
#: (`Worker._worker_commit_message`). NEVER THE TASK ID (archived issue 361): the subject is
#: the line a reviewer and `git log --oneline` read, and the owner's rule for
#: pull request titles (2026-09-28) holds for it too. The task id goes in the
#: body, where the provenance belongs.
UNCOMMITTED_COMMIT_SUBJECT = "Commit the changes the agent left uncommitted"


#: The most agent commits the publish keeps one by one (archived issue 242). Each costs two
#: git processes; past this many the agent's history is folded into one worker
#: commit as it was before, and the run result says which happened.
MAX_KEPT_COMMITS = 200


#: The most bytes of one agent commit message kept, after it is cleaned.
COMMIT_MESSAGE_MAX_BYTES = 16 * 1024


def _carries_attribution(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ATTRIBUTION_MARKERS)


#: U+1F916 ROBOT FACE, which Claude Code's "Generated with" footer opens with.
#: A line that starts with it is the footer, or what is left of one.
_ROBOT_FACE = "\U0001f916"


def _is_attribution_line(line: str) -> bool:
    return _carries_attribution(line) or line.lstrip().startswith(_ROBOT_FACE)


def strip_attribution(text: str) -> str:
    """`text` without its attribution lines, or `text` itself when it has none (archived issue 735).

    A line is attribution when it carries one of `gitops.ATTRIBUTION_MARKERS`
    -- the "Generated with" footer, a `Co-Authored-By:` trailer, a link to
    the tool -- or starts with the robot face the footer opens with. Those
    lines are removed and nothing else is: a body with none is returned as
    the same object, byte for byte. Where lines were removed, runs of blank
    lines are collapsed to one and the ends trimmed, so the footer's
    paragraph does not leave a gap.

    The empty string means nothing meaningful was left -- no letter or digit
    outside the removed lines, as when the body was only a footer, maybe
    under a `---` rule -- and the caller falls back to the generated body.

    OWNER DECISION, 2026-10-06: strip, don't drop. The worker used to refuse a
    whole `pr-body.md` for its footer, so a body that said `Closes #N`
    published without it (16 of 248 pull requests in 7 days).
    """
    lines = text.split("\n")
    kept = [line for line in lines if not _is_attribution_line(line)]
    if len(kept) == len(lines):
        return text
    collapsed: list[str] = []
    for line in kept:
        if line.strip() or (collapsed and collapsed[-1].strip()):
            collapsed.append(line)
    cleaned = "\n".join(collapsed).strip()
    if not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


#: A trailer line as git reads one: `Token: value`, the token letters, digits
#: and hyphens. `Co-Authored-By:`, `Signed-off-by:`, `Change-Id:`.
_TRAILER_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*[ \t]*:[ \t]")


def clean_commit_message(message: str, *, scrub: Callable[[str], str], fallback: str) -> str:
    """An agent's commit message as the worker pushes it (archived issue 242).

    * every line carrying attribution (`gitops.ATTRIBUTION_MARKERS`) is
      removed, wherever it is -- the "Generated with" footer is a paragraph of
      its own, not a trailer;
    * then the trailer block -- a closing paragraph of `Token: value` lines,
      as git reads one -- is removed, more than once if there are several;
      the subject is never taken for one;
    * registered secrets are scrubbed, then the message is cut to
      `COMMIT_MESSAGE_MAX_BYTES`;
    * a message with nothing left is `fallback`, the worker's own.

    NULs are dropped: an argv cannot carry one, and a commit message can.
    """
    text = message.replace("\x00", "").replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    lines = [line for line in lines if not _carries_attribution(line)]
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    paragraphs = [p for p in paragraphs if p]
    while len(paragraphs) > 1 and _is_trailer_block(paragraphs[-1]):
        paragraphs.pop()
    cleaned = scrub("\n\n".join("\n".join(p) for p in paragraphs)).strip()
    if not cleaned:
        return fallback
    encoded = cleaned.encode("utf-8")
    if len(encoded) > COMMIT_MESSAGE_MAX_BYTES:
        cleaned = (
            encoded[:COMMIT_MESSAGE_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
            + f"\n\n[cut at {COMMIT_MESSAGE_MAX_BYTES} bytes by the worker]"
        )
    return cleaned


def _is_trailer_block(paragraph: list[str]) -> bool:
    """True when every line is a trailer, or continues the one above it."""
    if not _TRAILER_LINE.match(paragraph[0]):
        return False
    return all(_TRAILER_LINE.match(line) or line[:1] in (" ", "\t") for line in paragraph)


#: How much added text one window of the leak scan holds (`_DiffLeakScanner`).
#: OWNER DECISION, 2026-09-29 (archived pull request 259): a diff is scanned WHOLE however big it
#: is, in windows of this size, rather than refused or cut at a cap. Sized so
#: a window is cheap to hold and to run every rule over, not as a limit.
SCAN_WINDOW_CHARS = 4 * 1024 * 1024


#: How much of one window is scanned again at the start of the next, so a
#: credential straddling the cut is still seen whole in one of them. It must
#: be at least the longest credential match and the longest registered
#: secret; the callers add the longest registered secret to this. Every
#: credential rule matches within one line, and a key/value assignment or a
#: provider token is far under 64 KiB. A PEM block's marker is what its rule
#: finds, and the marker is 40 characters.
SCAN_OVERLAP_CHARS = 64 * 1024


class ScanHit(NamedTuple):
    """The first leak a diff scan found: the file, the rule, and the line of
    the new file it is on. This is what a refusal names (archived issue 373), and it holds
    no part of the value."""

    path: str
    rule: str
    line: int


#: The predicate both publish scans ask about each file's added text:
#: `(path, text) -> CredentialHit | None` (`Worker._leaks_in_added_text`).
#: The path is the file's, so the guard can be tiered by it (archived issue 373).
LeakPredicate = Callable[[str, str], "CredentialHit | None"]


#: The rule name a refusal gives for a task's registered secret.
REGISTERED_SECRET_RULE = "registered_secret"


#: The new-file start line, and line count, of a `-U0` hunk header:
#: `@@ -a[,b] +c[,d] @@`.
_HUNK_NEW_START = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))?")


class _DiffLeakScanner:
    """Scans a `git diff` stream's ADDED text, file by file, in bounded windows.

    `feed` takes the diff's bytes as they arrive (`gitops._git_stream`), in
    chunks of any size; `close` ends the stream and returns the first hit --
    the file, the rule `leaks` named, and the new file's line (`ScanHit`) --
    or None. `leaks` is asked with the file's path, so the guard is tiered by
    it (archived issue 373). Nothing is held but the
    current window, the overlap carried from the one before, and one line's
    first few characters -- a 40 MB single-line file costs a window, not 40 MB.

    A `+++ ` line is a file's header only before its first `@@`, and every
    `+` line after is content: an added line whose own text is `++ AKIA...`
    prints as `+++ AKIA...`, and reading it as a header would let a
    credential through behind two plus signs. A deleted file
    (`+++ /dev/null`) adds nothing. Bytes are decoded incrementally as UTF-8 with
    replacement, and no newline translation happens, so a lone `\\r` stays
    inside its line (archived pull request 259 review, M2).

    WINDOWS OVERLAP. When a file's added text passes `window` characters, it
    is scanned and all but its last `overlap` characters are dropped; those
    are scanned again with what follows. The carry starts at a line boundary
    when one is within another `overlap` of the cut, so a match at the start
    of a window reads the true character before it. On a single line longer
    than that the carry starts mid-line, where a pattern starting the window
    counts as starting a token: a possible false hit, which folds or refuses
    -- the safe direction.

    ONLY WHAT A SECOND DIFF ALSO ADDS (`within`, the merged-main floor). Given
    a map of each file's added line ranges from another diff with the same
    new side (`added` of a scan of `floor..HEAD`), an added line is scanned
    only when its new-file line is in one of them: a line the other old side
    already holds is never asked about. The lines kept are scanned exactly as
    an unfiltered diff of them would be -- windows, overlap, and the line a
    hit names -- and `added` records only them.
    """

    _HEADS = ("diff --git ", "+++ ", "@@")

    def __init__(
        self,
        leaks: LeakPredicate,
        *,
        window: int,
        overlap: int,
        within: dict[str, list[tuple[int, int]]] | None = None,
    ) -> None:
        import codecs

        self._leaks = leaks
        self._window = max(window, 2 * overlap + 1)
        self._overlap = overlap
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.hit: ScanHit | None = None
        self._path = ""
        # WHERE A HIT IS, IN THE FILE'S OWN LINES (archived issue 373): the refusal names the
        # line. `_file_lines` counts the added lines fed so far, `_window_line`
        # is the added-line index the current window starts at, and `_hunks`
        # maps the added-line index each `@@` hunk starts at to the new-file
        # line it starts at. With `-U0` a hunk's added lines are consecutive.
        self._file_lines = 0
        self._window_line = 0
        self._hunks: list[tuple[int, int]] = []
        self._in_header = True
        self._mode: str | None = None  # None (line start), "add", "header", "skip"
        self._head = ""
        self._header_line = ""
        self._buf: list[str] = []
        self._buf_len = 0
        self._carry = ""
        # WHERE EACH FILE GAINED LINES (archived issue 361 box 79): the new file's path
        # (unquoted) to its added line ranges, `(first line, count)`, one per
        # hunk. `-U0` shows no line the branch kept, so an added key body
        # between an unchanged BEGIN/END pair carries no marker here; the
        # caller reads the new file around these ranges
        # (`_added_inside_a_private_key`).
        self.added: dict[str, list[tuple[int, int]]] = {}
        self._added_key = ""
        # The `within` filter: how the current hunk's added lines are taken
        # ("all", "none" or "some"), and for "some" the new-file line of the
        # next added line, the kept spans `(first, last)` still ahead, whether
        # a line is open and kept, and the last kept line (a gap starts a new
        # entry in `_hunks`, so a hit still names its own line).
        self._within = within
        self._take = "all"
        self._line_no = 0
        self._spans: list[tuple[int, int]] = []
        self._line_open = False
        self._line_kept = False
        self._last_kept: int | None = None

    def feed(self, data: bytes) -> None:
        if self.hit is None:
            self._text(self._decoder.decode(data))

    def close(self) -> ScanHit | None:
        if self.hit is None:
            self._text(self._decoder.decode(b"", final=True))
        if self.hit is None:
            self._end_line()
            self._flush_file()
        return self.hit

    # -- parsing -------------------------------------------------------------

    #: Runs of whole content lines, taken in one step rather than a line at a
    #: time: a 40 MB diff is millions of lines, and a Python step per line
    #: made the scan the slowest thing in the publish. Inside a hunk a line
    #: starting `+` is added content, and `-`, ` ` or `\\` is not; neither
    #: run can hold a `diff --git` or `@@` line, which start otherwise.
    _ADDED_RUN = re.compile(r"(?:\+[^\n]*\n)+")
    _OTHER_RUN = re.compile(r"(?:[-\\ ][^\n]*\n)+")

    def _text(self, text: str) -> None:
        i, n = 0, len(text)
        while i < n and self.hit is None:
            if self._mode is None and not self._head and not self._in_header:
                run = self._ADDED_RUN.match(text, i)
                if run is not None:
                    # Each line's one `+` prefix goes: the first by the
                    # slice, every other as the character after a newline.
                    self._add(run.group()[1:].replace("\n+", "\n"))
                    i = run.end()
                    continue
                run = self._OTHER_RUN.match(text, i)
                if run is not None:
                    i = run.end()
                    continue
            newline = text.find("\n", i)
            end = n if newline < 0 else newline
            if self._mode is None:
                need = 11 - len(self._head)
                take = text[i : min(end, i + need)]
                self._head += take
                i += len(take)
                complete = i == end and newline >= 0
                if not complete and len(self._head) < 11 and i >= n:
                    if any(p.startswith(self._head) for p in self._HEADS):
                        return  # undecided until more arrives
                self._classify()
                continue
            segment = text[i:end]
            if self._mode == "add":
                self._add(segment)
            elif self._mode in ("header", "hunk") and len(self._header_line) < 64 * 1024:
                self._header_line += segment
            i = end
            if newline >= 0:
                i += 1
                self._end_line()

    def _classify(self) -> None:
        head = self._head
        if head.startswith("diff --git "):
            self._flush_file()
            self._path, self._in_header, self._mode = "", True, "skip"
        elif head.startswith("@@"):
            self._in_header, self._mode, self._header_line = False, "hunk", head
        elif self._in_header:
            if head.startswith("+++ "):
                self._mode, self._header_line = "header", head
            else:
                self._mode = "skip"
        elif head.startswith("+"):
            self._mode = "add"
            self._add(head[1:])
        else:
            self._mode = "skip"

    def _end_line(self) -> None:
        if self._mode is None and self._head:
            self._classify()
        if self._mode == "add":
            self._add("\n")
        elif self._mode == "header":
            name = self._header_line[4:].rstrip("\r")
            quoted = len(name) > 1 and name.startswith('"') and name.endswith('"')
            if quoted:
                name = name[1:-1]
            self._path = name[2:] if name.startswith("b/") else name
            self._added_key = _c_unquote(self._path) if quoted else self._path
        elif self._mode == "hunk":
            start = _HUNK_NEW_START.match(self._header_line)
            if start is not None:
                count = 1 if start.group(2) is None else int(start.group(2))
                if self._within is None:
                    self._hunks.append((self._file_lines, int(start.group(1))))
                    if count and self._added_key and self._added_key != "/dev/null":
                        self.added.setdefault(self._added_key, []).append(
                            (int(start.group(1)), count)
                        )
                else:
                    self._begin_filtered_hunk(int(start.group(1)), count)
        self._mode, self._head, self._header_line = None, "", ""

    def _begin_filtered_hunk(self, first: int, count: int) -> None:
        """Set how the hunk starting at new-file line `first`, adding `count`
        lines, is taken under `within`: its lines the other diff also adds."""
        last = first + count - 1
        spans: list[tuple[int, int]] = []
        if count and self._added_key and self._added_key != "/dev/null":
            assert self._within is not None
            for start, length in self._within.get(self._added_key, ()):
                low, high = max(first, start), min(last, start + length - 1)
                if low <= high:
                    spans.append((low, high))
        spans.sort()
        for low, high in spans:
            self.added.setdefault(self._added_key, []).append((low, high - low + 1))
        if spans == [(first, last)]:
            self._take = "all"
            self._hunks.append((self._file_lines, first))
        elif not spans:
            self._take = "none"
        else:
            self._take = "some"
            self._line_no, self._spans = first, spans
            self._line_open, self._line_kept, self._last_kept = False, False, None

    # -- scanning ------------------------------------------------------------

    def _add(self, text: str) -> None:
        if not text or self._take == "none":
            return
        if self._take == "some":
            self._add_some(text)
            return
        self._add_kept(text)

    def _add_some(self, text: str) -> None:
        """`text` of a hunk only some of whose lines `within` keeps: each
        line's pieces go to the scan when its new-file line is kept."""
        pos, n = 0, len(text)
        while pos < n and self.hit is None:
            newline = text.find("\n", pos)
            end = n if newline < 0 else newline + 1
            if not self._line_open:
                self._line_open = True
                line = self._line_no
                while self._spans and self._spans[0][1] < line:
                    self._spans.pop(0)
                self._line_kept = bool(self._spans) and self._spans[0][0] <= line
                if self._line_kept:
                    if self._last_kept != line - 1:
                        self._hunks.append((self._file_lines, line))
                    self._last_kept = line
            if self._line_kept:
                self._add_kept(text[pos:end])
            if newline >= 0:
                self._line_open = False
                self._line_no += 1
            pos = end

    def _add_kept(self, text: str) -> None:
        self._file_lines += text.count("\n")
        self._buf.append(text)
        self._buf_len += len(text)
        if self._buf_len >= self._window:
            self._scan(last=False)

    def _scan(self, *, last: bool) -> None:
        text = self._carry + "".join(self._buf)
        self._buf, self._buf_len = [], 0
        found = self._leaks(self._path, text) if text else None
        if found:
            self.hit = ScanHit(
                self._path or "a file",
                found.rule,
                self._file_line(self._window_line + text.count("\n", 0, found.offset)),
            )
            return
        if last:
            self._carry = ""
            return
        cut = max(len(text) - self._overlap, 0)
        line_start = text.rfind("\n", max(cut - self._overlap, 0), cut)
        carry_from = line_start + 1 if line_start >= 0 else cut
        self._window_line += text.count("\n", 0, carry_from)
        self._carry = text[carry_from:]

    def _file_line(self, added_index: int) -> int:
        """The new file's 1-based line number of the added line at `added_index`."""
        line = added_index + 1
        for first, start in self._hunks:
            if first > added_index:
                break
            line = start + (added_index - first)
        return line

    def _flush_file(self) -> None:
        if self._buf:
            self._scan(last=True)
        self._carry = ""
        self._file_lines, self._window_line, self._hunks = 0, 0, []
        self._added_key = ""
        self._take = "all"


def _c_unquote(body: str) -> str:
    """A path git C-quoted in a diff header (the quotes already removed), as
    the path itself: `\\t`, `\\"`, `\\\\` and octal byte escapes undone."""
    simple = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    raw = body.encode("utf-8", "surrogateescape")
    out = bytearray()
    i = 0
    while i < len(raw):
        byte = raw[i]
        if byte == 92 and i + 1 < len(raw):
            following = chr(raw[i + 1])
            if following in simple:
                out.append(simple[following])
                i += 2
                continue
            octal = raw[i + 1 : i + 4]
            if len(octal) == 3 and all(48 <= b <= 55 for b in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
        out.append(byte)
        i += 1
    return out.decode("utf-8", "replace")


def _private_key_spans(text: str) -> list[tuple[int, int]]:
    """The spans of `text` main's private-key rule masks, as `(start, end)`.

    `swarm_redaction.rules.mask_private_keys` (decoded=False, not inside a
    key), step for step and with its own helpers, so "inside a key" means
    exactly what the private-key rule means everywhere else on the platform
    (owner decision, 2026-10-01). A test rebuilds `mask_private_keys`'s output
    from these spans for every shape it masks.
    """
    from swarm_redaction.rules import _PEM_BEGIN, _PEM_END, _PEM_HINT, _block_end, _tail_start

    if _PEM_HINT not in text:
        return []
    ends = [(m.start(), m.end()) for m in _PEM_END.finditer(text)]
    end_starts = [start for start, _ in ends]
    end_ends = [end for _, end in ends]
    spans: list[tuple[int, int]] = []

    def orphans(low: int, high: int) -> None:
        piece = text[low:high]
        if _PEM_HINT not in piece:
            return
        pos = 0
        # An orphan END is a key's tail even with nothing before it to take
        # (`_mask_orphan_ends`, archived issue 361 box 82): its span may be empty.
        for marker in _PEM_END.finditer(piece):
            start = _tail_start(piece, marker.start(), pos)
            spans.append((low + start, low + marker.start()))
            pos = marker.start()

    pos = search = 0
    while True:
        begin = _PEM_BEGIN.search(text, search)
        if begin is None:
            break
        orphans(pos, begin.start())
        stop = _block_end(text, begin.end(), end_starts, end_ends, to_end=False)
        spans.append((begin.end(), stop))
        pos = stop
        search = max(stop, begin.end())
    orphans(pos, len(text))
    return spans


#: The most of one file the private-key context check reads (archived issue 361 box 79). A
#: bigger file holding a key marker, with lines added to it, is refused: the
#: check cannot tell, so it fails closed.
KEY_CONTEXT_MAX_BYTES = 32 * 1024 * 1024


#: How many paths one `git grep` is given.
_KEY_CONTEXT_PATHS_PER_GREP = 200


def _added_inside_a_private_key(
    added: dict[str, list[tuple[int, int]]],
    *,
    git: list[str],
    rev: str,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
) -> ScanHit | None:
    """The first added line that falls inside a private key of the NEW file
    `rev` holds, or None (archived issue 361 box 79, security pass 2026-10-09).

    The scans read a `-U0` diff, which shows no line the branch kept: a key
    body put between an existing, unchanged BEGIN/END pair (a test stub
    replaced by a real key) adds no marker, so the private-key rule never saw
    one, and the key was published. This asks the whole new file instead:
    `git grep` names the files at `rev` holding a marker; for each that gained
    lines, the file is streamed (never stored) and main's private-key spans
    (`_private_key_spans`) are laid over the added ranges. An added line
    inside a span is refused as `private_key_block`, on its line.
    """
    if not added:
        return None
    from swarm_redaction.rules import _PEM_HINT

    kwargs: dict[str, Any] = {
        "repo": repo, "private_dir": private_dir, "logs_dir": logs_dir,
        "timeout_seconds": timeout_seconds, "logger": logger,
    }
    paths = sorted(added)
    marked: list[str] = []
    for index in range(0, len(paths), _KEY_CONTEXT_PATHS_PER_GREP):
        chunk = paths[index : index + _KEY_CONTEXT_PATHS_PER_GREP]
        code, listing, truncated = _git_text_full(
            [*git, "grep", "-l", "-z", "--no-color", "--no-textconv", "-F", "-e", _PEM_HINT,
             rev, "--", *(":(literal)" + path for path in chunk)],
            slug="publish-key-context-grep", **kwargs,
        )
        # `git grep` exits 1 when nothing matched.
        if code not in (0, 1) or truncated:
            raise GitError("could not search the branch's files for private-key markers")
        prefix = rev + ":"
        for entry in listing.split("\0"):
            if entry:
                marked.append(entry[len(prefix):] if entry.startswith(prefix) else entry)
    for path in marked:
        ranges = added.get(path)
        if not ranges:
            continue
        chunks: list[bytes] = []
        size = [0]

        def keep(data: bytes) -> None:
            if size[0] <= KEY_CONTEXT_MAX_BYTES:
                chunks.append(data)
            size[0] += len(data)

        code = _git_stream(
            [*git, "cat-file", "blob", f"{rev}:{path}"],
            slug="publish-key-context-read", consume=keep, **kwargs,
        )
        if code != 0:
            raise GitError("could not read a file the branch adds lines to")
        if size[0] > KEY_CONTEXT_MAX_BYTES:
            return ScanHit(path, "private_key_block", ranges[0][0])
        text = b"".join(chunks).decode("utf-8", "replace")
        spans = _private_key_spans(text)
        if not spans:
            continue
        for start, end in spans:
            first = text.count("\n", 0, start) + 1
            last = first + text.count("\n", start, max(end - 1, start))
            for line, count in ranges:
                if line <= last and line + count - 1 >= first:
                    return ScanHit(path, "private_key_block", max(line, first))
    return None


def _scan_diff_stream(
    argv: list[str],
    *,
    leaks: LeakPredicate,
    overlap: int,
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    slug: str,
    timeout_seconds: int,
    logger: Any,
    new_rev: str | None = None,
    git: list[str] | None = None,
    within: dict[str, list[tuple[int, int]]] | None = None,
) -> tuple[int, ScanHit | None]:
    """Run a `git diff` and scan everything it adds; (exit code, the first hit or None).

    With `new_rev` (the diff's new side) and `git` (the git argv prefix), a
    diff that added nothing credential-shaped is also asked whether an added
    line sits inside a private key of the new file
    (`_added_inside_a_private_key`, archived issue 361 box 79).

    With `within`, only the added lines it also names are scanned
    (`_DiffLeakScanner`), and only they are asked the private-key question.
    """
    scanner = _DiffLeakScanner(
        leaks, window=SCAN_WINDOW_CHARS, overlap=overlap, within=within
    )
    code = _git_stream(
        argv,
        repo=repo,
        private_dir=private_dir,
        logs_dir=logs_dir,
        slug=slug,
        timeout_seconds=timeout_seconds,
        logger=logger,
        consume=scanner.feed,
    )
    hit = scanner.close()
    if code == 0 and hit is None and new_rev is not None and git is not None:
        hit = _added_inside_a_private_key(
            scanner.added, git=git, rev=new_rev, repo=repo, private_dir=private_dir,
            logs_dir=logs_dir, timeout_seconds=timeout_seconds, logger=logger,
        )
    return code, hit


is_test_path = _credentials.is_test_path


HEADERLESS_KEY_RULE = _credentials.HEADERLESS_KEY_RULE


GENERIC_CREDENTIAL_RULES = _credentials.GENERIC_CREDENTIAL_RULES


CREDENTIAL_MIN_CHARS = _credentials.CREDENTIAL_MIN_CHARS


CREDENTIAL_MIN_ENTROPY_BITS = _credentials.CREDENTIAL_MIN_ENTROPY_BITS


_looks_like_a_credential = _credentials._looks_like_a_credential


def _first_leaking_commit(
    *,
    shas: list[str],
    keep: str | None,
    leaks: LeakPredicate,
    git: list[str],
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    overlap: int = SCAN_OVERLAP_CHARS,
    floor: str = "",
    upstream: str = "",
) -> int | None:
    """The 1-based index of the first agent commit whose ADDED text `leaks`, or None.

    `floor` is the clone base the first commit must sit on ("" for an empty
    repository, whose first commit has no parent). A commit whose first
    parent is not the entry before it in `shas` (or `floor`) is returned as
    a hit too: the list is not the chain the replay would write.

    Each commit is diffed against the tree it will sit on once replayed --
    the entry before it, which the chain check makes its first parent (the
    empty tree for a parentless first commit) -- with `--text` so a file git would call
    binary is still shown byte for byte, and `--no-renames` so a moved file's
    content is shown rather than only its new name. `leaks` is asked about
    what each file ADDS, `+` removed: a removed line was in the parent's tree
    already. The worker's own `keep` commit is skipped here; its tree is the
    final tree, which `final_tree_leak` scans before any push.

    `upstream` is the default-branch commit the lane merged, proven on the
    forge (`Worker._scan_floor`, archived issue 453), or "". A merge whose second parent is
    that commit or one of its ancestors is diffed against its second parent:
    what it adds over a published tree, so `main`'s own lines never fold the
    lane's history. Every other commit, a merge of anything else included,
    is diffed against its first parent as before.
    """

    def run(argv: list[str], slug: str, cap: int = 4 * 1024 * 1024) -> tuple[int, str, bool]:
        return _git_text_full(
            argv,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
            max_bytes=cap,
        )

    empty_tree: str | None = None
    for index, sha in enumerate(shas, 1):
        code, raw, _ = run([*git, "cat-file", "commit", sha], "publish-scan-read")
        if code != 0:
            raise GitError(f"could not read commit {sha[:12]}")
        header = raw.partition("\n\n")[0]
        parents = [line[len("parent "):].strip() for line in header.split("\n") if line.startswith("parent ")]
        # EACH COMMIT IS SCANNED AGAINST THE TREE IT WILL ACTUALLY SIT ON
        # (archived pull request 259 re-review, grafts). The replay writes commit i on the
        # rewritten commit i-1 (or the base for the first), so the diff that
        # matters is against THAT, and it is the same as the commit's own
        # first parent only when the list is an unbroken chain. A list that
        # skips a commit -- `.git/info/grafts`, which `rev-list` and
        # `merge-base` follow and `GIT_GRAFT_FILE=/dev/null` now switches off
        # (`gitops._git_env`) -- would have scanned each commit against a
        # parent the push never sends, and shipped a skipped commit's secret
        # in the next kept tree. A broken chain folds: the index is returned.
        expected = shas[index - 2] if index > 1 else (floor or None)
        actual = parents[0] if parents else None
        if actual != expected:
            return index
        if sha == keep:
            continue
        if expected is not None:
            before = expected
            if upstream and len(parents) == 2:
                code, _, _ = run(
                    [*git, "merge-base", "--is-ancestor", parents[1], upstream],
                    "publish-scan-upstream",
                )
                if code == 0:
                    before = parents[1]
        else:
            if empty_tree is None:
                code, made, _ = run([*git, "hash-object", "-t", "tree", "/dev/null"], "publish-scan-empty")
                empty_tree = made.strip()
                if code != 0 or not _SHA_RE.match(empty_tree):
                    raise GitError("could not name the empty tree")
            before = empty_tree
        # EVERY BYTE OF THE DIFF IS SCANNED, AND NONE IS STORED (owner
        # decision, 2026-09-29): streamed from git through a pipe into
        # overlapping windows (`_DiffLeakScanner`), so a big diff is neither
        # refused for its size nor scanned only in part, and no file holds
        # the value it was scanned for.
        code, hit = _scan_diff_stream(
            [
                *git, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "--text", "--no-renames", "--submodule=short", "--src-prefix=a/",
                "--dst-prefix=b/", "-U0", before, sha, "--",
            ],
            leaks=leaks,
            overlap=overlap,
            new_rev=sha,
            git=git,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="publish-scan-diff",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        if code != 0:
            raise GitError(f"could not diff commit {sha[:12]} against its parent")
        if hit is not None:
            return index
    return None


def _commits_leak(
    shas: list[str],
    *,
    leaks: LeakPredicate,
    git: list[str],
    repo: Path,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    overlap: int = SCAN_OVERLAP_CHARS,
) -> bool:
    """Whether any of `shas` ADDS text `leaks` finds, each diffed against its
    own first parent (the empty tree for a root).

    The scan of a branch the agent merged (`gitops.merged_head_kept`, archived issue 3359
    review): kept as a parent, each of its commits is pushed as written, so
    each is read, as `_first_leaking_commit` reads HEAD's own line.
    """
    empty_tree: str | None = None
    for sha in shas:
        code, listed = _git_text(
            [*git, "rev-list", "--parents", "-n", "1", sha], repo=repo,
            private_dir=private_dir, logs_dir=logs_dir, slug="publish-merged-scan-parents",
            timeout_seconds=timeout_seconds, logger=logger,
        )
        parents = listed.split()
        if code != 0 or not parents or parents[0] != sha:
            raise GitError(f"could not read the parents of merged commit {sha[:12]}")
        if len(parents) > 1:
            before = parents[1]
        else:
            if empty_tree is None:
                code, made = _git_text(
                    [*git, "hash-object", "-t", "tree", "/dev/null"], repo=repo,
                    private_dir=private_dir, logs_dir=logs_dir, slug="publish-merged-scan-empty",
                    timeout_seconds=timeout_seconds, logger=logger,
                )
                empty_tree = made.strip()
                if code != 0 or not _SHA_RE.match(empty_tree):
                    raise GitError("could not name the empty tree")
            before = empty_tree
        code, hit = _scan_diff_stream(
            [
                *git, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "--text", "--no-renames", "--submodule=short", "--src-prefix=a/",
                "--dst-prefix=b/", "-U0", before, sha, "--",
            ],
            leaks=leaks,
            overlap=overlap,
            new_rev=sha,
            git=git,
            repo=repo,
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug="publish-merged-scan-diff",
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
        if code != 0:
            raise GitError(f"could not diff merged commit {sha[:12]} against its parent")
        if hit is not None:
            return True
    return False


def final_tree_leak(
    *,
    repo: Path,
    base: str | None,
    leaks: LeakPredicate,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    overlap: int = SCAN_OVERLAP_CHARS,
    floor: str | None = None,
) -> str | None:
    """Why the branch about to be pushed must not be, or None when it may.

    OWNER DECISION, 2026-09-28 (archived pull request 259 review, M4). The per-commit scan
    (`_first_leaking_commit`) decides only between keeping the agent's
    commits and folding them; it skips the worker's own commit of
    uncommitted work, and a fold pushes the final tree unscanned. So a key
    the agent committed and never deleted -- or wrote and never committed --
    reached the forge in the one commit the worker wrote. This reads what
    the push would add, `base..HEAD` diffed as one span AFTER the replay or
    the fold, so it covers every commit's tree the push carries: each kept
    commit is scanned by the per-commit pass and the final tree here.

    `leaks` is asked about the text each FILE adds, with the `+` removed, so
    a line the branch removes -- the repository's own, already public --
    never refuses it. The answer names the FILE and never the value: the
    reason becomes the attempt's error, which the retry and a human both
    read. The whole diff is scanned however big it is, streamed in
    overlapping windows (`_DiffLeakScanner`; owner decision, 2026-09-29):
    size alone never refuses a publish, and no file holds the diff.

    `repo` is the worker's clean repository (`gitops.prepare_publish_repo`,
    owner decision 2026-09-29): the agent's objects were fetched into it with
    object checking, and it holds no commit-graph, grafts, shallow file,
    replace refs or config of the agent's -- a forged commit-graph entry made
    `git diff` in the clone compare a tree the push never sends. The
    `GIT_NO_REPLACE_OBJECTS=1`, `GIT_GRAFT_FILE` and `core.commitGraph=false`
    of `gitops._git_env`, and the explicit `--src-prefix`/`--dst-prefix` and
    `core.quotePath=false`, stay as a second belt.

    `floor`, when given, is a commit of the repository's default branch the
    lane merged, proven on the forge (`Worker._scan_floor`, archived issue 453). Every line
    its tree holds is published already. So is every line of the clone base's
    tree: `base` is the commit the worker recorded when it cloned from the
    forge (`Worker._publish_base`), never a value the agent names. So only a
    line the branch adds over BOTH is asked about (publish scan, merged-main
    floor, 2026-11-10): `floor..HEAD` is read first for the line ranges it
    adds, and `base..HEAD` is then scanned on those lines alone
    (`_DiffLeakScanner`'s `within`). The floor used to REPLACE the base, so on
    a stacked branch -- whose base is a parent branch already on the forge,
    holding lines the default branch does not -- every such line was scanned
    again as if the lane had added it, and the lane was refused for a
    parent's line it never wrote. What is asked is never more than either
    diff alone would ask.
    """
    g = [git_binary, *_NO_HOOKS, "-c", "core.quotePath=false"]
    run_kwargs: dict[str, Any] = {
        "repo": Path(repo),
        "private_dir": private_dir,
        "logs_dir": logs_dir,
        "timeout_seconds": timeout_seconds,
        "logger": logger,
    }
    code, _head, _ = _git_text_full(
        [*g, "rev-parse", "--verify", "--quiet", "HEAD"], slug="publish-final-head", **run_kwargs
    )
    if code != 0:
        # Nothing is committed, so the push has nothing to send.
        return None
    if base == EMPTY_CLONE_BASE:
        code, made, _ = _git_text_full(
            [*g, "hash-object", "-t", "tree", "/dev/null"], slug="publish-final-empty", **run_kwargs
        )
        before = made.strip()
        if code != 0 or not _SHA_RE.match(before):
            raise GitError("could not name the empty tree")
    elif base and _SHA_RE.match(base.strip()):
        before = base.strip()
    else:
        raise GitError(
            "the clone base is unknown, so the worker cannot read what the push "
            "would add; nothing was pushed"
        )

    def diff(old: str) -> list[str]:
        return [
            *g, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text",
            "--no-renames", "--submodule=short", "--src-prefix=a/", "--dst-prefix=b/", "-U0",
            old, "HEAD", "--",
        ]

    within: dict[str, list[tuple[int, int]]] | None = None
    if floor and _SHA_RE.match(floor.strip()) and floor.strip() != before:
        # What the branch adds over the floor, as line ranges only: the
        # predicate asks nothing, and no added text is kept.
        ranges = _DiffLeakScanner(
            lambda _path, _text: None, window=SCAN_WINDOW_CHARS, overlap=overlap
        )
        code = _git_stream(
            diff(floor.strip()), slug="publish-final-floor-diff", consume=ranges.feed,
            **run_kwargs,
        )
        ranges.close()
        if code != 0:
            raise GitError("could not diff the branch against the merged default branch")
        within = ranges.added
    code, hit = _scan_diff_stream(
        diff(before),
        leaks=leaks,
        overlap=overlap,
        new_rev="HEAD",
        git=g,
        slug="publish-final-diff",
        within=within,
        **run_kwargs,
    )
    if code != 0:
        raise GitError("could not diff the branch against the clone base")
    if hit is not None:
        # What the refusal says, and why: `credential_refusal`.
        return credential_refusal(hit)
    return None


#: What a refusal and the repair turn tell the agent to do (SCAN-FP, archived issue 2030;
#: the owner's decision, 2026-10-23). NEVER to split, join or encode the literal:
#: that defeats the scan rather than removing the value, and some
#: repositories forbid it, which left a peer application's agent no way through.
CREDENTIAL_ADVICE = "remove the value, or read it at runtime from configuration"


def credential_reason(rule: str, path: str) -> str:
    """Why a line `rule` matched in the file at `path` looked like a
    credential, as the refusal and the repair turn say it (archived issue 2030). Fixed text
    by the rule and the path's tier: never any part of the value."""
    in_tests = is_test_path(path)
    if rule == REGISTERED_SECRET_RULE:
        return "it is the value of a secret registered for this task"
    if rule == "private_key_block":
        return "it is a private key block"
    if rule == HEADERLESS_KEY_RULE:
        return "it is private key material with no BEGIN or END marker"
    if rule == "jwt":
        return "it is a JSON Web Token: its header decodes and names an algorithm"
    if rule in GENERIC_CREDENTIAL_RULES:
        if in_tests:
            return (
                "it is a literal given to a credential's name, at least "
                f"{CREDENTIAL_MIN_CHARS} characters of letters and digits with at least "
                f"{CREDENTIAL_MIN_ENTROPY_BITS} bits of entropy per character"
            )
        return (
            "it is a literal given to a credential's name, and not a reference: not a "
            "${name} slot, one lowercase word, or a lowercase hyphen-joined name ending "
            "-token, -api-key, -secret or -git"
        )
    if in_tests:
        return (
            f"it matches the {rule} vendor format, with no placeholder word "
            "(example, test, fake, dummy) or run of x in it"
        )
    return f"it matches the {rule} vendor format"


def credential_refusal(hit: ScanHit) -> str:
    """The final-tree scan's refusal for `hit`: the file, the rule, the line,
    why it looked like a credential, and `CREDENTIAL_ADVICE`.

    The rule and the line tell the retry what to remove (archived issue 373); the value is
    never in it, and the caller scrubs the reason anyway. The PATH is the
    agent's choice, so it is shown masked and cut (`_reason_path`, archived issue 361 box
    71); the reason is judged on the real path. The opening words stay whole:
    swarm-api's outcome classifier (`outcomes._PUBLISH_REFUSED_RE`) reads them.
    """
    reason = credential_reason(hit.rule, hit.path)
    hit = hit._replace(path=_reason_path(hit.path))
    # One literal, whole: swarm-api's outcome classifier matches it as one.
    return (
        f"the final tree adds a credential in {hit.path} (rule "
        f"{hit.rule}, line {hit.line}): {reason}; {CREDENTIAL_ADVICE}"
    )


#: The longest a refusal shows a file's path, in characters (archived issue 361 box 71).
REASON_PATH_MAX_CHARS = 160


#: A run of token characters in a path, judged as a possible credential.
_PATH_TOKEN = re.compile(r"[A-Za-z0-9+=_\-]{16,}")


def _reason_path(path: str) -> str:
    """`path` as a refusal may show it (archived issue 361 box 71, security pass 2026-10-09).

    The path is the AGENT's choice, and the reason reaches the task's error,
    its result summary and the log, where the caller's scrub replaces only
    REGISTERED secrets: a file named after a token it minted was echoed
    whole. So the shown path has every non-printable character replaced, every
    credential-shaped run masked -- `swarm_redaction.redact`'s rules (a
    `ghp_` name) and any run `_looks_like_a_credential` accepts (a random
    directory name) -- and is cut to `REASON_PATH_MAX_CHARS`, keeping the
    end, where the file's own name is.
    """
    shown = "".join(c if c.isprintable() else "?" for c in path) or "a file"
    shown = redact_text(shown).text
    shown = _PATH_TOKEN.sub(
        lambda m: CREDENTIAL_MASK if _looks_like_a_credential(m.group()) else m.group(), shown
    )
    if len(shown) > REASON_PATH_MAX_CHARS:
        shown = "..." + shown[-(REASON_PATH_MAX_CHARS - 3) :]
    return shown


def replay_agent_commits(
    *,
    repo: Path,
    base: str | None,
    keep: str | None,
    task_id: str,
    scrub: Callable[[str], str],
    author_name: str,
    author_email: str,
    private_dir: Path,
    logs_dir: Path,
    timeout_seconds: int,
    logger: Any,
    git_binary: str = "git",
    leaks: LeakPredicate | None = None,
    overlap: int = SCAN_OVERLAP_CHARS,
    upstream: str = "",
    on_forge: Callable[[str], bool] | None = None,
) -> int | None:
    """Rewrite each commit on HEAD's first-parent line since `base` as the worker's (archived issue 242).

    Returns how many of the AGENT's commits were kept -- `keep`, the worker's
    own commit from `commit_dirty`, is rewritten too and not counted -- or
    None when this history cannot be kept commit by commit, and the caller
    folds it as before (`gitops.fold_agent_commits`):

    * HEAD does not descend from `base` (the agent reset or checked out
      another line): nothing tells its commits from the repository's. Since
      archived issue 2094 the harvest publishes nothing for such a HEAD before this runs
      (`Worker._head_moved`), so this is the fold's last line of defence;
    * more than `MAX_KEPT_COMMITS` of them;
    * `leaks` finds a credential in the text any agent commit ADDS to a file
      (archived pull request 259 review), read whole in windows overlapping by `overlap`. A kept commit keeps its TREE, so a key the agent added
      in one commit and deleted in the next would be pushed in the first
      one's tree even though the final tree is clean -- and a pushed object
      on a public forge is published for good. The fold pushes only the
      final tree. Only the offending commit's index is logged, never any of
      its content. A diff of any size is scanned whole (owner decision,
      2026-09-29), so size alone never folds a history.

    WHAT IS KEPT, AND WHAT IS NOT. Each commit keeps its TREE, exactly -- so
    the tests-only commit of a red-first change is still tests only, and CI
    can run it -- and its message, cleaned (`clean_commit_message`) and
    ended with the worker's line, "Made by the worker for task <id>." (archived issue 2505
    review: the merge step reads that line to tell a platform head from a
    person's push). Its
    author, committer, dates and signature are the worker's: the commit is
    made with `git commit-tree` under `_worker_identity`, and archived pull request 219's rule that
    the worker writes every commit it pushes holds, checked again at the push
    by `verify_worker_authorship`. A merge the agent made keeps its merged
    tree, and keeps each parent after the first that
    `_published_merge_parents` keeps: `upstream` or one of its ancestors --
    a commit of the default branch proven on the forge
    (`Worker._scan_floor`), already published (archived issue 1321) -- or any other branch
    the base does not contain whose own commits carry no Claude attribution,
    such as a merge train's branch (archived issue 3359). A side branch the agent committed
    on as Claude is dropped: every commit on that side would otherwise be
    pushed as it was written. Dropping main's parent was archived issue 1321: a conflict
    fix round's `git merge origin/main` reached the forge with one parent,
    GitHub kept the old merge base and the pull request still conflicted;
    dropping train/an's was archived issue 3359, every conflict the lane resolved raised
    again when its pull request merged back. A parent `on_forge` says the
    forge holds is kept whatever its commits say: it is published already
    (archived issue 3359 again, 6 November 2026, a train branch with one attributed
    commit made outside the platform).

    The index and working tree are not touched; `reset --soft` moves the
    branch onto the rewritten line, which ends in the same tree HEAD had.
    An unknown `base` refuses, exactly as the fold does.

    `repo` is the worker's clean repository, never the agent's clone
    (`gitops.prepare_publish_repo`, owner decision 2026-09-29): the list, the
    scan and the rewrite read only objects fetched with object checking, and
    none of the agent's grafts, shallow file, replace refs, commit-graph or
    config.
    """
    empty = base == EMPTY_CLONE_BASE
    if not empty and (not base or not _SHA_RE.match(base.strip())):
        raise GitError(
            "the clone base is unknown, so the worker cannot tell the agent's "
            "commits from the repository's; nothing was pushed rather than "
            "commits whose author and message the worker did not write"
        )
    floor = "" if empty else (base or "").strip()
    g = [git_binary, *_NO_HOOKS, *_worker_identity(author_name, author_email)]

    def run(argv: list[str], slug: str) -> tuple[int, str]:
        return _git_text(
            argv,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            slug=slug,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )

    if empty:
        code, _ = run([*g, "rev-parse", "--verify", "--quiet", "HEAD"], "publish-keep-born")
        if code != 0:
            return 0
        span = "HEAD"
    else:
        code, _ = run([*g, "merge-base", "--is-ancestor", floor, "HEAD"], "publish-keep-descends")
        if code == 1:
            logger.warning(
                "the agent's branch does not descend from the clone base; its "
                "commits are folded into one worker commit"
            )
            return None
        if code != 0:
            raise GitError("could not tell whether the agent's work descends from the clone base")
        span = f"{floor}..HEAD"

    code, text = run([*g, "rev-list", "--first-parent", "--reverse", span], "publish-keep-list")
    if code != 0:
        raise GitError("could not list the commits made since the clone base")
    shas = text.split()
    if not shas:
        return 0
    if len(shas) > MAX_KEPT_COMMITS:
        logger.warning(
            "the agent made more commits than are kept one by one; they are "
            "folded into one worker commit",
            commits=len(shas),
            cap=MAX_KEPT_COMMITS,
        )
        return None

    if leaks is not None:
        index = _first_leaking_commit(
            shas=shas,
            keep=keep,
            leaks=leaks,
            overlap=overlap,
            floor=floor,
            git=g,
            repo=Path(repo),
            private_dir=private_dir,
            logs_dir=logs_dir,
            timeout_seconds=timeout_seconds,
            logger=logger,
            upstream=upstream,
        )
        if index is not None:
            logger.warning(
                "an agent commit's diff carries a registered secret; the agent's "
                "commits are folded into one worker commit of the final tree",
                commit_index=index,
                commits=len(shas),
            )
            return None

    # A merged branch kept as a parent is pushed commit by commit, and the
    # scan above reads only HEAD's first-parent line: its commits are scanned
    # too, and a branch one of them leaks from is not kept (archived issue 3359 review).
    history_leaks: Callable[[list[str]], bool] | None = None
    if leaks is not None:
        scan_leaks = leaks
        history_leaks = lambda commits: _commits_leak(  # noqa: E731
            commits, leaks=scan_leaks, overlap=overlap, git=g, repo=Path(repo),
            private_dir=private_dir, logs_dir=logs_dir,
            timeout_seconds=timeout_seconds, logger=logger,
        )

    parent = floor or None
    kept = 0
    for index, sha in enumerate(shas, 1):
        code, raw = run([*g, "cat-file", "commit", sha], "publish-keep-read")
        if code != 0:
            raise GitError(f"could not read commit {sha[:12]}")
        header, _, message = raw.partition("\n\n")
        trees = [line[len("tree "):] for line in header.split("\n") if line.startswith("tree ")]
        tree = trees[0].strip() if trees else ""
        if not _SHA_RE.match(tree):
            raise GitError(f"could not read the tree of commit {sha[:12]}")
        merged = _published_merge_parents(
            run, g, header, upstream=upstream, base=floor, logger=logger,
            history_leaks=history_leaks, on_forge=on_forge,
        )
        if sha == keep:
            # The worker's own message, from `commit_dirty`.
            text = message.replace("\x00", "").strip()
            text = text or UNCOMMITTED_COMMIT_SUBJECT
        else:
            kept += 1
            text = clean_commit_message(
                message,
                scrub=scrub,
                fallback=f"swarm: commit {index} of {len(shas)} from {task_id}",
            )
            # Every commit the worker pushes ends with its line (archived issue 2505
            # review): the committer is the dispatcher since archived issue 765, so this
            # line is all that tells the merge step a kept agent commit is
            # the platform's (`merge.WORKER_COMMIT_LINE`), not a person's push.
            text = f"{text}\n\nMade by the worker for task {task_id}."
        argv = [*g, "commit-tree", tree]
        if parent:
            argv += ["-p", parent]
        for other in merged:
            argv += ["-p", other]
        argv += ["-m", text]
        code, made = run(argv, "publish-keep-write")
        made = made.strip()
        if code != 0 or not _SHA_RE.match(made):
            raise GitError(f"could not rewrite commit {sha[:12]} as the worker's")
        parent = made
    code, _ = run([*g, "reset", "--soft", parent or ""], "publish-keep-reset")
    if code != 0:
        raise GitError("could not move the branch onto the worker's rewritten commits")
    logger.info(
        "kept the agent's commits, each rewritten as the worker's",
        kept=kept,
        rewritten=len(shas),
    )
    return kept


def _published_merge_parents(
    run: Callable[[list[str], str], tuple[int, str]],
    g: list[str],
    header: str,
    *,
    upstream: str,
    base: str,
    logger: Any,
    history_leaks: Callable[[list[str]], bool] | None = None,
    on_forge: Callable[[str], bool] | None = None,
) -> list[str]:
    """The merge parents of a commit (its `header`) the replay keeps (archived issue 1321, archived issue 3359).

    Every parent after the first that `gitops.merged_head_kept` keeps, the
    fold's rule too: one `base` does not already contain, and either `upstream`
    or one of its ancestors -- the default branch's commit the lane merged,
    proven on the forge (`Worker._scan_floor`) -- or a branch whose own
    commits carry no Claude attribution, such as the `origin/train/an` a lane
    merged into its pull request. Dropping that parent pushed train/an's tree
    without its history, and merging the pull request back raised every
    conflict the lane had resolved again (archived issue 3359). A side branch the agent
    committed on as Claude is still dropped: its commits would be pushed as
    they were written. So is one `history_leaks` finds a credential in --
    unless `on_forge` says the forge holds it already.
    """
    parents = [line[len("parent "):].strip() for line in header.split("\n")
               if line.startswith("parent ")]
    kept: list[str] = []
    for other in parents[1:]:
        if not _SHA_RE.match(other):
            continue
        if merged_head_kept(run, g, other, base=base, upstream=upstream, logger=logger,
                            history_leaks=history_leaks, on_forge=on_forge):
            kept.append(other)
    return kept


def _recheck_runner_input(runner_profile: str, stored: Any) -> dict[str, Any] | None:
    """Refuse a stored `input` its profile's declaration refuses. Every profile.

    The same rule swarm-api and the plugin's bridge apply at submission,
    `swarm_common.profiles.check_inputs`, asked of the input as stored, without
    its `prompt` (which every profile takes and no declaration names). A
    refusal is `ConfigError`, which exits 78, "cannot start": the task fails
    with `EndCause.CANNOT_START`, and the reconciler does not retry it, because
    every retry would read the same document and be refused the same way.

    NO PROFILE IS EXEMPT (the owner, on archived pull request 345, 2026-09-29). The mock reads keys
    no caller may send -- `spend`, `provider`, `credential_revoked_times` and
    the others with which it acts out a provider -- and the worker's unit suite
    used to write them into the stored input. It hands them to the runner
    through a test-only seam now (`simulated`, in the worker suite's setup),
    so a mock task is held to its declaration like any other.

    The message names the key and the bound, never the value: a refused URL
    may carry `user:password@`, and this text is stored as the task's error.

    Returns the input AS CHECKED -- the stored map with every declared key
    replaced by `check_inputs`' normalised value, the prompt kept -- or None
    when nothing is stored (contract request 58).
    """
    profile = RUNNER_PROFILES[runner_profile]
    raw = stored if stored is not None else {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"the task's input is a {type(raw).__name__}, not an object; runner profile "
            f"{runner_profile!r} cannot read it (checked again by the worker, contract "
            "request 32)"
        )
    try:
        # `prompt` and `territory` are the platform's, taken by every profile
        # and named by no declaration (`territory_mod`, TERRITORY-WARN).
        checked = check_inputs(
            profile,
            {
                key: value for key, value in raw.items()
                if key not in ("prompt", territory_mod.TERRITORY_INPUT_KEY)
            },
        )
    except InputRefused as refused:
        bound = f"; expected {refused.expected}" if refused.expected else ""
        raise ConfigError(
            f"the task's input is refused by runner profile {runner_profile!r}: "
            f"{_refused_key_names(refused.keys)}{bound} (checked again by the worker, "
            "contract request 32)"
        ) from None
    if stored is None:
        return None
    return {**raw, **checked}


#: The most refused key names `_recheck_runner_input`'s message lists, and the
#: most characters of each (archived issue 346). The names are the CALLER'S: a stored input
#: of 10,000 keys of 10,000 characters each made a 100 MB task error.
_REFUSED_NAMES_SHOWN = 10


_REFUSED_NAME_CHARS = 64


def _refused_key_names(keys: Sequence[str]) -> str:
    """`keys` for an error message: the first `_REFUSED_NAMES_SHOWN`, each cut
    to `_REFUSED_NAME_CHARS` characters, then "and N more" for the rest."""
    shown = [
        key if len(key) <= _REFUSED_NAME_CHARS else key[:_REFUSED_NAME_CHARS] + "..."
        for key in (str(key) for key in keys[:_REFUSED_NAMES_SHOWN])
    ]
    rest = len(keys) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest > 0 else "")


def _error_chain(exc: BaseException) -> list[BaseException]:
    """`exc`, then what it was raised from or during (`__cause__`, `__context__`).

    Breadth first, bounded at 16, and cycle-safe. A library can raise
    something else on the way up: Firestore's rollback of a transaction whose
    begin failed raises a ValueError whose `__context__` is the RetryError,
    and google-auth raises RefreshError FROM the TransportError.
    """
    seen: set[int] = set()
    chain: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)
    return chain


def _unavailable_cause(exc: BaseException) -> BaseException | None:
    """The error in `exc`'s chain that says a dependency could not be reached, or None.

    UNAVAILABLE ONLY, not every Google API error. Counted: RetryError (a
    budget ran out), EVERY ServerError, RESOURCE_EXHAUSTED and 429, ABORTED
    (contention that outlasted the transaction's own retries), CANCELLED, and
    google-auth's transport and timeout errors or any it marks retryable.
    These say the call did not complete, and the next attempt may find the
    service back.

    EVERY ServerError, not a list of them: UNAVAILABLE, DEADLINE_EXCEEDED,
    INTERNAL, 502 and 504, and also gRPC UNKNOWN, DATA_LOSS and UNIMPLEMENTED.
    The first version named five and left those three out. UNKNOWN is what
    Firestore's client raises when a stream is reset under it ("Stream
    removed"), and the startup Retry does not retry it. So a stream reset
    FAILED the task for good after the generation check, and ended it through
    a 78 at the check (review of archived pull request 59). Each of the three could be permanent,
    UNIMPLEMENTED above all. But a wrong guess here costs one bounded retry,
    and the other wrong guess ends the task. CANCELLED is the RPC being
    dropped, not an answer.

    Not counted: PERMISSION_DENIED, NOT_FOUND, INVALID_ARGUMENT,
    FAILED_PRECONDITION, UNAUTHENTICATED and a non-retryable RefreshError.
    Those are answers: the service was reached and said no, and it will say no
    to the next attempt too. The attempt owns its task at this point (step 1
    passed), so it fails it with the reason, as it did before the startup
    budget. Step 1 is different, and `_exit_control_plane_unreachable` turns
    anything that is not a named refusal (`_refusal_cause`) into a retry:
    without the generation check, the attempt does not know that the task is
    its to fail.

    The whole chain is searched (`_error_chain`).
    """
    try:
        from google.api_core import exceptions as core
    except ImportError:
        return None
    transient: tuple[type[BaseException], ...] = (
        core.RetryError,
        # UNAVAILABLE, DEADLINE_EXCEEDED, INTERNAL, UNKNOWN, DATA_LOSS,
        # UNIMPLEMENTED, 502, 504: every 5xx and every server-side gRPC code.
        core.ServerError,
        core.TooManyRequests,  # ResourceExhausted is a subclass
        core.Aborted,
        core.Cancelled,
    )
    try:
        from google.auth import exceptions as auth
    except ImportError:
        auth = None  # type: ignore[assignment]
    auth_transient: tuple[type[BaseException], ...] = ()
    if auth is not None:
        auth_transient = tuple(
            kind
            for kind in (getattr(auth, "TransportError", None), getattr(auth, "TimeoutError", None))
            if isinstance(kind, type)
        )

    for current in _error_chain(exc):
        if isinstance(current, transient):
            return current
        if auth is not None and isinstance(current, auth.GoogleAuthError):
            if isinstance(current, auth_transient) or getattr(current, "retryable", False):
                return current
    return None
