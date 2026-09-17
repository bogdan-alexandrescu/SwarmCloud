#!/usr/bin/env python3
"""Render the tenant manifests in this directory for one tenant.

    kubernetes/render.py tenant --tenant eng
    kubernetes/render.py policies
    kubernetes/render.py job --tenant eng --profile browser \
        --task task_9f3a --attempt att_7b21 --lease lease_c4 --generation 3

Every manifest here is a template with `__TOKEN__` placeholders rather than a
Helm chart or a kustomization, for one reason: the file on disk has to be
readable as the thing that will exist in the cluster. An operator reading
`namespaces/tenant-namespace.yaml` during an incident should see the
ResourceQuota, not a values file and a template function.

Substitution is done here rather than with `sed` or `envsubst` for two reasons.
The resource numbers come from the frozen catalogue rather than from a flag, and
-- more importantly -- every interpolated value is validated against a pattern
before it reaches the YAML. `substitute()` is a plain `str.replace`, so a tenant
id containing a newline would otherwise be arbitrary YAML added to the
namespace, RBAC, ServiceAccount and NetworkPolicy documents that
`apply.sh --confirm` sends to the cluster. `sed` and `envsubst` cannot do that
check at all.

Defaults come from the frozen contract wherever one exists: resource classes and
runner profiles are read from `swarm_common.profiles`, so a manifest rendered
here cannot disagree with what the scheduler admits or what the worker runs.

Two things are deliberately NOT rendered into a worker Job.

`GCS_PREFIX` is gone: nothing read it. `WorkerConfig.from_env` never looks it up
and the worker derives its own prefix --
`tenants/<tenant>/tasks/<task>/attempts/<attempt>` -- from identifiers it
already has. The value this file used to inject was `tenants/<tenant>`, three
levels short, so an operator grepping for it while chasing artifacts that landed
somewhere unexpected was being pointed at the wrong path by the manifest.

The provider key is gone as a `secretKeyRef` env entry. Every process in the
container runs as uid 10001, so a variable in the worker's environment is
readable at `/proc/1/environ` by the runner child, by anything the agent spawns
and by a `cat` -- the env allowlist in `workspace.child_env` confines nothing it
has already been given. The worker instead reads the key from Secret Manager
itself, through Workload Identity, and passes it only into the environment of
the one child that needs it (`lifecycle._build_child_env`). It also removes a
dependency that was never satisfied: `secretKeyRef` names a KUBERNETES Secret,
and nothing in this repository creates one -- the tenant's key lives in Secret
Manager as `swarm-tenant-<tenant>-<provider>`, so the browser pod would have
failed with CreateContainerConfigError.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# The frozen catalogue is the source of truth for sizing. Import it rather than
# restating the numbers: a manifest that disagrees with RESOURCE_CLASSES would
# produce a pod the scheduler's accounting does not describe.
sys.path.insert(0, str(REPO / "apps" / "common"))
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES  # noqa: E402

NAMESPACE_PREFIX = "swarm-"
_NAME_SAFE = re.compile(r"[^a-z0-9-]+")


class RenderError(SystemExit):
    """A value that must not reach the YAML. Exits non-zero with the reason."""


#: What each interpolated value is allowed to look like. `substitute()` is a
#: plain `str.replace` over YAML text, so an unvalidated value is YAML
#: injection: a tenant id containing a newline and two spaces adds keys to the
#: intentionally-empty `swarm-worker` Role, or rules to the egress policy, in
#: documents `apply.sh --confirm` then sends to the cluster. This is
#: operator-supplied rather than caller-supplied input, which bounds the
#: exposure -- it does not make an unvalidated interpolation into a safe one,
#: and this file renders every tenant-isolation object the platform has.
_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    # RFC 1123 label, which is what a namespace, a service account and a label
    # value all have to be anyway.
    "TENANT_ID": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "NAMESPACE": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "KSA_NAME": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "GSA_EMAIL": re.compile(r"^[A-Za-z0-9._%+-]{1,128}@[A-Za-z0-9.-]{1,128}$"),
    "PROJECT_ID": re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
    "REGION": re.compile(r"^[a-z]+-[a-z]+[0-9]$"),
    "PSS_ENFORCE": re.compile(r"^(privileged|baseline|restricted)$"),
    "POD_CIDR": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$"),
    "SERVICE_CIDR": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$"),
    "QUOTA_PODS": re.compile(r"^[0-9]{1,6}$"),
    "QUOTA_JOBS": re.compile(r"^[0-9]{1,6}$"),
    "QUOTA_CPU": re.compile(r"^[0-9]{1,6}m?$"),
    "QUOTA_MEMORY": re.compile(r"^[0-9]{1,9}(Ki|Mi|Gi|Ti|K|M|G|T)?$"),
    "QUOTA_EPHEMERAL": re.compile(r"^[0-9]{1,9}(Ki|Mi|Gi|Ti|K|M|G|T)?$"),
    # Firestore document ids. Underscores are legal here and are exactly why the
    # label spellings have to be sanitised separately.
    "TASK_ID": re.compile(r"^[A-Za-z0-9._-]{1,128}$"),
    "ATTEMPT_ID": re.compile(r"^[A-Za-z0-9._-]{1,128}$"),
    "LEASE_ID": re.compile(r"^[A-Za-z0-9._-]{1,128}$"),
    "TASK_LABEL": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "JOB_NAME": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "GENERATION": re.compile(r"^[0-9]{1,12}$"),
    "RUNNER_PROFILE": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    # A container image reference: registry/path:tag or registry/path@sha256:...
    "IMAGE": re.compile(r"^[A-Za-z0-9._/-]{1,200}(:[A-Za-z0-9._-]{1,128}|@sha256:[a-f0-9]{64})$"),
    "CPU": re.compile(r"^[0-9]{1,4}m?$"),
    "MEMORY": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "DISK": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "TIMEOUT_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "CHECKPOINT_INTERVAL_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "MAX_IN_WORKER_RETRY_DELAY_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "FIRESTORE_DATABASE": re.compile(r"^[A-Za-z0-9._-]{1,64}$"),
    "ARTIFACT_BUCKET": re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$"),
    # Which subscription account the broker assigned (BUILD_PROMPT_V2 §2.6).
    # A label, not an id: "which account is this agent burning?" is the first
    # question asked when one is exhausted and the others are not, and an
    # opaque id does not answer it.
    "ACCOUNT_LABEL": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
}


def check_values(values: dict[str, str]) -> dict[str, str]:
    """Refuse any value that does not match its pattern, before substitution.

    Every key must have a pattern: a new placeholder added to a template without
    one here fails loudly rather than being interpolated unchecked, which is the
    property that keeps this list from rotting.
    """
    for key, value in sorted(values.items()):
        pattern = _VALUE_PATTERNS.get(key)
        if pattern is None:
            raise RenderError(
                f"render: no validation pattern for placeholder __{key}__; add one to "
                "_VALUE_PATTERNS before interpolating it into a manifest"
            )
        if not pattern.fullmatch(str(value)):
            raise RenderError(
                f"render: {key}={value!r} is not allowed here; it must match "
                f"{pattern.pattern}. An unconstrained value in a template is YAML "
                "injection into the objects that isolate tenants from one another."
            )
    return values


#: Files that make up a tenant's namespace, in apply order. The namespace comes
#: first because everything else lives in it; the network policies come last
#: because a default-deny applied before the service accounts exist would be
#: correct but confusing to watch.
TENANT_FILES = (
    "namespaces/tenant-namespace.yaml",
    "service-accounts/worker-serviceaccount.yaml",
    "rbac/worker-rbac.yaml",
    "network-policies/default-deny.yaml",
    "network-policies/allow-egress.yaml",
)

POLICY_FILES = ("policies/pod-security.yaml",)

JOB_FILES = {
    "browser": "worker-templates/worker-job-browser.yaml",
    "default": "worker-templates/worker-job.yaml",
}

#: v2: root inside a gVisor sandbox. Selected by `--runtime gvisor`, and NOT the
#: default while the Cloud Run Jobs path is still the one in service.
#:
#: There is no per-profile variant here on purpose. The browser profile's own
#: template exists because Chromium needs a large /dev/shm; under gVisor that
#: profile needs measuring before it gets a sandboxed template of its own,
#: rather than a copy made on the assumption it still works.
JOB_FILES_GVISOR = {
    "default": "worker-templates/worker-job-v2.yaml",
}


def sanitize_name(*parts: str, max_length: int = 63) -> str:
    """The same shape the dispatcher's `sanitize_name` produces.

    Reproduced rather than imported because `apps/scheduler` is not a dependency
    of this directory; the two must agree, and the manifest test asserts they do
    for the names that matter.
    """
    joined = "-".join(p for p in parts if p)
    slug = _NAME_SAFE.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"cannot build a resource name from {parts!r}")
    if len(slug) > max_length:
        import hashlib

        digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
        slug = slug[: max_length - 9].rstrip("-") + "-" + digest
    if not slug[0].isalpha():
        slug = "s" + slug[: max_length - 1]
    return slug


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace every `__TOKEN__`, and refuse to emit one that was missed.

    Every value is checked against `_VALUE_PATTERNS` first. This function is a
    plain `str.replace` over YAML text, which means an unchecked value is a way
    to write arbitrary YAML into the namespace, RBAC, ServiceAccount and
    NetworkPolicy documents that `apply.sh --confirm` sends to the cluster.

    A leftover placeholder in applied YAML is not a cosmetic problem either:
    `name: __NAMESPACE__` is a valid-looking string that would create a real
    namespace called `__NAMESPACE__`, so that fails loudly too.
    """
    check_values(values)
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))

    leftover = sorted(set(re.findall(r"__[A-Z0-9_]+__", text)))
    if leftover:
        raise RenderError(f"render: unsubstituted placeholders {leftover}")
    return text


def tenant_values(args: argparse.Namespace) -> dict[str, str]:
    tenant = args.tenant
    namespace = args.namespace or f"{NAMESPACE_PREFIX}{tenant}"
    return {
        "TENANT_ID": tenant,
        "NAMESPACE": namespace,
        # The name the dispatcher's manifest asks for: sanitize_name("swarm", id).
        "KSA_NAME": args.ksa or sanitize_name("swarm", tenant),
        "GSA_EMAIL": args.gsa
        or f"swarm-agent-worker-{tenant}@{args.project}.iam.gserviceaccount.com",
        "PROJECT_ID": args.project,
        "REGION": args.region,
        "PSS_ENFORCE": args.pss_enforce,
        "POD_CIDR": args.pod_cidr,
        "SERVICE_CIDR": args.service_cidr,
        "QUOTA_PODS": str(args.quota_pods),
        "QUOTA_JOBS": str(args.quota_jobs),
        "QUOTA_CPU": str(args.quota_cpu),
        "QUOTA_MEMORY": args.quota_memory,
        "QUOTA_EPHEMERAL": args.quota_ephemeral,
    }


def render_files(files: tuple[str, ...], values: dict[str, str]) -> str:
    documents = [substitute((HERE / name).read_text(), values).rstrip("\n") for name in files]
    return "\n---\n".join(documents) + "\n"


def render_job(args: argparse.Namespace) -> str:
    profile = RUNNER_PROFILES[args.profile]
    rc = RESOURCE_CLASSES[profile.resource_class]
    values = tenant_values(args)
    generation = str(args.generation)
    values.update(
        {
            "RUNNER_PROFILE": profile.name,
            "TASK_ID": args.task,
            "TASK_LABEL": sanitize_name(args.task),
            "ATTEMPT_ID": args.attempt,
            "LEASE_ID": args.lease,
            "GENERATION": generation,
            "JOB_NAME": args.job_name
            or sanitize_name("swarm", args.task.replace("task_", ""), generation),
            "IMAGE": args.image
            or f"{args.region}-docker.pkg.dev/{args.project}/"
            f"{args.registry}/{profile.image}:{args.tag}",
            "CPU": str(int(rc.cpu)),
            "MEMORY": f"{rc.memory_gib}Gi",
            "DISK": f"{rc.disk_gib}Gi",
            "TIMEOUT_SECONDS": str(args.timeout or profile.timeout_seconds),
            "CHECKPOINT_INTERVAL_SECONDS": str(profile.checkpoint_interval_seconds),
            "MAX_IN_WORKER_RETRY_DELAY_SECONDS": str(args.max_in_worker_retry_delay_seconds),
            "FIRESTORE_DATABASE": args.firestore_database,
            "ARTIFACT_BUCKET": args.bucket or f"{args.project}-swarm-artifacts",
            "ACCOUNT_LABEL": getattr(args, "account", "") or "unassigned",
        }
    )
    if getattr(args, "runtime", "default") == "gvisor":
        # Pod Security Admission REFUSES this pod at `restricted`, which is the
        # level every existing namespace runs at: restricted forbids running as
        # root, and root is the whole point of the v2 shape. A sandboxed agent
        # namespace has to be `baseline`, which still forbids privileged
        # containers, host namespaces and hostPath, and gVisor is what stands in
        # for the part `restricted` was doing.
        #
        # Refused here rather than discovered at dispatch: the API server's
        # rejection names a securityContext field, not the namespace label that
        # caused it, and that sends people to edit the pod spec.
        if getattr(args, "pss_enforce", "restricted") == "restricted":
            raise RenderError(
                "a gvisor job runs as root and Pod Security Admission refuses "
                "that at `restricted`; render its namespace with "
                "--pss-enforce baseline, and see BUILD_PROMPT_V2 §2.2 for why "
                "the sandbox is what replaces the part restricted was doing"
            )
        template = JOB_FILES_GVISOR["default"]
    else:
        template = JOB_FILES.get(profile.name, JOB_FILES["default"])
    return substitute((HERE / template).read_text(), values)


def add_tenant_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", required=True, help="tenant id, e.g. eng or u-alice")
    parser.add_argument("--namespace", default="", help="override the derived namespace")
    parser.add_argument("--ksa", default="", help="override the Kubernetes service account name")
    parser.add_argument(
        "--gsa",
        default="",
        help=(
            "the tenant's Google service account email. Defaults to the name "
            "terraform/modules/tenancy creates; scripts/register-tenant.sh uses "
            "swarm-t-<tenant> instead, so pass it explicitly if the tenant was "
            "registered by the script."
        ),
    )
    parser.add_argument("--project", default="saga-agents-staging")
    parser.add_argument("--region", default="us-central1")
    parser.add_argument(
        "--pss-enforce",
        default="restricted",
        choices=("privileged", "baseline", "restricted"),
        help=(
            "Pod Security Admission enforce level. The default is `restricted`, "
            "which is what the GKE path runs at: the dispatcher's manifest "
            "(apps/scheduler/scheduler/dispatch.py POD_SECURITY_CONTEXT and "
            "CONTAINER_SECURITY_CONTEXT) satisfies it, and so does "
            "worker-templates/worker-job.yaml. `baseline` exists as an escape "
            "hatch for an incident, not as a setting to leave in place -- "
            "restricted is the level the browser profile's "
            "`chromium_sandbox=False` depends on being true."
        ),
    )
    parser.add_argument("--pod-cidr", default="10.0.0.0/8")
    parser.add_argument(
        "--service-cidr",
        default="34.118.224.0/20",
        help="GKE Autopilot's default service range; public space, so not covered by RFC1918",
    )
    parser.add_argument("--quota-pods", type=int, default=8)
    parser.add_argument("--quota-jobs", type=int, default=32)
    parser.add_argument("--quota-cpu", type=int, default=64)
    parser.add_argument("--quota-memory", default="128Gi")
    parser.add_argument("--quota-ephemeral", default="320Gi")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    tenant = sub.add_parser("tenant", help="namespace, quota, accounts, RBAC and policies")
    add_tenant_arguments(tenant)

    sub.add_parser("policies", help="the cluster-scoped admission policies")

    job = sub.add_parser("job", help="one worker Job")
    add_tenant_arguments(job)
    job.add_argument("--profile", required=True, choices=sorted(RUNNER_PROFILES))
    job.add_argument("--task", required=True)
    job.add_argument("--attempt", required=True)
    job.add_argument("--lease", required=True)
    job.add_argument("--generation", type=int, required=True)
    job.add_argument("--job-name", default="")
    job.add_argument("--image", default="")
    job.add_argument("--registry", default="swarm-images")
    job.add_argument(
        "--tag",
        default=os.environ.get("WORKER_IMAGE_TAG", "latest"),
        help=(
            "image tag. Defaults to $WORKER_IMAGE_TAG, then `latest`. Deployments "
            "pass the immutable tag `scripts/lib/deploy.sh` built; `latest` is for "
            "a hand-run repro, where the point is to reproduce what is deployed now."
        ),
    )
    job.add_argument("--bucket", default="")
    job.add_argument("--firestore-database", default="swarm")
    job.add_argument("--timeout", type=int, default=0)
    job.add_argument("--max-in-worker-retry-delay-seconds", type=int, default=45)
    job.add_argument(
        "--runtime",
        default="default",
        choices=("default", "gvisor"),
        help=(
            "`gvisor` renders the v2 shape: root inside a GKE Sandbox pod "
            "(BUILD_PROMPT_V2 §2.2). Not the default while Cloud Run Jobs is "
            "still the substrate in service."
        ),
    )
    job.add_argument(
        "--account",
        default="",
        help=(
            "the subscription account label the broker assigned. Recorded on "
            "the pod so `kubectl describe` can answer which account an agent is "
            "burning without a Firestore lookup."
        ),
    )


    args = parser.parse_args(argv)

    if args.command == "tenant":
        sys.stdout.write(render_files(TENANT_FILES, tenant_values(args)))
    elif args.command == "policies":
        sys.stdout.write(render_files(POLICY_FILES, {}))
    elif args.command == "job":
        sys.stdout.write(render_job(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
