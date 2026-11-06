"""`swarm team add <group>`: a team, end to end, through the same path as the
console's team creation (docs/design/day-one-plan.md lane DAYONE-TEAM-1, part
of #2688; `swarm_install.team` holds the steps and says why each exists).
`swarm team repair <team>`: that team's tenant render, applied again by the
same job (DAYONE-TEAM-REPAIR; the repair `swarm doctor` prints for
rbac.dispatcher-role).

    swarm team add eng@example.com --project P [--env prod]
        [--first-admin alice@example.com] [--git-owner alice@example.com]
        [--display-name Engineering] [--json-events]
    swarm team repair eng --project P [--context C] [--json-events]

It reads the install's state (`gs://swarm-tfstate-<P>/swarm-install/state.json`)
for the region, the cluster and the deny-list, calls swarm-api as the person
(a platform admin, signed in with `swarm login`), and ends only when the
team's smoke agent SUCCEEDED. It never asks for a forge token: the team's git
value goes in through the one-time link it opens, and the team is not ready
until Secret Manager holds it. Running the same command again resumes: a
stopped workspace is retried, and every give-up names this command.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from ..events import Events
from ..doctor import team_repair_command
from ..exitcodes import ExitCode
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, StateStore, local_path, state_bucket
from ..team import (
    KubeCluster,
    RepairRefused,
    SwarmApi,
    TeamAdd,
    TeamError,
    TeamRepair,
    git_secret_ready,
    scheduler_spec_settings,
)
from ..waits import SystemClock, WaitGaveUp

NAME = "team"
HELP = ("add a team (a Google group) to the deployment: workspace, namespace, keys, git link and a smoke agent; "
        "or repair one: its tenant render, applied again")

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
                     help="who opens the team's one-time GitHub credential link (default: the first admin, else you)")
    add.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    add.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")
    repair = sub.add_parser("repair", help="apply one team's tenant render again (swarm doctor names the team)")
    repair.add_argument("team", help="the team as swarm doctor names it (e.g. eng), or its group's address")
    repair.add_argument("--project", required=True, type=_project, help="the install's Google Cloud project")
    repair.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    repair.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")


# -- seams the tests replace -------------------------------------------------


def read_state(project: str) -> InstallState:
    found = GcsObjectStore(state_bucket(project)).read(STATE_OBJECT)
    if found is None:
        raise TeamError(f"no install state in gs://{state_bucket(project)}: run swarm install first")
    return InstallState.from_bytes(found[0])


def verb_context(project: str, state: InstallState, events: Events) -> Any:
    """What kube_for and the reopen read off an engine context (finding 16):
    the project, the state's region and deny-list, infra pass 1's cluster name,
    and the bucket and a re-reading save for the infra pass that reopens the
    control plane after the closing pass."""
    from ..steps.infra import VerbContext
    from .install import holder_identity

    store = StateStore(project=project, environment=state.environment, local=local_path(project),
                       bucket=GcsObjectStore(state_bucket(project)))
    return VerbContext(project=project, environment=state.environment, holder=holder_identity(), state=state,
                       events=events, bucket=store.bucket, save=store.update)


def build(args: argparse.Namespace, state: InstallState, events: Events, ctx: Any = None) -> TeamAdd:
    from ..steps.cluster import CLUSTER_GCP, kube_for

    ctx = ctx if ctx is not None else verb_context(args.project, state, events)
    region = str(state.region or "")
    if not region:
        raise TeamError("the install state records no region: run swarm install --status")
    hostname = str(state.hostname or state.outputs.get("hostname") or "")
    if not hostname:
        raise TeamError("the install state records no hostname: run swarm install --status")
    return TeamAdd(
        api=SwarmApi(context=args.context),
        cluster=lambda: KubeCluster(kube_for(ctx)),
        spec_settings=lambda: scheduler_spec_settings(CLUSTER_GCP(), args.project, region),
        git_secret=lambda team: git_secret_ready(CLUSTER_GCP(), args.project, team),
        console=f"https://{hostname}",
        clock=SystemClock(),
        events=events,
        out=sys.stdout,
        resume=resume_command(args),
    )


def build_repair(args: argparse.Namespace, state: InstallState, events: Events, ctx: Any = None) -> TeamRepair:
    from ..steps.cluster import CLUSTER_GCP, kube_for
    from .install import _typed

    ctx = ctx if ctx is not None else verb_context(args.project, state, events)
    region = str(state.region or "")
    if not region:
        raise TeamError("the install state records no region: run swarm install --status")
    return TeamRepair(
        api=SwarmApi(context=args.context),
        cluster=lambda: KubeCluster(kube_for(ctx)),
        spec_settings=lambda: scheduler_spec_settings(CLUSTER_GCP(), args.project, region),
        confirm=_typed,
        clock=SystemClock(),
        events=events,
        out=sys.stdout,
        resume=repair_command(args),
    )


def repair_command(args: argparse.Namespace) -> str:
    """The command swarm doctor prints for the team, and the one a give-up names."""
    command = team_repair_command(args.team, args.project)
    if getattr(args, "context", None):
        command += f" --context {args.context}"
    return command


def resume_command(args: argparse.Namespace) -> str:
    """The command that resumes this add: the same verb, group and project
    (audit 4e F13; `swarm install` resumes none of it)."""
    command = f"swarm team add {args.group} --project {args.project}"
    if getattr(args, "context", None):
        command += f" --context {args.context}"
    return command


def run_repair(args: argparse.Namespace) -> int:
    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(getattr(args, "json_events", False)))
    from ..steps.infra import REACH

    try:
        state = read_state(args.project)
        ctx = verb_context(args.project, state, events)
        flow = build_repair(args, state, events, ctx)
        try:
            result: dict[str, Any] = flow.run(args.team)
        finally:
            # The control plane admits this machine only while kubectl runs (decision 2, finding 16).
            left = REACH.close_after(ctx, "swarm team repair")
            if left:
                print(left, file=sys.stderr)
    except WaitGaveUp as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.PAUSED)
    except RepairRefused as exc:
        print(f"swarm team repair: refused: {exc}", file=sys.stderr)
        return int(ExitCode.REFUSED)
    except TeamError as exc:
        print(f"swarm team repair: {exc}", file=sys.stderr)
        return int(ExitCode.FAIL)
    events.emit("run_end", outcome="ok", exit_code=int(ExitCode.OK),
                message=f"team {result['team']}'s tenant render applied again")
    return int(ExitCode.OK)


def run(args: argparse.Namespace) -> int:
    if getattr(args, "action", "add") == "repair":
        return run_repair(args)
    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(getattr(args, "json_events", False)))
    from ..steps.infra import REACH

    try:
        state = read_state(args.project)
        ctx = verb_context(args.project, state, events)
        flow = build(args, state, events, ctx)
        try:
            result: dict[str, Any] = flow.run(args.group, display_name=args.display_name,
                                              first_admin=args.first_admin, git_owner=args.git_owner)
        finally:
            # The control plane admits this machine only while kubectl runs (decision 2, finding 16).
            left = REACH.close_after(ctx, "swarm team add")
            if left:
                print(left, file=sys.stderr)
    except WaitGaveUp as exc:
        print(str(exc), file=sys.stderr)
        return int(ExitCode.PAUSED)
    except TeamError as exc:
        print(f"swarm team add: {exc}", file=sys.stderr)
        return int(ExitCode.FAIL)
    events.emit("run_end", outcome="ok", exit_code=int(ExitCode.OK), message=f"team {result['team']} ready")
    return int(ExitCode.OK)
