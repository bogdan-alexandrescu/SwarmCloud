"""The runner catalogue, as a session is allowed to see it.

WHY THIS EXISTS AT ALL. Invariant 10 says an API caller picks a `runner_profile`
BY NAME and never supplies an image, a command, a resource spec or a backend.
Every write path in this bridge already honours that -- `client.dispatch` has no
image parameter and `workflows.build_steps` refuses an unknown step key by name.
What was missing was the other half of "by name": nowhere in the plugin could a
session find out what the names ARE. The `delegate` skill listed them in a
sentence of prose, which is a restatement of the frozen contract in a file no
test read, and which was already one edit away from being wrong -- `codex` was
disabled on 2026-09-23 and the sentence had to be changed by hand to say so.

So the names come from `swarm_common.profiles.RUNNER_PROFILES` and from nowhere
else. `render.py` already imports `swarm_common.states` for exactly this reason;
this is the same move for the same reason.

WHAT THIS DELIBERATELY DOES NOT SHOW: the image and the command. They are on
every `RunnerProfile` and it would be one dictionary key to serve them. They are
withheld because the invariant is not only "a caller may not SUPPLY an image" --
it is that the image is not part of the caller's vocabulary. A session that can
read `agent-runtime-browser` off a tool response is a session that will sooner
or later offer to change it, and the first thing anyone does with a field they
can see is ask for a parameter that sets it. Nothing downstream needs them: the
name is what dispatch takes, and the size, the backend and the credential
requirement are what a chooser actually weighs.

The `available` flag is served WITH its reason, and that pairing is the point.
`RunnerProfile.__post_init__` already refuses a disabled profile that does not
say why, on the grounds that "a caller told only that a known profile was
refused has nothing to act on". Serving the flag without the reason here would
reintroduce exactly that, one layer up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES, resolve_backend

from .client import SwarmError


# --------------------------------------------------------------------------
# The inputs a caller may send a runner (#142)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InputSpec:
    """One input a runner reads from its task's `input`, and what it must be.

    `kind` is one of `number`, `integer`, `boolean`, `string` and `filename` (a
    bare file name, no directory). The bounds are what the runner can do
    anything useful with; a value outside them is refused here, before it has
    been admitted, rather than coerced or crashed on four minutes later.
    """

    kind: str
    minimum: float | None = None
    maximum: float | None = None
    means: str = ""
    #: Values inside the bounds that are refused all the same, each with what
    #: the platform would read it as instead. A tuple of pairs, not a dict, so
    #: the spec stays hashable.
    refused: tuple[tuple[Any, str], ...] = ()

    def describe(self) -> str:
        if self.minimum is not None and self.maximum is not None:
            text = f"{self.kind} {self.minimum:g}..{self.maximum:g}"
        elif self.minimum is not None:
            text = f"{self.kind} >= {self.minimum:g}"
        else:
            text = self.kind
        if self.refused:
            text += " except " + ", ".join(str(value) for value, _ in self.refused)
        return text


#: THE ONE PLACE a runner's caller-settable inputs are named, keyed by profile
#: NAME, until the frozen catalogue can declare them itself -- contract request
#: 25 in docs/contract-change-requests.md asks for `RunnerProfile.inputs`, and
#: when it lands this table is read from there and deleted.
#:
#: WHY PER PROFILE, AND WHY THIS IS NOT INVARIANT 10 LOOSENED. A caller still
#: names a profile and supplies DATA; nothing here is an image, a command, a
#: resource spec or a backend parameter, and none of those can be declared. But
#: an input means something only to the runner that reads it, and to another
#: runner it can mean something else entirely: `input.model` is read by the CLI
#: runners and would select the model a `claude-code` agent runs, which is the
#: contract change test_model_flag_is_attribution_only.py exists to stop. So a
#: profile that declares nothing takes nothing, and a key a profile does not
#: declare is refused by name -- never dropped, never passed through.
#:
#: WHAT THE MOCK DECLARES, and what it deliberately does not. It reads more
#: than this (`agent_worker/runners/mock.py`); left out are the keys that write
#: platform records rather than shape the run: `spend`, which the worker books
#: as the attempt's cost; `provider`, which names whose quota document a park
#: writes; `credential_revoked_times`, `credential_detail`, `quota_detail` and
#: `reset_at`, which simulate a provider's refusal.
#:
#: `quota_exhausted` AND `retry_after_seconds` ARE LEFT OUT TOO, because a
#: caller could not stop what they start (review of PR #201). The mock raises
#: its rate limit on EVERY attempt -- the check runs before any saved state
#: and keeps no count, unlike `credential_revoked_times` -- and a park does
#: not spend an attempt: `admission.acquire_lease_in_transaction` counts it,
#: but neither the worker's `ControlPlane.park` nor the scheduler's
#: `promote_to_ready` reads `retries_exhausted`, and neither does admission.
#: So a mock step sent `{"quota_exhausted": true}`
#: re-leased and re-parked, in a new Cloud Run execution each time, until
#: somebody ran `swarm cancel`. The park path wants a bounded park in the
#: mock -- a `quota_exhausted_times` counter kept in its state file -- which
#: is a worker change and an owner's decision, not this table's.
#: `retry_after_seconds` only shapes that signal, so it goes with it.
#:
#: tests/unit/mcp/test_runner_inputs.py holds every name here to the frozen
#: catalogue, every key to a `payload` read in the runner's source, and no key
#: to the branch that raises the rate limit.
DECLARED_INPUTS: dict[str, dict[str, InputSpec]] = {
    "mock": {
        "sleep_seconds": InputSpec("number", minimum=0, means="how long the run sleeps, in total"),
        "cpu_burn_seconds": InputSpec("number", minimum=0, means="how long it burns CPU, in total"),
        "steps": InputSpec("integer", minimum=1, means="how many progress files, and checkpoints, it writes"),
        "fail": InputSpec("boolean", means="fail on purpose, after the steps"),
        "fail_message": InputSpec("string", means="the error a failure reports"),
        # THE WORKER DECIDES WHAT AN ATTEMPT WAS FROM ITS EXIT CODE, so a code
        # it reads as something else turns a failure on purpose into that
        # thing (`agent_worker/runners/base.py`, `lifecycle._finalise`).
        # Minimum 1: a 0 beside the result.json the mock writes is recorded
        # SUCCEEDED. The three refused below are the EXIT_* codes that are
        # not a plain failure; restated here because swarm-mcp cannot import
        # the worker, and test_runner_inputs.py reads base.py's EXIT_*
        # constants and fails if one is missing (contract request 21 asks for
        # the codes to have a shared home).
        "exit_code": InputSpec(
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
        "artifact_text": InputSpec("string", means="what the output artifact holds"),
        "artifact_name": InputSpec("filename", means="the output artifact's file name"),
    },
}


def declared_inputs(name: str) -> dict[str, InputSpec]:
    """The inputs this profile declares; `{}` for one that declares none."""
    return dict(DECLARED_INPUTS.get(name, {}))


def _declaring() -> str:
    return ", ".join(sorted(DECLARED_INPUTS)) or "none"


def _check_value(key: str, value: Any, spec: InputSpec, where: str) -> Any:
    prefix = f"{where}: " if where else ""
    wanted = f"{prefix}input {key!r} must be a {spec.describe()}"
    if spec.kind in ("number", "integer"):
        # A bool is an int to Python and is refused here: `sleep_seconds: true`
        # is a caller who meant something else.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SwarmError(f"{wanted}, not {value!r}")
        if spec.kind == "integer" and not (isinstance(value, int) or float(value).is_integer()):
            raise SwarmError(f"{wanted}, not {value!r}")
        if spec.kind == "integer":
            value = int(value)
        if spec.minimum is not None and value < spec.minimum:
            raise SwarmError(f"{wanted}, not {value!r}")
        if spec.maximum is not None and value > spec.maximum:
            raise SwarmError(f"{wanted}, not {value!r}")
        for refused, reads_as in spec.refused:
            if value == refused:
                raise SwarmError(f"{wanted}, not {value!r}: {reads_as}")
        return value
    if spec.kind == "boolean":
        if not isinstance(value, bool):
            raise SwarmError(f"{wanted} (true or false), not {value!r}")
        return value
    if not isinstance(value, str):
        raise SwarmError(f"{wanted}, not {value!r}")
    if spec.kind == "filename":
        # The worker strips a path to its last segment (`RunnerContext.
        # artifact_path`), so `../x` would quietly become `x`. Said instead.
        if not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value:
            raise SwarmError(f"{wanted} -- a bare file name with no directory -- not {value!r}")
    return value


def check_inputs(name: str, raw: Any, *, where: str = "") -> dict[str, Any]:
    """The declared inputs `raw` asks for, checked; refuses anything else. Returns them.

    `raw` is None (nothing asked for) or an object. The profile NAME has already
    been checked by `check`; this is only about what may travel with it.
    """
    prefix = f"{where}: " if where else ""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SwarmError(
            f"{prefix}`inputs` must be an object of the inputs {name} declares, not "
            f"{type(raw).__name__}"
        )
    declared = DECLARED_INPUTS.get(name, {})
    if raw and not declared:
        raise SwarmError(
            f"{prefix}runner profile {name!r} declares no inputs, so none can be sent "
            f"({sorted(raw)} were given). Only these profiles declare any: {_declaring()} "
            "-- see `swarm_profiles`. A step's instructions go in `prompt`."
        )
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        offered = ", ".join(f"{key} ({spec.describe()})" for key, spec in sorted(declared.items()))
        raise SwarmError(
            f"{prefix}{name} does not declare {unknown} as an input, so it is not sent. "
            f"It declares: {offered}. The instructions go in `prompt`; an image, a "
            "command, a resource spec, a backend or a model are never sent "
            "(invariant 10)."
        )
    return {key: _check_value(key, raw[key], declared[key], where) for key in sorted(raw)}


def parse_input_flags(name: str, flags: list[str] | None, *, where: str = "") -> dict[str, Any]:
    """`swarm dispatch --input KEY=VALUE ...`, typed by what `name` declares.

    A string input keeps its text as typed -- `fail_message=123` is the message
    "123" -- and every other kind is read as JSON (`120`, `2.5`, `true`), so
    the value is typed by the DECLARATION rather than by whatever JSON happens
    to make of the text. The result goes through `check_inputs`.
    """
    prefix = f"{where}: " if where else ""
    declared = DECLARED_INPUTS.get(name, {})
    raw: dict[str, Any] = {}
    for flag in flags or []:
        key, sep, text = str(flag).partition("=")
        key = key.strip()
        if not sep or not key:
            raise SwarmError(f"{prefix}--input takes KEY=VALUE, not {flag!r}")
        spec = declared.get(key)
        if spec is None or spec.kind in ("string", "filename"):
            raw[key] = text
            continue
        try:
            raw[key] = json.loads(text)
        except json.JSONDecodeError:
            raw[key] = text
    return check_inputs(name, raw or None, where=where)


def names() -> list[str]:
    """Every profile name in the catalogue, including disabled ones.

    Disabled profiles are INCLUDED because they are still names the platform
    knows. A caller who typed `codex` needs to be told it is refused and why,
    not that it does not exist -- which is the distinction the frozen catalogue
    goes out of its way to preserve by disabling rather than deleting.
    """
    return sorted(RUNNER_PROFILES)


def dispatchable() -> list[str]:
    """The names a dispatch can actually name today."""
    return sorted(name for name, p in RUNNER_PROFILES.items() if p.available)


def public_profile(name: str) -> dict[str, Any]:
    """One profile, in the fields a chooser needs and no others.

    An ALLOW-LIST, not a filtered copy of the dataclass. `sc`'s account table
    uses the same shape for the same reason: a projection built by removing
    fields serves every field somebody adds later, and here the field somebody
    adds later could be the image.
    """
    profile = RUNNER_PROFILES[name]
    resource = RESOURCE_CLASSES[profile.resource_class]
    out: dict[str, Any] = {
        "name": profile.name,
        "available": profile.available,
        # Resolved, not raw. A profile may be declared AUTO and the caller
        # cares which backend it will actually land on -- that is the answer to
        # "why did this one queue behind GKE capacity when the others did not".
        "backend": resolve_backend(profile).value,
        "resource_class": profile.resource_class,
        "cpu": resource.cpu,
        "memory_gib": resource.memory_gib,
        # A slice OF memory, not extra capacity: the workspace is a tmpfs. Named
        # `workspace_gib` rather than `disk_gib` because "disk" is what made
        # people read it as additional storage in the first place.
        "workspace_gib": resource.disk_gib,
        "timeout_seconds": profile.timeout_seconds,
        # WHETHER a credential is needed, never WHICH VALUE. `secrets` holds
        # variable NAMES and no material, but serving the names invites a
        # session to go looking for the values, and this bridge has no business
        # anywhere near them.
        "needs_provider_credential": profile.provider is not None,
        "provider": profile.provider,
    }
    if not profile.available:
        out["disabled_reason"] = profile.disabled_reason
    declared = declared_inputs(name)
    if declared:
        # What a caller may send this profile besides its prompt (#142), so the
        # names are learned here rather than from prose that goes stale.
        out["inputs"] = {
            key: {"kind": spec.describe(), "means": spec.means}
            for key, spec in sorted(declared.items())
        }
    return out


def catalogue() -> list[dict[str, Any]]:
    """The whole catalogue, dispatchable first, then refused.

    Ordered rather than alphabetical because the list is read to make a choice,
    and a disabled profile sorted into the middle of the usable ones is a name
    a reader picks before reaching the reason it is there.
    """
    usable = [public_profile(n) for n in dispatchable()]
    refused = [public_profile(n) for n in names() if n not in set(dispatchable())]
    return usable + refused


def check(name: str, *, where: str = "") -> str:
    """Refuse a profile name here, before it costs a dispatch. Returns the name.

    THE COST OF NOT DOING THIS IS MEASURED, not hypothetical. On 2026-09-23 a
    twenty-step run dispatched four `codex` steps and all four failed at the
    provider -- the incident recorded in `swarm_common.profiles` next to the
    `available=False` flag it produced. Every one of those four was admitted,
    leased and dispatched before anything said no. `build_steps` already makes
    this argument about a missing prompt: "a refusal at the keyboard costs
    nothing; the same refusal four minutes later costs a dispatch, a lease and
    a pod." A name the catalogue refuses is the same shape of mistake.

    IT IS NOT A SECOND OPINION ABOUT WHO MAY RUN WHAT. The API refuses these
    too, and it remains the authority -- this cannot admit anything the API
    would refuse, only refuse sooner. It is safe to be this confident locally
    for one specific reason: `apps/swarm-mcp` depends on `swarm-common`, which
    is the SAME frozen module the API imports, so the two cannot hold different
    catalogues. If that dependency ever goes away this check must go with it,
    because then it would be a copy rather than the original.

    The disabled reason is quoted from the catalogue VERBATIM rather than
    reworded. It is written to be read by the person who has to act on it, and
    a paraphrase here would be a second wording of the platform's own answer.
    """
    prefix = f"{where}: " if where else ""
    if not isinstance(name, str) or not name.strip():
        raise SwarmError(
            f"{prefix}no runner profile was named. Pick one by name: "
            f"{', '.join(dispatchable())}"
        )
    profile = RUNNER_PROFILES.get(name)
    if profile is None:
        raise SwarmError(
            f"{prefix}there is no runner profile called {name!r}. "
            f"The catalogue holds: {', '.join(names())}. "
            "A profile is chosen BY NAME -- an image, a command or a resource "
            "spec cannot be supplied instead."
        )
    if not profile.available:
        raise SwarmError(f"{prefix}{name} is refused: {profile.disabled_reason}")
    return name


def check_resource_class(name: str, *, where: str = "") -> str:
    """Refuse a resource-class name the catalogue does not hold. Returns it.

    ONLY THE NAME. Whether the class is within what the step's profile allows
    is `swarm_api.validation`'s decision and stays there; this refuses
    `standrad` for `standard`, which is a typo the API would answer with a 4xx
    after the submission had travelled.
    """
    prefix = f"{where}: " if where else ""
    if name not in RESOURCE_CLASSES:
        raise SwarmError(
            f"{prefix}there is no resource class called {name!r}. "
            f"The catalogue holds: {', '.join(sorted(RESOURCE_CLASSES))}. "
            "A class is chosen BY NAME -- cpu and memory numbers cannot be "
            "supplied instead."
        )
    return name


def backend_of(name: Any) -> str | None:
    """The backend a task's profile resolves to, or None if it is unknowable.

    NEVER RAISES, because its caller is `describe_task`, which reads a task
    document that may name a profile the catalogue no longer holds -- a run
    from before a profile was renamed, say. A result read that threw on an old
    task would take away the one thing that read is for. None here means "not
    known from the catalogue", and the caller must render it as unknown rather
    than as a default backend, which is the same three-marks rule the rest of
    this plugin is held to.
    """
    if not isinstance(name, str):
        return None
    profile = RUNNER_PROFILES.get(name)
    if profile is None:
        return None
    return resolve_backend(profile).value
