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
from swarm_install.terraform import Completed, TerraformRefused

from .test_preflight import PROJECT, Answers, FakeGcp, Rig, ctx_for

VERSION = "v1.0.0"
REGION = "us-central1"
CONTEXT = f"gke_{PROJECT}_{REGION}_{cluster.DEFAULT_CLUSTER}"
UIDS = {
    cluster.account_email(PROJECT, "swarm-scheduler"): "100000000000000000001",
    cluster.account_email(PROJECT, "swarm-reconciler"): "100000000000000000002",
    cluster.account_email(PROJECT, "swarm-workspace-deployer"): "100000000000000000003",
}
LIVE = "---\nkind: StatefulSet\nmetadata:\n  name: swarm-live\n  namespace: swarm-system\n"
GRAPH = "---\nkind: Deployment\nmetadata:\n  name: swarm-graph\n  namespace: swarm-system\n"
UNREACHABLE = "Unable to connect to the server: dial tcp 34.1.2.3:443: i/o timeout"
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


def sign(blob: bytes) -> bytes:
    """The fake release key's signature: what `FakeCosign` accepts for `blob`."""
    return b"signed " + hashlib.sha256(blob).hexdigest().encode()


class FakeCosign:
    """`cosign verify-blob --key K --signature S BLOB`, faked: it verifies when S
    is `sign(BLOB)`, as the pinned release key would."""

    def __init__(self) -> None:
        self.verified: list[str] = []

    def __call__(self, argv: Sequence[str], env: Any, cwd: Path) -> Completed:
        argv = list(argv)
        assert argv[1] == "verify-blob", argv
        signature = Path(argv[argv.index("--signature") + 1]).read_bytes()
        blob = Path(argv[-1])
        if signature != sign(blob.read_bytes()):
            return Completed(1, "", "Error: invalid signature when validating ASN.1 encoded signature")
        self.verified.append(blob.name)
        return Completed(0, "Verified OK", "")


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
        self.release[f"{base}/SHA256SUMS.sig"] = sign(self.release[f"{base}/SHA256SUMS"])

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

    def __init__(self, *, current_context: str = CONTEXT, unreachable: int = 0,
                 unreachable_error: str = UNREACHABLE) -> None:
        self.current_context = current_context
        # The control plane answers neither the first `unreachable` acting calls.
        self.unreachable = unreachable
        self.unreachable_error = unreachable_error
        # kind/name -> the error `kubectl rollout status` ends with; absent rolls out.
        self.rollouts: dict[str, str] = {}
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
        if self.unreachable:
            self.unreachable -= 1
            return 1, "", self.unreachable_error
        if verbs[0] == "diff":
            return (0, "", "") if stdin in self.applied else (1, "diff -u a/x b/x\n", "")
        if verbs[0] == "apply":
            self.applied.append(str(stdin))
            for manifest, name in ((LIVE, "statefulset/swarm-live"), (GRAPH, "deployment/swarm-graph")):
                if stdin == manifest:
                    self.workloads[name] = (1, 1)
            return 0, "", ""
        if verbs[:2] == ["rollout", "status"]:
            if verbs[2] in self.rollouts:
                return 1, "", self.rollouts[verbs[2]]
            return 0, f"{verbs[2]} successfully rolled out\n", ""
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
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("SWARM_KUBECTL", raising=False)
    return tmp_path / "home"


@pytest.fixture
def cosign(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCosign:
    """bootstrap's `Release`, with cosign and the pinned key faked (finding 31)."""
    fake = FakeCosign()
    real = cluster.Release

    def release(read_public: Any) -> Any:
        return real(read_public=read_public, exec_=fake, cosign=lambda: tmp_path / "cosign",
                    key=lambda: tmp_path / "release_key.pub")

    monkeypatch.setattr(cluster, "Release", release)
    return fake


@pytest.fixture(autouse=True)
def _signed_release(cosign: FakeCosign) -> FakeCosign:
    """Every stage 10 test reads the bundle through the faked `Release`."""
    return cosign


def fake_plugin() -> Path:
    """`tools.ensure("gke-gcloud-auth-plugin")`'s answer: the pinned binary,
    cached under its versioned name, as `ensure` caches every tool."""
    return _executable(Path(os.environ["HOME"]) / ".cache" / "swarmcloud" / "tools" / "gke-gcloud-auth-plugin-0.5.10")


def _kube(shell: FakeShell, deny: Sequence[str] = ()) -> Any:
    def make(ctx: Any) -> cluster.Kube:
        return cluster.Kube(project=ctx.project, region=str(ctx.state.region), cluster=cluster.cluster_name(ctx),
                            deny_list=tuple(deny) or tuple(ctx.state.deny_list),
                            find=lambda tool: "/opt/bin/kubectl", run=shell,
                            gcloud=lambda: "/opt/bin/gcloud", plugin=fake_plugin,
                            clock=ctx.clock, events=ctx.events)
    return make


class FakeOutputs:
    """terraform/infra's outputs, as `terraform output -json` gives them (a null
    output is absent); records each read."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.outputs = {"swarm_live_manifest": LIVE, "swarm_graph_manifest": GRAPH} if outputs is None else outputs
        self.reads = 0

    def __call__(self, ctx: Any) -> dict[str, Any]:
        self.reads += 1
        return dict(self.outputs)


def _steps(gcp: ClusterFakeGcp, shell: FakeShell, deny: Sequence[str] = (),
           outputs: FakeOutputs | None = None) -> list[Any]:
    return cluster.build_steps(lambda: gcp, Answers(), kube=_kube(shell, deny), run=shell,
                               outputs=outputs or FakeOutputs())


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
    # The relay and the graph service, infra's renders, go last (DAYONE-CLUSTER-REACH).
    assert shell.applied == [POLICIES, SWARM_SYSTEM, LIVE, GRAPH]


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
                        gcloud=lambda: "/opt/bin/gcloud", plugin=fake_plugin)
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
    one names its Terraform output."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    shell.applied.extend([LIVE, GRAPH])
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


# -- DAYONE-CLUSTER-REACH: the laptop reaches the control plane, and stage 10
# applies the relay and the graph service from infra's outputs -------------------


def test_credentials_in_the_engines_kubeconfig_with_the_auth_plugin_on_path(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get-credentials writes the engine's own kubeconfig, never the person's
    ~/.kube/config or $KUBECONFIG; gcloud and kubectl both see a PATH whose
    first directory holds `gke-gcloud-auth-plugin` (the kubeconfig's exec
    names it bare), the pinned binary `tools.ensure` returned."""
    theirs = tmp_path / "theirs.kubeconfig"
    monkeypatch.setenv("KUBECONFIG", str(theirs))
    monkeypatch.setenv("PATH", "/usr/bin")
    ensured: list[str] = []

    def ensure(name: str, **_: Any) -> Path:
        ensured.append(name)
        return fake_plugin()

    monkeypatch.setattr(cluster.tools, "ensure", ensure)
    shell = FakeShell()
    ctx = _ctx()
    kube = cluster.kube_for(ctx, find=lambda tool: "/opt/bin/kubectl", run=shell)
    kube.gcloud = lambda: "/opt/bin/gcloud"
    assert kube("get", "namespace", "swarm-system")[0] == 0
    assert ensured == ["gke-gcloud-auth-plugin"]

    config = home / ".config" / "swarmcloud" / "install" / f"{PROJECT}.kubeconfig"
    creds = [(argv, env) for argv, _, env in shell.calls if argv[0].endswith("gcloud")]
    assert len(creds) == 1 and creds[0][0][1:4] == ["container", "clusters", "get-credentials"]
    for argv, _, env in shell.calls:
        if argv[1].endswith("render.py"):
            continue
        assert env is not None and env["KUBECONFIG"] == str(config), argv
        first = Path(env["PATH"].split(os.pathsep)[0])
        assert (first / "gke-gcloud-auth-plugin").resolve() == fake_plugin().resolve(), argv
        assert "/usr/bin" in env["PATH"].split(os.pathsep)
    assert not theirs.exists() and not (home / ".kube").exists()
    assert os.environ["KUBECONFIG"] == str(theirs)  # the person's own environment is untouched


def test_unreachable_control_plane_is_polled_with_a_countdown() -> None:
    """The authorized network (installer_cidr) takes a minute to reach the
    control plane: a timeout or a refused connection is polled, with a
    countdown, until it answers; any other error is the caller's at once;
    past the bound the wait gives up (exit 4, paused)."""
    from swarm_install.waits import WaitGaveUp

    for error in (UNREACHABLE, "The connection to the server 34.1.2.3:443 was refused - did you specify "
                               "the right host or port?"):
        ctx = _ctx()
        shell = FakeShell(unreachable=2, unreachable_error=error)
        kube = _kube(shell)(ctx)
        code, out, _ = kube("get", "namespace", "swarm-system", "-o", "name")
        assert (code, out) == (0, "namespace/swarm-system\n")
        waits = ctx.events.of_kind("wait_countdown")
        assert len(waits) == 2 and waits[0]["reason"] == cluster.CONTROL_PLANE.reason
        assert ctx.clock.sleeps == [cluster.CONTROL_PLANE.interval_s] * 2

    ctx = _ctx()
    shell = FakeShell()
    code, _, err = _kube(shell)(ctx)("get", "statefulset/nothing", "--namespace=swarm-system", "-o", "json")
    assert code == 0 and not ctx.events.of_kind("wait_countdown")

    ctx = _ctx()
    shell = FakeShell(unreachable=10_000)
    with pytest.raises(WaitGaveUp, match="installer_cidr"):
        _kube(shell)(ctx)("get", "namespace", "swarm-system")
    assert ctx.events.of_kind("wait_gave_up")


def test_relay_and_graph_applied_from_infra_outputs() -> None:
    """swarm_live_manifest and swarm_graph_manifest, read from infra's
    Terraform outputs, are applied with the engine's pinned kubectl on the
    install cluster's context, then their rollouts are waited for."""
    gcp, shell, outputs = ClusterFakeGcp(), FakeShell(), FakeOutputs()
    shell.workloads = {"statefulset/swarm-live": None, "deployment/swarm-graph": None}
    *_, workloads = _steps(gcp, shell, outputs=outputs)
    ctx = _ctx()
    observed = workloads.check(ctx)
    assert observed.status == UNMET and "swarm_live_manifest" in observed.detail
    plan = workloads.plan(ctx)
    assert any("swarm_graph_manifest" in line for line in plan.actions)

    workloads.apply(ctx, plan)
    assert shell.applied == [LIVE, GRAPH]
    applies = [argv for argv in shell.kubectl_calls() if "apply" in argv]
    assert len(applies) == 2
    for argv in applies:
        assert argv[0] == "/opt/bin/kubectl" and f"--context={CONTEXT}" in argv and "--server-side" in argv
    rollouts = [argv for argv in shell.kubectl_calls() if "rollout" in argv]
    assert [a[a.index("status") + 1] for a in rollouts] == ["statefulset/swarm-live", "deployment/swarm-graph"]
    assert all("--namespace=swarm-system" in a for a in rollouts)
    assert workloads.verify(ctx).status == MET
    assert outputs.reads >= 1


def test_unhealthy_rollout_fails_the_apply() -> None:
    gcp, shell = ClusterFakeGcp(), FakeShell()
    shell.rollouts["deployment/swarm-graph"] = "error: timed out waiting for the condition"
    *_, workloads = _steps(gcp, shell)
    ctx = _ctx()
    with pytest.raises(RuntimeError, match="deployment/swarm-graph.*timed out"):
        workloads.apply(ctx, workloads.plan(ctx))


def test_switched_off_workload_is_not_demanded() -> None:
    """Terraform renders null while a workload is switched off (live.tf,
    graph_service.tf); nothing is applied for it and the step does not wait on it."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    shell.workloads = {"statefulset/swarm-live": None, "deployment/swarm-graph": None}
    *_, workloads = _steps(gcp, shell, outputs=FakeOutputs({"swarm_live_manifest": LIVE}))
    ctx = _ctx()
    workloads.apply(ctx, workloads.plan(ctx))
    assert shell.applied == [LIVE]
    observed = workloads.check(ctx)
    assert observed.status == MET and "swarm_graph_manifest" in observed.detail


def test_infra_outputs_read_through_terraform(tmp_path: Path) -> None:
    """The manifests come from `terraform output -json` in the release's infra
    root, after `init`, as the deployer (as every infra pass runs); an output
    Terraform left null is absent, read as None."""
    from swarm_install import terraform
    from swarm_install.steps.bootstrap import Deps

    calls: list[tuple[list[str], dict[str, str]]] = []

    def exec_(argv: Sequence[str], env: Any, cwd: Path) -> terraform.Completed:
        calls.append((list(argv), dict(env)))
        if argv[1] == "version":
            return terraform.Completed(0, json.dumps({"terraform_version": terraform.TERRAFORM_VERSION}), "")
        if argv[1] == "output":
            return terraform.Completed(0, json.dumps({"swarm_live_manifest": {"value": LIVE, "sensitive": False},
                                                      "gke_cluster_name": {"value": "swarm-agents"}}), "")
        return terraform.Completed(0, "", "")

    deps = Deps(gcp=lambda: None, exec_=exec_, root_dir=lambda ctx, root: tmp_path / root)
    found = cluster.infra_outputs(_ctx(), deps=deps)
    assert found["swarm_live_manifest"] == LIVE and found.get("swarm_graph_manifest") is None
    verbs = [argv[1] for argv, _ in calls]
    assert verbs.index("init") < verbs.index("output")
    output_env = next(env for argv, env in calls if argv[1] == "output")
    assert terraform.IMPERSONATE_ENV in output_env and PROJECT in output_env[terraform.IMPERSONATE_ENV]


# -- finding 31: the bundle is read through Release.fetch ---------------------------


def test_bundle_with_unsigned_sums_never_renders(cosign: FakeCosign) -> None:
    """Finding 31: SHA256SUMS is cosign-verified against the pinned release key
    before the bundle's digest is compared and before render.py runs. A bundle
    and a SHA256SUMS swapped together (the digest matches, the signature does
    not) is refused, and nothing from it runs."""
    gcp, shell = ClusterFakeGcp(), FakeShell()
    base = f"{RELEASE_HOME}/releases/{VERSION}"
    swapped = bundle_bytes({"render.py": b"raise SystemExit('the swapped renderer ran')\n"})
    gcp.release[f"{base}/{cluster.BUNDLE}"] = swapped
    gcp.release[f"{base}/SHA256SUMS"] = f"{hashlib.sha256(swapped).hexdigest()}  {cluster.BUNDLE}\n".encode()
    _, policies, *_ = _steps(gcp, shell)
    with pytest.raises(TerraformRefused, match="does not verify with the pinned release key"):
        policies.rendered(_ctx())
    assert not any(argv[1].endswith("render.py") for argv, _, _ in shell.calls), "render.py never ran"
    assert not cluster.bundle_dir(PROJECT, VERSION).exists(), "nothing unpacked"


def test_bundle_sums_verified_before_render(cosign: FakeCosign) -> None:
    gcp, shell = ClusterFakeGcp(), FakeShell()
    _, policies, *_ = _steps(gcp, shell)
    policies.rendered(_ctx())
    assert cosign.verified == ["SHA256SUMS"]
    assert f"{RELEASE_HOME}/releases/{VERSION}/SHA256SUMS.sig" in {a[0] for a in gcp.called("read_public")}
    assert any(argv[1].endswith("render.py") for argv, _, _ in shell.calls)


# -- finding 16: kubectl reopens the control plane after the closing pass -----------


def test_kube_reopens_once_before_its_first_kubectl_call(home: Path) -> None:
    shell = FakeShell()
    reopened: list[int] = []
    ctx = _ctx()
    ctx.state.region = REGION
    kube = cluster.Kube(project=PROJECT, region=REGION, cluster=cluster.DEFAULT_CLUSTER,
                        find=lambda tool: "/opt/bin/kubectl", run=shell, gcloud=lambda: "/opt/bin/gcloud",
                        plugin=fake_plugin, clock=ctx.clock, events=ctx.events,
                        reach=lambda: reopened.append(len(shell.kubectl_calls())))
    kube.credentials()
    assert reopened == [], "get-credentials reaches the GKE API, not the control plane"
    kube("get", "namespace", "swarm-system")
    kube("get", "namespace", "swarm-system")
    # Only `config current-context` (no control plane) ran before the reopen.
    assert reopened == [1]


def test_kube_for_reopens_through_the_reach_on_an_engine_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_install.steps import infra

    ctx = _ctx()
    ctx.state.region = REGION
    ctx.engine = object()  # an engine-shaped context: it can apply a pass
    seen: list[Any] = []
    monkeypatch.setattr(infra.REACH, "reopen", lambda c: seen.append(c))
    kube = cluster.kube_for(ctx)
    assert kube.reach is not None
    kube.reach()
    assert seen == [ctx]

    class Bare:
        """Uninstall's context: no engine, so no pass can be applied from it."""
        project = PROJECT
        state = ctx.state
        outputs = ctx.state.outputs

    assert cluster.kube_for(Bare()).reach is None
