"""`swarm team add <group>`: a team, end to end, through the same path as the
console's team creation (docs/design/day-one-plan.md lane DAYONE-TEAM-1, part
of #2688; `swarm_install.team` holds the steps and says why each exists).

    swarm team add eng@example.com --project P [--env prod]
        [--first-admin alice@example.com] [--git-owner alice@example.com]
        [--display-name Engineering] [--json-events]

It reads the install's state (`gs://swarm-tfstate-<P>/swarm-install/state.json`)
for the region, the cluster and the deny-list, calls swarm-api as the person
(a platform admin, signed in with `swarm login`), and ends only when the
team's smoke agent SUCCEEDED. It never asks for a forge token: the team's git
value goes in through the one-time link it prints.
"""

from __future__ import annotations

import argparse
import re
import sys
from types import SimpleNamespace
from typing import Any

from ..events import Events
from ..exitcodes import ExitCode
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, state_bucket
from ..team import KubeCluster, SwarmApi, TeamAdd, TeamError, scheduler_spec_settings
from ..waits import SystemClock, WaitGaveUp

NAME = "team"
HELP = "add a team (a Google group) to the deployment: workspace, namespace, keys, git link and a smoke agent"

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
    add = sub.add_parser("add", help="add a team for a Google group")
    add.add_argument("group", type=_email, help="the team's Google group, e.g. eng@example.com")
    add.add_argument("--project", required=True, type=_project, help="the install's Google Cloud project")
    add.add_argument("--display-name", default=None, help="the team's name in the console")
    add.add_argument("--first-admin", type=_email, default=None, help="the team's first workspace admin")
    add.add_argument("--git-owner", type=_email, default=None,
                     help="who opens the team's one-time GitHub credential link (default: the first admin)")
    add.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    add.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")


# -- seams the tests replace -------------------------------------------------


def read_state(project: str) -> InstallState:
    found = GcsObjectStore(state_bucket(project)).read(STATE_OBJECT)
    if found is None:
        raise TeamError(f"no install state in gs://{state_bucket(project)}: run swarm install first")
    return InstallState.from_bytes(found[0])


def build(args: argparse.Namespace, state: InstallState, events: Events) -> TeamAdd:
    from ..steps.cluster import CLUSTER_GCP, kube_for

    # What kube_for reads off an engine context: the project, the state's
    # region and deny-list, and infra pass 1's cluster name.
    ctx = SimpleNamespace(project=args.project, state=state, outputs=state.outputs)
    region = str(state.region or "")
    if not region:
        raise TeamError("the install state records no region: run swarm install --status")
    return TeamAdd(
        api=SwarmApi(context=args.context),
        cluster=lambda: KubeCluster(kube_for(ctx)),
        spec_settings=lambda: scheduler_spec_settings(CLUSTER_GCP(), args.project, region),
        clock=SystemClock(),
        events=events,
        out=sys.stdout,
    )


def run(args: argparse.Namespace) -> int:
    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(getattr(args, "json_events", False)))
    try:
        state = read_state(args.project)
        flow = build(args, state, events)
        result: dict[str, Any] = flow.run(args.group, display_name=args.display_name,
                                          first_admin=args.first_admin, git_owner=args.git_owner)
    except WaitGaveUp as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.PAUSED)
    except TeamError as exc:
        print(f"swarm team add: {exc}", file=sys.stderr)
        return int(ExitCode.FAIL)
    events.emit("run_end", outcome="ok", exit_code=int(ExitCode.OK), message=f"team {result['team']} ready")
    return int(ExitCode.OK)
