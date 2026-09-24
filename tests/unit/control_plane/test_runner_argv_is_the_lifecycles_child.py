"""`RunnerProfile.runner_argv` is the argv the worker lifecycle starts as its CHILD.

Contract request 18 in docs/contract-change-requests.md (filed from incident
wf_ebb3ab2d65664707a559 as CR-2), accepted by the owner on 2026-09-24 in its
stronger form: the field is RENAMED, not only commented.

THE MISREADING IT REMOVES. The field was called `command`, on the object that
describes what a worker container runs, so it read as a container command. Both
dispatchers used it as one -- `"command": list(profile.command)` on the GKE pod
and `command=list(profile.command)` on the Cloud Run Job -- and a container
`command` REPLACES the image ENTRYPOINT, which is the lifecycle. Every GKE pod
ran the bare runner: no fencing (invariant 5), no checkpoint (invariant 8), no
heartbeat, no lease release. Five leases held every browser slot until an
operator intervened.

The dispatch-side guard is `test_dispatch_manifests.py`, which asserts for every
profile on both backends that no container `command` or `args` is set. This file
pins the other half: the catalogue no longer offers a field whose name invites
that line to be written, and the field says what it is where it is declared.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
from pathlib import Path

from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

PROFILES_PY = Path(__file__).resolve().parents[3] / "apps/common/swarm_common/profiles.py"


def test_the_catalogue_has_no_field_called_command() -> None:
    names = {f.name for f in dataclasses.fields(RunnerProfile)}
    assert "command" not in names, (
        "RunnerProfile still has a `command` field. It is the RUNNER's argv, which "
        "the lifecycle starts as a child; named `command`, it was set as the "
        "container command by both dispatchers (incident wf_ebb3ab2d65664707a559)"
    )
    assert "runner_argv" in names, sorted(names)


def test_every_profile_names_a_runner_module_the_lifecycle_can_start() -> None:
    """The value is unchanged by the rename: `python -m agent_worker.runners.<x>`."""
    for name, profile in RUNNER_PROFILES.items():
        argv = profile.runner_argv
        assert isinstance(argv, tuple) and len(argv) == 3, (name, argv)
        assert argv[:2] == ("python", "-m"), (name, argv)
        module = argv[2]
        assert module.startswith("agent_worker.runners."), (name, argv)
        assert importlib.util.find_spec(module) is not None, (
            f"{name}: runner module {module} does not exist"
        )


def test_the_field_says_where_it_is_declared_that_it_is_never_a_container_command() -> None:
    """The prohibition used to live in one place, a YAML comment nothing read.

    `kubernetes/worker-templates/worker-job.yaml` said "`command` is ABSENT,
    deliberately". The code that built the Job never read it. The warning now
    sits on the field itself, which is the line a dispatcher author is reading
    when they reach for it.
    """
    source = PROFILES_PY.read_text()
    match = re.search(r"((?:[ \t]*#:[^\n]*\n)+)[ \t]*runner_argv\s*:", source)
    assert match is not None, "runner_argv has no `#:` comment directly above it"
    comment = " ".join(line.strip().lstrip("#:").strip() for line in match.group(1).splitlines())
    for phrase in ("lifecycle", "CHILD", "NEVER a container command", "ENTRYPOINT"):
        assert phrase in comment, f"the runner_argv comment no longer says {phrase!r}: {comment}"
