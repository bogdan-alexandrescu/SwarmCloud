"""Resource classes and runner profiles -- the admin-defined execution catalogue.

API callers choose a `runner_profile` by NAME and nothing else. They never supply
an image, a command, a resource spec or a backend. That is the whole point: it is
what stops an authenticated caller from turning the swarm into arbitrary compute.

Sizing note. These numbers are not guesses. They were measured on the reference
workstation on 2026-09-15: one working Claude Code lane was `claude` 1,532 MB +
`pytest` 769 MB + node/tsx guards 207 MB = ~2.5 GiB resident. The classes below
are roughly 2x that, and the worker exports peak RSS and peak disk per profile so
the numbers get corrected from production rather than from arithmetic.

Two hard rules follow from the stability requirement:
  * requests == limits. Bursting past a request is precisely what gets a
    container OOM-killed under node pressure, so we never do it.
  * Spot is disabled. Verified against GKE docs: Spot Pods cannot use Autopilot
    extended run time, so Spot and "no preemption" are mutually exclusive.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class Backend(str, Enum):
    CLOUD_RUN_JOB = "CLOUD_RUN_JOB"
    GKE_AUTOPILOT = "GKE_AUTOPILOT"
    AUTO = "AUTO"


class SpotStrategy(str, Enum):
    ON_DEMAND_ONLY = "on_demand_only"
    SPOT_PREFERRED = "spot_preferred"
    SPOT_ONLY = "spot_only"


@dataclass(frozen=True)
class ResourceClass:
    """A sizing envelope. `cpu` and `memory_gib` are BOTH request and limit."""

    name: str
    cpu: float
    memory_gib: int
    disk_gib: int
    #: Capacity units consumed from the weighted global budget. Heavier classes
    #: cost more scheduling budget than light ones.
    units: int
    #: Cloud Run ephemeral disk is a Preview feature and, per Google's docs,
    #: disables live migration -- which is why mandatory checkpointing exists.
    requires_preview_disk: bool = False

    def __post_init__(self) -> None:
        if self.cpu <= 0 or self.memory_gib <= 0:
            raise ValueError(f"resource class {self.name}: cpu and memory must be positive")
        if self.cpu > 8 or self.memory_gib > 32:
            # Cloud Run's hard ceiling. Anything above must be GKE-only.
            raise ValueError(f"resource class {self.name} exceeds the Cloud Run Jobs ceiling")


# WORKSPACE SIZES ARE MEMORY, NOT DISK.
#
# These originally claimed 20/40/100 GiB of disk-backed ephemeral storage. That
# is a Cloud Run Preview feature the Terraform google provider cannot express:
# `empty_dir.medium` accepts only "MEMORY". The workspace is therefore a tmpfs
# carved out of the container's memory, so `disk_gib` is a slice OF `memory_gib`
# and not additional capacity.
#
# The measured reference workload -- Claude Code plus a Node/Python toolchain
# running a test suite -- peaks near 2.5 GiB, which leaves standard roughly
# 1.5 GiB of real workspace headroom. A large monorepo with node_modules will
# not fit on `standard`; use `browser` or `large`, or route to GKE.
#
# Upside: this path is fully GA and supports live migration, which the Preview
# disk explicitly does not -- so the reliability requirement that drove the
# Cloud Run choice is better served here than by the feature we set out to use.
RESOURCE_CLASSES: dict[str, ResourceClass] = {
    "standard": ResourceClass("standard", cpu=4, memory_gib=8, disk_gib=4, units=1),
    "browser": ResourceClass("browser", cpu=8, memory_gib=16, disk_gib=8, units=2),
    "large": ResourceClass("large", cpu=8, memory_gib=32, disk_gib=16, units=4),
}


# ---------------------------------------------------------------------------
# The inputs a caller may send a runner (contract request 25)
# ---------------------------------------------------------------------------
#
# Accepted by the owner on #142, 2026-09-25: "RunnerProfile.inputs goes in the
# frozen catalogue and the API enforces it for every caller, not only the
# bridge." Invariant 10 is unchanged -- a caller names a profile and supplies
# DATA, never an image, a command, a resource spec or a backend -- but an input
# means something only to the runner that reads it, and to another runner it
# can mean something else: `input.model` is read by the CLI runners and would
# choose the model a `claude-code` agent runs. So what may be sent is declared
# per profile, here, once. swarm-api refuses anything else at submission and
# the plugin's bridge refuses it sooner; both read this, and neither keeps a
# table of its own.

#: The kinds an input can be. `filename` is a bare file name with no
#: directory: the worker keeps only the last path segment of an artifact name
#: (`RunnerContext.artifact_path`), so `../x` would quietly become `x`.
INPUT_KINDS = ("number", "integer", "boolean", "string", "filename")


class InputRefused(ValueError):
    """An input a profile does not accept, and why.

    `key` is the first key refused and `keys` every one; `expected` is the
    declared bound a value broke, or None when the key is not declared at all.
    The message names the key and the bound, and never repeats a value longer
    than it has to.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str,
        keys: tuple[str, ...] = (),
        expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.keys = keys or (key,)
        self.expected = expected


@dataclass(frozen=True)
class RunnerInput:
    """One key of a task's `input` that a runner reads, and what it must be.

    The bounds are what the runner can do anything useful with. A value outside
    them is refused at submission rather than coerced, or crashed on, after
    admission has spent a lease on it.
    """

    kind: str
    minimum: float | None = None
    maximum: float | None = None
    #: What the runner does with it, in the runner's own terms.
    means: str = ""
    #: Values inside the bounds that are refused all the same, each with what
    #: the platform would read it as instead -- the mock's exit codes 77, 78
    #: and 143. Pairs, not a dict, so the declaration stays hashable.
    refused: tuple[tuple[Any, str], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in INPUT_KINDS:
            raise ValueError(f"input kind {self.kind!r} is not one of {INPUT_KINDS}")
        numeric = self.kind in ("number", "integer")
        if not numeric and (self.minimum is not None or self.maximum is not None or self.refused):
            raise ValueError(f"a {self.kind} input has no bounds; only a number or an integer does")
        for bound in (self.minimum, self.maximum):
            if bound is not None and not math.isfinite(bound):
                raise ValueError("an input's bounds must be finite numbers")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"input bounds {self.minimum}..{self.maximum} admit nothing")
        for _, reads_as in self.refused:
            if not reads_as:
                raise ValueError("a refused value must say what the platform would read it as")

    def describe(self) -> str:
        """`integer 1..255 except 77, 78, 143`: the kind and the bound, as a caller reads it."""
        if self.minimum is not None and self.maximum is not None:
            text = f"{self.kind} {self.minimum:g}..{self.maximum:g}"
        elif self.minimum is not None:
            text = f"{self.kind} >= {self.minimum:g}"
        elif self.maximum is not None:
            text = f"{self.kind} <= {self.maximum:g}"
        else:
            text = self.kind
        if self.refused:
            text += " except " + ", ".join(str(value) for value, _ in self.refused)
        return text

    def check(self, key: str, value: Any) -> Any:
        """`value`, normalised (an integral float becomes an int), or InputRefused."""
        expected = self.describe()
        article = "an" if expected[0] in "aeiou" else "a"
        wanted = f"input {key!r} must be {article} {expected}"

        def refuse(detail: str = "") -> InputRefused:
            return InputRefused(
                f"{wanted}, not {value!r}{detail}", key=key, expected=expected
            )

        if self.kind in ("number", "integer"):
            # A bool is an int to Python and is refused: `sleep_seconds: true`
            # is a caller who meant something else.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise refuse()
            # json.loads reads NaN and Infinity, and every comparison with NaN
            # is False, so the bounds below would let it straight through.
            if isinstance(value, float) and not math.isfinite(value):
                raise refuse(" -- NaN and Infinity are not numbers a runner can use")
            if self.kind == "integer":
                if isinstance(value, float):
                    if not value.is_integer():
                        raise refuse()
                    value = int(value)
            if self.minimum is not None and value < self.minimum:
                raise refuse()
            if self.maximum is not None and value > self.maximum:
                raise refuse()
            for refused, reads_as in self.refused:
                if value == refused:
                    raise refuse(f": {reads_as}")
            return value
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise refuse(" (true or false)")
            return value
        if not isinstance(value, str):
            raise refuse()
        if self.kind == "filename" and (
            not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value
        ):
            raise refuse(" -- a bare file name, with no directory")
        return value


def _frozen_inputs(declared: Mapping[str, RunnerInput] | None) -> Mapping[str, RunnerInput] | None:
    """A read-only copy, so no caller can widen a profile's declaration in place."""
    return None if declared is None else MappingProxyType(dict(declared))


@dataclass(frozen=True)
class RunnerProfile:
    name: str
    image: str
    resource_class: str
    backend: Backend
    #: The argv of the RUNNER, which the worker lifecycle starts as a supervised
    #: CHILD (`agent_worker.lifecycle._runner_argv`). NEVER a container command:
    #: both worker images' ENTRYPOINT is the lifecycle (`tini -- python -m
    #: agent_worker`), and a container `command` replaces it, switching off
    #: fencing, cancel, heartbeat, checkpointing and lease release at once. A
    #: dispatcher names the profile in RUNNER_PROFILE and sets no command.
    #:
    #: Called `command` until 2026-09-24. Under that name both dispatchers put
    #: it on the container, and every GKE pod ran the bare runner (incident
    #: wf_ebb3ab2d65664707a559). Renamed by contract request 18, accepted by
    #: the owner, so `list(profile.command)` can no longer be written.
    runner_argv: tuple[str, ...]
    #: Provider whose quota and credentials this runner consumes. None means the
    #: runner needs no external provider, so it works before a tenant registers
    #: any key -- which is what keeps the mock smoke path always available.
    provider: str | None = None
    secrets: tuple[str, ...] = ()
    #: True when `secrets` lists INTERCHANGEABLE credentials rather than a set
    #: that must all be present. claude-code takes either metered API access or
    #: a subscription token, never both, so requiring all of them would refuse a
    #: tenant who supplied exactly the one they pay for.
    secrets_any_of: bool = False
    #: Whether this profile may be dispatched AT ALL.
    #:
    #: A profile is disabled, not deleted, when its provider stops working.
    #: Deleting the entry would refuse new work (which is the point) but also
    #: strand anything already queued against it, and break the catalogue
    #: lookup for every task document that still names it -- 4 exist for
    #: `codex` today. The flag keeps the profile readable and makes re-enabling
    #: one word.
    #:
    #: `disabled_reason` is required when this is False and is served to the
    #: caller. "unknown runner_profile" would be a lie: the profile is known,
    #: it is refused, and a caller who cannot tell those apart goes looking for
    #: a typo that is not there.
    available: bool = True
    disabled_reason: str = ""
    supports_checkpoint: bool = True
    spot: SpotStrategy = SpotStrategy.ON_DEMAND_ONLY
    timeout_seconds: int = 3600
    #: Seconds between mandatory workspace checkpoints. This is what replaces
    #: live migration on the Cloud Run ephemeral-disk path.
    checkpoint_interval_seconds: int = 120
    #: The keys of `input`, besides `prompt`, a caller may set for this
    #: profile, each with its kind and bounds (contract request 25). swarm-api
    #: refuses any other key, from every caller, with 422 `invalid_input`; the
    #: plugin's bridge reads the same declaration to refuse it before it
    #: travels.
    #:
    #: EMPTY IS A DECLARATION: the profile takes its prompt and nothing else.
    #: That is what closes `input.model` on `claude-code`, which its runner
    #: would pass as `--model`.
    #:
    #: NONE MEANS NOT DECLARED YET, and then only the input's size is bounded,
    #: as it was for every profile before this field existed. It exists for
    #: the runners whose work IS their input -- `browser` cannot start without
    #: `url` or `actions`, `generic` without `command` -- where an empty
    #: declaration would refuse every task they run. What they declare is an
    #: open question in docs/contract-change-requests.md (25), not a default.
    #:
    #: Excluded from the hash: a mapping is not hashable, and a profile's
    #: identity is its name.
    inputs: Mapping[str, RunnerInput] | None = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if self.inputs is not None:
            for key, declared in self.inputs.items():
                if not isinstance(declared, RunnerInput):
                    raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
                if key == "prompt":
                    raise ValueError(
                        f"runner {self.name}: `prompt` is every profile's input and is not declared"
                    )
            object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
        if not self.available and not self.disabled_reason:
            raise ValueError(
                f"runner {self.name}: a disabled profile must say why. A caller "
                "told only that a known profile was refused has nothing to act on."
            )
        if self.resource_class not in RESOURCE_CLASSES:
            raise ValueError(f"runner {self.name}: unknown resource class {self.resource_class}")
        if self.spot is not SpotStrategy.ON_DEMAND_ONLY:
            raise ValueError(
                f"runner {self.name}: Spot is disabled platform-wide; Spot Pods cannot "
                "use extended run time and would violate the no-preemption requirement"
            )


#: The mock's test knobs (agent_worker/runners/mock.py), as the owner decided
#: them on #142 on 2026-09-25. Left out on purpose, because they write platform
#: records rather than shape a run: `spend`, which the worker books as the
#: attempt's cost; `provider`, which names whose quota document a park writes;
#: `credential_revoked_times` and `credential_detail`, which simulate a refused
#: credential; `quota_detail` and `reset_at`, which dress a simulated rate
#: limit. tests/unit/mcp/test_runner_inputs.py holds every key here to a
#: `payload` read in the mock's source.
_MOCK_INPUTS: dict[str, RunnerInput] = {
    "sleep_seconds": RunnerInput("number", minimum=0, means="how long the run sleeps, in total"),
    "cpu_burn_seconds": RunnerInput("number", minimum=0, means="how long it burns CPU, in total"),
    "steps": RunnerInput(
        "integer", minimum=1, means="how many progress files, and checkpoints, it writes"
    ),
    "fail": RunnerInput("boolean", means="fail on purpose, after the steps"),
    "fail_message": RunnerInput("string", means="the error a failure reports"),
    # THE WORKER DECIDES WHAT AN ATTEMPT WAS FROM ITS EXIT CODE, so a code it
    # reads as something else turns a failure on purpose into that thing
    # (`agent_worker/runners/base.py`, `lifecycle._finalise`). Minimum 1: a 0
    # beside the result.json the mock writes is recorded SUCCEEDED. The three
    # refused are the EXIT_* codes that are not a plain failure, restated
    # because the catalogue cannot import the worker; test_runner_inputs.py
    # reads base.py's EXIT_* constants and fails when one is missing here
    # (contract request 21 asks for the codes to have a shared home).
    "exit_code": RunnerInput(
        "integer",
        minimum=1,
        maximum=255,
        means="the exit code a failure uses",
        refused=(
            (77, "77 is a provider rate limit to the worker, which parks the task "
                 "instead of failing it -- and with no quota.json, again on every attempt"),
            (78, "78 is the code a runner exits with when its credential is refused, "
                 "so a failure recorded with it reads as one"),
            (143, "143 is a runner stopped by SIGTERM, which the worker records CANCELLED"),
        ),
    ),
    "artifact_text": RunnerInput("string", means="what the output artifact holds"),
    "artifact_name": RunnerInput("filename", means="the output artifact's file name"),
    # THE BOUNDED PARK. Withheld by the review of PR #201, because the mock
    # raised its rate limit on every attempt and a park does not spend one, so
    # a task sent this parked and resumed until someone cancelled it. The mock
    # now counts the attempts it has parked in its state file
    # (`quota_exhausted_times` in mock_state.json, carried to the next attempt
    # by the park's own checkpoint) and parks one attempt only.
    "quota_exhausted": RunnerInput(
        "boolean",
        means="park the first attempt on a simulated provider rate limit; the next one runs",
    ),
    # One second to one hour. A wait under the worker's in-place threshold is
    # retried in place three times and then parked, a longer one is parked at
    # once; the mock refuses every retry of the same attempt, so either way one
    # attempt parks. The task waits parked, holding no capacity, and an hour is
    # longer than any test of the park path needs to be held.
    "retry_after_seconds": RunnerInput(
        "integer",
        minimum=1,
        maximum=3600,
        means="the retry-after that simulated rate limit reports",
    ),
}


RUNNER_PROFILES: dict[str, RunnerProfile] = {
    "mock": RunnerProfile(
        name="mock",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
        provider=None,          # no key required -- smoke tests must always work
        timeout_seconds=600,
        checkpoint_interval_seconds=30,
        inputs=_MOCK_INPUTS,
    ),
    "generic": RunnerProfile(
        name="generic",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.generic"),
        provider=None,
        # NOT DECLARED YET. The runner cannot start without `input.command`,
        # the NAME of an entry in its own catalogue, so an empty declaration
        # would refuse every task it runs. See RunnerProfile.inputs.
        inputs=None,
    ),
    "claude-code": RunnerProfile(
        name="claude-code",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        # BOTH are accepted, and a tenant supplies exactly one. Claude Code runs
        # on either metered API access (ANTHROPIC_API_KEY) or a Claude
        # subscription token (CLAUDE_CODE_OAUTH_TOKEN, from
        # `claude setup-token`). Requiring the API key would make a tenant who
        # already pays for a subscription buy metered access on top of it.
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
    ),
    "codex": RunnerProfile(
        name="codex",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.codex"),
        provider="openai",
        secrets=("OPENAI_API_KEY",),
        timeout_seconds=7200,
        # DISABLED 2026-09-23 by the owner's decision: the platform is focusing
        # on Claude, and codex does not currently work here anyway. A twenty-step
        # load test that day dispatched four codex steps and all four failed
        # with "openai refused the credential" -- the tenant's
        # swarm-tenant-eng-openai holds a single version written 2026-09-16 that
        # the provider rejects.
        #
        # Left in the catalogue rather than deleted, so the four task documents
        # that name it stay readable and re-enabling is one word. Nothing was
        # queued or running against it when this landed.
        available=False,
        disabled_reason=(
            "codex is disabled on this platform. The provider refused the "
            "registered credential and the platform is focused on Claude. Use "
            "claude-code."
        ),
    ),
    "browser": RunnerProfile(
        name="browser",
        image="agent-runtime-browser",
        resource_class="browser",
        # Chromium under Cloud Run needs a large /dev/shm; GKE gives us direct
        # control over that, so browser work stays on Autopilot.
        backend=Backend.GKE_AUTOPILOT,
        runner_argv=("python", "-m", "agent_worker.runners.browser"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY",),
        timeout_seconds=5400,
        # NOT DECLARED YET. The runner cannot start without `input.url` or
        # `input.actions`, so an empty declaration would refuse every task it
        # runs, the smoke suite's GKE row included. See RunnerProfile.inputs.
        inputs=None,
    ),
}


def resolve_backend(profile: RunnerProfile) -> Backend:
    """Resolve AUTO to a concrete backend.

    Cloud Run Jobs is preferred for everything it can hold, because it has no
    nodes, no autoscaler and no node upgrades -- far fewer ways for the platform
    to kill a task. GKE Autopilot takes what does not fit.
    """
    if profile.backend is not Backend.AUTO:
        return profile.backend
    rc = RESOURCE_CLASSES[profile.resource_class]
    if rc.cpu <= 8 and rc.memory_gib <= 32:
        return Backend.CLOUD_RUN_JOB
    return Backend.GKE_AUTOPILOT


def check_inputs(profile: RunnerProfile, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The inputs `raw` sets, each checked against what `profile` declares.

    `raw` holds the keys BESIDES the prompt. Returns them normalised (an
    integral float for an integer input becomes an int), or raises
    InputRefused: for every key the profile does not declare, naming them all,
    or for the first declared key whose value is out of its bounds, naming the
    bound. A profile whose inputs are not declared yet (`inputs is None`)
    declares nothing here; a caller that bounds such a profile by size alone
    decides that before it asks.
    """
    declared = profile.inputs or {}
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        if declared:
            offered = ", ".join(
                f"{key} ({spec.describe()})" for key, spec in sorted(declared.items())
            )
            message = (
                f"runner profile {profile.name!r} does not declare {unknown} as an input; "
                f"it declares {offered}"
            )
        else:
            message = (
                f"runner profile {profile.name!r} takes its prompt and no other input, "
                f"so {unknown} cannot be sent"
            )
        raise InputRefused(message, key=unknown[0], keys=tuple(unknown))
    return {key: declared[key].check(key, raw[key]) for key in sorted(raw)}
