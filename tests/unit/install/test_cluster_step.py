"""Stage 10, the cluster's policies and swarm-system (DAYONE-ENGINE-4; design
§4; lessons 29, 33, 37; manifest `other.release-kubernetes-bundle`).

On fakes, with no credentials and no cluster: the admission policies are
applied before swarm-system's objects, and swarm-system refuses to go while
they differ; RBAC subjects are the uniqueIds IAM assigned, never an email;
kubectl is resolved the way `prefer_local_bin` resolves it; every kubectl
call is pinned to the install cluster's context in the engine's own
kubeconfig. (That the render comes from the release's checked bundle is
test_verify_step.py::test_cluster_objects_from_release_bundle, as the plan names it.)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.steps import cluster
from swarm_install.steps.preflight import RELEASE_HOME

from .test_preflight import PROJECT, Answers, FakeGcp, Rig, ctx_for

VERSION = "v1.0.0"
REGION = "us-central1"
CONTEXT = f"gke_{PROJECT}_{REGION}_{cluster.DEFAULT_CLUSTER}"
UIDS = {
    cluster.account_email(PROJECT, "swarm-scheduler"): "100000000000000000001",
    cluster.account_email(PROJECT, "swarm-reconciler"): "100000000000000000002",
    cluster.account_email(PROJECT, "swarm-workspace-deployer"): "100000000000000000003",
}
POLICIES = ("---\nkind: ValidatingAdmissionPolicy\nmetadata:\n  name: swarm-worker-pod-constraints\n"
            "---\nkind: ClusterRole\nmetadata:\n  name: swarm-provisioner\n")
SWARM_SYSTEM = "---\nkind: NetworkPolicy\nmetadata:\n  name: default-deny-ingress\n  namespace: swarm-system\n"


def bundle_bytes(extra: dict[str, bytes] | None = None) -> bytes:
    files = {"render.py": b"# the release's renderer\n", "policies/vap.yaml": b"kind: X\n",
             "swarm-system/netpol.yaml": b"kind: Y\n", "rbac/provisioner.yaml": b"kind: Z\n", **(extra or {})}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class ClusterFakeGcp(FakeGcp):
    """IAM knows each policy subject's uniqueId; the release home holds the bundle."""

    def __init__(self, *, uids: dict[str, str] | None = None, bundle: bytes | None = None,
                 summed: bytes | None = None) -> None:
        super().__init__()
        self.uids = dict(UIDS if uids is None else uids)
        data = bundle if bundle is not None else bundle_bytes()
        base = f"{RELEASE_HOME}/releases/{VERSION}"
        self.release[f"{base}/{cluster.BUNDLE}"] = data
        digest = hashlib.sha256(summed if summed is not None else data).hexdigest()
        self.release[f"{base}/SHA256SUMS"] += f"{digest}  {cluster.BUNDLE}\n".encode()

    def service_account(self, email: str) -> dict[str, Any] | None:
        self._call("service_account", email)
        uid = self.uids.get(email)
        return {"email": email, "uniqueId": uid} if uid else {"email": email}

    def run_service(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        self._call("run_service", project, region, name)
        subnet = f"projects/{project}/regions/{region}/subnetworks/swarm-api-egress"
        return {"template": {"vpcAccess": {"networkInterfaces": [{"subnetwork": subnet}]}}}

    def subnetwork(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        self._call("subnetwork", project, region, name)
        return {"ipCidrRange": "10.8.0.0/28"}


class FakeShell:
    """gcloud, kubectl and render.py, faked. The cluster holds what was applied;
    `kubectl diff` exits 0 only for a render it already holds."""

    def __init__(self, *, current_context: str = CONTEXT) -> None:
        self.current_context = current_context
        self.calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []
        self.applied: list[str] = []
        # swarm-system's core workloads: name -> (replicas, ready); None is NotFound.
        self.workloads: dict[str, tuple[int, int] | None] = {
            "statefulset/swarm-live": (1, 1), "deployment/swarm-graph": (2, 2)}

    def kubectl_calls(self) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if argv[0].endswith("kubectl")]

    def __call__(self, argv: Sequence[str], stdin: str | None,
                 env: dict[str, str] | None) -> tuple[int, str, str]:
        argv = list(argv)
        self.calls.append((argv, stdin, env))
        if argv[1].endswith("render.py"):
            return 0, POLICIES if argv[2] == "policies" else SWARM_SYSTEM, ""
        if argv[0].endswith("gcloud"):
            return 0, "", ""
        verbs = [a for a in argv[1:] if not a.startswith("--")]
        if verbs[:2] == ["config", "current-context"]:
            return 0, self.current_context + "\n", ""
        if verbs[0] == "diff":
            return (0, "", "") if stdin in self.applied else (1, "diff -u a/x b/x\n", "")
        if verbs[0] == "apply":
            self.applied.append(str(stdin))
            return 0, "", ""
        if verbs[0] == "get" and verbs[1] in self.workloads:
            found = self.workloads[verbs[1]]
            if found is None:
                return 1, "", f'Error from server (NotFound): {verbs[1]} not found'
            return 0, json.dumps({"spec": {"replicas": found[0]}, "status": {"readyReplicas": found[1]}}), ""
        if verbs[0] == "get":
            return 0, "namespace/swarm-system\n", ""
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The engine's kubeconfig and bundle live under ~/.config; keep them in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SWARM_KUBECTL", raising=False)
    return tmp_path / "home"


def _kube(shell: FakeShell, deny: Sequence[str] = ()) -> Any:
    def make(ctx: Any) -> cluster.Kube:
        return cluster.Kube(project=ctx.project, region=str(ctx.state.region), cluster=cluster.cluster_name(ctx),
                            deny_list=tuple(deny) or tuple(ctx.state.deny_list),
                            find=lambda tool: "/opt/bin/kubectl", run=shell,
                            gcloud=lambda: "/opt/bin/gcloud")
    return make


def _steps(gcp: ClusterFakeGcp, shell: FakeShell, deny: Sequence[str] = ()) -> list[Any]:
    return cluster.build_steps(lambda: gcp, Answers(), kube=_kube(shell, deny), run=shell)


def _ctx() -> Any:
    ctx = ctx_for(dry_run=False)
    ctx.state.release = VERSION
    return ctx


def _rig(tmp_path: Path) -> Rig:
    rig = Rig(tmp_path)

    def seed(state: Any) -> None:
        state.region = REGION
        state.release = VERSION
    rig.store.update(seed)
    return rig


def test_policies_before_tenant_objects(tmp_path: Path) -> None:
    """Lesson 33: the policies are applied before swarm-system's objects, and
    swarm-system refuses to apply while the policies differ from the render."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    bundle, policies, swarm_system, _ = _steps(gcp, shell)
    assert [s.id for s in (bundle, policies, swarm_system)] == ["cluster.bundle", "cluster.policies",
                                                               "cluster.swarm-system"]
    assert {s.stage for s in (bundle, policies, swarm_system)} == {10}

    ctx = _ctx()
    with pytest.raises(RuntimeError, match="admission policies go first"):
        swarm_system.apply(ctx, swarm_system.plan(ctx))
    assert shell.applied == []

    result = _rig(tmp_path).engine(_steps(gcp, shell := FakeShell())).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert shell.applied == [POLICIES, SWARM_SYSTEM]


def test_subjects_from_unique_id_read_from_iam() -> None:
    """Lesson 29: the RBAC's subjects are the uniqueIds IAM assigned; an
    account IAM gives no uniqueId stops the render before it runs."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    _, policies, *_ = _steps(gcp, shell)
    policies.rendered(_ctx())
    render = next(argv for argv, _, _ in shell.calls if argv[1].endswith("render.py"))
    assert "--scheduler-uid=100000000000000000001" in render
    assert "--reconciler-uid=100000000000000000002" in render
    assert "--workspace-deployer-uid=100000000000000000003" in render
    assert not any("@" in arg for arg in render[2:])
    assert set(UIDS) <= {args[0] for args in gcp.called("service_account")}

    no_uid = dict(UIDS)
    no_uid.pop(cluster.account_email(PROJECT, "swarm-reconciler"))
    gcp, shell = ClusterFakeGcp(uids=no_uid), FakeShell()
    _, policies, *_ = _steps(gcp, shell)
    with pytest.raises(LookupError, match="lesson 29"):
        policies.check(_ctx())
    assert not any(argv[1].endswith("render.py") for argv, _, _ in shell.calls)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_kubectl_resolved_through_prefer_local_bin(home: Path, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """`SWARM_KUBECTL`, then ~/.local/bin, then $PATH: an old client first on
    $PATH is never the one the engine runs."""
    on_path = _executable(tmp_path / "path" / "kubectl")
    local = _executable(home / ".local" / "bin" / "kubectl")
    monkeypatch.setenv("PATH", f"{on_path.parent}{os.pathsep}{os.environ.get('PATH', '')}")

    shell = FakeShell()
    kube = cluster.Kube(project=PROJECT, region=REGION, cluster=cluster.DEFAULT_CLUSTER, run=shell,
                        gcloud=lambda: "/opt/bin/gcloud")
    assert kube.kubectl() == str(local)
    kube("get", "namespace", "swarm-system")
    assert {argv[0] for argv in shell.kubectl_calls()} == {str(local)}

    monkeypatch.setenv("SWARM_KUBECTL", "/opt/pinned/kubectl")
    assert kube.kubectl() == "/opt/pinned/kubectl"
    monkeypatch.delenv("SWARM_KUBECTL")
    local.unlink()
    assert kube.kubectl() == str(on_path)


def test_context_is_the_install_cluster(home: Path) -> None:
    """Lesson 37: every kubectl call names the engine's own kubeconfig and the
    install cluster's context; a kubeconfig that lands on another cluster, or
    a deny-listed cluster, is refused before kubectl acts."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    _, policies, swarm_system, _ = _steps(gcp, shell)
    ctx = _ctx()
    assert policies.check(ctx).status == UNMET
    assert swarm_system.check(ctx).status == UNMET
    config = home / ".config" / "swarmcloud" / "install" / f"{PROJECT}.kubeconfig"
    acting = [argv for argv in shell.kubectl_calls() if "current-context" not in argv]
    assert acting
    for argv in acting:
        assert f"--kubeconfig={config}" in argv and f"--context={CONTEXT}" in argv
    for _, _, env in shell.calls:
        if env is not None:
            assert env["KUBECONFIG"] == str(config)
    creds = [argv for argv, _, _ in shell.calls if argv[0].endswith("gcloud")]
    assert creds and len(creds) <= 2  # at most once per step, and only for the install cluster
    for argv in creds:
        assert argv[4] == cluster.DEFAULT_CLUSTER and f"--project={PROJECT}" in argv

    other = FakeShell(current_context="gke_saga-agents-staging_us-central1_agents-staging")
    _, policies, *_ = _steps(ClusterFakeGcp(), other)
    with pytest.raises(cluster.WrongCluster, match=CONTEXT):
        policies.check(_ctx())
    assert not [a for a in other.kubectl_calls() if "current-context" not in a]

    denied = FakeShell()
    ctx = _ctx()
    ctx.outputs["gke_cluster_name"] = "agents-staging"
    _, policies, *_ = _steps(ClusterFakeGcp(), denied, deny=("agents-staging",))
    with pytest.raises(cluster.WrongCluster, match="deny-list"):
        policies.check(ctx)
    assert not [argv for argv, _, _ in denied.calls if not argv[1].endswith("render.py")]


def test_steps_satisfy_every_stage_10_entry() -> None:
    from swarm_install.manifest import load

    ids = {e.id for e in load().entries}
    covered = {entry for step in cluster.STEPS for entry in step.satisfies}
    assert covered <= ids
    assert "other.release-kubernetes-bundle" in covered and "cluster.swarm-system-namespace" in covered
    # Review finding 5: every core stage 10 entry has a step; the addons' are their addons'.
    core = {e.id for e in load().entries if e.stage == 10 and e.scope == "core"}
    assert core <= covered
    assert {"workload.swarm-live", "workload.swarm-graph"} <= covered


def test_core_workloads_ready_in_swarm_system() -> None:
    """Review finding 5: swarm-live and swarm-graph, Terraform's renders, are
    checked ready in swarm-system on the install cluster's context; a missing
    one names its Terraform output, and the apply names the pass that writes
    it and applies nothing."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    *_, workloads = _steps(gcp, shell)
    ctx = _ctx()
    assert workloads.check(ctx).status == MET
    gets = [argv for argv in shell.kubectl_calls() if "get" in argv and "-o" in argv]
    assert len(gets) == 2 and all(f"--context={CONTEXT}" in argv for argv in gets)
    assert all("--namespace=swarm-system" in argv for argv in gets)

    shell.workloads["statefulset/swarm-live"] = None
    shell.workloads["deployment/swarm-graph"] = (2, 1)
    observed = workloads.check(ctx)
    assert observed.status == UNMET
    assert "swarm_live_manifest" in observed.detail and "1 of 2 replicas" in observed.detail
    with pytest.raises(RuntimeError, match="DAYONE-ENGINE-3"):
        workloads.apply(ctx, workloads.plan(ctx))
    assert shell.applied == []
