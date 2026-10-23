"""Which files in `$SWARM_ARTIFACTS_DIR` go into the manifest, and in what order (#228).

Found by reading on 2026-09-26, while fixing PR #225's review findings:
`Worker._upload_outputs` put one entry, `{"name", "bytes", "uri"}`, into
`result_summary.artifacts` for EVERY file in the artifacts folder that fitted
`max_artifact_bytes`. Nothing capped how many. `result_summary` is a field of
the task's Firestore document, and Firestore limits a document to 1 MiB, so an
agent that left about 6,000 small files there (a dataset split into parts, a
screenshot per step, a package tree installed into the folder) would have
taken the document past it: `control.finish` refused, `_safe_finish` refused
again, and the task lost a result that may well have succeeded. Since #225
every claude-code and codex prompt names the folder, so more agents write
there.

THE OWNER'S DECISION, 2026-09-26, recorded on #228: at most 500 files are
uploaded from the folder per attempt. The rest are not uploaded; they are
counted in `result_summary.artifacts_over_cap`, and every one of their names
goes to the worker's log, 100 names per WARNING line -- the shape #225 gave the
working-folder cap, through the same log helper (`Worker._log_not_uploaded`).

WHAT IS NOT CHANGED: the manifest still lists the uploaded files sorted by
name, as it always has, so the Artifacts tab and the listing route's page show
them in the same order. The order below decides only WHICH files are taken
(and which the byte cap reaches first), not how they are listed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from . import standalone_outputs as standalone

#: At most this many files are uploaded from `$SWARM_ARTIFACTS_DIR` per attempt.
#: The owner's number (#228, 2026-09-26). The default for
#: `WorkerConfig.max_artifact_files`.
MAX_FILES = 500

#: The longest manifest name, in bytes of UTF-8. A file whose path under the
#: artifacts folder is longer is not uploaded, and is named in the log with
#: `NAME_TOO_LONG`.
#:
#: WHY A BOUND, AND WHY 256. The count cap alone does not keep the document
#: under 1 MiB, because every entry carries its name twice -- as `name`, and
#: at the end of its `uri` -- and GCS allows an object name of 1,024 bytes. At
#: the longest ids a deployment makes (a 63-character bucket, an 11-character
#: tenant, `task_` and `att_` ids of 25 and 24 characters) an entry is
#: 190 + 2 * N bytes as Firestore counts them, N being the name's length. At
#: the GCS limit that is about 2,000 bytes, and 500 of them are 1 MB on their
#: own. At 256 an entry is at most 702 bytes. 500 of them would be 343 KiB,
#: but up to `MAX_DECLARED_NAMES` (49) are exempt from this bound (#232
#: review) and held only to the GCS limit instead, so the manifest's worst
#: case is 49 entries at about 2,238 bytes (107 KiB) plus 451 at 702 bytes
#: (309 KiB): about 416 KiB. What else the document can hold at the same
#: time, at its worst: the task's input (`max_input_bytes`, 256 KiB -- SEE THE
#: CAVEAT BELOW), #225's working-folder block (50 entries at the GCS limit,
#: about 100 KiB, and 50 listed as not uploaded, about 55 KiB),
#: `artifacts_skipped` and `redaction_skipped` (50 each, names cut short, about
#: 67 KiB together), and the runner envelope (about 12 KiB of ASCII). About
#: 906 KiB in all, leaving the rest of the task document -- its metadata, its
#: timestamps -- about 118 KiB. At 512 the manifest alone would be 593 KiB,
#: and the sum would pass 1 MiB.
#:
#: THE CAVEAT (#232 review, not fixed here -- it is the API's input-size
#: check, tracked on the wave epic): "the input costs at most `max_input_bytes`
#: in Firestore" assumes a roughly text-shaped input, where a byte of the
#: serialized JSON costs about a byte of Firestore string. Firestore counts a
#: number as a flat 8 bytes whatever its digit count, so an input dense with
#: small integers (a long list of small numbers, say) can cost MORE Firestore
#: bytes once decoded than the same count of bytes as text -- `max_input_bytes`
#: bounds the submitted bytes, not what they become once Firestore has parsed
#: them into native values. This budget is sized for the ordinary case, not a
#: proof for every input shape.
#:
#: 256 bytes is also a path, not a file name: Linux allows 255 bytes for one
#: component, so only a nested path can pass this.
#:
#: A name a later step DECLARES is EXEMPT from this bound (#232 review): it is
#: only put first (`upload_order`), not excused from upload. Holding it to the
#: bound too made a declared output over 256 bytes fail every attempt
#: ("written but not uploaded"), and `_publish_withheld` had by then already
#: published, so the retry's push was refused as a non-fast-forward -- worse
#: than #228 shipped with, so this restores the pre-#228 behaviour for
#: declared names. There are at most `max_workflow_steps - 1` = 49 of them for
#: any one task: each dependant's `input_from` maps one upstream step to one
#: filename, and a workflow has at most 50 steps including this one. Held only
#: to GCS's own object-name limit (`standalone.MAX_OBJECT_NAME_BYTES`, 1,024
#: bytes), the same way the pre-#228 code was, and the same way an upload that
#: is too long for GCS fails today: at upload time, counted in
#: `artifacts_skipped` like any other upload error, not before. 49 entries at
#: that length are about 107 KiB rather than the 34 KiB 49 entries at 256 bytes
#: would be -- see the revised total below.
MAX_NAME_BYTES = 256

#: At most this many declared names are exempt from `MAX_NAME_BYTES` above.
#: ENFORCED by `plan()` (#227): the first this many distinct declared names are
#: exempt, and any past them are held to `MAX_NAME_BYTES` like every other
#: file and returned in `Plan.declared_past_bound`, which the worker names in
#: a WARNING (`Worker._upload_outputs`, scrubbed before it is cut). A
#: workflow's step count should already bound them --
#: `swarm_common.config.Settings.max_workflow_steps` (50) minus this
#: step -- but the 1 MiB budget above is arithmetic on THIS number, so it is
#: held here rather than trusted to hold upstream.
#: tests/unit/worker/test_artifact_manifest_declared_bound.py fails when the
#: frozen `max_workflow_steps` default passes `MAX_DECLARED_NAMES + 1`: raise
#: this and redo the budget then, not before.
MAX_DECLARED_NAMES = 49

#: The reasons a file in the folder is named in the log as not uploaded.
#: "over cap" is the owner's wording, one spelling for both caps.
OVER_CAP = standalone.OVER_CAP
NAME_TOO_LONG = f"name is longer than the manifest's {MAX_NAME_BYTES} bytes"


def fits(name: str) -> bool:
    """True when `name` is short enough to be a manifest entry's name."""
    return len(name.encode("utf-8")) <= MAX_NAME_BYTES


def upload_order(files: Mapping[str, int], *, first: Sequence[str]) -> list[tuple[str, int]]:
    """The files in the order the caps are applied to them.

    `first`, in its own order, for each name present: the caller passes the
    names a later step's `expected_outputs` declares, and then the files the
    platform itself writes into the folder. Then every other file, shallowest
    first and then by path.

    WHY THIS ORDER:

    * a DECLARED name first, so a declared output is never the one dropped:
      a later step stages it by that exact name, and the attempt fails when it
      is missing (#149). There are few of them -- each dependant's
      `input_from` maps one upstream step to ONE filename, and a workflow has
      at most `max_workflow_steps` (50) steps -- so they always fit;
    * then the PLATFORM's files: the agent CLI's own stdout, stderr and
      transcript (`result_summary.agent_streams`, and the Artifacts tab's
      answer and transcript read them) and the harvest's patch. An agent's own
      file count must not push out what the tab is built on;
    * then the rest, SHALLOWEST FIRST AND THEN BY PATH, the rule #225 chose for
      the working folder, so a top-level report is not pushed out by a
      generated tree beside it. It is a total order on names, so the same
      folder always loses the same files.
    """
    head: list[tuple[str, int]] = []
    for name in dict.fromkeys(first):
        if name in files:
            head.append((name, files[name]))
    taken = {name for name, _ in head}
    rest = standalone.upload_order({name: size for name, size in files.items() if name not in taken})
    return head + rest


@dataclass(frozen=True)
class Plan:
    """What of the folder is uploaded, and what is not.

    `take` is the names to upload, in `upload_order`, at most the cap. The byte
    cap (`max_artifact_bytes`) and an upload error can still drop one of them;
    they are listed in `artifacts_skipped` as before.

    `unstorable` is each name that is not UTF-8, RAW (#232 review): no object
    can be named with it and no Firestore string can carry it (#225 review).
    The caller scrubs before it shows one -- `standalone.shown(self._scrub(name))`,
    the same order the other three sites in `lifecycle.py` use -- because a
    name cut to `standalone.shown`'s length FIRST can sever a registered
    secret at the cut and leave its leading fragment unscrubbed. `not_uploaded`
    is `{"name", "bytes", "reason"}` for every
    file past the cap (`OVER_CAP`) or over the name bound (`NAME_TOO_LONG`),
    with its WHOLE name, for the log. Neither takes a place under the cap. A
    DECLARED name is never given `NAME_TOO_LONG` (#232 review, `plan`'s
    `declared`): it can still be `OVER_CAP`, or fail to upload for its own
    reasons, which is a different list (`Worker._written_not_uploaded`).
    """

    take: tuple[str, ...] = ()
    unstorable: tuple[str, ...] = ()
    not_uploaded: tuple[dict[str, Any], ...] = ()
    #: Declared names past `MAX_DECLARED_NAMES`, in the order given: NOT exempt
    #: from `MAX_NAME_BYTES` (#227). Each is still uploaded when it fits.
    declared_past_bound: tuple[str, ...] = ()

    @property
    def over_cap(self) -> int:
        """How many files were past the cap: `result_summary.artifacts_over_cap`."""
        return sum(1 for entry in self.not_uploaded if entry["reason"] == OVER_CAP)


def plan(
    files: Mapping[str, int], *, first: Sequence[str], cap: int, declared: Sequence[str] = ()
) -> Plan:
    """Decide which of `files` (name -> size) are uploaded. See `upload_order`.

    `declared` (a later step's `expected_outputs`) is exempt from
    `MAX_NAME_BYTES`: see the constant's docstring for why that is safe. At
    most `MAX_DECLARED_NAMES` of them, the first in `declared`'s own order: the
    rest are held to the bound and returned in `Plan.declared_past_bound`.
    Pure, and it logs nothing: the names come from task metadata, and only the
    worker's logger scrubs registered secrets out of what it writes.
    """
    distinct = list(dict.fromkeys(declared))
    declared_names = frozenset(distinct[:MAX_DECLARED_NAMES])
    past_bound = tuple(distinct[MAX_DECLARED_NAMES:])
    take: list[str] = []
    unstorable: list[str] = []
    not_uploaded: list[dict[str, Any]] = []
    for name, size in upload_order(files, first=first):
        if not standalone.storable(name):
            # RAW, not `standalone.shown(name)`: the caller scrubs before it
            # cuts (#232 review). See `Plan.unstorable`'s docstring.
            unstorable.append(name)
        elif not fits(name) and name not in declared_names:
            not_uploaded.append({"name": name, "bytes": size, "reason": NAME_TOO_LONG})
        elif len(take) >= cap:
            not_uploaded.append({"name": name, "bytes": size, "reason": OVER_CAP})
        else:
            take.append(name)
    return Plan(
        take=tuple(take),
        unstorable=tuple(unstorable),
        not_uploaded=tuple(not_uploaded),
        declared_past_bound=past_bound,
    )


def add_late_entry(artifacts: list[dict[str, Any]], entry: Mapping[str, Any]) -> int:
    """Put a file the worker uploaded AFTER the folder upload into the manifest
    `artifacts`, in place, and return how many bytes the manifest's total grew by.

    Found on task_58815d231b2146689808 (#2023): a refused workflow publish runs
    after the folder upload has built the manifest, so the `workflow-lines.patch`
    it stores was in the bucket and in the step's error, and in no listing a
    following step reads. The manifest is still written once, by the finish;
    this only adds to the list the finish writes.

    `entry` is `{"name", "bytes", "uri"}`. An entry of the same name -- the
    agent's own file, uploaded under the same key and since overwritten -- is
    replaced, not listed twice. The list stays in PATH ORDER, the order the
    folder upload sorts it in. One entry the worker writes is not held to
    `MAX_FILES`, which bounds what an agent leaves: it is a short name well
    inside the document budget worked out at `MAX_NAME_BYTES`.
    """
    late = {"name": str(entry["name"]), "bytes": int(entry["bytes"]), "uri": str(entry["uri"])}
    replaced = 0
    for index in range(len(artifacts) - 1, -1, -1):
        if artifacts[index].get("name") == late["name"]:
            replaced += int(artifacts[index].get("bytes") or 0)
            del artifacts[index]
    artifacts.append(late)
    artifacts.sort(key=lambda item: str(item.get("name", "")).split("/"))
    return late["bytes"] - replaced


# ---------------------------------------------------------------------------
# A browser session copied into the workspace (LB-L4b, part of #1030)
# ---------------------------------------------------------------------------
#
# docs/design/live-browser.md section 5.5. The live browser's profile lives on
# a sidecar-only memory volume, never under `work/`. But the agent drives that
# browser, and it can write Playwright's `storageState()` into its workspace or
# copy a Chromium `Cookies` database there -- the signed-in session of the
# person who took control. The checkpoint (`CheckpointManager._add_entry`) and
# the output upload (`Worker._upload_copy`, `Worker._upload_workdir_outputs`)
# ask `browser_state_kind` of every file they are about to send, and a file
# with either shape stays in the pod and is recorded as `browser_state_refused`
# with its path.
#
# A SHAPE CHECK, NOT A PROOF, as the design says. A cookie re-encoded, zipped
# or split across files passes; the agent's instructions forbid that, and the
# residual is stated in the design rather than hidden. What the check must not
# do is refuse ordinary work, so it asks for the shape, not a word: a JSON
# file merely mentioning "cookies", or any SQLite database, passes.

#: The event and the reason, in the log and in `workdir_outputs.not_uploaded`.
#: The design's name for it (section 5.5): searched for, so never reworded.
BROWSER_STATE_REFUSED = "browser_state_refused"

#: What `browser_state_kind` found, for the log line.
STORAGE_STATE = "storage-state JSON"
COOKIE_LIST = "cookie list JSON"
COOKIE_DATABASE = "Chromium Cookies database"

#: JSON this size or under is parsed and its shape checked. A storage state is
#: kilobytes (cookies, plus localStorage per origin), so this is generous; a
#: larger file is judged from its head (`STATE_HEAD_BYTES`) instead.
STATE_PARSE_MAX_BYTES = 32 * 1024 * 1024

#: Read first, from every file: enough for SQLite's first page at its largest
#: page size (65,536), where Chromium's schema lives, and for the start of a
#: storage state too large to parse, where Playwright writes `cookies` first.
STATE_HEAD_BYTES = 1024 * 1024

#: A SQLite database, its rollback journal and its write-ahead log. The two
#: logs carry copies of the database's pages, cookies included.
_SQLITE_MAGICS = (
    b"SQLite format 3\x00",
    bytes.fromhex("d9d505f920a163d7"),
    bytes.fromhex("377f0682"),
    bytes.fromhex("377f0683"),
)

#: The names Chromium gives the database and its logs: refused on the name
#: and the SQLite magic alone, since a log need not hold the schema page.
_COOKIE_DATABASE_NAMES = frozenset({"Cookies", "Cookies-journal", "Cookies-wal"})

#: Chromium's table, as its schema in `sqlite_master` declares it. With
#: `host_key` beside it, which a table of baking recipes does not have.
_COOKIES_TABLE = re.compile(
    rb"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"'`\[]?cookies[\"'`\]]?\s*\(",
    re.IGNORECASE,
)

_UTF8_BOM = b"\xef\xbb\xbf"
_COOKIE_KEYS = frozenset({"name", "value", "domain"})


def _is_cookie(entry: Any) -> bool:
    return isinstance(entry, dict) and _COOKIE_KEYS <= entry.keys()


def browser_state_kind(name: str, handle: BinaryIO) -> str | None:
    """What browser state the file open as `handle` holds, or None.

    `name` is the file's own name, the last component of its path. `handle`
    is read from where it stands; the caller rewinds it if it reads it again.

    * SQLite (a database, a journal or a WAL) named as Chromium names its
      cookie store, or whose first page declares Chromium's `cookies` table:
      `COOKIE_DATABASE`, under any name.
    * A JSON object whose `cookies` is a list with an entry carrying `name`,
      `value` and `domain` -- Playwright's storage state, the design's shape
      -- or whose `cookies` and `origins` are both lists, which is Playwright's
      storage state with an empty jar and localStorage beside it:
      `STORAGE_STATE`. Over `STATE_PARSE_MAX_BYTES`, an object whose head
      names `cookies` with `domain` or `origins` is refused unparsed: failing
      closed on a file too large to read through costs a deliverable that
      looks like a session, not a session.
    * A JSON list of such entries, `context.cookies()` dumped as it is:
      `COOKIE_LIST`. The design names the object; the bare jar is the same
      session without its wrapper.
    """
    head = handle.read(STATE_HEAD_BYTES)
    if head.startswith(_SQLITE_MAGICS):
        if name in _COOKIE_DATABASE_NAMES:
            return COOKIE_DATABASE
        if _COOKIES_TABLE.search(head) and b"host_key" in head:
            return COOKIE_DATABASE
        return None
    start = head[len(_UTF8_BOM) :] if head.startswith(_UTF8_BOM) else head
    opener = start.lstrip(b" \t\r\n")[:1]
    if opener not in (b"{", b"["):
        return None
    data = head + handle.read(STATE_PARSE_MAX_BYTES + 1 - len(head))
    # Cheap before the parse: every shape below has a cookie's `domain` key or
    # Playwright's `origins`, so most JSON in a workspace is never parsed.
    if b'"domain"' not in data and b'"origins"' not in data:
        return None
    if len(data) > STATE_PARSE_MAX_BYTES:
        if opener == b"{" and b'"cookies"' in head:
            return STORAGE_STATE
        return None
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if isinstance(value, dict):
        cookies = value.get("cookies")
        if isinstance(cookies, list) and (
            any(_is_cookie(entry) for entry in cookies) or isinstance(value.get("origins"), list)
        ):
            return STORAGE_STATE
        return None
    if isinstance(value, list) and any(_is_cookie(entry) for entry in value):
        return COOKIE_LIST
    return None


def browser_state_of(path: Path, *, name: str) -> str | None:
    """`browser_state_kind` of the file at `path`, judged under `name`.

    For a copy in the worker's own scratch: the upload checks the very bytes
    it is about to send, so a file swapped after the check cannot pass it.
    """
    with open(path, "rb") as handle:
        return browser_state_kind(name, handle)
