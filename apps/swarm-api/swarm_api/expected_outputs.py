"""Which artifacts each workflow step's dependants will stage from it (#149).

`input_from = {upstream_step: filename}` is declared on the DEPENDANT, so the
upstream step's agent never learns that a later step needs a file from it, or
that the file has to be written into `$SWARM_ARTIFACTS_DIR`, because only files
there are uploaded. On 2026-09-25 that cost workflow `wf_73946ff4a32a4f99b3a4`
its whole second half: eight scan steps succeeded having written `scan-01.md`
and its siblings into their working directories, all four merges failed at
staging, and seventeen steps were cancelled.

The owner chose option (b) on #149. At submission this module inverts every
`input_from` edge, and the service records on each UPSTREAM step's task the
names its dependants expect, as `metadata.expected_outputs`. The worker hands
that list to the runner, and a CLI runner tells the agent the names and the
absolute artifacts path (apps/agent-worker/agent_worker/expected_outputs.py).

It lives in `metadata` because `Task` is a FROZEN type and `metadata` is its
free-form per-dispatch dict, already used this way by `input_from`,
`workflow_step` and `dispatch`. It is written onto the task BEFORE the store is
called, so it is part of the one write that creates the task document
(`Store.create_tasks`), never a second update that a worker could race.

A plain `POST /v1/tasks` never reaches this module, so it stores nothing under
the key. A workflow step does, and there the workflow's graph is the authority:
`record_expected_outputs` sets the key on a step something stages from and
REMOVES it from every other step. The workflow's own `metadata` is copied onto
every step, so without the removal a value there would reach steps nothing
stages from.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: The key under `task.metadata`. The worker reads the same key through its own
#: constant, `agent_worker.expected_outputs.METADATA_KEY`. The two packages never
#: import each other (a worker that imported swarm-api would carry the control
#: plane into every agent image), so the two spellings are held equal by
#: tests/unit/control_plane/test_expected_outputs_seam.py and recorded in
#: docs/mirrored-values.md.
EXPECTED_OUTPUTS_METADATA_KEY = "expected_outputs"


def expected_outputs_by_step(
    steps: Iterable[tuple[str, Mapping[str, str]]],
) -> dict[str, list[str]]:
    """Invert `input_from`: upstream step id -> the filenames staged from it.

    `steps` is `(step_id, input_from)` for every step of one workflow. Each list
    is sorted and holds each name once, so two dependants asking for the same
    file tell the agent about it once, and the stored value does not depend on
    the order the steps were submitted in.

    Filenames are stripped, because the worker strips the name it stages
    (`agent_worker.inputs.declared_inputs`), so the stripped name is the one the
    dependant looks for. An empty name is left out: it names no file, and the
    dependant refuses it.
    """
    expected: dict[str, set[str]] = {}
    for _step_id, input_from in steps:
        for upstream, filename in (input_from or {}).items():
            name = filename if isinstance(filename, str) else ""  # MUTATION M1 (red run only)
            if name:
                expected.setdefault(upstream, set()).add(name)
    return {step: sorted(names) for step, names in expected.items()}


def record_expected_outputs(metadata: dict[str, Any], names: Sequence[str] | None) -> None:
    """Set the key to `names` on a step something stages from, else remove it."""
    if names:
        metadata[EXPECTED_OUTPUTS_METADATA_KEY] = list(names)
    else:
        metadata.pop(EXPECTED_OUTPUTS_METADATA_KEY, None)
