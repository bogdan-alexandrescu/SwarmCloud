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
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "scripts" / "register-tenant.sh"
GUARD = REPO / "scripts" / "lib" / "workspace-guard.sh"
SHIMS = REPO / "scripts" / "lib" / "guard-bin"
WORLD_PY = Path(__file__).resolve().parent / "fixtures" / "workspace_world.py"
DEPLOYER_RBAC = REPO / "kubernetes" / "rbac" / "provisioner-rbac.yaml"
#: Where every Python a run starts (the fake world on each of a run's ~130
#: gcloud, kubectl and curl calls, kubernetes/render.py) keeps its bytecode.
#: An image whose stdlib ships no .pyc, and cannot write one -- the SwarmCloud
#: runtime image, 2026-10-16 -- recompiles `re`, `json` and the rest on every
#: start: 0.21 s a fake call there against 0.08 s with this, ~20 s of a
#: create's 80 s (#1334). Outside the repository, shared by sessions and
#: workers: a .pyc is checked against its source's mtime, so a stale one is
#: never used.
PYCACHE = Path(tempfile.gettempdir()) / f"swarm-test-pycache-{os.getuid()}"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

PROJECT = "saga-agents-staging"
WORKSPACE = "w-3f9a2c"
PRINCIPAL = "alice@example.com"
TENANT = "u-alice"
ADMIN = "root@example.com"
WORKER = f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER = f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com"
RECONCILER = f"swarm-reconciler@{PROJECT}.iam.gserviceaccount.com"
NAMESPACE = f"swarm-tenant-{TENANT}"
BUCKET = f"swarm-artifacts-{PROJECT}"
SLOT = f"swarm-tenant-{TENANT}-git-u-" + hashlib.sha256(PRINCIPAL.encode()).hexdigest()[:16]
TWIN = SLOT + "-refresh"
#: The tenant's QA target credential secret (docs/design/visual-qa-any-app.md
#: §2.1, §7 V-1/V-2): the deployer makes it for a PERSONAL workspace only (BGD,
#: 2026-10-22), with swarm-api alone bound on it.
QA = f"swarm-tenant-{TENANT}-qa"
API = f"swarm-api@{PROJECT}.iam.gserviceaccount.com"
QA_ADDER = "roles/secretmanager.secretVersionAdder"
QA_ACCESSOR = "roles/secretmanager.secretAccessor"
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


#: The API backend's IAP policy, for `gcloud iap web get-iam-policy` and
#: `add-iam-policy-binding` (docs/design/workspace-kinds.md §4.5). Kept here,
#: not in fixtures/workspace_world.py, which this lane does not own: the
#: same world file, the same call log, the same injected failures.
IAP_FAKE = r"""
import json
import os
import sys
from pathlib import Path

world_path = Path(os.environ["FAKE_WORLD"])
world = json.loads(world_path.read_text())
argv = sys.argv[1:]
if argv[:3] != ["iap", "web", argv[2] if len(argv) > 2 else ""]:
    # A describe: the shared world's, unless the account is marked disabled.
    account = world.get("accounts", {}).get(argv[3] if len(argv) > 3 else "")
    if argv[:3] != ["iam", "service-accounts", "describe"] or not (account or {}).get("disabled"):
        os.environ["FAKE_TOOL"] = "gcloud"
        os.execv(sys.executable, [sys.executable, os.environ["FAKE_WORLD_PY"], *argv])
    world.setdefault("calls", []).append({"tool": "gcloud", "argv": argv})
    world_path.write_text(json.dumps(world, indent=1, sort_keys=True))
    sys.stdout.write(json.dumps({"email": argv[3], "uniqueId": account["uid"], "disabled": True}) + "\n")
    sys.exit(0)
world.setdefault("calls", []).append({"tool": "gcloud", "argv": argv})


def save():
    world_path.write_text(json.dumps(world, indent=1, sort_keys=True))


pos, flags, i = [], {}, 0
while i < len(argv):
    arg = argv[i]
    if arg.startswith("--") and "=" in arg:
        key, value = arg.split("=", 1)
        flags[key] = value
    elif arg in ("--project", "--format"):
        flags[arg] = argv[i + 1]
        i += 1
    elif arg.startswith("-"):
        flags[arg] = ""
    else:
        pos.append(arg)
    i += 1
for rule in world.get("fail", []):
    if rule["tool"] == "gcloud" and pos[: len(rule["words"])] == rule["words"] and rule.get("times", 0) > 0:
        rule["times"] -= 1
        save()
        sys.stderr.write(rule.get("stderr", "ERROR: injected failure") + "\n")
        sys.exit(int(rule.get("rc", 1)))
service = flags.get("--service", "")
policies = world.setdefault("iap", {})
policy = policies.setdefault(service, {"version": 1, "etag": "BwX=", "bindings": [
    {"role": "roles/iap.httpsResourceAccessor", "members": ["domain:example.com"]}]})
if pos[2] == "add-iam-policy-binding":
    for binding in policy["bindings"]:
        if binding["role"] == flags["--role"] and "condition" not in binding:
            if flags["--member"] not in binding["members"]:
                binding["members"].append(flags["--member"])
            break
    else:
        policy["bindings"].append({"role": flags["--role"], "members": [flags["--member"]]})
elif pos[2] != "get-iam-policy":
    sys.stderr.write("fake gcloud iap: unhandled call " + " ".join(argv) + "\n")
    sys.exit(2)
sys.stdout.write(json.dumps(policy) + "\n")
save()
"""


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
        # `gcloud iap web ...` (a service workspace's S2 and A9), and the
        # describe of an account marked disabled (S1), are answered by
        # IAP_FAKE below; every other gcloud call by the shared fake world.
        iap = tmp / "iap_fake.py"
        iap.write_text(IAP_FAKE)
        (self.bin / "gcloud").write_text(
            '#!/bin/sh\nif { [ "$1" = iap ] && [ "$2" = web ]; } || [ "$3" = describe ]; then\n'
            f'  FAKE_WORLD_PY="{WORLD_PY}" exec "{sys.executable}" "{iap}" "$@"\nfi\n'
            f'FAKE_TOOL=gcloud exec "{sys.executable}" "{WORLD_PY}" "$@"\n')
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
            PYTHONPYCACHEPREFIX=str(PYCACHE),
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


def _once(tmp_path_factory, name: str, run) -> dict:
    """`run(job)` ONCE per test session, shared by every xdist worker.

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
    result = root / f"register-tenant-workspace-{name}.json"
    with open(root / f"register-tenant-workspace-{name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not result.exists():
            first = Job(Path(tmp_path_factory.mktemp(name)))
            proc = run(first)
            result.write_text(json.dumps({
                "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
                "stopped": first.stopped(), "world": first.read()}))
    return json.loads(result.read_text())


@pytest.fixture(scope="session")
def created(tmp_path_factory) -> dict:
    """ONE full create per test session (`_once`)."""
    return _once(tmp_path_factory, "created", lambda job: job.create())


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


MARKER = f"gs://{BUCKET}/tenants/{TENANT}/.tenant"


@pytest.fixture(scope="session")
def rerun(created: dict, tmp_path_factory) -> dict:
    """ONE admin-retried create over a complete workspace (`_once`), whose
    marker an earlier run wrote. Two tests read it: the full re-run changes
    nothing, and the marker is left as it is. Each was its own ~40 s run of
    the same script over the same world but the marker's text (#1334)."""
    assert created["returncode"] == 0, created["stderr"][-6000:]

    def run(job: Job) -> subprocess.CompletedProcess[str]:
        world = copy.deepcopy(created["world"])
        world["calls"] = []
        world["writes"] = []
        world["objects"][MARKER] = "written by an earlier run"
        world["firestore"][f"workspaces/{TENANT}"]["fields"].update(
            state=_s("failed"),
            retry={"mapValue": {"fields": {"by": _s(ADMIN), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}})
        job.write(world)
        return job.create()

    return _once(tmp_path_factory, "rerun", run)


def _rerun_job(job: Job, rerun: dict) -> subprocess.CompletedProcess[str]:
    """`rerun`'s result, and its world in `job`, as if `job` had run it."""
    job.write(rerun["world"])
    return subprocess.CompletedProcess([], rerun["returncode"], rerun["stdout"], rerun["stderr"])


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

    # A5: the two prefix-conditioned grants, exactly the expressions C4 accepts,
    # and, because W0 found the principal-set grant impossible and the WD9
    # fallback is on (wd9_fallback in workspace-calls.json, docs/workspaces.md
    # §2.3), the unconditioned bucket-metadata role and the three project roles.
    grants = {(b["role"], (b.get("condition") or {}).get("expression"))
              for b in world["bucket_policy"]["bindings"] if f"serviceAccount:{WORKER}" in b["members"]}
    assert grants == {("roles/storage.objectViewer", READ_EXPR), ("roles/storage.objectUser", WRITE_EXPR),
                      (f"projects/{PROJECT}/roles/swarmBucketMetadataReader", None)}
    assert f"gs://{BUCKET}/tenants/{TENANT}/.tenant" in world["objects"]
    project_grants = {(b["role"], (b.get("condition") or {}).get("expression"))
                      for b in world["project_policy"]["bindings"] if f"serviceAccount:{WORKER}" in b["members"]}
    assert project_grants == {(f"projects/{PROJECT}/roles/swarmTenantWorkerFirestore", None),
                              ("roles/logging.logWriter", None), ("roles/monitoring.metricWriter", None)}

    # A6: the slot and its twin, labelled as swarm-api labels its own; the
    # worker reads the slot and never the twin. And the QA secret.
    # MUTATION: drop qa_secret_ensure from ws_a6 and QA is missing here.
    assert set(world["secrets"]) == {SLOT, TWIN, QA}
    assert world["secrets"][SLOT]["labels"]["tenant"] == TENANT
    assert world["secrets"][TWIN]["labels"]["provider"].endswith("-refresh")
    assert _members(world["secrets"][SLOT]["policy"], "roles/secretmanager.secretAccessor") == {
        f"serviceAccount:{WORKER}"}
    assert world["secrets"][TWIN]["policy"].get("bindings", []) == []
    # A6, the QA secret: labelled as qa_targets.tf labels its own, and swarm-api
    # alone holds V-1 and V-2 on it -- never the worker, whose agent would then
    # read a test account's password out of Secret Manager itself.
    qa = world["secrets"][QA]
    # MUTATION: label it for the forge slot (provider=...) instead and these fail.
    assert qa["labels"]["component"] == "qa-target-credential", qa["labels"]
    assert qa["labels"]["tenant"] == TENANT, qa["labels"]
    # MUTATION: grant the worker either role, or any third role, and this fails.
    assert {b["role"]: set(b["members"]) for b in qa["policy"].get("bindings", [])} == {
        QA_ADDER: {f"serviceAccount:{API}"}, QA_ACCESSOR: {f"serviceAccount:{API}"}}
    creates = [c for c in created["world"]["calls"] if c["tool"] == "gcloud"
               and c["argv"][:3] == ["secrets", "create", QA]]
    # MUTATION: create it with --replication-policy automatic and this fails.
    assert len(creates) == 1 and "user-managed" in creates[0]["argv"], creates

    # A7: the namespace and its objects, with §8's quota.
    quota = world["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    assert (quota["pods"], quota["requests.cpu"], quota["requests.memory"]) == ("16", "64", "128Gi"), quota
    # CPU bursts to 2x its request (SIZES-SCHED-1, contract request RS-1), so
    # the limits line is twice the requests line or the namespace fits half
    # its pods. MUTATION: render limits.cpu from __QUOTA_CPU__ and this reads 64.
    assert quota["limits.cpu"] == "128", quota
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
               for c in changed), (
        f"the re-run remade what the first run had made: {changed}")
    assert not any(c[:3] == ["iam", "service-accounts", "create"] for c in changed)
    assert not any("add-iam-policy-binding" in c and c[0] in ("iam", "storage") for c in changed)


def test_a_full_rerun_on_a_complete_workspace_changes_nothing(job: Job, rerun: dict) -> None:
    proc = _rerun_job(job, rerun)
    out = _out(proc)
    assert proc.returncode == 0, out
    changed = _changes(job.calls("gcloud"))
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


def _qa_secret(tenant: str, component: str = "qa-target-credential", members: tuple[str, ...] = ()) -> dict:
    """A QA secret someone made before the job ran, labelled for `tenant`."""
    bindings = [{"role": QA_ACCESSOR, "members": list(members)}] if members else []
    return {"labels": {"managed-by": "swarm-register-tenant", "component": component, "tenant": tenant,
                       "swarm-tenant": tenant}, "policy": {"version": 1, "etag": "BwX=", "bindings": bindings}}


def test_a_qa_secret_labelled_for_another_tenant_holds_the_job_slot_not_ours(job: Job) -> None:
    job.edit(lambda w: w["secrets"].update({QA: _qa_secret("u-mallory")}))
    proc = job.create()
    out = _out(proc)
    # MUTATION: map qa_secret_ensure's 3 to a plain failure and this is exit 1, failed.
    assert proc.returncode == STOPPED, out
    record = job.record()
    assert _field(record, "state") == "needs_owner", out
    assert _field(record, "steps", "A6", "code") == "SLOT_NOT_OURS", record.get("steps")
    assert "u-mallory" in proc.stderr, out
    # MUTATION: adopt the secret (bind swarm-api on it) and this fails.
    assert not any(c[:3] == ["secrets", "add-iam-policy-binding", QA] for c in job.calls("gcloud")), out
    assert job.read()["secrets"][QA]["policy"]["bindings"] == []
    assert job.doc(f"tenants/{TENANT}") is None, "nothing after A6 runs"


def test_a_qa_secret_another_account_reads_holds_the_job_slot_not_ours(job: Job) -> None:
    foreign = f"serviceAccount:{WORKER}"
    job.edit(lambda w: w["secrets"].update({QA: _qa_secret(TENANT, members=(foreign,))}))
    proc = job.create()
    out = _out(proc)
    # MUTATION: ignore qa_secret_ensure's 4 and the run ends ready.
    assert proc.returncode == STOPPED, out
    assert _field(job.record(), "steps", "A6", "code") == "SLOT_NOT_OURS", job.record().get("steps")
    # MUTATION: remove the foreign member and this fails: a removal is never the job's.
    assert job.read()["secrets"][QA]["policy"]["bindings"] == [{"role": QA_ACCESSOR, "members": [foreign]}]
    assert not any(c[:3] == ["secrets", "add-iam-policy-binding", QA] for c in job.calls("gcloud")), out


def test_the_qa_secret_is_made_empty_and_its_value_never_printed(created: dict) -> None:
    world = created["world"]
    # MUTATION: pass --data-file to the create and this fails.
    assert not any("versions" in c["argv"] or any(a.startswith("--data-file") for a in c["argv"])
                   for c in world["calls"] if c["tool"] == "gcloud" and QA in c["argv"])
    assert f"qa secret: created {QA}" in created["stderr"], created["stderr"][-4000:]


UPLOAD = f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o"


def _marker_uploads(calls: list[list[str]]) -> list[list[str]]:
    return [argv for argv in calls if any(a.startswith(UPLOAD) for a in argv)]


def test_the_marker_is_one_create_only_upload(created: dict) -> None:
    """A5's .tenant marker needs storage.objects.create and nothing else.

    `gcloud storage cp` read the destination first, and the deployer holds no
    object read: w-7f9285's A5 died on storage.objects.get, 2026-10-13 01:40Z.
    One JSON API upload with ifGenerationMatch=0 can never replace an object,
    so the create on tenants/ the deployer holds is the whole of it."""
    assert created["returncode"] == 0, created["stderr"][-6000:]
    calls = created["world"]["calls"]
    assert not [c["argv"] for c in calls if c["tool"] == "gcloud" and c["argv"][:2] == ["storage", "cp"]]
    uploads = _marker_uploads([c["argv"] for c in calls if c["tool"] == "curl"])
    assert len(uploads) == 1, uploads
    url = next(a for a in uploads[0] if a.startswith(UPLOAD))
    assert "ifGenerationMatch=0" in url.split("?", 1)[1].split("&"), url
    assert f"name=tenants%2F{TENANT}%2F.tenant" in url, url
    assert json.loads(created["world"]["objects"][MARKER]) == {"tenant_id": TENANT, "workspace_id": WORKSPACE}


def test_an_existing_marker_is_left_as_it_is(job: Job, rerun: dict) -> None:
    """A re-run meets its own marker: the upload answers 412, which is "already
    there", and A5 is done without reading or replacing it."""
    proc = _rerun_job(job, rerun)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert _steps(job.record())["A5"] == "done", out
    assert job.read()["objects"][MARKER] == "written by an earlier run"
    assert len(_marker_uploads(job.calls("curl"))) == 1, out


def test_a_refused_marker_upload_fails_a5_retryable(job: Job) -> None:
    job.edit(lambda w: w["fail"].append(
        {"tool": "curl", "words": ["POST", f"/upload/storage/v1/b/{BUCKET}/o"], "times": 1, "status": 403}))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = job.record()
    assert _field(record, "failure", "step") == "A5", out
    assert _field(record, "failure", "code") == "GRANT_FAILED"
    assert _field(record, "failure", "retryable") is True
    assert "HTTP 403" in proc.stderr, out
    assert MARKER not in job.read()["objects"]


def test_the_guard_admits_no_kubectl_verb_a_run_does_not_use(created: dict) -> None:
    """The guard's cluster shapes against a real run's kubectl calls: a verb the
    guard admits and no step makes is a door left open (WS-WALK).
    kubernetes/apply.sh runs `diff` only without --confirm, which A7 always
    passes."""
    assert created["returncode"] == 0, created["stderr"][-6000:]
    used = {_kube_parse(c["argv"])[0][0] for c in created["world"]["calls"] if c["tool"] == "kubectl"}
    rules = json.loads((REPO / "scripts" / "lib" / "workspace-calls.json").read_text())["rules"]
    admitted = {s["command"][0] for r in rules for s in r.get("shapes", [])
                if s["tool"] == "kubectl" and not s.get("local")}
    assert admitted <= used, f"the guard admits kubectl {sorted(admitted - used)}, which no step runs"


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
    # MUTATION: render limits.cpu from __QUOTA_CPU__ and this reads 96.
    assert quota["limits.cpu"] == "192", quota
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


def test_verify_finds_a_quota_whose_cpu_limit_cannot_hold_its_pods(job: Job, ready_world: dict) -> None:
    """A namespace applied before CPU burst (limits.cpu == requests.cpu) fits
    half its pods: verify must name it, so `--mode limits` re-applies it.

    MUTATION: drop the limits.cpu comparison from register-tenant.sh's
    ResourceQuota check and verify passes this quota.
    """
    _from_ready(job, ready_world)

    def old_quota(world: dict) -> None:
        hard = world["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
        hard["limits.cpu"] = hard["requests.cpu"]

    job.edit(old_quota)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    out = _out(proc)
    assert proc.returncode == 1, out
    record = job.record()
    assert (_field(record, "failure", "step"), _field(record, "failure", "code"),
            _field(record, "failure", "object")) == ("A9", "VERIFY_FAILED", "ResourceQuota")


def _hand_applied_quota(world: dict) -> None:
    """kubernetes/render.py's flat defaults, as `kubernetes/apply.sh --tenant`
    applied them by hand to u-bogdan's namespace (#2039)."""
    hard = world["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    hard.update({"pods": "100", "requests.cpu": "400", "limits.cpu": "800"})


def _failed_at(step: str, code: str, obj: str | None) -> dict:
    fields = {"step": _s(step), "code": _s(code), "retryable": {"booleanValue": True},
              "at": {"timestampValue": "2026-10-23T02:30:00Z"}}
    if obj is not None:
        fields["object"] = _s(obj)
    return {"state": _s("failed"), "failure": {"mapValue": {"fields": fields}}}


def test_verify_names_the_quota_s_numbers_not_a_missing_object(job: Job, ready_world: dict) -> None:
    """QUOTA-ONE (#2039): the quota exists with other numbers, so A9 says
    which numbers, never that an object is missing."""
    _from_ready(job, ready_world)
    job.edit(_hand_applied_quota)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    out = _out(proc)
    assert proc.returncode == 1, out
    # MUTATION: report the quota through ws_object and this reads "a
    # ResourceQuota is missing or not as specified".
    assert ("the namespace quota is pods 100, cpu 400 (limit 800); "
            "the record wants pods 16, cpu 64 (limit 128)") in proc.stderr, out
    assert "ResourceQuota is missing" not in proc.stderr, out
    record = job.record()
    # The record keeps its shape: the step, the code and the object's type.
    assert (_field(record, "failure", "step"), _field(record, "failure", "code"),
            _field(record, "failure", "object")) == ("A9", "VERIFY_FAILED", "ResourceQuota")


def test_limits_repairs_a_workspace_failed_on_its_quota_alone(job: Job, ready_world: dict) -> None:
    """A failed A9 on ResourceQuota is the one failure `--mode limits` may
    run on: it re-applies the quota and the documents, runs A9 again, and
    the workspace is ready."""
    _from_ready(job, ready_world)
    job.edit(_hand_applied_quota)
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        _failed_at("A9", "VERIFY_FAILED", "ResourceQuota")))
    proc = job.run("--workspace", WORKSPACE, "--mode", "limits")
    out = _out(proc)
    # MUTATION: keep refusing every workspace that is not ready, and this
    # exits 1 with NOT_READY.
    assert proc.returncode == 0, out
    hard = job.read()["k8s"][f"ResourceQuota/{NAMESPACE}/swarm-tenant-quota"]["spec"]["hard"]
    assert (hard["pods"], hard["requests.cpu"], hard["limits.cpu"]) == ("16", "64", "128"), hard
    record = job.record()
    # MUTATION: skip A9 on the repair, and the record is ready on A7's word.
    assert _steps(record).get("A9") == "done", _steps(record)
    # MUTATION: close a repair as a ceiling change (run.finished_at alone)
    # and the workspace stays failed.
    assert _field(record, "state") == "ready"
    assert _field(record, "failure") is None
    assert _changes(job.calls("gcloud")) == [], "a repair makes no IAM change"


@pytest.mark.parametrize("failure", [
    _failed_at("A9", "VERIFY_FAILED", "NetworkPolicy"),
    _failed_at("A9", "NAMESPACE_LABEL_MISSING", "ResourceQuota"),
    _failed_at("A7", "NAMESPACE_APPLY_FAILED", "ResourceQuota"),
    _failed_at("A9", "VERIFY_FAILED", None),
    {"state": _s("failed")},
], ids=["other-object", "other-code", "other-step", "no-object", "no-failure"])
def test_limits_refuses_any_other_failed_workspace(job: Job, ready_world: dict, failure: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(lambda w: w["firestore"][f"workspaces/{TENANT}"]["fields"].update(failure))
    proc = job.run("--workspace", WORKSPACE, "--mode", "limits")
    # MUTATION: admit every failed A9, or every failure naming ResourceQuota,
    # and one of these runs.
    assert proc.returncode == 1 and "NOT_READY" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


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


LEGACY_OBJECTS = (f"ServiceAccount/{NAMESPACE}/swarm-worker", f"RoleBinding/{NAMESPACE}/swarm-worker-legacy")


def _as_terraform_made(world: dict) -> None:
    """u-bogdan's shape (W9, measured 2026-10-10 on w-752763): a migrated
    record, no legacy swarm-worker binding and no legacy namespace objects."""
    fields = world["firestore"][f"workspaces/{TENANT}"]["fields"]
    fields.pop("decision")
    fields["migrated"] = {"booleanValue": True}
    legacy = f"serviceAccount:{PROJECT}.svc.id.goog[{NAMESPACE}/swarm-worker]"
    for b in world["accounts"][WORKER]["policy"]["bindings"]:
        if b["role"] == WORKLOAD:
            b["members"] = [m for m in b["members"] if m != legacy]
    for key in LEGACY_OBJECTS:
        world["k8s"].pop(key)


def test_verify_of_a_migrated_record_renders_without_the_legacy_ksa(job: Job, ready_world: dict) -> None:
    # A Terraform-made tenant has no swarm-worker KSA (kubernetes/README.md: the
    # legacy account is rendered only where IAM binds it).
    _from_ready(job, ready_world)
    assert all(key in job.read()["k8s"] for key in LEGACY_OBJECTS)
    job.edit(_as_terraform_made)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 0, _out(proc)
    assert _field(job.record(), "state") == "ready"
    assert _steps(job.record())["A9"] == "done"
    assert set(job.read()["secrets"]) == {SLOT, TWIN, QA}


def test_verify_of_a_job_made_record_still_needs_the_legacy_objects(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(lambda w: w["k8s"].pop(f"ServiceAccount/{NAMESPACE}/swarm-worker"))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "ServiceAccount"


@pytest.mark.parametrize("missing", [SLOT, TWIN], ids=["slot", "twin"])
def test_verify_of_a_migrated_record_still_needs_the_forge_slot_pair(
        job: Job, ready_world: dict, missing: str) -> None:
    _from_ready(job, ready_world)
    job.edit(_as_terraform_made)
    job.edit(lambda w: w["secrets"].pop(missing))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "forge slot"


def test_verify_of_a_migrated_record_still_needs_the_worker_on_the_slot(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(_as_terraform_made)
    job.edit(lambda w: w["secrets"][SLOT].update(policy={"version": 1, "etag": "BwX="}))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == "forge slot binding"


# The tenant and pool documents as terraform/modules/firestore/bootstrap.tf
# writes them (read back through the REST API: an integer is a string inside
# integerValue), at the ready world's limits (8 and 8, pool min = 8).
def _terraform_tenant_doc() -> dict:
    return {"fields": {
        "tenant_id": _s(TENANT), "kind": _s("user"), "principal": _s(PRINCIPAL), "display_name": _s(""),
        "max_active": {"integerValue": "8"}, "capacity_units": {"integerValue": "8"},
        "enabled": {"booleanValue": True}, "service_account": _s(WORKER),
        "gcs_prefix": _s(f"gs://{BUCKET}/tenants/{TENANT}"), "namespace": _s(NAMESPACE),
        "credentials": {"arrayValue": {"values": [_s("anthropic")]}}, "managed_by": _s("swarm-terraform"),
    }, "updateTime": "2026-09-01T00:00:00.000001Z"}


def _terraform_pool_doc() -> dict:
    return {"fields": {
        "name": _s(f"tenant:{TENANT}"), "hard_limit": {"integerValue": "8"}, "active": {"integerValue": "0"},
        "enabled": {"booleanValue": True}, "managed_by": _s("swarm-terraform"),
    }, "updateTime": "2026-09-01T00:00:00.000001Z"}


def _terraform_docs(tenant_change=None, pool_change=None):
    def apply(world: dict) -> None:
        tenant, pool = _terraform_tenant_doc(), _terraform_pool_doc()
        if tenant_change:
            tenant_change(tenant["fields"])
        if pool_change:
            pool_change(pool["fields"])
        world["firestore"][f"tenants/{TENANT}"] = tenant
        world["firestore"][f"pools/tenant:{TENANT}"] = pool
    return apply


def _job_made(world: dict) -> None:
    world["firestore"][f"workspaces/{TENANT}"]["fields"].update(
        migrated={"booleanValue": False}, decision=_record()["fields"]["decision"])


# What a Terraform-era document may legitimately hold that A8 never writes,
# all at once (each run of the script is ~20 s): no `namespace`, which
# ignore_changes keeps off a document written before the module named it; a
# limit as an integral double or a string of digits (a hand patch; the Tenant
# and SlotPool models read either as the same whole number); and the principal
# as tfvars spelled it. Everything else is bootstrap.tf's shape, extra
# `managed_by`, `credentials` and `display_name` included.
def _terraform_era_tenant(fields: dict) -> None:
    fields.pop("namespace")
    fields.update(max_active={"doubleValue": 8.0}, capacity_units=_s("8"), principal=_s(" Alice@Example.com "))


def _terraform_era_pool(fields: dict) -> None:
    fields.update(hard_limit=_s("8"))


def test_verify_accepts_terraforms_documents_on_a_migrated_record_only(job: Job, ready_world: dict) -> None:
    _from_ready(job, ready_world)
    job.edit(_as_terraform_made)
    job.edit(_terraform_docs(_terraform_era_tenant, _terraform_era_pool))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 0, _out(proc)
    assert _field(job.record(), "state") == "ready"
    assert _steps(job.record())["A9"] == "done"


# The same documents on a record the job made: A8 wrote every field as
# checked, so the strict check refuses each one that differs, by name.
# (jq reads 8.0 and 8 as one number, so the double passes both checks.)
# Each case was a third of the test above, run one after another on one
# worker from a fresh ready world; as cases they run side by side (#1334).
JOB_MADE_REFUSES = {
    "tenant": (_terraform_era_tenant, "tenant",
               "tenant document's fields principal, namespace, capacity_units are not as the record specifies\n"),
    "pool": (None, "pool", "pool document's fields hard_limit are not as the record specifies\n"),
}


@pytest.mark.parametrize("tenant_change,document,line", JOB_MADE_REFUSES.values(), ids=JOB_MADE_REFUSES.keys())
def test_verify_refuses_terraforms_documents_on_a_job_made_record(
        job: Job, ready_world: dict, tenant_change, document: str, line: str) -> None:
    _from_ready(job, ready_world)
    job.edit(_terraform_docs(tenant_change, _terraform_era_pool))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == f"{document} document"
    assert line in proc.stderr, _out(proc)


def _mismatched_tenant(fields: dict) -> None:
    fields.update(principal=_s("mallory@example.com"), service_account=_s(SCHEDULER),
                  namespace=_s("swarm-tenant-eng"), max_active={"integerValue": "9"})


def _unreadable_tenant(fields: dict) -> None:
    fields.pop("service_account")
    fields.update(max_active=_s("8 agents"), capacity_units={"doubleValue": 8.5})


# What a migrated record still refuses: the limits must agree, the identity
# must be the tenant's, and a namespace that is present must be its own.
STILL_REFUSED = {
    "foreign": (_mismatched_tenant, None, "tenant", "principal, service_account, namespace, max_active"),
    "unreadable": (_unreadable_tenant, None, "tenant", "service_account, max_active, capacity_units"),
    "hard-limit": (None, lambda f: f.update(hard_limit={"integerValue": "16"}), "pool", "hard_limit"),
    "no-hard-limit": (None, lambda f: f.pop("hard_limit"), "pool", "hard_limit"),
}


@pytest.mark.parametrize("tenant_change,pool_change,document,fields", STILL_REFUSED.values(), ids=STILL_REFUSED.keys())
def test_verify_of_a_migrated_record_still_needs_the_documents_to_agree(
        job: Job, ready_world: dict, tenant_change, pool_change, document: str, fields: str) -> None:
    _from_ready(job, ready_world)
    job.edit(_as_terraform_made)
    job.edit(_terraform_docs(tenant_change, pool_change))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == f"{document} document"
    assert f"{document} document's fields {fields} are not as the record specifies (migrated record)" \
        in proc.stderr, _out(proc)


@pytest.mark.parametrize("migrated,path,document", [
    (True, f"tenants/{TENANT}", "tenant"),
    (False, f"tenants/{TENANT}", "tenant"),
    (True, f"pools/tenant:{TENANT}", "pool"),
], ids=["tenant-migrated", "tenant-job-made", "pool-migrated"])
def test_verify_fails_a_missing_document_on_every_record(
        job: Job, ready_world: dict, migrated: bool, path: str, document: str) -> None:
    _from_ready(job, ready_world)
    if migrated:
        job.edit(_as_terraform_made)
        job.edit(_terraform_docs())
    else:
        job.edit(_job_made)
    job.edit(lambda w: w["firestore"].pop(path))
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(job.record(), "failure", "object") == f"{document} document"
    assert f"the {document} document is missing" in proc.stderr, _out(proc)


# ---------------------------------------------------------------------------
# A1: nothing is written for a record that is not admissible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change,needle", [
    (lambda f: f.update(state=_s("requested")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f.update(state=_s("denied")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f["decision"]["mapValue"]["fields"].update(by=_s("mallory@example.com")), "not an admin"),
    (lambda f: f["decision"]["mapValue"]["fields"].update(verdict=_s("denied")), "WORKSPACE_NOT_APPROVED"),
    (lambda f: f.update(tenant_id=_s("u-bob")), "WORKSPACE_ID_TAKEN"),
    (lambda f: f.update(principal=_s("bob@example.com")), "WORKSPACE_ID_TAKEN"),
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
    ["--user", PRINCIPAL], ["--group", "eng@example.com"], ["--tenant", TENANT], ["--providers", "anthropic"],
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
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "PROJECT_ID": PROJECT, "SWARM_CALL_GUARD": str(expect),
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
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "PROJECT_ID": PROJECT, "SWARM_CALL_GUARD": str(expect),
           "SWARM_CALL_GUARD_ENFORCE": "1", "NO_COLOR": "1", "STUB_SELFTEST_RC": "1"}
    proc = subprocess.run([str(scripts / "workspace-apply.sh"), WORKSPACE, "create"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 1, _out(proc)
    assert (tmp_path / "stub.log").read_text().splitlines() == ["guard self-test"]
    assert not expect.exists()


@pytest.mark.parametrize("enforce", [None, "0", ""])
def test_the_job_script_refuses_to_run_unenforced(tmp_path: Path, enforce: str | None) -> None:
    scripts = _stub_tree(tmp_path)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "PROJECT_ID": PROJECT,
           "SWARM_CALL_GUARD": str(tmp_path / "expect.json"), "NO_COLOR": "1"}
    if enforce is not None:
        env["SWARM_CALL_GUARD_ENFORCE"] = enforce
    proc = subprocess.run([str(scripts / "workspace-apply.sh"), WORKSPACE, "create"], env=env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 1 and "never runs unguarded" in proc.stderr, _out(proc)
    assert not (tmp_path / "stub.log").exists()


# ---------------------------------------------------------------------------
# Kinds: a group's and a service's workspace (docs/design/workspace-kinds.md
# §4.3, lane WK-JOB). The same job, the same guard, the same fake world.
# ---------------------------------------------------------------------------

RULES_FILE = REPO / "scripts" / "lib" / "workspace-calls.json"
COMMON = REPO / "scripts" / "lib" / "common.sh"
#: Who alone approves a service workspace in this project (Q2 (a)): the
#: image's, never the record's word.
OWNER = json.loads(RULES_FILE.read_text()).get("platform_owner", {}).get(PROJECT, "")
FRONT_DOOR = json.loads(RULES_FILE.read_text()).get("front_door_backend", "")
ACCESSOR = "roles/iap.httpsResourceAccessor"

GROUP = "research@example.com"
G_TENANT = "research"
G_WORKER = f"swarm-agent-worker-{G_TENANT}@{PROJECT}.iam.gserviceaccount.com"
G_NAMESPACE = f"swarm-tenant-{G_TENANT}"

SERVICE = f"reports-bot@{PROJECT}.iam.gserviceaccount.com"
SERVICE_UID = "112233445566778899001"


def _user_tenant(email: str) -> str:
    sys.path.insert(0, str(REPO / "apps" / "common"))
    from swarm_common.identity import tenant_id_for_user

    return tenant_id_for_user(email)


S_TENANT = _user_tenant(SERVICE)
S_WORKER = f"swarm-agent-worker-{S_TENANT}@{PROJECT}.iam.gserviceaccount.com"
S_NAMESPACE = f"swarm-tenant-{S_TENANT}"
S_SLOT = f"swarm-tenant-{S_TENANT}-git"


def _iso_ago(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kind_record(kind: str, tenant: str, principal: str, by: str, **extra: dict) -> dict:
    record = _record(**extra)
    fields = record["fields"]
    fields.update(tenant_id=_s(tenant), principal=_s(principal), kind=_s(kind),
                  display_name=_s(principal.split("@")[0]))
    fields["decision"]["mapValue"]["fields"]["by"] = _s(by)
    if kind == "service":
        fields["limits"]["mapValue"]["fields"].update(max_active={"integerValue": "4"})
    return record


def _group_world(directory_age: int | None = 3600) -> dict:
    world = _world()
    world["firestore"].pop(f"workspaces/{TENANT}")
    extra = {}
    if directory_age is not None:
        extra["directory"] = {"mapValue": {"fields": {
            "checked_at": {"timestampValue": _iso_ago(directory_age)}, "readable": {"booleanValue": True}}}}
    world["firestore"][f"workspaces/{G_TENANT}"] = _kind_record("group", G_TENANT, GROUP, ADMIN, **extra)
    return world


def _service_world(principal: str = SERVICE, by: str = OWNER, owner_role: str = "owner",
                   account: dict | None = None) -> dict:
    world = _world()
    world["firestore"].pop(f"workspaces/{TENANT}")
    tenant = _user_tenant(principal)
    world["firestore"][f"workspaces/{tenant}"] = _kind_record("service", tenant, principal, by)
    world["firestore"][f"admin_roles/{OWNER}"] = {"fields": {"role": _s(owner_role)},
                                                  "updateTime": "2026-10-01T00:00:00Z"}
    world["accounts"][principal] = account if account is not None else {
        "policy": {"version": 1, "etag": "BwX="}, "keys": [], "uid": SERVICE_UID}
    return world


def _rec(job: Job, tenant: str) -> dict:
    return job.read()["firestore"][f"workspaces/{tenant}"]["fields"]


def _step_order(stderr: str) -> list[str]:
    """The steps a run announced, in order (`step` in common.sh)."""
    import re

    return re.findall(r"^== ([AS][0-9]) ", stderr, re.M)


def _kind_run(tmp_path_factory, name: str, world: dict) -> dict:
    def run(job: Job) -> subprocess.CompletedProcess[str]:
        job.write(world)
        return job.create()

    return _once(tmp_path_factory, name, run)


@pytest.fixture(scope="session")
def service_created(tmp_path_factory) -> dict:
    """ONE full create of a service workspace per session (`_once`)."""
    return _kind_run(tmp_path_factory, "service-created", _service_world())


@pytest.fixture(scope="session")
def group_created(tmp_path_factory) -> dict:
    """ONE full create of a group workspace per session (`_once`)."""
    return _kind_run(tmp_path_factory, "group-created", _group_world())


def _proc(result: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], result["returncode"], result["stdout"], result["stderr"])


def _gcloud_calls(world: dict) -> list[list[str]]:
    return [c["argv"] for c in world.get("calls", []) if c["tool"] == "gcloud"]


def _index(calls: list[dict], match) -> list[int]:
    return [i for i, c in enumerate(calls) if match(c)]


# -- service ----------------------------------------------------------------


def test_a_service_create_runs_s1_a2_to_a8_s2_a9_in_that_order(service_created: dict) -> None:
    proc = _proc(service_created)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert not service_created["stopped"], f"the guard refused a call a service workspace needs:\n{out}"
    # A1 twice: the claim, then a kind's own A1 checks (the tenant id is free).
    assert _step_order(proc.stderr) == ["A1", "A1", "S1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "S2", "A9"], out
    record = service_created["world"]["firestore"][f"workspaces/{S_TENANT}"]["fields"]
    assert _field(record, "state") == "ready", out
    assert _steps(record) == {s: "done" for s in ("A1", "S1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "S2", "A9")}
    # S1 reads the account before anything is made; S2 is the last write,
    # after A8's documents, and A9 reads its member back.
    calls = service_created["world"]["calls"]
    s1 = _index(calls, lambda c: c["tool"] == "gcloud" and c["argv"][:4] == ["iam", "service-accounts", "describe", SERVICE])
    made = _index(calls, lambda c: c["tool"] == "gcloud" and bool(_changes([c["argv"]])))
    a8 = _index(calls, lambda c: c["tool"] == "curl" and "PATCH" in c["argv"]
                and any(f"/pools/tenant%3A{S_TENANT}" in a or f"/pools/tenant:{S_TENANT}" in a for a in c["argv"]))
    s2 = _index(calls, lambda c: c["tool"] == "gcloud" and c["argv"][:3] == ["iap", "web", "add-iam-policy-binding"])
    reads = _index(calls, lambda c: c["tool"] == "gcloud" and c["argv"][:3] == ["iap", "web", "get-iam-policy"])
    assert s1 and made and a8 and len(s2) == 1 and len(reads) == 2, (s1, made, a8, s2, reads)
    assert s1[0] < made[0], "S1 must read the account before any write"
    assert max(a8) < s2[0], "S2 must come after A8's documents"
    assert reads[0] < s2[0] < reads[1], "S2 reads before it adds, and A9 reads the member back"
    assert s2[0] == max(made), "S2 is the run's last change"


def test_a_service_is_admitted_on_the_api_backend_only_and_its_account_is_never_written(
        service_created: dict) -> None:
    world = service_created["world"]
    assert set(world.get("iap", {})) == {FRONT_DOOR}, sorted(world.get("iap", {}))
    assert FRONT_DOOR == "swarm-ui-backend" and not FRONT_DOOR.endswith("-ui-ui-backend")
    policy = world["iap"][FRONT_DOOR]
    assert _members(policy, ACCESSOR) == {"domain:example.com", f"serviceAccount:{SERVICE}"}
    assert {b["role"] for b in policy["bindings"]} == {ACCESSOR}
    adds = [a for a in _gcloud_calls(world) if a[:3] == ["iap", "web", "add-iam-policy-binding"]]
    assert adds == [["iap", "web", "add-iam-policy-binding", "--project", PROJECT,
                     "--resource-type=backend-services", f"--service={FRONT_DOOR}", f"--role={ACCESSOR}",
                     f"--member=serviceAccount:{SERVICE}", "--condition=None", "--format=json"]]
    # The caller's own account: read, never written.
    assert world["accounts"][SERVICE]["policy"] == {"version": 1, "etag": "BwX="}
    assert not [a for a in _gcloud_calls(world) if SERVICE in a and a[:3] != ["iam", "service-accounts", "describe"]
                and a[:3] != ["iap", "web", "add-iam-policy-binding"]]
    record = world["firestore"][f"workspaces/{S_TENANT}"]["fields"]
    assert _field(record, "service", "unique_id") == SERVICE_UID
    assert _field(record, "service", "front_door", "backend") == FRONT_DOOR
    assert _field(record, "service", "front_door", "admitted_at")


def test_a_service_gets_a_team_slot_with_no_twin_and_a_labelled_namespace(service_created: dict) -> None:
    world = service_created["world"]
    # MUTATION: run qa_secret_ensure for a service too and swarm-tenant-<t>-qa appears here.
    assert set(world["secrets"]) == {S_SLOT}, sorted(world["secrets"])
    assert not any(c["argv"][2:3] == [f"swarm-tenant-{S_TENANT}-qa"] for c in world["calls"]
                   if c["tool"] == "gcloud" and c["argv"][:1] == ["secrets"])
    assert world["secrets"][S_SLOT]["labels"]["tenant"] == S_TENANT
    assert _members(world["secrets"][S_SLOT]["policy"], "roles/secretmanager.secretAccessor") == {
        f"serviceAccount:{S_WORKER}"}
    namespace = world["k8s"][f"Namespace//{S_NAMESPACE}"]
    assert namespace["metadata"]["labels"]["swarm.saga.xyz/workspace"] == WORKSPACE
    tenant = world["firestore"][f"tenants/{S_TENANT}"]["fields"]
    assert (_field(tenant, "kind"), _field(tenant, "principal"), _field(tenant, "max_active"),
            _field(tenant, "service_account"), _field(tenant, "display_name")) == (
        "user", SERVICE, "4", S_WORKER, "reports-bot")
    assert "directory_group" not in tenant
    assert set(world["writes"]) == {f"workspaces/{S_TENANT}", f"tenants/{S_TENANT}", f"pools/tenant:{S_TENANT}"}


def _deny_listed_account() -> str:
    for line in COMMON.read_text().splitlines():
        entry = line.strip().strip('"')
        if entry.endswith(f"@{PROJECT}.iam.gserviceaccount.com"):
            return entry
    raise AssertionError("SHARED_DENY_LIST names no service account of the project")


@pytest.mark.parametrize("principal,account", [
    (_deny_listed_account(), None),
    (f"swarm-api@{PROJECT}.iam.gserviceaccount.com", None),
    ("reports-bot@other-project-1.iam.gserviceaccount.com", None),
    (SERVICE, {}),
    (SERVICE, {"policy": {"version": 1}, "keys": [], "uid": SERVICE_UID, "disabled": True}),
], ids=["shared-deny-list", "platform-account", "another-project", "absent", "disabled"])
def test_s1_stops_for_the_owner_before_any_write(job: Job, principal: str, account: dict | None) -> None:
    world = _service_world(principal=principal, account=account)
    if account == {}:
        world["accounts"].pop(principal)
    if account and account.get("disabled"):
        world["accounts"][principal] = account
    job.write(world)
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == STOPPED, out
    tenant = _user_tenant(principal)
    record = _rec(job, tenant)
    assert _field(record, "state") == "needs_owner", out
    assert _steps(record)["S1"] == "held" and _field(record, "steps", "S1", "code") == "SERVICE_ACCOUNT_REFUSED"
    assert _changes(job.calls("gcloud")) == [], "S1 refused after something was made"
    assert job.read().get("writes", []) and set(job.read()["writes"]) == {f"workspaces/{tenant}"}
    assert not job.read().get("iap"), "nothing reached the front door"


@pytest.mark.parametrize("by,owner_role,needle", [
    (ADMIN, "owner", "WORKSPACE_APPROVAL_OWNER_ONLY"),
    (OWNER, "admin", "WORKSPACE_APPROVAL_OWNER_ONLY"),
], ids=["approved-by-an-admin", "image-owner-not-owner-in-admin-roles"])
def test_a1_refuses_a_service_not_approved_by_the_platform_owner(job: Job, by: str, owner_role: str,
                                                                 needle: str) -> None:
    world = _service_world(by=by, owner_role=owner_role)
    job.write(world)
    before = copy.deepcopy(_rec(job, S_TENANT))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1 and needle in proc.stderr, out
    assert job.read().get("writes", []) == [], out
    assert _rec(job, S_TENANT) == before
    assert _changes(job.calls("gcloud")) == []


def test_a1_refuses_a_service_retry_not_asked_by_the_platform_owner(job: Job) -> None:
    world = _service_world()
    world["firestore"][f"workspaces/{S_TENANT}"]["fields"].update(
        state=_s("failed"),
        retry={"mapValue": {"fields": {"by": _s(ADMIN), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}})
    job.write(world)
    proc = job.create()
    assert proc.returncode == 1 and "WORKSPACE_APPROVAL_OWNER_ONLY" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


def test_a_front_door_iap_refuses_stops_for_the_owner(job: Job, service_created: dict) -> None:
    """K1 not granted, or O1 found the number-form name matches nothing
    (§4.5): the owner admits the account by hand (FRONT_DOOR_OWNER)."""
    world = copy.deepcopy(service_created["world"])
    world["calls"], world["writes"] = [], []
    world["iap"] = {}
    world["firestore"][f"workspaces/{S_TENANT}"]["fields"].update(
        state=_s("failed"),
        retry={"mapValue": {"fields": {"by": _s(OWNER), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}})
    world["fail"] = [{"tool": "gcloud", "words": ["iap", "web", "add-iam-policy-binding"], "times": 1,
                      "stderr": "ERROR: (gcloud.iap.web.add-iam-policy-binding) PERMISSION_DENIED: "
                                "The caller does not have permission"}]
    job.write(world)
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == STOPPED, out
    record = _rec(job, S_TENANT)
    assert _field(record, "state") == "needs_owner"
    assert _steps(record)["S2"] == "held" and _field(record, "steps", "S2", "code") == "FRONT_DOOR_OWNER", out


def test_an_account_the_owner_already_admitted_finishes_with_the_read_alone(job: Job, service_created: dict) -> None:
    """FRONT-DOOR-READ (part of #847): the deployer may read the API backend's
    IAP policy (swarmFrontDoorReader) but not change it. The owner has added
    the account through frontend_iap_members and pressed Retry: S2 reads it
    there, makes no change call, and the run finishes. Any add would be refused."""
    world = copy.deepcopy(service_created["world"])
    world["calls"], world["writes"] = [], []
    assert f"serviceAccount:{SERVICE}" in world["iap"][FRONT_DOOR]["bindings"][0]["members"]
    world["firestore"][f"workspaces/{S_TENANT}"]["fields"].update(
        state=_s("failed"),
        retry={"mapValue": {"fields": {"by": _s(OWNER), "at": {"timestampValue": "2099-01-01T00:00:00Z"}}}})
    world["fail"] = [{"tool": "gcloud", "words": ["iap", "web", "add-iam-policy-binding"], "times": 1,
                      "stderr": "ERROR: (gcloud.iap.web.add-iam-policy-binding) PERMISSION_DENIED: "
                                "The caller does not have permission"}]
    job.write(world)
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 0, out
    record = _rec(job, S_TENANT)
    assert _field(record, "state") == "ready", out
    assert _steps(record)["S2"] != "held", out
    assert not [a for a in _gcloud_calls(job.read()) if a[:3] == ["iap", "web", "add-iam-policy-binding"]]
    assert [a for a in _gcloud_calls(job.read()) if a[:3] == ["iap", "web", "get-iam-policy"]], "S2 read nothing"


def test_verify_finds_a_service_missing_from_the_front_door(job: Job, service_created: dict) -> None:
    world = copy.deepcopy(service_created["world"])
    world["calls"], world["writes"] = [], []
    world["iap"][FRONT_DOOR]["bindings"][0]["members"].remove(f"serviceAccount:{SERVICE}")
    job.write(world)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    out = _out(proc)
    assert proc.returncode == 1, out
    record = _rec(job, S_TENANT)
    assert (_field(record, "failure", "step"), _field(record, "failure", "object")) == ("A9", "front door member")
    assert _changes(job.calls("gcloud")) == []


def test_verify_finds_a_service_account_made_again_under_the_same_name(job: Job, service_created: dict) -> None:
    world = copy.deepcopy(service_created["world"])
    world["calls"], world["writes"] = [], []
    world["accounts"][SERVICE]["uid"] = "998877665544332211009"
    job.write(world)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    assert _field(_rec(job, S_TENANT), "failure", "object") == "service account"


# -- group ------------------------------------------------------------------


def test_a_group_create_makes_its_team_slot_label_and_group_tenant(group_created: dict) -> None:
    proc = _proc(group_created)
    out = _out(proc)
    assert proc.returncode == 0, out
    assert not group_created["stopped"], f"the guard refused a call a group workspace needs:\n{out}"
    world = group_created["world"]
    record = world["firestore"][f"workspaces/{G_TENANT}"]["fields"]
    assert _field(record, "state") == "ready", out
    assert _steps(record) == {f"A{i}": "done" for i in range(1, 10)}
    # A6: the TEAM slot and its twin, empty, the worker on the slot only.
    slot, twin = f"swarm-tenant-{G_TENANT}-git", f"swarm-tenant-{G_TENANT}-git-refresh"
    # MUTATION: run qa_secret_ensure for every kind and swarm-tenant-<g>-qa appears
    # here: the deployer's secret binder stops at swarm-tenant-u-.
    assert set(world["secrets"]) == {slot, twin}, sorted(world["secrets"])
    assert not any(c["argv"][2:3] == [f"swarm-tenant-{G_TENANT}-qa"] for c in world["calls"]
                   if c["tool"] == "gcloud" and c["argv"][:1] == ["secrets"])
    assert {world["secrets"][s]["labels"]["tenant"] for s in (slot, twin)} == {G_TENANT}
    assert _members(world["secrets"][slot]["policy"], "roles/secretmanager.secretAccessor") == {
        f"serviceAccount:{G_WORKER}"}
    assert world["secrets"][twin]["policy"].get("bindings", []) == []
    # A7: the label, on the Namespace and nowhere else.
    labelled = [k for k, v in world["k8s"].items()
                if "swarm.saga.xyz/workspace" in ((v.get("metadata") or {}).get("labels") or {})]
    assert labelled == [f"Namespace//{G_NAMESPACE}"], labelled
    assert world["k8s"][f"Namespace//{G_NAMESPACE}"]["metadata"]["labels"]["swarm.saga.xyz/workspace"] == WORKSPACE
    # A8: a group tenant the directory resolves.
    tenant = world["firestore"][f"tenants/{G_TENANT}"]["fields"]
    assert (_field(tenant, "kind"), _field(tenant, "principal"), _field(tenant, "directory_group"),
            _field(tenant, "display_name"), _field(tenant, "namespace")) == (
        "group", GROUP, True, "research", G_NAMESPACE)
    assert not world.get("iap") and not [a for a in _gcloud_calls(world) if a[:1] == ["iap"]]
    assert set(world["writes"]) == {f"workspaces/{G_TENANT}", f"tenants/{G_TENANT}", f"pools/tenant:{G_TENANT}"}


@pytest.mark.parametrize("age", [25 * 3600, None], ids=["25-hours-old", "never-checked"])
def test_a1_fails_a_group_whose_directory_check_is_stale(job: Job, age: int | None) -> None:
    job.write(_group_world(directory_age=age))
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = _rec(job, G_TENANT)
    assert _field(record, "state") == "failed", out
    assert (_field(record, "failure", "step"), _field(record, "failure", "code"),
            _field(record, "failure", "retryable")) == ("A1", "DIRECTORY_CHECK_STALE", True), out
    assert _changes(job.calls("gcloud")) == []
    assert set(job.read()["writes"]) == {f"workspaces/{G_TENANT}"}


def _tenant_doc(principal: str, kind: str, **extra: dict) -> dict:
    return {"fields": {"tenant_id": _s("x"), "kind": _s(kind), "principal": _s(principal), **extra},
            "updateTime": "2026-10-01T00:00:00.000001Z"}


@pytest.mark.parametrize("make,code", [
    (lambda w: w["firestore"].__setitem__(f"tenants/{G_TENANT}", _tenant_doc(
        GROUP, "group", managed_by=_s("swarm-terraform"))), "WORKSPACE_ID_TERRAFORM"),
    (lambda w: w["firestore"].__setitem__(f"pools/tenant:{G_TENANT}", {
        "fields": {"name": _s(f"tenant:{G_TENANT}"), "managed_by": _s("swarm-terraform")}}), "WORKSPACE_ID_TERRAFORM"),
    (lambda w: w["firestore"].__setitem__(f"tenants/{G_TENANT}", _tenant_doc(
        "research@elsewhere.example", "group")), "WORKSPACE_ID_TAKEN"),
    (lambda w: w["firestore"].__setitem__(f"tenants/{G_TENANT}", _tenant_doc(GROUP, "user")), "WORKSPACE_ID_TAKEN"),
], ids=["terraform-tenant", "terraform-pool", "another-groups-tenant", "a-user-tenant"])
def test_a1_refuses_a_group_whose_tenant_is_someone_elses_before_any_write(job: Job, make, code: str) -> None:
    """A group `research@` of any domain slugs to `research`; Terraform's
    `eng` would too. A2-A7 would reach that tenant's worker, grants, slot and
    namespace, so A1 stops it first (security review of WK-JOB)."""
    world = _group_world()
    make(world)
    job.write(world)
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = _rec(job, G_TENANT)
    assert (_field(record, "failure", "step"), _field(record, "failure", "code")) == ("A1", code), out
    assert _changes(job.calls("gcloud")) == [], "a write came before A1 refused"
    assert set(job.read()["writes"]) == {f"workspaces/{G_TENANT}"}


def test_a1_refuses_a_service_whose_id_is_a_persons(job: Job) -> None:
    """`bogdan@<project>.iam.gserviceaccount.com` derives `u-bogdan`, the id
    of bogdan@example.com's personal workspace: never adopted."""
    world = _service_world()
    world["firestore"][f"tenants/{S_TENANT}"] = _tenant_doc("someone@example.com", "user")
    job.write(world)
    proc = job.create()
    out = _out(proc)
    assert proc.returncode == 1, out
    record = _rec(job, S_TENANT)
    assert (_field(record, "failure", "step"), _field(record, "failure", "code")) == ("A1", "WORKSPACE_ID_TAKEN"), out
    assert _changes(job.calls("gcloud")) == [] and not job.read().get("iap")


def test_a1_fails_a_group_whose_directory_check_is_dated_in_the_future(job: Job) -> None:
    job.write(_group_world(directory_age=-30 * 86400))
    proc = job.create()
    assert proc.returncode == 1, _out(proc)
    assert _field(_rec(job, G_TENANT), "failure", "code") == "DIRECTORY_CHECK_STALE"
    assert _changes(job.calls("gcloud")) == []


def test_verify_never_takes_the_migrated_shortcut_for_a_service(job: Job) -> None:
    """Only a person's record predates approval (§3.3): a service record marked
    migrated still needs the platform owner's approval."""
    world = _service_world(by=ADMIN)
    world["firestore"][f"workspaces/{S_TENANT}"]["fields"].update(
        state=_s("ready"), migrated={"booleanValue": True})
    job.write(world)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1 and "WORKSPACE_APPROVAL_OWNER_ONLY" in proc.stderr, _out(proc)
    assert job.read().get("writes", []) == []


def test_verify_finds_a_group_namespace_without_its_label(job: Job, group_created: dict) -> None:
    world = copy.deepcopy(group_created["world"])
    world["calls"], world["writes"] = [], []
    del world["k8s"][f"Namespace//{G_NAMESPACE}"]["metadata"]["labels"]["swarm.saga.xyz/workspace"]
    job.write(world)
    proc = job.run("--workspace", WORKSPACE, "--mode", "verify")
    assert proc.returncode == 1, _out(proc)
    record = _rec(job, G_TENANT)
    assert (_field(record, "failure", "code"), _field(record, "failure", "object")) == (
        "NAMESPACE_LABEL_MISSING", "Namespace")


# -- the guard, for a service's expectation ------------------------------------


def _service_expectation(job: Job, tmp: Path) -> dict[str, str]:
    record = tmp / "service-record.json"
    record.write_text(json.dumps(dict(_service_world()["firestore"][f"workspaces/{S_TENANT}"],
                                      name=f"projects/{PROJECT}/databases/swarm/documents/workspaces/{S_TENANT}")))
    job.init_guard()
    done = subprocess.run([str(GUARD), "expect", "--record", str(record)], env=job.env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(job.expect.read_text())


def _check(job: Job, *argv: str) -> subprocess.CompletedProcess[str]:
    Path(str(job.expect) + ".stop").unlink(missing_ok=True)
    return subprocess.run([str(GUARD), "check", "--", "gcloud", *argv], env=job.env,
                          capture_output=True, text=True, timeout=120, check=False)


def _admit(member: str = f"serviceAccount:{SERVICE}", service: str = FRONT_DOOR,
           verb: str = "add-iam-policy-binding", role: str = ACCESSOR) -> list[str]:
    return ["iap", "web", verb, "--project", PROJECT, "--resource-type=backend-services",
            f"--service={service}", f"--role={role}", f"--member={member}", "--condition=None"]


def test_the_guard_names_a_services_principal_and_the_api_backend(job: Job, tmp_path: Path) -> None:
    exp = _service_expectation(job, tmp_path)
    assert (exp["kind"], exp["tenant_id"], exp["service_principal"], exp["front_door_backend"]) == (
        "service", S_TENANT, SERVICE, FRONT_DOOR)
    assert exp["forge_slot"] == S_SLOT and "forge_slot_twin" not in exp
    assert exp["namespace_label"] == WORKSPACE


@pytest.mark.parametrize("argv,allowed", [
    (_admit(), True),
    (_admit(member=f"serviceAccount:other-bot@{PROJECT}.iam.gserviceaccount.com"), False),
    (_admit(member="allAuthenticatedUsers"), False),
    (_admit(service="swarm-ui-ui-backend"), False),
    (_admit(role="roles/iap.admin"), False),
    (_admit(verb="remove-iam-policy-binding"), False),
], ids=["the-approved-account", "another-account", "everyone", "the-console-backend", "another-role", "a-removal"])
def test_c10_admits_only_the_approved_account_on_the_api_backend(job: Job, tmp_path: Path, argv: list[str],
                                                                 allowed: bool) -> None:
    _service_expectation(job, tmp_path)
    proc = _check(job, *argv)
    if allowed:
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0 and ("rule C10" in proc.stderr or "rule C9" in proc.stderr), proc.stderr


def test_the_platform_owner_pinned_in_the_image_is_the_environments() -> None:
    """workspace-calls.json's platform_owner is what A1 holds a service
    approval to; it must be the owner the environment's tfvars name, for every
    environment of the project that names one."""
    import re

    owners = json.loads(RULES_FILE.read_text()).get("platform_owner", {})
    seen = 0
    for tfvars in sorted((REPO / "terraform" / "environments").glob("*/*.tfvars")):
        text = tfvars.read_text()
        project = re.search(r'^project_id\s*=\s*"([^"]+)"', text, re.M)
        owner = re.search(r'^platform_owner\s*=\s*"([^"]*)"', text, re.M)
        if project and owner and owner.group(1):
            seen += 1
            assert owners.get(project.group(1)) == owner.group(1).lower(), (tfvars, owners)
    assert seen >= 1, "no environment names a platform_owner"
    assert set(owners) <= {re.search(r'^project_id\s*=\s*"([^"]+)"', p.read_text(), re.M).group(1)
                           for p in (REPO / "terraform" / "environments").glob("*/*.tfvars")}


def test_the_terraform_tenants_pinned_in_the_image_are_each_environments() -> None:
    """workspace-calls.json's terraform_tenants is what the guard refuses a
    group or service expectation for: every environment's var.tenants keys,
    read the way kubernetes/render.py reads them for the scope policy."""
    sys.path.insert(0, str(REPO / "kubernetes"))
    import render

    pinned = json.loads(RULES_FILE.read_text()).get("terraform_tenants", {})
    files = sorted((REPO / "terraform" / "environments").glob("*/*.tfvars"))
    assert files, "no environment tfvars found"
    assert set(pinned) == {f.parent.name for f in files}, sorted(pinned)
    for tfvars in files:
        assert sorted(pinned[tfvars.parent.name]) == sorted(render.terraform_tenants(tfvars)), tfvars


@pytest.mark.parametrize("kind,principal,tenant", [
    ("group", "eng@example.com", "eng"),
    ("group", "smoke@example.com", "smoke"),
    ("service", f"oracle-api@{PROJECT}.iam.gserviceaccount.com", "u-or-9a0792"),
], ids=["eng", "smoke", "u-or-9a0792"])
def test_the_guard_writes_no_expectation_for_a_terraform_tenant(job: Job, tmp_path: Path, kind: str,
                                                                 principal: str, tenant: str) -> None:
    record = tmp_path / "record.json"
    fields = _kind_record(kind, tenant, principal, OWNER)["fields"]
    record.write_text(json.dumps({"name": f"projects/{PROJECT}/databases/swarm/documents/workspaces/{tenant}",
                                  "fields": fields}))
    job.init_guard()
    done = subprocess.run([str(GUARD), "expect", "--record", str(record)], env=job.env,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode != 0 and "WORKSPACE_ID_TERRAFORM" in done.stderr, done.stderr
    assert json.loads(job.expect.read_text())["phase"] == "bootstrap", "an expectation was written"
