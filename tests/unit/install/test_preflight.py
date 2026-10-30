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
           today: dt.date = dt.date(2026, 10, 29)) -> list[Any]:
    make = lambda: gcp  # noqa: E731
    return [
        preflight.ToolsStep(make, ask, **(tools or good_tools())),
        signed_in(make, ask),
        preflight.InputsStep(make, ask),
        preflight.RegionStep(make, ask),
        preflight.RightsStep(make, ask),
        preflight.OrgDomainStep(make, ask, account=lambda: "admin@example.com"),
        preflight.InstallerAddressStep(make, ask),
        preflight.OrgPoliciesStep(make, ask),
        preflight.QuotasStep(make, ask),
        preflight.SizingStep(make, ask),
        preflight.ReleaseStep(make, ask, today=lambda: today),
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

    gcp.quotas["cloudkms.googleapis.com"] = []
    with pytest.raises(LookupError):  # unreadable is unknown to the engine, never fine
        step.check(ctx_for())


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
    observed = step.check(ctx_for())
    assert observed.status == MET
    assert "CVE-2026-1111 in openssl, accepted until 2026-12-31" in observed.detail
    assert step.plan(ctx_for()).actions == ("CVE-2026-1111 in openssl, accepted until 2026-12-31: no fix upstream yet",)

    gcp.release = release_files(dt.date(2026, 10, 1))
    expired = step.check(ctx_for())
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

    gcp.billing_account_list = []  # nothing listed: the id is typed
    ask = Answers(preflight_inputs=["alerts@example.com", "swarm.example.com", GITHUB_ORG, ACCOUNT])
    ctx = ctx_for()
    preflight.InputsStep(lambda: gcp, ask).apply(ctx, step.plan(ctx))
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


class Login:
    """gcloud, faked: each login argv run flips what it signs in. `adc` is the
    scopes the application-default credentials carry (None: no ADC); an ADC
    login grants exactly the scopes its --scopes flag names."""

    def __init__(self, *, account: str = "", adc: frozenset[str] | None = None, code: int = 0) -> None:
        self.account, self.adc, self.code = account, adc, code
        self.ran: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        self.ran.append(list(argv))
        if self.code == 0 and argv[1:] == ["auth", "login"]:
            self.account = "admin@example.com"
        if self.code == 0 and argv[1:4] == ["auth", "application-default", "login"]:
            flag = next((a for a in argv[4:] if a.startswith("--scopes=")), "--scopes=")
            self.adc = frozenset(filter(None, flag.removeprefix("--scopes=").split(",")))
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
    assert ids == ["preflight.tools", "preflight.sign-in", "preflight.inputs", "preflight.region", "preflight.rights",
                   "preflight.org-domain", "preflight.installer-address", "preflight.org-policies",
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
    assert ask.asked[3][1].startswith("New value for tasks at once, the whole deployment (global) (now 54)")
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
    recorded = sizing.propose("team", 50)
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
    assert ctx.outputs["sizing"]["pool_limits"]["runner_profiles"]["codex"] == 20  # under the cap: kept
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
