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

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

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
#: own. At 256 an entry is at most 702 bytes, and 500 are 343 KiB. What else
#: the document can hold at the same time, at its worst: the task's input
#: (`max_input_bytes`, 256 KiB), #225's working-folder block (50 entries at
#: the GCS limit, about 100 KiB, and 50 listed as not uploaded, about 55 KiB),
#: `artifacts_skipped` and `redaction_skipped` (50 each, names cut short, about
#: 67 KiB together), and the runner envelope (about 12 KiB of ASCII). About
#: 833 KiB in all, leaving the rest of the task document -- its metadata, its
#: timestamps -- about 190 KiB. At 512 the manifest alone would be 593 KiB,
#: and the sum would pass 1 MiB.
#:
#: 256 bytes is also a path, not a file name: Linux allows 255 bytes for one
#: component, so only a nested path can pass this. A name a later step
#: DECLARES is held to it too (a declared name is only put first, not
#: excused), and a declared name over it fails the attempt as missing, named
#: in the log as written and not uploaded.
MAX_NAME_BYTES = 256

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

    `unstorable` is each name that is not UTF-8, as `standalone.shown` spells
    it: no object can be named with it and no Firestore string can carry it
    (#225 review). `not_uploaded` is `{"name", "bytes", "reason"}` for every
    file past the cap (`OVER_CAP`) or over the name bound (`NAME_TOO_LONG`),
    with its WHOLE name, for the log. Neither takes a place under the cap.
    """

    take: tuple[str, ...] = ()
    unstorable: tuple[str, ...] = ()
    not_uploaded: tuple[dict[str, Any], ...] = ()

    @property
    def over_cap(self) -> int:
        """How many files were past the cap: `result_summary.artifacts_over_cap`."""
        return sum(1 for entry in self.not_uploaded if entry["reason"] == OVER_CAP)


def plan(files: Mapping[str, int], *, first: Sequence[str], cap: int) -> Plan:
    """Decide which of `files` (name -> size) are uploaded. See `upload_order`."""
    take: list[str] = []
    unstorable: list[str] = []
    not_uploaded: list[dict[str, Any]] = []
    for name, size in upload_order(files, first=first):
        if not standalone.storable(name):
            unstorable.append(standalone.shown(name))
        elif not fits(name):
            not_uploaded.append({"name": name, "bytes": size, "reason": NAME_TOO_LONG})
        elif len(take) >= cap:
            not_uploaded.append({"name": name, "bytes": size, "reason": OVER_CAP})
        else:
            take.append(name)
    return Plan(take=tuple(take), unstorable=tuple(unstorable), not_uploaded=tuple(not_uploaded))
