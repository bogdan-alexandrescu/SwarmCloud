"""Telling an upstream step which files its dependants will stage (#149).

A workflow step may declare `input_from = {upstream_step: filename}`, and the
worker stages that file from the upstream step's uploaded ARTIFACTS
(`agent_worker.inputs`). Only files the upstream step wrote into
`$SWARM_ARTIFACTS_DIR` are artifacts (`runners/base.py`: "files written here are
uploaded when the attempt ends"), and that directory is outside the agent's
working directory and outside the repository checkout. A file the agent writes
anywhere else ends up, at most, in `swarm-work.patch`.

Measured on 2026-09-25, workflow `wf_73946ff4a32a4f99b3a4`: eight claude-code
scan steps were prompted "write it to scan-01.md". All eight SUCCEEDED and
uploaded only `claude-code.stdout.log`, `claude-code.stderr.log` and
`claude-transcript.json`, and two of them also `swarm-work.patch`. All four merge
steps then FAILED with "upstream task ... did not produce an artifact named
'scan-01.md'", and seventeen downstream steps were cancelled. The failure could
only show up after every upstream step had spent its compute and its provider
quota.

The owner chose option (b) on #149, and this module is the worker's half of it:

    API, at submission    metadata.expected_outputs = ["scan-01.md"] on the
                          UPSTREAM task (swarm_api/expected_outputs.py); a
                          caller may not set that key, the API refuses it
    worker, _prepare      copies the usable names into input.json under the
                          same key, minus `swarm-work.patch`, and drops a
                          caller's own input.expected_outputs
    CLI runner            appends the names, minus its own log and transcript
                          files, and the ABSOLUTE artifacts path to the prompt
                          it passes the agent (`with_instructions`)
    worker, _finalise     names any that are missing, in one log line and in
                          result_summary[MISSING_SUMMARY_KEY], and, when the
                          runner finished cleanly, FAILS THE ATTEMPT with
                          those names as its cause: retryably only when a
                          retry can produce them (#165, `retryable`)

INSTRUCTIONS ALONE WERE MEASURED NOT TO BE ENOUGH. On the third run,
`wf_06a3a949d2c242c3b0e9` (2026-09-25), every prompt named `$SWARM_ARTIFACTS_DIR`
and told the agent to echo it. scan-02 echoed the right path and then wrote
`<work>/artifacts/scan-02.md`. So the worker also links `work/artifacts` to the
artifacts directory (`workspace.link_artifacts`), and the natural wrong guess
lands in the uploaded directory. The instructions say where; the link catches
the agent that reads it and writes next to its working directory anyway; the
end-of-attempt check says when neither worked, and fails the attempt so the
dependant never starts on a parent that did not write what it promised.

A MISSING EXPECTED OUTPUT FAILS THE ATTEMPT, RETRYABLY (owner decision on
#149, 2026-09-25T10:30Z, replacing the report-only behaviour #153 first
shipped). The cause names the missing files. The task goes back to READY and
is admitted again as a new attempt while it has attempts left, and ends FAILED
once `max_attempts` is spent (`control.fail_retryably`), after which its
dependants are cancelled like any failed parent's. NOT RETRIED when the cause
is one a retry cannot change (#165, owner decision 2026-09-28): the artifact
cap, a refused file, or a `swarm-work.patch` the harvest could not write (an
empty diff, a patch over its cap, no clone base) end the task FAILED at once.
Only a file never written, or one whose upload raised, is retried
(`RETRYABLE_CAUSES`). Only an attempt whose runner
finished cleanly is failed this way: a runner that failed, timed out or was
stopped keeps its own cause, and the missing names are recorded beside it.

A RETRY STARTS WITH AN EMPTY ARTIFACTS DIRECTORY. It resumes `work/` from the
checkpoint the failed attempt took as it ended, and that checkpoint does not
hold `artifacts/` (only `work/` is archived, and the `./artifacts` link is left
out). The dependant stages from the attempt that SUCCEEDS, so every expected
output must be written again by the retry, not only the one that was missing.
The agent is given the same instructions, which list every name.

An attempt with a missing expected output OPENS NO PULL REQUEST, the last
attempt included (#165): the publish -- its push and its pull request -- waits
until the upload manifest has passed this check (`lifecycle._publish_checked`).
Under `carrier: checkpoints`, the default, such an attempt therefore pushes
nothing. Under `carrier: branches` its COMMITTED work is still pushed to
`swarm/<task>` by the checkpoints taken once the runner has stopped
(`lifecycle._push_carrier_branch`): owner decision 2026-10-02, keep the work.
What it withholds is the publish's own push, which carries the uncommitted
changes too, and any pull request (#453). A retry's own push would otherwise be refused as a
non-fast-forward: the final checkpoint is taken before the publish step
auto-commits the agent's uncommitted changes.

A NAME THE PLATFORM WRITES ITSELF IS NEVER IN THE INSTRUCTIONS
(`without_platform_names`). A dependant may stage the upstream's
`swarm-work.patch`, runner log or transcript, and the API records that name
like any other. But the patch is the platform's record of the agent's
repository changes, written after the agent exits over whatever is there (or,
with an empty diff, not written, leaving an agent's own file to pass for it),
and the runner writes its log while the agent runs. Telling the agent to write
either is worse than telling it nothing. Each writer leaves
out its own names -- the worker the patch, the runner its three files -- so no
list of them is spelled out twice.

Two things are deliberately NOT done here.

* **The worker does not copy a same-named file out of the working directory.**
  That was option (c) on #149, and the owner rejected it. The `./artifacts`
  link is not a copy: a file written through it is written into the artifacts
  directory in the first place. A file left anywhere else -- the repository, or
  the working directory outside `./artifacts` -- does not become the artifact
  a dependant stages. For a task with NO repository the worker does upload
  what the agent created in its working folder (#184, owner decision of
  2026-09-26; `agent_worker.standalone_outputs`), but under `workdir/<path>`,
  so `scan-01.md` left in the working folder is `workdir/scan-01.md` in the
  manifest and still does not satisfy an expected `scan-01.md`.
* **A malformed declaration does not fail the attempt.** An entry that cannot
  name a file in the artifacts directory is not a promise anyone can keep, and
  no dependant can stage it either (`inputs.destination_for` refuses the same
  shapes). That is the opposite of `inputs.declared_inputs`, which fails the
  attempt because a step that declares an INPUT has been promised that file. An
  unusable entry here is dropped, and the caller of `declared_outputs` logs it
  by name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Collection, Iterable, Mapping, Sequence

#: The key under `task.metadata`, and the key `input.json` carries it under.
#:
#: `Task.metadata` is a free-form dict on a FROZEN type, and per-dispatch
#: options already live there (`input_from`, `workflow_step`, `dispatch`). So a
#: new field on `Task` would be a contract change, and this is not one.
#:
#: swarm-api writes the same key through its own constant,
#: `swarm_api.expected_outputs.EXPECTED_OUTPUTS_METADATA_KEY`. The two packages
#: never import each other, so the two spellings are held equal by
#: tests/unit/control_plane/test_expected_outputs_seam.py and recorded in
#: docs/mirrored-values.md.
#:
#: The same key in `input.json`, because it is the same list, and one word is
#: one thing to grep for when an agent says it was never told. The worker
#: ASSIGNS it there rather than using `setdefault`, like `staged_inputs`, and
#: removes a caller's own value when it has none to assign: the key states what
#: the platform knows, so a caller's `expected_outputs` in the step input must
#: neither shadow it nor stand in for it.
METADATA_KEY = "expected_outputs"

#: The key in `task.result_summary` that names the expected outputs this attempt
#: did not upload. Present only when at least one is missing.
MISSING_SUMMARY_KEY = "expected_outputs_missing"

#: Beside it, `[{name, cause}]` for the same names, in the same order (#165):
#: why each is missing, one of the `CAUSE_*` values below.
MISSING_CAUSES_SUMMARY_KEY = "expected_outputs_missing_causes"


@dataclass(frozen=True)
class Declared:
    """The usable names, sorted and each once, and every entry refused."""

    names: tuple[str, ...] = ()
    rejected: tuple[Any, ...] = ()


def _usable(entry: Any) -> str | None:
    """`entry` as a relative path inside the artifacts directory, or None.

    Stripped, because `inputs.declared_inputs` strips the filename it stages,
    so the stripped name is the one the dependant will look for.

    Refused: anything not a string; an empty name; an absolute path; any empty,
    `.` or `..` segment; a backslash; a NUL; and a line break. A line break
    could not name a file the agent should write, and it would let one entry
    rewrite the lines of the instruction around it. The same shapes cannot be
    staged either (`inputs.destination_for`), so dropping one here loses no
    file the dependant could have received.
    """
    if not isinstance(entry, str):
        return None
    name = entry.strip()
    if not name or name.startswith("/"):
        return None
    if any(ch in name for ch in ("\\", "\x00", "\n", "\r")):
        return None
    if any(segment in ("", ".", "..") for segment in name.split("/")):
        return None
    return name


def parse_names(value: Any) -> Declared:
    """The list under `METADATA_KEY`, from task metadata or from `input.json`.

    Absent means nothing is expected, so it is not an error. A value that is
    not a list is refused as a whole. A bare string is refused rather than read
    as one name, because the API has only ever written a list and a string
    here means some other writer.
    """
    if value is None:
        return Declared()
    if not isinstance(value, (list, tuple)):
        return Declared(rejected=(value,))
    names: set[str] = set()
    rejected: list[Any] = []
    for entry in value:
        usable = _usable(entry)
        if usable is None:
            rejected.append(entry)
        else:
            names.add(usable)
    return Declared(names=tuple(sorted(names)), rejected=tuple(rejected))


def declared_outputs(metadata: Any) -> Declared:
    """What `task.metadata` says this task's dependants will stage from it."""
    if not isinstance(metadata, dict):
        # The contract types `metadata` as a dict. A value of any other type
        # cannot carry this key, so there is nothing here to honour.
        return Declared()
    return parse_names(metadata.get(METADATA_KEY))


def without_platform_names(names: Sequence[str], platform_written: Iterable[str]) -> tuple[str, ...]:
    """`names` minus the ones the caller of this writes into the artifacts itself.

    The order of `names` is kept. See the module docstring for why a
    platform-written name is never put in front of the agent.
    """
    own = set(platform_written)
    return tuple(name for name in names if name not in own)


def deliverables_line(artifacts_dir: Path | str) -> str:
    """The ONE line every claude-code and codex prompt ends with (#184).

    Owner decision, 2026-09-26: every prompt the worker hands a CLI agent
    names `$SWARM_ARTIFACTS_DIR` and says what happens to the files there.
    Found in the post-deploy QA of #184 (`task_0e5b1f8b7bc1448fafdf`): a task
    with no repository wrote its answer into its working folder, and the
    Artifacts tab showed only the runner's logs, because no prompt had said
    where a deliverable goes unless a later workflow step expected one.

    INFORMATION, IN THE OWNER'S WORDS, NOT AN ORDER (#225 review). The
    decision on #184 reads "one line naming $SWARM_ARTIFACTS_DIR: files written
    there are uploaded and shown in Artifacts". The first build said "Write
    deliverables to ...", which came from a paraphrase of it. That order
    reached every REPOSITORY task too, and the same decision says a repository
    task's deliverable is its diff or pull request: an agent asked to write
    `docs/design.md` and told, last thing, to write deliverables to the
    artifacts folder can put the document there and leave the pull request
    empty. The line says what happens to a file written there, and leaves
    where to write to the task.

    The directory is given as an ABSOLUTE path beside the variable's name. The
    name alone is not enough: an agent has reported `SWARM_ARTIFACTS_DIR` as
    "not set in this environment" and written nothing (wf_bcdc9180e4fb4a209f31,
    recorded in `runners/cliagent.py`).

    Built here and appended by `with_instructions`, which `run_cli_agent`
    calls for claude-code and codex alike, so the line exists once, not once
    per runner. It must never carry a rate-limit or credential marker
    (`cliagent._RATE_LIMIT_MARKERS`): a CLI that echoes its prompt would turn
    it into evidence.
    """
    directory = os.path.abspath(os.fspath(artifacts_dir))
    return (
        f"Files written to {directory} ($SWARM_ARTIFACTS_DIR) are uploaded and "
        "shown in Artifacts."
    )


def questions_line() -> str:
    """The ONE sentence telling a CLI agent it may ask the owner (2026-10-05).

    Owner decision, 2026-10-05: a remote agent that meets a decision that is
    the owner's writes it into `questions.json` rather than guessing, or
    burying it in its answer and shipping `part of #N` (I310, W532). Follows
    `deliverables_line`, so "there" is the folder that line named and the
    prompt still names `$SWARM_ARTIFACTS_DIR` once. The shape is given inline
    because the agent cannot read this platform's docs; the worker checks it
    (`agent_worker.questions`). "Never acted on" is said because it is true:
    the file is data for the operator, not a request to the platform.

    Shared by claude-code and codex, like the line before it: both are told
    about the folder in the one place, `agent_instructions`.
    """
    return (
        "If a decision is the owner's to make, ask instead of guessing: write "
        "questions.json there, a JSON list of {question, options: [{label, "
        "description}], recommended, context}; it is shown to the owner and "
        "never acted on."
    )


def staged_paths(value: Any, work_dir: Path | str) -> tuple[str, ...]:
    """The absolute paths of the inputs the worker staged, from `input.json`.

    `value` is `input.json`'s `staged_inputs`: the worker's own record of what
    `inputs.stage_inputs` put in `work/`, each entry's `path` relative to it.
    The worker ASSIGNS that key when it staged something and removes a
    caller's own value when it staged nothing (`lifecycle._prepare`), so what
    is here is the platform's.

    Each path goes through `_usable` anyway, because it becomes a line of the
    prompt in the platform's voice: a path that leaves `work/`, is absolute or
    carries a line break is dropped rather than written. Order kept, each once.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    root = PurePosixPath(os.path.abspath(os.fspath(work_dir)))
    seen: list[str] = []
    for entry in value:
        raw = entry.get("path") if isinstance(entry, dict) else None
        usable = _usable(raw)
        if usable is None:
            continue
        full = str(root / usable)
        if full not in seen:
            seen.append(full)
    return tuple(seen)


def agent_instructions(
    names: Sequence[str], artifacts_dir: Path | str, staged: Sequence[str] = ()
) -> str:
    """The lines a CLI runner appends to the agent's prompt.

    Always `deliverables_line`, once, and `questions_line` after it, which
    says the agent may ask the owner in `questions.json` there. Then, when
    later steps of a workflow stage files from this one, their names and
    each file's full path, as the one order this text gives: a dependant
    cannot run without them (#149).
    The directory is not named a second time: "there" is the directory the
    first line named, so the prompt carries that line exactly once. "Outside the
    repository" is said because the working directory is where an agent
    assumes its output belongs.

    Then, when `staged` is not empty, the files earlier steps gave this one,
    by absolute path (#226, owner decision of 2026-09-26). The runner passes
    them only when the agent starts in the repository checkout: a staged
    input lands in `work/`, which is then the checkout's parent, so "read
    scan-01.md" in a prompt no longer finds it. A task with no repository
    starts in `work/`, where it does, and its prompt is unchanged. Last, so
    that "there" above still means the artifacts directory.

    The `./artifacts` link is deliberately not mentioned. It is a net under
    the agent that reads this path and writes somewhere else anyway, and it
    may be absent (`workspace.link_artifacts` skips a name already taken), so
    naming it here would be a promise this text cannot check. For the same
    reason the list's last sentence names the repository and not "the working
    directory": a file written through the link IS written in the working
    directory's `./artifacts` (the checkout's, when the agent starts there),
    and it does reach later steps, because it is written THERE. Nor does it
    mention the working-folder upload of a task with no repository
    (`agent_worker.standalone_outputs`): that is a net as well, and a file it
    catches is named `workdir/<path>`, which no later step stages by the bare
    name.
    """
    directory = os.path.abspath(os.fspath(artifacts_dir))
    lines = [deliverables_line(directory), questions_line()]
    if names:
        listed = ", ".join(names)
        lines += [
            f"Later steps of this workflow need these files from you: {listed}.",
            "Write each one there, which is outside the repository and outside "
            "your working directory. Files written anywhere else, the "
            "repository included, do not reach those steps.",
        ]
        lines += [f"- {PurePosixPath(directory) / name}" for name in names]
    if staged:
        lines.append(
            "Earlier steps of this workflow gave you these files, which are outside "
            "the repository:"
        )
        lines += [f"- {path}" for path in staged]
    return "\n".join(lines)


def with_instructions(
    prompt: str,
    names: Sequence[str],
    artifacts_dir: Path | str,
    staged: Sequence[str] = (),
) -> str:
    """`prompt`, a blank line, then `agent_instructions`.

    The caller's own prompt comes first and is kept whole. Until #184's
    owner decision of 2026-09-26 a prompt with no expected names was passed
    unchanged; now every prompt ends with `deliverables_line`.
    """
    return f"{prompt}\n\n{agent_instructions(names, artifacts_dir, staged)}"


def missing_outputs(names: Sequence[str], produced: Iterable[str]) -> list[str]:
    """The expected names this attempt did not upload, in the order given."""
    have = set(produced)
    return [name for name in names if name not in have]


#: WHY a file in `$SWARM_ARTIFACTS_DIR` was not uploaded, or an expected one is
#: missing (#165, owner decision 2026-09-28). `_upload_outputs` records one per
#: skipped file in `result_summary.artifacts_skipped`, and the missing-output
#: check reads it to decide whether a retry can help.
#:
#: The artifact cap (bytes, or the file-count cap) and a file refused at read
#: (a link, an unstorable or over-long name): the next attempt writes the same
#: file into the same cap. `swarm-work.patch` the harvest did not write: the
#: diff was empty, it was over the patch cap and discarded, or there was no
#: clone base to diff against. Each is a property of the work or the
#: repository, which a retry restores unchanged.
CAUSE_CAP = "cap"
CAUSE_REFUSED = "refused"
CAUSE_EMPTY_DIFF = "empty_diff"
CAUSE_PATCH_OMITTED = "patch_omitted"
CAUSE_NO_BASE = "no_base"
#: The two a retry CAN change: the store raised on the upload, or the file was
#: never written. Only these leave a missing output retryable.
CAUSE_UPLOAD_ERROR = "upload_error"
CAUSE_NOT_WRITTEN = "not_written"
RETRYABLE_CAUSES = frozenset({CAUSE_UPLOAD_ERROR, CAUSE_NOT_WRITTEN})

#: `task.result_summary.artifacts_skipped` entries are `{name, cause}` (#165,
#: owner decision 2026-09-28), one per skipped name, capped at 50. A summary
#: written before causes were recorded holds bare names, and Firestore keeps
#: those documents, so every reader goes through the two functions below,
#: which accept both. swarm-api normalises the same way before it answers, and
#: keeps its HTTP `artifacts_skipped` a list of names (with the causes beside
#: it as `artifacts_skipped_causes`), so no API client changes shape.
def skipped_entries(value: Any) -> list[tuple[str, str | None]]:
    """`(name, cause)` for each entry of a stored `artifacts_skipped`.

    A legacy bare-name entry has cause None: the attempt that wrote it did not
    say why, and guessing the cap is the assumption #165 removed.
    """
    if not isinstance(value, list):
        return []
    out: list[tuple[str, str | None]] = []
    for entry in value:
        if isinstance(entry, str):
            out.append((entry, None))
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            cause = entry.get("cause")
            out.append((entry["name"], cause if isinstance(cause, str) and cause else None))
    return out


def skipped_names(value: Any) -> list[str]:
    """The names in a stored `artifacts_skipped`, whichever shape wrote it."""
    return [name for name, _ in skipped_entries(value)]

#: What each cause says, in a missing-output cause and a dependant's refusal.
_CAUSE_TEXT = {
    CAUSE_NOT_WRITTEN: "not written to $SWARM_ARTIFACTS_DIR",
    CAUSE_CAP: "written but not uploaded: over the artifact cap (cap)",
    CAUSE_UPLOAD_ERROR: "written but not uploaded: the upload failed (upload_error)",
    CAUSE_REFUSED: (
        "written but not uploaded: refused when read -- a link, not a regular "
        "file, or a name no object can carry (refused)"
    ),
    CAUSE_EMPTY_DIFF: "not written: the agent's diff was empty (empty_diff)",
    CAUSE_PATCH_OMITTED: (
        "not written: the diff was over the patch cap and was discarded (patch_omitted)"
    ),
    CAUSE_NO_BASE: "not written: there is no clone base to diff against (no_base)",
}


def cause_text(cause: str) -> str:
    """One cause, as the phrase a log line, an error and a refusal use."""
    return _CAUSE_TEXT.get(cause, f"not uploaded ({cause})")


def patch_cause(git: Any) -> str | None:
    """Why the harvest wrote no `swarm-work.patch`, from `result_summary["git"]`.

    None when the harvest failed (its `error`), which a retry may not meet, and
    when it did write one. No harvest at all (no repository) is `no_base`:
    there is nothing a patch could be taken against, on this attempt or the next.
    """
    if git is None:
        return CAUSE_NO_BASE
    if not isinstance(git, dict) or git.get("error"):
        return None
    if git.get("patch"):
        return None
    if git.get("patch_omitted"):
        return CAUSE_PATCH_OMITTED
    if not git.get("base"):
        return CAUSE_NO_BASE
    return CAUSE_EMPTY_DIFF


def causes_of(missing: Sequence[str], causes: Mapping[str, str]) -> dict[str, str]:
    """Each missing name with its cause; a name with none recorded was not written."""
    return {name: causes.get(name, CAUSE_NOT_WRITTEN) for name in missing}


def retryable(missing: Sequence[str], causes: Mapping[str, str]) -> bool:
    """Whether a retry can produce every missing name (#165).

    False as soon as ONE of them has a cause a retry cannot change: the
    dependant needs every file, so a retry that can at best write the others
    still leaves it without one.
    """
    return all(cause in RETRYABLE_CAUSES for cause in causes_of(missing, causes).values())


def missing_cause(missing: Sequence[str], *, causes: Mapping[str, str] | None = None) -> str:
    """Which files are missing, and why. The cause a failed attempt names.

    Grouped by cause, in the order of `missing`, so a file the cap kept out is
    never described as one the agent did not write: the remedy is different
    (`inputs.artifact_reference` says the same thing on the dependant's side).
    """
    grouped: dict[str, list[str]] = {}
    for name, cause in causes_of(missing, causes or {}).items():
        grouped.setdefault(cause, []).append(name)
    return "; ".join(f"{cause_text(cause)}: " + ", ".join(names) for cause, names in grouped.items())


def missing_line(
    missing: Sequence[str], *, causes: Mapping[str, str] | None = None, consequence: str = ""
) -> str:
    """One log line naming the missing files, and what that costs this attempt.

    `consequence` is the caller's, because only the caller knows whether the
    runner finished cleanly, which decides whether the attempt fails for this
    or for a cause of its own.
    """
    line = (
        "expected outputs missing, so a later step that stages them would fail ("
        + missing_cause(missing, causes=causes)
        + ")."
    )
    return f"{line} {consequence}" if consequence else line


def missing_error(missing: Sequence[str], *, causes: Mapping[str, str] | None = None) -> str:
    """`last_error` for an attempt failed for missing expected outputs.

    Says whether it is retried, from the causes (`retryable`).
    """
    known = causes or {}
    head = "expected outputs missing (" + missing_cause(missing, causes=known) + "). "
    if retryable(missing, known):
        return (
            head
            + "A later step of this workflow stages them from this task, so the "
            "attempt failed; it is retried while the task has attempts left, and "
            "the retry must write every expected output again."
        )
    return (
        head
        + "A later step of this workflow stages them from this task, and another "
        "attempt would meet the same cause, so the task failed without a retry."
    )


# ---------------------------------------------------------------------------
# An empty diff that is a result, and the steps that needed a change
# ---------------------------------------------------------------------------
#
# Owner decision, 2026-10-05 (lane review P2). A step may say that changing
# nothing is a correct outcome -- "fix it if it is broken" -- with
# `allow_empty_diff: true`, which swarm-api stores in the signed dispatch
# block. Such a step whose diff is empty ends SUCCEEDED with
# `result_summary.no_change: true`, its other artifacts (the verification it
# ran) uploaded as usual, instead of failing on `empty_diff`. A step that
# NEEDED its change then has nothing to work on, and ends SUCCEEDED with
# `result_summary.skipped`, without an agent and without a clone. The frozen
# `TaskState` has no SKIPPED and forbids PARKED -> SUCCEEDED, so the skip is
# made by the worker, on the attempt the scheduler leased, as the verdict
# gate's no-agent ending is (#264); swarm-api's rollup reads the marker.

#: The dispatch-block keys, as swarm-api's `DispatchOptions.to_metadata`
#: writes them (tests/unit/control_plane/test_allow_empty_diff_workflow.py).
ALLOW_EMPTY_DIFF_KEY = "allow_empty_diff"
PR_LABEL_KEY = "pr_label"
#: `result_summary` keys. `SKIPPED_SUMMARY_KEY` is spelled again in
#: `swarm_api.rollup`, which cannot import this module.
NO_CHANGE_SUMMARY_KEY = "no_change"
SKIPPED_SUMMARY_KEY = "skipped"
#: `result_summary.skipped.reason`, the owner's words.
NOTHING_TO_CHANGE = "nothing to change"
#: The patch the harvest writes; `lifecycle.PATCH_NAME` is the same name.
WORK_PATCH_NAME = "swarm-work.patch"


def allows_empty_diff(dispatch: Any) -> bool:
    """Whether this step's signed dispatch block allows an empty diff. Only a
    literal true does: anything else is the default, which fails on one."""
    return isinstance(dispatch, Mapping) and dispatch.get(ALLOW_EMPTY_DIFF_KEY) is True


def changed_nothing(git: Any) -> bool:
    """True when `result_summary["git"]` records a clone left exactly as cloned.

    The harvest ran (no `error`), had a base to diff against, wrote no patch
    because the diff was EMPTY (`patch_cause` is `empty_diff`, not a patch
    over its cap), and found no commit and no uncommitted path.
    """
    if patch_cause(git) != CAUSE_EMPTY_DIFF:
        return False
    return git.get("commit_count", 0) == 0 and git.get("dirty_count", 0) == 0


def left_nothing(summary: Any) -> str | None:
    """What an upstream step's `result_summary` says it left: `no_change`
    (it ran, with nothing to change), `skipped` (it did not run), or None."""
    if not isinstance(summary, Mapping):
        return None
    if isinstance(summary.get(SKIPPED_SUMMARY_KEY), Mapping):
        return SKIPPED_SUMMARY_KEY
    if summary.get(NO_CHANGE_SUMMARY_KEY) is True:
        return NO_CHANGE_SUMMARY_KEY
    return None


def nothing_to_work_on(
    *,
    input_from: Mapping[str, str],
    branch_from: Sequence[str],
    integrates: Sequence[str],
    left: Mapping[str, str | None],
    builds_on: str = "",
    read_only: Collection[str] = (),
) -> list[str]:
    """The upstream task ids whose missing change leaves this step nothing to
    do, in the order met; empty when the step runs.

    `left` is `left_nothing` of each upstream read. A step READS an
    upstream's change when it:

      * stages that upstream's `swarm-work.patch` (`input_from`) -- or stages
        anything at all from a SKIPPED upstream, which wrote nothing;
      * starts from that upstream's branch: `builds_on`, or `branch_from` (a
        `single-pr` author, a merge step's pull request);
      * integrates it (`integrates`, given only while integration is
        pending), less the `read_only` contributors -- a review that writes
        only `verdict.json` -- which never had a change to merge.

    THE STEP IS SKIPPED ONLY WHEN NONE OF THE CHANGES IT READS EXISTS (#978).
    Written for one implementer, the rule skipped a step when ANY upstream it
    read left nothing; an issue run with two implement steps, whose review
    and integrator build on the LAST one, then stranded the other's pushed
    branch whenever the last changed nothing. So:

      * a missing change is reported only while no other changed input
        exists: a patch staged from an upstream that changed something, or,
        for an integrator, a contributor that did;
      * a `builds_on` upstream that left nothing does not, on its own, skip a
        step that has such an input: the worker starts it from the nearest
        ancestor along `builds_on` that pushed, else the default branch
        (`Worker._builds_on_base`);
      * `branch_from` keeps the old rule: a `single-pr` reader or a merge has
        exactly one branch to read, and nothing else stands in for it.

    A step that stages only another file from a `no_change` upstream -- the
    verification it ran -- still runs: that file exists. An integrator with
    every contributor unchanged is skipped as before, which issueci's
    `already_on_main` reads (#646).
    """
    needed: list[str] = []

    def add(task_id: str) -> None:
        if task_id not in needed:
            needed.append(task_id)

    changed = False
    for task_id, filename in input_from.items():
        what = left.get(task_id)
        if unstaged_left_nothing(filename, what):
            add(task_id)
        elif what is None and filename == WORK_PATCH_NAME:
            changed = True
    single_branch = False
    for task_id in branch_from:
        if left.get(task_id) is not None:
            add(task_id)
            single_branch = True
    if builds_on and left.get(builds_on) is not None:
        add(builds_on)
    if integrates:
        if any(left.get(t) is None and t not in read_only for t in integrates):
            changed = True
        elif all(left.get(task_id) is not None for task_id in integrates):
            for task_id in integrates:
                add(task_id)
    if changed and not single_branch:
        return []
    return needed


def unstaged_left_nothing(filename: str, left: str | None) -> bool:
    """True when a declared input from an upstream that `left` nothing does
    not exist to stage: its `swarm-work.patch` (a `no_change` step wrote
    none), or anything from a SKIPPED step (it wrote nothing). A step that
    runs because another of its inputs changed stages the rest (#978)."""
    return left == SKIPPED_SUMMARY_KEY or (
        left == NO_CHANGE_SUMMARY_KEY and filename == WORK_PATCH_NAME
    )
