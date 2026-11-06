"""Stage 0, preflight: nothing writes until the person's rights, the org's
policies, the quotas and the release are known (design §4, D14; plan §9.1).

Every step here declares `read_only = True`: its `apply` only asks and reads,
so `swarm install --dry-run` runs it (design §2.4) and a failed preflight
leaves the project exactly as it found it (lesson 1, B01: the fake records
zero mutating calls).

| Step | Manifest entries | Lesson |
|---|---|---|
| `preflight.tools` | the core `local-tool.*` | 12: terraform `1.16.2`, kubectl ≥ 1.30 |
| `preflight.sign-in` | `other.installer-sign-in` | gcloud and ADC signed in, by the installer itself; ADC with the Directory scopes, or without them when Google refuses them (`directory_scopes` false; stage 8 asks, N14), a blocked consent page included: that sign-in waits at most `CONSENT_WAIT_S` and Ctrl-C stops gcloud, not the run (audit 4a F2); records `installer` |
| `preflight.organization` | — | audit #24: no organisation is said first; the Cloud Identity Free sign-up opens and is polled; billing creation opens when the organisation waits on it (N16) |
| `preflight.inputs` | `other.github-org` | R7: the alert email, the hostname (the verb's), the GitHub org, the billing account (listed; with none, billing creation opens and is polled, N15) |
| `preflight.region` | — | 2: the region (the verb's) is confirmed, typed, before stage 1 |
| `preflight.rights` | `switch.project-create-right`, `switch.billing-link-right` | 1 and 16; the parent is listed |
| `preflight.org-domain` | `other.org-domain` | the domain IAP admits, read from the organization |
| `preflight.installer-address` | `other.installer-address` | the installer's /32 for the cluster's authorized networks |
| `preflight.org-policies` | `org-policy.*` | read **effective** (inherited) policy |
| `preflight.quotas` | `quota.*` | 4: the KMS signing quota is read; the CPUS quota is the smaller of the region's and the project-wide CPUS_ALL_REGIONS (audit 4a F5); the Quotas page opens (a Free Trial account's upgrade page when that is the cap) and the increase is polled for (#32) |
| `preflight.sizing` | `other.deployment-sizing` | decision 7: one person or a team of N, the proposed ceilings |
| `preflight.sizing-quota` (stage 1) | `other.deployment-sizing-quota` | the sizing against a new project's usable CPU quota (regional and project-wide) |
| `preflight.release` | `other.release-home`, `other.release-bundle` | 27: CVE acceptances with expiry; records `state.release` (audit #2) |

This module also holds `Gcp`, the engine's one REST client for stages 0-2. It
builds its session lazily (no client at import time) and every call is a typed
probe, never a parsed gcloud line (design §2.2).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import secret_entry, sizing, tools
from ..api_client import CLOUD_PLATFORM, application_default
from ..exitcodes import MET, UNMET
from ..secret_entry import DevTty, Terminal
from ..waits import WaitSpec, wait_until
from . import Observation, Plan, open_page

# -- the REST client ---------------------------------------------------------


@dataclass(frozen=True)
class CpuQuotas:
    """The two Compute Engine CPU quotas a GKE Autopilot cluster's nodes count
    against: the region's CPUS and the project-wide CPUS_ALL_REGIONS (audit 4a
    F5). `usable` is the smaller, what the sizing caps and requires on."""

    region: int | None
    all_regions: int | None

    @property
    def usable(self) -> int | None:
        return sizing.usable_cpus(self.region, self.all_regions)

    @property
    def all_regions_binds(self) -> bool:
        """CPUS_ALL_REGIONS, not the region's CPUS, is the smaller limit."""
        return (self.all_regions is not None and self.all_regions >= 0
                and (self.region is None or self.region < 0 or self.all_regions < self.region))

    def said(self, region: str) -> str:
        return f"{self.usable} ({region} CPUS {self.region}, CPUS_ALL_REGIONS {self.all_regions})"


def _compute_limit(found: Mapping[str, Any], metric: str) -> int | None:
    """`metric`'s limit in a Compute Engine region or project's `quotas`, or None."""
    for quota in found.get("quotas", []) or []:
        if quota.get("metric") == metric and quota.get("limit") is not None:
            return int(float(quota["limit"]))
    return None

CRM = "https://cloudresourcemanager.googleapis.com"
BILLING = "https://cloudbilling.googleapis.com/v1"
BUDGETS = "https://billingbudgets.googleapis.com/v1"
USAGE = "https://serviceusage.googleapis.com"
STORAGE = "https://storage.googleapis.com/storage/v1"
IAM = "https://iam.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
IAP = "https://iap.googleapis.com/v1"
SECRET_MANAGER_API = "https://secretmanager.googleapis.com/v1"

#: The release home (D25, `other.release-home`): public, read anonymously.
RELEASE_HOME = "https://storage.googleapis.com/swarmcloud"
#: Where an access token's granted scopes are read (preflight.sign-in).
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"


class ApiError(RuntimeError):
    """A Google API answered with an error. `status` is the HTTP code and
    `reason` the canonical status (`PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`)."""

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(f"HTTP {status} {reason}: {message}")
        self.status = status
        self.reason = reason

    @property
    def rate_limited(self) -> bool:
        """A write-rate limit (lesson 3): 429, or a quota error on a write. Never a denial."""
        return self.status == 429 or self.reason in ("RESOURCE_EXHAUSTED", "rateLimitExceeded")

    @property
    def denied(self) -> bool:
        return self.status == 403 and not self.rate_limited


class Gcp:
    """The Google Cloud calls stages 0-2 make, over REST with the person's
    application-default credentials.

    `quota_project` is the install's project, which the billing-budget calls
    (stage 1 on, once the project exists) send as `x-goog-user-project`: with
    user credentials the Budgets API refuses a call that names none, and a
    fresh laptop's sign-in sets none (audit #8). It is the header
    `credentials.with_quota_project(project)` makes a session send. Every
    stage-0 call goes without one: no project exists yet.

    The session sends no other quota project. `gcloud auth application-default
    login` writes the laptop's gcloud project into the credentials (and
    GOOGLE_CLOUD_QUOTA_PROJECT can name one), and google-auth would send it on
    every call: a project unrelated to the install, whose disabled Cloud
    Billing API refused stage 0 (issue 2942). The session's credentials drop
    it (`api_client.application_default`, the one place that rule lives), so
    only `quota_project`, where a call names it, is ever sent."""

    def __init__(self, session: Any = None, *, quota_project: str | None = None) -> None:
        self._session = session
        self._from_adc = session is None
        self.quota_project = quota_project

    def reset_credentials(self) -> None:
        """Forget the session built from application-default credentials, so the
        next call loads them again: after preflight.organization signs in as
        another account, the cached session still holds the old account's and
        would never see the new organisation. A session given in is kept."""
        if self._from_adc:
            self._session = None

    @property
    def session(self) -> Any:
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession  # lazy, as application_default

            self._session = AuthorizedSession(application_default([CLOUD_PLATFORM_SCOPE]))
        return self._session

    def _call(self, method: str, url: str, *, body: Any = None, params: Any = None,
              missing_ok: bool = False, quota_project: str | None = None) -> Any:
        extra = {"headers": {"x-goog-user-project": quota_project}} if quota_project else {}
        response = self.session.request(method, url, json=body, params=params, timeout=60, **extra)
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            reason = str(error.get("status") or "")
            if not reason and error.get("errors"):
                reason = str(error["errors"][0].get("reason", ""))
            raise ApiError(response.status_code, reason, str(error.get("message") or response.reason))
        return response.json() if response.content else {}

    def _pages(self, url: str, key: str, params: dict[str, Any] | None = None, *,
               quota_project: str | None = None) -> list[Any]:
        found: list[Any] = []
        params = dict(params or {})
        while True:
            page = self._call("GET", url, params=params, quota_project=quota_project) or {}
            found.extend(page.get(key, []))
            token = page.get("nextPageToken")
            if not token:
                return found
            params["pageToken"] = token

    # Resource Manager and billing ------------------------------------------

    def project(self, project: str) -> dict[str, Any] | None:
        """The project, or None when it does not exist or the caller cannot see it."""
        found = self._call("GET", f"{CRM}/v3/projects:search", params={"query": f"id:{project}"})
        projects = (found or {}).get("projects", [])
        return projects[0] if projects else None

    def ancestry(self, project: str) -> list[str]:
        """`projects/p`, then each folder, then the organization."""
        found = self._call("POST", f"{CRM}/v1/projects/{project}:getAncestry", body={})
        return [
            f"{a['resourceId']['type']}s/{a['resourceId']['id']}" for a in found.get("ancestor", [])
        ]

    def test_permissions(self, resource: str, permissions: Sequence[str]) -> set[str]:
        """The subset of `permissions` the caller holds on `resource`
        (`projects/..`, `folders/..`, `organizations/..`, `billingAccounts/..`),
        inherited and group bindings included."""
        base = BILLING if resource.startswith("billingAccounts/") else f"{CRM}/v3"
        found = self._call("POST", f"{base}/{resource}:testIamPermissions",
                           body={"permissions": list(permissions)})
        return set(found.get("permissions", []))

    def billing_accounts(self) -> list[dict[str, Any]]:
        """The open billing accounts the caller can see (`billingAccounts.list`)."""
        return self._pages(f"{BILLING}/billingAccounts", "billingAccounts", {"filter": "open=true"})

    def organizations(self) -> list[dict[str, Any]]:
        """The organizations the caller can see (`organizations.search`)."""
        return self._pages(f"{CRM}/v3/organizations:search", "organizations")

    def folders(self) -> list[dict[str, Any]]:
        """The active folders the caller can see (`folders.search`)."""
        return self._pages(f"{CRM}/v3/folders:search", "folders", {"query": "state=ACTIVE"})

    def organization(self, name: str) -> dict[str, Any] | None:
        """`organizations/N`: its `displayName` is the Workspace or Cloud Identity primary domain."""
        return self._call("GET", f"{CRM}/v3/{name}", missing_ok=True)

    def folder(self, name: str) -> dict[str, Any] | None:
        """`folders/N`, whose `parent` is a folder or the organization."""
        return self._call("GET", f"{CRM}/v3/{name}", missing_ok=True)

    def billing_info(self, project: str) -> dict[str, Any]:
        return self._call("GET", f"{BILLING}/projects/{project}/billingInfo")

    def iam_policy(self, project: str) -> dict[str, Any]:
        return self._call("POST", f"{CRM}/v1/projects/{project}:getIamPolicy",
                          body={"options": {"requestedPolicyVersion": 3}})

    def set_iam_policy(self, project: str, policy: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{CRM}/v1/projects/{project}:setIamPolicy", body={"policy": policy})

    def budgets(self, account: str) -> list[dict[str, Any]]:
        return self._pages(f"{BUDGETS}/billingAccounts/{account}/budgets", "budgets",
                           quota_project=self.quota_project)

    def create_budget(self, account: str, budget: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{BUDGETS}/billingAccounts/{account}/budgets", body=budget,
                          quota_project=self.quota_project)

    # Org policy and quotas ---------------------------------------------------

    def effective_policy(self, resource: str, constraint: str) -> dict[str, Any]:
        """The policy in force on `resource` (`projects/..`, `folders/..`,
        `organizations/..`) after inheritance, not only its own. Read through
        Resource Manager v1's `getEffectiveOrgPolicy`, which takes no quota
        project: the Org Policy v2 API refuses user credentials that name none,
        and in stage 0 there is no project to name (audit #8). The answer is a
        v1 policy (`listPolicy`, `booleanPolicy`); `_values` reads it. Should
        Google ask for a quota project after all, a project that exists is
        asked again as its own quota project."""
        url, body = f"{CRM}/v1/{resource}:getEffectiveOrgPolicy", {"constraint": f"constraints/{constraint}"}
        try:
            return self._call("POST", url, body=body)
        except ApiError as exc:
            if not (exc.status == 403 and "quota project" in str(exc).lower() and resource.startswith("projects/")):
                raise
            return self._call("POST", url, body=body, quota_project=resource.removeprefix("projects/"))

    def quota_metrics(self, project: str, service: str) -> list[dict[str, Any]]:
        return self._pages(
            f"{USAGE}/v1beta1/projects/{project}/services/{service}/consumerQuotaMetrics",
            "metrics", {"view": "BASIC"},
        )

    # Services ----------------------------------------------------------------

    def cpu_quotas(self, project: str, region: str) -> CpuQuotas:
        """The region's CPUS limit (Compute Engine `regions.get`) and the
        project's CPUS_ALL_REGIONS limit (`projects.get`), each None when it
        is not reported: an Autopilot node counts against both (audit 4a F5)."""
        regional = self._call("GET", f"{COMPUTE}/projects/{project}/regions/{region}") or {}
        project_wide = self._call("GET", f"{COMPUTE}/projects/{project}") or {}
        return CpuQuotas(_compute_limit(regional, "CPUS"), _compute_limit(project_wide, "CPUS_ALL_REGIONS"))

    def enabled_services(self, project: str) -> set[str]:
        found = self._pages(f"{USAGE}/v1/projects/{project}/services", "services",
                            {"filter": "state:ENABLED", "pageSize": 200})
        return {s["config"]["name"] for s in found}

    def enable_services(self, project: str, names: Sequence[str]) -> None:
        for start in range(0, len(names), 20):  # batchEnable takes at most 20
            self._call("POST", f"{USAGE}/v1/projects/{project}/services:batchEnable",
                       body={"serviceIds": list(names[start:start + 20])})

    def generate_service_identity(self, project: str, service: str) -> dict[str, Any]:
        return self._call(
            "POST", f"{USAGE}/v1beta1/projects/{project}/services/{service}:generateServiceIdentity",
            body={},
        )

    def service_account(self, email: str) -> dict[str, Any] | None:
        return self._call("GET", f"{IAM}/projects/-/serviceAccounts/{email}", missing_ok=True)

    # Storage -----------------------------------------------------------------

    def bucket(self, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{STORAGE}/b/{name}", missing_ok=True)

    def create_bucket(self, project: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{STORAGE}/b", params={"project": project}, body=body)

    def object_store(self, bucket: str) -> Any:
        from ..state import GcsObjectStore

        return GcsObjectStore(bucket)

    # Edge ----------------------------------------------------------------------

    def global_address(self, project: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{COMPUTE}/projects/{project}/global/addresses/{name}",
                          missing_ok=True)

    def create_global_address(self, project: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{COMPUTE}/projects/{project}/global/addresses", body=body)

    def ssl_certificate(self, project: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{COMPUTE}/projects/{project}/global/sslCertificates/{name}",
                          missing_ok=True)

    def iap_brands(self, project_number: str) -> list[dict[str, Any]]:
        found = self._call("GET", f"{IAP}/projects/{project_number}/brands")
        return list((found or {}).get("brands", []))

    def resolve(self, hostname: str) -> set[str]:
        """The IPv4 addresses `hostname` resolves to now (empty if none)."""
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        except socket.gaierror:
            return set()
        return {str(info[4][0]) for info in infos}

    # Secret Manager ----------------------------------------------------------

    def secret(self, project: str, secret_id: str) -> dict[str, Any] | None:
        return self._call("GET", f"{SECRET_MANAGER_API}/projects/{project}/secrets/{secret_id}", missing_ok=True)

    def create_secret(self, project: str, secret_id: str, labels: Mapping[str, str]) -> None:
        self._call("POST", f"{SECRET_MANAGER_API}/projects/{project}/secrets", params={"secretId": secret_id},
                   body={"replication": {"automatic": {}}, "labels": dict(labels)})

    def enabled_version(self, project: str, secret_id: str) -> int | None:
        found = self._pages(f"{SECRET_MANAGER_API}/projects/{project}/secrets/{secret_id}/versions",
                            "versions", {"filter": "state:ENABLED"})
        numbers = [int(v["name"].rsplit("/", 1)[1]) for v in found]
        return max(numbers) if numbers else None

    def access_secret_version(self, project: str, secret_id: str, version: int) -> bytes:
        """One version's payload, read with the person's own credentials: the
        Desktop client's secret the console saved, for this machine's swarm
        credential store (issue #2964). The caller never prints or keeps it."""
        found = self._call("GET", f"{SECRET_MANAGER_API}/projects/{project}/secrets/{secret_id}"
                                  f"/versions/{int(version)}:access")
        return base64.b64decode((found.get("payload") or {}).get("data") or "")

    def add_secret_version(self, project: str, secret_id: str, data: bytes) -> int:
        import google_crc32c  # a dependency of google-cloud-secret-manager

        found = self._call(
            "POST", f"{SECRET_MANAGER_API}/projects/{project}/secrets/{secret_id}:addVersion",
            body={"payload": {"data": base64.b64encode(data).decode(),
                              "dataCrc32c": str(google_crc32c.value(data))}},
        )
        return int(found["name"].rsplit("/", 1)[1])

    # The release home (anonymous) -------------------------------------------

    def read_public(self, url: str) -> bytes:
        import requests

        response = requests.get(url, timeout=60)
        if response.status_code >= 400:
            raise ApiError(response.status_code, "", f"GET {url}")
        return response.content


# -- what every step here shares --------------------------------------------

#: An `ask(ctx, step_id, question, default=None) -> str` (the terminal by default).
Ask = Callable[..., str]


def terminal_ask(open_terminal: Callable[[], Terminal] = DevTty.open) -> Ask:
    def _ask(ctx: Any, step_id: str, question: str, default: str | None = None) -> str:
        return secret_entry.ask(ctx, step_id, question, default=default, open_terminal=open_terminal)

    return _ask


def remember(ctx: Any, *, outputs: Mapping[str, Any] | None = None, **fields: Any) -> None:
    """Keep a non-secret answer: in memory on a dry run, in the state otherwise."""

    def change(state: Any) -> None:
        for name, value in fields.items():
            setattr(state, name, value)
        state.outputs.update(outputs or {})

    if ctx.dry_run:
        change(ctx.state)
    else:
        ctx.engine.save(change)


#: A typed confirmation that is empty or does not match is asked again, saying
#: what it expected, up to this many answers in all before the step fails:
#: enough for a slip of the finger or a stray Enter, not a loop that never ends
#: on a terminal that keeps answering the same thing (issue 2942).
CONFIRM_TRIES = 3


def confirm(ask: Ask, ctx: Any, step_id: str, question: str, expected: str, *,
            also: Sequence[str] = (), nothing: str = "nothing was recorded") -> str:
    """Ask `question` until `expected` is typed, at most `CONFIRM_TRIES` times,
    and return the answer that matched. `also` names the other answers the
    question takes, in any case (identity.groups-reader-role's `delegation`),
    returned as written there. An empty line is never an answer. Raises
    ValueError ending in `nothing` (what the caller left undone) when no
    answer matched."""
    others = {other.lower(): other for other in also}
    wanted = expected + "".join(f" or {other}" for other in also)
    asked = question
    again = ""
    for _ in range(CONFIRM_TRIES):
        again = ask(ctx, step_id, asked).strip()
        if again == expected:
            return expected
        if again.lower() in others:
            return others[again.lower()]
        said = "nothing was typed" if not again else f"{again!r} is not {wanted}"
        asked = f"{said}; type {wanted} exactly. {question}"
    raise ValueError(f"the confirmation {again!r} does not match {wanted!r} after {CONFIRM_TRIES} tries; "
                     + nothing)


def choose(ask: Ask, ctx: Any, step_id: str, what: str, options: Sequence[tuple[str, str]], *,
           pattern: re.Pattern[str], typed: str) -> str:
    """A numbered choice of `options` (value, label), read through the APIs.
    The number or the value itself is accepted. With nothing listed, the
    value is typed: `typed` says its form."""
    if not options:
        answer = ask(ctx, step_id, f"{what} ({typed})").strip()
    else:
        lines = [f"  {n}) {value}" + (f"  {label}" if label else "") for n, (value, label) in enumerate(options, 1)]
        answer = ask(ctx, step_id, f"{what}:\n" + "\n".join(lines) + f"\nNumber (1-{len(options)})",
                     default="1" if len(options) == 1 else None).strip()
        if answer.isdigit():
            if not 1 <= int(answer) <= len(options):
                raise ValueError(f"{answer} is not one of 1-{len(options)}: {what}")
            answer = options[int(answer) - 1][0]
    if not pattern.match(answer):
        raise ValueError(f"{answer!r} is not a valid answer to: {what} ({typed})")
    return answer


def project_number(gcp: Gcp, project: str) -> str:
    found = gcp.project(project)
    number = str((found or {}).get("name", "")).removeprefix("projects/")
    if not number:
        raise LookupError(f"project {project} does not exist yet (foundations.project creates it)")
    return number


class _Base:
    stage = 0
    kind = "engine"
    read_only = True
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask) -> None:
        self._gcp = gcp
        self._ask = ask

    @property
    def gcp(self) -> Gcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    def check(self, ctx: Any) -> Observation:  # pragma: no cover - every step overrides it
        raise TypeError(f"{type(self).__name__} has no check")


# -- preflight.tools (lesson 12) ---------------------------------------------

#: The pins (docs/versions.md; lesson 12). terraform is exact: 1.3.6 refuses
#: the state and a newer one diverges plans. kubectl is a floor: older clients
#: silently drop manifest fields (CLAUDE.md "Writing scripts").
TERRAFORM_VERSION = tools.TERRAFORM_VERSION
KUBECTL_MIN = (1, 30)
PYTHON_MIN = (3, 11)


@dataclass(frozen=True)
class Tool:
    entry: str
    name: str
    argv: tuple[str, ...]
    how: str
    exact: str | None = None
    minimum: tuple[int, int] | None = None
    env: str | None = None
    #: Pinned in `tools.PINS`: preflight fetches it instead of refusing.
    fetch: bool = False


_FETCHES = "the installer fetches the pinned one itself"

TOOLS: tuple[Tool, ...] = (
    Tool("local-tool.gcloud", "gcloud", ("version", "--format=value(core)"), "install the Google Cloud SDK"),
    Tool("local-tool.terraform", "terraform", ("version",), _FETCHES, exact=TERRAFORM_VERSION, fetch=True),
    Tool("local-tool.kubectl", "kubectl", ("version", "--client"), _FETCHES, minimum=KUBECTL_MIN,
         env="SWARM_KUBECTL", fetch=True),
    Tool("local-tool.jq", "jq", ("--version",), _FETCHES, fetch=True),
    Tool("local-tool.uv", "uv", ("--version",), "install uv (https://docs.astral.sh/uv/)"),
    #: Checked on `sys.executable` only: a stock macOS python3 (3.9) on PATH runs nothing here
    #: (render.py runs on `sys.executable`, steps/cluster.py), so it is never looked at or refused.
    Tool("local-tool.python", "python3", ("--version",), "run swarm install through install.sh (uv's Python)",
         minimum=PYTHON_MIN),
    Tool("local-tool.cosign", "cosign", ("version",), _FETCHES, fetch=True),
    Tool("local-tool.gcrane", "gcrane", ("version",), _FETCHES, fetch=True),
    #: kubectl execs it by name for a GKE context; the link in `tools.bin_dir()` is first on PATH.
    Tool("local-tool.gke-gcloud-auth-plugin", "gke-gcloud-auth-plugin", ("--version",), _FETCHES, fetch=True),
)

_VERSION = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")

#: The one optional choice preflight asks when gcloud is missing.
SDK_QUESTION = "Install the Google Cloud SDK into ~/.swarm/google-cloud-sdk? [Y/n]"
#: The SDK's install page, opened when the person declines the installer's copy.
SDK_PAGE = "https://cloud.google.com/sdk/docs/install"
#: What "no" stops with: exactly how to install it by hand.
SDK_HOW = (
    "the Google Cloud SDK (gcloud) is needed and was not installed. Install it from "
    f"{SDK_PAGE} and put its bin directory on PATH (or run swarm install "
    f"again and answer Y to install the pinned {tools.GCLOUD_VERSION} into ~/.swarm/google-cloud-sdk), "
    "then run swarm install again: it signs you in itself (your browser opens)"
)


def run_version(argv: Sequence[str]) -> str:
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)
    return (done.stdout or "") + (done.stderr or "")


def run_login(gcloud: str) -> int:
    """`gcloud auth login --update-adc` on the person's terminal: gcloud opens
    the browser and the person signs in (their credential step, never ours)."""
    return subprocess.run([gcloud, "auth", "login", "--update-adc"], check=False).returncode


def find_tool(tool: Tool) -> str | None:
    """Like common.sh's `prefer_local_bin`: the override, the pinned tools this
    installer fetched (`tools.bin_dir`), ~/.local/bin, then $PATH. Python is
    the interpreter running the installer (uv's managed one), never a prerequisite."""
    if tool.name == "python3":
        return sys.executable
    if tool.env and os.environ.get(tool.env):
        return os.environ[tool.env]
    for where in (tools.bin_dir(), Path("~/.local/bin").expanduser()):
        candidate = where / tool.name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(tool.name)


def tool_problem(tool: Tool, path: str | None, output: str) -> str | None:
    """Why this tool is not good enough, or None."""
    if path is None:
        return f"{tool.name} is not installed: {tool.how}"
    if tool.name == "kubectl":  # `Client Version: v1.36.1`; the server line is not ours
        match = re.search(r"Client Version:\s*v?(\d+)\.(\d+)(?:\.(\d+))?", output) or _VERSION.search(output)
    else:
        match = _VERSION.search(output)
    if tool.exact is None and tool.minimum is None:
        return None
    if match is None:
        return f"{tool.name} at {path} printed no version: {tool.how}"
    found = f"{match.group(1)}.{match.group(2)}" + (f".{match.group(3)}" if match.group(3) else "")
    if tool.exact is not None and found != tool.exact:
        return f"{tool.name} at {path} is {found}; {tool.exact} is pinned: {tool.how}"
    if tool.minimum is not None and (int(match.group(1)), int(match.group(2))) < tool.minimum:
        return f"{tool.name} at {path} is {found}; {'.'.join(map(str, tool.minimum))} or newer is required: {tool.how}"
    return None


class ToolsStep(_Base):
    """Every local tool the install runs. terraform, kubectl, jq, cosign and
    gcrane are fetched at their pins (SHA-256 checked) when missing or the
    wrong version, linked into `tools.bin_dir()` and that directory put first
    on the engine's PATH, so every later step and subprocess runs the pinned
    ones; a wrong one on the person's PATH is ignored and said once. A missing
    gcloud is one optional-choice question (DAYONE-INSTALL-TOOLS-AUTO)."""

    id = "preflight.tools"
    satisfies = tuple(t.entry for t in TOOLS)
    _explain = (
        "Every Terraform root is pinned to terraform 1.16.2 (Homebrew's 1.3.6 refused the state), and an "
        "old kubectl client silently drops manifest fields (lesson 12). The installer fetches the pinned "
        "tools itself, each checked against its pinned SHA-256."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 find: Callable[[Tool], str | None] = find_tool,
                 run: Callable[[Sequence[str]], str] = run_version,
                 ensure: Callable[[str], Path] = tools.ensure,
                 install_sdk: Callable[[], Path] = tools.install_sdk,
                 login: Callable[[str], int] = run_login,
                 bin_dir: Callable[[], Path] = tools.bin_dir,
                 environ: MutableMapping[str, str] | None = None) -> None:
        super().__init__(gcp, ask)
        self._find = find
        self._run = run
        self._ensure = ensure
        self._install_sdk = install_sdk
        self._login = login
        self._bin_dir = bin_dir
        self._environ = environ
        #: tool name -> the link to the pinned binary fetched in this run.
        self._fetched: dict[str, str] = {}

    @property
    def environ(self) -> MutableMapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def findings(self) -> list[tuple[Tool, str | None, str]]:
        """(tool, the path found, why it is not good enough) per unfit tool."""
        found = []
        for tool in TOOLS:
            if tool.name == "python3":  # the interpreter running this, never a python3 on PATH
                path, output = sys.executable, "Python {}.{}.{}".format(*sys.version_info[:3])
            else:
                path = self._fetched.get(tool.name) or self._find(tool)
                output = self._run([path, *tool.argv]) if path else ""
            problem = tool_problem(tool, path, output)
            if problem:
                found.append((tool, path, problem))
        return found

    def problems(self) -> list[str]:
        return [problem for _, _, problem in self.findings()]

    def check(self, ctx: Any) -> Observation:
        tools.use_bin_dir(self._bin_dir(), self.environ)
        problems = self.problems()
        return Observation(UNMET if problems else MET, "; ".join(problems))

    def plan(self, ctx: Any) -> Plan:
        actions = []
        for tool, path, problem in self.findings():
            if tool.fetch:
                ignored = f" ({problem.removesuffix(': ' + tool.how)}: ignored)" if path else ""
                actions.append(f"fetch the pinned {tool.name}, checked against its SHA-256{ignored}")
            elif tool.name == "gcloud":
                actions.append(f"ask: {SDK_QUESTION}")
            else:
                actions.append(problem)
        return Plan("fetch the pinned local tools", tuple(actions))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """Fetch what is pinned, ask about gcloud; anything else stays a
        problem the re-check names."""
        for tool, path, problem in self.findings():
            if tool.fetch:
                self._fetch(ctx, tool, path, problem)
            elif tool.name == "gcloud":
                self._gcloud(ctx)
        tools.use_bin_dir(self._bin_dir(), self.environ)

    def _fetch(self, ctx: Any, tool: Tool, path: str | None, problem: str) -> None:
        binary = self._ensure(tool.name)
        linked = tools.link(tool.name, binary, directory=self._bin_dir())
        self._fetched[tool.name] = str(linked)
        if tool.env and self.environ.get(tool.env):
            self.environ[tool.env] = str(linked)  # the override named the unfit one
        message = f"{tool.name}: fetched the pinned {binary.name}, SHA-256 checked ({linked})"
        if path:
            message = f"{problem.removesuffix(': ' + tool.how)}; ignoring it: {message}"
        ctx.events.emit("step_apply", step=self.id, message=message)

    def _gcloud(self, ctx: Any) -> None:
        answer = self._ask(ctx, self.id, SDK_QUESTION).strip().lower()
        if answer not in ("", "y", "yes"):
            open_page(SDK_PAGE, why="Install the Google Cloud SDK from its page, then run swarm install again:")
            raise RuntimeError(SDK_HOW)
        gcloud = self._install_sdk()
        linked = tools.link("gcloud", gcloud, directory=self._bin_dir())
        self._fetched["gcloud"] = str(linked)
        ctx.events.emit("step_apply", step=self.id, message=(
            f"installed the Google Cloud SDK {tools.GCLOUD_VERSION} into {gcloud.parent.parent}, SHA-256 checked; "
            "sign in now: gcloud opens your browser"))
        if self._login(str(linked)) != 0:
            raise RuntimeError("the Google sign-in did not finish; run swarm install again and it opens the "
                               "sign-in again")


# -- preflight.sign-in -----------------------------------------------------------


def find_gcloud() -> str | None:
    return find_tool(next(t for t in TOOLS if t.name == "gcloud"))


def gcloud_account(gcloud: Callable[[], str | None] = find_gcloud) -> str:
    """The account gcloud is signed in as, or "" when none is (or no gcloud)."""
    path = gcloud()
    if not path:
        return ""
    try:
        done = subprocess.run([path, "config", "get-value", "account"], capture_output=True, text=True,
                              timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    account = (done.stdout or "").strip()
    return account if done.returncode == 0 and "@" in account else ""


#: The scopes preflight's own application-default sign-in asks for: Cloud
#: Platform for the engine and terraform, and the two read-only Directory
#: scopes stage 6 (IDENTITY-STAGE) reads the Workspace customer and its admin
#: roles with (BGD, 2026-10-30, decision 8).
ADC_SCOPES = (
    CLOUD_PLATFORM,
    "https://www.googleapis.com/auth/admin.directory.customer.readonly",
    "https://www.googleapis.com/auth/admin.directory.rolemanagement",
)
#: What the engine and terraform cannot run without; the Directory scopes are
#: the rest, and stage 8 asks for them itself when Google refuses them here
#: (audit N14, DAYONE-DIR-SCOPES).
CLOUD_PLATFORM_SCOPE = ADC_SCOPES[0]
DIRECTORY_SCOPES = ADC_SCOPES[1:]
DIRECTORY_LATER = ("Google refused the Directory scopes to this sign-in, so it went on without them; stage 8 "
                   "asks for them through the deployment's own Desktop client when it needs them.")


def adc_login(scopes: Sequence[str] = ADC_SCOPES) -> list[str]:
    """The application-default sign-in. `--disable-quota-project`: gcloud would
    otherwise copy the laptop's own gcloud project into the credentials as
    their quota project, and terraform and every Google client would bill
    their calls to it (issue 2942). The install names its own project where
    a call needs one."""
    return ["auth", "application-default", "login", "--disable-quota-project", "--scopes=" + ",".join(scopes)]


def directory_declined(ctx: Any) -> bool:
    """Stage 0's sign-in went on without the Directory scopes (`directory_scopes`
    is false): stage 8 asks for them, and stage 0 never asks again."""
    return ctx is not None and ctx.outputs.get("directory_scopes") is False


def adc_scopes() -> frozenset[str] | None:
    """The scopes the application-default credentials' access token carries,
    or None when no credentials load. A token that cannot be read back is an
    empty set: the sign-in runs again rather than guessing.

    The credentials load without scopes: a user's refresh grant then asks for
    what was granted, where asking for `ADC_SCOPES` would make Google answer
    invalid_scope after a Cloud-Platform-only sign-in and hide that grant
    behind an empty set. Only credentials that must be scoped (a service
    account) are scoped to `ADC_SCOPES`."""
    import requests
    from google.auth.credentials import with_scopes_if_required
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
    from google.auth.transport.requests import Request

    try:
        credentials = application_default()
    except DefaultCredentialsError:
        return None
    credentials = with_scopes_if_required(credentials, list(ADC_SCOPES))
    try:
        credentials.refresh(Request())
        # POSTed, so the token is never in a URL a proxy or a log could keep.
        response = requests.post(TOKENINFO, data={"access_token": credentials.token}, timeout=30)
        granted = str(response.json().get("scope", "")) if response.status_code == 200 else ""
    except (GoogleAuthError, requests.RequestException, ValueError):
        return frozenset()
    return frozenset(granted.split())


#: How long the sign-in that asks the Directory scopes may wait on Google's
#: consent page. Google's "This app is blocked" page never redirects back to
#: gcloud, so gcloud would wait forever (audit 4a F2); past this the sign-in
#: goes on with Cloud Platform alone. Five minutes is ample for a password and
#: a second factor.
CONSENT_WAIT_S = 5 * 60
#: `run_auth`'s answer when gcloud was stopped at `timeout` (timeout(1)'s code).
TIMED_OUT = 124
CONSENT_HINT = ("If Google says this app is blocked, press Ctrl-C here (or wait "
                f"{CONSENT_WAIT_S // 60} minutes): the installer then signs in with Cloud Platform alone and "
                "asks for the Directory scopes later, through the deployment's own client.")


def run_auth(argv: Sequence[str], *, timeout: float | None = None) -> int:
    """One gcloud sign-in on the person's terminal: gcloud opens the browser.
    With `timeout`, gcloud is stopped once it has waited that long and the
    answer is `TIMED_OUT`."""
    try:
        return subprocess.run(list(argv), check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired:  # subprocess.run has already killed gcloud
        return TIMED_OUT


class SignInStep(_Base):
    """gcloud signed in (scripts and `gcloud` calls) and application-default
    credentials present (this engine's REST calls and terraform) carrying
    `ADC_SCOPES`, the Directory scopes stage 6 reads with beside Cloud
    Platform. When either is missing, or an existing ADC lacks a scope, the
    step runs the sign-in itself; each opens the browser, the person's
    credential step. It never prints a command for them to run.

    Google may refuse the Directory scopes to gcloud's own OAuth client (the
    browser says the app is blocked, or the token comes back without them,
    audit N14). The sign-in is then run again with Cloud Platform alone,
    `directory_scopes` is recorded false and one line says stage 8 asks for
    them later (`identity.directory_sign_in`); it is never a dead end. A
    sign-in that fails with Cloud Platform alone too still stops the run."""

    id = "preflight.sign-in"
    satisfies = ("other.installer-sign-in",)
    _explain = (
        "Every later step reads Google Cloud as the person: gcloud with their account and the engine and "
        "terraform with their application-default credentials. The installer runs both sign-ins itself."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 gcloud: Callable[[], str | None] = find_gcloud,
                 account: Callable[[], str] | None = None,
                 adc: Callable[[], frozenset[str] | None] = adc_scopes,
                 login: Callable[..., int] = run_auth) -> None:
        super().__init__(gcp, ask)
        self._gcloud = gcloud
        self._account = account or (lambda: gcloud_account(gcloud))
        self._adc = adc
        self._login = login

    def _findings(self, ctx: Any) -> tuple[list[tuple[str, list[str]]], frozenset[str] | None]:
        found = []
        if not self._account():
            found.append(("gcloud has no signed-in account", ["auth", "login"]))
        granted = self._adc()
        wanted = (CLOUD_PLATFORM_SCOPE,) if directory_declined(ctx) else ADC_SCOPES
        lacking = [scope.rsplit("/", 1)[1] for scope in wanted if scope not in (granted or ())]
        if granted is None:
            found.append(("the application-default credentials are missing", adc_login()))
        elif lacking:
            found.append((f"the application-default credentials lack the scopes {', '.join(lacking)}", adc_login()))
        return found, granted

    def missing(self, ctx: Any = None) -> list[tuple[str, list[str]]]:
        """(what is missing, the gcloud arguments that sign it in). The
        Directory scopes are not missing once stage 0 recorded that Google
        refused them (`directory_declined`)."""
        return self._findings(ctx)[0]

    def check(self, ctx: Any) -> Observation:
        missing, granted = self._findings(ctx)
        if missing:
            return Observation(UNMET, "; ".join(what for what, _ in missing))
        installer = str(ctx.outputs.get("installer") or "")
        if not installer:
            return Observation(UNMET, "the installer's account is not recorded yet")
        return Observation(MET, f"signed in as {installer}",
                           {"installer": installer, "directory_scopes": set(ADC_SCOPES) <= (granted or set())})

    def plan(self, ctx: Any) -> Plan:
        return Plan("sign in to Google Cloud (gcloud opens your browser)",
                    tuple(f"{what}: sign in in the browser" for what, _ in self.missing(ctx)))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """The missing sign-ins, then `installer`: the signed-in gcloud account,
        which bootstrap, IAP and the admin list read (audit #3). The first
        installer recorded is kept; preflight.organization replaces a consumer
        account with the organisation's."""
        self.sign_in(ctx, self.missing(ctx))
        account = self._account()
        if account and not ctx.outputs.get("installer"):
            remember(ctx, outputs={"installer": account})

    def sign_in(self, ctx: Any, missing: Sequence[tuple[str, list[str]]]) -> None:
        if not missing:
            return
        gcloud = self._gcloud()
        if not gcloud:
            raise RuntimeError("gcloud is not installed, so nobody can sign in; run swarm install again and "
                               "answer Y to install the Google Cloud SDK")
        for what, args in missing:
            ctx.events.emit("step_apply", step=self.id, message=f"{what}; signing in: your browser opens")
            if args == adc_login():
                if directory_declined(ctx):
                    # Google already refused the Directory scopes; stage 8 asks for them.
                    args = adc_login((CLOUD_PLATFORM_SCOPE,))
                else:
                    self._adc_sign_in(ctx, gcloud, what)
                    continue
            code = self._login([gcloud, *args])
            if code != 0:
                raise RuntimeError(f"the Google sign-in did not finish ({what}; gcloud exited {code}); "
                                   "run swarm install again to sign in")

    def _adc_sign_in(self, ctx: Any, gcloud: str, what: str) -> None:
        """The application-default sign-in with `ADC_SCOPES`; refused the
        Directory scopes (gcloud fails, or the token comes back without them),
        once more with Cloud Platform alone. `directory_scopes` records which.

        Google's "This app is blocked" page never returns to gcloud, so the
        scoped sign-in waits at most `CONSENT_WAIT_S`, and Ctrl-C during it
        stops gcloud alone, not the run (audit 4a F2). Either records
        `directory_scopes` false before the Cloud-Platform-only sign-in, so a
        run stopped during that one never asks the blocked scopes again."""
        ctx.events.emit("step_apply", step=self.id, message=CONSENT_HINT)
        try:
            code = self._login([gcloud, *adc_login()], timeout=CONSENT_WAIT_S)
        except KeyboardInterrupt:
            code = TIMED_OUT
        if code == TIMED_OUT:
            remember(ctx, outputs={"directory_scopes": False})
        granted = self._adc() if code == 0 else None
        if granted is not None and set(ADC_SCOPES) <= granted:
            remember(ctx, outputs={"directory_scopes": True})
            return
        code = self._login([gcloud, *adc_login((CLOUD_PLATFORM_SCOPE,))])
        if code != 0:
            raise RuntimeError(f"the Google sign-in did not finish ({what}; gcloud exited {code}); "
                               "run swarm install again to sign in")
        remember(ctx, outputs={"directory_scopes": False})
        ctx.events.emit("step_apply", step=self.id, message=DIRECTORY_LATER)

    def sign_in_again(self, ctx: Any) -> None:
        """Both sign-ins once more, as another account (preflight.organization)."""
        self.sign_in(ctx, [("signing in as your organisation's account", ["auth", "login"]),
                           ("the application-default credentials for that account", adc_login())])


# -- preflight.organization (audit #24) ------------------------------------------

#: Google's sign-up for Cloud Identity Free: an organisation for a domain, at no cost.
CLOUD_IDENTITY_SIGNUP = "https://workspace.google.com/signup/gcpidentity/welcome"
#: Consumer domains: `domain:gmail.com` in IAP would admit everyone with a Google account there.
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
NEEDS_ORGANISATION = (
    "SwarmCloud needs a Google organisation: Google Workspace, or the free Cloud Identity, on a domain you "
    "own. The project lives in it and its people sign in to the console with its accounts."
)
#: The sign-up, then the domain's verification record at its registrar.
ORGANISATION_WAIT = WaitSpec(
    reason="a Google organisation appearing (the Cloud Identity Free or Google Workspace sign-up)",
    expected="15 to 60 min (the domain is verified by a DNS record)",
    expected_s=60 * 60,
    bound_s=3 * 60 * 60,
    look_at=("the account swarm install signed in with must be in the new domain, not gmail.com; "
             "admin.google.com must show the domain verified; Google creates the organisation when that account "
             "first creates a billing account (console.cloud.google.com/billing/create) or a project; "
             "then swarm install goes on from here"),
    interval_s=30,
)
#: Google's page for a new Cloud Billing account (there is no API to create one).
BILLING_CREATE = "https://console.cloud.google.com/billing/create"
#: A person adding a billing account (a payment method) on that page.
BILLING_WAIT = WaitSpec(
    reason="a billing account appearing (the person creates one on the Cloud Billing page)",
    expected="5 to 15 min",
    expected_s=15 * 60,
    bound_s=60 * 60,
    look_at=("console.cloud.google.com/billing: the account must be open and created by the account swarm install "
             "signed in with, or shared with it as Billing Account User"),
    interval_s=15,
)


def consumer_account(account: str) -> bool:
    """No account, or one in a consumer domain: no organisation is ever its own."""
    return account.rpartition("@")[2].strip().lower() in CONSUMER_DOMAINS or "@" not in account


class OrganizationStep(_Base):
    """A Google organisation the signed-in account can see, said FIRST in
    stage 0, before any question (BGD, 2026-10-30: "Require an org, say so up
    front"). With none, the step says that SwarmCloud needs one, opens the
    Cloud Identity Free sign-up and polls (a wait: the person never types
    "done") until an organisation appears. A consumer account (gmail.com)
    never sees the new organisation, so it is signed in again first, as the
    organisation's account, and that account becomes the installer. There is
    no path that installs without an organisation."""

    id = "preflight.organization"
    kind = "wait"
    wait = ORGANISATION_WAIT
    _explain = (
        "The project lives in an organisation, IAP admits the organisation's domain and the console's people "
        "are its accounts; a personal Gmail account has none, so the installer says so before asking "
        "anything, opens the Cloud Identity Free sign-up and goes on once the organisation exists."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 account: Callable[[], str] = gcloud_account,
                 sign_in: Callable[[], None] | None = None) -> None:
        super().__init__(gcp, ask)
        self._account = account
        self._sign_in = sign_in

    def organizations(self, ctx: Any) -> list[str]:
        """The project's organisation when it exists in one; else every
        organisation (or folder, which lives in one) the account can see."""
        gcp = self.gcp
        if gcp.project(ctx.project) is not None:
            found = [r for r in gcp.ancestry(ctx.project) if r.startswith("organizations/")]
            if found:
                return found
        found = [str(o.get("name", "")) for o in gcp.organizations()]
        found = [name for name in found if PARENT.match(name)]
        return found or [str(f.get("name", "")) for f in gcp.folders() if PARENT.match(str(f.get("name", "")))]

    def check(self, ctx: Any) -> Observation:
        found = self.organizations(ctx)
        if found:
            return Observation(MET, f"organisation {', '.join(found[:3])}")
        account = self._account()
        who = account or "the signed-in account"
        if consumer_account(account):
            return Observation(UNMET, f"{NEEDS_ORGANISATION} {who} sees none yet")
        # organizations:search shows only what the caller holds a role on
        return Observation(UNMET, f"{NEEDS_ORGANISATION} {who} sees none yet: sign one up, or ask your "
                                  f"organisation's admin for a role on it. {who} must be in the new domain; "
                                  "Google creates the organisation when it first creates a billing account "
                                  "or a project")

    def plan(self, ctx: Any) -> Plan:
        return Plan("SwarmCloud needs a Google organisation: sign up for Cloud Identity Free (or Google "
                    "Workspace); the install goes on by itself once it exists",
                    ("sign up with a domain you own and verify it with the DNS record the sign-up shows",
                     "a personal account (gmail.com) signs in again as the new organisation's admin"))

    def apply(self, ctx: Any, plan: Plan) -> None:
        account = self._account()
        consumer = consumer_account(account)
        open_page(CLOUD_IDENTITY_SIGNUP, why=(
            f"{NEEDS_ORGANISATION} {account or 'The signed-in account'} has none. Sign up for Cloud Identity "
            "Free on this page with your domain and verify the domain with the DNS record it shows"
            + ("; then sign in as the new organisation's admin in the browser window that opens next."
               if consumer else "; this installer goes on by itself once the organisation exists.")))
        if not consumer:
            self.bring_into_being(ctx, account)
            return
        ctx.events.emit("step_apply", step=self.id, message=(
            f"{account or 'no account'} is not an organisation's account: sign in as the new organisation's "
            "admin once the sign-up is done"))
        if self._sign_in is not None:
            self._sign_in()
        else:
            SignInStep(self._gcp, self._ask).sign_in_again(ctx)
        self.gcp.reset_credentials()  # the wait's polls go as the account just signed in
        now = self._account()
        if now and not consumer_account(now):
            remember(ctx, outputs={"installer": now})
            self.bring_into_being(ctx, now)

    def bring_into_being(self, ctx: Any, account: str) -> None:
        """Audit N16 [plausible]: Google may create the organisation resource
        only when the domain's account first creates a billing account or a
        project, so `organizations:search` alone could poll for ever. Signed in
        as that account with no organisation and no billing account, the
        billing account creation page opens as well (stage 0 links that account
        next). Whichever way Google answers, the wait sees the organisation:
        at once when the sign-up made it, after the billing account when not."""
        if self.organizations(ctx):
            return
        try:
            accounts = self.gcp.billing_accounts()
        except ApiError:
            accounts = []
        if accounts:
            return
        open_page(BILLING_CREATE, why=(
            f"Signed in as {account}, create a billing account on this page: Google creates the organisation "
            f"for {account.rpartition('@')[2]} when its account first creates a billing account, and the "
            "install links this account to its project next. This installer goes on by itself."))


# -- preflight.inputs (R7) ---------------------------------------------------

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
BILLING_ACCOUNT = re.compile(r"^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$")
#: A GitHub login: letters, digits and single inner hyphens, at most 39.
GITHUB_LOGIN = re.compile(r"^(?=.{1,39}$)[A-Za-z0-9](?:-?[A-Za-z0-9])*$")

#: Each question stage 0 asks, the output it lands in and what a valid answer is.
#: The hostname is the verb's when it asked (the state's), and the billing
#: account is read when the project is already billed or chosen from the list.
QUESTIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("alert_email", "Email address that receives SwarmCloud's alerts (required: alerts that reach nobody "
                    "were lesson 41)", EMAIL),
    ("hostname", "Hostname the deployment will answer on (its A record is set in stage 2)", HOSTNAME),
    ("github_org", "GitHub organisation login the deployment serves (github.com/<login>)", GITHUB_LOGIN),
    ("billing_account", "Billing account to link the project to", BILLING_ACCOUNT),
)


class InputsStep(_Base):
    id = "preflight.inputs"
    satisfies = ("other.github-org",)
    _explain = (
        "Stage 0 asks everything later stages need and cannot read. The alert email is asked here, not at "
        "stage 13, because infra pass 2 writes the channel and require_alert_channels (plan R7, #1817)."
    )

    def missing(self, ctx: Any) -> list[str]:
        return [key for key, _, pattern in QUESTIONS if not pattern.match(str(self._known(ctx, key) or ""))]

    @staticmethod
    def _known(ctx: Any, key: str) -> Any:
        if key == "hostname" and not ctx.outputs.get(key):
            return ctx.state.hostname  # the verb's answer (`--domain` or its prompt)
        return ctx.outputs.get(key)

    def check(self, ctx: Any) -> Observation:
        missing = self.missing(ctx)
        return Observation(UNMET if missing else MET, f"not answered yet: {', '.join(missing)}" if missing else "",
                           {k: self._known(ctx, k) for k, _, _ in QUESTIONS if k not in missing})

    def plan(self, ctx: Any) -> Plan:
        missing = self.missing(ctx)
        return Plan("ask what the install cannot read", tuple(q for k, q, _ in QUESTIONS if k in missing))

    def apply(self, ctx: Any, plan: Plan) -> None:
        missing = self.missing(ctx)
        for key, question, pattern in QUESTIONS:
            if key in missing:
                answer = self._answer(ctx, key, question, pattern)
            elif not ctx.outputs.get(key):
                answer = str(self._known(ctx, key))  # read, not asked: kept with the answers
            else:
                continue
            extra = {"hostname": answer} if key == "hostname" else {}
            remember(ctx, outputs={key: answer}, **extra)

    def _answer(self, ctx: Any, key: str, question: str, pattern: re.Pattern[str]) -> str:
        if key == "billing_account":
            return self._billing_account(ctx, question)
        answer = self._ask(ctx, self.id, question).strip()
        if key == "hostname":
            answer = answer.lower().rstrip(".")
        if not pattern.match(answer):
            raise ValueError(f"{answer!r} is not a valid answer to: {question}")
        return answer

    def _billing_account(self, ctx: Any, question: str) -> str:
        """The project's own billing account when it is billed; else a numbered
        choice of the open accounts `billingAccounts.list` returns. With none
        listed (a brand-new organisation, audit N15), the billing account
        creation page opens and the list is polled until one appears; only a
        list that cannot be read at all asks for the id to be typed."""
        gcp = self.gcp
        if gcp.project(ctx.project) is not None:
            info = gcp.billing_info(ctx.project)
            linked = str(info.get("billingAccountName") or "").removeprefix("billingAccounts/")
            if info.get("billingEnabled") and BILLING_ACCOUNT.match(linked):
                return linked
        try:
            options = self._billing_options()
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=f"the billing accounts could not be listed ({exc})")
            options = []
        else:
            if not options:
                options = self._wait_for_billing(ctx)
        return choose(self._ask, ctx, self.id, question, options, pattern=BILLING_ACCOUNT,
                      typed="its id, XXXXXX-XXXXXX-XXXXXX")

    def _billing_options(self) -> list[tuple[str, str]]:
        accounts = self.gcp.billing_accounts()
        options = [(str(a.get("name", "")).removeprefix("billingAccounts/"), str(a.get("displayName") or ""))
                   for a in accounts]
        return [o for o in options if BILLING_ACCOUNT.match(o[0])]

    def _wait_for_billing(self, ctx: Any) -> list[tuple[str, str]]:
        """No billing account can be used: say so, open the page that creates
        one (Google has no API for it) and poll the list, never "press Enter"."""
        open_page(BILLING_CREATE, why=(
            "The signed-in account has no billing account the project can be linked to, so there is no "
            "billing account to choose. Create one on this page (it asks for a payment method); this installer goes on by itself once it appears."))
        found: list[tuple[str, str]] = []

        def listed() -> str:
            try:
                found[:] = self._billing_options()
            except ApiError:  # an unreadable poll has not seen it yet; the next one asks again
                return UNMET
            return MET if found else UNMET

        # Past its bound the WaitGaveUp goes through as it is (BGD, 2026-10-30): the
        # engine pauses the run on it (ENGINE-WAIT-PAUSE), and it names what to look at.
        wait_until(self.id + ":billing-account", BILLING_WAIT, listed, clock=ctx.clock, events=ctx.events,
                   on_poll=getattr(ctx.engine, "_heartbeat", None))
        return found


# -- preflight.region (lesson 2) ---------------------------------------------

DEFAULT_REGION = "us-central1"
REGION = re.compile(r"^[a-z]+-[a-z]+\d$")


class RegionStep(_Base):
    id = "preflight.region"
    _explain = (
        "Firestore's location can never change once the database exists (lesson 2), so the region is "
        "chosen and confirmed, typed twice, before stage 1 creates anything."
    )

    def check(self, ctx: Any) -> Observation:
        region = ctx.state.region
        if region and ctx.outputs.get("region_confirmed") == region:
            return Observation(MET, f"region {region}, confirmed")
        return Observation(UNMET, "the region is not confirmed")

    def plan(self, ctx: Any) -> Plan:
        return Plan("choose and confirm the region (permanent: the Firestore location)")

    def apply(self, ctx: Any, plan: Plan) -> None:
        """The verb's region (the state's) is never asked again, and never
        defaulted back to DEFAULT_REGION: only the typed confirmation is asked."""
        region = ctx.state.region or self._ask(
            ctx, self.id, "Region for every regional resource", default=DEFAULT_REGION).strip()
        if not REGION.match(region):
            raise ValueError(f"{region!r} is not a region name like {DEFAULT_REGION}")
        confirm(self._ask, ctx, self.id,
                f"The Firestore location can never change. Type the region again to confirm ({region})", region)
        remember(ctx, region=region, outputs={"region_confirmed": region})


# -- preflight.rights (lessons 1 and 16) ---------------------------------------

PROJECT_CREATE = "resourcemanager.projects.create"
BILLING_LINK = "billing.resourceAssociations.create"
#: IAP settings need project-level rights (lesson 16): a grant on a backend matched nothing.
IAP_SETTINGS = "iap.webServices.updateSettings"
PARENT = re.compile(r"^(organizations|folders)/[0-9]+$")


def holds(gcp: Gcp, resources: Iterable[str], permission: str) -> tuple[bool, list[str]]:
    """Whether `permission` is held on any of `resources`, which are tried in
    order (the project, then each folder, then the organization): a right
    inherited from a folder, the organization or a group counts (lesson 16).
    Returns the answer and every resource asked, so "missing" names them all."""
    asked: list[str] = []
    for resource in resources:
        asked.append(resource)
        if permission in gcp.test_permissions(resource, [permission]):
            return True, asked
    return False, asked


class RightsStep(_Base):
    id = "preflight.rights"
    satisfies = ("switch.project-create-right", "switch.billing-link-right")
    _explain = (
        "In September the owner could create a project but not link billing, and a project with no "
        "billing can enable no API (lesson 1). A right inherited from a folder, the organization or a "
        "group counts: every level is asked before a right is called missing (lesson 16)."
    )

    def _parents(self, ctx: Any) -> list[str]:
        parent = str(ctx.outputs.get("parent") or "")
        return [parent] if parent else []

    def findings(self, ctx: Any) -> tuple[list[str], dict[str, Any]]:
        gcp = self.gcp
        problems: list[str] = []
        outputs: dict[str, Any] = {}
        found = gcp.project(ctx.project)
        if found is None:
            parents = self._parents(ctx)
            if not parents:
                problems.append(
                    f"project {ctx.project} does not exist and no parent is known; preflight.rights asks "
                    "which organization or folder stage 1 creates it under"
                )
            else:
                ok, asked = holds(gcp, parents, PROJECT_CREATE)
                if not ok:
                    problems.append(f"you lack {PROJECT_CREATE} on {', '.join(asked)} (inherited and group "
                                    "bindings included): ask an organization admin for "
                                    "roles/resourcemanager.projectCreator")
            chain: list[str] = []
        else:
            chain = gcp.ancestry(ctx.project)
            outputs["project_number"] = str(found.get("name", "")).removeprefix("projects/")
            ok, asked = holds(gcp, chain, IAP_SETTINGS)
            if not ok:
                problems.append(f"you lack {IAP_SETTINGS} on {', '.join(asked)} (inherited and group bindings "
                                "included): IAP settings need a project-level grant of roles/iap.settingsAdmin, "
                                "a grant on a backend matches nothing (lesson 16)")
        billing_enabled = bool(found and gcp.billing_info(ctx.project).get("billingEnabled"))
        if not billing_enabled:
            account = str(ctx.outputs.get("billing_account") or "")
            if not account:
                problems.append("no billing account is named; answer preflight.inputs' billing question")
            elif BILLING_LINK not in gcp.test_permissions(f"billingAccounts/{account}", [BILLING_LINK]):
                problems.append(
                    f"you cannot link billing account {account}: you lack {BILLING_LINK} on it. Fix: ask its "
                    f"admin for roles/billing.user on billingAccounts/{account}, then run swarm install again"
                )
        return problems, outputs

    def check(self, ctx: Any) -> Observation:
        problems, outputs = self.findings(ctx)
        return Observation(UNMET if problems else MET, "; ".join(problems), outputs)

    def plan(self, ctx: Any) -> Plan:
        return Plan("confirm your rights to create the project and link its billing", tuple(self.findings(ctx)[0]))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """A right is granted by an admin, never by the installer: the plan names
        each fix. The one thing asked is where a new project will live."""
        gcp = self.gcp
        if ctx.outputs.get("parent") or gcp.project(ctx.project) is not None:
            return
        parent = choose(self._ask, ctx, self.id, f"Project {ctx.project} does not exist yet. Create it under",
                        self._creatable(ctx, gcp), pattern=PARENT, typed="organizations/N or folders/N")
        remember(ctx, outputs={"parent": parent})

    def _creatable(self, ctx: Any, gcp: Gcp) -> list[tuple[str, str]]:
        """Each organization, then each folder, the person can create a project in."""
        try:
            found = [*gcp.organizations(), *gcp.folders()]
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=f"the organizations and folders could not be "
                                                                 f"listed ({exc})")
            return []
        options = []
        for item in found:
            name = str(item.get("name", ""))
            if not PARENT.match(name):
                continue
            try:
                held = PROJECT_CREATE in gcp.test_permissions(name, [PROJECT_CREATE])
            except ApiError:
                held = False  # unreadable is not offered; the typed answer and the check still decide
            if held:
                options.append((name, str(item.get("displayName") or "")))
        return options


# -- preflight.org-domain ------------------------------------------------------

DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _domain_problem(domain: str) -> str | None:
    if not DOMAIN.match(domain):
        return f"{domain!r} is not a domain like acme.com"
    if domain in CONSUMER_DOMAINS:
        return (f"{domain} is a consumer domain, not your organization's: IAP admits every account in the "
                "domain it is given")
    return None


class OrgDomainStep(_Base):
    """`org_domain`, the Google Workspace or Cloud Identity primary domain IAP
    admits (`domain:<it>`) and swarm-api's ALLOWED_DOMAINS holds. Read from the
    organization's `displayName` (its primary domain); only when there is no
    organization to read is the signed-in account's domain offered, confirmed."""

    id = "preflight.org-domain"
    satisfies = ("other.org-domain",)
    _explain = (
        "IAP admits the organization's domain and swarm-api allows it; the domain is read from the "
        "organization the project lives in, which is its Workspace or Cloud Identity primary domain."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 account: Callable[[], str] = gcloud_account) -> None:
        super().__init__(gcp, ask)
        self._account = account

    def check(self, ctx: Any) -> Observation:
        domain = str(ctx.outputs.get("org_domain") or "")
        if domain and _domain_problem(domain) is None:
            return Observation(MET, f"organization domain {domain}", {"org_domain": domain})
        return Observation(UNMET, "the organization's domain is not recorded yet")

    def plan(self, ctx: Any) -> Plan:
        return Plan("read the organization's primary domain (IAP admits domain:<it>)")

    def organization(self, ctx: Any) -> str | None:
        """`organizations/N` the project lives (or will live) under, or None."""
        gcp = self.gcp
        parent = str(ctx.outputs.get("parent") or "")
        if not parent and gcp.project(ctx.project) is not None:
            return next((r for r in reversed(gcp.ancestry(ctx.project)) if r.startswith("organizations/")), None)
        for _ in range(10):  # folders nest at most 10 deep
            if parent.startswith("organizations/"):
                return parent
            if not parent.startswith("folders/"):
                return None
            parent = str((gcp.folder(parent) or {}).get("parent") or "")
        return None

    def apply(self, ctx: Any, plan: Plan) -> None:
        domain, source = "", ""
        try:
            organization = self.organization(ctx)
            if organization:
                domain = str((self.gcp.organization(organization) or {}).get("displayName") or "").lower()
                source = organization
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=f"the organization could not be read ({exc})")
        if not domain or _domain_problem(domain):
            domain, source = self._confirmed(ctx), "the signed-in account, confirmed"
        problem = _domain_problem(domain)
        if problem:
            raise ValueError(problem)
        ctx.events.emit("step_apply", step=self.id, message=f"organization domain {domain}, from {source}")
        remember(ctx, outputs={"org_domain": domain})

    def _confirmed(self, ctx: Any) -> str:
        guess = self._account().rpartition("@")[2].strip().lower()
        offered = guess if guess and _domain_problem(guess) is None else None
        return self._ask(ctx, self.id, "Your Google Workspace or Cloud Identity domain (IAP admits every "
                                       "account in it)", default=offered).strip().lower()


# -- preflight.installer-address -----------------------------------------------

#: Two independent HTTPS echoes of the caller's public IPv4; both must agree.
ADDRESS_ECHOES = ("https://api.ipify.org", "https://checkip.amazonaws.com")


#: What an echo of a public address can never be: RFC 1918, carrier-grade NAT, loopback, link-local.
_NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/3"))


def _public_ipv4(text: str) -> str | None:
    try:
        address = ipaddress.IPv4Address(text.strip())
    except ValueError:
        return None
    return None if any(address in network for network in _NOT_PUBLIC) else str(address)


class InstallerAddressStep(_Base):
    """`installer_cidr`, this machine's public IPv4 as `a.b.c.d/32`: the infra
    passes add it to the cluster's master authorized networks and a closing
    pass removes it. Two echoes must agree; when they do not, the person says
    which is theirs."""

    id = "preflight.installer-address"
    satisfies = ("other.installer-address",)
    _explain = (
        "The installer reaches the private cluster's control plane from this machine during the install, "
        "so its public address is authorized for the install's infra passes and removed afterwards."
    )

    def echoes(self) -> list[str | None]:
        found: list[str | None] = []
        for url in ADDRESS_ECHOES:
            try:
                found.append(_public_ipv4(self.gcp.read_public(url).decode("ascii", "replace")))
            except (ApiError, OSError):
                found.append(None)
        return found

    @staticmethod
    def agreed(echoes: Sequence[str | None]) -> str | None:
        return echoes[0] if echoes[0] is not None and len(set(echoes)) == 1 else None

    def check(self, ctx: Any) -> Observation:
        recorded = str(ctx.outputs.get("installer_cidr") or "")
        if not recorded:
            return Observation(UNMET, "the installer's address is not recorded yet")
        agreed = self.agreed(self.echoes())
        if agreed is not None and recorded != f"{agreed}/32":
            return Observation(UNMET, f"this machine's address is now {agreed}, not the recorded {recorded}")
        return Observation(MET, f"installer address {recorded}", {"installer_cidr": recorded})

    def plan(self, ctx: Any) -> Plan:
        return Plan("record this machine's public address (authorized on the cluster during the install)")

    def apply(self, ctx: Any, plan: Plan) -> None:
        echoes = self.echoes()
        address = self.agreed(echoes)
        if address is None:
            said = ", ".join(f"{url} answered {found or 'nothing usable'}" for url, found in zip(ADDRESS_ECHOES, echoes))
            typed = self._ask(ctx, self.id, f"The public-address echoes disagree ({said}). This machine's public "
                                            "IPv4 address").strip()
            address = _public_ipv4(typed)
            if address is None:
                raise ValueError(f"{typed!r} is not a public IPv4 address")
        remember(ctx, outputs={"installer_cidr": f"{address}/32"})


# -- preflight.org-policies ----------------------------------------------------

#: entry -> the constraint whose EFFECTIVE policy is read.
ORG_POLICIES: dict[str, str] = {
    "org-policy.resource-locations": "gcp.resourceLocations",
    "org-policy.domain-restricted-sharing": "iam.allowedPolicyMemberDomains",
    "org-policy.cmek": "gcp.restrictNonCmekServices",
    "org-policy.vm-external-ip-access": "compute.vmExternalIpAccess",
    "org-policy.automatic-iam-grants-default-sa": "iam.automaticIamGrantsForDefaultServiceAccounts",
}

#: Services this platform creates resources in; a CMEK constraint naming one blocks it.
CMEK_SENSITIVE = ("storage.googleapis.com", "secretmanager.googleapis.com", "firestore.googleapis.com",
                  "run.googleapis.com", "container.googleapis.com", "artifactregistry.googleapis.com")


def _values(policy: Mapping[str, Any], kind: str) -> list[str]:
    """`kind` (`allowedValues`, `deniedValues`) of a v1 policy's `listPolicy`,
    or of a v2 policy's rules."""
    found: list[str] = list(((policy.get("listPolicy") or {}).get(kind)) or [])
    for rule in (policy.get("spec") or {}).get("rules", []) or []:
        found.extend(((rule.get("values") or {}).get(kind)) or [])
    return found


def _location_admits(allowed: Sequence[str], region: str) -> bool:
    """`gcp.resourceLocations` values: `in:us-locations`, `in:us-central1-locations`, `us-central1`."""
    area = region.split("-")[0]
    group = {"europe": "eu"}.get(area, area)  # europe-west1 is in `eu-locations`
    admitted = {region, f"{region}-locations", f"{group}-locations"}
    return any(value.removeprefix("in:") in admitted for value in allowed)


class OrgPoliciesStep(_Base):
    id = "preflight.org-policies"
    satisfies = tuple(ORG_POLICIES)
    _explain = (
        "An organization constraint inherited from a folder or the org blocks the first apply with "
        "'Operation denied by org policy'; the EFFECTIVE policy (after inheritance) is read, not only the "
        "project's own."
    )

    def findings(self, ctx: Any) -> tuple[list[str], dict[str, Any]]:
        gcp = self.gcp
        if gcp.project(ctx.project) is not None:
            resource = f"projects/{ctx.project}"
        elif ctx.outputs.get("parent"):
            resource = str(ctx.outputs["parent"])  # the project inherits its parent's policy
        else:
            raise LookupError("the project does not exist and no parent is named (preflight.rights asks)")
        problems: list[str] = []
        region = ctx.state.region or DEFAULT_REGION
        for entry, constraint in ORG_POLICIES.items():
            policy = gcp.effective_policy(resource, constraint)
            if entry == "org-policy.resource-locations":
                allowed = _values(policy, "allowedValues")
                if allowed and not _location_admits(allowed, region):
                    problems.append(f"{constraint} admits {allowed}, not {region}: an organization admin must "
                                    f"allow {region} before any bucket, service or cluster is created")
            elif entry == "org-policy.cmek":
                denied = _values(policy, "deniedValues")
                blocked = sorted(set(denied) & set(CMEK_SENSITIVE))
                if blocked:
                    problems.append(f"{constraint} demands CMEK for {blocked}; this release creates Google-managed "
                                    "keys only: an organization admin must exempt the project")
        return problems, {"org_policies_read_on": resource}

    def check(self, ctx: Any) -> Observation:
        problems, outputs = self.findings(ctx)
        return Observation(UNMET if problems else MET, "; ".join(problems), outputs)

    def plan(self, ctx: Any) -> Plan:
        return Plan("org policies an admin must change", tuple(self.findings(ctx)[0]))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """An organization policy is an admin's to change: the plan names each one."""


# -- preflight.quotas (lesson 4) -------------------------------------------------


@dataclass(frozen=True)
class Quota:
    entry: str
    service: str
    metric: str  # a substring of the metric name, or its whole name when `exact`
    floor: int
    why: str
    exact: bool = False


#: The regional CPUS quota's metric, exactly (audit #32).
CPUS_METRIC = "compute.googleapis.com/cpus"
#: The project-wide CPUS_ALL_REGIONS quota's metric: every Autopilot node
#: counts against it as well as against the region's CPUS (audit 4a F5).
CPUS_ALL_REGIONS_METRIC = "compute.googleapis.com/cpus_all_regions"
#: The most vCPUs a Free Trial billing account runs at once across all
#: regions; Google grants such an account no quota increase until it is
#: upgraded to a paid account.
FREE_TRIAL_CPUS = 8

QUOTAS: tuple[Quota, ...] = (
    Quota("quota.kms-signing-h4", "cloudkms.googleapis.com", "crypto_requests", 600,
          "every step-spec signature is one KMS AsymmetricSign; running out is a 503 on every submission"),
    Quota("quota.cloud-run-jobs", "run.googleapis.com", "job", 1, "every dispatch is a Cloud Run Job"),
    # Exactly `compute.googleapis.com/cpus` (audit #32), not `a2_cpus` or `preemptible_cpus`;
    # the cluster's nodes also draw on the project-wide `cpus_all_regions`, read with it
    # (CPUS_ALL_REGIONS_METRIC, audit 4a F5), and the smaller binds. 24 is the floor before anyone says who the deployment is for; once a sizing is
    # recorded the floor is what it needs, if more (`cpus_floor`, DAYONE-SIZING-CAP).
    Quota("quota.compute-regional-cpus", "compute.googleapis.com", CPUS_METRIC, 24,
          "the Autopilot cluster's nodes", exact=True),
    Quota("quota.gke-regional", "container.googleapis.com", "clusters", 1, "one Autopilot cluster"),
    Quota("quota.nat-static-ips", "compute.googleapis.com", "static_addresses", 1, "Cloud NAT's address"),
)


def _limits(metrics: Iterable[Mapping[str, Any]], needle: str, region: str, *,
            exact: bool = False) -> list[tuple[str, int]]:
    """Each limit of the metrics `needle` names, as it applies in `region`: a
    limit's bucket for that region, else its bucket with no region (the
    default every other region gets). A bucket for another region never applies."""
    found = []
    for metric in metrics:
        name = str(metric.get("metric", ""))
        if (name != needle) if exact else (needle not in name):
            continue
        for limit in metric.get("consumerQuotaLimits", []) or []:
            buckets = [b for b in limit.get("quotaBuckets", []) or [] if b.get("effectiveLimit") is not None]
            ours = [b for b in buckets if (b.get("dimensions") or {}).get("region") == region]
            ours = ours or [b for b in buckets if "region" not in (b.get("dimensions") or {})]
            found.extend((f"{name} ({limit.get('unit', '')})", int(b["effectiveLimit"])) for b in ours)
    return found


#: A quota increase requested on the Quotas page: many are granted in minutes,
#: some are reviewed by Google for up to two business days (then the run pauses
#: and goes on from here).
QUOTA_WAIT = WaitSpec(
    reason="a quota increase being granted (requested on the console's Quotas page)",
    expected="a few minutes; a reviewed request up to 2 business days",
    expected_s=30 * 60,
    bound_s=4 * 60 * 60,
    look_at="the Quotas page's increase requests and the email Google sends about each one; "
            "swarm install goes on from here once it is granted",
    interval_s=60,
)


class QuotasStep(_Base):
    """Each quota below its floor opens the Quotas page, and the step waits
    (polling the quota with a countdown) until the increase is granted, so
    nobody runs swarm install again for it (audit #32)."""

    id = "preflight.quotas"
    kind = "wait"
    wait = QUOTA_WAIT
    satisfies = tuple(q.entry for q in QUOTAS)
    _explain = (
        "The Cloud KMS signing quota was never checked on the live deployment; if it runs out every "
        "submission fails with a 503 (lesson 4, contract request 34). Each quota is read, and a limit "
        "below what the platform needs is named before anything is created."
    )

    def binding(self, ctx: Any) -> tuple[list[tuple[Quota, str, str | None]], dict[str, Any]]:
        """Each quota below its floor in the install region, with what to say
        about it and the page to open on it (None: the Quotas page at large)."""
        gcp = self.gcp
        if gcp.project(ctx.project) is None:
            return [], {"quotas": "a new project has the default quotas; swarm doctor reads them after stage 1"}
        enabled = gcp.enabled_services(ctx.project)
        region = ctx.state.region or DEFAULT_REGION
        found: list[tuple[Quota, str, str | None]] = []
        read: dict[str, Any] = {}
        for quota in QUOTAS:
            if quota.service not in enabled:
                read[quota.entry] = f"{quota.service} is enabled in stage 1; the default quota applies"
                continue
            metrics = gcp.quota_metrics(ctx.project, quota.service)
            limits = _limits(metrics, quota.metric, region, exact=quota.exact)
            if quota.metric == CPUS_METRIC and limits:
                cpus = self._cpus(ctx, quota, limits, _limits(metrics, CPUS_ALL_REGIONS_METRIC, region, exact=True))
                read[quota.entry] = {name: value for name, value in limits + cpus[1]}
                if cpus[0] is not None:
                    found.append(cpus[0])
                continue
            if not limits:  # audit #32: said in one line, never raised
                read[quota.entry] = f"{quota.service} reports no {quota.metric} quota, so {quota.entry} is not checked"
                continue
            read[quota.entry] = {name: value for name, value in limits}
            bounded = [value for _, value in limits if value >= 0]  # -1 is unlimited
            lowest = min(bounded) if bounded else None
            floor, why = cpus_floor(ctx, quota) if quota.metric == CPUS_METRIC else (quota.floor, quota.why)
            if lowest is not None and lowest < floor:
                found.append((quota, f"{quota.entry}: {lowest} is below {floor} ({why}); request an "
                                     "increase on the console's Quotas page",
                              quotas_page(ctx.project, metric=quota.metric, region=region) if quota.exact else None))
        return found, {"quotas": read}

    def _cpus(self, ctx: Any, quota: Quota, regional: list[tuple[str, int]],
              project_wide: list[tuple[str, int]]) -> tuple[tuple[Quota, str, str | None] | None, list[tuple[str, int]]]:
        """The CPUS quota: the smaller of the region's CPUS and the project's
        CPUS_ALL_REGIONS below the floor, with the page that raises it (a
        Free Trial account's upgrade page when it is the project-wide cap of
        one), else None; and the project-wide limits read."""
        def lowest(limits: list[tuple[str, int]]) -> int | None:
            bounded = [value for _, value in limits if value >= 0]  # -1 is unlimited
            return min(bounded) if bounded else None

        quotas = CpuQuotas(lowest(regional), lowest(project_wide))
        floor, why = cpus_floor(ctx, quota)
        usable = quotas.usable
        if usable is None or usable < 0 or usable >= floor:
            return None, project_wide
        key, url, what = cpus_page(ctx, quotas, floor)
        name = "CPUS_ALL_REGIONS" if key != "region" else str(ctx.state.region or DEFAULT_REGION) + " CPUS"
        return (quota, f"{quota.entry}: {usable} is below {floor} ({why}; {name} is the smaller limit); {what}",
                url), project_wide

    def findings(self, ctx: Any) -> tuple[list[str], dict[str, Any]]:
        found, outputs = self.binding(ctx)
        return [said for _, said, _ in found], outputs

    def check(self, ctx: Any) -> Observation:
        problems, outputs = self.findings(ctx)
        if problems:
            return Observation(UNMET, "; ".join(problems), outputs)
        read = outputs["quotas"] if isinstance(outputs["quotas"], dict) else {}
        unread = [said for said in read.values() if isinstance(said, str) and " reports no " in said]
        return Observation(MET, "; ".join(unread), outputs)

    def plan(self, ctx: Any) -> Plan:
        return Plan("quota increases to request", tuple(self.findings(ctx)[0]))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """A quota increase is requested by a person on the Quotas page: the plan
        names each one, and the page opens, on the metric itself when the
        quota names one exactly (audit #32)."""
        if not plan.actions:
            return
        found, _ = self.binding(ctx)
        exact = [(said, url) for _, said, url in found if url]
        rest = [said for _, said, url in found if not url]
        for said, url in exact:
            open_page(url, why=f"Request this quota increase on the Quotas page:\n  {said}"
                      if url.startswith(QUOTAS_PAGE) else f"Do this on the page that opens:\n  {said}")
        if rest:
            open_page(quotas_page(ctx.project), why="Request these quota increases on the Quotas page:\n  "
                                                    + "\n  ".join(rest))


def _answered(ctx: Any) -> tuple[str, int] | None:
    """Who the recorded sizing is for, (mode, members), when one is recorded
    and says it plainly; else None."""
    found = ctx.outputs.get("sizing")
    if not isinstance(found, Mapping):
        return None
    mode, members = found.get("mode"), found.get("members")
    if mode not in sizing.MODES or isinstance(members, bool) or not isinstance(members, int) or members < 1:
        return None
    if mode == sizing.INDIVIDUAL and members != 1:
        return None
    return str(mode), members


def _who(mode: str, members: int) -> str:
    return "one person" if mode == sizing.INDIVIDUAL else f"a team of {members}"


def cpus_floor(ctx: Any, quota: Quota) -> tuple[int, str]:
    """The regional CPUS floor: the smallest quota the recorded sizing can use
    (`sizing.min_region_cpus`), never below the quota's own floor. Before a
    sizing is recorded, the quota's own floor."""
    answered = _answered(ctx)
    if answered is None:
        return quota.floor, quota.why
    need = sizing.min_region_cpus(*answered)
    if need <= quota.floor:
        return quota.floor, quota.why
    return need, (f"{quota.why}, and one task at once for each of the {answered[1]} workspaces of "
                  f"{_who(*answered)}: {sizing.SYSTEM_CPUS} for the cluster plus {sizing.CPU_PER_UNIT} per task")


# -- preflight.sizing (decision 7) -------------------------------------------------

WHO_QUESTION = "Who is this SwarmCloud for? [1] just me  [2] a team"
MEMBERS_QUESTION = "How many people will use it?"
REVIEW_QUESTION = "Press Enter to accept it, or type a row number (1-{rows}) to change that one number"
WHO = {"1": sizing.INDIVIDUAL, "just me": sizing.INDIVIDUAL, "me": sizing.INDIVIDUAL,
       sizing.INDIVIDUAL: sizing.INDIVIDUAL, "2": sizing.TEAM, "a team": sizing.TEAM, sizing.TEAM: sizing.TEAM}
#: A positive whole number, as typed.
POSITIVE = re.compile(r"^[1-9][0-9]{0,5}$")
#: An answer that is not one of the choices is asked again this many times.
TRIES = 3
#: Changes one review takes before it stops (a terminal that repeats itself).
MAX_CHANGES = 100


QUOTAS_PAGE = "https://console.cloud.google.com/iam-admin/quotas"


def quotas_page(project: str, *, metric: str | None = None, region: str | None = None) -> str:
    """The console's Quotas page, filtered to `metric` in `region` when named."""
    query = {"project": project, **({"metric": metric} if metric else {}), **({"location": region} if region else {})}
    return f"{QUOTAS_PAGE}?" + urllib.parse.urlencode(query)


def billing_page(ctx: Any) -> str:
    """The billing account's overview, where a Free Trial account is upgraded
    to a paid one ("Activate full account"); the billing list when no account
    is recorded."""
    account = str(ctx.outputs.get("billing_account") or "")
    path = f"/{account}" if BILLING_ACCOUNT.match(account) else ""
    return f"https://console.cloud.google.com/billing{path}?" + urllib.parse.urlencode({"project": ctx.project})


def cpus_page(ctx: Any, quotas: CpuQuotas, need: int) -> tuple[str, str, str]:
    """(which, the page, what to do there) for a usable CPU quota below `need`
    (audit 4a F5). A project-wide CPUS_ALL_REGIONS at a Free Trial account's
    cap is raised by upgrading the account, never on the Quotas page, where
    Google refuses the request; a larger CPUS_ALL_REGIONS that binds has its own
    Quotas page; else the region's CPUS."""
    region = str(ctx.state.region or DEFAULT_REGION)
    if quotas.all_regions_binds and int(quotas.all_regions or 0) <= FREE_TRIAL_CPUS:
        return "upgrade", billing_page(ctx), (
            f"the project may run only {quotas.all_regions} CPUs across all regions (CPUS_ALL_REGIONS), the cap "
            "of a Free Trial billing account, and Google grants a Free Trial account no quota increase: activate "
            "a paid account on this page (Activate full account); the quota is then read again")
    if quotas.all_regions_binds:
        return "all-regions", quotas_page(ctx.project, metric=CPUS_ALL_REGIONS_METRIC), (
            f"request a CPUS_ALL_REGIONS increase to at least {need} on this page (a Free Trial billing account "
            f"is granted none: upgrade it first at {billing_page(ctx)})")
    return "region", quotas_page(ctx.project, metric=CPUS_METRIC, region=region), (
        f"request a CPUS increase in {region} to at least {need} on this page")


class SizingStep(_Base):
    """`sizing`: who the deployment is for (one person or a team of N), turned
    into proposed pool ceilings, service maximums, Claude accounts and a monthly
    budget (`swarm_install.sizing.propose`), capped at the region's CPU quota
    when it can be read. The person accepts the proposal with Enter or changes
    any one number, each change validated. Recorded as `state.outputs["sizing"]`
    (the interface sheet's shape) and the quota it was checked against as
    `sizing_cpu_quota`."""

    id = "preflight.sizing"
    satisfies = ("other.deployment-sizing",)
    _explain = (
        "A deployment for one person and one for a team of fifty want very different ceilings; the "
        "installer proposes them from the answer, capped at the region's CPU quota, and the person accepts "
        "or changes any one number."
    )

    def check(self, ctx: Any) -> Observation:
        found = ctx.outputs.get("sizing")
        if not isinstance(found, Mapping):
            return Observation(UNMET, "who the deployment is for is not answered yet")
        wrong = sizing.problems(found, region_cpu_quota=checked_quota(ctx))
        if wrong:
            return Observation(UNMET, "; ".join(wrong))
        who = "one person" if found["mode"] == sizing.INDIVIDUAL else f"a team of {found['members']}"
        return Observation(MET, f"sized for {who}: global {found['pool_limits']['global']}, "
                                f"budget ${found['budget_usd']}/month", {"sizing": dict(found)})

    def plan(self, ctx: Any) -> Plan:
        return Plan("ask who the deployment is for and propose its capacity and ceilings", (WHO_QUESTION,))

    def apply(self, ctx: Any, plan: Plan) -> None:
        # An answer already recorded (a run paused on the CPUS quota, or a sizing a
        # later quota no longer takes) is kept: who it is for is never asked twice.
        answered = _answered(ctx)
        recorded = dict(ctx.outputs["sizing"]) if answered else None
        if answered is None:
            mode = self._until(ctx, WHO_QUESTION, lambda a: WHO.get(a.lower()), "type 1 or 2", default="1")
            members = 1 if mode == sizing.INDIVIDUAL else int(self._until(
                ctx, MEMBERS_QUESTION, lambda a: a if POSITIVE.match(a) else None, "type a whole number, 1 or more"))
        else:
            mode, members = answered
        quotas = self.read_quota(ctx)
        quota = quotas.usable if quotas else None
        waited = False
        if quotas and not sizing.fits_quota(mode, members, quota):
            recorded = recorded or dict(sizing.propose(mode, members))
            remember(ctx, outputs={"sizing": recorded,
                                   "sizing_cpu_quota": {"region": ctx.state.region, "cpus": quota}})
            quota = wait_for_cpus(ctx, self.gcp, self.id, mode, members, quotas)
            waited = True
        # The plain proposal recorded while waiting was never accepted: propose it
        # afresh within the quota. A sizing the person changed keeps their numbers.
        kept = recorded is not None and recorded != sizing.propose(mode, members)
        chosen = review(self._ask, ctx, self.id, mode, members, quota, offer_increase=not waited,
                        start=sizing.capped(recorded, quota) if kept and recorded else None)
        remember(ctx, outputs={"sizing": chosen,
                               "sizing_cpu_quota": {"region": ctx.state.region, "cpus": quota}})

    def _until(self, ctx: Any, question: str, parse: Callable[[str], Any], hint: str, *,
               default: str | None = None) -> Any:
        asked = question
        for _ in range(TRIES):
            answer = self._ask(ctx, self.id, asked, default=default).strip()
            value = parse(answer)
            if value is not None:
                return value
            asked = f"{answer!r} is not an answer ({hint}). {question}"
        raise ValueError(f"no valid answer to: {question} ({hint})")

    def read_quota(self, ctx: Any) -> CpuQuotas | None:
        """The region's CPUS and the project's CPUS_ALL_REGIONS quotas, or None
        (said) when they cannot be read yet."""
        region = ctx.state.region
        gcp = self.gcp
        if not region or gcp.project(ctx.project) is None:
            ctx.events.emit("step_apply", step=self.id, message=(
                f"project {ctx.project} does not exist yet, so the region's CPU quota cannot be read: the "
                "proposal is checked against it again after stage 1 creates the project"))
            return None
        try:
            quotas = gcp.cpu_quotas(ctx.project, region)
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=(
                f"the {region} CPU quota could not be read ({exc}): the proposal is checked against it again "
                "after stage 1 enables Compute Engine"))
            return None
        ctx.events.emit("step_apply", step=self.id, message=f"{region} CPU quota: {quotas.said(region)}")
        return quotas


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def checked_quota(ctx: Any) -> int | None:
    """The CPUS quota the recorded sizing was checked against, for this region."""
    found = ctx.outputs.get("sizing_cpu_quota")
    if isinstance(found, Mapping) and found.get("region") == ctx.state.region and isinstance(found.get("cpus"), int):
        return int(found["cpus"])
    return None


#: QUOTA_WAIT for a project held to a Free Trial account's CPUs: the account
#: is upgraded on its billing page, and only then can a quota be raised.
FREE_TRIAL_WAIT = WaitSpec(
    reason="the billing account being upgraded from its Free Trial to a paid account (on its billing page)",
    expected="a few minutes once the account is upgraded",
    expected_s=30 * 60,
    bound_s=4 * 60 * 60,
    look_at="the billing account's page (Activate full account); then, if the CPU quota is still too low, the "
            "Quotas page this installer opens; swarm install goes on from here once the CPUs are there",
    interval_s=60,
)


def wait_for_cpus(ctx: Any, gcp: Gcp, step_id: str, mode: str, members: int, quotas: CpuQuotas) -> int | None:
    """The usable CPU quota (the smaller of the region's CPUS and the
    project's CPUS_ALL_REGIONS, audit 4a F5) cannot run one task for each
    tenant, so no sizing can pass terraform's floor: the increase is
    REQUIRED. The page that raises the binding quota opens (`cpus_page`: a
    Free Trial account's upgrade page, else the Quotas page on that quota)
    and both are polled, like the engine's other waits, until the usable
    quota is at least `sizing.min_region_cpus`; a poll that finds another
    quota binding opens that one's page. Past the wait's bound the WaitGaveUp
    goes through and the engine pauses the run, which `swarm install` resumes
    here. Returns the granted usable quota."""
    region = str(ctx.state.region or DEFAULT_REGION)
    need = sizing.min_region_cpus(mode, members)
    tenants = sizing.provider_tenants(mode, members)
    opened: set[str] = set()

    def show(found: CpuQuotas) -> str:
        which, url, what = cpus_page(ctx, found, need)
        if which not in opened:
            opened.add(which)
            open_page(url, why=(
                f"The {found.usable} CPUs this project may use in {region} cannot run one task at once for each "
                f"of the {tenants} workspaces of {_who(mode, members)} ({sizing.SYSTEM_CPUS} CPUs are kept for the "
                f"cluster and each task takes {sizing.CPU_PER_UNIT}), and terraform refuses global below "
                f"{tenants} x provider_tenant. At least {need} usable CPUs are required: {what}. This installer "
                "goes on by itself once they are there."))
        return which

    first = show(quotas)
    seen: list[CpuQuotas] = [quotas]

    def granted() -> str:
        try:
            seen[0] = gcp.cpu_quotas(ctx.project, region)
        except ApiError:  # an unreadable poll has not seen it yet; the next one reads again
            return UNMET
        if sizing.fits_quota(mode, members, seen[0].usable):
            return MET
        show(seen[0])
        return UNMET

    wait_until(f"{step_id}:cpus-quota", FREE_TRIAL_WAIT if first == "upgrade" else QUOTA_WAIT, granted,
               clock=ctx.clock, events=ctx.events, on_poll=getattr(ctx.engine, "_heartbeat", None))
    ctx.events.emit("step_apply", step=step_id, message=f"{region} CPU quota: {seen[0].said(region)}")
    return seen[0].usable


def review(ask: Ask, ctx: Any, step_id: str, mode: str, members: int, quota: int | None, *,
           start: Mapping[str, Any] | None = None, offer_increase: bool = True) -> dict[str, Any]:
    """Show the proposal (or `start`) as a table; Enter accepts it, a row
    number changes that one number. A change that terraform or the region's
    quota would refuse is not taken, nor is Enter on a table they would
    refuse: the reason (which number breaks which rule) is said with it.
    `offer_increase` False: the Quotas page was just opened for a required
    increase (`wait_for_cpus`), so it is not opened again as an optional one."""
    current: dict[str, Any] = dict(start or sizing.propose(mode, members, region_cpu_quota=quota))
    uncapped = sizing.propose(mode, members)
    if offer_increase and quota is not None and current["pool_limits"]["global"] < uncapped["pool_limits"]["global"]:
        open_page(quotas_page(ctx.project), why=(
            f"The {quota} CPUs this project may use in the region cap this proposal at {current['pool_limits']['global']} tasks at once "
            f"(a {'team of ' + str(members) if mode == sizing.TEAM else 'person'} would get "
            f"{uncapped['pool_limits']['global']}). To run more, request a CPUS increase on the Quotas page "
            "(optional; the proposal stands either way)"))
    said = ""
    for _ in range(MAX_CHANGES):
        numbered = sizing.rows(current)
        answer = ask(ctx, step_id, f"{said}{sizing.table(current)}\n" + REVIEW_QUESTION.format(rows=len(numbered)),
                     default=None).strip()
        if not answer:
            wrong = sizing.problems(current, region_cpu_quota=quota)
            if not wrong:
                return current
            said = "Not accepted: " + "; ".join(wrong) + ". Type a row number to change that number.\n"
            continue
        if not answer.isdigit() or not 1 <= int(answer) <= len(numbered):
            said = f"{answer!r} is not a row number.\n"
            continue
        path, now = numbered[int(answer) - 1]
        typed = ask(ctx, step_id, f"New value for {sizing.label(path)} (now {now})", default=None).strip()
        if not POSITIVE.match(typed):
            said = f"Not changed: {typed!r} is not a whole number, 1 or more.\n"
            continue
        changed = sizing.with_change(current, path, int(typed))
        wrong = sizing.problems(changed, region_cpu_quota=quota)
        if wrong:
            said = "Not changed: " + "; ".join(wrong) + ".\n"
            continue
        current, said = changed, f"Changed {sizing.label(path)} to {typed}.\n"
    raise ValueError(f"the sizing was changed {MAX_CHANGES} times without being accepted; run swarm install again")


class SizingQuotaStep(SizingStep):
    """The proposal checked again against the usable CPU quota (the region's
    CPUS and the project's CPUS_ALL_REGIONS, the smaller binding) once stage 1
    has created the project and enabled Compute Engine: stage 0 could not read
    it for a project that did not exist yet. MET when the recorded sizing fits
    the quota; otherwise the capped proposal is shown and accepted or changed."""

    id = "preflight.sizing-quota"
    stage = 1
    read_only = False
    satisfies = ("other.deployment-sizing-quota",)
    _explain = (
        "A new project's CPU quota can only be read once stage 1 has created it; the sizing chosen in "
        "stage 0 is checked against it here, and a ceiling the quota cannot run is proposed again."
    )

    def check(self, ctx: Any) -> Observation:
        found = ctx.outputs.get("sizing")
        if not isinstance(found, Mapping):
            return Observation(UNMET, "no sizing is recorded (preflight.sizing asks it)")
        known = checked_quota(ctx)
        if known is None:
            if self.gcp.project(ctx.project) is None:
                return Observation(UNMET, f"project {ctx.project} does not exist yet (foundations.project creates it)")
            try:
                known = self.gcp.cpu_quotas(ctx.project, str(ctx.state.region)).usable
            except ApiError as exc:
                # BGD, 2026-10-30: one plain line, and the proposal stands.
                return Observation(MET, f"the {ctx.state.region} CPU quota could not be read "
                                        f"({_one_line(exc)}), so the proposed sizing is kept")
            if known is None:
                return Observation(MET, f"{ctx.state.region} reports no CPUS quota: nothing caps the sizing",
                                   {"sizing_cpu_quota": {"region": ctx.state.region, "cpus": None}})
        wrong = sizing.problems(found, region_cpu_quota=known)
        if wrong:
            return Observation(UNMET, "; ".join(wrong))
        return Observation(MET, f"the sizing fits the {ctx.state.region} CPU quota ({known})",
                           {"sizing_cpu_quota": {"region": ctx.state.region, "cpus": known}})

    def plan(self, ctx: Any) -> Plan:
        return Plan("propose the sizing again within the region's CPU quota")

    def apply(self, ctx: Any, plan: Plan) -> None:
        found = ctx.outputs["sizing"]
        try:
            quotas = self.gcp.cpu_quotas(ctx.project, str(ctx.state.region))
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=(
                f"the {ctx.state.region} CPU quota could not be read ({_one_line(exc)}), so the proposed "
                "sizing is kept"))
            return
        quota = quotas.usable
        mode, members = str(found["mode"]), int(found["members"])
        waited = not sizing.fits_quota(mode, members, quota)
        if waited:
            quota = wait_for_cpus(ctx, self.gcp, self.id, mode, members, quotas)
        chosen = review(self._ask, ctx, self.id, mode, members, quota, start=sizing.capped(found, quota),
                        offer_increase=not waited)
        remember(ctx, outputs={"sizing": chosen,
                               "sizing_cpu_quota": {"region": ctx.state.region, "cpus": quota}})


# -- preflight.release (lesson 27) -------------------------------------------------


@dataclass(frozen=True)
class Acceptance:
    cve: str
    package: str
    reason: str
    expires: dt.date

    def line(self, today: dt.date) -> str:
        state = "EXPIRED" if self.expires < today else f"until {self.expires.isoformat()}"
        return f"{self.cve} in {self.package}, accepted {state}: {self.reason}"


def parse_acceptances(data: bytes) -> list[Acceptance]:
    """The release's `cve-acceptances.json`: a list of
    `{cve, package, reason, expires: YYYY-MM-DD}`; an entry with no expiry is refused."""
    found = []
    for item in json.loads(data or b"[]"):
        if not item.get("expires"):
            raise ValueError(f"accepted {item.get('cve')} has no expiry; an acceptance always ends")
        found.append(Acceptance(str(item["cve"]), str(item.get("package", "")), str(item.get("reason", "")),
                                dt.date.fromisoformat(str(item["expires"]))))
    return found


def parse_sums(data: bytes) -> dict[str, str]:
    sums = {}
    for line in data.decode().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.strip().lstrip("*")] = digest
    return sums


#: The release channels install.sh reads (`SWARM_CHANNEL`, default stable).
CHANNELS = ("stable", "rc")
#: A wheel's PEP 440 version of a release name: 2026.1.2 is 2026.01.2, 2026.11.3rc2 is 2026.11.3-rc.2.
WHEEL_RELEASE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)(?:rc(\d+))?$")


def release_of_wheel(version: str) -> str | None:
    """The release name a wheel version was built from (install.sh's mapping,
    reversed), or None for a version no release carries (a source checkout's)."""
    match = WHEEL_RELEASE.match(version.strip())
    if not match:
        return None
    year, month, number, candidate = match.groups()
    name = f"{year}.{int(month):02d}.{int(number)}"
    return name if candidate is None else f"{name}-rc.{int(candidate)}"


def installed_release() -> str | None:
    """The release this engine was installed from: its own wheel's version."""
    from importlib import metadata

    try:
        return release_of_wheel(metadata.version("swarm-install"))
    except metadata.PackageNotFoundError:
        return None


def release_channel(environ: Mapping[str, str] = os.environ) -> str:
    """The channel install.sh used: `SWARM_CHANNEL`, `stable` when unset."""
    channel = environ.get("SWARM_CHANNEL") or "stable"
    if channel not in CHANNELS:
        raise ValueError(f"SWARM_CHANNEL={channel!r} is not a release channel: use stable (the default) or rc")
    return channel


def default_release_said(environ: Mapping[str, str] = os.environ) -> str:
    """What `swarm install` with no --version installs, in words, read offline.
    A channel that is not one is said as such, never as a release."""
    found = installed_release()
    if found:
        return found
    try:
        return f"the current {release_channel(environ)} release"
    except ValueError as error:
        return f"none: {error}"


class ReleaseStep(_Base):
    """The release every later stage reads (`state.release`: bootstrap,
    images, the cluster bundle). `--version` records it; with none, this step
    records it (audit #2, #30): the installed engine wheel's own release, so an
    rc CLI installs the rc it came as, else the release the channel install.sh
    used names. A release whose CVE acceptances fail is never recorded."""

    id = "preflight.release"
    satisfies = ("other.release-home", "other.release-bundle")
    _explain = (
        "A same-day CVE stopped every release four times in October. The installer never skips a scan: it "
        "shows what the release accepts and until when, and an expired acceptance stops the install "
        "(lesson 27). Its signature is verified with the release key before any image is copied (stage 4)."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.timezone.utc).date(),
                 installed: Callable[[], str | None] = installed_release,
                 environ: Mapping[str, str] = os.environ) -> None:
        super().__init__(gcp, ask)
        self._today = today
        self._installed = installed
        self._environ = environ

    def _version(self, ctx: Any) -> str:
        if ctx.state.release:
            return str(ctx.state.release)
        own = self._installed()
        if own:
            return own
        channel = release_channel(self._environ)
        return self.gcp.read_public(f"{RELEASE_HOME}/channels/{channel}").decode().strip()

    def findings(self, ctx: Any) -> tuple[list[str], dict[str, Any]]:
        gcp = self.gcp
        version = self._version(ctx)
        base = f"{RELEASE_HOME}/releases/{version}"
        sums = parse_sums(gcp.read_public(f"{base}/SHA256SUMS"))
        data = gcp.read_public(f"{base}/cve-acceptances.json")
        problems: list[str] = []
        if sums.get("cve-acceptances.json") != hashlib.sha256(data).hexdigest():
            problems.append(f"release {version}: cve-acceptances.json does not match SHA256SUMS")
        today = self._today()
        acceptances = parse_acceptances(data)
        lines = [a.line(today) for a in acceptances]
        expired = [a.cve for a in acceptances if a.expires < today]
        if expired:
            problems.append(f"release {version} accepts {', '.join(expired)} past its expiry; install a newer "
                            "release instead (the scan is never skipped)")
        return problems, {"release": version, "cve_acceptances": lines}

    def check(self, ctx: Any) -> Observation:
        problems, outputs = self.findings(ctx)
        if problems:
            return Observation(UNMET, "; ".join(problems), outputs)
        accepted = "accepted CVEs: " + ("; ".join(outputs["cve_acceptances"]) or "none")
        if not ctx.state.release:
            return Observation(UNMET, f"release {outputs['release']} is not recorded yet; {accepted}", outputs)
        return Observation(MET, f"release {outputs['release']}; {accepted}", outputs)

    def plan(self, ctx: Any) -> Plan:
        problems, outputs = self.findings(ctx)
        return Plan(f"release {outputs['release']}: the CVEs it accepts, each with its expiry",
                    tuple(outputs["cve_acceptances"]) + tuple(problems))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """The plan lists every acceptance and its expiry; a release that
        passes is recorded as `state.release` when nothing recorded one."""
        if ctx.state.release:
            return
        problems, outputs = self.findings(ctx)
        if not problems:
            remember(ctx, release=outputs["release"])


# -- the registry ----------------------------------------------------------------


@dataclass
class _Lazy:
    """One `Gcp` for every step, built on first use (never at import)."""

    made: list[Gcp] = field(default_factory=list)
    quota_project: str | None = None

    def __call__(self) -> Gcp:
        if not self.made:
            self.made.append(Gcp(quota_project=self.quota_project))
        return self.made[0]

    def use_quota_project(self, project: str) -> None:
        """The install's project, the quota project of its billing-budget calls (`Gcp`)."""
        self.quota_project = project
        for gcp in self.made:
            gcp.quota_project = project


GCP = _Lazy()
ASK = terminal_ask()


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK) -> list[Any]:
    """Stage 0 in order: tools, the sign-in, the questions, the region, then what they let us read."""
    return [
        ToolsStep(gcp, ask),
        SignInStep(gcp, ask),
        OrganizationStep(gcp, ask),  # said first: no question is asked of an account with no organisation
        InputsStep(gcp, ask),
        RegionStep(gcp, ask),
        RightsStep(gcp, ask),
        OrgDomainStep(gcp, ask),
        InstallerAddressStep(gcp, ask),
        OrgPoliciesStep(gcp, ask),
        QuotasStep(gcp, ask),
        SizingStep(gcp, ask),
        ReleaseStep(gcp, ask),
        SizingQuotaStep(gcp, ask),  # stage 1: the engine runs it after foundations creates the project
    ]


STEPS = build_steps()
