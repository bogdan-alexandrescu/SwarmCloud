"""Stopping a cancelled task's execution from the cancel route itself (#627).

WHY THE API DOES THIS NOW. A cancel used to be a flag: the worker would see it
at its next control poll, or the reconciler would stop the execution once the
worker went silent. Some executions took 7-13 hours to stop that way (four mock
steps for 12.8 h on 2026-10-04, two browser steps for 7.6 h on 2026-09-24), all
the while holding their leases and pools. So the route asks the backend to stop
the execution directly, the moment the cancel is requested: Cloud Run's
`CancelExecution`, or a delete of the GKE Job with Background propagation --
the same two operations `reconciler.backends` uses, restated here because the
swarm-api image carries only `apps/common`, `apps/redaction` and this package.
Both are spoken over REST with google-auth's session, so no new client library
enters the image.

WHAT THIS DOES NOT DO: release a lease, decrement a pool or write the task.
Stopping the execution sends the worker a SIGTERM; the worker sees the cancel
and ends the task CANCELLED with its spend (`agent_worker.lifecycle.
_handle_interruption`), and if it cannot, the reconciler finds the execution
gone and finishes it (invariants 1-3 are kept where they were kept before).

WHAT IT REFUSES TO TOUCH. The project is shared with another team's GKE
cluster, VPC, buckets and service accounts (scripts/lib/common.sh deny-list).
Nothing here takes a resource name from a caller: the name is the one the
dispatcher recorded on the attempt, and it is checked before it is used --

  * a Cloud Run execution must be under THIS project and region and a job
    whose name starts `swarm-`; the dispatcher's `pending-` placeholder is not
    an execution and is skipped;
  * a GKE Job must be in a `swarm-tenant-` namespace on the cluster
    GKE_ENDPOINT names (the swarm's own; nothing here can name another), and
    is READ first: it is deleted only if it carries a runtime `managed-by`
    marker and its tenant label matches the task's tenant.

Never raises: a cancel that could not reach the backend is still a cancel, and
the reconciler remains the backstop. Every outcome is logged with the
execution's name, never with a credential.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Cloud Run's API host. Fixed: the execution name says which project.
CLOUD_RUN_API = "https://run.googleapis.com/v2"

#: The backends an attempt records (`swarm_common.profiles.Backend`), by value.
CLOUD_RUN_JOB = "CLOUD_RUN_JOB"
GKE_AUTOPILOT = "GKE_AUTOPILOT"

#: The dispatcher's stand-in when Cloud Run named no execution
#: (`scheduler.dispatch`, `reconciler.backends.PLACEHOLDER_EXECUTION_MARKER`).
PLACEHOLDER_MARKER = "/executions/pending-"

#: Markers whose Jobs the runtime created, and the only ones this deletes --
#: the reconciler's `GC_MANAGED_VALUES`. A `swarm-terraform` resource has an
#: owner that recreates it.
RUNTIME_MARKERS = frozenset({"swarm", "swarm-scheduler", "swarm-worker"})
TENANT_LABELS = ("swarm-tenant-id", "swarm-tenant")

#: Every call's bound. The route has answered by the time this runs (it is a
#: background task), but a hung call would still hold a worker thread.
TIMEOUT_SECONDS = 15.0

_NAME = r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?"


@dataclass(frozen=True)
class ExecutionTarget:
    """The execution a cancel should stop: what the attempt document recorded."""

    tenant_id: str
    task_id: str
    attempt_id: str
    backend: str
    execution_name: str


class ExecutionCanceller(Protocol):
    def cancel(self, target: ExecutionTarget) -> str:
        """Ask the backend to stop it. Returns the outcome word; never raises."""


class NoExecutionCanceller:
    """Asks nothing: a deployment with execution cancel turned off."""

    def cancel(self, target: ExecutionTarget) -> str:
        return "disabled"


def _label_safe(value: str) -> str:
    """How a label value spells an id: lower case, `_` and other marks as `-`."""
    return re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")


class RestExecutionCanceller:
    """Cloud Run and GKE, over REST, with the service's own credentials.

    `session` is anything with `post`, `get` and `delete` the way
    `google.auth.transport.requests.AuthorizedSession` has them; built on first
    use, so `create_app()` builds no client and a test injects a fake.
    """

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        gke_endpoint: str = "",
        gke_ca_cert_b64: str = "",
        namespace_prefix: str = "swarm-tenant-",
        session: Any | None = None,
    ) -> None:
        self._project = project_id
        self._region = region
        self._gke_endpoint = gke_endpoint.strip()
        self._gke_ca_b64 = gke_ca_cert_b64.strip()
        self._prefix = namespace_prefix
        self._session = session
        self._ca_file: str | None = None
        self._run_name = re.compile(
            rf"^projects/{re.escape(project_id)}/locations/{re.escape(region)}"
            rf"/jobs/swarm-[-a-z0-9]{{1,60}}/executions/{_NAME}$"
        )
        self._job_name = re.compile(rf"^({re.escape(namespace_prefix)}[-a-z0-9]{{1,50}})/({_NAME})$")

    # -- the one entry point -------------------------------------------------
    def cancel(self, target: ExecutionTarget) -> str:
        try:
            if target.backend == CLOUD_RUN_JOB:
                outcome = self._cancel_cloud_run(target)
            elif target.backend == GKE_AUTOPILOT:
                outcome = self._cancel_gke(target)
            else:
                outcome = "unknown_backend"
        except Exception as exc:  # never raises: the reconciler is the backstop
            outcome = f"error:{type(exc).__name__}"
        level = logging.INFO if outcome in {"requested", "gone", "finished"} else logging.WARNING
        log.log(
            level,
            "asked the backend to stop a cancelled task's execution",
            extra={
                "tenant_id": target.tenant_id,
                "task_id": target.task_id,
                "attempt_id": target.attempt_id,
                "backend": target.backend,
                "execution": target.execution_name,
                "outcome": outcome,
            },
        )
        return outcome

    # -- Cloud Run -----------------------------------------------------------
    def _cancel_cloud_run(self, target: ExecutionTarget) -> str:
        name = target.execution_name
        if PLACEHOLDER_MARKER in name:
            return "placeholder"
        if not self._run_name.match(name):
            return "refused_name"
        response = self._http().post(
            f"{CLOUD_RUN_API}/{name}:cancel", json={}, timeout=TIMEOUT_SECONDS
        )
        status = int(getattr(response, "status_code", 0))
        if 200 <= status < 300:
            return "requested"
        if status == 404:
            return "gone"
        if status in (400, 409):
            # "cannot be cancelled because it is not running": it finished
            # between the worker's last word and this call.
            return "finished"
        return f"http_{status}"

    # -- GKE -----------------------------------------------------------------
    def _cancel_gke(self, target: ExecutionTarget) -> str:
        if not self._gke_endpoint or not self._gke_ca_b64:
            return "gke_unconfigured"
        match = self._job_name.match(target.execution_name)
        if not match:
            return "refused_name"
        namespace, job = match.group(1), match.group(2)
        url = (
            f"https://{self._gke_host()}/apis/batch/v1/namespaces/{namespace}/jobs/{job}"
        )
        http = self._http()
        verify = self._ca_path()
        read = http.get(url, timeout=TIMEOUT_SECONDS, verify=verify)
        status = int(getattr(read, "status_code", 0))
        if status == 404:
            return "gone"
        if not 200 <= status < 300:
            return f"http_{status}"
        labels = ((read.json() or {}).get("metadata") or {}).get("labels") or {}
        if labels.get("managed-by") not in RUNTIME_MARKERS:
            return "refused_unmanaged"
        tenant = next((labels[k] for k in TENANT_LABELS if labels.get(k)), "")
        if _label_safe(str(tenant)) != _label_safe(target.tenant_id):
            return "refused_tenant"
        deleted = http.delete(
            url,
            json={"propagationPolicy": "Background", "gracePeriodSeconds": 30},
            timeout=TIMEOUT_SECONDS,
            verify=verify,
        )
        status = int(getattr(deleted, "status_code", 0))
        if 200 <= status < 300:
            return "requested"
        if status == 404:
            return "gone"
        return f"http_{status}"

    def _gke_host(self) -> str:
        endpoint = self._gke_endpoint
        for scheme in ("https://", "http://"):
            if endpoint.startswith(scheme):
                endpoint = endpoint[len(scheme):]
        return endpoint.rstrip("/")

    def _ca_path(self) -> str:
        if self._ca_file is None:
            handle = tempfile.NamedTemporaryFile(prefix="gke-ca-", suffix=".crt", delete=False)
            handle.write(base64.b64decode(self._gke_ca_b64))
            handle.close()
            self._ca_file = handle.name
        return self._ca_file

    def _http(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._session = AuthorizedSession(credentials)
        return self._session


__all__ = [
    "CLOUD_RUN_JOB",
    "ExecutionCanceller",
    "ExecutionTarget",
    "GKE_AUTOPILOT",
    "NoExecutionCanceller",
    "RestExecutionCanceller",
]
