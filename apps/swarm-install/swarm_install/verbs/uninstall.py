"""`swarm uninstall`: remove what the install made, in reverse, after one typed
confirmation, and record what it cannot (docs/design/day-one-plan.md §9.2,
lane DAYONE-UNINSTALL-1, part of #2688; `swarm_install.uninstall` holds the
steps and says why each is where it is).

    swarm uninstall [--project P] [--env prod] [--json-events]

It reads the install's state (`gs://swarm-tfstate-<P>/swarm-install/state.json`,
else the local cache once the bucket is gone), prints the steps, and asks for
the project id typed at the terminal; `SWARM_ASSUME_YES` never answers it. A
second run continues at the next step not yet removed. The "what remains"
record is printed at the end and kept in
`~/.config/swarmcloud/install/<P>.remains.json`.

`GcpTeardown` is the real world: Google's REST APIs with the person's
application-default credentials, impersonating `swarm-tf-deployer` for the
calls `uninstall.who_for` says (steps 1-5), kubectl through the install's own
pinned `Kube`, and Terraform from the release's module.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..events import Events
from ..exitcodes import ExitCode
from ..lease import Lease
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, StateStore, StateTooNew, local_path, state_bucket
from ..terraform import Exec, Terraform, subprocess_exec
from ..uninstall import TOKEN_CREATOR, Uninstall
from ..waits import SystemClock

NAME = "uninstall"
HELP = "remove the SwarmCloud install from its project, in reverse, after one typed confirmation"

SCHEDULER = "https://cloudscheduler.googleapis.com/v1"
RUN = "https://run.googleapis.com/v2"
SECRET_MANAGER = "https://secretmanager.googleapis.com/v1"
CONTAINER = "https://container.googleapis.com/v1"
ENGINE_SECRET_FILTER = "labels.managed-by=swarm-install"
#: Tenant namespaces carry this label (kubernetes/namespaces/tenant-namespace.yaml).
TENANT_LABEL = "swarm-tenant"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    from .install import DEFAULT_ENV, _env, _project

    parser.add_argument("--project", default=None, type=_project,
                        help="the Google Cloud project to uninstall from (asked when not given)")
    parser.add_argument("--env", default=DEFAULT_ENV, type=_env, help=f"the environment (default: {DEFAULT_ENV})")
    parser.add_argument("--json-events", action="store_true", help="write events as one JSON object per line")


# -- the real world ---------------------------------------------------------------


@dataclass
class GcpTeardown:
    """`uninstall.Teardown` on Google Cloud."""

    state: InstallState
    exec_: Exec = subprocess_exec
    _apis: dict[Any, Any] = field(default_factory=dict, repr=False)

    @property
    def project(self) -> str:
        return self.state.project

    @property
    def region(self) -> str:
        if not self.state.region:
            raise LookupError("the install state records no region")
        return str(self.state.region)

    def api(self, who: str | None) -> Any:
        if who not in self._apis:
            from ..adopt import InstallGcp
            from ..steps.bootstrap import _impersonated

            self._apis[who] = InstallGcp() if who is None else _impersonated(who)
        return self._apis[who]

    # 1. Scheduler and Cloud Run jobs ---------------------------------------------

    def scheduler_jobs(self, who: str | None) -> list[str]:
        jobs = self.api(who)._pages(f"{SCHEDULER}/projects/{self.project}/locations/{self.region}/jobs", "jobs")
        return [str(j["name"]) for j in jobs if j.get("state") == "ENABLED"]

    def pause_job(self, name: str, who: str | None) -> None:
        self.api(who)._call("POST", f"{SCHEDULER}/{name}:pause", body={})

    def running_executions(self, who: str | None) -> list[str]:
        api = self.api(who)
        running: list[str] = []
        for job in api._pages(f"{RUN}/projects/{self.project}/locations/{self.region}/jobs", "jobs"):
            for execution in api._pages(f"{RUN}/{job['name']}/executions", "executions"):
                if not execution.get("completionTime"):
                    running.append(str(execution["name"]))
        return running

    # 2. Kubernetes -------------------------------------------------------------------

    def _kube(self, who: str | None) -> Any | None:
        """The install cluster's pinned kubectl, its gcloud calls (credentials and
        the auth plugin's tokens) impersonating `who`; None once the cluster is gone."""
        from ..steps.cluster import cluster_name, kube_for, run_command

        if ("kube", who) in self._apis:
            return self._apis[("kube", who)]
        ctx = SimpleNamespace(project=self.project, state=self.state, outputs=self.state.outputs)
        found = self.api(None)._call(
            "GET", f"{CONTAINER}/projects/{self.project}/locations/{self.region}/clusters/{cluster_name(ctx)}",
            missing_ok=True)

        def run(argv: Any, stdin: str | None = None, env: dict[str, str] | None = None) -> tuple[int, str, str]:
            if who is not None:
                env = {**(env or {}), "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": who}
            return run_command(argv, stdin, env)

        kube = None if found is None else kube_for(ctx, run=run)
        self._apis[("kube", who)] = kube
        return kube

    def tenant_namespaces(self, who: str | None) -> list[str]:
        kube = self._kube(who)
        if kube is None:
            return []
        code, out, err = kube("get", "namespaces", "-l", TENANT_LABEL, "-o", "jsonpath={.items[*].metadata.name}")
        if code != 0:
            raise RuntimeError(f"kubectl get namespaces failed: {err.strip()[:300]}")
        return out.split()

    def delete_namespace(self, name: str, who: str | None) -> None:
        kube = self._kube(who)
        if kube is None:
            return
        code, _, err = kube("delete", "namespace", name, "--ignore-not-found", "--wait=true", "--timeout=9m")
        if code != 0:
            raise RuntimeError(f"kubectl delete namespace {name} failed: {err.strip()[:300]}")

    def delete_policies(self, who: str | None) -> str | None:
        from ..steps.cluster import bundle_dir

        kube = self._kube(who)
        if kube is None:
            return None
        policies = bundle_dir(self.project, str(self.state.release or "")) / "policies"
        if not policies.is_dir():
            return f"not deleted one by one: the release's policies are not unpacked at {policies}"
        code, _, err = kube("delete", "-f", str(policies), "--recursive", "--ignore-not-found")
        if code != 0:
            raise RuntimeError(f"kubectl delete -f {policies} failed: {err.strip()[:300]}")
        return None

    # 3, 7. Terraform --------------------------------------------------------------------

    def terraform(self, root: str) -> Terraform:
        from ..steps.bootstrap import Release

        if not self.state.release:
            raise LookupError("the install state records no release, so its Terraform module cannot be fetched")
        release = Release(read_public=self.api(None).read_public, exec_=self.exec_)
        root_dir: Path = release.module_dir(self.project, str(self.state.release)) / "terraform" / root
        return Terraform(root=root, root_dir=root_dir, state_bucket=state_bucket(self.project),
                         environment=self.state.environment, exec_=self.exec_)

    # 4. images ---------------------------------------------------------------------------

    def registry_images(self, who: str | None) -> list[str]:
        from .. import tfvars
        from ..steps.images import ARTIFACT_REGISTRY

        repo = f"{ARTIFACT_REGISTRY}/projects/{self.project}/locations/{self.region}/repositories/{tfvars.REPOSITORY}"
        api = self.api(who)
        if api._call("GET", repo, missing_ok=True) is None:
            return []
        return [str(p["name"]) for p in api._pages(f"{repo}/packages", "packages")]

    def delete_image(self, name: str, who: str | None) -> None:
        from ..steps.images import ARTIFACT_REGISTRY

        self.api(who)._call("DELETE", f"{ARTIFACT_REGISTRY}/{name}", missing_ok=True)

    # 5. secrets ----------------------------------------------------------------------------

    def engine_secrets(self, who: str | None) -> list[str]:
        found = self.api(who)._pages(f"{SECRET_MANAGER}/projects/{self.project}/secrets", "secrets",
                                     {"filter": ENGINE_SECRET_FILTER})
        return [str(s["name"]) for s in found]

    def delete_secret(self, name: str, who: str | None) -> None:
        self.api(who)._call("DELETE", f"{SECRET_MANAGER}/{name}", missing_ok=True)

    # 6. IAM, as the owner -------------------------------------------------------------------

    def missing_permissions(self, permissions: Any) -> list[str]:
        held = self.api(None).test_permissions(f"projects/{self.project}", list(permissions))
        return [p for p in permissions if p not in held]

    def remove_project_binding(self, role: str, member: str) -> bool:
        api = self.api(None)
        policy = dict(api.iam_policy(self.project))  # carries its etag: a moved policy is refused
        changed = False
        bindings = []
        for binding in policy.get("bindings") or []:
            if binding.get("role") == role and member in (binding.get("members") or []):
                binding = {**binding, "members": [m for m in binding["members"] if m != member]}
                changed = True
            if binding.get("members"):
                bindings.append(binding)
        if changed:
            api.set_iam_policy(self.project, {**policy, "bindings": bindings})
        return changed

    def remove_impersonation_grant(self, deployer: str, member: str) -> bool:
        from ..steps.preflight import IAM

        api = self.api(None)
        resource = f"{IAM}/projects/{self.project}/serviceAccounts/{deployer}"
        policy = api._call("POST", f"{resource}:getIamPolicy", body={}, missing_ok=True)
        if policy is None:
            return False
        changed = False
        bindings = []
        for binding in policy.get("bindings") or []:
            if binding.get("role") == TOKEN_CREATOR and member in (binding.get("members") or []):
                binding = {**binding, "members": [m for m in binding["members"] if m != member]}
                changed = True
            if binding.get("members"):
                bindings.append(binding)
        if changed:
            api._call("POST", f"{resource}:setIamPolicy", body={"policy": {**policy, "bindings": bindings}})
        return changed

    # 7. KMS key versions, as the owner ----------------------------------------------------------

    def destroy_key_versions(self, key: str) -> int:
        from ..adopt import KMS

        api = self.api(None)
        if api._call("GET", f"{KMS}/{key}", missing_ok=True) is None:
            return 0
        destroyed = 0
        for version in api._pages(f"{KMS}/{key}/cryptoKeyVersions", "cryptoKeyVersions"):
            if version.get("state") in ("ENABLED", "DISABLED"):
                api._call("POST", f"{KMS}/{version['name']}:destroy", body={})
                destroyed += 1
        return destroyed

    # 8. the state buckets, as the owner ----------------------------------------------------------

    def delete_bucket(self, name: str) -> bool:
        from google.cloud import storage  # lazy: no client at import time

        client = storage.Client(project=self.project)
        bucket = client.lookup_bucket(name)
        if bucket is None:
            return False
        for blob in client.list_blobs(name, versions=True):
            blob.delete()
        bucket.delete()
        return True


# -- seams the tests replace -----------------------------------------------------------


def open_store(project: str, environment: str) -> StateStore:
    """The bucket's state when it is there, else the local cache (after step 8)."""
    local = local_path(project)
    store = StateStore(project=project, environment=environment, local=local)
    try:
        bucket = GcsObjectStore(state_bucket(project))
        found = bucket.read(STATE_OBJECT)
    except Exception as exc:  # no bucket any more, or no access: the local cache is what is left
        print(f"swarm uninstall: gs://{state_bucket(project)} could not be read ({type(exc).__name__}); "
              f"using {local}", file=sys.stderr)
        found, bucket = None, None
    if found is not None:
        store.bucket = bucket
    elif not local.is_file():
        raise LookupError(f"no install state for {project} in gs://{state_bucket(project)} or {local}")
    return store


def build_world(state: InstallState) -> Any:
    return GcpTeardown(state=state)


def typed(prompt: str) -> str:
    """The project id typed at the terminal; SWARM_ASSUME_YES never answers it."""
    from ..steps.bootstrap import _confirm_typed

    return _confirm_typed(prompt)


def run(args: argparse.Namespace) -> int:
    from .install import _ask, _project, _Usage, holder_identity, interactive

    try:
        if args.project is None:
            if not interactive():
                print("swarm uninstall: --project is required with no terminal", file=sys.stderr)
                return int(ExitCode.USAGE)
            args.project = _ask("The Google Cloud project to uninstall from:", _project)
        store = open_store(args.project, args.env)
        state = store.load()
    except LookupError as exc:
        print(f"swarm uninstall: {exc}", file=sys.stderr)
        return int(ExitCode.FAIL)
    except StateTooNew as exc:
        print(f"swarm uninstall: {exc}", file=sys.stderr)
        return int(ExitCode.REFUSED)
    except _Usage as exc:
        print(f"swarm uninstall: {exc}", file=sys.stderr)
        return int(ExitCode.USAGE)
    clock = SystemClock()
    holder = holder_identity()
    lease = (Lease(holder=holder, store=store.bucket, clock=clock.now) if store.bucket is not None
             else Lease.local(holder, clock.now))
    flow = Uninstall(store=store, world=build_world(state), confirm=typed, clock=clock,
                     events=Events(clock.now, json_lines=bool(args.json_events)), out=sys.stdout,
                     holder=holder, lease=lease)
    return int(flow.run())
