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
                          UPSTREAM task (swarm_api/expected_outputs.py)
    worker, _prepare      copies the usable names into input.json under the
                          same key
    CLI runner            appends the names and the ABSOLUTE artifacts path to
                          the prompt it passes the agent (`with_instructions`)
    worker, _finalise     names any that are missing, in one log line and in
                          result_summary[MISSING_SUMMARY_KEY]

Three things are deliberately NOT done here.

* **The worker does not copy a same-named file out of the working directory.**
  That was option (c) on #149, and the owner rejected it. The agent is told
  where the file must go, and a file left anywhere else stays there.
* **A missing expected output does not fail the attempt.** Whether it should is
  an open owner decision. Until it is made, the attempt says so and succeeds or
  fails on its own terms, and the dependant still fails at staging, naming the
  upstream task and the file, exactly as it did before.
* **A malformed declaration does not fail the attempt either.** This is advice
  to the agent, not a promise made to it. That is the opposite of
  `inputs.declared_inputs`, which fails the attempt because a step that declares
  an INPUT has been promised that file. An unusable entry here is dropped from
  the advice, and the caller of `declared_outputs` logs it by name.
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
#: ASSIGNS it there rather than using `setdefault`, like `staged_inputs`: it
#: states what the platform knows, so a caller's own `expected_outputs` in the
#: step input must not shadow it.
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
    if any(ch in name for ch in ("\\", "\x00")):  # MUTATION M4 (red run only)
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


def agent_instructions(names: Sequence[str], artifacts_dir: Path | str) -> str:
    """The lines a CLI runner appends to the agent's prompt.

    The directory is given as an ABSOLUTE path, and every file with its full
    path too. The variable name alone is not enough, because an agent has
    already reported `SWARM_ARTIFACTS_DIR` as "not set in this environment"
    and written nothing (wf_bcdc9180e4fb4a209f31, recorded in
    `runners/cliagent.py`). "Outside the repository" is said because the
    working directory is where an agent assumes its output belongs.
    """
    directory = os.fspath(artifacts_dir)  # MUTATION M3 (red run only)
    listed = ", ".join(names)
    lines = [
        "---",
        f"Later steps of this workflow need these files from you: {listed}.",
        f"Write each one to {directory} (this is $SWARM_ARTIFACTS_DIR), which is "
        "outside the repository and outside your working directory. Files "
        "written anywhere else, including the working directory and the "
        "repository, do not reach those steps.",
    ]
    lines += [f"- {PurePosixPath(directory) / name}" for name in names]
    return "\n".join(lines)


def with_instructions(prompt: str, names: Sequence[str], artifacts_dir: Path | str) -> str:
    """`prompt` with `agent_instructions` appended, or `prompt` unchanged.

    Unchanged means the identical string, so a task no dependant stages from
    runs exactly as it did before this module existed.
    """
    if not names:
        return prompt
    return f"{prompt}\n\n{agent_instructions(names, artifacts_dir)}"


def missing_outputs(names: Sequence[str], produced: Iterable[str]) -> list[str]:
    """The expected names this attempt did not upload, in the order given."""
    have = set(produced)
    return [name for name in names if name not in have]


def missing_line(missing: Sequence[str], *, skipped: Iterable[str] = ()) -> str:
    """One log line naming the missing files.

    A file that was written but not uploaded is named separately, because the
    remedy is different: it is the artifact cap or an upload error, not the
    agent's prompt. `inputs.artifact_reference` makes the same distinction on
    the dependant's side.
    """
    skipped_set: set[str] = set()  # MUTATION M2 (red run only)
    over_cap = [name for name in missing if name in skipped_set]
    absent = [name for name in missing if name not in skipped_set]
    parts: list[str] = []
    if absent:
        parts.append(
            "not written to $SWARM_ARTIFACTS_DIR: " + ", ".join(absent)
        )
    if over_cap:
        parts.append("written but not uploaded: " + ", ".join(over_cap))
    return (
        "expected outputs missing, so a later step that stages them would fail ("
        + "; ".join(parts)
        + "). The attempt is not failed for it."
    )
