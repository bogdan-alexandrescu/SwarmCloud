"""Stage 0, preflight (DAYONE-ENGINE-2; plan §1.3, lessons 1, 2, 4, 12, 16, 27, R7).

On a fake Google Cloud with no credentials: a missing right to link billing
stops the install before stage 1 with the fix named and no write made (B01);
the tool pins are terraform 1.16.2 exactly and kubectl 1.30 or newer; the
region is typed twice before anything is created; the KMS signing quota and
the effective org policies are read; the alert email is asked; a right held on
a folder or the organization counts; the release's CVE acceptances are shown
with their expiry.

`FakeGcp` and the helpers here are shared by test_foundations.py,
test_edge.py and test_secret_entry.py.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_install.engine import Engine
from swarm_install.events import Events
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.lease import Lease
from swarm_install.manifest import load as load_manifest
from swarm_install.state import InstallState, StateStore
from swarm_install import sizing
from swarm_install.steps import foundations, preflight
from swarm_install.steps.preflight import RELEASE_HOME, ApiError
from swarm_install.waits import WaitGaveUp

from .test_state_store import FakeBucket, FakeClock

PROJECT = "p-test"
NUMBER = "123456789012"
ORG = "organizations/4242"
FOLDER = "folders/77"
ACCOUNT = "01ABCD-23EF45-6789AB"
OTHER_ACCOUNT = "0FEDCB-A98765-43210F"
GITHUB_ORG = "acme-eng"
INSTALLER_IP = "198.51.100.7"

MUTATING = (
    "set_iam_policy", "create_budget", "enable_services", "generate_service_identity", "create_bucket",
    "create_global_address", "create_secret", "add_secret_version",
)


class FakeGcp:
    """`preflight.Gcp` on dictionaries. Every call is logged in `calls` (and in
    `log`, shared with the fake asker, for ordering); a mutating call is also
    in `mutations`."""

    def __init__(self, log: list[Any] | None = None, *, exists: bool = True, billed: bool = True) -> None:
        self.log = log if log is not None else []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.mutations: list[tuple[str, tuple[Any, ...]]] = []
        self.projects: dict[str, dict[str, Any]] = (
            {PROJECT: {"name": f"projects/{NUMBER}", "state": "ACTIVE", "parent": ORG}} if exists else {}
        )
        self.chain = [f"projects/{PROJECT}", FOLDER, ORG]
        self.perms: dict[str, set[str]] = {
            f"projects/{PROJECT}": {preflight.IAP_SETTINGS},
            ORG: {preflight.PROJECT_CREATE},
            f"billingAccounts/{ACCOUNT}": {preflight.BILLING_LINK},
        }
        self.billing = {"billingEnabled": billed, "billingAccountName": f"billingAccounts/{ACCOUNT}" if billed else ""}
        self.policies: dict[str, dict[str, Any]] = {}
        self.policy_reads: list[str] = []
        self.quotas: dict[str, list[dict[str, Any]]] = {
            "cloudkms.googleapis.com": [_metric("cloudkms.googleapis.com/crypto_requests", 60_000)],
        }
        self.enabled: set[str] = {"cloudkms.googleapis.com"}
        self.budget_list: list[dict[str, Any]] = []
        self.iam: dict[str, Any] = {"etag": "e1", "bindings": []}
        self.set_iam_failures: list[ApiError] = []
        self.buckets: dict[str, dict[str, Any]] = {}
        self.stores: dict[str, FakeBucket] = {}
        self.service_accounts: set[str] = set()
        self.addresses: dict[str, dict[str, Any]] = {}
        self.certs: dict[str, dict[str, Any]] = {}
        self.brands: Callable[[], list[dict[str, Any]]] = lambda: [{"name": "b", "orgInternalOnly": True}]
        self.dns: dict[str, set[str]] = {}
        self.secrets: dict[str, list[bytes]] = {}
        self.release = release_files(dt.date(2026, 12, 31))
        self.release.update(echo_files(INSTALLER_IP))
        self.billing_account_list: list[dict[str, Any]] = [
            {"name": f"billingAccounts/{ACCOUNT}", "displayName": "Main", "open": True},
        ]
        self.orgs: dict[str, dict[str, Any]] = {ORG: {"name": ORG, "displayName": "example.com",
                                                     "directoryCustomerId": "C0abc12"}}
        self.folder_tree: dict[str, dict[str, Any]] = {FOLDER: {"name": FOLDER, "displayName": "eng", "parent": ORG}}
        self.org_failure: ApiError | None = None
        self.cpu_quota: int | None = 3000
        self.cpu_quota_failure: ApiError | None = None

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        self.log.append(("gcp", name))
        if name in MUTATING:
            self.mutations.append((name, args))

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]

    def reset_credentials(self):
        self._call("reset_credentials")

    # reads
    def project(self, project):
        self._call("project", project)
        return self.projects.get(project)

    def ancestry(self, project):
        self._call("ancestry", project)
        return list(self.chain)

    def test_permissions(self, resource, permissions):
        self._call("test_permissions", resource, tuple(permissions))
        return set(permissions) & self.perms.get(resource, set())

    def billing_info(self, project):
        self._call("billing_info", project)
        return dict(self.billing)

    def billing_accounts(self):
        self._call("billing_accounts")
        return list(self.billing_account_list)

    def organizations(self):
        self._call("organizations")
        return list(self.orgs.values())

    def folders(self):
        self._call("folders")
        return list(self.folder_tree.values())

    def organization(self, name):
        self._call("organization", name)
        if self.org_failure is not None:
            raise self.org_failure
        return self.orgs.get(name)

    def folder(self, name):
        self._call("folder", name)
        return self.folder_tree.get(name)

    def iam_policy(self, project):
        self._call("iam_policy", project)
        return json.loads(json.dumps(self.iam))

    def budgets(self, account):
        self._call("budgets", account)
        return list(self.budget_list)

    def effective_policy(self, resource, constraint):
        self._call("effective_policy", resource, constraint)
        self.policy_reads.append(f"{resource}:{constraint}")
        return self.policies.get(constraint, {})

    def quota_metrics(self, project, service):
        self._call("quota_metrics", project, service)
        return self.quotas.get(service, [])

    def region_cpu_quota(self, project, region):
        self._call("region_cpu_quota", project, region)
        if self.cpu_quota_failure is not None:
            raise self.cpu_quota_failure
        return self.cpu_quota

    def enabled_services(self, project):
        self._call("enabled_services", project)
        return set(self.enabled)

    def service_account(self, email):
        self._call("service_account", email)
        return {"email": email} if email in self.service_accounts else None

    def bucket(self, name):
        self._call("bucket", name)
        return self.buckets.get(name)

    def object_store(self, name):
        self._call("object_store", name)
        return self.stores.setdefault(name, FakeBucket())

    def global_address(self, project, name):
        self._call("global_address", project, name)
        return self.addresses.get(name)

    def ssl_certificate(self, project, name):
        self._call("ssl_certificate", project, name)
        return self.certs.get(name)

    def iap_brands(self, number):
        self._call("iap_brands", number)
        return self.brands()

    def resolve(self, hostname):
        self._call("resolve", hostname)
        return set(self.dns.get(hostname, set()))

    def secret(self, project, secret_id):
        self._call("secret", project, secret_id)
        return {"name": secret_id} if secret_id in self.secrets else None

    def enabled_version(self, project, secret_id):
        self._call("enabled_version", project, secret_id)
        return len(self.secrets.get(secret_id, [])) or None

    def read_public(self, url):
        self._call("read_public", url)
        if url not in self.release:
            raise ApiError(404, "", url)
        return self.release[url]

    # writes
    def set_iam_policy(self, project, policy):
        self._call("set_iam_policy", project, json.dumps(policy, sort_keys=True))
        if self.set_iam_failures:
            raise self.set_iam_failures.pop(0)
        self.iam = json.loads(json.dumps(policy))

    def create_budget(self, account, body):
        self._call("create_budget", account, body["displayName"])
        self.budget_list.append(dict(body))

    def enable_services(self, project, names):
        self._call("enable_services", project, tuple(names))
        self.enabled.update(names)

    def generate_service_identity(self, project, service):
        self._call("generate_service_identity", project, service)
        if service == "iap.googleapis.com":
            self.service_accounts.add(f"service-{NUMBER}@gcp-sa-iap.iam.gserviceaccount.com")

    def create_bucket(self, project, body):
        self._call("create_bucket", project, json.dumps(body, sort_keys=True))
        self.buckets[body["name"]] = json.loads(json.dumps(body))

    def create_global_address(self, project, body):
        self._call("create_global_address", project, body["name"])
        self.addresses[body["name"]] = {"address": "203.0.113.7", "status": "RESERVED"}

    def create_secret(self, project, secret_id, labels):
        self._call("create_secret", project, secret_id, tuple(sorted(labels.items())))
        self.secrets.setdefault(secret_id, [])

    def add_secret_version(self, project, secret_id, data):
        # The one place a typed value may appear: recorded here, nowhere else.
        self._call("add_secret_version", project, secret_id, "<value>")
        self.secrets.setdefault(secret_id, []).append(bytes(data))
        return len(self.secrets[secret_id])


def _metric(name: str, limit: int) -> dict[str, Any]:
    return {"metric": name, "consumerQuotaLimits": [{"unit": "1/min/{project}",
                                                     "quotaBuckets": [{"effectiveLimit": str(limit)}]}]}


def release_files(expires: dt.date, version: str = "v1.0.0") -> dict[str, bytes]:
    acceptances = json.dumps([{"cve": "CVE-2026-1111", "package": "openssl", "reason": "no fix upstream yet",
                               "expires": expires.isoformat()}]).encode()
    sums = f"{hashlib.sha256(acceptances).hexdigest()}  cve-acceptances.json\n".encode()
    base = f"{RELEASE_HOME}/releases/{version}"
    return {
        f"{RELEASE_HOME}/channels/stable": f"{version}\n".encode(),
        f"{base}/SHA256SUMS": sums,
        f"{base}/cve-acceptances.json": acceptances,
    }


def echo_files(*addresses: str) -> dict[str, bytes]:
    """What each public-address echo answers: one address for all, or one each."""
    echoes = preflight.ADDRESS_ECHOES
    chosen = addresses if len(addresses) == len(echoes) else addresses[:1] * len(echoes)
    return {url: f"{address}\n".encode() for url, address in zip(echoes, chosen)}


class Answers:
    """The terminal, faked: answers by step id, in order; every question logged."""

    def __init__(self, log: list[Any] | None = None, **by_step: list[str]) -> None:
        self.log = log if log is not None else []
        self.by_step = {self._step_id(k): list(v) for k, v in by_step.items()}
        self.asked: list[tuple[str, str]] = []
        self.defaults: list[str | None] = []

    @staticmethod
    def _step_id(key: str) -> str:
        stage, _, name = key.partition("_")  # preflight_inputs -> preflight.inputs
        return f"{stage}.{name.replace('_', '-')}"

    def __call__(self, ctx: Any, step_id: str, question: str, default: str | None = None) -> str:
        self.asked.append((step_id, question))
        self.defaults.append(default)
        self.log.append(("ask", step_id, question))
        queue = self.by_step.get(step_id) or []
        if not queue:
            raise AssertionError(f"{step_id} asked an unexpected question: {question}")
        return queue.pop(0) or (default or "")  # Enter takes the default, as secret_entry.ask does


def good_tools() -> dict[str, Any]:
    versions = {"terraform": "Terraform v1.16.2\non linux_amd64", "kubectl": "Client Version: v1.36.1",
                "python3": "Python 3.11.9"}
    return {
        "find": lambda tool: f"/usr/local/bin/{tool.name}",
        "run": lambda argv: versions.get(argv[0].rsplit("/", 1)[1], "1.0.0"),
    }


def answers(log: list[Any] | None = None, *, region: str = "us-central1", confirm: str | None = None,
            **extra: list[str]) -> Answers:
    by_step: dict[str, list[str]] = {
        "preflight_inputs": ["alerts@example.com", "swarm.example.com", GITHUB_ORG, ACCOUNT],
        "preflight_region": [region, confirm if confirm is not None else region],
        "preflight_sizing": ["1", ""],  # just me; Enter accepts the proposal
        "foundations_default_compute_editor": ["remove-editor"],
    }
    by_step.update(extra)
    return Answers(log, **by_step)


def stage0(gcp: FakeGcp, ask: Answers, *, tools: dict[str, Any] | None = None,
           today: dt.date = dt.date(2026, 10, 29), account: str = "admin@example.com",
           sign_in: Callable[[], None] | None = None) -> list[Any]:
    make = lambda: gcp  # noqa: E731
    return [
        preflight.ToolsStep(make, ask, **(tools or good_tools())),
        signed_in(make, ask),
        preflight.OrganizationStep(make, ask, account=lambda: account, sign_in=sign_in),
        preflight.InputsStep(make, ask),
        preflight.RegionStep(make, ask),
        preflight.RightsStep(make, ask),
        preflight.OrgDomainStep(make, ask, account=lambda: "admin@example.com"),
        preflight.InstallerAddressStep(make, ask),
        preflight.OrgPoliciesStep(make, ask),
        preflight.QuotasStep(make, ask),
        preflight.SizingStep(make, ask),
        preflight.ReleaseStep(make, ask, today=lambda: today, installed=lambda: None, environ={}),
    ]


def signed_in(make: Callable[[], Any], ask: Answers) -> Any:
    """`preflight.sign-in` on a machine already signed in: it never runs gcloud."""
    def no_login(argv: Any) -> int:
        raise AssertionError(f"signed in already, yet ran {argv}")

    return preflight.SignInStep(make, ask, gcloud=lambda: "/usr/local/bin/gcloud", account=lambda: "admin@example.com",
                                adc=lambda: frozenset(preflight.ADC_SCOPES), login=no_login)


def stage1(gcp: FakeGcp, ask: Answers) -> list[Any]:
    return foundations.build_steps(lambda: gcp, ask, load_manifest())


class Rig:
    """An engine over a fake bucket, a fake clock and JSON events in memory."""

    def __init__(self, tmp_path: Path, *, local_only: bool = False) -> None:
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.lease_store = FakeBucket()
        self.bucket = None if local_only else self.lease_store
        self.out = io.StringIO()
        self.events = Events(self.clock.now, json_lines=True, out=self.out)
        self.store = StateStore(project=PROJECT, environment="dev", local=tmp_path / "state.json",
                                bucket=self.bucket)

    def engine(self, steps: list[Any], *, manifest: Any = None, dry_run: bool = False) -> Engine:
        return Engine(
            steps, store=self.store,
            lease=Lease(holder="admin@example.com", store=self.lease_store, clock=self.clock.now),
            events=self.events, clock=self.clock, holder="admin@example.com", manifest=manifest, dry_run=dry_run,
        )

    def of_kind(self, kind: str, step: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.events.of_kind(kind) if step is None or e.get("step") == step]


def ctx_for(*, outputs: dict[str, Any] | None = None, region: str | None = "us-central1",
            hostname: str | None = None, dry_run: bool = True) -> Any:
    state = InstallState(project=PROJECT, environment="dev", region=region, hostname=hostname,
                         outputs=dict(outputs or {}))
    clock = FakeClock()
    return SimpleNamespace(project=PROJECT, environment="dev", state=state, outputs=state.outputs,
                           dry_run=dry_run, clock=clock, events=Events(clock.now, out=io.StringIO()), engine=None)


# -- the red-first tests -------------------------------------------------------


def test_missing_billing_link_right_stops_with_fix(tmp_path: Path) -> None:
    """B01 (lesson 1): the person can create the project but not link billing.
    The install stops at preflight, names the fix, and writes nothing."""
    gcp = FakeGcp(exists=False, billed=False)
    gcp.perms[f"billingAccounts/{ACCOUNT}"] = set()  # no billing.resourceAssociations.create
    ask = answers(preflight_rights=[ORG])
    rig = Rig(tmp_path)
    result = rig.engine(stage0(gcp, ask) + stage1(gcp, ask), manifest=load_manifest()).run()

    assert result.exit_code == ExitCode.FAIL
    assert result.step == "preflight.rights"
    assert preflight.BILLING_LINK in result.message
    assert f"roles/billing.user on billingAccounts/{ACCOUNT}" in result.message
    assert gcp.mutations == []
    assert not [e for e in rig.of_kind("step_apply") if str(e.get("step", "")).startswith("foundations.")]


def test_tool_versions_pinned() -> None:
    """Lesson 12: terraform is pinned exactly to 1.16.2; kubectl is 1.30 or newer."""
    assert preflight.TERRAFORM_VERSION == "1.16.2"
    assert preflight.KUBECTL_MIN == (1, 30)
    tools = {t.name: t for t in preflight.TOOLS}
    terraform, kubectl = tools["terraform"], tools["kubectl"]

    assert preflight.tool_problem(terraform, "/bin/terraform", "Terraform v1.16.2\n") is None
    for wrong in ("Terraform v1.3.6", "Terraform v1.16.3", "Terraform v1.17.0"):
        problem = preflight.tool_problem(terraform, "/bin/terraform", wrong)
        assert problem is not None and "1.16.2 is pinned" in problem
    assert "not installed" in str(preflight.tool_problem(terraform, None, ""))

    assert preflight.tool_problem(kubectl, "/bin/kubectl", "Client Version: v1.36.1\nServer Version: v1.20.0") is None
    assert preflight.tool_problem(kubectl, "/bin/kubectl", "Client Version: v1.30.0") is None
    for old in ("Client Version: v1.25.3\nServer Version: v1.36.0", "Client Version: v1.22.17"):
        problem = preflight.tool_problem(kubectl, "/bin/kubectl", old)
        assert problem is not None and "1.30 or newer" in problem

    old_terraform = dict(good_tools())
    old_terraform["run"] = lambda argv: "Terraform v1.3.6" if argv[0].endswith("terraform") else "Client Version: v1.36.1"
    step = preflight.ToolsStep(lambda: FakeGcp(), answers(), **old_terraform)
    observed = step.check(ctx_for())
    assert observed.status == UNMET and "1.16.2" in observed.detail
    assert preflight.ToolsStep(lambda: FakeGcp(), answers(), **good_tools()).check(ctx_for()).status == MET


def test_region_confirmed_before_stage_1(tmp_path: Path) -> None:
    """Lesson 2: the Firestore location is permanent, so the region is typed
    twice in stage 0, and stage 1 creates nothing before it is confirmed."""
    log: list[Any] = []
    gcp = FakeGcp(log)
    wrong = answers(log, region="europe-west1", confirm="europe-west2")
    rig = Rig(tmp_path)
    result = rig.engine(stage0(gcp, wrong) + stage1(gcp, wrong), manifest=load_manifest()).run()
    assert result.exit_code == ExitCode.FAIL and result.step == "preflight.region"
    assert gcp.mutations == []

    log.clear()
    gcp = FakeGcp(log)
    gcp.iam["bindings"] = [{"role": "roles/editor",
                            "members": [f"serviceAccount:{NUMBER}-compute@developer.gserviceaccount.com"]}]
    right = answers(log, region="europe-west1", foundations_budget=[""])
    rig = Rig(tmp_path / "second")
    result = rig.engine(stage0(gcp, right) + stage1(gcp, right), manifest=load_manifest()).run()
    assert result.exit_code == ExitCode.OK, result.message

    confirmed = max(i for i, item in enumerate(log) if item[:2] == ("ask", "preflight.region"))
    first_write = min(i for i, item in enumerate(log) if item[0] == "gcp" and item[1] in MUTATING)
    assert confirmed < first_write
    assert rig.store.load().region == "europe-west1"
    created = json.loads(gcp.called("create_bucket")[0][1])
    assert created["location"] == "europe-west1"


def test_kms_signing_quota_read() -> None:
    """Lesson 4: the Cloud KMS signing quota is read, and a low one is named."""
    gcp = FakeGcp()
    step = preflight.QuotasStep(lambda: gcp, answers())
    observed = step.check(ctx_for())
    assert observed.status == MET
    assert (PROJECT, "cloudkms.googleapis.com") in gcp.called("quota_metrics")
    assert observed.outputs["quotas"]["quota.kms-signing-h4"] == {
        "cloudkms.googleapis.com/crypto_requests (1/min/{project})": 60_000
    }

    gcp.quotas["cloudkms.googleapis.com"] = [_metric("cloudkms.googleapis.com/crypto_requests", 100)]
    low = step.check(ctx_for())
    assert low.status == UNMET and "quota.kms-signing-h4" in low.detail

    gcp.quotas["cloudkms.googleapis.com"] = []  # #32: not reported is said in one line, never raised
    unreported = step.check(ctx_for())
    assert unreported.status == MET and "reports no crypto_requests quota" in unreported.detail


def test_org_policies_read_effective() -> None:
    """The EFFECTIVE policy (after inheritance from folders and the org) is
    read for every org-policy entry, on the project, or on its parent before it exists."""
    gcp = FakeGcp()
    step = preflight.OrgPoliciesStep(lambda: gcp, answers())
    assert step.check(ctx_for()).status == MET
    assert sorted(gcp.policy_reads) == sorted(
        f"projects/{PROJECT}:{c}" for c in preflight.ORG_POLICIES.values()
    )
    assert set(step.satisfies) == {e.id for e in load_manifest().entries if e.id.startswith("org-policy.")}

    gcp.policies["gcp.resourceLocations"] = {"spec": {"rules": [{"values": {"allowedValues": ["in:eu-locations"]}}]}}
    blocked = step.check(ctx_for(region="us-central1"))
    assert blocked.status == UNMET and "gcp.resourceLocations" in blocked.detail
    assert step.check(ctx_for(region="europe-west1")).status == MET

    fresh = FakeGcp(exists=False)
    preflight.OrgPoliciesStep(lambda: fresh, answers()).check(ctx_for(outputs={"parent": FOLDER}))
    assert {r.split(":")[0] for r in fresh.policy_reads} == {FOLDER}


def test_alert_email_asked(tmp_path: Path) -> None:
    """R7: stage 0 asks the alert email (infra pass 2 writes the channel), and
    even a dry run asks it (preflight asks and reads)."""
    gcp = FakeGcp()
    ask = answers()
    rig = Rig(tmp_path)
    rig.engine(stage0(gcp, ask), dry_run=True).run()
    questions = [q for step, q in ask.asked if step == "preflight.inputs"]
    assert questions and "alert" in questions[0].lower()

    step = preflight.InputsStep(lambda: gcp, Answers(preflight_inputs=["not-an-email"]))
    ctx = ctx_for()
    assert step.check(ctx).status == UNMET
    with pytest.raises(ValueError):
        step.apply(ctx, step.plan(ctx))
    ctx = ctx_for()
    preflight.InputsStep(lambda: gcp, answers()).apply(ctx, step.plan(ctx))
    assert ctx.outputs["alert_email"] == "alerts@example.com"
    assert ctx.state.hostname == "swarm.example.com"


def test_inherited_bindings_checked_before_saying_missing() -> None:
    """Lesson 16: a right held on a folder or the organization (or through a
    group) counts; every level is asked before a right is called missing."""
    gcp = FakeGcp()
    gcp.perms = {ORG: {preflight.IAP_SETTINGS}}
    step = preflight.RightsStep(lambda: gcp, answers())
    observed = step.check(ctx_for(outputs={"billing_account": ACCOUNT}))
    assert observed.status == MET, observed.detail
    asked = [args[0] for args in gcp.called("test_permissions") if preflight.IAP_SETTINGS in args[1]]
    assert asked == [f"projects/{PROJECT}", FOLDER, ORG]

    gcp.perms = {}
    missing = step.check(ctx_for())
    assert missing.status == UNMET
    assert f"on projects/{PROJECT}, {FOLDER}, {ORG}" in missing.detail


def test_release_cve_acceptances_shown_with_expiry() -> None:
    """Lesson 27: the installer shows each accepted CVE and until when; an
    expired acceptance stops it, and the scan is never skipped."""
    gcp = FakeGcp()
    step = preflight.ReleaseStep(lambda: gcp, answers(), today=lambda: dt.date(2026, 10, 29))
    ctx = ctx_for()
    ctx.state.release = "v1.0.0"
    observed = step.check(ctx)
    assert observed.status == MET
    assert "CVE-2026-1111 in openssl, accepted until 2026-12-31" in observed.detail
    assert step.plan(ctx).actions == ("CVE-2026-1111 in openssl, accepted until 2026-12-31: no fix upstream yet",)

    gcp.release = release_files(dt.date(2026, 10, 1))
    expired = step.check(ctx)
    assert expired.status == UNMET and "CVE-2026-1111" in expired.detail and "expiry" in expired.detail

    with pytest.raises(ValueError):
        preflight.parse_acceptances(json.dumps([{"cve": "CVE-1", "package": "x"}]).encode())


# -- DAYONE-INSTALL-TOOLS-AUTO: preflight fetches the pinned tools ----------------

PINNED_OUTPUT = {
    "terraform": "Terraform v1.16.2\non linux_amd64",
    "kubectl": "Client Version: v1.36.6",
    "jq": "jq-1.8.2",
    "cosign": "GitVersion: v2.4.1",
    "gcrane": "0.20.2",
    "gcloud": "591.0.0",
    "gke-gcloud-auth-plugin": "Kubernetes v1.30.0+0.5.20",
}


class FetchingTools:
    """The machine, faked: what is on PATH, and a fake `tools.ensure` that
    records every fetch. Nothing touches the network or the person's home."""

    def __init__(self, tmp_path: Path, on_path: dict[str, str]) -> None:
        self.on_path = on_path  # name -> the `version` output of the one on PATH
        self.cache = tmp_path / "cache"
        self.bin = tmp_path / "bin"
        self.fetched: list[str] = []
        self.sdk_installs = 0
        self.logins: list[str] = []
        self.environ: dict[str, str] = {"PATH": "/usr/local/bin:/usr/bin"}

    def find(self, tool: Any) -> str | None:
        return f"/usr/local/bin/{tool.name}" if tool.name in self.on_path else None

    def run(self, argv: list[str]) -> str:
        path = Path(argv[0])
        if path.parent == self.bin:
            return PINNED_OUTPUT[path.name]
        name = path.name
        return self.on_path.get(name, "1.0.0")

    def ensure(self, name: str) -> Path:
        self.fetched.append(name)
        self.cache.mkdir(parents=True, exist_ok=True)
        binary = self.cache / f"{name}-pinned"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        return binary

    def install_sdk(self) -> Path:
        self.sdk_installs += 1
        gcloud = self.cache / "google-cloud-sdk" / "bin" / "gcloud"
        gcloud.parent.mkdir(parents=True, exist_ok=True)
        gcloud.write_text("#!/bin/sh\n")
        gcloud.chmod(0o755)
        return gcloud

    def login(self, gcloud: str) -> int:
        self.logins.append(gcloud)
        return 0

    def step(self, ask: Any) -> preflight.ToolsStep:
        return preflight.ToolsStep(lambda: FakeGcp(), ask, find=self.find, run=self.run, ensure=self.ensure,
                                   install_sdk=self.install_sdk, login=self.login, bin_dir=lambda: self.bin,
                                   environ=self.environ)


EVERY_GOOD = {"gcloud": "591.0.0", "terraform": "Terraform v1.16.2", "kubectl": "Client Version: v1.36.1",
              "jq": "jq-1.7.1", "uv": "uv 0.13.6", "python3": "Python 3.11.9", "cosign": "v2.4.1", "gcrane": "0.20.2",
              "gke-gcloud-auth-plugin": "Kubernetes v1.30.0+0.5.20"}


def _applied(step: preflight.ToolsStep, ctx: Any) -> Any:
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    return step.check(ctx)


@pytest.mark.parametrize("name", ["terraform", "kubectl", "jq", "cosign", "gcrane", "gke-gcloud-auth-plugin"])
def test_missing_tool_fetched_not_refused(tmp_path: Path, name: str) -> None:
    """A tool the release pins is fetched (SHA-256 checked by tools.ensure)
    instead of a refusal telling the person to install it."""
    machine = FetchingTools(tmp_path, {k: v for k, v in EVERY_GOOD.items() if k != name})
    step = machine.step(answers())
    ctx = ctx_for()
    detail = step.check(ctx).detail
    assert f"{name} is not installed" in detail
    assert "fetch the pinned " + name in " ".join(step.plan(ctx).actions)

    assert _applied(step, ctx).status == MET
    assert machine.fetched == [name]
    link = machine.bin / name
    assert link.is_symlink() and link.resolve() == (machine.cache / f"{name}-pinned").resolve()
    assert machine.environ["PATH"].split(":")[0] == str(machine.bin), "the engine's subprocesses run the pinned one"


@pytest.mark.parametrize(("name", "wrong"), [("terraform", "Terraform v1.3.6"),
                                             ("kubectl", "Client Version: v1.25.3")])
def test_wrong_version_ignored_and_said_once(tmp_path: Path, name: str, wrong: str) -> None:
    machine = FetchingTools(tmp_path, {**EVERY_GOOD, name: wrong})
    step = machine.step(answers())
    ctx = ctx_for()
    assert _applied(step, ctx).status == MET
    assert machine.fetched == [name]
    said = [e["message"] for e in ctx.events.of_kind("step_apply") if f"/usr/local/bin/{name}" in e.get("message", "")]
    assert len(said) == 1 and "ignoring it" in said[0]
    assert machine.environ["PATH"].startswith(str(machine.bin) + ":")
    # Checked again (a later step, the next run): no second fetch, nothing said again.
    assert step.check(ctx).status == MET and machine.fetched == [name]


def test_override_naming_an_old_kubectl_moves_to_the_pinned_one(tmp_path: Path) -> None:
    machine = FetchingTools(tmp_path, {**EVERY_GOOD, "kubectl": "Client Version: v1.22.17"})
    machine.environ["SWARM_KUBECTL"] = "/usr/local/bin/kubectl"
    step = machine.step(answers())
    assert _applied(step, ctx_for()).status == MET
    assert machine.environ["SWARM_KUBECTL"] == str(machine.bin / "kubectl")


def test_missing_gcloud_yes_installs_the_sdk_and_signs_in(tmp_path: Path) -> None:
    machine = FetchingTools(tmp_path, {k: v for k, v in EVERY_GOOD.items() if k != "gcloud"})
    ask = answers(preflight_tools=[""])  # Enter takes the default, yes
    step = machine.step(ask)
    ctx = ctx_for()
    assert _applied(step, ctx).status == MET
    assert ask.asked == [("preflight.tools", "Install the Google Cloud SDK into ~/.swarm/google-cloud-sdk? [Y/n]")]
    assert machine.sdk_installs == 1
    assert machine.logins == [str(machine.bin / "gcloud")], "signing in is the person's browser step"
    assert machine.fetched == []


def test_missing_gcloud_no_stops_with_how_to_install_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(preflight, "open_page", lambda url, *, why: opened.append(url))
    machine = FetchingTools(tmp_path, {k: v for k, v in EVERY_GOOD.items() if k != "gcloud"})
    ask = answers(preflight_tools=["n"])
    step = machine.step(ask)
    ctx = ctx_for()
    with pytest.raises(RuntimeError) as stopped:
        step.apply(ctx, step.plan(ctx))
    assert "https://cloud.google.com/sdk/docs/install" in str(stopped.value)
    assert "gcloud auth" not in str(stopped.value), "the installer signs in itself; it hands over no command"
    assert "signs you in itself" in str(stopped.value)
    assert machine.sdk_installs == 0 and machine.logins == []
    assert len(ask.asked) == 1
    assert opened == [preflight.SDK_PAGE]  # the page it names opens (steps.open_page)


def test_python_is_the_running_interpreter() -> None:
    """uv's managed Python runs the installer; python3 on PATH is never a prerequisite."""
    python = next(t for t in preflight.TOOLS if t.name == "python3")
    assert preflight.find_tool(python) == sys.executable


# -- DAYONE-PREFLIGHT-ASKS: preflight asks only what it cannot read ----------------


def test_stock_python3_on_path_is_never_refused(tmp_path: Path) -> None:
    """Stock macOS python3 is 3.9 and nothing runs it (render.py runs on
    sys.executable): what is on PATH is never looked at, so never refused."""
    machine = FetchingTools(tmp_path, {**EVERY_GOOD, "python3": "Python 3.9.6"})
    step = machine.step(answers())
    observed = step.check(ctx_for())
    assert observed.status == MET, observed.detail
    assert "python3" not in observed.detail


def test_hostname_and_region_from_the_verb_are_not_asked_again() -> None:
    """`swarm install` asked the hostname and region and handed them to the
    state: preflight asks neither again, and never offers us-central1 back as
    a default that Enter would silently switch to."""
    gcp = FakeGcp()
    ctx = ctx_for(hostname="swarm.example.com", region="europe-west1")
    inputs = preflight.InputsStep(lambda: gcp, Answers(preflight_inputs=["alerts@example.com", GITHUB_ORG]))
    assert "hostname" not in inputs.missing(ctx)
    inputs.apply(ctx, inputs.plan(ctx))
    assert ctx.outputs["hostname"] == "swarm.example.com"
    assert not [q for _, q in inputs._ask.asked if "Hostname" in q]

    ask = Answers(preflight_region=["europe-west1"])  # the typed confirmation only
    region = preflight.RegionStep(lambda: gcp, ask)
    region.apply(ctx, region.plan(ctx))
    assert len(ask.asked) == 1 and "europe-west1" in ask.asked[0][1]
    assert ask.defaults == [None], "the confirmation is typed, never defaulted"
    assert region.check(ctx).status == MET and ctx.state.region == "europe-west1"

    # Missing from the state: asked, with no silent switch back to another region.
    ask = Answers(preflight_region=["", "us-central1"])
    fresh = ctx_for(region=None)
    preflight.RegionStep(lambda: gcp, ask).apply(fresh, preflight.RegionStep(lambda: gcp, ask).plan(fresh))
    assert fresh.state.region == "us-central1" and ask.defaults[0] == "us-central1"


def test_billing_account_is_a_numbered_choice_from_the_api() -> None:
    gcp = FakeGcp(exists=False, billed=False)
    gcp.billing_account_list.append({"name": f"billingAccounts/{OTHER_ACCOUNT}", "displayName": "Lab", "open": True})
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG, "2"])
    step = preflight.InputsStep(lambda: gcp, ask)
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["billing_account"] == OTHER_ACCOUNT
    question = ask.asked[-1][1]
    assert f"1) {ACCOUNT}" in question and f"2) {OTHER_ACCOUNT}" in question and "Lab" in question
    assert gcp.called("billing_accounts")

    unlisted = FakeGcp(exists=False, billed=False)  # the list cannot be read: the id is typed

    def refused() -> list[dict[str, Any]]:
        raise ApiError(403, "PERMISSION_DENIED", "billing.accounts.list")

    unlisted.billing_accounts = refused  # type: ignore[method-assign]
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG, ACCOUNT])
    ctx = ctx_for()
    preflight.InputsStep(lambda: unlisted, ask).apply(ctx, step.plan(ctx))
    assert ctx.outputs["billing_account"] == ACCOUNT and "XXXXXX-XXXXXX-XXXXXX" in ask.asked[-1][1]

    for wrong in ("3", "0", "nope"):
        gcp.billing_account_list = [{"name": f"billingAccounts/{ACCOUNT}", "displayName": "Main"}] * 2
        ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG, wrong])
        with pytest.raises(ValueError):
            preflight.InputsStep(lambda: gcp, ask).apply(ctx_for(), step.plan(ctx_for()))


def test_billing_account_of_a_billed_project_is_read_not_asked() -> None:
    gcp = FakeGcp()  # exists, billing linked to ACCOUNT
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG])
    ctx = ctx_for()
    step = preflight.InputsStep(lambda: gcp, ask)
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["billing_account"] == ACCOUNT
    assert not [q for _, q in ask.asked if "Billing" in q]


def test_parent_is_a_numbered_choice_of_where_you_can_create_projects() -> None:
    gcp = FakeGcp(exists=False)
    gcp.perms[FOLDER] = {preflight.PROJECT_CREATE}
    gcp.orgs["organizations/999"] = {"name": "organizations/999", "displayName": "other.com"}  # no right there
    ask = Answers(preflight_rights=["2"])
    step = preflight.RightsStep(lambda: gcp, ask)
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["parent"] == FOLDER
    question = ask.asked[0][1]
    assert f"1) {ORG}" in question and f"2) {FOLDER}" in question and "organizations/999" not in question

    gcp.perms = {}  # nowhere listed: the parent is typed
    ask = Answers(preflight_rights=[ORG])
    ctx = ctx_for()
    preflight.RightsStep(lambda: gcp, ask).apply(ctx, step.plan(ctx))
    assert ctx.outputs["parent"] == ORG and "organizations/N" in ask.asked[0][1]


def test_github_org_asked_and_recorded() -> None:
    gcp = FakeGcp()
    ctx = ctx_for()
    step = preflight.InputsStep(lambda: gcp, answers())
    assert "github_org" in step.missing(ctx)
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["github_org"] == GITHUB_ORG
    assert any("GitHub organisation" in q for _, q in step._ask.asked)
    assert step.check(ctx).status == MET

    for wrong in ("-acme", "acme_eng", "https://github.com/acme", "a" * 40):
        bad = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", wrong])
        with pytest.raises(ValueError):
            preflight.InputsStep(lambda: gcp, bad).apply(ctx_for(), step.plan(ctx_for()))


def test_org_domain_read_from_the_organization() -> None:
    gcp = FakeGcp()  # the project exists, under FOLDER under ORG
    step = preflight.OrgDomainStep(lambda: gcp, Answers(), account=lambda: "admin@example.com")
    ctx = ctx_for()
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["org_domain"] == "example.com"
    assert step.check(ctx).status == MET
    assert (ORG,) in gcp.called("organization")

    fresh = FakeGcp(exists=False)  # a new project under a folder: the folder's organization
    ctx = ctx_for(outputs={"parent": FOLDER})
    preflight.OrgDomainStep(lambda: fresh, Answers(), account=lambda: "x@y.com").apply(ctx, step.plan(ctx))
    assert ctx.outputs["org_domain"] == "example.com"


def test_org_domain_falls_back_to_the_account_confirmed() -> None:
    gcp = FakeGcp()
    gcp.org_failure = ApiError(403, "PERMISSION_DENIED", "no")
    ask = Answers(preflight_org_domain=[""])  # Enter confirms the account's domain
    step = preflight.OrgDomainStep(lambda: gcp, ask, account=lambda: "admin@Acme.com")
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["org_domain"] == "acme.com" and ask.defaults == ["acme.com"]

    # A consumer account's domain is never offered, and never accepted: IAP would admit everyone on it.
    ask = Answers(preflight_org_domain=["gmail.com"])
    step = preflight.OrgDomainStep(lambda: gcp, ask, account=lambda: "someone@gmail.com")
    with pytest.raises(ValueError):
        step.apply(ctx_for(), step.plan(ctx_for()))
    assert ask.defaults == [None]


def test_installer_cidr_from_two_agreeing_echoes() -> None:
    gcp = FakeGcp()
    step = preflight.InstallerAddressStep(lambda: gcp, Answers())
    ctx = ctx_for()
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["installer_cidr"] == f"{INSTALLER_IP}/32"
    assert step.check(ctx).status == MET
    assert {args[0] for args in gcp.called("read_public")} >= set(preflight.ADDRESS_ECHOES)
    assert all(url.startswith("https://") for url in preflight.ADDRESS_ECHOES)

    gcp.release.update(echo_files("203.0.113.9"))  # the laptop moved: recorded again
    assert step.check(ctx).status == UNMET


def test_installer_cidr_asked_when_the_echoes_disagree() -> None:
    gcp = FakeGcp()
    gcp.release.update(echo_files(INSTALLER_IP, "203.0.113.9"))
    ask = Answers(preflight_installer_address=["203.0.113.9"])
    step = preflight.InstallerAddressStep(lambda: gcp, ask)
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["installer_cidr"] == "203.0.113.9/32"
    assert INSTALLER_IP in ask.asked[0][1] and "203.0.113.9" in ask.asked[0][1]
    assert step.check(ctx).status == MET

    del gcp.release[preflight.ADDRESS_ECHOES[0]]  # one echo down is not agreement either
    ask = Answers(preflight_installer_address=["not-an-address"])
    with pytest.raises(ValueError):
        preflight.InstallerAddressStep(lambda: gcp, ask).apply(ctx_for(), step.plan(ctx_for()))


#: The ADC sign-in preflight runs: Cloud Platform and the two Directory scopes.
ADC_LOGIN = ["auth", "application-default", "login",
             "--scopes=https://www.googleapis.com/auth/cloud-platform,"
             "https://www.googleapis.com/auth/admin.directory.customer.readonly,"
             "https://www.googleapis.com/auth/admin.directory.rolemanagement"]
CLOUD_PLATFORM_ONLY = frozenset({"https://www.googleapis.com/auth/cloud-platform", "openid"})
DIRECTORY_SCOPES = frozenset({"https://www.googleapis.com/auth/admin.directory.customer.readonly",
                              "https://www.googleapis.com/auth/admin.directory.rolemanagement"})
CLOUD_PLATFORM_LOGIN = ["auth", "application-default", "login",
                        "--scopes=https://www.googleapis.com/auth/cloud-platform"]


class Login:
    """gcloud, faked: each login argv run flips what it signs in. `adc` is the
    scopes the application-default credentials carry (None: no ADC); an ADC
    login grants exactly the scopes its --scopes flag names."""

    def __init__(self, *, account: str = "", adc: frozenset[str] | None = None, code: int = 0,
                 directory: str = "granted") -> None:
        """`directory` is what Google does with the Directory scopes: "granted",
        "blocked" (the browser says the app is blocked, so gcloud exits 1) or
        "dropped" (the sign-in finishes and the token comes back without them)."""
        self.account, self.adc, self.code, self.directory = account, adc, code, directory
        self.ran: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.ran.append(list(argv))
        if self.code == 0 and argv[1:] == ["auth", "login"]:
            self.account = "admin@example.com"
        if self.code == 0 and argv[1:4] == ["auth", "application-default", "login"]:
            flag = next((a for a in argv[4:] if a.startswith("--scopes=")), "--scopes=")
            scopes = frozenset(filter(None, flag.removeprefix("--scopes=").split(",")))
            if scopes & DIRECTORY_SCOPES and self.directory == "blocked":
                return 1
            if self.directory == "dropped":
                scopes -= DIRECTORY_SCOPES
            self.adc = scopes
        return self.code

    def step(self, ask: Any = None) -> preflight.SignInStep:
        return preflight.SignInStep(lambda: FakeGcp(), ask or Answers(), gcloud=lambda: "/opt/gcloud",
                                    account=lambda: self.account, adc=lambda: self.adc, login=self)


def test_sign_in_runs_gcloud_itself() -> None:
    """No account and no ADC: preflight runs both sign-ins itself (each opens
    the browser, the person's credential step) and checks again."""
    gcloud = Login()
    step = gcloud.step()
    ctx = ctx_for()
    observed = step.check(ctx)
    assert observed.status == UNMET and "account" in observed.detail and "application-default" in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", "auth", "login"], ["/opt/gcloud", *ADC_LOGIN]]
    assert step.check(ctx).status == MET

    only_adc = Login(account="admin@example.com")
    only_adc.step().apply(ctx, only_adc.step().plan(ctx))
    assert only_adc.ran == [["/opt/gcloud", *ADC_LOGIN]]


def test_sign_in_failure_never_hands_the_person_a_command() -> None:
    gcloud = Login(code=1)
    step = gcloud.step()
    ctx = ctx_for()
    with pytest.raises(RuntimeError) as stopped:
        step.apply(ctx, step.plan(ctx))
    assert "gcloud auth" not in str(stopped.value) and "swarm install" in str(stopped.value)
    assert not [e for e in ctx.events.of_kind("step_apply") if "gcloud auth" in e.get("message", "")]


def test_stage_0_order_signs_in_before_any_api_read() -> None:
    ids = [step.id for step in preflight.build_steps(lambda: FakeGcp(), Answers())]
    assert ids == ["preflight.tools", "preflight.sign-in", "preflight.organization", "preflight.inputs",
                   "preflight.region", "preflight.rights", "preflight.org-domain", "preflight.installer-address", "preflight.org-policies",
                   "preflight.quotas", "preflight.sizing", "preflight.release", "preflight.sizing-quota"]


# -- DAYONE-SIZING-ASK: the Directory scopes, the sizing questions, the Quotas page -----------


def test_adc_sign_in_asks_the_directory_scopes() -> None:
    """BGD's answer for IDENTITY-STAGE: preflight's own ADC sign-in asks the
    admin.directory.customer.readonly and admin.directory.rolemanagement scopes
    beside cloud-platform, and the check is MET only when the ADC carries them."""
    gcloud = Login(account="admin@example.com")
    step = gcloud.step()
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", *ADC_LOGIN]]
    assert gcloud.adc == frozenset(preflight.ADC_SCOPES)
    assert step.check(ctx).status == MET


def test_existing_adc_without_the_directory_scopes_signs_in_again() -> None:
    gcloud = Login(account="admin@example.com", adc=CLOUD_PLATFORM_ONLY)
    step = gcloud.step()
    ctx = ctx_for()
    observed = step.check(ctx)
    assert observed.status == UNMET
    assert "admin.directory.customer.readonly" in observed.detail
    assert "admin.directory.rolemanagement" in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", *ADC_LOGIN]]
    assert step.check(ctx).status == MET



# -- DAYONE-DIR-SCOPES: Google refusing the Directory scopes is never a dead end ----------


def test_directory_scopes_accepted_at_stage_0_are_recorded() -> None:
    gcloud = Login(account="admin@example.com")
    step = gcloud.step()
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", *ADC_LOGIN]]
    assert ctx.outputs["directory_scopes"] is True
    observed = step.check(ctx)
    assert observed.status == MET and observed.outputs.get("directory_scopes") is True


@pytest.mark.parametrize("refusal", ["blocked", "dropped"])
def test_directory_scopes_refused_at_stage_0_signs_in_without_them(refusal: str) -> None:
    """Google blocks the sign-in that asks the Directory scopes (gcloud's own
    client may not ask them), or finishes it without them: the ADC sign-in
    runs again with Cloud Platform alone, `directory_scopes` is recorded false,
    one plain line says stage 8 asks for them later, and the step is MET on
    this and every later run, never asking for them again at stage 0."""
    gcloud = Login(account="admin@example.com", directory=refusal)
    step = gcloud.step()
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", *ADC_LOGIN], ["/opt/gcloud", *CLOUD_PLATFORM_LOGIN]]
    assert gcloud.adc == frozenset({"https://www.googleapis.com/auth/cloud-platform"})
    assert ctx.outputs["directory_scopes"] is False
    said = [e["message"] for e in ctx.events.of_kind("step_apply") if "stage 8" in e.get("message", "")]
    assert len(said) == 1 and "\n" not in said[0] and "Directory" in said[0]
    assert step.check(ctx).status == MET
    assert step.missing(ctx) == []
    gcloud.ran.clear()
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == []


def test_a_sign_in_that_fails_without_the_directory_scopes_too_still_stops() -> None:
    gcloud = Login(account="admin@example.com", code=1)
    step = gcloud.step()
    ctx = ctx_for()
    with pytest.raises(RuntimeError) as stopped:
        step.apply(ctx, step.plan(ctx))
    assert "swarm install" in str(stopped.value)
    assert gcloud.ran == [["/opt/gcloud", *ADC_LOGIN], ["/opt/gcloud", *CLOUD_PLATFORM_LOGIN]]
    assert "directory_scopes" not in ctx.outputs


def test_a_declined_stage_0_signs_in_again_without_asking_the_directory_scopes() -> None:
    """Review minor: once `directory_scopes` is false, a missing ADC signs in
    with Cloud Platform alone; Google's "app is blocked" page never shows again."""
    gcloud = Login(account="admin@example.com", directory="blocked")
    step = gcloud.step()
    ctx = ctx_for(outputs={"directory_scopes": False, "installer": "admin@example.com"})
    step.apply(ctx, step.plan(ctx))
    assert gcloud.ran == [["/opt/gcloud", *CLOUD_PLATFORM_LOGIN]]
    assert ctx.outputs["directory_scopes"] is False
    assert step.check(ctx).status == MET


class GoogleAdc:
    """google-auth and tokeninfo, faked the way Google answers: an
    authorized_user refresh grant that asks for a scope the refresh token was
    never granted gets invalid_scope; one that asks for none gets the token's
    own grant, which tokeninfo reports."""

    def __init__(self, granted: frozenset[str]) -> None:
        self.granted = granted
        self.asked: list[Any] = []
        self.token = secrets.token_hex(16)

    def default(self, scopes: Any = None, **_: Any) -> tuple[Any, None]:
        from google.auth.exceptions import RefreshError

        self.asked.append(scopes)
        adc = self

        class Credentials:
            token = None
            requires_scopes = False

            def refresh(self, request: Any) -> None:
                if scopes and not set(scopes) <= adc.granted:
                    raise RefreshError("invalid_scope")
                self.token = adc.token

        return Credentials(), None

    def post(self, url: str, data: dict[str, str], timeout: float) -> Any:
        assert url == preflight.TOKENINFO and data["access_token"] == self.token
        return Response(200, {"scope": " ".join(sorted(self.granted))})


def test_adc_scopes_reads_a_cloud_platform_only_grant_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding: after the Cloud-Platform-only fallback, adc_scopes
    reads {cloud-platform} back rather than forcing the Directory scopes on
    the refresh (invalid_scope, an empty set, the sign-in again on every
    run), and the sign-in step is MET with directory_scopes false."""
    import google.auth
    import requests

    google_adc = GoogleAdc(frozenset({preflight.CLOUD_PLATFORM_SCOPE}))
    monkeypatch.setattr(google.auth, "default", google_adc.default)
    monkeypatch.setattr(requests, "post", google_adc.post)
    assert preflight.adc_scopes() == frozenset({preflight.CLOUD_PLATFORM_SCOPE})
    assert google_adc.asked == [None]

    gcloud = Login(account="admin@example.com")
    step = preflight.SignInStep(lambda: FakeGcp(), Answers(), gcloud=lambda: "/opt/gcloud",
                                account=lambda: gcloud.account, login=gcloud)
    ctx = ctx_for(outputs={"directory_scopes": False, "installer": "admin@example.com"})
    observed = step.check(ctx)
    assert observed.status == MET and observed.outputs["directory_scopes"] is False
    assert gcloud.ran == []


def test_the_organisation_sign_in_again_falls_back_the_same_way() -> None:
    gcloud = Login(account="admin@example.com", directory="blocked")
    step = gcloud.step()
    ctx = ctx_for()
    step.sign_in_again(ctx)
    assert gcloud.ran[-1] == ["/opt/gcloud", *CLOUD_PLATFORM_LOGIN]
    assert ctx.outputs["directory_scopes"] is False


def _sized(gcp: FakeGcp, *answered: str, outputs: dict[str, Any] | None = None) -> tuple[Any, Answers, Any]:
    ask = Answers(preflight_sizing=list(answered))
    step = preflight.SizingStep(lambda: gcp, ask)
    ctx = ctx_for(outputs=outputs)
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    return step, ask, ctx


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(preflight, "open_page", lambda url, *, why: opened.append((url, why)))
    return opened


def test_just_me_proposes_a_small_deployment_accepted_with_enter(pages: list[Any]) -> None:
    gcp = FakeGcp()
    step, ask, ctx = _sized(gcp, "1", "")
    assert ask.asked[0] == ("preflight.sizing", preflight.WHO_QUESTION)
    assert preflight.WHO_QUESTION == "Who is this SwarmCloud for? [1] just me  [2] a team"
    assert ask.defaults[0] == "1"  # an optional choice: Enter is "just me"
    assert len(ask.asked) == 2  # no "how many" for one person
    assert "Proposed sizing for one person" in ask.asked[1][1]
    assert ctx.outputs["sizing"] == sizing.propose("individual", 1, region_cpu_quota=3000)
    assert ctx.outputs["sizing"]["mode"] == "individual" and ctx.outputs["sizing"]["members"] == 1
    assert ctx.outputs["sizing_cpu_quota"] == {"region": "us-central1", "cpus": 3000}
    assert (PROJECT, "us-central1") in gcp.called("region_cpu_quota")
    observed = step.check(ctx)
    assert observed.status == MET and observed.outputs["sizing"] == ctx.outputs["sizing"]
    assert pages == []
    assert gcp.mutations == []


def test_a_team_is_asked_how_many_people() -> None:
    gcp = FakeGcp()
    _, ask, ctx = _sized(gcp, "2", "15", "")
    assert ask.asked[1] == ("preflight.sizing", preflight.MEMBERS_QUESTION)
    assert preflight.MEMBERS_QUESTION == "How many people will use it?"
    assert ctx.outputs["sizing"] == sizing.propose("team", 15, region_cpu_quota=3000)
    assert "Proposed sizing for a team of 15" in ask.asked[2][1]


def test_an_answer_that_is_not_a_choice_or_a_count_is_asked_again() -> None:
    gcp = FakeGcp()
    _, ask, ctx = _sized(gcp, "3", "2", "zero", "-4", "15", "")
    assert ctx.outputs["sizing"]["members"] == 15
    assert "'3' is not an answer" in ask.asked[1][1]
    assert "'-4' is not an answer" in ask.asked[4][1]

    with pytest.raises(ValueError):
        _sized(FakeGcp(), "x", "y", "z")
    with pytest.raises(ValueError):
        _sized(FakeGcp(), "2", "0", "0", "0")


def test_one_number_changed_then_accepted() -> None:
    gcp = FakeGcp()
    _, ask, ctx = _sized(gcp, "2", "15", "1", "60", "")
    expected = sizing.with_change(sizing.propose("team", 15, region_cpu_quota=3000), ("pool_limits", "global"), 60)
    assert ctx.outputs["sizing"] == expected
    assert ask.asked[3][1].startswith("New value for tasks at once, the whole deployment (global) (now 81)")
    assert ask.asked[4][1].startswith("Changed tasks at once, the whole deployment (global) to 60.")

    budget = len(sizing.rows(ctx.outputs["sizing"]))
    _, _, ctx = _sized(FakeGcp(), "1", str(budget), "750", "")
    assert ctx.outputs["sizing"]["budget_usd"] == 750


def test_a_change_terraform_would_refuse_is_not_taken() -> None:
    """provider_tenant above global breaks terraform's own floor
    (global >= tenants x provider_tenant): the change is refused, said, and
    the proposal stands; a row that is not a row, or a value that is not a
    positive number, is said too."""
    gcp = FakeGcp()
    _, ask, ctx = _sized(gcp, "1", "3", "11", "99", "2", "abc", "")
    assert ctx.outputs["sizing"] == sizing.propose("individual", 1, region_cpu_quota=3000)
    assert ask.asked[3][1].startswith("Not changed: global (10) is below 1 x provider_tenant (11)")
    assert ask.asked[4][1].startswith("'99' is not a row number.")
    assert ask.asked[6][1].startswith("Not changed: 'abc' is not a whole number")


def test_the_region_cpu_quota_caps_the_proposal_and_opens_the_quotas_page(pages: list[Any]) -> None:
    gcp = FakeGcp()
    gcp.cpu_quota = 48  # (48 - 8 for the cluster) / 4 vCPU = 10 tasks
    _, ask, ctx = _sized(gcp, "2", "50", "")
    pools = ctx.outputs["sizing"]["pool_limits"]
    assert pools["global"] == 10
    assert all(v <= 10 for _, v in sizing.rows(ctx.outputs["sizing"]) if _[0] == "pool_limits")
    assert [url for url, _ in pages] == [f"https://console.cloud.google.com/iam-admin/quotas?project={PROJECT}"]
    assert "48 CPUs" in pages[0][1]

    _, ask, ctx = _sized(FakeGcp(), "2", "50", "1", "500", "")  # 3,000 CPUs run 748 at most
    assert ctx.outputs["sizing"]["pool_limits"]["global"] == 500
    gcp = FakeGcp()
    gcp.cpu_quota = 48
    _, ask, ctx = _sized(gcp, "2", "50", "1", "11", "")
    assert ctx.outputs["sizing"]["pool_limits"]["global"] == 10
    assert "above the 10 tasks the region's 48 CPUs can run" in ask.asked[4][1]


def test_a_new_project_says_the_quota_is_checked_after_stage_1(pages: list[Any]) -> None:
    gcp = FakeGcp(exists=False)
    _, _, ctx = _sized(gcp, "1", "", outputs={"parent": ORG})
    assert not gcp.called("region_cpu_quota")
    said = [e.get("message", "") for e in ctx.events.of_kind("step_apply")]
    assert any("checked against it again after stage 1" in m for m in said)
    assert ctx.outputs["sizing"] == sizing.propose("individual", 1)
    assert ctx.outputs["sizing_cpu_quota"] == {"region": "us-central1", "cpus": None}

    gcp = FakeGcp()
    gcp.cpu_quota_failure = ApiError(403, "SERVICE_DISABLED", "Compute Engine API has not been used")
    _, _, ctx = _sized(gcp, "1", "")
    said = [e.get("message", "") for e in ctx.events.of_kind("step_apply")]
    assert any("after stage 1 enables Compute Engine" in m for m in said)


def test_stage_1_checks_the_sizing_against_the_new_projects_quota(pages: list[Any]) -> None:
    recorded = sizing.propose("team", 15)
    outputs = {"sizing": recorded, "sizing_cpu_quota": {"region": "us-central1", "cpus": None}}
    gcp = FakeGcp()
    gcp.cpu_quota = 108  # (108 - 8) / 4 = 25 tasks
    step = preflight.SizingQuotaStep(lambda: gcp, Answers(preflight_sizing_quota=[""]))
    assert step.stage == 1
    ctx = ctx_for(outputs=outputs)
    observed = step.check(ctx)
    assert observed.status == UNMET and "above the 25 tasks" in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["sizing"] == sizing.capped(recorded, 108)
    assert ctx.outputs["sizing"]["pool_limits"]["runner_profiles"]["codex"] == 9  # under the cap: kept
    assert ctx.outputs["sizing"]["budget_usd"] == recorded["budget_usd"]
    assert step.check(ctx).status == MET

    roomy = FakeGcp()
    fits = preflight.SizingQuotaStep(lambda: roomy, Answers()).check(ctx_for(outputs=outputs))
    assert fits.status == MET and fits.outputs["sizing_cpu_quota"] == {"region": "us-central1", "cpus": 3000}
    assert preflight.SizingQuotaStep(lambda: FakeGcp(exists=False), Answers()).check(
        ctx_for(outputs=outputs)).status == UNMET


def test_sizing_recorded_by_a_full_stage_0(tmp_path: Path) -> None:
    gcp = FakeGcp()
    ask = answers(preflight_sizing=["2", "5", ""])
    rig = Rig(tmp_path)
    result = rig.engine(stage0(gcp, ask), manifest=load_manifest()).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.store.load().outputs["sizing"] == sizing.propose("team", 5, region_cpu_quota=3000)
    assert gcp.mutations == []


def test_quotas_step_opens_the_quotas_page_for_an_increase(pages: list[Any]) -> None:
    gcp = FakeGcp()
    gcp.quotas["cloudkms.googleapis.com"] = [_metric("cloudkms.googleapis.com/crypto_requests", 100)]
    step = preflight.QuotasStep(lambda: gcp, answers())
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [f"https://console.cloud.google.com/iam-admin/quotas?project={PROJECT}"]
    assert "quota.kms-signing-h4" in pages[0][1]

    pages.clear()
    gcp.quotas["cloudkms.googleapis.com"] = [_metric("cloudkms.googleapis.com/crypto_requests", 60_000)]
    step.apply(ctx, step.plan(ctx))
    assert pages == []


# -- DAYONE-PREFLIGHT-RECORD: the release, the installer, the organisation, the quota project ----------


def test_the_release_is_recorded_from_the_installers_own_wheel() -> None:
    """Audit #2 and #30: with no --version, preflight.release records
    `state.release` itself, defaulting to the installed wheel's own release, so
    an rc CLI installs the rc it was downloaded as, never channels/stable."""
    gcp = FakeGcp()
    gcp.release = release_files(dt.date(2026, 12, 31), version="2026.11.3-rc.2")
    step = preflight.ReleaseStep(lambda: gcp, answers(), today=lambda: dt.date(2026, 10, 29),
                                 installed=lambda: "2026.11.3-rc.2", environ={})
    ctx = ctx_for()
    observed = step.check(ctx)
    assert observed.status == UNMET and "not recorded" in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert ctx.state.release == "2026.11.3-rc.2"
    assert step.check(ctx).status == MET
    assert not [url for (url,) in gcp.called("read_public") if "/channels/" in url]


def test_with_no_wheel_release_the_channel_install_sh_used_is_read() -> None:
    gcp = FakeGcp()
    gcp.release = release_files(dt.date(2026, 12, 31), version="2026.11.4-rc.1")
    gcp.release[f"{RELEASE_HOME}/channels/rc"] = gcp.release.pop(f"{RELEASE_HOME}/channels/stable")
    step = preflight.ReleaseStep(lambda: gcp, answers(), today=lambda: dt.date(2026, 10, 29),
                                 installed=lambda: None, environ={"SWARM_CHANNEL": "rc"})
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.state.release == "2026.11.4-rc.1"

    stable = FakeGcp()
    default = preflight.ReleaseStep(lambda: stable, answers(), today=lambda: dt.date(2026, 10, 29),
                                    installed=lambda: None, environ={})
    ctx = ctx_for()
    default.apply(ctx, default.plan(ctx))
    assert ctx.state.release == "v1.0.0"
    assert (f"{RELEASE_HOME}/channels/stable",) in stable.called("read_public")


def test_the_summary_never_names_a_channel_that_is_not_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "installed_release", lambda: None)
    assert preflight.default_release_said({"SWARM_CHANNEL": "rc"}) == "the current rc release"
    said = preflight.default_release_said({"SWARM_CHANNEL": "foo"})
    assert "the current foo release" not in said and "not a release channel" in said


def test_a_workspace_account_that_sees_no_organisation_is_told_to_ask_for_a_role() -> None:
    gcp = _no_organisation()
    workspace = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: "dev@acme.example")
    assert "ask your organisation's admin for a role" in workspace.check(ctx_for()).detail
    consumer = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: NO_ORG_ACCOUNT)
    assert "admin for a role" not in consumer.check(ctx_for()).detail


def test_a_release_whose_acceptances_expired_is_never_recorded() -> None:
    gcp = FakeGcp()
    gcp.release = release_files(dt.date(2026, 10, 1))
    step = preflight.ReleaseStep(lambda: gcp, answers(), today=lambda: dt.date(2026, 10, 29),
                                 installed=lambda: None, environ={})
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.state.release is None
    assert step.check(ctx).status == UNMET


def test_wheel_versions_map_back_to_release_names() -> None:
    assert preflight.release_of_wheel("2026.1.2") == "2026.01.2"
    assert preflight.release_of_wheel("2026.11.3rc2") == "2026.11.3-rc.2"
    assert preflight.release_of_wheel("0.1.0") is None
    assert preflight.release_of_wheel("2026.10.1.dev3") is None


def test_a_full_stage_0_records_the_release_and_the_installer(tmp_path: Path) -> None:
    gcp = FakeGcp()
    rig = Rig(tmp_path)
    result = rig.engine(stage0(gcp, answers()), manifest=load_manifest()).run()
    assert result.exit_code == ExitCode.OK, result.message
    state = rig.store.load()
    assert state.release == "v1.0.0"
    assert state.outputs["installer"] == "admin@example.com"


def test_sign_in_records_the_installer() -> None:
    """Audit #3: preflight.sign-in records `installer`, the signed-in gcloud
    account, and keeps the first one recorded."""
    gcloud = Login()
    step = gcloud.step()
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["installer"] == "admin@example.com"
    assert step.check(ctx).status == MET

    signed = Login(account="admin@example.com", adc=frozenset(preflight.ADC_SCOPES))
    ctx = ctx_for()
    unrecorded = signed.step().check(ctx)
    assert unrecorded.status == UNMET and "installer" in unrecorded.detail
    signed.step().apply(ctx, signed.step().plan(ctx))
    assert signed.ran == [] and ctx.outputs["installer"] == "admin@example.com"


NO_ORG_ACCOUNT = "someone@gmail.com"


def _no_organisation() -> FakeGcp:
    gcp = FakeGcp(exists=False)
    gcp.orgs, gcp.folder_tree = {}, {}
    return gcp


def test_no_organisation_is_said_first_and_the_cloud_identity_page_opens(tmp_path: Path,
                                                                        pages: list[Any]) -> None:
    """Audit #24, BGD 2026-10-30 "require an org, say so up front": an account
    with no Google organisation hears, before any question, that SwarmCloud
    needs one; the Cloud Identity Free sign-up opens; there is no path on."""
    log: list[Any] = []
    gcp = _no_organisation()
    gcp.log = log
    ask = answers(log)
    signed: list[str] = []
    rig = Rig(tmp_path)
    steps = stage0(gcp, ask, account=NO_ORG_ACCOUNT, sign_in=lambda: signed.append("again"))
    result = rig.engine(steps, manifest=load_manifest()).run()

    assert result.step == "preflight.organization"
    assert result.exit_code == ExitCode.PAUSED
    assert ask.asked == []  # nothing was asked before it
    assert [url for url, _ in pages] == [preflight.CLOUD_IDENTITY_SIGNUP]
    assert "needs a Google organisation" in pages[0][1] and "Cloud Identity" in pages[0][1]
    assert signed == ["again"]  # a consumer account signs in again, as the organisation's admin
    assert len(gcp.called("organizations")) > 2  # polled, never "press Enter"
    assert gcp.mutations == []


def test_the_organisation_step_goes_on_once_the_organisation_appears(pages: list[Any]) -> None:
    gcp = _no_organisation()
    accounts = [NO_ORG_ACCOUNT]

    def sign_in() -> None:
        accounts.append("admin@acme.example")
        gcp.orgs = {ORG: {"name": ORG, "displayName": "acme.example"}}

    step = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: accounts[-1], sign_in=sign_in)
    assert step.kind == "wait" and step.wait.bound_s >= 30 * 60
    ctx = ctx_for(outputs={"installer": NO_ORG_ACCOUNT})
    observed = step.check(ctx)
    assert observed.status == UNMET and "needs a Google organisation" in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert step.check(ctx).status == MET
    assert ctx.outputs["installer"] == "admin@acme.example"  # the organisation's account, not the gmail one


def test_an_organisation_account_is_not_signed_in_again(pages: list[Any]) -> None:
    gcp = FakeGcp(exists=False)
    assert preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: "admin@example.com").check(
        ctx_for()).status == MET

    workspace = _no_organisation()
    step = preflight.OrganizationStep(lambda: workspace, answers(), account=lambda: "admin@acme.example",
                                      sign_in=lambda: pytest.fail("signed in again"))
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [preflight.CLOUD_IDENTITY_SIGNUP]


def test_stage_0_says_the_organisation_before_asking_anything() -> None:
    ids = [step.id for step in preflight.build_steps(lambda: FakeGcp(), Answers())]
    assert ids.index("preflight.organization") == ids.index("preflight.sign-in") + 1
    assert ids.index("preflight.organization") < ids.index("preflight.inputs")


class Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()
        self.reason = "x"

    def json(self) -> Any:
        return self._body


class Session:
    """An AuthorizedSession, faked: each request kept with its headers."""

    def __init__(self, body: Any = None) -> None:
        self.body = body if body is not None else {}
        self.requests: list[dict[str, Any]] = []

    def request(self, method, url, *, json=None, params=None, timeout=None, headers=None):  # noqa: A002
        self.requests.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        return Response(200, self.body)


def test_org_policy_is_read_through_resource_manager_v1_with_no_quota_project() -> None:
    """Audit #8: the org policy API needs a quota project a fresh laptop does
    not have; Resource Manager v1's getEffectiveOrgPolicy does not."""
    session = Session({"constraint": "constraints/gcp.resourceLocations",
                       "listPolicy": {"allowedValues": ["in:eu-locations"]}})
    gcp = preflight.Gcp(session)
    policy = gcp.effective_policy(FOLDER, "gcp.resourceLocations")
    sent = session.requests[0]
    assert sent["method"] == "POST"
    assert sent["url"] == f"{preflight.CRM}/v1/{FOLDER}:getEffectiveOrgPolicy"
    assert sent["json"] == {"constraint": "constraints/gcp.resourceLocations"}
    assert "x-goog-user-project" not in sent["headers"]
    assert preflight._values(policy, "allowedValues") == ["in:eu-locations"]

    class QuotaProjectAsked(Session):
        def request(self, method, url, **kw):
            super().request(method, url, **kw)
            if "x-goog-user-project" in (kw.get("headers") or {}):
                return Response(200, {})
            return Response(403, {"error": {"status": "PERMISSION_DENIED",
                                            "message": "This API requires a quota project, which is not set"}})

    asked = QuotaProjectAsked()
    preflight.Gcp(asked).effective_policy(f"projects/{PROJECT}", "gcp.resourceLocations")
    assert [r["headers"].get("x-goog-user-project") for r in asked.requests] == [None, PROJECT]
    with pytest.raises(ApiError):
        preflight.Gcp(QuotaProjectAsked()).effective_policy(FOLDER, "gcp.resourceLocations")

    fake = FakeGcp()
    fake.policies["gcp.resourceLocations"] = policy
    blocked = preflight.OrgPoliciesStep(lambda: fake, answers()).check(ctx_for(region="us-central1"))
    assert blocked.status == UNMET and "gcp.resourceLocations" in blocked.detail


def test_the_organisation_wait_polls_as_the_account_signed_in_again(monkeypatch: pytest.MonkeyPatch,
                                                                     pages: list[Any]) -> None:
    """A gmail account signs in again as the new organisation's admin; the
    wait's polls then load the application-default credentials afresh. The
    session cached from the gmail sign-in never sees the organisation."""
    import google.auth
    import google.auth.transport.requests as transport

    signed_in = ["gmail"]
    loaded: list[str] = []

    def default(scopes=None):
        loaded.append(signed_in[-1])
        return signed_in[-1], None

    class AccountSession(Session):
        def __init__(self, credentials: str) -> None:
            super().__init__()
            self.credentials = credentials

        def request(self, method, url, **kw):
            super().request(method, url, **kw)
            if url.endswith("organizations:search") and self.credentials == "admin":
                return Response(200, {"organizations": [{"name": ORG, "displayName": "acme.example"}]})
            return Response(200, {})

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(transport, "AuthorizedSession", AccountSession)
    gcp = preflight.Gcp()
    accounts = [NO_ORG_ACCOUNT]

    def sign_in() -> None:
        signed_in.append("admin")
        accounts.append("admin@acme.example")

    step = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: accounts[-1], sign_in=sign_in)
    ctx = ctx_for()
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert step.check(ctx).status == MET
    assert loaded == ["gmail", "admin"]

    given = Session()
    kept = preflight.Gcp(given)
    kept.reset_credentials()
    assert kept.session is given  # a session given in is never dropped


def test_budget_calls_carry_the_project_as_quota_project() -> None:
    session = Session({"budgets": []})
    gcp = preflight.Gcp(session)
    gcp.quota_project = PROJECT
    gcp.budgets(ACCOUNT)
    gcp.create_budget(ACCOUNT, {"displayName": "swarm-p-test"})
    assert [r["headers"].get("x-goog-user-project") for r in session.requests] == [PROJECT, PROJECT]

    shared = preflight._Lazy()
    shared.use_quota_project(PROJECT)
    shared.made.append(preflight.Gcp(Session()))
    shared.use_quota_project("p-other")
    assert shared().quota_project == "p-other"


def _cpus(region_limits: dict[str | None, int], name: str = "compute.googleapis.com/cpus") -> dict[str, Any]:
    buckets = [{"effectiveLimit": str(v), **({"dimensions": {"region": r}} if r else {})}
               for r, v in region_limits.items()]
    return {"metric": name, "consumerQuotaLimits": [{"unit": "1/{project}/{region}", "quotaBuckets": buckets}]}


def test_the_cpu_quota_is_the_exact_metric_in_the_install_region(pages: list[Any]) -> None:
    """Audit #32: only `compute.googleapis.com/cpus`, and only the install
    region's bucket (else the default), is the regional CPU quota; a low one
    opens the Quotas page for that metric."""
    gcp = FakeGcp()
    gcp.enabled.add("compute.googleapis.com")
    gcp.quotas["compute.googleapis.com"] = [
        _cpus({None: 24, "us-central1": 200, "asia-south2": 8}),
        _cpus({None: 0}, name="compute.googleapis.com/a2_cpus"),
        _cpus({None: 1}, name="compute.googleapis.com/preemptible_cpus"),
        _metric("compute.googleapis.com/static_addresses", 8),
    ]
    step = preflight.QuotasStep(lambda: gcp, answers())
    observed = step.check(ctx_for(region="us-central1"))
    assert observed.status == MET, observed.detail
    assert list(observed.outputs["quotas"]["quota.compute-regional-cpus"].values()) == [200]

    assert step.check(ctx_for(region="europe-west1")).status == MET  # the default bucket, 24
    gcp.quotas["compute.googleapis.com"][0] = _cpus({None: 24, "us-central1": 12})
    ctx = ctx_for(region="us-central1")
    low = step.check(ctx)
    assert low.status == UNMET and "quota.compute-regional-cpus: 12 is below 24" in low.detail
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [preflight.quotas_page(PROJECT, metric="compute.googleapis.com/cpus",
                                                               region="us-central1")]
    assert "metric=compute.googleapis.com%2Fcpus" in pages[0][0]


def test_an_unreadable_stage_1_quota_keeps_the_proposed_sizing() -> None:
    """BGD 2026-10-30: preflight.sizing-quota says in one line that the quota
    could not be read and keeps the proposal, never "run swarm install again"."""
    recorded = sizing.propose("team", 15)
    gcp = FakeGcp()
    gcp.cpu_quota_failure = ApiError(403, "SERVICE_DISABLED", "Compute Engine API has not been used")
    step = preflight.SizingQuotaStep(lambda: gcp, Answers())
    ctx = ctx_for(outputs={"sizing": recorded})
    observed = step.check(ctx)
    assert observed.status == MET
    assert "could not be read" in observed.detail and "\n" not in observed.detail
    assert "swarm install again" not in observed.detail
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["sizing"] == recorded


# -- DAYONE-PREFLIGHT-3: a new organisation's billing, the organisation landing, the quota wait (part of #2688) --


def _billing_after(gcp: FakeGcp, polls: int) -> None:
    """`billingAccounts.list` is empty for `polls` reads, then lists ACCOUNT."""
    listed = list(gcp.billing_account_list)
    seen = {"n": 0}

    def billing_accounts() -> list[dict[str, Any]]:
        gcp._call("billing_accounts")
        seen["n"] += 1
        return listed if polls is not None and seen["n"] > polls else []

    gcp.billing_account_list = []
    gcp.billing_accounts = billing_accounts  # type: ignore[method-assign]


def test_no_billing_account_opens_billing_creation_and_polls_until_one_appears(pages: list[Any]) -> None:
    """N15: a brand-new organisation has no billing account; the installer
    says so, opens the billing account creation page and polls the list,
    then offers the account that appeared. It never asks for an id to type."""
    gcp = FakeGcp(exists=False, billed=False)
    _billing_after(gcp, polls=3)
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG, ""])
    step = preflight.InputsStep(lambda: gcp, ask)
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [preflight.BILLING_CREATE]
    assert "no billing account" in pages[0][1]
    assert ctx.outputs["billing_account"] == ACCOUNT
    assert len(gcp.called("billing_accounts")) == 4  # polled, never "press Enter"
    assert not [q for _, q in ask.asked if "XXXXXX" in q]
    assert ctx.clock.sleeps and all(s <= preflight.BILLING_WAIT.interval_s for s in ctx.clock.sleeps)
    assert gcp.mutations == []


def test_a_billing_account_that_never_appears_says_what_to_look_at(pages: list[Any]) -> None:
    gcp = FakeGcp(exists=False, billed=False)
    _billing_after(gcp, polls=10**9)
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG])
    step = preflight.InputsStep(lambda: gcp, ask)
    ctx = ctx_for()
    with pytest.raises(WaitGaveUp) as raised:
        step.apply(ctx, step.plan(ctx))
    # BGD, 2026-10-30: the wait's own WaitGaveUp, so the engine pauses the run (ENGINE-WAIT-PAUSE).
    assert type(raised.value) is WaitGaveUp
    assert "billing" in str(raised.value) and preflight.BILLING_WAIT.look_at in str(raised.value)
    assert [url for url, _ in pages] == [preflight.BILLING_CREATE]
    assert not [q for _, q in ask.asked if "Billing" in q]


def test_a_new_domain_account_with_no_organisation_opens_billing_creation(pages: list[Any]) -> None:
    """N16 [plausible]: Google may create the organisation only when the
    domain's account first creates a billing account (or a project). Once the
    person is signed in as the domain account and no organisation shows, the
    billing account creation page opens too, so the organisation comes into
    being whichever way Google answers; the wait says what to check."""
    gcp = _no_organisation()
    gcp.billing_account_list = []
    accounts = [NO_ORG_ACCOUNT]
    step = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: accounts[-1],
                                      sign_in=lambda: accounts.append("admin@acme.example"))
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [preflight.CLOUD_IDENTITY_SIGNUP, preflight.BILLING_CREATE]
    assert "admin@acme.example" in pages[1][1] and "creates the organisation" in pages[1][1]
    observed = step.check(ctx)
    assert observed.status == UNMET and "admin@acme.example" in observed.detail
    assert "must be in the new domain" in observed.detail
    assert "must be in the new domain" in preflight.ORGANISATION_WAIT.look_at
    assert preflight.ORGANISATION_WAIT.bound_s >= 3 * 60 * 60


def test_an_organisation_that_lands_at_sign_up_opens_no_billing_page(pages: list[Any]) -> None:
    gcp = _no_organisation()
    gcp.billing_account_list = []
    accounts = [NO_ORG_ACCOUNT]

    def sign_in() -> None:
        accounts.append("admin@acme.example")
        gcp.orgs = {ORG: {"name": ORG, "displayName": "acme.example"}}

    step = preflight.OrganizationStep(lambda: gcp, answers(), account=lambda: accounts[-1], sign_in=sign_in)
    ctx = ctx_for()
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [preflight.CLOUD_IDENTITY_SIGNUP]


def test_a_quota_the_service_does_not_report_is_said_in_one_line_not_raised() -> None:
    """#32: a metric the service does not report is said in one plain line."""
    gcp = FakeGcp()
    gcp.enabled.add("run.googleapis.com")
    gcp.quotas["run.googleapis.com"] = [_metric("run.googleapis.com/requests", 100)]
    observed = preflight.QuotasStep(lambda: gcp, answers()).check(ctx_for())
    assert observed.status == MET
    said = observed.outputs["quotas"]["quota.cloud-run-jobs"]
    assert isinstance(said, str) and "reports no" in said and "\n" not in said
    assert "reports no" in observed.detail


def test_the_quotas_step_polls_for_the_increase_after_the_page_opens(tmp_path: Path, pages: list[Any]) -> None:
    """#32: the Quotas page opens and the step waits for the increase to land,
    with a countdown, instead of "after apply its check is unmet"."""
    gcp = FakeGcp()
    gcp.quotas["cloudkms.googleapis.com"] = [_metric("cloudkms.googleapis.com/crypto_requests", 100)]
    reads = {"n": 0}
    original = gcp.quota_metrics

    def quota_metrics(project: str, service: str) -> list[dict[str, Any]]:
        reads["n"] += 1
        if reads["n"] > 4:  # granted while the installer polls
            gcp.quotas["cloudkms.googleapis.com"] = [_metric("cloudkms.googleapis.com/crypto_requests", 60_000)]
        return original(project, service)

    gcp.quota_metrics = quota_metrics  # type: ignore[method-assign]
    step = preflight.QuotasStep(lambda: gcp, answers())
    assert step.kind == "wait" and step.wait is preflight.QUOTA_WAIT
    rig = Rig(tmp_path)
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(pages) == 1 and "quota.kms-signing-h4" in pages[0][1]
    assert rig.of_kind("wait_countdown", "preflight.quotas")
    assert gcp.mutations == []


# -- DAYONE-SIZING-CAP (audit 3, A6): a quota too small for the team is a wait, never optional ----------

CPUS_PAGE = preflight.quotas_page(PROJECT, metric="compute.googleapis.com/cpus", region="us-central1")


def _cpus_after(gcp: FakeGcp, first: int, granted: int, *, polls: int) -> None:
    """The region's CPUS quota reads `first` for `polls` reads, then `granted`."""
    seen = {"n": 0}

    def region_cpu_quota(project: str, region: str) -> int:
        gcp._call("region_cpu_quota", project, region)
        seen["n"] += 1
        return granted if seen["n"] > polls else first

    gcp.region_cpu_quota = region_cpu_quota  # type: ignore[method-assign]


def test_enter_is_refused_while_the_sizing_breaks_a_rule(pages: list[Any]) -> None:
    """A6: Enter accepted whatever the table held; now it says which number
    breaks which rule and asks again until the sizing is one terraform takes."""
    bad = sizing.with_change(sizing.propose("team", 5), ("pool_limits", "provider_tenant"), 10)
    ask = Answers(preflight_sizing=["", "3", "5", ""])
    chosen = preflight.review(ask, ctx_for(), "preflight.sizing", "team", 5, None, start=bad)
    assert ask.asked[1][1].startswith("Not accepted: global (27) is below 5 x provider_tenant (10)")
    assert chosen["pool_limits"]["provider_tenant"] == 5
    assert sizing.problems(chosen) == []


def test_a_quota_too_small_for_the_team_is_a_required_wait(pages: list[Any]) -> None:
    """A6: 24 CPUs run 4 tasks, fewer than a team of 5's workspaces, so no
    edit can pass. The increase is required: the CPUS quota opens on the
    Quotas page and the step polls it until it is granted, then proposes."""
    gcp = FakeGcp()
    _cpus_after(gcp, 24, 48, polls=3)
    _, ask, ctx = _sized(gcp, "2", "5", "")
    assert [url for url, _ in pages] == [CPUS_PAGE]
    assert "required" in pages[0][1] and "optional" not in pages[0][1]
    assert "at least 28" in pages[0][1]
    assert len(gcp.called("region_cpu_quota")) == 4  # polled, never "press Enter"
    assert ctx.clock.sleeps and all(s <= preflight.QUOTA_WAIT.interval_s for s in ctx.clock.sleeps)
    assert ctx.outputs["sizing"] == sizing.propose("team", 5, region_cpu_quota=48)
    assert ctx.outputs["sizing_cpu_quota"] == {"region": "us-central1", "cpus": 48}
    assert preflight.SizingStep(lambda: gcp, Answers()).check(ctx).status == MET


def test_a_quota_never_granted_pauses_and_the_rerun_keeps_the_answer(pages: list[Any]) -> None:
    """A6: past the wait's bound the run pauses (the engine's WaitGaveUp); the
    answer is recorded, so `swarm install` again never asks who it is for."""
    gcp = FakeGcp()
    gcp.cpu_quota = 24
    ask = Answers(preflight_sizing=["2", "5"])
    step = preflight.SizingStep(lambda: gcp, ask)
    ctx = ctx_for()
    with pytest.raises(WaitGaveUp) as raised:
        step.apply(ctx, step.plan(ctx))
    assert preflight.QUOTA_WAIT.look_at in str(raised.value)
    assert ctx.outputs["sizing"]["mode"] == "team" and ctx.outputs["sizing"]["members"] == 5
    assert step.check(ctx).status == UNMET

    gcp.cpu_quota = 48  # granted while the run was paused
    again = Answers(preflight_sizing=[""])
    rerun = preflight.SizingStep(lambda: gcp, again)
    rerun.apply(ctx, rerun.plan(ctx))
    assert preflight.WHO_QUESTION not in [q for _, q in again.asked]
    assert "Proposed sizing for a team of 5" in again.asked[0][1]
    assert ctx.outputs["sizing"] == sizing.capped(sizing.propose("team", 5), 48)
    assert rerun.check(ctx).status == MET


def test_stage_1_waits_for_the_cpus_a_recorded_team_needs(pages: list[Any]) -> None:
    """A6 in stage 1: the new project's 24 CPUs cannot fit a team of 10, so
    preflight.sizing-quota opens the CPUS quota and waits, then caps."""
    recorded = sizing.propose("team", 10)
    gcp = FakeGcp()
    _cpus_after(gcp, 24, 48, polls=2)
    step = preflight.SizingQuotaStep(lambda: gcp, Answers(preflight_sizing_quota=[""]))
    ctx = ctx_for(outputs={"sizing": recorded})
    assert step.check(ctx).status == UNMET
    step.apply(ctx, step.plan(ctx))
    assert [url for url, _ in pages] == [CPUS_PAGE]
    assert "required" in pages[0][1] and "at least 48" in pages[0][1]
    assert ctx.outputs["sizing"] == sizing.capped(recorded, 48)
    assert ctx.outputs["sizing_cpu_quota"] == {"region": "us-central1", "cpus": 48}
    assert step.check(ctx).status == MET


@pytest.mark.parametrize(("recorded", "floor"), [(None, 24), (sizing.propose("individual", 1), 24),
                                                 (sizing.propose("team", 5), 28),
                                                 (sizing.propose("team", 10), 48)])
def test_the_cpus_floor_follows_the_recorded_sizing(recorded: Any, floor: int) -> None:
    """A6: the CPUS floor of 24 let through a quota the sizing cannot use; it
    is now the smallest quota the recorded sizing can use (never below 24)."""
    gcp = FakeGcp()
    gcp.enabled.add("compute.googleapis.com")
    step = preflight.QuotasStep(lambda: gcp, answers())
    ctx = ctx_for(outputs={"sizing": recorded} if recorded else None)
    gcp.quotas["compute.googleapis.com"] = [_metric("compute.googleapis.com/cpus", floor)]
    assert step.check(ctx).status == MET
    gcp.quotas["compute.googleapis.com"] = [_metric("compute.googleapis.com/cpus", floor - 1)]
    observed = step.check(ctx)
    assert observed.status == UNMET and f"{floor - 1} is below {floor}" in observed.detail
