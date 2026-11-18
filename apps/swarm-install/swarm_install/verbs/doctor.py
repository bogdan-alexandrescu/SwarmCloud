"""`swarm doctor`: the client tier, then the platform tier (design §2.1; plan
§1.5 DAYONE-DOCTOR-1).

    swarm doctor                         this machine's tier, then the deployment
    swarm doctor --project P [--env E]   the deployment of P, every manifest probe
    swarm doctor --project P --json      one JSON document

The **client tier** is the bridge's own doctor (`swarm_mcp.cli.cmd_doctor`):
which deployment this machine resolves and which auth tier it is on. It runs
first whenever the bridge is installed beside the installer.

The **platform tier** runs when the caller can read the install state of the
deployment (`--project`, else the gcloud configuration's project): every
manifest entry's probe (`swarm_install.doctor`), `met`, `unmet` or `unknown`
per entry, with the fix in words and the `swarm install --repair <id>` that
repairs it, one line on the hourly doctor, and the managed plugins' check
(`plugins_finding`: the bucket exists, the intake job is deployed and a tenant
worker account may read `platform/`; managed-plugins.md §15) with the bundle
build's (`bundles_finding`: with the built-ins switch on, the builder account
exists and may push to the plugin bundles repository and to no repository it
reads; §1.4.2), which stage 14's `verify.plugins` runs too. It never writes.

Exit codes follow `sc`: 0 every probed entry is met; 3 something is unmet or
could not be checked (never 0 when a probe did not run); 1 the doctor could
not start (no install state readable, no manifest); 2 usage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from .. import doctor
from .. import manifest as manifest_model
from ..exitcodes import MET, UNKNOWN, UNMET, ExitCode
from ..state import STATE_OBJECT, GcsObjectStore, InstallState, local_path, state_bucket

NAME = "doctor"
HELP = "check this machine and the deployment against the manifest (never writes)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=None,
                        help="the deployment's Google Cloud project (default: gcloud's configured project)")
    parser.add_argument("--env", default="prod", help="the environment's name (default: prod)")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="the release's manifest.yaml (default: the one beside this package)")
    parser.add_argument("--json", action="store_true", help="the platform tier as one JSON document")
    parser.add_argument("--platform-only", action="store_true",
                        help="skip the client tier (this machine's auth)")


# -- seams the tests replace ---------------------------------------------------


def client_tier() -> int | None:
    """The bridge's doctor, or None when the bridge is not installed here."""
    try:
        from swarm_mcp import cli as bridge
    except ImportError:
        return None
    try:
        return int(bridge.cmd_doctor(None, argparse.Namespace()))
    except Exception as exc:  # noqa: BLE001 - the platform tier still runs
        print(f"client tier could not run ({type(exc).__name__}: {exc})")
        return int(ExitCode.TROUBLE)


def _gcloud(*words: str) -> str:
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        return ""
    try:
        done = subprocess.run([gcloud, *words], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def default_project() -> str:
    return _gcloud("config", "get-value", "project")


def principal() -> str:
    account = _gcloud("config", "get-value", "account")
    if "@" not in account:
        return doctor.principal()
    kind = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    return f"{kind}:{account}"


def read_state(project: str) -> tuple[InstallState | None, str]:
    """The install state (the bucket's, else the local copy) and where it came from."""
    problem = ""
    try:
        found = GcsObjectStore(state_bucket(project)).read(STATE_OBJECT)
        if found is not None:
            return InstallState.from_bytes(found[0]), f"gs://{state_bucket(project)}/{STATE_OBJECT}"
    except Exception as exc:  # noqa: BLE001 - said, then the local copy is tried
        problem = f" (the bucket could not be read: {type(exc).__name__}: {exc})"
    local = local_path(project)
    if local.is_file():
        return InstallState.from_bytes(local.read_bytes()), str(local)
    return None, f"no install state in gs://{state_bucket(project)}/{STATE_OBJECT} or at {local}{problem}"


def load_manifest(path: Path | None) -> manifest_model.Manifest:
    base = path if path is not None else manifest_model.MANIFEST_PATH
    return manifest_model.load(base, base.parent / "manifest.d")


class _InstallApi:
    """swarm-api's groups probe through the install's client (the deployer's
    ID token for the API backend's IAP audience, `api_client`)."""

    def __init__(self, state: InstallState) -> None:
        self._state = state

    def groups_probe(self) -> list[dict[str, Any]]:
        from .. import api_client

        ctx = SimpleNamespace(project=self._state.project, state=self._state, outputs=self._state.outputs)
        client = api_client.for_install(ctx)
        # No pairs: swarm-api probes every registered group and answers
        # `{mode, results}`, the shape stage 8 reads too (finding N6).
        answer = client.groups_probe(())
        return [{"group": r.group, "state": r.state, "reason": r.reason}
                for r in answer.results]


def make_deps(state: InstallState) -> doctor.Deps:
    rest = doctor.RestGcp()
    return doctor.Deps(gcp=rest, api=_InstallApi(state), kube=doctor.kube_for(rest._bearer))


# -- the managed plugins' infrastructure (managed-plugins.md §15) ---------------

PLUGINS_CHECK = "plugins.infrastructure"
#: terraform/infra/plugins.tf's job and the account prefix modules/tenancy and
#: scripts/register-tenant.sh give every tenant's worker.
PLUGIN_INTAKE_JOB = "swarm-plugin-intake"
TENANT_WORKER_PREFIX = "swarm-agent-worker-"
#: Roles carrying storage.objects.get.
OBJECT_READ_ROLES = frozenset({"roles/storage.objectViewer", "roles/storage.objectUser",
                               "roles/storage.objectAdmin", "roles/storage.admin"})
PLUGINS_FIX = ("re-run `swarm install`: its infra passes apply terraform/infra/plugins.tf "
               "(the plugins bucket, its grants and the swarm-plugin-intake job)")
STORAGE_API = "https://storage.googleapis.com/storage/v1"


def plugins_bucket(project: str) -> str:
    """The bucket terraform/infra names `<project>-swarm-plugins`: the install
    never sets `plugins_bucket_name`, and records no other name."""
    return f"{project}-swarm-plugins"


def platform_reader(policy: Mapping[str, Any], bucket: str) -> str:
    """The first tenant worker account `policy` lets get objects under
    `platform/`, or "". A condition counts only when one of its `||` arms is
    exactly the `platform/` prefix of this bucket."""
    platform = f'resource.name.startsWith("projects/_/buckets/{bucket}/objects/platform/")'
    for binding in policy.get("bindings") or ():
        if binding.get("role") not in OBJECT_READ_ROLES:
            continue
        expression = str((binding.get("condition") or {}).get("expression") or "")
        if expression and platform not in (arm.strip() for arm in expression.split("||")):
            continue
        for member in binding.get("members") or ():
            account = str(member).removeprefix("serviceAccount:")
            if str(member).startswith("serviceAccount:") and account.startswith(TENANT_WORKER_PREFIX):
                return account
    return ""


def _found(gcp: doctor.Gcp, url: str, permission: str, resource: str) -> Mapping[str, Any] | None:
    try:
        return gcp.get(url, permission=permission, resource=resource)
    except doctor.NotFound:
        return None


def plugins_finding(target: doctor.Target, gcp: doctor.Gcp, *, intake_released: bool = True,
                    reader_required: bool = True) -> doctor.Finding:
    """The plugins bucket exists, the intake job is deployed in the region, and
    at least one tenant worker account may read `platform/`. Never writes.

    `swarm doctor` asks for all three. Stage 14 passes `intake_released` False
    while the release carries no swarm-plugin-intake image (the job is planned
    only once it does) and `reader_required` False because installs declare no
    Terraform tenants and scripts/register-tenant.sh does not yet grant a
    workspace's read: either gap is then named in the detail as pending, not a
    failure that keeps the install from READY."""
    bucket = plugins_bucket(target.project)
    gcs = f"//storage.googleapis.com/projects/_/buckets/{bucket}"
    job = f"projects/{target.project}/locations/{target.region}/jobs/{PLUGIN_INTAKE_JOB}"
    try:
        if _found(gcp, f"{STORAGE_API}/b/{bucket}", "storage.buckets.get", gcs) is None:
            return doctor.Finding(PLUGINS_CHECK, UNMET, f"the plugins bucket gs://{bucket} does not exist",
                                  fix=PLUGINS_FIX)
        if not target.region:
            return doctor.Finding(PLUGINS_CHECK, UNKNOWN, "the install state records no region to look for "
                                  f"the {PLUGIN_INTAKE_JOB} job in", fix=PLUGINS_FIX)
        pending: list[str] = []
        if _found(gcp, f"https://run.googleapis.com/v2/{job}", "run.jobs.get",
                  f"//run.googleapis.com/{job}") is not None:
            deployed = f"{PLUGIN_INTAKE_JOB} is deployed"
        elif not intake_released:
            deployed = f"{PLUGIN_INTAKE_JOB} is not deployed"
            pending.append(f"the release carries no {PLUGIN_INTAKE_JOB} image, so its job is not made yet")
        else:
            return doctor.Finding(PLUGINS_CHECK, UNMET, f"the plugin intake job {PLUGIN_INTAKE_JOB} is not deployed "
                                  f"in {target.region} (it is made once the release carries its image)",
                                  fix=PLUGINS_FIX)
        policy = _found(gcp, f"{STORAGE_API}/b/{bucket}/iam?optionsRequestedPolicyVersion=3",
                        "storage.buckets.getIamPolicy", gcs) or {}
    except Exception as exc:  # noqa: BLE001 - a read that cannot run is unknown, never fine
        return doctor.Finding(PLUGINS_CHECK, UNKNOWN, f"{type(exc).__name__}: {exc}", fix=PLUGINS_FIX)
    reader = platform_reader(policy, bucket)
    if not reader and reader_required:
        return doctor.Finding(PLUGINS_CHECK, UNMET, f"no tenant worker account may read platform/ in gs://{bucket}",
                              fix=PLUGINS_FIX)
    if not reader:
        pending.append("no tenant worker account may read platform/ yet")
        return doctor.Finding(PLUGINS_CHECK, MET, f"gs://{bucket} exists, {deployed}; pending: "
                              + "; ".join(pending))
    if pending:
        return doctor.Finding(PLUGINS_CHECK, MET, f"gs://{bucket} exists, {deployed} and {reader} may read "
                              "platform/; pending: " + "; ".join(pending))
    return doctor.Finding(PLUGINS_CHECK, MET, f"gs://{bucket} exists, {deployed}, "
                          f"and {reader} may read platform/")


# -- the bundle build (managed-plugins.md §1.4.2 and §15.2) ------------------------

BUNDLES_CHECK = "plugins.bundle-build"
#: terraform/infra/plugin-bundles.tf's account and repository, and the
#: repositories it reads (the release images, build_mirror.tf's Docker Hub
#: mirror, its own npm mirror): it pushes to the first and to none of these.
PLUGIN_BUNDLER_ACCOUNT = "swarm-plugin-bundler"
BUNDLES_REPOSITORY = "swarm-plugin-bundles"
BUNDLER_READ_ONLY_REPOSITORIES = ("swarm-images", "swarm-mirror-dockerhub", "swarm-mirror-npm")
#: Roles carrying artifactregistry.repositories.uploadArtifacts.
REPOSITORY_WRITE_ROLES = frozenset({"roles/artifactregistry.writer", "roles/artifactregistry.repoAdmin",
                                    "roles/artifactregistry.admin", "roles/artifactregistry.createOnPushWriter",
                                    "roles/artifactregistry.createOnPushRepoAdmin"})
BUNDLES_FIX = ("re-run `swarm install`: its infra passes apply terraform/infra/plugin-bundles.tf (the "
               "swarm-plugin-bundler account, the swarm-plugin-bundles repository and their grants)")
ARTIFACT_REGISTRY_API = "https://artifactregistry.googleapis.com/v1"
#: The install state's record of the `builtins_managed` infra last applied
#: (tfvars.BUILTINS_MANAGED_KEY, written by the infra pass; the doctor loads no
#: apply path, so it names the key rather than importing it).
BUILTINS_MANAGED_KEY = "builtins_managed"


def applied_switch(outputs: Mapping[str, Any]) -> str:
    """The `builtins_managed` infra last applied: "on" only when recorded so;
    no record means no infra pass planned plugin-bundles.tf yet."""
    return "on" if outputs.get(BUILTINS_MANAGED_KEY) == "on" else "off"


def bundler_email(project: str) -> str:
    return f"{PLUGIN_BUNDLER_ACCOUNT}@{project}.iam.gserviceaccount.com"


def writes(policy: Mapping[str, Any], email: str) -> bool:
    """`policy` grants `email` a role that pushes to its repository."""
    member = f"serviceAccount:{email}"
    return any(binding.get("role") in REPOSITORY_WRITE_ROLES and member in (binding.get("members") or ())
               for binding in policy.get("bindings") or ())


def bundles_finding(target: doctor.Target, gcp: doctor.Gcp) -> doctor.Finding:
    """With `builtins_managed` applied on: the builder account exists, may
    push to the bundles repository, and holds no push role on a repository it
    reads. Off, plugin-bundles.tf plans nothing, so there is nothing to find.
    Never writes. Push rights granted on the project rather than a repository
    are the release's IAM review's to show (the deployer may grant none here)."""
    if applied_switch(target.outputs) != "on":
        return doctor.Finding(BUNDLES_CHECK, MET, "builtins_managed is off: no bundle build account or "
                              "repository is planned")
    if not target.region:
        return doctor.Finding(BUNDLES_CHECK, UNKNOWN, "the install state records no region to look for the "
                              f"{BUNDLES_REPOSITORY} repository in", fix=BUNDLES_FIX)
    email = bundler_email(target.project)
    account = f"projects/-/serviceAccounts/{email}"
    repositories = f"projects/{target.project}/locations/{target.region}/repositories"

    def policy(repository: str) -> Mapping[str, Any] | None:
        name = f"{repositories}/{repository}"
        return _found(gcp, f"{ARTIFACT_REGISTRY_API}/{name}:getIamPolicy", "artifactregistry.repositories.getIamPolicy",
                      f"//artifactregistry.googleapis.com/{name}")

    try:
        if _found(gcp, f"https://iam.googleapis.com/v1/{account}", "iam.serviceAccounts.get",
                  f"//iam.googleapis.com/{account}") is None:
            return doctor.Finding(BUNDLES_CHECK, UNMET, f"the bundle builder account {email} does not exist",
                                  fix=BUNDLES_FIX)
        own = policy(BUNDLES_REPOSITORY)
        if own is None:
            return doctor.Finding(BUNDLES_CHECK, UNMET, f"the {BUNDLES_REPOSITORY} repository does not exist "
                                  f"in {target.region}", fix=BUNDLES_FIX)
        if not writes(own, email):
            return doctor.Finding(BUNDLES_CHECK, UNMET, f"{email} may not push to {BUNDLES_REPOSITORY}",
                                  fix=BUNDLES_FIX)
        also = [name for name in BUNDLER_READ_ONLY_REPOSITORIES if writes(policy(name) or {}, email)]
    except Exception as exc:  # noqa: BLE001 - a read that cannot run is unknown, never fine
        return doctor.Finding(BUNDLES_CHECK, UNKNOWN, f"{type(exc).__name__}: {exc}", fix=BUNDLES_FIX)
    if also:
        return doctor.Finding(BUNDLES_CHECK, UNMET, f"{email} may also push to {', '.join(also)}; it pushes to "
                              f"{BUNDLES_REPOSITORY} only (managed-plugins.md section 1.4.2)",
                              fix="remove the push grant on " + ", ".join(also) + "; " + BUNDLES_FIX)
    return doctor.Finding(BUNDLES_CHECK, MET, f"{email} exists and may push to {BUNDLES_REPOSITORY} only")


# -- the verb ------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    client_code = None if args.platform_only or args.json else client_tier()
    if client_code is not None:
        print()
    project = args.project or default_project()
    if not project:
        print("swarm doctor: no --project given and gcloud names no project; the platform tier did not run",
              file=sys.stderr)
        return ExitCode.TROUBLE if client_code is not None else ExitCode.USAGE
    state, source = read_state(project)
    if state is None:
        print(f"swarm doctor: {source}; the platform tier did not run. Start an install with: "
              f"swarm install --project {project}", file=sys.stderr)
        return ExitCode.FAIL
    if state.environment != args.env:
        print(f"swarm doctor: the install state of {project} is for environment {state.environment!r}, "
              f"not {args.env!r}", file=sys.stderr)
        return ExitCode.USAGE
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, manifest_model.ManifestError) as exc:
        print(f"swarm doctor: the manifest could not be loaded ({exc})", file=sys.stderr)
        return ExitCode.FAIL
    target = doctor.Target(
        project=project,
        environment=state.environment,
        region=state.region or "",
        outputs=dict(state.outputs),
        principal=principal(),
    )
    deps = make_deps(state)
    findings = [*doctor.run(manifest, target, deps), plugins_finding(target, deps.gcp),
                bundles_finding(target, deps.gcp)]
    code = doctor.exit_code(findings)
    hourly = doctor.hourly_doctor(target, deps)
    skipped = len(manifest.entries) - (len(findings) - 2)
    if args.json:
        print(json.dumps({
            "project": project,
            "environment": state.environment,
            "source": source,
            "hourly_doctor": hourly,
            "checks": [f.to_json() for f in findings],
            "unprobed": skipped,
            "exit_code": int(code),
        }, sort_keys=True))
        return code
    print(f"platform    {project} ({state.environment}), from {source}")
    doctor.render(findings, skipped)
    print(hourly)
    if client_code not in (None, 0) and code == ExitCode.OK:
        return ExitCode.TROUBLE
    return code
