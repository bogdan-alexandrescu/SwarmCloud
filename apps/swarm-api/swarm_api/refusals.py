"""Refusal switches: a NEW refusal ships switched off.

Owner decision 2026-10-08 (observer proposal I). On 2026-10-07 #845 deployed
a new refusal -- 403 REPOSITORY_NOT_GRANTED -- before #840, the GitHub App id
and slug a person needs to connect GitHub and choose repositories at all. The
refusal was correct and nobody could satisfy it, so every person's submission
was refused for about two hours. A refusal is only as safe as whatever lets a
caller avoid it, and that is often another change, deployed separately.

So from now on a refusal added to swarm-api ships REPORT-ONLY: the request
goes through, and the refusal it would have made is logged with its code. A
later release turns it on once the log shows only the refusals that were
meant. Every refusal that existed on main on 2026-10-08 is ESTABLISHED and
always enforced; `tests/unit/control_plane/test_new_refusals_ship_off.py`
lists them and fails on a 4xx code that is neither listed there nor in
`SWITCHES` below.

How a refusal is added:

    1. Give it its own code (an `ApiError` subclass with its own `code`, or a
       new `onboarding.COPY` failure code). A new refusal that reuses
       `forbidden` cannot be switched, because the code is the switch's key.
    2. Register a `Switch` for that code in `SWITCHES`, default off.
    3. Raise it through `refuse(error)`, never `raise error`, and write the
       call site so that execution continuing past `refuse` is the request
       being let through.

How it is turned on: `REFUSAL_<CODE>=on` in the service's environment,
rendered from terraform's `api_refusals` map (terraform/infra/locals.tf,
`local.api_refusal_env`). `off` turns it back off without a redeploy of the
code. A value other than on/off, or a REFUSAL_ variable naming no switch, is
refused at process start (`validate_environment`, called from
`ApiSettings.from_env`): a typo in a switch is a refusal someone believes is
on, the REQUIRE_AUTH mistake settings.py describes.

`REPOSITORY_GRANTS_ENFORCED` predates this module and is NOT one of these
switches: its off position is not "let the request through unchanged" but
"run with the tenant token instead" (`SubmissionService._resolve_forge`),
so it keeps its own setting. REPOSITORY_NOT_GRANTED is in the established
list because it is on main, behind that setting, today.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ApiError

log = logging.getLogger(__name__)

ENV_PREFIX = "REFUSAL_"
ON = "on"
OFF = "off"


def env_name(code: str) -> str:
    """The environment variable that switches the refusal `code`."""
    return ENV_PREFIX + re.sub(r"[^A-Z0-9]+", "_", code.upper()).strip("_")


@dataclass(frozen=True)
class Switch:
    """One refusal that can be off. `code` is the `ApiError.code` (or the
    `detail.failure_code`) it gates; `why` says what a caller needs before it
    can be on, so whoever turns it on knows what to check first."""

    code: str
    why: str
    #: Enforced when the environment says nothing. False for every new
    #: refusal; a later release may flip it once the switch has been on in
    #: every environment.
    default: bool = False

    @property
    def env(self) -> str:
        return env_name(self.code)


#: Every switched refusal, by code. Empty on the day this module landed: every
#: refusal on main then was established and stays enforced.
SWITCHES: dict[str, Switch] = {
    # The schedule tick's budget and failure stops (docs/schedules.md §4.3,
    # §4.4, lane S2). Each refuses a SCHEDULE'S OWN next firing, never a
    # person's request, and is raised by `schedulefire` through `refuse`.
    # Off, the firing goes ahead and the log names the stop it would have
    # made. A departed owner (OWNER_NOT_MEMBER) is NOT here: submitting as
    # someone who left the tenant is an isolation breach (invariant 9), and
    # it is the established `owner_not_member` refusal.
    "BUDGET_EXHAUSTED": Switch(
        code="BUDGET_EXHAUSTED",
        why="a schedule's firing is skipped when today's reported spend plus the per-run "
        "cap for every live item and unreported attempt passes per_day_usd (§4.3). On "
        "once the budget figures of S1 have been read against real firings' costs",
    ),
    "RUN_OVER_BUDGET": Switch(
        code="RUN_OVER_BUDGET",
        why="a firing whose work recorded more than per_run_usd ends failed and pauses its "
        "schedule (§4.3). On once attempts report cost_usd for the schedule's profiles",
    ),
    "CONSECUTIVE_FAILURES": Switch(
        code="CONSECUTIVE_FAILURES",
        why="a schedule pauses after N failed or refused firings in a row (§4.4, default "
        "3). On once a schedule's failures are visible in the console (S7), so a "
        "pause has a screen that says why",
    ),
}


def _value(name: str, raw: str) -> bool | None:
    value = raw.strip().lower()
    if not value:
        return None
    if value == ON:
        return True
    if value == OFF:
        return False
    raise ValueError(f"{name} must be {ON!r} or {OFF!r}, got {raw!r}")


def validate_environment(environ: Mapping[str, str] | None = None) -> None:
    """Refuse, at start, a REFUSAL_ variable that names no switch or says
    neither on nor off."""
    environ = os.environ if environ is None else environ
    known = {switch.env for switch in SWITCHES.values()}
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        if name not in known:
            raise ValueError(
                f"{name} names no refusal switch in swarm_api.refusals.SWITCHES; "
                "a switch that matches nothing would be a refusal believed on. "
                "Remove the variable or correct its name."
            )
        _value(name, raw)


def _switch_for(error: ApiError) -> Switch | None:
    switch = SWITCHES.get(error.code)
    if switch is None:
        failure_code = error.detail.get("failure_code")
        if isinstance(failure_code, str):
            switch = SWITCHES.get(failure_code)
    return switch


def enforced(code: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the refusal `code` is on. A code with no switch is established
    and always on."""
    switch = SWITCHES.get(code)
    if switch is None:
        return True
    environ = os.environ if environ is None else environ
    value = _value(switch.env, environ.get(switch.env, ""))
    return switch.default if value is None else value


def refuse(error: ApiError, environ: Mapping[str, str] | None = None) -> None:
    """Raise `error` if its refusal is on; otherwise log it and RETURN.

    The environment is read on each call, so a revision that changes a
    switch takes effect without a code change; Cloud Run starts a new
    revision for any environment change, so nothing flips mid-process.
    """
    switch = _switch_for(error)
    if switch is None:
        raise error
    environ = os.environ if environ is None else environ
    value = _value(switch.env, environ.get(switch.env, ""))
    if switch.default if value is None else value:
        raise error
    # Code, status and the served sentence: what the caller WOULD have been
    # told. ApiError messages are served to callers, so they are token-free
    # by the same rule (errors.py).
    log.warning(
        "refusal report-only code=%s status=%s switch=%s=off message=%s",
        switch.code, error.status_code, switch.env, error.message,
    )
