"""The step protocol and the step registry (docs/design/day-one-install.md §2.2).

Two levels, because one action often satisfies many dependencies:

* a **dependency** is a manifest entry, observed as `met`, `unmet`, `unknown`
  (the probe could not run) or `waiting`;
* a **step** is code that declares which manifest ids it `satisfies` and
  implements `check` (read-only), `plan` (what `apply` would do; printed by
  `--dry-run`), `apply` (idempotent; never run while `check` is met),
  `verify` (proves it works, not only exists) and `explain`.

A step has one of three kinds (`kind`, default `engine`):

* `engine`: Terraform applies, image copies, secret generation, policies;
* `human`: `apply` prints the exact instructions and a deep link, then the
  engine **polls the step's own check** (the person never types "done"); past
  the step's `wait` bound the install pauses with exit 4;
* `wait`: certificate, DNS, IAM propagation, the console hand-off. `apply`
  starts nothing or something cheap, then the engine polls `check` with a
  countdown up to `wait`'s bound. A wait may carry `triggered` steps: on every
  poll the engine checks each one's `trigger(ctx)` and runs the ones whose
  trigger has landed (how the hand-off runs infra passes 3 and 4, plan §5.5).

**Pages, opened and then polled.** A human step's exact page (its
`deep_link(ctx)`, else the first URL its plan names) is opened in the browser
by the engine once, after `apply` and before the first poll, through
`open_page`; the person never copies a URL and never presses Enter: the check
sees the change. A wait or engine step that sends a person to a page (the DNS
record, the Desktop client, the console hand-off) calls `open_page` itself.

A stage-0 step may declare `read_only = True`: its `apply` only asks and
reads (preflight), so `--dry-run` runs it (design §2.4). No other step's
`apply` runs in a dry run.

**The registry is discovered, never listed** (plan §1 conventions): every
module in this package that defines `STEPS` (a sequence of steps) contributes
them, so each Day One lane adds its own module and no two lanes edit a shared
file.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import pkgutil
import re
import sys
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..exitcodes import STATUSES
from ..waits import WaitSpec

ENGINE_STEP = "engine"
HUMAN_STEP = "human"
WAIT_STEP = "wait"
KINDS = (ENGINE_STEP, HUMAN_STEP, WAIT_STEP)


@dataclass(frozen=True)
class Observation:
    """What a check or a verify saw. `outputs` are non-secret values a later
    step needs (an id, an audience); they are recorded in the state."""

    status: str
    detail: str = ""
    outputs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"observation status {self.status!r} is not one of {STATUSES}")


@dataclass(frozen=True)
class Plan:
    """What `apply` would do, in words a person can read before it happens."""

    summary: str
    actions: tuple[str, ...] = ()

    def digest(self) -> str:
        body = json.dumps([self.summary, list(self.actions)]).encode()
        return hashlib.sha256(body).hexdigest()[:16]


@runtime_checkable
class Step(Protocol):
    id: str
    stage: int
    satisfies: tuple[str, ...]

    def check(self, ctx: Any) -> Observation: ...

    def plan(self, ctx: Any) -> Plan: ...

    def apply(self, ctx: Any, plan: Plan) -> None: ...

    def verify(self, ctx: Any) -> Observation: ...

    def explain(self) -> str: ...


@runtime_checkable
class TriggeredStep(Step, Protocol):
    def trigger(self, ctx: Any) -> bool: ...


def kind_of(step: Step) -> str:
    kind = getattr(step, "kind", ENGINE_STEP)
    if kind not in KINDS:
        raise ValueError(f"step {step.id}: kind {kind!r} is not one of {KINDS}")
    return kind


def wait_of(step: Step) -> WaitSpec | None:
    return getattr(step, "wait", None)


def triggered_of(step: Step) -> tuple[TriggeredStep, ...]:
    return tuple(getattr(step, "triggered", ()) or ())


#: Set to 1 to print pages without opening a browser (SSH sessions, CI, `/sc:install`).
NO_BROWSER = "SWARM_NO_BROWSER"

_URL = re.compile(r"https://[^\s,;)]+")


def has_display(env: Mapping[str, str] | None = None, platform: str | None = None) -> bool:
    """Whether a browser opened here would be in front of the person.

    Linux and the BSDs need an X or Wayland display: without one `webbrowser`
    falls back to a text browser (lynx, w3m) that would take over this
    terminal. A Mac or Windows session has a display unless it is reached over
    SSH, where the browser would open on a screen nobody is looking at.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform == "darwin" or platform.startswith("win"):
        return not (env.get("SSH_CONNECTION") or env.get("SSH_TTY"))
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def open_page(url: str, *, why: str, say: Callable[[str], None] = print,
              env: Mapping[str, str] | None = None, platform: str | None = None,
              browser: Callable[[str], object] | None = None) -> None:
    """Print `why` and `url`, then open `url` in the default browser unless
    SWARM_NO_BROWSER=1 or there is no display. It never raises: a page that
    cannot be opened is still printed, and the caller goes on polling.

    `say`, `env`, `platform` and `browser` are seams for tests and for a
    step that prints through its own writer; callers pass `url` and `why`.
    """
    env = os.environ if env is None else env
    try:
        say(f"{why}\n  {url}")
    except Exception:  # noqa: BLE001 - printing a page must never stop the install
        pass
    try:
        if str(env.get(NO_BROWSER, "")).strip() in ("1", "true", "yes"):
            return
        if not has_display(env, platform):
            return
        (browser or _browser)(url)
    except Exception:  # noqa: BLE001 - a browser that fails to open is not a failed step
        return


def _browser(url: str) -> object:
    return webbrowser.open(url, new=2)


def deep_link_of(step: Step, ctx: Any, plan: Plan | None = None) -> str | None:
    """The page a human step's person works on: its own `deep_link(ctx)`, else
    the first https URL its plan names, else None."""
    link = getattr(step, "deep_link", None)
    if callable(link):
        found = link(ctx)
        if found:
            return str(found)
    for line in (plan.actions if plan is not None else ()):
        match = _URL.search(line)
        if match:
            return match.group(0).rstrip(".:")
    return None


def check_steps(steps: Sequence[Step]) -> None:
    """Every step (and every triggered step) has a unique id and a known kind;
    a `wait` step has a bound."""
    seen: set[str] = set()
    pending = list(steps)
    while pending:
        step = pending.pop(0)
        if step.id in seen:
            raise ValueError(f"two steps have the id {step.id!r}")
        seen.add(step.id)
        if not 0 <= step.stage <= 15:
            raise ValueError(f"step {step.id}: stage {step.stage} is outside 0..15")
        if kind_of(step) == WAIT_STEP and wait_of(step) is None:
            raise ValueError(f"wait step {step.id} has no bound (`wait`)")
        pending.extend(triggered_of(step))


def discover(package: str = __name__) -> list[Step]:
    """Every step every module of `package` declares in `STEPS`, by module name."""
    root = importlib.import_module(package)
    found: list[Step] = []
    for info in sorted(pkgutil.iter_modules(root.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{package}.{info.name}")
        found.extend(getattr(module, "STEPS", ()))
    check_steps(found)
    return found
