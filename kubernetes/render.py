#!/usr/bin/env python3
"""Render the tenant manifests in this directory for one tenant.

    kubernetes/render.py tenant --tenant eng
    kubernetes/render.py policies
    kubernetes/render.py job --tenant eng --profile browser \
        --task task_9f3a --attempt att_7b21 --lease lease_c4 --generation 3
    kubernetes/render.py identity --tenant eng   # namespace, GSA, KSA

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
import dataclasses
import ipaddress
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# The frozen catalogue is the source of truth for sizing. Import it rather than
# restating the numbers: a manifest that disagrees with RESOURCE_CLASSES would
# produce a pod the scheduler's accounting does not describe.
#
# The same argument applies to the two RULES this file used to restate, and one
# of them had already drifted:
#
#   * the slugification character class. The reconciler maps a running Job back
#     to its Firestore task by re-slugging the label and looking the result up
#     (`reconciler/detect.py` `sanitised()`), so a name rendered here that does
#     not slug the way the frozen module slugs is a task the reconciler cannot
#     resolve -- it then either reclaims a live attempt or misses an orphaned
#     one.
#   * the account-label rule. This file carried the generic 63-character RFC1123
#     pattern where `quota_broker.accounts` enforces a 40-character one, so
#     `--account` accepted a label the broker could never have registered --
#     defeating the one job `_VALUE_PATTERNS` has.
#
# `quota_broker.accounts` is pure: it imports nothing but the standard library,
# builds no client, and reads no environment. Both paths are added explicitly
# because this file is run by a bare `python3` (see the Makefile and
# `kubernetes/apply.sh`), not from inside the uv workspace venv.
#
# `scheduler.dispatch` is imported for ONE function, the Job's deadline, and is
# importable here for the same reason: at import it needs only the standard
# library and swarm_common, and it builds no client (google.cloud and kubernetes
# are imported inside the methods that use them). The deadline is a formula over
# the lifecycle's own timeout, and a second copy of it here would be the next
# value stated twice -- see `backend_deadline_seconds`.
sys.path.insert(0, str(REPO / "apps" / "common"))
sys.path.insert(0, str(REPO / "apps" / "quota-broker"))
sys.path.insert(0, str(REPO / "apps" / "scheduler"))
from quota_broker.accounts import _LABEL as _ACCOUNT_LABEL  # noqa: E402
from scheduler.dispatch import backend_deadline_seconds  # noqa: E402
from swarm_common.config import Settings  # noqa: E402
from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE  # noqa: E402
from swarm_common.profiles import RESOURCE_CLASSES, RUNNER_PROFILES  # noqa: E402

#: The frozen contract's default, read from the dataclass rather than restated.
#: A deployment that sets DISPATCH_TIMEOUT_SECONDS passes the same value as
#: `--dispatch-timeout-seconds`.
DEFAULT_DISPATCH_TIMEOUT_SECONDS = {
    f.name: f.default for f in dataclasses.fields(Settings)
}["dispatch_timeout_seconds"]

#: THE TENANT NAMESPACE PREFIX, and it must equal the scheduler's.
#:
#: This read `"swarm-"` and the scheduler reads `"swarm-tenant-{tenant}"`
#: (apps/scheduler/scheduler/dispatch.py:646), so the provisioner created
#: `swarm-eng` while the dispatcher wrote into `swarm-tenant-eng`. Kubernetes
#: authorises before it resolves, so a Job created into a namespace that does
#: not exist comes back as `jobs.batch is forbidden` -- a 403 about permissions,
#: never a 404 about the namespace. Every `browser` task this platform ever
#: accepted (seven, over two days) failed that way, and the message sent three
#: separate investigations at IAM.
#:
#: `swarm-tenant-` is the spelling the rest of the platform already agreed on:
#: swarm_common.models.Tenant.secret_name (the FROZEN contract),
#: terraform/modules/tenancy/variables.tf's namespace_prefix default, and the
#: secret ids in terraform/modules/secret_manager. This file was the outlier.
#:
#: scripts/lib/check-contract-parity.sh is the thing that would have caught a
#: second spelling of a contract value; it did not cover this one.
NAMESPACE_PREFIX = "swarm-tenant-"

#: THE KUBERNETES SERVICE ACCOUNT THE DISPATCHER'S POD SPEC NAMES.
#:
#: This is the SAME CLASS OF DEFECT as NAMESPACE_PREFIX above, one layer down,
#: and it is what the RBAC fix would have hit next. Nothing in `terraform/`
#: creates a Kubernetes object -- there is no kubernetes provider in this
#: repository -- so `service-accounts/worker-serviceaccount.yaml` is the ONLY
#: thing that creates a tenant's KSA. It created `swarm-worker` and
#: `swarm-<tenant>`, while `apps/scheduler/scheduler/dispatch.py` asks for
#: `swarm-agent-worker` (GkeTarget.ksa_name / Settings.worker_ksa_name) and
#: `terraform/modules/tenancy` issues the Workload Identity binding for that
#: same `swarm-agent-worker`. So the one name that mattered was the one nobody
#: created.
#:
#: The failure mode is not a clean error either. A Job whose pod names a
#: missing ServiceAccount is accepted by the API server and then never
#: scheduled -- the Job controller retries with
#: `serviceaccount "swarm-agent-worker" not found` on the Job's events, the pod
#: stays absent, and the task sits RUNNING against a lease until its deadline.
#: That reads as a capacity or scheduling problem, which is the same disguise
#: the namespace bug wore.
#:
#: `swarm-<tenant>` is gone rather than kept as a third alias: it was created to
#: match a dispatcher spelling that no longer exists (kubernetes/README.md §5
#: records both the older mismatch and this one). `swarm-worker` is rendered
#: only where it is bound -- see LEGACY_KSA_NAME.
#:
#: tests/unit/worker/test_kubernetes_manifests.py asserts that the rendered
#: ServiceAccount set contains exactly what `GkeJobDispatcher.ksa_for` asks for,
#: so the two cannot drift apart again without failing CI.
DEFAULT_KSA_NAME = "swarm-agent-worker"

#: THE OLDER WORKER KSA, AND IT IS RENDERED ONLY WHERE IT IS BOUND.
#:
#: This name used to be rendered into every tenant namespace, on the claim that
#: `scripts/register-tenant.sh` had workload-identity-bound it everywhere. It had
#: not. Measured 2026-09-25 on eng -- a terraform tenant, and the one tenant with
#: a namespace -- the only roles/iam.workloadIdentityUser member on
#: swarm-agent-worker-eng@ is `[swarm-tenant-eng/swarm-agent-worker]`, which is
#: what terraform/modules/tenancy issues. So in swarm-tenant-eng, `swarm-worker`
#: carried the GSA annotation and got no Google identity: an account whose
#: annotation says one thing and whose IAM says another, and which nothing names.
#:
#: Only IAM knows whether it is bound, so the render is TOLD, never assumes:
#: `--bound-ksa` names each KSA the tenant GSA binds in this namespace, and
#: `kubernetes/apply.sh` reads them from the GSA's IAM policy (and refuses the
#: flag from its own command line). The older identity -- its ServiceAccount and
#: its RoleBinding, LEGACY_KSA_FILES -- is rendered when this name is among them
#: and not otherwise. A render with no `--bound-ksa` (lint, CI, a hand render)
#: is a render for a namespace where nothing is known to be bound, which is the
#: terraform shape.
#:
#: Where it IS bound: register-tenant.sh binds both names in its KSAS array, and
#: binds them before it calls apply.sh, so a tenant it provisions has the older
#: account from its first apply. Whether new tenants should still get it at all
#: is a separate question from this one; this makes the render agree with IAM.
#:
#: Not rendering it does not delete it: apply.sh runs `kubectl apply`, which
#: never removes an object a manifest stopped declaring. kubernetes/README.md §5
#: has the one-line delete for eng.
LEGACY_KSA_NAME = "swarm-worker"


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
    "LEGACY_KSA_NAME": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
    "GSA_EMAIL": re.compile(r"^[A-Za-z0-9._%+-]{1,128}@[A-Za-z0-9.-]{1,128}$"),
    # The control-plane identities, bound as RBAC subjects in every tenant
    # namespace. Pinned to the exact account_id rather than accepting any
    # address: these two names decide who may create and who may delete a Job
    # in someone's namespace, and a typo that still looked like an email would
    # bind a subject that does not exist and fail open-looking -- the RoleBinding
    # applies cleanly and nothing can use it.
    "SCHEDULER_GSA": re.compile(r"^swarm-scheduler@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"),
    "RECONCILER_GSA": re.compile(r"^swarm-reconciler@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"),
    # THE SAME TWO IDENTITIES, BY NUMERIC uniqueId, AND THIS IS THE SUBJECT GKE
    # ACTUALLY PRESENTS.
    #
    # The comment above says a subject that does not exist "would bind a subject
    # that does not exist and fail open-looking -- the RoleBinding applies
    # cleanly and nothing can use it". That is exactly what happened, and not
    # from a typo. Measured on 2026-09-24 against a live dispatch, after the
    # namespace and the email-subject RoleBindings were both confirmed correct:
    #
    #     jobs.batch is forbidden: User "117405034245659033603" cannot create
    #     resource "jobs" in API group "batch" in the namespace
    #     "swarm-tenant-eng"
    #
    # `117405034245659033603` is `gcloud iam service-accounts describe
    # swarm-scheduler@... --format='value(uniqueId)'`. The scheduler reaches the
    # Kubernetes API with a Google OAuth ACCESS token (dispatch.py's
    # `install_google_bearer_token`), and for that path GKE resolves the caller
    # to the service account's uniqueId -- not, as the RBAC file asserted, to
    # its email. The email binding was applying cleanly and authorising nobody.
    #
    # BOTH FORMS ARE BOUND rather than swapping one for the other, because the
    # email form is what GKE presents on other paths (kubectl with an
    # impersonated GSA, and Workload Identity), and a binding that works for one
    # caller and not another is the shape this defect already had.
    #
    # Either spelling is accepted here. The default when no uniqueId is supplied
    # is the EMAIL, which renders a duplicate subject -- inert, because RBAC
    # subjects are a list and a repeat authorises nothing new. A numeric default
    # would be a fabricated identity in a live RoleBinding, which is strictly
    # worse than a duplicate: it reads as a grant and is not one.
    "SCHEDULER_UID": re.compile(
        r"^([0-9]{15,25}|swarm-scheduler@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com)$"
    ),
    "RECONCILER_UID": re.compile(
        r"^([0-9]{15,25}|swarm-reconciler@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com)$"
    ),
    "PROJECT_ID": re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"),
    "REGION": re.compile(r"^[a-z]+-[a-z]+[0-9]$"),
    "PSS_ENFORCE": re.compile(r"^(privileged|baseline|restricted)$"),
    # The cluster's network. The shape is checked here like every other value;
    # `network_values` additionally parses each one and checks that the four
    # agree with one another, which a pattern cannot.
    "POD_CIDR": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$"),
    "SERVICE_CIDR": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$"),
    "CLUSTER_DNS_IP": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$"),
    "NODE_LOCAL_DNS_IP": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$"),
    "NETWORK_SOURCE": re.compile(
        r"^(offline-render-not-for-apply|operator-supplied"
        r"|gke/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z0-9-]{1,63}/[a-z0-9-]{1,63})$"
    ),
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
    "ACTIVE_DEADLINE_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "CHECKPOINT_INTERVAL_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "MAX_IN_WORKER_RETRY_DELAY_SECONDS": re.compile(r"^[0-9]{1,8}$"),
    "FIRESTORE_DATABASE": re.compile(r"^[A-Za-z0-9._-]{1,64}$"),
    "ARTIFACT_BUCKET": re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$"),
    # Which subscription account the broker assigned (BUILD_PROMPT_V2 §2.6).
    # A label, not an id: "which account is this agent burning?" is the first
    # question asked when one is exhausted and the others are not, and an
    # opaque id does not answer it.
    #
    # The broker's own rule, imported rather than restated. The 40-character cap
    # is not cosmetic: the label becomes part of a Secret Manager name
    # (`swarm-account-<tenant>--<label>`) and of the annotation below, so a label
    # this accepted but the broker would refuse names a secret that cannot exist.
    "ACCOUNT_LABEL": _ACCOUNT_LABEL,
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
    # The control plane's own access, without which the GKE backend cannot
    # dispatch at all -- see the file's header for the failure it fixes.
    "rbac/dispatcher-rbac.yaml",
    "network-policies/default-deny.yaml",
    "network-policies/allow-egress.yaml",
)

#: The older worker identity -- LEGACY_KSA_NAME's ServiceAccount and the
#: RoleBinding that ties it to the empty worker Role -- in one file, so it is
#: included whole or not at all. See LEGACY_KSA_NAME for when.
LEGACY_KSA_FILES = ("service-accounts/legacy-worker-serviceaccount.yaml",)


def bound_ksas(args: argparse.Namespace) -> frozenset[str]:
    """The KSAs the tenant GSA binds in this namespace, as `--bound-ksa` says.

    Validated like every other KSA name here. Nothing from this list is
    interpolated -- it only decides which files are rendered -- but deciding
    which objects reach a cluster is exactly the kind of input that must not be
    free text.
    """
    names = list(getattr(args, "bound_ksa", None) or [])
    pattern = _VALUE_PATTERNS["KSA_NAME"]
    for name in names:
        if not pattern.fullmatch(name):
            raise RenderError(
                f"render: --bound-ksa {name!r} is not a Kubernetes service account name; "
                f"it must match {pattern.pattern}"
            )
    return frozenset(names)


def tenant_files(bound: Iterable[str] = ()) -> tuple[str, ...]:
    """TENANT_FILES, plus the older identity where LEGACY_KSA_NAME is bound.

    Placed after rbac/worker-rbac.yaml, whose empty `swarm-worker` Role the
    older identity's RoleBinding names, so the Role exists before its binding.
    """
    if LEGACY_KSA_NAME not in set(bound):
        return TENANT_FILES
    at = TENANT_FILES.index("rbac/worker-rbac.yaml") + 1
    return TENANT_FILES[:at] + LEGACY_KSA_FILES + TENANT_FILES[at:]


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

    The BODY is reproduced -- it predates this file importing `scheduler.dispatch`
    for the Job deadline, and it raises ValueError where the dispatcher's raises
    DispatchError -- but the character class is no longer a third copy of
    `[^a-z0-9-]+`: it is `swarm_common.identity._TENANT_SAFE`, imported above, so
    the part most likely to be edited in one place and not the others cannot be.
    `test_kubernetes_manifests.py` asserts the whole function still agrees with
    the dispatcher's over a matrix that includes the truncation and
    leading-digit branches, so a change on either side fails in CI.
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


#: THE CLUSTER'S NETWORK: four values this file must be TOLD, never assume.
#:
#: (flag attribute, placeholder, flag) for each. They reach
#: `network-policies/allow-egress.yaml`: the pod and service ranges are carved
#: out of the internet rule, and the two DNS addresses are where a pod's DNS
#: queries actually go. `kubernetes/apply.sh` reads all four from the live
#: cluster (`kubernetes/cluster-network.sh` names the field or object each one
#: comes from) and passes them.
#:
#: They used to be defaults -- `--pod-cidr 10.0.0.0/8`,
#: `--service-cidr 34.118.224.0/20` -- and nothing ever overrode them, so the
#: policy on swarm-autopilot (pods 10.44.0.0/14, services 10.48.0.0/20) was
#: rendered for no cluster in particular. The DNS addresses did not exist as
#: inputs at all, and that is the half that hurt: the policy allowed DNS only to
#: kube-dns pods, which a Cloud DNS + NodeLocal DNSCache cluster never sends a
#: pod's query to, and on 2026-09-24 a worker ran for 390 s with every lookup
#: silently dropped.
NETWORK_INPUTS = (
    ("pod_cidr", "POD_CIDR", "--pod-cidr"),
    ("service_cidr", "SERVICE_CIDR", "--service-cidr"),
    ("cluster_dns_ip", "CLUSTER_DNS_IP", "--cluster-dns-ip"),
    ("node_local_dns_ip", "NODE_LOCAL_DNS_IP", "--node-local-dns-ip"),
)

#: What an OFFLINE render uses when it is given no network at all: `make lint`,
#: CI's checkov scan and `scripts/lib/validate-manifests.sh` render
#: `tenant --tenant lint` with no cluster to read. Documentation addresses
#: (RFC 5737 TEST-NET-1/2/3) rather than plausible ones, so an offline render
#: can never pass for a real cluster's the way 10.0.0.0/8 and 34.118.224.0/20
#: did. The policy is marked with OFFLINE_NETWORK_SOURCE, the renderer says so
#: on stderr, and `kubernetes/apply.sh` refuses to apply a render carrying the
#: mark. Isolation does not rest on these values even if one were applied by
#: hand: the internet rule excepts all of private space unconditionally.
OFFLINE_NETWORK = {
    "POD_CIDR": "192.0.2.0/24",
    "SERVICE_CIDR": "198.51.100.0/24",
    "CLUSTER_DNS_IP": "198.51.100.10",
    "NODE_LOCAL_DNS_IP": "203.0.113.10",
}
OFFLINE_NETWORK_SOURCE = "offline-render-not-for-apply"


def network_values(args: argparse.Namespace) -> dict[str, str]:
    """The cluster's network as placeholders: all four given, or none.

    None given is an offline render (see OFFLINE_NETWORK). Some given is
    refused, naming what is missing: half a network is exactly the RC3 shape --
    some values from the cluster and the rest from wherever a default came from.
    All four given are parsed and checked against one another, because each is
    individually well-formed in the failures that matter: a kube-dns IP from
    another cluster, or two arguments swapped.
    """
    given = {token: str(getattr(args, attr, "") or "") for attr, token, _ in NETWORK_INPUTS}
    source = str(getattr(args, "network_source", "") or "")

    if not any(given.values()):
        if source:
            raise RenderError(
                f"render: --network-source {source!r} names where the network values came "
                "from, but none was passed. Pass --pod-cidr, --service-cidr, "
                "--cluster-dns-ip and --node-local-dns-ip with it."
            )
        return {**OFFLINE_NETWORK, "NETWORK_SOURCE": OFFLINE_NETWORK_SOURCE}

    missing = [flag for _, token, flag in NETWORK_INPUTS if not given[token]]
    if missing:
        raise RenderError(
            f"render: the cluster network is all or nothing; missing {' '.join(missing)}. "
            "kubernetes/apply.sh reads all four from the live cluster -- a partial set "
            "renders a policy that is right about some of the cluster and wrong about "
            "the rest, which is how the 2026-09-24 policy came to allow DNS to nothing."
        )
    if source == OFFLINE_NETWORK_SOURCE:
        raise RenderError(
            f"render: --network-source {OFFLINE_NETWORK_SOURCE} marks a render with NO "
            "network values; this one was given all four. Name where they came from."
        )

    try:
        pods = ipaddress.IPv4Network(given["POD_CIDR"], strict=True)
        services = ipaddress.IPv4Network(given["SERVICE_CIDR"], strict=True)
        cluster_dns = ipaddress.IPv4Address(given["CLUSTER_DNS_IP"])
        node_local = ipaddress.IPv4Address(given["NODE_LOCAL_DNS_IP"])
    except ValueError as exc:
        raise RenderError(f"render: not a usable cluster network value: {exc}") from exc
    # Canonical spelling only. The API server validates `except` entries as
    # CIDRs, and the parity check compares strings with the cluster's own.
    for token, parsed in (("POD_CIDR", pods), ("SERVICE_CIDR", services)):
        if str(parsed) != given[token]:
            raise RenderError(
                f"render: {token}={given[token]!r} is not in canonical form; the cluster "
                f"reports it as {parsed}"
            )
    if pods.overlaps(services):
        raise RenderError(
            f"render: the pod range {pods} and the service range {services} overlap. No "
            "GKE cluster has that shape; two values were swapped or come from different "
            "clusters."
        )
    if cluster_dns not in services:
        raise RenderError(
            f"render: --cluster-dns-ip {cluster_dns} is not inside the service range "
            f"{services}. The kube-dns Service IP is a ClusterIP, so it always is; this "
            "one belongs to another cluster, or the arguments are swapped, and the policy "
            "would allow DNS to an address nothing answers on."
        )
    if node_local == cluster_dns:
        raise RenderError(
            f"render: --node-local-dns-ip and --cluster-dns-ip are both {cluster_dns}. "
            "node-local-dns answers on BOTH its own address and the kube-dns IP; pass "
            "the first entry of its -localip, not the second."
        )
    return {**given, "NETWORK_SOURCE": source or "operator-supplied"}


def tenant_values(args: argparse.Namespace) -> dict[str, str]:
    tenant = args.tenant
    namespace = args.namespace or f"{NAMESPACE_PREFIX}{tenant}"
    # AN OVERRIDE MAY RENAME THE NAMESPACE; IT MAY NOT MOVE IT OUT OF THE
    # PLATFORM'S PREFIX. `kubernetes/apply.sh` forwards every argument it does
    # not recognise straight to this renderer, so `--namespace swarm-eng` is one
    # word away from re-creating the outage in the header of this file: the
    # objects would be applied to a namespace the dispatcher never writes to,
    # and the dispatch would keep failing with a 403 that names permissions.
    # The flag stays -- rendering against a namespace neither provisioning path
    # derived is a real need -- but it stays inside `swarm-tenant-`, which is
    # also what the reconciler's GC filter and this repository's kubectl guard
    # both recognise as ours.
    if not namespace.startswith(NAMESPACE_PREFIX):
        raise RenderError(
            f"render: --namespace {namespace!r} is outside {NAMESPACE_PREFIX!r}. "
            "The scheduler dispatches into "
            f"{NAMESPACE_PREFIX}<tenant> (apps/scheduler/scheduler/dispatch.py, "
            "GkeTarget.namespace_template), so objects applied anywhere else "
            "isolate a namespace nothing ever runs in -- and the resulting "
            "dispatch failure is reported as `jobs.batch is forbidden`, never "
            "as a missing namespace. See docs/gke-dispatch-403.md."
        )
    return {
        "TENANT_ID": tenant,
        "NAMESPACE": namespace,
        # The name the dispatcher's pod spec asks for. See DEFAULT_KSA_NAME:
        # this used to be sanitize_name("swarm", id), a spelling nothing has
        # asked for since the dispatcher's KSA was renamed.
        "KSA_NAME": args.ksa or DEFAULT_KSA_NAME,
        # Supplied always, interpolated only by LEGACY_KSA_FILES, which
        # tenant_files() includes only where this name is bound.
        "LEGACY_KSA_NAME": LEGACY_KSA_NAME,
        "GSA_EMAIL": args.gsa
        or f"swarm-agent-worker-{tenant}@{args.project}.iam.gserviceaccount.com",
        # The control-plane identities, as RBAC subjects. Derived rather than
        # passed: they are fixed per project, and a flag for them would be a
        # way to bind the wrong identity into a tenant's namespace.
        "SCHEDULER_GSA": f"swarm-scheduler@{args.project}.iam.gserviceaccount.com",
        "RECONCILER_GSA": f"swarm-reconciler@{args.project}.iam.gserviceaccount.com",
        # The numeric uniqueId of each, which is the subject GKE presents for a
        # service account reaching the API with an OAuth access token. NOT
        # derivable from the project id -- it is assigned by IAM at creation --
        # so unlike the emails above these have to be passed in. apply.sh looks
        # them up; when nothing is passed they fall back to the email, which
        # renders an inert duplicate subject rather than a fabricated identity.
        # See _VALUE_PATTERNS for the measurement that made this necessary.
        "SCHEDULER_UID": args.scheduler_uid
        or f"swarm-scheduler@{args.project}.iam.gserviceaccount.com",
        "RECONCILER_UID": args.reconciler_uid
        or f"swarm-reconciler@{args.project}.iam.gserviceaccount.com",
        "PROJECT_ID": args.project,
        "REGION": args.region,
        "PSS_ENFORCE": args.pss_enforce,
        **network_values(args),
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
    timeout = args.timeout or profile.timeout_seconds
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
            # The LIFECYCLE's deadline (TASK_TIMEOUT_SECONDS) and the JOB's are
            # two tokens, because they were one and the Job always won: see
            # WORKER_FINALISE_BUDGET_SECONDS in apps/scheduler/scheduler/dispatch.py.
            "TIMEOUT_SECONDS": str(timeout),
            "ACTIVE_DEADLINE_SECONDS": str(
                backend_deadline_seconds(
                    timeout,
                    dispatch_timeout_seconds=getattr(
                        args, "dispatch_timeout_seconds", DEFAULT_DISPATCH_TIMEOUT_SECONDS
                    ),
                )
            ),
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
    parser.add_argument(
        "--namespace",
        default="",
        help=(
            f"override the derived namespace. It must still start with "
            f"{NAMESPACE_PREFIX!r}: that is the prefix the scheduler dispatches "
            "into and the one the reconciler collects by, and a namespace "
            "outside it is isolated but never used."
        ),
    )
    parser.add_argument(
        "--ksa",
        default="",
        help=(
            f"override the Kubernetes service account name. Defaults to "
            f"{DEFAULT_KSA_NAME}, which is what the dispatcher's pod spec names "
            "and what terraform's Workload Identity binding was issued for; "
            "pass this only for a cluster provisioned with another spelling, "
            "and pass the same value to the scheduler as WORKER_KSA_NAME."
        ),
    )
    parser.add_argument(
        "--bound-ksa",
        action="append",
        default=[],
        metavar="KSA",
        help=(
            "a Kubernetes service account in this namespace that the tenant GSA "
            "binds (roles/iam.workloadIdentityUser). Repeat for each. "
            f"{LEGACY_KSA_NAME} is rendered only when it is listed. "
            "kubernetes/apply.sh reads these from the GSA's IAM policy and "
            "refuses them on its own command line; with none, the render is for a "
            "namespace where nothing is known to be bound."
        ),
    )
    # THE NUMERIC IDENTITIES, WHICH CANNOT BE DERIVED.
    #
    # Every other control-plane value here is computed from the project id, and
    # the comment on SCHEDULER_GSA in tenant_values explains why that is
    # deliberate: "a flag for them would be a way to bind the wrong identity
    # into a tenant's namespace". These two are the exception the design did not
    # anticipate, because a uniqueId is assigned by IAM at creation and is not a
    # function of anything this script knows.
    #
    # The risk the original comment names is handled by validation instead:
    # _VALUE_PATTERNS accepts only a 15-25 digit number or the exact matching
    # swarm-scheduler@/swarm-reconciler@ address, so a flag cannot smuggle in an
    # arbitrary subject. Omitting the flag falls back to the email, which is a
    # duplicate of the subject already bound -- inert, and specifically not a
    # fabricated numeric id that would read as a grant and authorise nobody.
    for role in ("scheduler", "reconciler"):
        parser.add_argument(
            f"--{role}-uid",
            default="",
            help=(
                f"the numeric uniqueId of swarm-{role}@<project>. GKE presents a "
                "service account by uniqueId, not by email, when it authenticates "
                "with an OAuth access token -- which is how the scheduler reaches "
                "the API. Resolve it with `gcloud iam service-accounts describe "
                f"swarm-{role}@<project>.iam.gserviceaccount.com "
                "--format='value(uniqueId)'`. kubernetes/apply.sh does this for "
                "you; omit it only for a render that will not be applied."
            ),
        )
    parser.add_argument(
        "--gsa",
        default="",
        help=(
            "the tenant's Google service account email. Defaults to "
            "swarm-agent-worker-<tenant>, which is what "
            "terraform/modules/tenancy creates AND what scripts/register-tenant.sh "
            "creates -- the script's older swarm-t-<tenant> identity is gone, so "
            "the default is right for both provisioning paths. Pass this only to "
            "render against an identity neither of them made."
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
    # NO DEFAULTS, deliberately -- see NETWORK_INPUTS for what the defaults
    # cost. Each help names where kubernetes/apply.sh reads the value; that
    # script is the supported way to render for a cluster, and it refuses these
    # flags from its own command line, in full or abbreviated, so a hand-typed
    # value cannot override the one it read. main() turns abbreviation off for
    # the same reason.
    parser.add_argument(
        "--pod-cidr",
        default="",
        help=(
            "the cluster's pod range: `gcloud container clusters describe` "
            ".clusterIpv4Cidr (= .ipAllocationPolicy.clusterIpv4CidrBlock). "
            "Pass all four network values or none; none renders offline "
            "placeholders marked not-for-apply."
        ),
    )
    parser.add_argument(
        "--service-cidr",
        default="",
        help=(
            "the cluster's service range: `gcloud container clusters describe` "
            ".servicesIpv4Cidr. GKE Autopilot's DEFAULT (34.118.224.0/20) is public "
            "space, so the value matters and is never assumed."
        ),
    )
    parser.add_argument(
        "--cluster-dns-ip",
        default="",
        help=(
            "the kube-dns Service's ClusterIP: `kubectl -n kube-system get service "
            "kube-dns -o jsonpath={.spec.clusterIP}`. Not in `clusters describe`. "
            "node-local-dns also answers on it."
        ),
    )
    parser.add_argument(
        "--node-local-dns-ip",
        default="",
        help=(
            "the NodeLocal DNSCache address a pod's resolver uses: the first "
            "entry of `-localip` on the node-cache container of "
            "`kubectl -n kube-system get daemonset node-local-dns`. Not in "
            "`clusters describe`, which only says whether the cache is on."
        ),
    )
    parser.add_argument(
        "--network-source",
        default="",
        help=(
            "where the four network values came from, recorded on the egress "
            "policy: gke/<project>/<location>/<cluster> from apply.sh. Defaults to "
            "`operator-supplied` when the values are given by hand."
        ),
    )
    parser.add_argument("--quota-pods", type=int, default=8)
    parser.add_argument("--quota-jobs", type=int, default=32)
    parser.add_argument("--quota-cpu", type=int, default=64)
    parser.add_argument("--quota-memory", default="128Gi")
    parser.add_argument("--quota-ephemeral", default="320Gi")


def main(argv: list[str] | None = None) -> int:
    # NO ABBREVIATED FLAGS, on every parser. argparse expands an unambiguous
    # prefix of a long option by default, which made every flag here reachable
    # under a dozen spellings nobody reviewed: kubernetes/apply.sh refused the
    # full names of the network flags, and `--pod-cid 10.200.0.0/14`, forwarded
    # after the values it had read from the cluster, became a second
    # `--pod-cidr` and won. A caller that withholds a flag should only have to
    # withhold its name. (Subparsers do not inherit the setting; each is told.)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    tenant = sub.add_parser(
        "tenant", help="namespace, quota, accounts, RBAC and policies", allow_abbrev=False
    )
    add_tenant_arguments(tenant)

    sub.add_parser("policies", help="the cluster-scoped admission policies", allow_abbrev=False)

    # THE TENANT'S IDENTITY, AS A RENDER WOULD NAME IT: `<namespace> <gsa> <ksa>`
    # on one line. kubernetes/apply.sh reads the GSA's Workload Identity
    # bindings before it renders, and asks here which GSA and namespace those
    # are -- with the same forwarded arguments (--gsa, --namespace, --project)
    # -- rather than deriving the names a second time in shell. A second
    # derivation is a value stated twice, and the one that goes stale reads the
    # bindings of an account the render does not annotate.
    identity = sub.add_parser(
        "identity",
        help="the namespace, GSA and KSA a tenant render names, on one line",
        allow_abbrev=False,
    )
    add_tenant_arguments(identity)

    job = sub.add_parser("job", help="one worker Job", allow_abbrev=False)
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
    job.add_argument(
        "--dispatch-timeout-seconds",
        type=int,
        default=DEFAULT_DISPATCH_TIMEOUT_SECONDS,
        help=(
            "the platform's DISPATCH_TIMEOUT_SECONDS: how late a worker may start "
            "before the reconciler reclaims it. Part of the Job's "
            "activeDeadlineSeconds, which must outlast the worker's own timeout. "
            "Defaults to the frozen contract's value."
        ),
    )
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
        values = tenant_values(args)
        if values["NETWORK_SOURCE"] == OFFLINE_NETWORK_SOURCE:
            # stderr, so the render on stdout stays a clean manifest for the
            # lint and checkov callers that pipe it.
            print(
                f"render: WARNING: no cluster network was given, so the egress policy "
                f"carries documentation addresses and is marked {OFFLINE_NETWORK_SOURCE}. "
                "Fine for lint; wrong for a cluster. kubernetes/apply.sh reads the real "
                "values from the cluster.",
                file=sys.stderr,
            )
        sys.stdout.write(render_files(tenant_files(bound_ksas(args)), values))
    elif args.command == "identity":
        values = check_values(tenant_values(args))
        sys.stdout.write(f"{values['NAMESPACE']} {values['GSA_EMAIL']} {values['KSA_NAME']}\n")
    elif args.command == "policies":
        sys.stdout.write(render_files(POLICY_FILES, {}))
    elif args.command == "job":
        sys.stdout.write(render_job(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
