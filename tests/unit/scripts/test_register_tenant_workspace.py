"""`register-tenant.sh --workspace` makes one person's workspace, under the guard.

docs/workspaces.md §4 (lane W6 of #847). The Cloud Run job `swarm-workspace-apply`
runs the script, through scripts/workspace-apply.sh (lane W6b), as
`swarm-workspace-deployer`, an identity with project-wide account-IAM power in a
project shared with another team, so what these tests hold is:

* every step A1-A9 does what §4.2 says, and records itself on the record;
* the act-as grant the operator path lacks (§0) is made, for the scheduler and
  the reconciler, and nothing a step makes is made twice on a re-run;
* a call the guard refuses ends the run as `needs_owner`, with nothing the call
  would have done done;
* `--mode limits` makes no IAM call, and `--mode verify` writes nothing but the
  record and names the type of a missing object;
* A1 writes nothing for a record that is not admissible.

The harness runs the REAL script behind the REAL call guard: scripts/lib/guard-bin
first on PATH, and the "real" gcloud, kubectl and curl behind it are
fixtures/workspace_world.py, a fake project and cluster in one JSON file. So
every call the script, kubernetes/apply.sh and the network parity check make
must also pass scripts/lib/workspace-calls.json -- the script and the guard
cannot drift apart without this file going red (§2.5).
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "scripts" / "register-tenant.sh"
GUARD = REPO / "scripts" / "lib" / "workspace-guard.sh"
SHIMS = REPO / "scripts" / "lib" / "guard-bin"
WORLD_PY = Path(__file__).resolve().parent / "fixtures" / "workspace_world.py"
DEPLOYER_RBAC = REPO / "kubernetes" / "rbac" / "provisioner-rbac.yaml"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

PROJECT = "saga-agents-staging"
WORKSPACE = "w-3f9a2c"
PRINCIPAL = "alice@saga.xyz"
TENANT = "u-alice"
ADMIN = "root@saga.xyz"
WORKER = f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER = f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com"
RECONCILER = f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com"
NAMESPACE = f"swarm-tenant-{TENANT}"
BUCKET = f"swarm-artifacts-{PROJECT}"
SLOT = f"swarm-tenant-{TENANT}-git-u-" + hashlib.sha256(PRINCIPAL.encode()).hexdigest()[:16]
TWIN = SLOT + "-refresh"
STOPPED = 3

WORKLOAD = "roles/iam.workloadIdentityUser"
ACT_AS = "roles/iam.serviceAccountUser"
OBJECT_PREFIX = f'resource.name.startsWith("projects/_/buckets/{BUCKET}/objects/tenants/{TENANT}/")'
READ_EXPR = (
    OBJECT_PREFIX + ' || api.getAttribute("storage.googleapis.com/objectListPrefix", "")'
    f'.startsWith("tenants/{TENANT}/")'
)
WRITE_EXPR = (
    OBJECT_PREFIX
    + f' && !resource.name.startsWith("projects/_/buckets/{BUCKET}/objects/tenants/{TENANT}/verdicts/")'
)


def _s(value: str) -> dict:
    return {"stringValue": value}


def _record(state: str = "approved", **extra: dict) -> dict:
    fields = {
        "tenant_id": _s(TENANT),
        "workspace_id": _s(WORKSPACE),
        "principal": _s(PRINCIPAL),
        "state": _s(state),
        "request_id": _s("6f1c2d3e-0000-4000-8000-000000000001"),
        "decision": {"mapValue": {"fields": {
            "by": _s(ADMIN), "verdict": _s("approved"), "at": {"timestampValue": "2026-10-08T08:00:00Z"}}}},
        "limits": {"mapValue": {"fields": {
            "max_active": {"integerValue": "8"}, "capacity_units": {"integerValue": "8"},
            "quota_pods": {"integerValue": "16"}, "quota_cpu": {"integerValue": "64"}}}},
    }
    fields.update(extra)
    return {"fields": fields, "updateTime": "2026-10-08T08:00:00.000001Z"}


def _world() -> dict:
    uid = 100000000000000000001
    return {
        "project": PROJECT,
        "database": "swarm",
        "accounts": {
            SCHEDULER: {"policy": {"version": 1}, "keys": [], "uid": str(uid)},
            RECONCILER: {"policy": {"version": 1}, "keys": [], "uid": str(uid + 1)},
        },
        "bucket_policy": {"version": 3, "etag": "BwX=", "bindings": [
            {"role": "roles/storage.objectViewer", "members": ["serviceAccount:swarm-api@x.iam.gserviceaccount.com"]}]},
        "project_policy": {"version": 1, "bindings": []},
        "objects": {},
        "secrets": {},
        "k8s": {},
        "firestore": {
            f"workspaces/{TENANT}": _record(),
            f"admin_roles/{ADMIN}": {"fields": {"role": _s("admin")}, "updateTime": "2026-10-01T00:00:00Z"},
        },
        "fail": [],
    }


class Job:
    """One workspace job's machine: the guard installed, a fake world behind it."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "real-bin"
        self.bin.mkdir()
        for tool in ("gcloud", "kubectl", "curl"):
            path = self.bin / tool
            path.write_text(f'#!/bin/sh\nFAKE_TOOL={tool} exec "{sys.executable}" "{WORLD_PY}" "$@"\n')
            path.chmod(0o755)
        self.world_file = tmp / "world.json"
        self.write(_world())
        (tmp / "home").mkdir()
        (tmp / "tmp").mkdir()
        guard_dir = tmp / "guard"
        guard_dir.mkdir()
        self.expect = guard_dir / "expect.json"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SWARM_", "CLOUDSDK_", "GOOGLE_", "KUBECONFIG", "BUILD_ID"))}
        env.update(
            PATH=os.pathsep.join([str(SHIMS), str(self.bin), env.get("PATH", "/usr/bin:/bin")]),
            HOME=str(tmp / "home"),
            TMPDIR=str(tmp / "tmp"),
            SWARM_ENV_FILE=str(tmp / "no.env"),
            PROJECT_ID=PROJECT,
            REGION="us-central1",
            ENVIRONMENT="dev",
            FIRESTORE_DATABASE="swarm",
            GKE_CLUSTER="swarm-autopilot",
            GKE_LOCATION="us-central1",
            SWARM_CALL_GUARD=str(self.expect),
            SWARM_CALL_GUARD_ENFORCE="1",
            SWARM_WORKSPACE_RETRY_DELAYS="0 0 0",
            FAKE_WORLD=str(self.world_file),
            BUILD_ID="b-0001",
            NO_COLOR="1",
        )
        env.pop("ARTIFACT_BUCKET", None)
        self.env = env

    # -- the world ---------------------------------------------------------
    def read(self) -> dict:
        return json.loads(self.world_file.read_text())

    def write(self, world: dict) -> None:
        self.world_file.write_text(json.dumps(world))

    def edit(self, change) -> None:
        world = self.read()
        change(world)
        self.write(world)

    def record(self) -> dict:
        return self.read()["firestore"][f"workspaces/{TENANT}"]["fields"]

    def doc(self, path: str) -> dict | None:
        found = self.read()["firestore"].get(path)
        return None if found is None else found["fields"]

    def calls(self, tool: str | None = None, since: int = 0) -> list[list[str]]:
        return [c["argv"] for c in self.read().get("calls", [])[since:] if tool in (None, c["tool"])]

    def call_count(self) -> int:
        return len(self.read().get("calls", []))

    # -- the job ------------------------------------------------------------
    def init_guard(self) -> None:
        if self.expect.exists():
            self.expect.unlink()
        Path(str(self.expect) + ".stop").unlink(missing_ok=True)
        done = subprocess.run([str(GUARD), "init", "--workspace-id", WORKSPACE], env=self.env,
                              capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, done.stderr

    def run(self, *args: str, env: dict | None = None, init: bool = True) -> subprocess.CompletedProcess[str]:
        if init:
            self.init_guard()
        return subprocess.run([str(REGISTER), *args], env=env or self.env, capture_output=True,
                              text=True, timeout=900, check=False)

    def create(self, **kw) -> subprocess.CompletedProcess[str]:
        return self.run("--workspace", WORKSPACE, **kw)

    def stopped(self) -> bool:
        return Path(str(self.expect) + ".stop").exists()


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return f"exit {proc.returncode}\n--- stdout\n{proc.stdout[-4000:]}\n--- stderr\n{proc.stderr[-12000:]}"


def _field(fields: dict, *path: str):
    node: object = {"mapValue": {"fields": fields}}
    for part in path:
        node = (node.get("mapValue") or {}).get("fields", {}).get(part)  # type: ignore[union-attr]
        if node is None:
            return None
    value = node
    for typed in ("stringValue", "integerValue", "booleanValue", "timestampValue"):
        if isinstance(value, dict) and typed in value:
            return value[typed]
    return value


def _steps(fields: dict) -> dict[str, str]:
    steps = (fields.get("steps") or {}).get("mapValue", {}).get("fields", {})
    return {k: v["mapValue"]["fields"]["state"]["stringValue"] for k, v in steps.items()}


def _members(policy: dict, role: str) -> set[str]:
    return {m for b in policy.get("bindings", []) if b["role"] == role for m in b["members"]}


def _changes(calls: list[list[str]]) -> list[list[str]]:
    """gcloud calls that change something."""
    verbs = {"create", "add-iam-policy-binding", "cp", "remove-iam-policy-binding", "set-iam-policy", "delete"}
    return [c for c in calls if any(w in verbs for w in c[:4])]


@pytest.fixture()
def job(tmp_path: Path) -> Job:
    return Job(tmp_path)


@pytest.fixture(scope="session")
def created(tmp_path_factory) -> dict:
    """ONE full create per test session, shared by every xdist worker.

    A full run is ~40 s of guard judging, and a module fixture would make it
    once per worker. The first worker to take the lock runs it and leaves the
    result beside the lock; the others read it.
    """
    # Under xdist each worker's basetemp is popen-gwN inside the session's
    # directory, so its parent is shared by this session's workers and no
    # other. Without xdist the parent is pytest's root for EVERY session, and a
    # result left there would be read back by the next run, whatever it changed.
    root = tmp_path_factory.getbasetemp()
    if os.environ.get("PYTEST_XDIST_WORKER"):
        root = root.parent
    result = root / "register-tenant-workspace-created.json"
    with open(root / "register-tenant-workspace-created.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not result.exists():
            first = Job(Path(tmp_path_factory.mktemp("created")))
            proc = first.create()
            result.write_text(json.dumps({
                "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
                "stopped": first.stopped(), "world": first.read()}))
    return json.loads(result.read_text())


@pytest.fixture()
def ready_world(created: dict) -> dict:
    """The world a successful create leaves, with its call log cleared."""
    assert created["returncode"] == 0, created["stderr"][-6000:]
    world = copy.deepcopy(created["world"])
    world["calls"] = []
    world["writes"] = []
    return world


def _from_ready(job: Job, ready_world: dict) -> None:
    job.write(copy.deepcopy(ready_world))


# ---------------------------------------------------------------------------
# --mode create: A1-A9
# ---------------------------------------------------------------------------


def test_create_runs_every_step_and_ends_ready(job: Job, created: dict) -> None:
    proc = subprocess.CompletedProcess([], created["returncode"], created["stdout"], created["stderr"])
    out = _out(proc)
    assert proc.returncode == 0, out
    assert not created["stopped"], f"the guard refused a call the job needs:\n{out}"
    job.write(created["world"])
    world = job.read()
    record = job.record()
    assert _field(record, "state") == "ready", out
    assert _field(record, "ready_at"), out
    assert _steps(record) == {f"A{i}": "done" for i in range(1, 10)}, out
    assert _field(record, "run", "build_id") == "b-0001"
    assert _field(record, "run", "attempt") == "1"
    assert _field(record, "run", "finished_at")
    assert record.get("failure") is None
    assert "verified: " in proc.stderr and " of " in proc.stderr, out

    # A3: the account, named for the workspace id and never for the person.
    account = world["accounts"][WORKER]
    assert WORKSPACE in account["display_name"] and PRINCIPAL not in account["description"], account
    # A4: Workload Identity for both KSAs, and the act-as grant the operator
    # path lacks, for the scheduler and the reconciler.
    assert _members(account["policy"], WORKLOAD) == {
        f"serviceAccount:{PROJECT}.svc.id.goog[{NAMESPACE}/swarm-agent-worker]",
        f"serviceAccount:{PROJECT}.svc.id.goog[{NAMESPACE}/swarm-worker]",
    }
    assert _members(account["policy"], ACT_AS) == {f"serviceAccount:{SCHEDULER}", f"serviceAccount:{RECONCILER}"}
    assert {b["role"] for b in account["policy"]["bindings"]} == {WORKLOAD, ACT_AS}

    # A5: the two prefix-conditioned grants, exactly the expressions C4 accepts.
    grants = {(b["role"], (b.get("condition") or {}).get("expression"))
              for b in world["bucket_policy"]["bindings"] if f"serviceAccount:{WORKER}" in b["members"]}
    assert grants == {("roles/storage.objectViewer", READ_EXPR), ("roles/storage.objectUser", WRITE_EXPR)}
    assert f"gs://{BUCKET}/tenants/{TENANT}/.tenant" in world["objects"]
    assert world["project_policy"]["bindings"] == [], "WD9 is a principal-set grant: no project IAM per person"

    # A6: the slot and its twin, labelled as swarm-api labels its own; the
    # worker reads the slot and never the twin.
    assert set(world["secrets"]) == {SLOT, TWIN}
    assert world["secrets"][SLOT]["labels"]["tenant"] == TENANT
    assert world["secrets"][TWIN]["labels"]["provider"].endswith("-refresh")
    assert _members(world["secrets"][SLOT]["policy"], "roles/secretmanager.secretAccessor") == {
        f"serviceAccount:{WORKER}"}
    assert world["secrets"][TWIN]["policy"].get("bindings", []) == []

    # A7: the namespace and its objects, with §8's quota.
    quota = world["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    assert (quota["pods"], quota["requests.cpu"], quota["requests.memory"]) == ("16", "64", "128Gi"), quota
    assert f"Namespace//{NAMESPACE}" in world["k8s"]
    assert f"NetworkPolicy/{NAMESPACE}/swarm-default-deny" in world["k8s"]
    assert all(k.split("/")[1] in ("", NAMESPACE) for k in world["k8s"]), sorted(world["k8s"])

    # A8: the documents, with §8's limits; a new pool holds nothing.
    tenant = job.doc(f"tenants/{TENANT}")
    assert tenant is not None
    assert (_field(tenant, "kind"), _field(tenant, "principal"), _field(tenant, "max_active"),
            _field(tenant, "capacity_units"), _field(tenant, "service_account"),
            _field(tenant, "namespace")) == ("user", PRINCIPAL, "8", "8", WORKER, NAMESPACE)
    assert tenant["credentials"] == {"arrayValue": {"values": []}}, "no provider secret for a new person (§8)"
    pool = job.doc(f"pools/tenant:{TENANT}")
    assert (_field(pool, "hard_limit"), _field(pool, "active")) == ("8", "0")

    # Nothing it ran removes, replaces or deletes anything.
    for argv in job.calls("gcloud"):
        assert not {"remove-iam-policy-binding", "set-iam-policy", "delete", "disable"} & set(argv), argv
    for argv in job.calls("kubectl"):
        assert "delete" not in argv and "--prune" not in argv, argv
    # Firestore writes reach exactly the three documents the guard allows.
    assert set(world["writes"]) == {f"workspaces/{TENANT}", f"tenants/{TENANT}", f"pools/tenant:{TENANT}"}


#: kind -> (API group, resource), and kubectl's spellings of each.
KUBE_KINDS = {
    "Namespace": ("", "namespaces"),
    "ServiceAccount": ("", "serviceaccounts"),
    "ResourceQuota": ("", "resourcequotas"),
    "LimitRange": ("", "limitranges"),
    "NetworkPolicy": ("networking.k8s.io", "networkpolicies"),
    "Role": ("rbac.authorization.k8s.io", "roles"),
    "RoleBinding": ("rbac.authorization.k8s.io", "rolebindings"),
    "Service": ("", "services"),
    "DaemonSet": ("apps", "daemonsets"),
}
KUBE_WORDS = {
    "namespace": "Namespace", "namespaces": "Namespace", "ns": "Namespace",
    "serviceaccount": "ServiceAccount", "serviceaccounts": "ServiceAccount", "sa": "ServiceAccount",
    "resourcequota": "ResourceQuota", "resourcequotas": "ResourceQuota", "quota": "ResourceQuota",
    "limitrange": "LimitRange", "limitranges": "LimitRange", "limits": "LimitRange",
    "networkpolicy": "NetworkPolicy", "networkpolicies": "NetworkPolicy", "netpol": "NetworkPolicy",
    "role": "Role", "roles": "Role", "rolebinding": "RoleBinding", "rolebindings": "RoleBinding",
    "service": "Service", "services": "Service", "svc": "Service",
    "daemonset": "DaemonSet", "daemonsets": "DaemonSet", "ds": "DaemonSet",
}
KUBE_VALUE_FLAGS = {"--context", "-n", "--namespace", "-o", "--output", "-f", "--filename", "--dry-run",
                    "--raw", "--field-selector", "-l", "--selector", "--validate", "--request-timeout"}


def _kube_parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(argv):
        name, eq, value = argv[i].partition("=")
        if argv[i].startswith("-") and argv[i] != "-":
            if not eq and name in KUBE_VALUE_FLAGS and i + 1 < len(argv):
                value, i = argv[i + 1], i + 1
            flags[name] = value
        else:
            pos.append(argv[i])
        i += 1
    return pos, flags


def test_the_deployer_rbac_is_exactly_what_a_run_asks_for(created: dict) -> None:
    """W5's grant against W6's calls: every (group, resource, verb) the
    ClusterRole holds is one a run needs, and every one a run needs is held.

    kubectl's verbs are read off the calls the run made. `apply -f -` is a GET
    of each object, then a CREATE (absent) or a PATCH (present: every re-run,
    and every --mode limits) -- so each kind the run applied needs all three.
    Creating a Role whose rules the caller does not hold needs `escalate`;
    creating a RoleBinding needs `bind` on the Role it references, by name.
    `get --raw=/readyz` is a non-resource URL every authenticated caller may
    read, so it needs nothing here."""
    assert created["returncode"] == 0, created["stderr"][-6000:]
    world = created["world"]
    calls = [c["argv"] for c in world["calls"] if c["tool"] == "kubectl"]
    assert calls, "the run made no kubectl call; nothing was compared"

    needed: set[tuple[str, str, str]] = set()
    kube_system_reads: set[tuple[str, str, str]] = set()
    applied = 0
    for argv in calls:
        pos, flags = _kube_parse(argv)
        command = pos[:1]
        if command in (["version"], ["config"]):
            continue
        assert command in (["get"], ["apply"]), f"a kubectl verb this test does not map to RBAC: {argv}"
        if command == ["apply"]:
            applied += 1
            continue
        if "--raw" in flags:
            continue
        assert len(pos) == 3 and not {"--field-selector", "-l", "--selector", "-A", "--all-namespaces"} & set(flags), (
            f"a list, which the deployer's ClusterRole cannot make: {argv}")
        group, resource = KUBE_KINDS[KUBE_WORDS[pos[1]]]
        if flags.get("-n", flags.get("--namespace")) == "kube-system":
            kube_system_reads.add((group, resource, pos[2]))
        else:
            needed.add((group, resource, "get"))
    assert applied, "the run applied nothing; the apply's verbs were not compared"

    bound: set[str] = set()
    for name, obj in world["k8s"].items():
        kind = name.split("/", 1)[0]
        group, resource = KUBE_KINDS[kind]
        needed |= {(group, resource, verb) for verb in ("get", "create", "patch")}
        if kind == "Role":
            needed.add((group, resource, "escalate"))
        if kind == "RoleBinding":
            assert obj["roleRef"]["kind"] == "Role", obj["roleRef"]
            bound.add(obj["roleRef"]["name"])

    docs = [d for d in yaml.safe_load_all(DEPLOYER_RBAC.read_text()) if d]
    cluster_role = next(d for d in docs if d["kind"] == "ClusterRole")
    granted: set[tuple[str, str, str]] = set()
    bind_names: set[str] = set()
    for rule in cluster_role["rules"]:
        triples = {(g, r, v) for g in rule["apiGroups"] for r in rule["resources"] for v in rule["verbs"]}
        if "resourceNames" in rule:
            assert {v for _, _, v in triples} == {"bind"}, rule
            bind_names |= set(rule["resourceNames"])
        else:
            granted |= triples
    assert granted == needed, (
        f"granted and never asked for: {sorted(granted - needed)}; "
        f"asked for and not granted: {sorted(needed - granted)}")
    assert bind_names == bound

    dns_role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["namespace"] == "kube-system")
    dns_granted = {(g, r, n) for rule in dns_role["rules"] for g in rule["apiGroups"]
                   for r in rule["resources"] for n in rule["resourceNames"]}
    assert all(rule["verbs"] == ["get"] for rule in dns_role["rules"]), dns_role["rules"]
    assert dns_granted == kube_system_reads


def test_a_rerun_after_a_failure_makes_only_what_is_missing(job: Job) -> None:
    """A transient failure at A4 is asked again and passes; A6 fails past its
    retries; an admin retries; the second run adds nothing twice."""
    job.edit(lambda w: w["fail"].extend([
        {"tool": "gcloud", "words": ["iam", "service-accounts", "add-iam-policy-binding"], "times": 1,
         "stderr": "ERROR: There were concurrent policy changes. Please retry the whole read-modify-write"},
        {"tool": "gcloud", "words": ["secrets", "create"], "times": 4,
         "stderr": "ERROR: (gcloud.secrets.create) HTTPError 503: unavailable"}]))
    first = job.create()
    out = _out(first)
    assert first.returncode == 1, out
    assert "asking again (1 of 3)" in first.stderr, out
    assert _members(job.read()["accounts"][WORKER]["policy"], ACT_AS) == {
        f"serviceAccount:{SCHEDULER}", f"serviceAccount:{RECONCILER}"}, "the retried binding landed"
    record = job.record()
    assert _field(record, "state") == "failed", out
    assert _field(record, "failure", "step") == "A6"
    assert _field(record, "failure", "code") == "GRANT_FAILED"
    assert _field(record, "failure", "retryable") is True
    steps = _steps(record)
    assert [steps[f"A{i}"] for i in range(1, 7)] == ["done"] * 5 + ["failed"], steps
    assert steps["A7"] == "todo"
    tries = [c for c in job.calls("gcloud") if c[:2] == ["secrets", "create"]]
    assert len(tries) == 4, f"a 503 is retried 3 times after the first try (§2.2): {tries}"

    # Without an admin's retry recorded, a second build does nothing.
    before = job.call_count()
    again = job.create()
    assert again.returncode == 1 and "NOT_RETRIED" in again.stderr, _out(again)
    assert _changes(job.calls("gcloud", since=before)) == []
    assert _field(job.record(), "state") == "failed"

    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(retry={"mapValue": {"fields": {
        "by": _s(ADMIN), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}}))
    before = job.call_count()
    second = job.create()
    out = _out(second)
    assert second.returncode == 0, out
    assert _field(job.record(), "state") == "ready"
    assert _field(job.record(), "run", "attempt") == "2"
    changed = _changes(job.calls("gcloud", since=before))
    assert all(c[:2] == ["secrets", "create"] or (c[:2] == ["secrets", "add-iam-policy-binding"])
               or c[:2] == ["storage", "cp"] for c in changed), (
        f"the re-run remade what the first run had made: {changed}")
    assert not any(c[:3] == ["iam", "service-accounts", "create"] for c in changed)
    assert not any("add-iam-policy-binding" in c and c[0] in ("iam", "storage") for c in changed)


def test_a_full_rerun_on_a_complete_workspace_changes_nothing(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        state=_s("failed"),
        retry={"mapValue": {"fields": {"by": _s(ADMIN), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}}))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 0, out
    changed = [c for c in _changes(job.calls("gcloud")) if c[:2] != ["storage", "cp"]]
    assert changed == [], f"every object existed, yet the re-run changed: {changed}"
    assert _field(job.record(), "state") == "ready"


def test_an_existing_account_with_a_foreign_binding_is_not_adopted(job: Job) -> None:
    job.edit(lambda w: w["accounts"].update({WORKER: {"uid": "100000000000000000099", "keys": [], "policy": {
        "bindings": [{"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:mallory@example.com"]}]}}}))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = job.record()
    assert _field(record, "state") == "failed"
    assert (_field(record, "failure", "step"), _field(record, "failure", "code")) == ("A2", "IDENTITY_NOT_OURS")
    assert _field(record, "failure", "retryable") is False
    assert _changes(job.calls("gcloud")) == [], "nothing is made for an account somebody else may control"
    assert "serviceAccountTokenCreator" in proc.stderr


def test_an_existing_account_with_a_user_key_is_not_adopted(job: Job) -> None:
    job.edit(lambda w: w["accounts"].update({WORKER: {"uid": "100000000000000000099", "policy": {},
                                                       "keys": ["projects/p/serviceAccounts/x/keys/k1"]}}))
    proc = job.create()
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "code") == "IDENTITY_NOT_OURS"


def test_an_unreachable_cluster_fails_retryable_at_the_namespace(job: Job) -> None:
    job.edit(lambda w: w.update(cluster_down=True))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = job.record()
    assert (_field(record, "failure", "step"), _field(record, "failure", "code")) == ("A7", "CLUSTER_UNREACHABLE")
    assert _field(record, "failure", "retryable") is True
    assert job.read()["k8s"] == {}
    assert job.doc(f"tenants/{TENANT}") is None, "A8 runs only after the namespace (§4.2's order)"


# ---------------------------------------------------------------------------
# a guard refusal is needs_owner
# ---------------------------------------------------------------------------


def test_a_guard_refusal_stops_the_run_as_needs_owner(job: Job) -> None:
    """kubeconfig's current context is the other team's cluster at A7.

    kubernetes/apply.sh's client-side dry run names no --context, so the guard
    judges the kubeconfig's current one, refuses it by the shared deny-list,
    and the latch refuses everything after; the script records needs_owner.
    """
    job.edit(lambda w: w.update(kube_current_context="gke_saga-agents-staging_us-central1-a_agents-staging"))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == STOPPED, out
    assert job.stopped()
    assert "REFUSED" in proc.stderr
    record = job.record()
    assert _field(record, "state") == "needs_owner", out
    steps = _steps(record)
    assert steps["A7"] == "held", steps
    assert _field(record, "steps", "A7", "code").startswith("C"), record["steps"]
    assert record.get("failure") is None, "a guard stop is not a failure code (§4.2)"
    assert _field(record, "run", "finished_at")
    world = job.read()
    assert world["k8s"] == {}, "nothing the refused call would have done has happened"
    assert job.doc(f"tenants/{TENANT}") is None
    stop = json.loads(Path(str(job.expect) + ".stop").read_text())
    assert stop["workspace_id"] == WORKSPACE and stop["state"] == "needs_owner"


def test_a_stale_bucket_binding_goes_to_the_owner_and_is_never_removed(job: Job) -> None:
    job.edit(lambda w: w["bucket_policy"]["bindings"].append(
        {"role": "roles/storage.objectUser", "members": [f"serviceAccount:{WORKER}"]}))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == STOPPED, out
    record = job.record()
    assert _field(record, "state") == "needs_owner"
    assert _field(record, "steps", "A5", "code") == "STALE_BUCKET_BINDING"
    assert not any("remove-iam-policy-binding" in c for c in job.calls("gcloud"))


def test_a_needs_owner_record_runs_again_only_after_an_admin_retry(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(state=_s("needs_owner")))
    proc = job.create()
    assert proc.returncode == 1 and "NOT_RETRIED" in proc.stderr, _out(proc)
    assert job.read()["writes"] == []


# ---------------------------------------------------------------------------
# --mode limits and --mode verify
# ---------------------------------------------------------------------------


def test_limits_reapplies_the_quota_and_the_documents_and_makes_no_iam_call(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)

    def raise_ceiling(world: dict) -> None:
        limits = world["firestore"][f"workspaces/{TENANT}"]["fields"]["limits"]["mapValue"]["fields"]
        limits.update(max_active={"integerValue": "12"}, capacity_units={"integerValue": "12"},
                      quota_pods={"integerValue": "24"}, quota_cpu={"integerValue": "96"})
        world["firestore"][f"pools/tenant:{TENANT}"]["fields"]["active"] = {"integerValue": "3"}

    job.edit(raise_ceiling)
    proc = job.run("--workspace", WORKSPACE, "--mode", "limits")
    out = _out(proc)
    assert proc.returncode == 0, out
    world = job.read()
    quota = world["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    assert (quota["pods"], quota["requests.cpu"]) == ("24", "96"), quota
    assert _field(job.doc(f"tenants/{TENANT}"), "max_active") == "12"
    pool = job.doc(f"pools/tenant:{TENANT}")
    assert (_field(pool, "hard_limit"), _field(pool, "active")) == ("12", "3"), "active is never written"
    assert _field(job.record(), "state") == "ready"
    assert _changes(job.calls("gcloud")) == []
    iam = [c for c in job.calls("gcloud") if c[0] in ("iam", "secrets", "storage", "projects")
           and c[:3] != ["iam", "service-accounts", "describe"]
           and c[:3] != ["iam", "service-accounts", "get-iam-policy"]]
    assert iam == [], f"a ceiling change makes no IAM call (§2.2): {iam}"
    assert set(_steps(job.record())) >= {"A1", "A7", "A8"}


def test_limits_refuses_a_workspace_that_is_not_ready(job: Job) -> None:
    proc = job.run("--workspace", WORKSPACE, "--mode", "limits")
    assert proc.returncode == 1 and "NOT_READY" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


def test_verify_reads_everything_and_changes_nothing_but_the_record(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _changes(job.calls("gcloud")) == []
    assert not any(c[:1] == ["apply"] for c in job.calls("kubectl"))
    assert set(job.read()["writes"]) == {f"workspaces/{TENANT}"}
    assert _field(job.record(), "state") == "ready"
    assert _steps(job.record())["A9"] == "done"


def test_verify_names_the_type_of_a_missing_object_never_its_name(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(lambda w: w["k8s"].pop(f"NetworkPolicy/{NAMESPACE}/swarm-allow-worker-egress"))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    out = _out(proc)
    assert proc.returncode == 1, out
    record = job.record()
    assert _field(record, "state") == "failed"
    assert (_field(record, "failure", "step"), _field(record, "failure", "code"),
            _field(record, "failure", "object")) == ("A9", "VERIFY_FAILED", "NetworkPolicy")


def test_verify_finds_a_lost_act_as_grant(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)

    def drop(world: dict) -> None:
        policy = world["accounts"][WORKER]["policy"]
        policy["bindings"] = [b for b in policy["bindings"] if b["role"] != ACT_AS]

    job.edit(drop)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "service account binding"


def test_verify_of_a_migrated_record_needs_no_approval_and_allows_the_deployer(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    deployer = f"serviceAccount:swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"

    def migrate(world: dict) -> None:
        fields = world["firestore"][f"workspaces/{TENANT}"]["fields"]
        fields.pop("decision")
        fields["migrated"] = {"booleanValue": True}
        world["accounts"][WORKER]["policy"]["bindings"].append(
            {"role": "roles/iam.serviceAccountAdmin", "members": [deployer]})

    job.edit(migrate)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 0, _out(proc)
    # The same binding on a record that is not migrated is foreign.
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        migrated={"booleanValue": False}, decision=_record()["fields"]["decision"]))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "service account binding"


def test_verify_of_a_migrated_record_does_not_require_the_legacy_ksa_binding(job: Job, ready_world: dict) -> None:
    # A Terraform-made tenant (u-bogdan, W9) binds Workload Identity for
    # swarm-agent-worker only; the legacy swarm-worker is rendered only where
    # IAM binds it (kubernetes/README.md). Measured 2026-10-10: A9 failed
    # w-752763 on exactly this binding.
    _from_ready(job, ready_world)
    legacy = f"serviceAccount:{PROJECT}.svc.id.goog[{NAMESPACE}/swarm-worker]"

    def migrate_without_legacy(world: dict) -> None:
        fields = world["firestore"][f"workspaces/{TENANT}"]["fields"]
        fields.pop("decision")
        fields["migrated"] = {"booleanValue": True}
        for b in world["accounts"][WORKER]["policy"]["bindings"]:
            if b["role"] == WORKLOAD:
                b["members"] = [m for m in b["members"] if m != legacy]

    job.edit(migrate_without_legacy)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 0, _out(proc)
    # A workspace the job made still needs both.
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        migrated={"booleanValue": False}, decision=_record()["fields"]["decision"]))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "service account binding"


# ---------------------------------------------------------------------------
# A1: nothing is written for a record that is not admissible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change,needle", [
    (lambda f: f.update(state=_s("requested")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f.update(state=_s("denied")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f["decision"]["mapValue"]["fields"].update(by=_s("mallory@saga.xyz")), "not an admin"),
    (lambda f: f["decision"]["mapValue"]["fields"].update(verdict=_s("denied")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f.update(tenant_id=_s("u-bob")), "WORKSPACE_ID_TAKEN"),
    (lambda f: f.update(principal=_s("bob@saga.xyz")), "WORKSPACE_ID_TAKEN"),
], ids=["requested", "denied", "approver-not-admin", "verdict-denied", "tenant-edited", "principal-edited"])
def test_a1_writes_nothing_for_a_record_it_must_not_run(job: Job, change, needle: str) -> None:
    job.edit(lambda w: change(w["firestore"][f"workspaces/{TENANT}"]["fields"]))
    before = copy.deepcopy(job.record())
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    assert needle in proc.stderr, out
    assert job.read().get("writes", []) == [], out
    assert job.record() == before
    assert _changes(job.calls("gcloud")) == []


def test_a1_finds_no_record_and_writes_nothing(job: Job) -> None:
    job.edit(lambda w: w["firestore"].pop(f"workspaces/{TENANT}"))
    proc = job.create()
    assert proc.returncode == 1 and "WORKSPACE_NOT_FOUND" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


def test_a1_leaves_a_live_run_alone(job: Job) -> None:
    def live(world: dict) -> None:
        fields = world["firestore"][f"workspaces/{TENANT}"]["fields"]
        fields["state"] = _s("applying")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields["run"] = {"mapValue": {"fields": {"build_id": _s("b-other"), "claimed_at": {"timestampValue": now}}}}

    job.edit(live)
    proc = job.create()
    assert proc.returncode == 0 and "has not finished" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


def test_a1_reclaims_a_dead_run(job: Job) -> None:
    """Claimed hours ago and never finished: the build died. A2 is made to fail
    (an unreadable account, past its retries) so the test stops there."""
    job.edit(lambda w: (w["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        state=_s("applying"),
        run={"mapValue": {"fields": {"build_id": _s("b-dead"), "attempt": {"integerValue": "1"},
                                     "claimed_at": {"timestampValue": "2026-10-08T01:00:00Z"}}}}),
        w["fail"].append({"tool": "gcloud", "words": ["iam", "service-accounts", "describe"], "times": 4,
                          "stderr": "ERROR: (gcloud.iam) code=503 UNAVAILABLE"})))
    proc = job.create()
    assert proc.returncode == 1, _out(proc)
    record = job.record()
    assert (_field(record, "run", "attempt"), _field(record, "run", "build_id")) == ("2", "b-0001")
    assert (_field(record, "failure", "step"), _field(record, "failure", "code")) == ("A2", "APPLY_FAILED")


def test_a_create_for_a_ready_workspace_is_a_no_op(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    proc = job.create()
    assert proc.returncode == 0 and "nothing to create" in proc.stderr, _out(proc)
    assert job.read()["writes"] == []


# ---------------------------------------------------------------------------
# it never runs unguarded, and the operator paths are untouched
# ---------------------------------------------------------------------------


def test_it_refuses_to_start_without_the_guard_on_path(job: Job) -> None:
    env = dict(job.env, PATH=os.pathsep.join([str(job.bin), job.env["PATH"].split(os.pathsep, 2)[2]]))
    proc = job.run("--workspace", WORKSPACE, env=env)
    assert proc.returncode == 1 and "guard's shim" in proc.stderr, _out(proc)
    assert job.calls() == []


def test_it_refuses_to_start_without_an_expectation(job: Job) -> None:
    env = {k: v for k, v in job.env.items() if k != "SWARM_CALL_GUARD"}
    proc = job.run("--workspace", WORKSPACE, env=env, init=False)
    assert proc.returncode == 1 and "SWARM_CALL_GUARD is not set" in proc.stderr, _out(proc)
    assert job.calls() == []


def test_it_refuses_an_expectation_for_another_workspace(job: Job) -> None:
    job.init_guard()
    proc = job.run("--workspace", "w-000000", init=False)
    assert proc.returncode == 1 and "one job guards one workspace" in proc.stderr, _out(proc)
    assert job.calls() == []


@pytest.mark.parametrize("extra", [
    ["--user", PRINCIPAL], ["--group", "eng@saga.xyz"], ["--tenant", TENANT], ["--providers", "anthropic"],
    ["--max-active", "40"], ["--add-provider", "git"], ["--dry-run"], ["--skip-k8s"],
])
def test_operator_flags_are_refused_beside_workspace(job: Job, extra: list[str]) -> None:
    proc = job.run("--workspace", WORKSPACE, *extra)
    assert proc.returncode == 1 and "takes its every value from the approved record" in proc.stderr, _out(proc)
    assert job.calls() == []


@pytest.mark.parametrize("args,needle", [
    (["--workspace", "u-alice"], "w-<6 hex digits>"),
    (["--workspace", WORKSPACE, "--mode", "destroy"], "--mode must be create, limits or verify"),
    (["--user", PRINCIPAL, "--mode", "limits"], "--mode belongs to --workspace"),
])
def test_malformed_workspace_arguments_are_refused(job: Job, args: list[str], needle: str) -> None:
    proc = job.run(*args)
    assert proc.returncode == 1 and needle in proc.stderr, _out(proc)
    assert job.calls() == []


APPLY = REPO / "scripts" / "workspace-apply.sh"


def test_the_job_script_runs_the_script_behind_the_guard() -> None:
    """scripts/workspace-apply.sh, steps 1 to 3 of the Cloud Run job (W6b),
    replaced the Cloud Build file line for line (WD2 re-decided 2026-10-10)."""
    assert not (REPO / "scripts" / "cloudbuild" / "workspace-apply.yaml").exists()
    script = APPLY.read_text()
    assert os.access(APPLY, os.X_OK)
    assert '"${SCRIPTS_DIR}/register-tenant.sh" --workspace' in script
    assert "guard-bin" in script and "SWARM_CALL_GUARD_ENFORCE" in script
    assert '"${GUARD}" self-test' in script and '"${GUARD}" init --workspace-id' in script
    assert "google-cloud-cli:" not in script and "jq-linux" not in script, "no stock image, no downloaded jq"


@pytest.mark.parametrize("args", [
    [],
    [WORKSPACE],
    [WORKSPACE, "create", "extra"],
    ["create", WORKSPACE],
    ["u-alice", "create"],
    ["w-3F9A2C", "create"],
    [WORKSPACE, "verify"],
    [WORKSPACE, "destroy"],
    ["--workspace", WORKSPACE],
])
def test_the_job_script_refuses_anything_but_an_id_and_a_mode(job: Job, args: list[str]) -> None:
    """Step 1: before the guard is installed or the script runs, nothing is
    called and no expectation file is written."""
    proc = subprocess.run([str(APPLY), *args], env=job.env, capture_output=True, text=True,
                          timeout=120, check=False)
    assert proc.returncode == 1 and "refused:" in proc.stderr, _out(proc)
    assert job.calls() == []
    assert not job.expect.exists()


def _stub_tree(tmp: Path) -> Path:
    """workspace-apply.sh beside the real lib/, with the guard and
    register-tenant.sh stubbed: each logs what it was asked and exits as told."""
    scripts = tmp / "tree" / "scripts"
    shutil.copytree(REPO / "scripts" / "lib", scripts / "lib")
    shutil.copy2(APPLY, scripts / "workspace-apply.sh")
    log = tmp / "stub.log"
    guard = scripts / "lib" / "workspace-guard.sh"
    guard.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
echo "guard $*" >> "{log}"
case "$1" in
  self-test) exit "${{STUB_SELFTEST_RC:-0}}" ;;
  init) echo '{{}}' > "${{SWARM_CALL_GUARD}}" ;;
esac
""")
    register = scripts / "register-tenant.sh"
    register.write_text(f"""#!/usr/bin/env bash
set -euo pipefail
echo "register $* gcloud=$(command -v gcloud) enforce=${{SWARM_CALL_GUARD_ENFORCE}}" >> "{log}"
exit "${{STUB_RC:-0}}"
""")
    for path in (guard, register):
        path.chmod(0o755)
    return scripts


@pytest.mark.parametrize("script_rc,job_rc", [(0, 0), (3, 0), (1, 1), (2, 2)])
def test_the_job_script_ends_a_stop_for_the_owner_as_a_success(tmp_path: Path, script_rc: int, job_rc: int) -> None:
    """register-tenant.sh's 3 is needs_owner: the record says so and the owner
    decides (§2.5), so the execution ends as a success. Any other failure
    fails it. And the order holds: self-test, init, then the script, with the
    shims first on PATH."""
    scripts = _stub_tree(tmp_path)
    expect = tmp_path / "guard" / "expect.json"
    expect.parent.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "SWARM_CALL_GUARD": str(expect),
           "SWARM_CALL_GUARD_ENFORCE": "1", "NO_COLOR": "1", "STUB_RC": str(script_rc)}
    proc = subprocess.run([str(scripts / "workspace-apply.sh"), WORKSPACE, "limits"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == job_rc, _out(proc)
    log = (tmp_path / "stub.log").read_text().splitlines()
    shim = scripts / "lib" / "guard-bin" / "gcloud"
    assert log == ["guard self-test", f"guard init --workspace-id {WORKSPACE}",
                   f"register --workspace {WORKSPACE} --mode limits gcloud={shim} enforce=1"], log
    if script_rc == 3:
        assert "stopped for the platform owner" in proc.stderr


def test_the_job_script_runs_nothing_when_the_guard_fails_its_own_cases(tmp_path: Path) -> None:
    scripts = _stub_tree(tmp_path)
    expect = tmp_path / "expect.json"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "SWARM_CALL_GUARD": str(expect),
           "SWARM_CALL_GUARD_ENFORCE": "1", "NO_COLOR": "1", "STUB_SELFTEST_RC": "1"}
    proc = subprocess.run([str(scripts / "workspace-apply.sh"), WORKSPACE, "create"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 1, _out(proc)
    assert (tmp_path / "stub.log").read_text().splitlines() == ["guard self-test"]
    assert not expect.exists()


@pytest.mark.parametrize("enforce", [None, "0", ""])
def test_the_job_script_refuses_to_run_unenforced(tmp_path: Path, enforce: str | None) -> None:
    scripts = _stub_tree(tmp_path)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path),
           "SWARM_CALL_GUARD": str(tmp_path / "expect.json"), "NO_COLOR": "1"}
    if enforce is not None:
        env["SWARM_CALL_GUARD_ENFORCE"] = enforce
    proc = subprocess.run([str(scripts / "workspace-apply.sh"), WORKSPACE, "create"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 1 and "never runs unguarded" in proc.stderr, _out(proc)
    assert not (tmp_path / "stub.log").exists()
