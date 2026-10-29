"""`swarm install`: run the engine over every discovered step, or say where an
install stands (docs/design/day-one-install.md §2.1-§2.5, §6.3).

    swarm install --project P --env prod --version V --domain H
    swarm install --project P --dry-run [--json-events]
    swarm install --project P --status [--json]

**Run.** The steps come from `swarm_install.steps` (discovered); the engine
checks each, skips what is done, applies what is not and records the state
after every step, so the same line resumes an install wherever it stopped.
The state is the bucket's (`gs://swarm-tfstate-<P>/swarm-install/state.json`)
once that exists, else the local file; the lease is the bucket's lock once the
bucket exists, else local (design §2.3).

**Dry run** (`--dry-run`, §2.4) checks every step and prints each unmet step's
plan. It writes nothing: no state, no local cache, no lease. The store it runs
on refuses a write rather than trusting every step to ask first.

**Status** (`--status`): the install's stages, read from the state and never
written. This is not `swarm status`, which stays the teammates' one line per
task (plan review finding 13). It exits 0 only when every discovered step is
recorded done (`exitcodes.for_statuses`: nothing checked is not fine), and
then prints the ready screen (manifest `other.ready-screen`) once the
install has recorded `ready_at`.

`--version`, `--domain` and `--region` seed the state of a new install. A run
that names a different value from the one an install already records is
refused (exit 5): the release changes through `swarm upgrade`, and the
hostname and region do not change in place.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import manifest as manifest_model
from ..engine import Engine
from ..events import Events
from ..exitcodes import MET, UNMET, ExitCode, for_statuses
from ..lease import Lease
from ..record import stages_of
from ..state import (
    STATE_OBJECT,
    GcsObjectStore,
    InstallState,
    ObjectStore,
    StateStore,
    StateTooNew,
    local_path,
    state_bucket,
)
from ..steps import Step, triggered_of
from ..steps import discover as _discover_steps
from ..waits import SystemClock

NAME = "install"
HELP = "install SwarmCloud into a Google Cloud project, or resume one (--status: where it stands)"

#: `--env` when none is given: the install line in design §6.3 names `prod`,
#: and a company's first deployment is its production one.
DEFAULT_ENV = "prod"

#: The marketplace a teammate adds (plan §6.4, D28), printed on the ready screen.
PLUGIN_MARKETPLACE = "https://storage.googleapis.com/swarmcloud/marketplace.json"

#: Google's project id rule: 6-30 characters, lowercase letters, digits and
#: hyphens, starting with a letter and not ending with a hyphen.
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ENV = re.compile(r"^[a-z][a-z0-9-]{0,29}$")

#: The state fields a run's flags seed, with the words a refusal uses.
_SEEDED = {
    "release": ("release", "`swarm upgrade` changes the release"),
    "hostname": ("domain", "an install's domain does not change in place"),
    "region": ("region", "an install's region does not change in place"),
}


class _Usage(Exception):
    """The command line asked for something that cannot run (exit 2)."""


def _project(value: str) -> str:
    if not _PROJECT_ID.match(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a Google Cloud project id")
    return value


def _env(value: str) -> str:
    if not _ENV.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an environment name (lowercase letters, digits and hyphens)"
        )
    return value


def _stage(value: str) -> int:
    try:
        stage = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a stage number") from None
    if not 0 <= stage <= 15:
        raise argparse.ArgumentTypeError(f"stage {stage} is outside 0..15")
    return stage


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", required=True, type=_project,
                        help="the Google Cloud project to install into")
    parser.add_argument("--env", default=DEFAULT_ENV, type=_env,
                        help=f"the environment's name (default: {DEFAULT_ENV})")
    parser.add_argument("--version", dest="release", default=None,
                        help="the SwarmCloud release to install, e.g. 2026.10.1")
    parser.add_argument("--domain", dest="hostname", default=None,
                        help="the hostname the console and API answer on")
    parser.add_argument("--region", default=None, help="the Google Cloud region")
    parser.add_argument("--stage", type=_stage, action="append", default=None,
                        help="run only this stage (repeatable); earlier stages are still checked")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="the release's manifest.yaml (default: the one beside this package)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="check every step and print each unmet step's plan; write nothing")
    mode.add_argument("--status", action="store_true",
                      help="where the install stands, read from its state; write nothing")
    parser.add_argument("--json-events", action="store_true",
                        help="write the engine's events as one JSON object per line")
    parser.add_argument("--json", action="store_true", help="with --status: one JSON document")


# -- seams the tests replace -------------------------------------------------


def discover_steps() -> list[Step]:
    return _discover_steps()


def _load_manifest_file(path: Path | None) -> manifest_model.Manifest:
    base = path if path is not None else manifest_model.MANIFEST_PATH
    if not base.is_file():
        raise _Usage(f"no manifest at {base}: pass --manifest with the release's manifest.yaml")
    return manifest_model.load(base, base.parent / "manifest.d")


load_manifest = _load_manifest_file


def open_bucket(project: str) -> ObjectStore:
    return GcsObjectStore(state_bucket(project))


def holder_identity() -> str:
    """Who holds the lease: the gcloud account (the email another admin types
    to take over an expired lease), else the local user at this host."""
    gcloud = shutil.which("gcloud")
    if gcloud is not None:
        try:
            done = subprocess.run(
                [gcloud, "config", "get-value", "account"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            account = done.stdout.strip()
            if done.returncode == 0 and "@" in account:
                return account
        except (OSError, subprocess.TimeoutExpired):
            pass
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _typed(prompt: str) -> str:
    """A typed confirmation; nothing typed when there is no terminal to type in."""
    if not sys.stdin.isatty():
        return ""
    return input(prompt)


# -- the store a run uses ----------------------------------------------------


@dataclass
class _RunStore(StateStore):
    """`StateStore` with this run's seeded fields, and read-only for a dry run."""

    seed: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False

    def peek(self) -> InstallState:
        """The current state, read without writing even the local cache."""
        if self.bucket is not None:
            found = self.bucket.read(STATE_OBJECT)
            if found is not None:
                return InstallState.from_bytes(found[0])
        return self._read_local() or self._fresh()

    def load(self) -> InstallState:
        state = self.peek() if self.read_only else super().load()
        for name, value in self.seed.items():
            if getattr(state, name) is None:
                setattr(state, name, value)
        self._state = state.model_copy(deep=True)
        return state

    def save(self, state: InstallState) -> None:
        if self.read_only:
            raise RuntimeError("a dry run writes no install state")
        super().save(state)

    def attach_bucket(self, bucket: ObjectStore) -> InstallState:
        if self.read_only:
            self.bucket = bucket
            return self.load()
        return super().attach_bucket(bucket)


def _read_bucket(project: str) -> tuple[ObjectStore | None, InstallState | None, str | None]:
    """The bucket, the state in it (None before stage 1 made it) and, when it
    could not be read, why."""
    try:
        bucket = open_bucket(project)
        found = bucket.read(STATE_OBJECT)
    except Exception as exc:  # no credentials, no access: said, never taken as "no install"
        return None, None, f"{type(exc).__name__}: {exc}"
    if found is None:
        return bucket, None, None
    return bucket, InstallState.from_bytes(found[0]), None


def _bucket_uri(project: str) -> str:
    return f"gs://{state_bucket(project)}/{STATE_OBJECT}"


def _refusal(current: InstallState, args: argparse.Namespace, seed: dict[str, Any]) -> str | None:
    if current.environment != args.env:
        return (
            f"the install state of {args.project} is for environment {current.environment!r}; "
            f"this run asked for {args.env!r}"
        )
    for name, value in seed.items():
        recorded = getattr(current, name)
        if recorded is not None and recorded != value:
            flag, why = _SEEDED[name]
            return (
                f"the install of {args.project} records {flag} {recorded}; this run asked for "
                f"{value}, and {why}"
            )
    return None


# -- the verb -----------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    try:
        if args.json and not args.status:
            raise _Usage("--json goes with --status; a run's machine output is --json-events")
        if args.json_events and args.status:
            raise _Usage("--status prints the state, not events; use --status --json")
        return _status(args) if args.status else _install(args)
    except _Usage as exc:
        print(f"swarm install: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    except StateTooNew as exc:
        print(f"swarm install: {exc}", file=sys.stderr)
        return ExitCode.REFUSED


def _install(args: argparse.Namespace) -> int:
    steps = discover_steps()
    if not steps:
        print(
            "swarm install: this installer has no install steps (swarm_install.steps holds none); "
            "nothing was checked",
            file=sys.stderr,
        )
        return ExitCode.TROUBLE
    manifest = load_manifest(args.manifest)
    local = local_path(args.project)
    bucket, remote, problem = _read_bucket(args.project)
    if problem is not None:
        print(
            f"swarm install: the state bucket {_bucket_uri(args.project)} could not be read "
            f"({problem}); using the local state at {local}",
            file=sys.stderr,
        )
    attached = bucket if remote is not None else None
    seed = {name: getattr(args, name) for name in _SEEDED if getattr(args, name) is not None}
    store = _RunStore(
        project=args.project,
        environment=args.env,
        local=local,
        bucket=attached,
        seed=seed,
        read_only=args.dry_run,
    )
    refused = _refusal(store.peek(), args, seed)
    if refused is not None:
        print(f"swarm install: refused: {refused}", file=sys.stderr)
        return ExitCode.REFUSED

    clock = SystemClock()
    holder = holder_identity()
    lease = (
        Lease(holder=holder, store=attached, clock=clock.now)
        if attached is not None
        else Lease.local(holder, clock.now)
    )
    engine = Engine(
        steps,
        store=store,
        lease=lease,
        events=Events(clock.now, json_lines=args.json_events),
        clock=clock,
        holder=holder,
        manifest=manifest,
        dry_run=args.dry_run,
        confirm=_typed,
    )
    return int(engine.run(stages=args.stage).exit_code)


def _when(at: float | None) -> str:
    if at is None:
        return ""
    return datetime.fromtimestamp(at, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _status(args: argparse.Namespace) -> int:
    stage_of: dict[str, int] = {}
    pending: list[Step] = list(discover_steps())
    while pending:
        step = pending.pop(0)
        stage_of[step.id] = step.stage
        pending.extend(triggered_of(step))

    local = local_path(args.project)
    _bucket, remote, problem = _read_bucket(args.project)
    state: InstallState | None
    if remote is not None:
        state, source = remote, _bucket_uri(args.project)
    elif local.is_file():
        state, source = InstallState.from_bytes(local.read_bytes()), str(local)
    else:
        state, source = None, None
    if problem is not None:
        shown = f"; showing the local copy at {local}" if state is not None else ""
        print(
            f"swarm install --status: the state bucket {_bucket_uri(args.project)} could not be "
            f"read ({problem}){shown}",
            file=sys.stderr,
        )

    wrong_env = state is not None and state.environment != args.env
    if wrong_env:
        print(
            f"swarm install --status: the install state of {args.project} is for environment "
            f"{state.environment!r}, not {args.env!r}",
            file=sys.stderr,
        )
    rows = stages_of(state, stage_of) if state is not None else [None] * 16
    stages = [
        {"stage": n, **(rows[n] or {"state": None, "at": None, "by": None})}
        for n in sorted(set(stage_of.values()))
    ]
    steps = [
        {"id": step_id, "stage": stage, "state": state.steps[step_id].state
         if state is not None and step_id in state.steps else None}
        for step_id, stage in sorted(stage_of.items(), key=lambda item: (item[1], item[0]))
    ]
    code = for_statuses(
        MET if state is not None and not wrong_env and state.done(step_id) else UNMET
        for step_id in stage_of
    )
    ready = (
        code == ExitCode.OK and state is not None and state.ready_at is not None
        and state.hostname is not None
    )

    if args.json:
        print(json.dumps({
            "project": args.project,
            "environment": args.env,
            "source": source,
            "release": state.release if state is not None else None,
            "hostname": state.hostname if state is not None else None,
            "stages": stages,
            "steps": steps,
            "ready": ready,
            "exit_code": int(code),
        }, sort_keys=True))
        return code

    if state is None:
        print(
            f"No install state for {args.project}: none in {_bucket_uri(args.project)} or at "
            f"{local}. Start it with: swarm install --project {args.project} --env {args.env}"
        )
        return code
    release = f", release {state.release}" if state.release else ""
    print(f"SwarmCloud in {args.project} ({state.environment}{release}), from {source}")
    for row in stages:
        if row["state"] is None:
            print(f"  stage {row['stage']:>2}  not started")
        else:
            print(f"  stage {row['stage']:>2}  {row['state']}  {_when(row['at'])}  by {row['by']}")
    if ready:
        url = f"https://{state.hostname}"
        print("Ready.")
        print(f"  Console:    {url}/")
        print(
            f"  Teammates:  claude plugin marketplace add {PLUGIN_MARKETPLACE}"
            f"  (deployment URL: {url})"
        )
        print("  Health:     swarm doctor")
    elif code != ExitCode.OK:
        print(f"Not finished. Resume with: swarm install --project {args.project} --env {args.env}")
    return code
