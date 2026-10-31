"""`swarm addon enable|disable <name>`: one add-on on or off, with its own
preflight, secret link, switch and check (docs/design/day-one-install.md §12;
docs/design/day-one-plan.md lane DAYONE-ADDON-1, part of #2688;
`swarm_install.addons` holds the add-ons and says why each phase exists).

    swarm addon enable <name> --project P [--owner alice@example.com] [--in-terminal]
        [--tailscale-target host.tailnet.ts.net] [--github-repository owner/repo]
        [--sandbox-repository owner/repo] [--runner-label swarm-ci]
        [--tenants id[,id...]] [--json-events]
    swarm addon disable <name> --project P [--delete-secrets] [--json-events]
    swarm addon status --project P

No flag takes a secret's value: a person's credential goes in through the
console's one-time link (for --owner, default the installer) or, with
--in-terminal, the hidden prompt on /dev/tty, never stdin.
"""

from __future__ import annotations

import argparse
import re
import sys
from types import SimpleNamespace
from typing import Any

from .. import addons
from ..engine import _Stop  # how a step with no terminal pauses the run (secret_entry)
from ..events import Events
from ..exitcodes import MET, ExitCode
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, StateStore, local_path, state_bucket
from ..waits import SystemClock, WaitGaveUp

NAME = "addon"
HELP = "turn an add-on (bright-data-egress, tailscale, typesafe, brave-search, ci) on or off"

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: flag dest -> the add-on choice it answers.
CHOICE_FLAGS = {
    "tailscale_target": "tailscale_egress_target",
    "github_repository": "github_repository",
    "sandbox_repository": "sandbox_repository",
    "runner_label": "runner_label",
    "tenants": "tenants",
}


def _project(value: str) -> str:
    if not _PROJECT_ID.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a Google Cloud project id")
    return value


def _email(value: str) -> str:
    if not _EMAIL.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an email address")
    return value.strip().lower()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True, metavar="ACTION")
    enable = sub.add_parser("enable", help="turn an add-on on: preflight, secret link, switch, check")
    enable.add_argument("name", choices=addons.NAMES)
    enable.add_argument("--project", required=True, type=_project, help="the install's Google Cloud project")
    enable.add_argument("--owner", type=_email, default=None,
                        help="who opens the secret's one-time console link (default: the installer)")
    enable.add_argument("--in-terminal", action="store_true",
                        help="type the secret at a hidden prompt on this terminal instead of the console link")
    enable.add_argument("--tailscale-target", default=None, help="tailscale: <host>.<tailnet>.ts.net")
    enable.add_argument("--github-repository", default=None, help="ci: the owner/repo whose Actions deploy")
    enable.add_argument("--sandbox-repository", default=None, help="ci: the private acceptance sandbox owner/repo")
    enable.add_argument("--runner-label", default=None, help="ci: the self-hosted runners' label")
    enable.add_argument("--tenants", default=None,
                        help="bright-data-egress, typesafe: the tenant ids it routes, comma-separated")
    enable.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    enable.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")
    disable = sub.add_parser("disable", help="turn an add-on off; its secret containers stay")
    disable.add_argument("name", choices=addons.NAMES)
    disable.add_argument("--project", required=True, type=_project, help="the install's Google Cloud project")
    disable.add_argument("--delete-secrets", action="store_true",
                         help="also delete the add-on's secret containers, after typing the project id")
    disable.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")
    status = sub.add_parser("status", help="each add-on's recorded phase")
    status.add_argument("--project", required=True, type=_project, help="the install's Google Cloud project")


# -- seams the tests replace -------------------------------------------------


def open_state(project: str) -> tuple[StateStore, InstallState]:
    bucket = GcsObjectStore(state_bucket(project))
    found = bucket.read(STATE_OBJECT)
    if found is None:
        raise addons.AddonError(f"no install state in gs://{state_bucket(project)}: run swarm install first")
    environment = InstallState.from_bytes(found[0]).environment
    store = StateStore(project=project, environment=environment, local=local_path(project), bucket=bucket)
    return store, store.load()


def build(args: argparse.Namespace, store: StateStore, state: InstallState, events: Events) -> addons.Flow:
    from ..secret_entry import enter_secret
    from ..steps import bootstrap, open_page
    from ..steps.preflight import Gcp, terminal_ask
    from .install import holder_identity

    ctx = SimpleNamespace(project=args.project, state=state, outputs=state.outputs, holder=holder_identity(),
                          environment=state.environment, events=events, bucket=store.bucket, dry_run=False)

    def save(change: Any) -> None:
        ctx.state = store.update(change)
        ctx.outputs = ctx.state.outputs
        flow.state = ctx.state

    asker = terminal_ask()

    def ask(question: str, default: str | None) -> str:
        return asker(ctx, "addon.choice", question, default)

    def hidden(secret_id: str, prompt: str) -> int:
        # With no terminal, enter_secret pauses the run (exit 4); it never reads stdin.
        return enter_secret(ctx, step_id=f"addon.{args.name}", project=args.project, secret_id=secret_id,
                            prompt=prompt, store=gcp)

    gcp = Gcp()
    world = addons.RealWorld(project=args.project, ctx=ctx, gcp=gcp, deps=bootstrap.DEPS,
                             api_context=getattr(args, "context", None))
    flow = addons.Flow(world=world, state=state, save=save, events=events, clock=SystemClock(), out=sys.stdout,
                       ask=ask, confirm=bootstrap.DEPS.confirm, hidden_entry=hidden,
                       open_page=lambda url, why: open_page(url, why=why))
    return flow


def _given(args: argparse.Namespace) -> dict[str, str]:
    return {choice: getattr(args, flag) for flag, choice in CHOICE_FLAGS.items() if getattr(args, flag, None)}


def run(args: argparse.Namespace) -> int:
    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(getattr(args, "json_events", False)))
    try:
        store, state = open_state(args.project)
        if args.action == "status":
            records = addons.recorded(state.outputs)
            for name in addons.NAMES:
                print(f"{name}: {(records.get(name) or {}).get('phase') or addons.OFF}")
            return int(ExitCode.OK)
        flow = build(args, store, state, events)
        if args.action == "enable":
            message = flow.enable(args.name, given=_given(args), owner=args.owner, in_terminal=args.in_terminal)
        else:
            message = flow.disable(args.name, delete_secrets=args.delete_secrets)
    except _Stop as exc:
        print(exc.message, file=sys.stderr)
        return int(ExitCode.PAUSED)
    except WaitGaveUp as exc:
        print(str(exc).replace("swarm install again", f"swarm addon {args.action} {args.name} again"),
              file=sys.stderr)
        return int(ExitCode.PAUSED)
    except addons.AddonRefused as exc:
        print(f"swarm addon {args.action}: {exc}", file=sys.stderr)
        return int(ExitCode.REFUSED)
    except addons.AddonError as exc:
        print(f"swarm addon {args.action}: {exc}", file=sys.stderr)
        return int(ExitCode.FAIL)
    events.emit("run_end", outcome="ok", exit_code=int(ExitCode.OK), message=message, state=MET)
    return int(ExitCode.OK)
