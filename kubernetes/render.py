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

Substitution is done here rather than with `sed` or `envsubst` because two
fields are structural rather than textual -- the provider secret block is
several lines of YAML or nothing at all, and the resource numbers come from the
frozen catalogue rather than from a flag. Getting those wrong with a regex is
easy and silent.

Defaults come from the frozen contract wherever one exists: resource classes and
runner profiles are read from `swarm_common.profiles`, so a manifest rendered
here cannot disagree with what the scheduler admits or what the worker runs.
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


def secret_env_block(profile_name: str, tenant_id: str, indent: int = 12) -> str:
    """The provider-key env entries, projected from the tenant's own secret.

    Returns an empty string for a provider-less profile such as `mock`, which is
    what keeps the smoke path working for a tenant that has registered no key.
    """
    profile = RUNNER_PROFILES[profile_name]
    if not profile.provider or not profile.secrets:
        return ""
    pad = " " * indent
    secret_name = f"swarm-tenant-{tenant_id}-{profile.provider}"
    lines: list[str] = []
    for env_name in profile.secrets:
        lines.extend(
            [
                f"{pad}- name: {env_name}",
                f"{pad}  valueFrom:",
                f"{pad}    secretKeyRef:",
                f"{pad}      name: {secret_name}",
                f"{pad}      key: {env_name}",
            ]
        )
    return "\n".join(lines)


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace every `__TOKEN__`, and refuse to emit one that was missed.

    A leftover placeholder in applied YAML is not a cosmetic problem: `name:
    __NAMESPACE__` is a valid-looking string that would create a real namespace
    called `__NAMESPACE__`, so this fails loudly instead.
    """
    for key, value in values.items():
        token = f"__{key}__"
        if token == "__SECRET_ENV__" or key == "SECRET_ENV":
            continue
        text = text.replace(token, str(value))

    if "__SECRET_ENV__" in text:
        block = values.get("SECRET_ENV", "")
        out: list[str] = []
        for line in text.splitlines():
            if line.strip() == "__SECRET_ENV__":
                if block:
                    out.append(block)
                continue  # drop the placeholder line entirely when unused
            out.append(line)
        text = "\n".join(out) + "\n"

    leftover = sorted(set(re.findall(r"__[A-Z0-9_]+__", text)))
    if leftover:
        raise SystemExit(f"render: unsubstituted placeholders {leftover}")
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
    tenant = values["TENANT_ID"]
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
            "GCS_PREFIX": f"tenants/{tenant}",
            "SECRET_ENV": secret_env_block(profile.name, tenant),
        }
    )
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
        default="baseline",
        choices=("privileged", "baseline", "restricted"),
        help=(
            "Pod Security Admission enforce level. `restricted` is the target; "
            "it rejects pods without a securityContext, which the dispatcher's "
            "current GKE manifest does not set."
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
