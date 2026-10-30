"""`swarm upgrade --to V`: move a deployment to release V (docs/design/day-one-plan.md
§9.3; lane DAYONE-UPGRADE-1, part of #2688; `swarm_install.upgrade` holds the
order and says why each step exists).

    swarm upgrade --to V --project P [--env prod] [--allow-downgrade] [--json-events]

The real `UpgradeWorld` is built here from the install's own steps, so an
upgrade applies exactly what the install applies: the release's images through
`images.copy`, the Terraform passes through `apply_pass` (the deployer, the
plan review, the typed destroy approval), the policies through
`cluster.policies`' render and the install cluster's context only, the verify
keys through `team.ensure_verify_keys`, and the checks through stage 14's
quick checks. Dispatch is paused and resumed as the person (a platform admin
signed in with `swarm login`) through `POST /v1/admin/dispatch/{pause,resume}`.

Exit: 0 upgraded (or nothing to do); 1 a step failed (the output names it and
`swarm upgrade --to V` resumes there); 4 the drain passed its bound; 5 refused
(a downgrade without `--allow-downgrade`, unapproved IAM changes, a plan that
cannot be applied safely).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from ..engine import Context, Engine
from ..events import Events
from ..exitcodes import MET, UNKNOWN, ExitCode
from ..lease import Lease, LeaseHeld, LeaseRefused
from ..migrations import FirestoreMigrationDocuments, MigrationFailed
from ..state import GcsObjectStore, StateStore, local_path, state_bucket
from ..upgrade import Upgrade, UpgradeFailed, UpgradeRefused
from ..waits import SystemClock, WaitGaveUp

NAME = "upgrade"
HELP = "upgrade the deployment to release V: plan, approve IAM changes, apply in order, check, roll back on failure"

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_VERSION = re.compile(r"^v?\d+(\.\d+)*(-[0-9A-Za-z.-]+)?$")
_ENV = re.compile(r"^[a-z][a-z0-9-]{0,29}$")

#: Stage 14's quick checks: what `swarm doctor` runs after a release.
CHECKS = ("verify.scheduler-targets", "verify.digests", "verify.reconciler-jobs", "verify.firestore-migrations")
#: The latest install pass of each root: an upgrade applies the widest inputs.
LATEST_PASS = {"bootstrap": "bootstrap-2", "infra": "infra-2"}
#: Task states that hold infrastructure (invariant 1; `swarm_common.states.CONCURRENCY_STATES`).
DEMAND_STATES = ("LEASED", "DISPATCHED", "STARTING", "RUNNING")
#: What a release may ship to name the GitHub App permissions it needs.
APP_PERMISSIONS_FILE = "github-app-permissions.json"
LOGGING = "https://logging.googleapis.com/v2"


def _check(pattern: re.Pattern[str], what: str) -> Any:
    def parse(value: str) -> str:
        if not pattern.match(value):
            raise argparse.ArgumentTypeError(f"{value!r} is not {what}")
        return value

    return parse


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", required=True, type=_check(_VERSION, "a release version"),
                        help="the release to upgrade to, e.g. 2026.11.1")
    parser.add_argument("--project", required=True, type=_check(_PROJECT_ID, "a Google Cloud project id"),
                        help="the install's Google Cloud project")
    parser.add_argument("--env", default="prod", type=_check(_ENV, "an environment name"),
                        help="the install's environment (default prod)")
    parser.add_argument("--allow-downgrade", action="store_true", help="allow a release older than the running one")
    parser.add_argument("--context", default=None, help="the swarm CLI context naming this deployment")
    parser.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")


def exit_code_for(exc: BaseException) -> ExitCode:
    if isinstance(exc, UpgradeRefused):
        return ExitCode.REFUSED
    if isinstance(exc, WaitGaveUp):
        return ExitCode.PAUSED
    if isinstance(exc, (UpgradeFailed, MigrationFailed)):
        return ExitCode.FAIL
    return ExitCode.TROUBLE


class LiveWorld:
    """`UpgradeWorld` on the real project, through the install's own steps."""

    def __init__(self, project: str, environment: str, store: StateStore, engine: Engine, events: Events,
                 clock: SystemClock, holder: str, context: str | None) -> None:
        from ..steps import discover as discover_steps
        from ..steps.bootstrap import DEPS

        self.project = project
        self.environment = environment
        self.store = store
        self.engine = engine
        self.events = events
        self.clock = clock
        self.holder = holder
        self.deps = DEPS
        self.steps = {step.id: step for step in discover_steps()}
        self._documents = FirestoreMigrationDocuments(project)
        self._context = context
        self._api: Any = None

    def ctx(self, release: str | None = None) -> Context:
        state = self.store.load()
        if release is not None:
            state.release = release
        ctx = Context(project=self.project, environment=self.environment, holder=self.holder, state=state,
                      events=self.events, clock=self.clock, dry_run=False, engine=self.engine)
        self.engine.ctx = ctx
        return ctx

    def _release(self) -> Any:
        from ..steps.bootstrap import Release

        if self.deps.release is None:
            self.deps.release = Release(read_public=self.deps.gcp().read_public, exec_=self.deps.exec_)
        return self.deps.release

    # -- release files and Terraform ---------------------------------------------

    def fetch(self, version: str, name: str) -> bytes:
        from ..steps.bootstrap import workdir

        return self._release().fetch(version, name, workdir(self.project, version))

    def _terraform(self, ctx: Context, root: str) -> Any:
        from ..terraform import Terraform

        return Terraform(root=root, root_dir=self.deps.module_root(ctx, root), state_bucket=state_bucket(self.project),
                         environment=self.environment, exec_=self.deps.exec_)

    def plan_root(self, root: str, version: str) -> dict[str, Any]:
        from .. import tfvars
        from ..api_client import deployer_email
        from ..steps.bootstrap import generate

        ctx = self.ctx(version)
        inputs = generate(ctx, LATEST_PASS[root], self.deps.gcp())
        tf = self._terraform(ctx, root)
        tfvars.write_local(tf.root_dir, inputs)
        deployer = deployer_email(self.project)
        tf.init(impersonate=deployer)
        return tf.plan(impersonate=deployer)

    def apply_root(self, root: str, version: str) -> None:
        from ..steps.bootstrap import apply_pass

        apply_pass(self.ctx(version), self.deps, LATEST_PASS[root])

    def patch_log_metric(self, address: str, after: Mapping[str, Any]) -> None:
        name = str(after.get("name") or address.rsplit(".", 1)[-1])
        descriptor = dict((after.get("metric_descriptor") or [{}])[0] or {})
        body = {
            "name": name,
            "filter": after.get("filter"),
            "description": after.get("description") or "",
            "labelExtractors": dict(after.get("label_extractors") or {}),
            "metricDescriptor": {
                "metricKind": descriptor.get("metric_kind") or "DELTA",
                "valueType": descriptor.get("value_type") or "INT64",
                "unit": descriptor.get("unit") or "1",
                "labels": [{"key": label.get("key"), "valueType": label.get("value_type") or "STRING",
                            "description": label.get("description") or ""}
                           for label in descriptor.get("labels") or ()],
            },
        }
        self.deps.gcp()._call("PUT", f"{LOGGING}/projects/{self.project}/metrics/{name}", body=body)

    # -- images and the cluster --------------------------------------------------

    def copy_images(self, version: str, images: Mapping[str, str]) -> None:
        step = self.steps["images.copy"]
        ctx = self.ctx(version)
        step.apply(ctx, step.plan(ctx))

    def policies(self, version: str) -> str:
        return str(self.steps["cluster.policies"].rendered(self.ctx(version)))

    def apply_policies(self, rendered: str) -> None:
        self.steps["cluster.policies"].apply_render(self.ctx(), rendered)

    def delete_policies(self, documents: Sequence[tuple[str, str]]) -> None:
        step = self.steps["cluster.policies"]
        kube = step.kube(self.ctx())
        for kind, name in documents:
            code, _, err = kube("delete", kind.lower(), name, "--ignore-not-found")
            if code != 0:
                raise RuntimeError(f"kubectl delete {kind} {name} failed: {err.strip()[:300]}")

    def tenants(self) -> list[str]:
        return sorted(path.split("/", 1)[1] for path, _, _ in self._documents.list("tenants"))

    def apply_namespace(self, tenant: str) -> None:
        from ..steps.cluster import CLUSTER_GCP, kube_for
        from ..team import KubeCluster, ensure_verify_keys, scheduler_spec_settings

        ctx = self.ctx()
        settings = scheduler_spec_settings(CLUSTER_GCP(), self.project, str(ctx.state.region))
        ensure_verify_keys(KubeCluster(kube_for(ctx)), tenant=tenant, settings=settings)

    # -- the GitHub App ----------------------------------------------------------

    def app_permissions(self) -> dict[str, str] | None:
        import requests

        state = self.store.load()
        slug = (state.console.get("github_app") or {}).get("slug")
        if not slug:
            return None
        found = requests.get(f"https://api.github.com/apps/{slug}", timeout=30,
                             headers={"Accept": "application/vnd.github+json"})
        found.raise_for_status()
        return {str(k): str(v) for k, v in (found.json().get("permissions") or {}).items()}

    def release_app_permissions(self, version: str) -> dict[str, str] | None:
        from ..steps.bootstrap import workdir

        if APP_PERMISSIONS_FILE not in self._release().sums(version, workdir(self.project, version)):
            return None
        found = json.loads(self.fetch(version, APP_PERMISSIONS_FILE))
        return {str(k): str(v) for k, v in dict(found).items()}

    def app_review_url(self) -> str:
        org = str(self.store.load().outputs.get("github_org") or "")
        return f"https://github.com/organizations/{org}/settings/installations"

    # -- dispatch and the checks -------------------------------------------------

    def _swarm_api(self) -> Any:
        if self._api is None:
            from swarm_mcp.client import SwarmClient  # the swarm_mcp wheel install.sh installs beside this one

            self._api = SwarmClient(context=self._context)
        return self._api

    def pause_dispatch(self, reason: str) -> None:
        self._swarm_api().request("POST", "/v1/admin/dispatch/pause", payload={"reason": reason})

    def resume_dispatch(self, reason: str) -> None:
        self._swarm_api().request("POST", "/v1/admin/dispatch/resume", payload={"reason": reason})

    def running(self) -> int:
        from google.cloud.firestore_v1.base_query import FieldFilter

        query = self._documents.client.collection("tasks").where(filter=FieldFilter("state", "in",
                                                                                     list(DEMAND_STATES)))
        return sum(1 for _ in query.stream())

    def checks(self) -> list[tuple[str, str, str]]:
        from ..record import FirestoreDocuments, InstallRecord

        if self.engine.record is None:
            self.engine.record = InstallRecord(self.environment, FirestoreDocuments(self.project))
        ctx = self.ctx()
        results = []
        for step_id in CHECKS:
            try:
                seen = self.steps[step_id].check(ctx)
                results.append((step_id, seen.status, seen.detail))
            except Exception as exc:  # noqa: BLE001 -- a check that cannot run is unknown, never green
                results.append((step_id, UNKNOWN, str(exc)[:300]))
        return results

    def documents(self) -> FirestoreMigrationDocuments:
        return self._documents


def run(args: argparse.Namespace) -> int:
    from ..steps import discover as discover_steps
    from .install import _typed, holder_identity

    clock = SystemClock()
    events = Events(clock.now, json_lines=bool(args.json_events))
    bucket = GcsObjectStore(state_bucket(args.project))
    store = StateStore(project=args.project, environment=args.env, local=local_path(args.project), bucket=bucket)
    if bucket.read("swarm-install/state.json") is None:
        print(f"swarm upgrade: gs://{state_bucket(args.project)} holds no install state: run swarm install first",
              file=sys.stderr)
        return int(ExitCode.FAIL)
    holder = holder_identity()
    lease = Lease(holder=holder, store=bucket, clock=clock.now)
    engine = Engine(discover_steps(), store=store, lease=lease, events=events, clock=clock, holder=holder,
                    confirm=_typed)
    world = LiveWorld(args.project, args.env, store, engine, events, clock, holder, args.context)
    flow = Upgrade(project=args.project, world=world, store=store, events=events, clock=clock, confirm=_typed)
    try:
        lease.acquire(_typed)
    except (LeaseHeld, LeaseRefused) as exc:
        print(f"swarm upgrade: {exc}", file=sys.stderr)
        return int(ExitCode.PAUSED)
    try:
        code = flow.run(args.to, allow_downgrade=args.allow_downgrade)
    except (UpgradeRefused, UpgradeFailed, MigrationFailed, WaitGaveUp) as exc:
        code = exit_code_for(exc)
        print(f"swarm upgrade: {exc}", file=sys.stderr)
        events.emit("run_end", outcome="refused" if code == ExitCode.REFUSED else "failed", exit_code=int(code),
                    message=str(exc)[:500])
        return int(code)
    finally:
        lease.release()
    events.emit("run_end", outcome="ok", exit_code=int(code), message=f"{args.project} runs {args.to}")
    return int(code)


__all__ = ["CHECKS", "HELP", "LiveWorld", "NAME", "MET", "add_arguments", "exit_code_for", "run"]
