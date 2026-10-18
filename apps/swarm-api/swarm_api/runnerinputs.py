"""What a runner refuses to start without, served so no client has to guess it.

A caller names a `runner_profile` and supplies an `input` dict. The platform
checks which keys it carries -- only those the profile declares in the frozen
catalogue, `RunnerProfile.inputs` (contract request 25,
`validation.validate_runner_input`; every profile declares since contract
request 32, #218) -- but it never acts on them: the
runner does, and some runners refuse the attempt outright when a key is MISSING,
which is the other half and the one this module serves. `run_cli_agent` is the one that matters
(apps/agent-worker/agent_worker/runners/cliagent.py):

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise RunnerFailure(f"{spec.name} requires a non-empty string input.prompt")

That refusal arrives AFTER admission, after a container start and after the
tenant's credential has been mounted. A form that cannot see the rule therefore
submits work that is guaranteed to fail and only finds out minutes later, per
step, having spent a slot on each -- which is precisely what the New Workflow
screen did to every workflow it ever created, because it sent no `input` at all.

WHY THE TABLE IS KEYED BY RUNNER MODULE AND NOT BY PROFILE NAME. The frozen
catalogue already answers "which module does this profile run": it is
`RunnerProfile.runner_argv`, e.g. ("python", "-m", "agent_worker.runners.codex")
-- the argv the worker lifecycle starts as its child.
Keying on the module means a new profile pointed at an existing runner inherits
that runner's requirement with no edit here, and only a genuinely NEW runner
needs a line. A table of profile names would have to be revisited every time the
catalogue grew a name, and that revisit is the step that gets missed -- which is
the same failure in a new place.

WHY THE RULE IS RESTATED HERE AT ALL. swarm-api cannot import `agent_worker` to
ask it: images/swarm-api/Dockerfile copies `apps/common/` and `apps/swarm-api/`
and nothing else, so the import would resolve in a developer's workspace and be
an ImportError in production -- the exact trap the `google-cloud-storage` note in
apps/swarm-api/pyproject.toml records. This is the same sort of permitted copy as
Terraform's copy of the runner catalogue, and it is permitted for the same
reason: a test asserts the copy still matches what it copies. That test is
tests/unit/control_plane/test_workflow_step_input_surface.py, which reads the
worker's own source for the call and then RUNS `run_cli_agent` against an empty
payload to prove the refusal is real rather than merely written down.

Nothing here is a second admission check. The runner remains the authority; this
only lets a screen refuse locally, before a slot is spent, with the same reason
the runner would have given.
"""

from __future__ import annotations

from typing import Any

from swarm_common.profiles import RunnerInput, RunnerProfile

#: Runner module -> input keys that module refuses to start without. A key in
#: this list must be present on `input` AND be a non-empty string; that is the
#: test `run_cli_agent` applies, and a weaker one here would let `{"prompt": ""}`
#: through to the same failure.
REQUIRED_INPUT_KEYS_BY_MODULE: dict[str, tuple[str, ...]] = {
    "agent_worker.runners.claude_code": ("prompt",),
    "agent_worker.runners.codex": ("prompt",),
}


def runner_module(profile: RunnerProfile) -> str | None:
    """The module a profile's `runner_argv` runs, or None if it is not `python -m`.

    Read off the frozen catalogue rather than mapped from the profile name, so
    this stays correct for a profile that is renamed or added.
    """
    argv = tuple(profile.runner_argv)
    if len(argv) >= 3 and argv[1] == "-m":
        return argv[2]
    return None


def required_input_keys(profile: RunnerProfile) -> tuple[str, ...]:
    """Input keys this profile's runner refuses to start without.

    Empty for every runner that reads its input defensively -- `mock` falls back
    to "no prompt supplied", `generic` takes a command, `browser` takes a url or
    actions. Empty means "this screen has no local rule to apply", never "this
    input is known to be fine".
    """
    module = runner_module(profile)
    if module is None:
        return ()
    return REQUIRED_INPUT_KEYS_BY_MODULE.get(module, ())


def declared_input(declared: RunnerInput) -> dict[str, Any]:
    """One `RunnerProfile.inputs` entry as /v1/capacity serves it.

    The catalogue's own values, copied field by field and never restated: the
    kind, the bounds (a list's are its length, a string's its characters),
    the `choices` a string must be one of, whether the caller must send it,
    and what the runner does with it. That is what a form needs to draw a
    picker and refuse locally -- the MCP server picker reads `maximum`, the
    browser tool picker reads `choices` (MCP-INPUTS, part of #1433).

    BOUNDS AND MEANINGS ONLY (invariant 10). Nothing served here names an
    image, a command, a resource or a backend parameter, so nothing gives a
    caller a way to choose one; it only describes keys the caller may already
    send. A list's `items`, an object's `variants` and a number's `refused`
    values are not served: no screen draws from them, and
    `validation.validate_runner_input` still refuses what they refuse, with
    its own reason, at submission.
    """
    return {
        "kind": declared.kind,
        "minimum": declared.minimum,
        "maximum": declared.maximum,
        "choices": list(declared.choices),
        "required": declared.required,
        "means": declared.means,
    }


def input_contract(profile: RunnerProfile) -> dict[str, Any]:
    """The per-profile block served on /v1/capacity.

    A block is served for EVERY profile, including the ones with no required
    key. Serving it only for the profiles that have one would make "this API is
    too old to say" and "this profile needs nothing" the same absence, and a
    form cannot tell a missing rule from a satisfied one.

    `inputs` is the profile's declared inputs, keyed by input name in the
    catalogue's order -- an object, not a list, because the console indexes it
    by key (`declaredMcpServers`, `declaredBrowserTools`) and reads an array
    as "nothing declared". `{}` for a profile that declares nothing, by the
    same reasoning as above: measured-empty, never absent.
    """
    return {
        "required_keys": list(required_input_keys(profile)),
        "inputs": {key: declared_input(declared) for key, declared in profile.inputs.items()},
    }
