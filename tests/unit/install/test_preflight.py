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
from swarm_install.steps import foundations, preflight
from swarm_install.steps.preflight import RELEASE_HOME, ApiError

from .test_state_store import FakeBucket, FakeClock

PROJECT = "p-test"
NUMBER = "123456789012"
ORG = "organizations/4242"
FOLDER = "folders/77"
ACCOUNT = "01ABCD-23EF45-6789AB"

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


class Answers:
    """The terminal, faked: answers by step id, in order; every question logged."""

    def __init__(self, log: list[Any] | None = None, **by_step: list[str]) -> None:
        self.log = log if log is not None else []
        self.by_step = {self._step_id(k): list(v) for k, v in by_step.items()}
        self.asked: list[tuple[str, str]] = []

    @staticmethod
    def _step_id(key: str) -> str:
        stage, _, name = key.partition("_")  # preflight_inputs -> preflight.inputs
        return f"{stage}.{name.replace('_', '-')}"

    def __call__(self, ctx: Any, step_id: str, question: str, default: str | None = None) -> str:
        self.asked.append((step_id, question))
        self.log.append(("ask", step_id, question))
        queue = self.by_step.get(step_id) or []
        if not queue:
            raise AssertionError(f"{step_id} asked an unexpected question: {question}")
        return queue.pop(0)


def good_tools() -> dict[str, Any]:
    versions = {"terraform": "Terraform v1.16.2\non linux_amd64", "kubectl": "Client Version: v1.36.1",
                "python3": "Python 3.11.9"}
    return {
        "find": lambda tool: f"/usr/local/bin/{tool.name}",
        "run": lambda argv: versions.get(argv[0].rsplit("/", 1)[1], "1.0.0"),
    }


def answers(log: list[Any] | None = None, *, region: str = "us-central1", confirm: str | None = None,
            **extra: list[str]) -> Answers:
    return Answers(
        log,
        preflight_inputs=["alerts@example.com", "swarm.example.com", ACCOUNT],
        preflight_region=[region, confirm if confirm is not None else region],
        foundations_default_compute_editor=["remove-editor"],
        **extra,
    )


def stage0(gcp: FakeGcp, ask: Answers, *, tools: dict[str, Any] | None = None,
           today: dt.date = dt.date(2026, 10, 29)) -> list[Any]:
    make = lambda: gcp  # noqa: E731
    return [
        preflight.ToolsStep(make, ask, **(tools or good_tools())),
        preflight.InputsStep(make, ask),
        preflight.RegionStep(make, ask),
        preflight.RightsStep(make, ask),
        preflight.OrgPoliciesStep(make, ask),
        preflight.QuotasStep(make, ask),
        preflight.ReleaseStep(make, ask, today=lambda: today),
    ]


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
    right = answers(log, region="europe-west1")
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
