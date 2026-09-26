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
                          runner finished cleanly, FAILS THE ATTEMPT RETRYABLY
                          with those names as its cause

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
dependants are cancelled like any failed parent's. Only an attempt whose runner
finished cleanly is failed this way: a runner that failed, timed out or was
stopped keeps its own cause, and the missing names are recorded beside it.

A RETRY STARTS WITH AN EMPTY ARTIFACTS DIRECTORY. It resumes `work/` from the
checkpoint the failed attempt took as it ended, and that checkpoint does not
hold `artifacts/` (only `work/` is archived, and the `./artifacts` link is left
out). The dependant stages from the attempt that SUCCEEDS, so every expected
output must be written again by the retry, not only the one that was missing.
The agent is given the same instructions, which list every name.

An attempt that is going to be retried PUBLISHES NOTHING, as a parked one does
not: its work is not finished, and the retry publishes it. Published now, the
retry would push again from the final checkpoint, which is taken before the
publish step auto-commits the agent's uncommitted changes; when it did, the
retry's commit does not descend from the pushed one and the push, which is
never forced, is refused as a non-fast-forward. The last attempt publishes
like any other failed attempt (`lifecycle._publish_withheld`).

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
from typing import Any, Iterable, Sequence

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
        f"Write deliverables to {directory} ($SWARM_ARTIFACTS_DIR); files there "
        "are uploaded and shown in Artifacts."
    )


def agent_instructions(names: Sequence[str], artifacts_dir: Path | str) -> str:
    """The lines a CLI runner appends to the agent's prompt.

    Always `deliverables_line`, once. Then, when later steps of a workflow
    stage files from this one, their names and each file's full path. The
    directory is not named a second time: "there" is the directory the first
    line named, so the prompt carries that line exactly once. "Outside the
    repository" is said because the working directory is where an agent
    assumes its output belongs.

    The `./artifacts` link is deliberately not mentioned. It is a net under
    the agent that reads this path and writes somewhere else anyway, and it
    may be absent (`workspace.link_artifacts` skips a name already taken), so
    naming it here would be a promise this text cannot check. For the same
    reason the list's last sentence names the repository and not "the working
    directory": a file written through the link IS written in the working
    directory's `./artifacts`, and it does reach later steps. Nor does it
    mention the working-folder upload of a task with no repository
    (`agent_worker.standalone_outputs`): that is a net as well, and a file it
    catches is named `workdir/<path>`, which no later step stages by the bare
    name.
    """
    directory = os.path.abspath(os.fspath(artifacts_dir))
    lines = [deliverables_line(directory)]
    if names:
        listed = ", ".join(names)
        lines += [
            f"Later steps of this workflow need these files from you: {listed}.",
            "Write each one there, which is outside the repository and outside "
            "your working directory. Files written anywhere else, the "
            "repository included, do not reach those steps.",
        ]
        lines += [f"- {PurePosixPath(directory) / name}" for name in names]
    return "\n".join(lines)


def with_instructions(prompt: str, names: Sequence[str], artifacts_dir: Path | str) -> str:
    """`prompt`, a blank line, then `agent_instructions`.

    The caller's own prompt comes first and is kept whole. Until #184's
    owner decision of 2026-09-26 a prompt with no expected names was passed
    unchanged; now every prompt ends with `deliverables_line`.
    """
    return f"{prompt}\n\n{agent_instructions(names, artifacts_dir)}"


def missing_outputs(names: Sequence[str], produced: Iterable[str]) -> list[str]:
    """The expected names this attempt did not upload, in the order given."""
    have = set(produced)
    return [name for name in names if name not in have]


def missing_cause(missing: Sequence[str], *, skipped: Iterable[str] = ()) -> str:
    """Which files are missing, and in which way. The cause a retry names.

    A file that was written but not uploaded is named separately, because the
    remedy is different: it is the artifact cap or an upload error, not the
    agent's prompt. `inputs.artifact_reference` makes the same distinction on
    the dependant's side.
    """
    skipped_set = set(skipped)
    over_cap = [name for name in missing if name in skipped_set]
    absent = [name for name in missing if name not in skipped_set]
    parts: list[str] = []
    if absent:
        parts.append(
            "not written to $SWARM_ARTIFACTS_DIR: " + ", ".join(absent)
        )
    if over_cap:
        parts.append("written but not uploaded: " + ", ".join(over_cap))
    return "; ".join(parts)


def missing_line(
    missing: Sequence[str], *, skipped: Iterable[str] = (), consequence: str = ""
) -> str:
    """One log line naming the missing files, and what that costs this attempt.

    `consequence` is the caller's, because only the caller knows whether the
    runner finished cleanly, which decides whether the attempt fails for this
    or for a cause of its own.
    """
    line = (
        "expected outputs missing, so a later step that stages them would fail ("
        + missing_cause(missing, skipped=skipped)
        + ")."
    )
    return f"{line} {consequence}" if consequence else line


def missing_error(missing: Sequence[str], *, skipped: Iterable[str] = ()) -> str:
    """`last_error` for an attempt failed for missing expected outputs."""
    return (
        "expected outputs missing ("
        + missing_cause(missing, skipped=skipped)
        + "). A later step of this workflow stages them from this task, so the "
        "attempt failed; it is retried while the task has attempts left, and "
        "the retry must write every expected output again."
    )
