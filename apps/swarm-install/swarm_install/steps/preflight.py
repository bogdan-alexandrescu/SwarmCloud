"""Stage 0, preflight: nothing writes until the person's rights, the org's
policies, the quotas and the release are known (design §4, D14; plan §9.1).

Every step here declares `read_only = True`: its `apply` only asks and reads,
so `swarm install --dry-run` runs it (design §2.4) and a failed preflight
leaves the project exactly as it found it (lesson 1, B01: the fake records
zero mutating calls).

| Step | Manifest entries | Lesson |
|---|---|---|
| `preflight.tools` | the core `local-tool.*` | 12: terraform `1.16.2`, kubectl ≥ 1.30 |
| `preflight.sign-in` | `other.installer-sign-in` | gcloud and ADC signed in, by the installer itself |
| `preflight.inputs` | `other.github-org` | R7: the alert email, the hostname (the verb's), the GitHub org, the billing account (listed) |
| `preflight.region` | — | 2: the region (the verb's) is confirmed, typed, before stage 1 |
| `preflight.rights` | `switch.project-create-right`, `switch.billing-link-right` | 1 and 16; the parent is listed |
| `preflight.org-domain` | `other.org-domain` | the domain IAP admits, read from the organization |
| `preflight.installer-address` | `other.installer-address` | the installer's /32 for the cluster's authorized networks |
| `preflight.org-policies` | `org-policy.*` | read **effective** (inherited) policy |
| `preflight.quotas` | `quota.*` | 4: the KMS signing quota is read |
| `preflight.release` | `other.release-home`, `other.release-bundle` | 27: CVE acceptances with expiry |

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
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import secret_entry, tools
from ..exitcodes import MET, UNMET
from ..secret_entry import DevTty, Terminal
from . import Observation, Plan

# -- the REST client ---------------------------------------------------------

CRM = "https://cloudresourcemanager.googleapis.com"
BILLING = "https://cloudbilling.googleapis.com/v1"
BUDGETS = "https://billingbudgets.googleapis.com/v1"
ORGPOLICY = "https://orgpolicy.googleapis.com/v2"
USAGE = "https://serviceusage.googleapis.com"
STORAGE = "https://storage.googleapis.com/storage/v1"
IAM = "https://iam.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
IAP = "https://iap.googleapis.com/v1"
SECRET_MANAGER_API = "https://secretmanager.googleapis.com/v1"

#: The release home (D25, `other.release-home`): public, read anonymously.
RELEASE_HOME = "https://storage.googleapis.com/swarmcloud"


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
    application-default credentials."""

    def __init__(self, session: Any = None) -> None:
        self._session = session

    @property
    def session(self) -> Any:
        if self._session is None:
            import google.auth  # lazy: no credentials needed to import the engine
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._session = AuthorizedSession(credentials)
        return self._session

    def _call(self, method: str, url: str, *, body: Any = None, params: Any = None,
              missing_ok: bool = False) -> Any:
        response = self.session.request(method, url, json=body, params=params, timeout=60)
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

    def _pages(self, url: str, key: str, params: dict[str, Any] | None = None) -> list[Any]:
        found: list[Any] = []
        params = dict(params or {})
        while True:
            page = self._call("GET", url, params=params) or {}
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
        return self._pages(f"{BUDGETS}/billingAccounts/{account}/budgets", "budgets")

    def create_budget(self, account: str, budget: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", f"{BUDGETS}/billingAccounts/{account}/budgets", body=budget)

    # Org policy and quotas ---------------------------------------------------

    def effective_policy(self, resource: str, constraint: str) -> dict[str, Any]:
        """The policy in force on `resource` (`projects/..`, `folders/..`,
        `organizations/..`) after inheritance, not only its own."""
        return self._call("GET", f"{ORGPOLICY}/{resource}/policies/{constraint}:getEffectivePolicy")

    def quota_metrics(self, project: str, service: str) -> list[dict[str, Any]]:
        return self._pages(
            f"{USAGE}/v1beta1/projects/{project}/services/{service}/consumerQuotaMetrics",
            "metrics", {"view": "BASIC"},
        )

    # Services ----------------------------------------------------------------

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
#: What "no" stops with: exactly how to install it by hand.
SDK_HOW = (
    "the Google Cloud SDK (gcloud) is needed and was not installed. Install it from "
    "https://cloud.google.com/sdk/docs/install and put its bin directory on PATH (or run swarm install "
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


def adc_ready() -> bool:
    """Whether application-default credentials load (what `Gcp` and terraform use)."""
    import google.auth  # lazy: no credentials needed to import the engine
    from google.auth.exceptions import DefaultCredentialsError

    try:
        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except DefaultCredentialsError:
        return False
    return True


def run_auth(argv: Sequence[str]) -> int:
    """One gcloud sign-in on the person's terminal: gcloud opens the browser."""
    return subprocess.run(list(argv), check=False).returncode


class SignInStep(_Base):
    """gcloud signed in (scripts and `gcloud` calls) and application-default
    credentials present (this engine's REST calls and terraform). When either
    is missing the step runs the sign-in itself; each opens the browser, the
    person's credential step. It never prints a command for them to run."""

    id = "preflight.sign-in"
    satisfies = ("other.installer-sign-in",)
    _explain = (
        "Every later step reads Google Cloud as the person: gcloud with their account and the engine and "
        "terraform with their application-default credentials. The installer runs both sign-ins itself."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 gcloud: Callable[[], str | None] = find_gcloud,
                 account: Callable[[], str] | None = None,
                 adc: Callable[[], bool] = adc_ready,
                 login: Callable[[Sequence[str]], int] = run_auth) -> None:
        super().__init__(gcp, ask)
        self._gcloud = gcloud
        self._account = account or (lambda: gcloud_account(gcloud))
        self._adc = adc
        self._login = login

    def missing(self) -> list[tuple[str, list[str]]]:
        """(what is missing, the gcloud arguments that sign it in)."""
        found = []
        if not self._account():
            found.append(("gcloud has no signed-in account", ["auth", "login"]))
        if not self._adc():
            found.append(("the application-default credentials are missing", ["auth", "application-default", "login"]))
        return found

    def check(self, ctx: Any) -> Observation:
        missing = self.missing()
        return Observation(UNMET if missing else MET, "; ".join(what for what, _ in missing))

    def plan(self, ctx: Any) -> Plan:
        return Plan("sign in to Google Cloud (gcloud opens your browser)",
                    tuple(f"{what}: sign in in the browser" for what, _ in self.missing()))

    def apply(self, ctx: Any, plan: Plan) -> None:
        missing = self.missing()
        if not missing:
            return
        gcloud = self._gcloud()
        if not gcloud:
            raise RuntimeError("gcloud is not installed, so nobody can sign in; run swarm install again and "
                               "answer Y to install the Google Cloud SDK")
        for what, args in missing:
            ctx.events.emit("step_apply", step=self.id, message=f"{what}; signing in: your browser opens")
            code = self._login([gcloud, *args])
            if code != 0:
                raise RuntimeError(f"the Google sign-in did not finish ({what}; gcloud exited {code}); "
                                   "run swarm install again to sign in")


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
        choice of the open accounts `billingAccounts.list` returns."""
        gcp = self.gcp
        if gcp.project(ctx.project) is not None:
            info = gcp.billing_info(ctx.project)
            linked = str(info.get("billingAccountName") or "").removeprefix("billingAccounts/")
            if info.get("billingEnabled") and BILLING_ACCOUNT.match(linked):
                return linked
        try:
            accounts = gcp.billing_accounts()
        except ApiError as exc:
            ctx.events.emit("step_apply", step=self.id, message=f"the billing accounts could not be listed ({exc})")
            accounts = []
        options = [(str(a.get("name", "")).removeprefix("billingAccounts/"), str(a.get("displayName") or ""))
                   for a in accounts]
        options = [o for o in options if BILLING_ACCOUNT.match(o[0])]
        return choose(self._ask, ctx, self.id, question, options, pattern=BILLING_ACCOUNT,
                      typed="its id, XXXXXX-XXXXXX-XXXXXX")


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
        again = self._ask(
            ctx, self.id,
            f"The Firestore location can never change. Type the region again to confirm ({region})",
        ).strip()
        if again != region:
            raise ValueError(f"the confirmation {again!r} does not match {region!r}; nothing was recorded")
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
#: Consumer domains: `domain:gmail.com` in IAP would admit everyone with a Google account there.
CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


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
    found: list[str] = []
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
    metric: str  # a substring of the metric name
    floor: int
    why: str


QUOTAS: tuple[Quota, ...] = (
    Quota("quota.kms-signing-h4", "cloudkms.googleapis.com", "crypto_requests", 600,
          "every step-spec signature is one KMS AsymmetricSign; running out is a 503 on every submission"),
    Quota("quota.cloud-run-jobs", "run.googleapis.com", "job", 1, "every dispatch is a Cloud Run Job"),
    Quota("quota.compute-regional-cpus", "compute.googleapis.com", "cpus", 24, "the Autopilot cluster's nodes"),
    Quota("quota.gke-regional", "container.googleapis.com", "clusters", 1, "one Autopilot cluster"),
    Quota("quota.nat-static-ips", "compute.googleapis.com", "static_addresses", 1, "Cloud NAT's address"),
)


def _limits(metrics: Iterable[Mapping[str, Any]], needle: str) -> list[tuple[str, int]]:
    found = []
    for metric in metrics:
        name = str(metric.get("metric", ""))
        if needle not in name:
            continue
        for limit in metric.get("consumerQuotaLimits", []) or []:
            for bucket in limit.get("quotaBuckets", []) or []:
                value = bucket.get("effectiveLimit")
                if value is not None:
                    found.append((f"{name} ({limit.get('unit', '')})", int(value)))
    return found


class QuotasStep(_Base):
    id = "preflight.quotas"
    satisfies = tuple(q.entry for q in QUOTAS)
    _explain = (
        "The Cloud KMS signing quota was never checked on the live deployment; if it runs out every "
        "submission fails with a 503 (lesson 4, contract request 34). Each quota is read, and a limit "
        "below what the platform needs is named before anything is created."
    )

    def findings(self, ctx: Any) -> tuple[list[str], dict[str, Any]]:
        gcp = self.gcp
        if gcp.project(ctx.project) is None:
            return [], {"quotas": "a new project has the default quotas; swarm doctor reads them after stage 1"}
        enabled = gcp.enabled_services(ctx.project)
        problems: list[str] = []
        read: dict[str, Any] = {}
        for quota in QUOTAS:
            if quota.service not in enabled:
                read[quota.entry] = f"{quota.service} is enabled in stage 1; the default quota applies"
                continue
            limits = _limits(gcp.quota_metrics(ctx.project, quota.service), quota.metric)
            if not limits:
                raise LookupError(f"{quota.service} reports no {quota.metric} quota; {quota.entry} could not be read")
            read[quota.entry] = {name: value for name, value in limits}
            bounded = [value for _, value in limits if value >= 0]  # -1 is unlimited
            lowest = min(bounded) if bounded else None
            if lowest is not None and lowest < quota.floor:
                problems.append(f"{quota.entry}: {lowest} is below {quota.floor} ({quota.why}); request an "
                                "increase on the console's Quotas page")
        return problems, {"quotas": read}

    def check(self, ctx: Any) -> Observation:
        problems, outputs = self.findings(ctx)
        return Observation(UNMET if problems else MET, "; ".join(problems), outputs)

    def plan(self, ctx: Any) -> Plan:
        return Plan("quota increases to request", tuple(self.findings(ctx)[0]))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """A quota increase is requested by a person on the Quotas page: the plan names each one."""


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


class ReleaseStep(_Base):
    id = "preflight.release"
    satisfies = ("other.release-home", "other.release-bundle")
    _explain = (
        "A same-day CVE stopped every release four times in October. The installer never skips a scan: it "
        "shows what the release accepts and until when, and an expired acceptance stops the install "
        "(lesson 27). Its signature is verified with the release key before any image is copied (stage 4)."
    )

    def __init__(self, gcp: Callable[[], Gcp], ask: Ask, *,
                 today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.timezone.utc).date()) -> None:
        super().__init__(gcp, ask)
        self._today = today

    def _version(self, ctx: Any) -> str:
        if ctx.state.release:
            return str(ctx.state.release)
        return self.gcp.read_public(f"{RELEASE_HOME}/channels/stable").decode().strip()

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
        detail = "; ".join(problems) or (
            f"release {outputs['release']}; accepted CVEs: " + ("; ".join(outputs["cve_acceptances"]) or "none")
        )
        return Observation(UNMET if problems else MET, detail, outputs)

    def plan(self, ctx: Any) -> Plan:
        problems, outputs = self.findings(ctx)
        return Plan(f"release {outputs['release']}: the CVEs it accepts, each with its expiry",
                    tuple(outputs["cve_acceptances"]) + tuple(problems))

    def apply(self, ctx: Any, plan: Plan) -> None:
        """Shown, never changed: the plan lists every acceptance and its expiry."""


# -- the registry ----------------------------------------------------------------


@dataclass
class _Lazy:
    """One `Gcp` for every step, built on first use (never at import)."""

    made: list[Gcp] = field(default_factory=list)

    def __call__(self) -> Gcp:
        if not self.made:
            self.made.append(Gcp())
        return self.made[0]


GCP = _Lazy()
ASK = terminal_ask()


def build_steps(gcp: Callable[[], Gcp] = GCP, ask: Ask = ASK) -> list[Any]:
    """Stage 0 in order: tools, the sign-in, the questions, the region, then what they let us read."""
    return [
        ToolsStep(gcp, ask),
        SignInStep(gcp, ask),
        InputsStep(gcp, ask),
        RegionStep(gcp, ask),
        RightsStep(gcp, ask),
        OrgDomainStep(gcp, ask),
        InstallerAddressStep(gcp, ask),
        OrgPoliciesStep(gcp, ask),
        QuotasStep(gcp, ask),
        ReleaseStep(gcp, ask),
    ]


STEPS = build_steps()
