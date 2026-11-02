#!/usr/bin/env python3
"""Render the tenant manifests in this directory for one tenant.

    kubernetes/render.py tenant --tenant eng
    kubernetes/render.py policies
    kubernetes/render.py job --tenant eng --profile browser \
        --task task_9f3a --attempt att_7b21 --lease lease_c4 --generation 3
    kubernetes/render.py identity --tenant eng   # namespace, GSA, KSA
    kubernetes/render.py verify-read --tenant eng  # swarm-verify, if it reads Jobs here
    kubernetes/render.py swarm-system            # the live relay's ingress

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
from scheduler.dispatch import (  # noqa: E402
    CACHE_STORAGE_CLASS,
    CACHE_VOLUME_GIB,
    backend_deadline_seconds,
    cpu_quantity,
    gke_disk,
    gke_ephemeral_storage_gib,
    scratch_volumes_value,
)
from swarm_common.config import Settings  # noqa: E402
from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE  # noqa: E402
from swarm_common.profiles import CPU_BURST_MAX_RATIO, RESOURCE_CLASSES, RUNNER_PROFILES  # noqa: E402

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
    # The verification gate (rbac/verify-rbac.yaml), by email and by uniqueId,
    # pinned to swarm-verify's account id like the two above: this name decides
    # who may read a tenant's Jobs, and the only identity that should is the
    # gate's (scripts/e2e-test.sh [10], VERIFY-GKE-READ).
    "VERIFY_GSA": re.compile(r"^swarm-verify@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"),
    # Numeric only, unlike the two above: the read is rendered only when this is
    # given (verify_read_files), so there is no email fallback to admit.
    "VERIFY_UID": re.compile(r"^[0-9]{15,25}$"),
    # The workspace deployer, as the subject of its ClusterRoleBinding and as
    # the caller its scope policy matches. Pinned to the account id like the
    # two above, by email or by uniqueId: the same two spellings, for the same
    # reason. Here a wrong name is worse than inert -- the binding would grant
    # nothing, but a policy matching a name the binding does not hold would
    # leave the one it does hold unscoped -- which is why both documents take
    # the same two values.
    "WORKSPACE_DEPLOYER_GSA": re.compile(
        r"^swarm-workspace-deployer@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
    ),
    "WORKSPACE_DEPLOYER_UID": re.compile(
        r"^([0-9]{15,25}|swarm-workspace-deployer@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com)$"
    ),
    # The namespaces of the tenants terraform/infra declares, as the JSON list
    # the deployer's scope policy refuses by name (terraform_namespaces). One
    # namespace name per item, quoted, comma-and-space separated: nothing else
    # can reach the CEL.
    "TERRAFORM_NAMESPACES": re.compile(
        r'^\[("swarm-tenant-[a-z0-9]([-a-z0-9]{0,47}[a-z0-9])?"(, "swarm-tenant-[a-z0-9]([-a-z0-9]{0,47}[a-z0-9])?")*)?\]$'
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
    # swarm-api's Direct VPC egress range, admitted to the live relay in
    # swarm-system; `swarm_system_values` parses it and holds it to one private
    # subnet. The source names where apply.sh read it:
    # run/<project>/<region>/<service>/<subnetwork>.
    "SWARM_API_EGRESS_CIDR": re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$"),
    "SWARM_API_EGRESS_SOURCE": re.compile(
        r"^(offline-render-not-for-apply|operator-supplied"
        r"|run/[a-z][a-z0-9-]{4,28}[a-z0-9]/[a-z]+-[a-z]+[0-9]/[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9-]{0,62})$"
    ),
    "QUOTA_PODS": re.compile(r"^[0-9]{1,6}$"),
    "QUOTA_JOBS": re.compile(r"^[0-9]{1,6}$"),
    "QUOTA_CPU": re.compile(r"^[0-9]{1,6}m?$"),
    "QUOTA_CPU_LIMIT": re.compile(r"^[0-9]{1,6}m?$"),
    "QUOTA_MEMORY": re.compile(r"^[0-9]{1,9}(Ki|Mi|Gi|Ti|K|M|G|T)?$"),
    "QUOTA_EPHEMERAL": re.compile(r"^[0-9]{1,9}(Ki|Mi|Gi|Ti|K|M|G|T)?$"),
    "QUOTA_CACHE_STORAGE": re.compile(r"^[0-9]{1,9}Gi$"),
    "CACHE_STORAGE_CLASS": re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
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
    # Five digits, as CPU_LIMIT: an admin-set request of 10 cores or more
    # renders as "10000m" (docs/design/runner-sizes.md §4.2).
    "CPU": re.compile(r"^[0-9]{1,5}m?$"),
    "CPU_LIMIT": re.compile(r"^[0-9]{1,5}m?$"),
    "MEMORY": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "DISK": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "TMP_DISK": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "CACHE_DISK": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "HOME_DISK": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    "EPHEMERAL": re.compile(r"^[0-9]{1,6}(Ki|Mi|Gi|Ti)?$"),
    # SWARM_SCRATCH_VOLUMES (#2436): `<volume>=<absolute path>:<GiB>`, comma
    # separated, from the dispatcher's `scratch_volumes_value`. No space, quote,
    # colon in a path or newline, so it stays one plain YAML scalar.
    "SCRATCH_VOLUMES": re.compile(
        r"^[a-z0-9-]{1,63}=/[A-Za-z0-9/_.-]{0,255}:[0-9]{1,4}"
        r"(,[a-z0-9-]{1,63}=/[A-Za-z0-9/_.-]{0,255}:[0-9]{1,4}){0,7}$"
    ),
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
    # The live browser's path to its relay (docs/design/live-browser.md §8.3).
    # LAST, so every document before it renders exactly as it did before the
    # live browser existed (test_live_netpol.py holds the digests).
    "network-policies/allow-live-egress.yaml",
    # The proxied browser's path to swarm-egress (docs/browser-egress.md §2.5).
    # After the live path, for the same reason: everything before it renders
    # as it did before browser egress existed.
    "network-policies/allow-egress-proxy.yaml",
    # The internal judge's path to the tailnet forwarder (lane JUDGE-NET,
    # kubernetes/swarm-system/tailscale-egress.yaml). Last, for the same
    # reason: everything before it renders as it did before.
    "network-policies/allow-judge-egress.yaml",
    # Every agent pod's path to the shared graph service (lane GS-INFRA,
    # docs/design/graph-service.md §2.1). Last, for the same reason: everything
    # before it renders as it did before. It opens nothing until swarm-graph is
    # deployed in swarm-system (terraform/infra/graph_service.tf, off by default).
    "network-policies/allow-graph-service.yaml",
    # A TypeSafe-backed pod's path to the TypeSafe forwarder (lane
    # TS-MIGRATE-POD, docs/design/workspace-secrets.md §3.5). Last, for the
    # same reason: everything before it renders as it did before. It opens
    # nothing until the dispatcher labels a pod `swarm-typesafe: inject`
    # (typesafe_forwarder.enabled, off by default).
    "network-policies/allow-typesafe-fwd.yaml",
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


#: The verification gate's Job read (VERIFY-GKE-READ): `get` and `list` on
#: jobs.batch, bound to swarm-verify. Appended AFTER the tenant files, so every
#: document before it renders as it did before it existed.
VERIFY_FILES = ("rbac/verify-rbac.yaml",)

#: The tenants whose own namespaces carry it: eng, which the gate runs as, and
#: smoke, the release acceptance tenant. Owner decision 2026-10-21
#: (VERIFY-GKE-READ-2): no other tenant, no personal (`u-*`) namespace and no
#: group workspace, and the workspace deployer's scope is not widened for it.
VERIFY_READ_TENANTS = ("eng", "smoke")


def verify_read_wanted(tenant: str, namespace: str, workspace_id: str = "") -> bool:
    """Whether a tenant render carries the verify read: in eng's and smoke's
    own namespaces, and nowhere else -- not under `--namespace` naming another
    one, and never with `--workspace-id`.

    kubernetes/apply.sh asks this (`render.py verify-read`) before it looks up
    swarm-verify's uniqueId, so the rule is stated once, here.

    NEVER in a person's namespace (`swarm-tenant-u-*`) or a group workspace's
    (--workspace-id), even when asked: the deployer applies those, and
    policies/workspace-provisioner-scope.yaml admits from it only the Roles,
    RoleBindings and subjects the tenant render held before this file, so a
    verify Role there would fail its next apply. A GKE holder in any namespace
    without the read is "not checked" by the gate.
    """
    return (
        not workspace_id
        and tenant in VERIFY_READ_TENANTS
        and namespace == f"{NAMESPACE_PREFIX}{tenant}"
    )


def verify_read_files(
    tenant: str, namespace: str, verify_uid: str = "", workspace_id: str = ""
) -> tuple[str, ...]:
    """VERIFY_FILES when the render is one verify_read_wanted() admits and the
    operator's apply names swarm-verify's uniqueId; nothing otherwise.

    ON --verify-uid, because the binding is only useful with it: the gate
    reaches the API with an OAuth access token, and GKE presents it by uniqueId,
    so an email-only binding applies cleanly and authorises nobody (the
    2026-09-24 dispatch failure). It also keeps the read out of every render
    that does not ask for it: the workspace deployer's, and register-tenant.sh's
    A9 check, which re-renders the namespace and expects every object it
    renders to exist.
    """
    if not verify_uid or not verify_read_wanted(tenant, namespace, workspace_id):
        return ()
    return VERIFY_FILES


#: The cluster-scoped objects `kubernetes/apply.sh --policies` applies, in
#: apply order. The workspace deployer's scope policy comes BEFORE its RBAC:
#: kubectl applies a render in document order, and the other way round there is
#: a moment in which the deployer holds a ClusterRole that may create any
#: namespace and nothing yet narrows it to `swarm-tenant-u-*`
#: (docs/workspaces.md §2.3).
POLICY_FILES = (
    # The cache's StorageClass (CACHE-PVC): cluster-scoped, so it is applied
    # with these and not per tenant. First: nothing here depends on it, and a
    # worker pod dispatched before it exists waits Pending for a class.
    "storage/cache-storage-class.yaml",
    "policies/pod-security.yaml",
    "policies/workspace-provisioner-scope.yaml",
    "rbac/provisioner-rbac.yaml",
)

#: `swarm-system`'s NetworkPolicies, which `kubernetes/apply.sh --swarm-system`
#: applies: a default deny for ingress, and TCP 8080 to the live relay from
#: labelled tenant pods and from swarm-api's Direct VPC egress range
#: (docs/design/live-browser.md §8.3). The namespace and the relay are lane
#: L8's (`kubernetes/swarm-system/`); these two only decide who reaches them.
SWARM_SYSTEM_FILES = ("network-policies/swarm-system-live.yaml",)

#: What an offline `swarm-system` render admits when it is told no range:
#: RFC 5737 TEST-NET-3, never a plausible subnet, and marked with
#: OFFLINE_NETWORK_SOURCE so `kubernetes/apply.sh` refuses to apply it.
OFFLINE_SWARM_API_EGRESS_CIDR = "203.0.113.128/25"

#: The keyless identity the `swarm-workspace-apply` Cloud Build job runs as
#: (docs/workspaces.md §2.3). Its account id is fixed by the bootstrap layer.
WORKSPACE_DEPLOYER_ACCOUNT = "swarm-workspace-deployer"

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


#: The only space swarm-api's Direct VPC egress range may come from: a GCP
#: subnet's primary range is RFC 1918 here (terraform's `subnet_cidr`,
#: 10.40.0.0/20 in dev, 10.60.0.0/20 in prod). Anything wider or public would
#: admit the internet's addresses to the relay the moment any path carried
#: them, which is not a value to accept on the strength of a flag.
_PRIVATE_SPACE = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def swarm_system_values(args: argparse.Namespace) -> dict[str, str]:
    """swarm-api's egress range for `swarm-system-live.yaml`: given, or offline.

    Given, it must be one canonical private subnet and arrive with the source
    apply.sh read it from. None given renders OFFLINE_SWARM_API_EGRESS_CIDR,
    marked not-for-apply -- the same contract as the tenant network.
    """
    cidr = str(getattr(args, "swarm_api_egress_cidr", "") or "")
    source = str(getattr(args, "swarm_api_egress_source", "") or "")
    if not cidr:
        if source:
            raise RenderError(
                f"render: --swarm-api-egress-source {source!r} names where the range came "
                "from, but no --swarm-api-egress-cidr was passed with it."
            )
        return check_values(
            {
                "SWARM_API_EGRESS_CIDR": OFFLINE_SWARM_API_EGRESS_CIDR,
                "SWARM_API_EGRESS_SOURCE": OFFLINE_NETWORK_SOURCE,
            }
        )
    if source == OFFLINE_NETWORK_SOURCE:
        raise RenderError(
            f"render: --swarm-api-egress-source {OFFLINE_NETWORK_SOURCE} marks a render with NO "
            "range; this one was given one. Name where it came from."
        )
    # Shape first: check_values refuses a newline or anything else that is not
    # a CIDR before it is parsed or echoed into an error.
    check_values({"SWARM_API_EGRESS_CIDR": cidr})
    try:
        network = ipaddress.IPv4Network(cidr, strict=True)
    except ValueError as exc:
        raise RenderError(
            f"render: --swarm-api-egress-cidr {cidr!r} is not one canonical IPv4 subnet: {exc}"
        ) from exc
    if not any(network.subnet_of(space) for space in _PRIVATE_SPACE):
        raise RenderError(
            f"render: --swarm-api-egress-cidr {network} is not inside RFC 1918 space. It is "
            "admitted to the live relay on 8080; swarm-api's Direct VPC egress subnet is "
            "private, and a public or wider range is not that subnet."
        )
    return check_values(
        {"SWARM_API_EGRESS_CIDR": str(network), "SWARM_API_EGRESS_SOURCE": source or "operator-supplied"}
    )


def tenant_values(args: argparse.Namespace) -> dict[str, str]:
    tenant = args.tenant
    project = project_of(args)
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
        or f"swarm-agent-worker-{tenant}@{project}.iam.gserviceaccount.com",
        # The control-plane identities, as RBAC subjects. Derived rather than
        # passed: they are fixed per project, and a flag for them would be a
        # way to bind the wrong identity into a tenant's namespace.
        "SCHEDULER_GSA": f"swarm-scheduler@{project}.iam.gserviceaccount.com",
        "RECONCILER_GSA": f"swarm-reconciler@{project}.iam.gserviceaccount.com",
        # The numeric uniqueId of each, which is the subject GKE presents for a
        # service account reaching the API with an OAuth access token. NOT
        # derivable from the project id -- it is assigned by IAM at creation --
        # so unlike the emails above these have to be passed in. apply.sh looks
        # them up. A render given the cluster network is refused without them
        # (require_cluster_inputs); offline, each falls back to its email, an
        # inert duplicate subject rather than a fabricated identity.
        # See _VALUE_PATTERNS for the measurement that made this necessary.
        "SCHEDULER_UID": args.scheduler_uid
        or f"swarm-scheduler@{project}.iam.gserviceaccount.com",
        "RECONCILER_UID": args.reconciler_uid
        or f"swarm-reconciler@{project}.iam.gserviceaccount.com",
        # The verification gate, the subject of rbac/verify-rbac.yaml: derived
        # like the two emails above, and its uniqueId passed in like theirs --
        # but with no fallback, because without it the read is not rendered.
        "VERIFY_GSA": f"swarm-verify@{project}.iam.gserviceaccount.com",
        **({"VERIFY_UID": args.verify_uid} if args.verify_uid else {}),
        "PROJECT_ID": project,
        "REGION": args.region,
        "PSS_ENFORCE": args.pss_enforce,
        **network_values(args),
        "QUOTA_PODS": str(offline_quota(args, "quota_pods")),
        "QUOTA_JOBS": str(offline_quota(args, "quota_jobs")),
        "QUOTA_CPU": str(offline_quota(args, "quota_cpu")),
        # CPU bursts to at most CPU_BURST_MAX_RATIO x its request on every
        # container the dispatcher renders (contract request RS-1), so the
        # limits line is that multiple of the requests line: equal, it refused
        # pods at half the ceiling. Derived, not a flag, like the cache's.
        "QUOTA_CPU_LIMIT": str(offline_quota(args, "quota_cpu") * CPU_BURST_MAX_RATIO),
        "QUOTA_MEMORY": str(offline_quota(args, "quota_memory")),
        "QUOTA_EPHEMERAL": str(offline_quota(args, "quota_ephemeral")),
        # The cache's network disks (CACHE-PVC): one claim per Job, because the
        # claim is the pod's and a finished Job keeps its pod for
        # ttlSecondsAfterFinished (ten minutes). Derived, not a flag: it is a consequence of
        # the jobs quota and the dispatcher's CACHE_VOLUME_GIB, never a third
        # number to keep in step with them.
        "QUOTA_CACHE_STORAGE": f"{offline_quota(args, 'quota_jobs') * CACHE_VOLUME_GIB}Gi",
        "CACHE_STORAGE_CLASS": CACHE_STORAGE_CLASS,
    }


#: A TERRAFORM TENANT'S ResourceQuota (eng, smoke, ...), FLAT, which has no
#: workspace record. Owner decision 2026-10-07 (contract request 53, moving
#: claude-code to GKE), flat over per-tenant: 100 pods matches the GKE_AUTOPILOT
#: backend ceiling of 100, and 400 vCPU / 800Gi / 1000Gi let that many pods
#: fit. The platform's admission ceilings (the Firestore slot pools, invariants
#: 1-3) remain the real limit; this quota is the second line of defence for
#: when that accounting is wrong. It was pods=8, cpu=64, memory=128Gi,
#: ephemeral-storage=320Gi, which capped a tenant at 8 GKE pods however much
#: the pools admitted. jobs (count/jobs.batch) was not part of that decision:
#: owner 2026-10-07 -- 100 running pods plus the finished Jobs awaiting
#: ttlSecondsAfterFinished; at 32 the quota refused new Jobs while pods were
#: still free. The TTL was 3600 s when this was set and is 600 s (ten minutes)
#: since BGD's decision of 2026-10-28, after finished Jobs filled eng's quota
#: of 240 and refused dispatches (#1820); the value is unchanged, so the same
#: quota now covers six times the Jobs an hour.
#:
#: NOT A DEFAULT (DAYONE-K8S-1, #2039). These numbers were the --quota-*
#: defaults, and kubernetes/apply.sh run by hand rendered them for every
#: tenant, which on 2026-10-23 failed u-bogdan's workspace (lesson 32). A
#: WORKSPACE tenant's quota is its record's (`workspace_quota`). apply.sh asks
#: `render.py terraform-quota` for these by name, for a Terraform tenant only,
#: and passes them as flags; an offline render (no cluster network: lint)
#: renders them too.
TERRAFORM_TENANT_QUOTA = {
    "quota_pods": 100,
    "quota_cpu": 400,
    "quota_memory": "800Gi",
    "quota_jobs": 200,
    "quota_ephemeral": "1000Gi",
}

#: THE PROJECT AN OFFLINE RENDER NAMES (DAYONE-K8S-1, design §7). `--project`
#: has no default: it was `saga-agents-staging`, so a render for any other
#: project bound that project's scheduler, reconciler and deployer -- and
#: kubernetes/apply.sh's tenant mode never passed it. A render that can reach a
#: cluster is refused without it (`require_cluster_inputs`); `render.py
#: policies`, `job` and an offline tenant render keep working without it for
#: the lint callers (`make lint`, security.yml, the worker's pre-publish
#: checkov), and name THIS. It fits PROJECT_ID's pattern so the templates
#: render, and it is no project at all: Google Cloud refuses a project id
#: containing the restricted string `google`, so nobody can own it, and an
#: identity rendered from it authorises nothing anywhere.
OFFLINE_PROJECT = "offline-render-google"


def offline_quota(args: argparse.Namespace, key: str) -> int | str:
    """The quota value given, or TERRAFORM_TENANT_QUOTA's for an offline render.

    `require_cluster_inputs` has already refused a cluster-bound render that
    lacks one, so the fallback is only ever reached offline.
    """
    value = getattr(args, key, None)
    return TERRAFORM_TENANT_QUOTA[key] if value is None else value


def project_of(args: argparse.Namespace) -> str:
    """The project given, or OFFLINE_PROJECT (see there) when none was."""
    return str(getattr(args, "project", "") or "") or OFFLINE_PROJECT


def require_cluster_inputs(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse, exit 2, a tenant render that can reach a cluster without the
    inputs only its caller knows: the project, both control-plane uniqueIds and
    all five quota numbers.

    A render "can reach a cluster" when it is given the cluster network, which
    kubernetes/apply.sh reads from the cluster it applies to; without one it is
    an offline render, marked OFFLINE_NETWORK_SOURCE, which apply.sh refuses to
    apply. Each refused input used to default: the project to the live one
    (every subject bound in another project's identities), each uniqueId to its
    email (the RoleBinding GKE never matches, lesson 29: eight months of
    `jobs.batch is forbidden`), the quota to flat numbers (#2039, lesson 32).
    """
    if network_values(args)["NETWORK_SOURCE"] == OFFLINE_NETWORK_SOURCE:
        return
    missing = []
    if not getattr(args, "project", ""):
        missing.append("--project")
    for role in ("scheduler", "reconciler"):
        if not re.fullmatch(r"[0-9]{15,25}", str(getattr(args, f"{role}_uid", "") or "")):
            missing.append(f"--{role}-uid (the numeric uniqueId)")
    missing += [f"--{key.replace('_', '-')}" for key in TERRAFORM_TENANT_QUOTA if getattr(args, key, None) is None]
    if missing:
        parser.error(
            "a render given the cluster network is for that cluster, and needs "
            f"{', '.join(missing)} from its caller; none has a default. kubernetes/apply.sh "
            "passes all of them: the project it applies to, the uniqueIds it resolves, and "
            "the tenant record's quota."
        )


def add_policy_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags `render.py policies` takes: the identities its RBAC names.

    No flag names an identity directly. The emails are derived from the
    project; the uniqueIds cannot be derived (IAM assigns them), so
    kubernetes/apply.sh looks them up and passes them, and without them each
    falls back to its email -- an inert duplicate, never a fabricated subject.
    """
    parser.add_argument(
        "--project",
        default="",
        help=f"the project whose identities the RBAC names; omitted, {OFFLINE_PROJECT} (no project)",
    )
    parser.add_argument("--scheduler-uid", default="")
    parser.add_argument("--reconciler-uid", default="")
    parser.add_argument("--workspace-deployer-uid", default="")
    parser.add_argument(
        "--terraform-tfvars",
        action="append",
        default=[],
        metavar="FILE",
        help=(
            "an environment's terraform/environments/<env>/<env>.tfvars: the workspace "
            "deployer's scope policy refuses its tenants' namespaces by name. Repeat for "
            "more than one. Omitted, every environment's (refuses more, never fewer); "
            "kubernetes/apply.sh --policies passes its own environment's."
        ),
    )


def policy_values(args: argparse.Namespace) -> dict[str, str]:
    """The values the cluster-scoped render interpolates.

    The scheduler and reconciler are here because the deployer's RoleBinding
    policy may bind the dispatcher and reaper Roles to them and nobody else;
    they are spelled exactly as `tenant_values` spells them for the bindings
    that policy judges.
    """
    project = project_of(args)
    scheduler = f"swarm-scheduler@{project}.iam.gserviceaccount.com"
    reconciler = f"swarm-reconciler@{project}.iam.gserviceaccount.com"
    deployer = f"{WORKSPACE_DEPLOYER_ACCOUNT}@{project}.iam.gserviceaccount.com"
    return check_values(
        {
            "PROJECT_ID": project,
            "SCHEDULER_GSA": scheduler,
            "SCHEDULER_UID": args.scheduler_uid or scheduler,
            "RECONCILER_GSA": reconciler,
            "RECONCILER_UID": args.reconciler_uid or reconciler,
            "WORKSPACE_DEPLOYER_GSA": deployer,
            "WORKSPACE_DEPLOYER_UID": args.workspace_deployer_uid or deployer,
            # The dispatcher's own name for the cache's class, so the
            # StorageClass applied and the claim every pod makes cannot differ.
            "CACHE_STORAGE_CLASS": CACHE_STORAGE_CLASS,
            # The Terraform tenants' namespaces, which the deployer's scope
            # policy refuses by name (workspace-kinds §3.1).
            "TERRAFORM_NAMESPACES": terraform_namespaces(args.terraform_tfvars),
        }
    )


#: The label that marks a namespace the workspace job created
#: (docs/design/workspace-kinds.md §3.1, §4.3 step A7). The deployer's scope
#: policy admits a Namespace outside `swarm-tenant-u-*`, and objects inside one,
#: only when it carries this label; policies/workspace-provisioner-scope.yaml
#: spells it too, and test_workspace_provisioner_scope.py runs that policy
#: against this render.
WORKSPACE_LABEL = "swarm.saga.xyz/workspace"

#: A workspace id, as the job's entry point admits it (images/workspace-apply).
_WORKSPACE_ID = re.compile(r"w-[0-9a-f]{6}")


def label_workspace(manifest: str, workspace_id: str) -> str:
    """The tenant render with WORKSPACE_LABEL on its Namespace, and nowhere else.

    The Namespace is the render's first document (TENANT_FILES), and its
    `labels:` is the one two-space-indented mapping in it. Inserted as text,
    after `substitute()`: the label is not a placeholder in the template,
    because a personal namespace -- applied by the job and by register-tenant.sh
    the same way -- must render exactly as it did before the label existed.
    """
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise RenderError(
            f"render: --workspace-id {workspace_id!r} is not a workspace id (w- and six hex "
            "digits). It becomes the label the deployer's scope policy admits a workspace "
            "namespace by, so nothing else may reach it."
        )
    first, separator, rest = manifest.partition("\n---\n")
    anchor = "\n  labels:\n"
    if "\nkind: Namespace\n" not in first or first.count(anchor) != 1:
        raise RenderError("render: the tenant render's first document is not one labelled Namespace")
    first = first.replace(anchor, f"{anchor}    {WORKSPACE_LABEL}: {workspace_id}\n")
    return first + separator + rest


#: Where every environment's tfvars lives; one root, parameterised by these.
TFVARS_GLOB = "terraform/environments/*/*.tfvars"

_TFVARS_TENANTS_OPEN = re.compile(r"tenants\s*=\s*\{")
_TFVARS_TENANT_KEY = re.compile(r"  ([a-z0-9][-a-z0-9]*)\s*=\s*\{")


def terraform_tenants(path: Path) -> list[str]:
    """The keys of `tenants = { ... }` in a tfvars file: the tenants
    terraform/infra declares, whose namespaces module.tenancy names
    `swarm-tenant-<key>`.

    Read from the file as `terraform fmt` lays it out -- the block opens at
    column 0 and each tenant is a `  <key> = {` line two spaces in -- because
    `make lint` holds every tfvars file to that layout. A file that declares
    no tenant is refused: an empty list here would let the deployer create
    `swarm-tenant-eng` with the label, which is the one thing the list is for.
    """
    keys: list[str] = []
    inside = False
    for line in path.read_text().splitlines():
        if not inside:
            inside = not line.startswith((" ", "\t")) and bool(_TFVARS_TENANTS_OPEN.fullmatch(line.strip()))
            continue
        if line.startswith("}"):
            break
        match = _TFVARS_TENANT_KEY.fullmatch(line.rstrip())
        if match:
            keys.append(match.group(1))
    if not keys:
        raise RenderError(
            f"render: {path} declares no `tenants`; the deployer's scope policy refuses the "
            "Terraform tenants' namespaces by name, and an empty list refuses none."
        )
    return keys


def terraform_namespaces(tfvars: Iterable[str]) -> str:
    """The TERRAFORM_NAMESPACES value: the namespaces of the tenants the given
    tfvars files declare, as a JSON list.

    With no file -- an offline render: lint, CI, the Makefile -- every
    environment's, so a render made without knowing the environment refuses
    more names than any one environment's, never fewer. kubernetes/apply.sh
    passes its own environment's file, because a name another environment's
    Terraform owns (prod's `research`) is one this environment's job may need.

    An offline render from a tree that holds no terraform/ at all (a copy of
    kubernetes/ alone, as the posture tests make) refuses no name, and says so
    on stderr in one `# ` line, as the offline network render does. It is
    never what is applied: apply.sh always passes a file, and a file naming no
    tenant is refused above.
    """
    paths = [Path(p) for p in tfvars] or sorted(REPO.glob(TFVARS_GLOB))
    if not paths:
        print(
            f"# render: WARNING: no tfvars under {TFVARS_GLOB}, so the workspace deployer's "
            "scope policy refuses no Terraform tenant's namespace by name. Fine for lint; "
            "kubernetes/apply.sh --policies passes its environment's tfvars.",
            file=sys.stderr,
            flush=True,
        )
    names = sorted({f"{NAMESPACE_PREFIX}{key}" for path in paths for key in terraform_tenants(path)})
    return "[" + ", ".join(f'"{name}"' for name in names) + "]"


# ---------------------------------------------------------------------------
# A workspace tenant's quota, from its record (QUOTA-ONE, #2039).
#
# The workspace job's A7 renders a workspace tenant's namespace with five
# --quota-* flags computed from the record's limits (scripts/register-tenant.sh
# ws_quota_args), and its A9 fails the workspace when the live quota differs.
# kubernetes/apply.sh run by hand passed none, so it rendered the flat defaults
# below `add_tenant_arguments` -- pods 100, cpu 400 -- and on 2026-10-23 that
# failed u-bogdan's workspace, whose 100 agents want pods 200, cpu 800. BGD's
# decision the same day: one formula, and apply.sh renders a workspace tenant's
# quota from its record. The constants are scripts/lib/workspace-quota.json,
# read here and by register-tenant.sh (ws_quota_constants); the formula is the
# shell's, line for line, and tests/unit/scripts/test_workspace_quota_one.py
# runs both over 1, 50, 100 and 1000 agents.
# ---------------------------------------------------------------------------

WORKSPACE_QUOTA_FILE = REPO / "scripts" / "lib" / "workspace-quota.json"
_WORKSPACE_QUOTA_KEYS = (
    "pods_per_agent", "cpu_per_agent", "memory_gi_per_cpu", "jobs_per_pod", "ephemeral_gi_per_pod",
    "max_active_top", "quota_pods_top", "quota_cpu_top",
)


def workspace_quota_constants() -> dict[str, int]:
    """scripts/lib/workspace-quota.json's numbers, each a whole number >= 1."""
    import json

    data = json.loads(WORKSPACE_QUOTA_FILE.read_text())
    out: dict[str, int] = {}
    for key in _WORKSPACE_QUOTA_KEYS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RenderError(f"render: {WORKSPACE_QUOTA_FILE} holds no whole {key} (got {value!r})")
        out[key] = value
    return out


def _limit(limits: dict, key: str, default: int | None, top: int) -> int:
    """One limit as ws_whole reads it: absent is DEFAULT, else a whole number
    from 1 to TOP, as an integer or a string of digits."""
    value = limits.get(key)
    if value is None or value == "":
        if default is None:
            raise RenderError(f"render: the workspace record holds no limits.{key}")
        value = default
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise RenderError(f"render: the record's limits.{key} {value!r} is not a whole number from 1 to {top}")
    return value


def workspace_quota(limits: dict) -> list[str]:
    """The five --quota-* arguments for a record's limits, as
    register-tenant.sh's ws_resolve_limits and ws_quota_args compute them:
    the record's quota_pods and quota_cpu when it states them, else the
    per-agent ratio of max_active; memory from cpu, jobs and ephemeral
    storage from pods."""
    c = workspace_quota_constants()
    max_active = _limit(limits, "max_active", None, c["max_active_top"])
    pods = _limit(limits, "quota_pods", max_active * c["pods_per_agent"], c["quota_pods_top"])
    cpu = _limit(limits, "quota_cpu", max_active * c["cpu_per_agent"], c["quota_cpu_top"])
    return [
        "--quota-pods", str(pods),
        "--quota-cpu", str(cpu),
        "--quota-memory", f"{cpu * c['memory_gi_per_cpu']}Gi",
        "--quota-jobs", str(pods * c["jobs_per_pod"]),
        "--quota-ephemeral", f"{pods * c['ephemeral_gi_per_pod']}Gi",
    ]


def _record_tenant(record: dict) -> str:
    """The tenant a record is filed under, derived from its principal by the
    frozen contract, as the job's A1 derives it: a group's by
    tenant_id_for_group, a person's and a service's by tenant_id_for_user.
    swarm-api's admin view never carries the tenant id itself."""
    from swarm_common.identity import tenant_id_for_group, tenant_id_for_user

    principal = str(record.get("principal") or "").strip().lower()
    if not principal:
        return ""
    derive = tenant_id_for_group if (record.get("kind") or "personal") == "group" else tenant_id_for_user
    try:
        return derive(principal)
    except ValueError:
        return ""


def workspace_record_for(payload: dict, tenant: str) -> dict | None:
    """TENANT's record in swarm-api's answer: `{"workspace": ...}` from
    `GET /v1/admin/workspaces/{id}`, which must be TENANT's, or
    `{"workspaces": [...]}` from `GET /v1/admin/workspaces`, where none is
    an answer (a tenant no workspace record names)."""
    if isinstance(payload.get("workspace"), dict):
        record = payload["workspace"]
        derived = _record_tenant(record)
        if derived != tenant:
            raise RenderError(
                f"render: workspace record {record.get('workspace_id')} is tenant "
                f"{derived or 'unknown'}'s, not {tenant}'s; its limits are not this namespace's"
            )
        return record
    if not isinstance(payload.get("workspaces"), list):
        raise RenderError("render: the record file holds neither `workspace` nor `workspaces`")
    found = [r for r in payload["workspaces"] if isinstance(r, dict) and _record_tenant(r) == tenant]
    if len(found) > 1:
        raise RenderError(f"render: {len(found)} workspace records name tenant {tenant}; there is one per tenant")
    return found[0] if found else None


def render_files(files: tuple[str, ...], values: dict[str, str]) -> str:
    documents = [substitute((HERE / name).read_text(), values).rstrip("\n") for name in files]
    return "\n---\n".join(documents) + "\n"


# ---------------------------------------------------------------------------
# The step-spec public keys, for GKE pods (contract request 34, #342).
#
# Owner decision, 2026-09-29 (#353): a browser pod's manifest is rendered per
# task by the scheduler, so the `{version: PEM}` map is NOT inlined into its
# `env:` -- one template placeholder away from a copy a task shapes. It lives
# in a ConfigMap in the tenant's namespace instead, which the pod mounts
# read-only at /etc/swarm/spec-verify-keys (one file per key, read by
# `agent_worker.specverify.read_mount`). No Kubernetes provider in Terraform:
# terraform/infra OUTPUTS the data (`spec_verify_keys_configmap`), this renders
# it, and kubernetes/apply.sh applies it with the rest of the namespace, in
# the release.
#
# NOT a template with placeholders: the values are multi-line PEMs, which
# `substitute()` and `_VALUE_PATTERNS` are not built for. Every value is
# validated here instead, as strictly -- a PEM carrying `---` would be a second
# YAML document in the manifest apply.sh sends to the cluster -- and emitted as
# a JSON string, which is a valid YAML double-quoted scalar.
# ---------------------------------------------------------------------------

#: The ConfigMap #353's GKE template and dispatcher mount.
SPEC_VERIFY_KEYS_CONFIG_MAP = "swarm-spec-verify-keys"

#: The keys terraform/infra's `spec_verify_keys_configmap` output may carry.
#: SPEC_VERIFY_KEYS and SPEC_SIGNING_KEY are what the worker reads from the
#: mount; the rollout pair travels beside them so a GKE pod's copy of the
#: platform's settings is the whole of what a Cloud Run Job carries.
SPEC_CONFIG_MAP_KEYS = (
    "SPEC_VERIFY_KEYS",
    "SPEC_SIGNING_KEY",
    "SPEC_SIGNATURE_MODE",
    "SPEC_LEGACY_CUTOVER",
)
SPEC_CONFIG_MAP_REQUIRED = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY")

_PEM_PUBLIC_KEY = re.compile(
    r"-----BEGIN PUBLIC KEY-----\n(?:[A-Za-z0-9+/=]{1,76}\n)+-----END PUBLIC KEY-----\n?"
)
_CUTOVER = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


#: The environment names terraform/bootstrap's `spec_signing_environments`
#: accepts, restated as a refusal here so a bad --environment cannot widen the
#: key pattern below.
_ENVIRONMENT = re.compile(r"[a-z][a-z0-9-]{0,19}")

#: The ring and key terraform/modules/spec_signing_key names, for the
#: `swarm` name prefix both roots use: `${name_prefix}-${environment}-specs`
#: and `step-spec` (terraform/bootstrap/spec_signing.tf passes
#: var.environment per ring). Spelled here because render.py reads no
#: terraform; a rename there must be made here too, or every apply refuses.
SPEC_KEY_RING = "swarm-{environment}-specs"
SPEC_CRYPTO_KEY = "step-spec"


def spec_verify_keys_data(path: str, project: str, environment: str) -> dict[str, str]:
    """The ConfigMap's data, from `terraform output -json spec_verify_keys_configmap`.

    Refuses, before anything is rendered, anything the worker would refuse or
    that could carry YAML: an unknown key, a key version outside the signing
    key, a signing key that is not THIS environment's step-spec key, a value
    that is not a PEM public key.

    THE RING AND THE KEY ARE PINNED, not just the project (#346). Any crypto
    key in the project used to pass, so a payload naming dev's ring, or some
    other key in the shared project, rendered a ConfigMap whose workers would
    verify specs signed by whatever holds that key -- the canonical form
    carries no environment, which is why each environment has its own ring.
    Only the location is free: it is the region the environment runs in.
    """
    import json

    if not _ENVIRONMENT.fullmatch(environment or ""):
        raise RenderError(
            f"render: --environment {environment!r} is not an environment name; the "
            "step-spec key is per environment (swarm-<env>-specs), so the keys cannot be "
            "checked without it"
        )

    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RenderError(f"render: --spec-verify-keys-file {path!r} is not readable JSON: {exc}")
    if not isinstance(payload, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in payload.items()
    ):
        raise RenderError(
            "render: --spec-verify-keys-file must hold terraform/infra's "
            "spec_verify_keys_configmap output, a JSON object of strings"
        )
    unknown = sorted(set(payload) - set(SPEC_CONFIG_MAP_KEYS))
    missing = [k for k in SPEC_CONFIG_MAP_REQUIRED if not payload.get(k)]
    if unknown or missing:
        raise RenderError(
            f"render: --spec-verify-keys-file: unknown keys {unknown}, missing {missing}; "
            f"expected {list(SPEC_CONFIG_MAP_KEYS)}"
        )

    signing_key = payload["SPEC_SIGNING_KEY"]
    ring = SPEC_KEY_RING.format(environment=environment)
    key_pattern = re.compile(
        rf"projects/{re.escape(project)}/locations/[a-z0-9-]{{1,63}}"
        rf"/keyRings/{re.escape(ring)}/cryptoKeys/{re.escape(SPEC_CRYPTO_KEY)}"
    )
    if not key_pattern.fullmatch(signing_key):
        raise RenderError(
            f"render: SPEC_SIGNING_KEY {signing_key!r} is not "
            f"projects/{project}/locations/<region>/keyRings/{ring}/cryptoKeys/"
            f"{SPEC_CRYPTO_KEY}, the {environment!r} environment's step-spec key"
        )

    try:
        keys = json.loads(payload["SPEC_VERIFY_KEYS"])
    except ValueError as exc:
        raise RenderError(f"render: SPEC_VERIFY_KEYS is not valid JSON: {exc}")
    if not isinstance(keys, dict) or not keys:
        raise RenderError(
            "render: SPEC_VERIFY_KEYS must be a non-empty JSON object of version name -> PEM; "
            "an empty one verifies nothing, and every signed task would fail CANNOT_START"
        )
    version_pattern = re.compile(rf"{re.escape(signing_key)}/cryptoKeyVersions/[0-9]+")
    for version, pem in keys.items():
        if not isinstance(version, str) or not version_pattern.fullmatch(version):
            raise RenderError(
                f"render: SPEC_VERIFY_KEYS names {version!r}, which is not a version of "
                f"{signing_key}; the worker would refuse every task it signed"
            )
        if not isinstance(pem, str) or not _PEM_PUBLIC_KEY.fullmatch(pem):
            raise RenderError(f"render: SPEC_VERIFY_KEYS[{version!r}] is not a PEM public key")

    mode = payload.get("SPEC_SIGNATURE_MODE", "")
    if mode and mode not in ("enforce", "legacy"):
        raise RenderError(f"render: SPEC_SIGNATURE_MODE must be enforce or legacy, got {mode!r}")
    cutover = payload.get("SPEC_LEGACY_CUTOVER", "")
    if cutover and not _CUTOVER.fullmatch(cutover):
        raise RenderError(
            f"render: SPEC_LEGACY_CUTOVER {cutover!r} is not an RFC 3339 time with a zone"
        )
    return {k: payload[k] for k in SPEC_CONFIG_MAP_KEYS if payload.get(k)}


def render_spec_verify_keys(data: dict[str, str], values: dict[str, str]) -> str:
    """The `swarm-spec-verify-keys` ConfigMap document, in the tenant's namespace."""
    import json

    names = check_values({k: values[k] for k in ("NAMESPACE", "TENANT_ID")})
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        f"  name: {SPEC_VERIFY_KEYS_CONFIG_MAP}",
        f"  namespace: {names['NAMESPACE']}",
        "  labels:",
        "    managed-by: swarm-terraform",
        f"    swarm-tenant: {names['TENANT_ID']}",
        "    app.kubernetes.io/part-of: swarm",
        "  annotations:",
        "    swarm.saga.xyz/rationale: >-",
        "      The step-spec public keys (contract request 34), from terraform/infra's",
        "      spec_verify_keys_configmap output. Mounted read-only by worker pods;",
        "      written only by kubernetes/apply.sh with the deployer's credentials.",
        "      No Role in this namespace may write a ConfigMap.",
        "data:",
    ]
    lines += [f"  {key}: {json.dumps(value)}" for key, value in data.items()]
    return "\n".join(lines) + "\n"


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
            or f"{args.region}-docker.pkg.dev/{project_of(args)}/"
            f"{args.registry}/{profile.image}:{args.tag}",
            # The CPU request and its burst limit, in millicores, from the
            # dispatcher's own `cpu_quantity` so the YAML renders what runs:
            # invariant 7 as amended for CPU by contract request RS-1
            # (SIZES-SCHED-1). Memory and ephemeral-storage stay one number.
            "CPU": cpu_quantity(rc.cpu),
            "CPU_LIMIT": cpu_quantity(rc.cpu_limit_cores),
            "MEMORY": f"{rc.memory_gib}Gi",
            # The local scratch volumes' sizeLimits -- workspace, /tmp, HOME --
            # and the whole pod's ephemeral-storage, their sum; and the cache's
            # network-disk claim (CACHE-PVC), which is not in that sum. The dispatcher's
            # own numbers (GkeDisk), imported, so the YAML cannot drift from
            # what runs: until 2026-10-07 the browser template's 8 + 2 + 4 GiB
            # of volumes sat under an 8 GiB pod limit (contract request 53).
            "DISK": f"{gke_disk(rc).workspace_gib}Gi",
            "TMP_DISK": f"{gke_disk(rc).tmp_gib}Gi",
            "CACHE_DISK": f"{CACHE_VOLUME_GIB}Gi",
            "HOME_DISK": f"{gke_disk(rc).home_gib}Gi",
            "EPHEMERAL": f"{gke_ephemeral_storage_gib(rc)}Gi",
            # The worker's local scratch volumes and their limits, the
            # dispatcher's own value (SCRATCH_VOLUMES_ENV, #2436); the gvisor
            # template's HOME volume differs and is set below.
            "SCRATCH_VOLUMES": scratch_volumes_value(gke_disk(rc)),
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
            "ARTIFACT_BUCKET": args.bucket or f"{project_of(args)}-swarm-artifacts",
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
        # v2's HOME is its own `work` volume at /work (worker-job-v2.yaml),
        # not the dispatcher's `home` at /home/swarm.
        values["SCRATCH_VOLUMES"] = scratch_volumes_value(
            gke_disk(rc), home_volume="work", home_mount="/work"
        )
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
        "--verify-uid",
        default="",
        help=(
            "the numeric uniqueId of swarm-verify@<project>, the verification gate, "
            "bound by rbac/verify-rbac.yaml to read Jobs. GKE presents it by uniqueId "
            "for the OAuth access token the gate uses, as for the scheduler. Resolve it "
            "with `gcloud iam service-accounts describe "
            "swarm-verify@<project>.iam.gserviceaccount.com --format='value(uniqueId)'`. "
            "The read is rendered only when this is given, and only in the eng and smoke "
            "tenants' own namespaces, never with --workspace-id (verify_read_wanted). "
            "kubernetes/apply.sh resolves and passes it itself."
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
    parser.add_argument(
        "--workspace-id",
        default="",
        help=(
            f"the workspace the job is building (w- and six hex digits): rendered as "
            f"{WORKSPACE_LABEL} on the Namespace and nowhere else. The workspace deployer's "
            "scope policy admits a namespace outside swarm-tenant-u-* only with it "
            "(docs/design/workspace-kinds.md §4.3, A7). Omitted for a personal namespace "
            "and for a Terraform tenant's, which render as they always have."
        ),
    )
    parser.add_argument(
        "--project",
        default="",
        help=(
            "the project whose identities the namespace binds. Required with the cluster "
            f"network; an offline render names {OFFLINE_PROJECT}, which is no project"
        ),
    )
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
    parser.add_argument(
        "--spec-verify-keys-file",
        default="",
        help=(
            "a file holding `terraform output -json spec_verify_keys_configmap` "
            "from terraform/infra: renders the swarm-spec-verify-keys ConfigMap "
            "(contract request 34) into the tenant's namespace. kubernetes/apply.sh "
            "passes it as --spec-verify-keys; omitted, no ConfigMap is rendered and "
            "any existing one is left as it is."
        ),
    )
    parser.add_argument(
        "--environment",
        default="",
        help=(
            "the environment the keys belong to (dev, prod, ...): "
            "--spec-verify-keys-file accepts only that environment's step-spec key, "
            "swarm-<env>-specs/step-spec. kubernetes/apply.sh passes its ${ENVIRONMENT}."
        ),
    )
    # THE TENANT NAMESPACE'S ResourceQuota, FROM ITS CALLER (DAYONE-K8S-1,
    # #2039, lesson 32). No default: a render that can reach a cluster (one
    # given the cluster network) is refused without all five, so a namespace's
    # quota is always the one its caller read -- the workspace record's
    # (`workspace-quota`) or a Terraform tenant's (`terraform-quota`, the
    # TERRAFORM_TENANT_QUOTA numbers and why). An offline render uses
    # TERRAFORM_TENANT_QUOTA, like its documentation network.
    parser.add_argument("--quota-pods", type=int, default=None)
    parser.add_argument("--quota-jobs", type=int, default=None)
    parser.add_argument("--quota-cpu", type=int, default=None)
    parser.add_argument("--quota-memory", default=None)
    parser.add_argument("--quota-ephemeral", default=None)


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

    policies = sub.add_parser(
        "policies",
        help="the cluster-scoped admission policies and the workspace deployer's RBAC",
        allow_abbrev=False,
    )
    add_policy_arguments(policies)

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

    # WHETHER THE RENDER BINDS swarm-verify (VERIFY-GKE-READ-2): the account's
    # email on one line where a tenant render carries the Job read
    # (verify_read_wanted), an empty line elsewhere. kubernetes/apply.sh asks
    # here, with the same forwarded arguments, before it resolves the
    # account's uniqueId -- so the namespaces that carry the read are named in
    # one place, and a workspace deployer's apply never looks the account up.
    verify_read = sub.add_parser(
        "verify-read",
        help="swarm-verify's email if a tenant render carries its Job read, else an empty line",
        allow_abbrev=False,
    )
    add_tenant_arguments(verify_read)

    # swarm-system's ingress (docs/design/live-browser.md §8.3). Its one input
    # is READ by kubernetes/apply.sh --swarm-system from swarm-api's service
    # and subnet, and refused on that script's command line, like the tenant
    # network.
    system = sub.add_parser(
        "swarm-system",
        help="swarm-system's default-deny ingress and the live relay's TCP 8080",
        allow_abbrev=False,
    )
    system.add_argument(
        "--swarm-api-egress-cidr",
        default="",
        help=(
            "swarm-api's Direct VPC egress subnet range: the ipCidrRange of the "
            "subnetwork in the run.googleapis.com/network-interfaces annotation of "
            "`gcloud run services describe swarm-api`. Omitted, renders a "
            "documentation range marked not-for-apply."
        ),
    )
    system.add_argument(
        "--swarm-api-egress-source",
        default="",
        help="where the range came from: run/<project>/<region>/<service>/<subnetwork> from apply.sh",
    )

    # A WORKSPACE TENANT'S QUOTA (QUOTA-ONE, #2039): the five --quota-* flags
    # for TENANT's record, one per line, or nothing when no record names it.
    # kubernetes/apply.sh reads the record from swarm-api and asks here, so
    # the formula is this file's and the constants the one JSON file's.
    workspace_quota_cmd = sub.add_parser(
        "workspace-quota",
        help="the --quota-* flags for a tenant's workspace record, one per line",
        allow_abbrev=False,
    )
    workspace_quota_cmd.add_argument("--tenant", required=True)
    workspace_quota_cmd.add_argument(
        "--record",
        required=True,
        help="swarm-api's answer: GET /v1/admin/workspaces/{id} or GET /v1/admin/workspaces",
    )
    workspace_quota_cmd.add_argument(
        "--with-id",
        action="store_true",
        help="print the record's workspace id on the first line, before the flags (apply.sh names it)",
    )

    # THE TERRAFORM TENANTS a tfvars file declares, one per line: apply.sh
    # keeps the defaults for these and reads no workspace record.
    terraform_tenants_cmd = sub.add_parser(
        "terraform-tenants",
        help="the tenants a tfvars file's `tenants` declares, one per line",
        allow_abbrev=False,
    )
    terraform_tenants_cmd.add_argument("--terraform-tfvars", required=True)

    # A TERRAFORM TENANT'S QUOTA: the five --quota-* flags of
    # TERRAFORM_TENANT_QUOTA, one per line, as `workspace-quota` prints a
    # workspace record's. kubernetes/apply.sh passes them for a tenant its
    # environment's tfvars declares, because the tenant render has no default.
    sub.add_parser(
        "terraform-quota",
        help="the --quota-* flags for a Terraform tenant's namespace, one per line",
        allow_abbrev=False,
    )

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
        require_cluster_inputs(tenant, args)
        values = tenant_values(args)
        if values["NETWORK_SOURCE"] == OFFLINE_NETWORK_SOURCE:
            # stderr, so the render on stdout stays a clean manifest for the
            # lint and checkov callers that pipe it. ONE line starting `# `:
            # a caller that captures stderr with stdout (the SwarmCloud
            # worker's pre-publish checkov does) still holds a YAML file, where
            # a bare warning line made the whole render unparseable and checkov
            # reported every job pod as having no NetworkPolicy (CKV2_K8S_6).
            print(
                f"# render: WARNING: no cluster network was given, so the egress policy "
                f"carries documentation addresses and is marked {OFFLINE_NETWORK_SOURCE}. "
                "Fine for lint; wrong for a cluster. kubernetes/apply.sh reads the real "
                "values from the cluster.",
                file=sys.stderr,
                flush=True,
            )
        # Validated BEFORE anything is written, so a bad output renders nothing.
        spec_data = (
            spec_verify_keys_data(args.spec_verify_keys_file, project_of(args), args.environment)
            if args.spec_verify_keys_file
            else None
        )
        files = tenant_files(bound_ksas(args)) + verify_read_files(
            values["TENANT_ID"], values["NAMESPACE"], args.verify_uid, args.workspace_id
        )
        manifest = render_files(files, values)
        if args.workspace_id:
            manifest = label_workspace(manifest, args.workspace_id)
        if spec_data is not None:
            manifest += "---\n" + render_spec_verify_keys(spec_data, values)
        sys.stdout.write(manifest)
    elif args.command == "identity":
        values = check_values(tenant_values(args))
        sys.stdout.write(f"{values['NAMESPACE']} {values['GSA_EMAIL']} {values['KSA_NAME']}\n")
    elif args.command == "verify-read":
        values = check_values(tenant_values(args))
        wanted = verify_read_wanted(values["TENANT_ID"], values["NAMESPACE"], args.workspace_id)
        sys.stdout.write(f"{values['VERIFY_GSA'] if wanted else ''}\n")
    elif args.command == "policies":
        sys.stdout.write(render_files(POLICY_FILES, policy_values(args)))
    elif args.command == "swarm-system":
        values = swarm_system_values(args)
        if values["SWARM_API_EGRESS_SOURCE"] == OFFLINE_NETWORK_SOURCE:
            # One `# ` line, for the reason the tenant render's warning gives.
            print(
                "# render: WARNING: no swarm-api egress range was given, so the relay's ingress "
                f"admits a documentation range and is marked {OFFLINE_NETWORK_SOURCE}. Fine for "
                "lint; wrong for a cluster. kubernetes/apply.sh --swarm-system reads the real one.",
                file=sys.stderr,
                flush=True,
            )
        sys.stdout.write(render_files(SWARM_SYSTEM_FILES, values))
    elif args.command == "workspace-quota":
        import json

        try:
            payload = json.loads(Path(args.record).read_text())
        except (OSError, ValueError) as exc:
            raise RenderError(f"render: could not read the workspace record {args.record}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RenderError("render: the record file holds neither `workspace` nor `workspaces`")
        record = workspace_record_for(payload, args.tenant)
        if record is not None:
            lines = workspace_quota(dict(record.get("limits") or {}))
            if args.with_id:
                lines.insert(0, str(record.get("workspace_id") or "unknown"))
            sys.stdout.write("\n".join(lines) + "\n")
    elif args.command == "terraform-tenants":
        sys.stdout.write("".join(f"{key}\n" for key in terraform_tenants(Path(args.terraform_tfvars))))
    elif args.command == "terraform-quota":
        sys.stdout.write(
            "".join(f"--{key.replace('_', '-')}\n{value}\n" for key, value in TERRAFORM_TENANT_QUOTA.items())
        )
    elif args.command == "job":
        sys.stdout.write(render_job(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
