"""Stage 10, the cluster's admission policies and swarm-system's ingress
(design §4: stage 10 before 11; lessons 29, 33, 37; manifest
`other.release-kubernetes-bundle`).

| Step | Manifest entries | Kind |
|---|---|---|
| `cluster.bundle` | `other.release-kubernetes-bundle` | engine: the release's `kubernetes.tgz`, checked against its cosign-verified `SHA256SUMS` (`Release.fetch`) and unpacked; never a checkout |
| `cluster.policies` | `cluster-policy.*` (9), `rbac.provisioner-clusterrole` | engine: `render.py policies` from the bundle, applied before anything a tenant runs |
| `cluster.swarm-system` | `cluster.swarm-system-namespace` | engine: swarm-system's default-deny ingress, only once the policies are in |
| `cluster.workloads` | `workload.swarm-live`, `workload.swarm-graph` | engine: the live relay and the graph service, infra's renders, applied and rolled out in swarm-system |

**swarm-system's two core workloads are Terraform's renders.** The live
relay's manifest is terraform/infra's `swarm_live_manifest` output and the
graph service's `swarm_graph_manifest` (`kubernetes/apply.sh --swarm-system`
and `--swarm-graph` apply them; neither is in the release bundle). Stage 10
reads them with `terraform output` in the release's infra root, as the
deployer (`terraform.read_outputs`), applies them with the pinned kubectl and
waits for their rollouts. Terraform renders null while a workload is switched
off (`enable_live_browser`, `graph_service.serve`): nothing is applied for it
and nothing waits on it. The addon workloads (`swarm-egress`,
`swarm-typesafe-fwd`, `tailscale-egress`) belong to their addons' installs.

**Policies before the code that needs them** (lesson 33): the scheduler once
shipped sizes the admission policy did not yet admit, and 54 dispatches
failed. Stage 10 runs before stage 11 (the first team's namespace), and
`cluster.swarm-system` refuses to apply while the policies differ from the
render.

**Subjects by uniqueId, read from IAM** (lesson 29): a RoleBinding whose
subject is a Google service account's email is never matched by GKE, which
names the caller by its numeric uniqueId. The engine reads each account's
`uniqueId` from IAM and refuses to render without it; `render.py` would fall
back to the email, an inert duplicate.

**The install cluster's context, and only it** (lesson 37): the laptop's
current context was once the other team's cluster. The engine writes its own
kubeconfig (`~/.config/swarmcloud/install/<project>.kubeconfig`), fetches the
install cluster's credentials into it, and passes `--kubeconfig` and
`--context gke_<project>_<region>_<cluster>` on every call; a context naming
any other cluster, or a deny-listed one, is refused before kubectl runs. The
person's own `~/.kube/config` and `$KUBECONFIG` are never read or written.

**The auth plugin, first on PATH.** The kubeconfig get-credentials writes runs
`gke-gcloud-auth-plugin` by its bare name, so gcloud and every kubectl call see
a PATH whose first directory holds it: the pinned binary from
`tools.ensure("gke-gcloud-auth-plugin")`, linked under that name beside the
kubeconfig.

**The control plane admits this machine a minute late** (decision 2): the
infra passes put `installer_cidr` in `gke_master_authorized_cidrs`, and the
change takes about a minute to reach the control plane. A kubectl call that
times out or is refused is polled with a countdown (`CONTROL_PLANE`) until the
control plane answers; any other error is returned at once.

**Reopened after the closing pass** (finding 16): `kube_for` gives the `Kube`
`infra.REACH.reopen` for an engine-shaped context (one with `engine`), run once
before its first kubectl call: when `infra.pass-close` has removed the
address, an infra pass puts this machine's current `/32` back, and the closing
pass (or the verb, when it finishes) removes it again. A context with no
`engine` (uninstall's) is never reopened.

**The bundle is signature-checked** (finding 31): `kubernetes.tgz` is read
through bootstrap's `Release.fetch`, so `SHA256SUMS` verifies with cosign
against the pinned release key before the bundle's digest is compared and
before `render.py` runs, as bootstrap does for `module.tgz`.

**kubectl through `prefer_local_bin`'s rule** (`preflight.find_tool`):
`SWARM_KUBECTL`, then `~/.local/bin`, then `$PATH`. An old client on `$PATH`
silently drops manifest fields.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import tools
from ..api_client import deployer_email
from ..events import Events
from ..exitcodes import MET, UNMET
from ..state import state_bucket
from ..terraform import Terraform, read_outputs
from ..waits import Clock, SystemClock, WaitSpec, wait_until
from . import Observation, Plan
from .bootstrap import Release
from .preflight import ASK, COMPUTE, TOOLS, Ask, Gcp, find_tool

#: terraform/infra's cluster (`google_container_cluster.agents`); infra pass 1
#: records the name it created as `gke_cluster_name`.
DEFAULT_CLUSTER = "swarm-agents"
BUNDLE = "kubernetes.tgz"
RUN_API = "https://run.googleapis.com/v2"
BUNDLE_MUST_HOLD = ("render.py", "policies/", "swarm-system/", "rbac/")

#: The accounts the cluster-scoped RBAC names (kubernetes/render.py
#: `policy_values`), each by email and by the uniqueId IAM assigned.
POLICY_SUBJECTS = (
    ("scheduler", "swarm-scheduler"),
    ("reconciler", "swarm-reconciler"),
    ("workspace-deployer", "swarm-workspace-deployer"),
)

KUBECTL = next(t for t in TOOLS if t.name == "kubectl")
#: The name the kubeconfig's `exec` runs; `tools.ensure` caches it versioned.
AUTH_PLUGIN = "gke-gcloud-auth-plugin"

#: The installer's address reaching the control plane once an infra pass has
#: put `installer_cidr` in its authorized networks (decision 2).
CONTROL_PLANE = WaitSpec(
    reason="the cluster's control plane admitting this machine's address",
    expected="about a minute",
    expected_s=90,
    bound_s=10 * 60,
    look_at="the cluster's authorized networks must hold installer_cidr (display name swarm-install); run "
            "swarm install again from the network it was recorded on",
)
#: kubectl's words for a control plane that does not answer this address yet.
UNREACHABLE = (
    "i/o timeout",
    "connection refused",
    "was refused",
    "Unable to connect to the server",
    "TLS handshake timeout",
    "no route to host",
)


def unreachable(code: int, err: str) -> bool:
    return code != 0 and any(words in err for words in UNREACHABLE)


def auth_plugin() -> Path:
    return Path(tools.ensure(AUTH_PLUGIN))


class ClusterGcp(Gcp):
    """`preflight.Gcp` plus the two reads stage 10 makes."""

    def run_service(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{RUN_API}/projects/{project}/locations/{region}/services/{name}", missing_ok=True)

    def subnetwork(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        return self._call("GET", f"{COMPUTE}/projects/{project}/regions/{region}/subnetworks/{name}", missing_ok=True)


@dataclass
class _Lazy:
    """One `ClusterGcp` for every stage 10 step, built on first use (never at import)."""

    made: list[ClusterGcp] = field(default_factory=list)

    def __call__(self) -> ClusterGcp:
        if not self.made:
            self.made.append(ClusterGcp())
        return self.made[0]


CLUSTER_GCP = _Lazy()

#: `run(argv, stdin, env) -> (exit code, stdout, stderr)`.
Run = Callable[[Sequence[str], str | None, dict[str, str] | None], tuple[int, str, str]]


def run_command(argv: Sequence[str], stdin: str | None = None,
                env: dict[str, str] | None = None) -> tuple[int, str, str]:
    done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=600,
                          check=False, env=env)
    return done.returncode, done.stdout or "", done.stderr or ""


def account_email(project: str, account: str) -> str:
    return f"{account}@{project}.iam.gserviceaccount.com"


def cluster_name(ctx: Any) -> str:
    return str(ctx.outputs.get("gke_cluster_name") or DEFAULT_CLUSTER)


def context_name(project: str, region: str, cluster: str) -> str:
    """The name `gcloud container clusters get-credentials` gives the context."""
    return f"gke_{project}_{region}_{cluster}"


def kubeconfig_path(project: str) -> Path:
    return Path("~/.config/swarmcloud/install").expanduser() / f"{project}.kubeconfig"


class WrongCluster(RuntimeError):
    """A kube context that is not the install cluster's; nothing ran."""


@dataclass
class Kube:
    """kubectl, pinned to the install cluster's context in the engine's own
    kubeconfig. Every call carries `--kubeconfig` and `--context`."""

    project: str
    region: str
    cluster: str
    deny_list: Sequence[str] = ()
    kubeconfig: Path | None = None
    find: Callable[[Any], str | None] = find_tool
    run: Run = run_command
    gcloud: Callable[[], str | None] = lambda: find_tool(next(t for t in TOOLS if t.name == "gcloud"))
    plugin: Callable[[], Path] = auth_plugin
    clock: Clock = field(default_factory=SystemClock)
    events: Events | None = None
    #: Run once before the first kubectl call: reopens the control plane after the close.
    reach: Callable[[], Any] | None = None
    _credentials: bool = field(default=False, repr=False)
    _reached: bool = field(default=False, repr=False)
    _plugin_dir: Path | None = field(default=None, repr=False)

    @property
    def context(self) -> str:
        return context_name(self.project, self.region, self.cluster)

    @property
    def config(self) -> Path:
        return self.kubeconfig or kubeconfig_path(self.project)

    def plugin_dir(self) -> Path:
        """A directory holding `gke-gcloud-auth-plugin` under that exact name."""
        if self._plugin_dir is None:
            binary = self.plugin()
            if binary.name != AUTH_PLUGIN:
                binary = tools.link(AUTH_PLUGIN, binary, directory=self.config.with_suffix(".bin"))
            self._plugin_dir = binary.parent
        return self._plugin_dir

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
        env["KUBECONFIG"] = str(self.config)
        first = str(self.plugin_dir())
        rest = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != first]
        env["PATH"] = os.pathsep.join([first, *rest])
        return env

    def kubectl(self) -> str:
        path = self.find(KUBECTL)
        if not path:
            raise FileNotFoundError(f"kubectl is not installed: {KUBECTL.how}")
        return path

    def guard(self) -> None:
        """Refuse any cluster but the install's, and any deny-listed one."""
        for name in (self.cluster, self.context):
            if name in self.deny_list or name.rsplit("_", 1)[-1] in self.deny_list:
                raise WrongCluster(f"{name} is on the shared deny-list; the engine never points kubectl at it")

    def credentials(self) -> None:
        """Fetch the install cluster's credentials into the engine's own kubeconfig."""
        self.guard()
        if self._credentials:
            return
        gcloud = self.gcloud()
        if not gcloud:
            raise FileNotFoundError("gcloud is not installed (preflight.tools names it)")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        code, _, err = self.run([gcloud, "container", "clusters", "get-credentials", self.cluster,
                                 f"--region={self.region}", f"--project={self.project}"], None, self._env())
        if code != 0:
            raise RuntimeError(f"get-credentials for {self.cluster} failed: {err.strip()[:300]}")
        code, out, _ = self.run([self.kubectl(), f"--kubeconfig={self.config}", "config", "current-context"],
                                None, self._env())
        if code != 0 or out.strip() != self.context:
            raise WrongCluster(f"the engine's kubeconfig points at {out.strip() or 'nothing'}, not {self.context}")
        self._credentials = True

    def __call__(self, *args: str, stdin: str | None = None) -> tuple[int, str, str]:
        """kubectl `args`; a control plane that does not answer yet is polled
        with a countdown (`CONTROL_PLANE`), any other answer returned as is."""
        self.credentials()
        if self.reach is not None and not self._reached:
            self.reach()
            self._reached = True
        argv = [self.kubectl(), f"--kubeconfig={self.config}", f"--context={self.context}", *args]
        answer: list[tuple[int, str, str]] = []

        def reached() -> str:
            answer[:] = [self.run(argv, stdin, self._env())]
            return UNMET if unreachable(answer[0][0], answer[0][2]) else MET

        events = self.events if self.events is not None else Events(self.clock.now)
        wait_until("cluster.control-plane", CONTROL_PLANE, reached, clock=self.clock, events=events)
        return answer[0]


def reopen_for(ctx: Any) -> Callable[[], Any] | None:
    """`infra.REACH.reopen` on `ctx`, when `ctx` can apply a pass (it has an `engine`)."""
    if getattr(ctx, "engine", None) is None:
        return None
    from .infra import REACH

    return lambda: REACH.reopen(ctx)


def kube_for(ctx: Any, *, find: Callable[[Any], str | None] = find_tool, run: Run = run_command,
             reach: Callable[[Any], Callable[[], Any] | None] = reopen_for) -> Kube:
    region = ctx.state.region
    if not region:
        raise LookupError("no region is recorded (preflight.region confirms it)")
    return Kube(project=ctx.project, region=str(region), cluster=cluster_name(ctx),
                deny_list=tuple(ctx.state.deny_list), find=find, run=run,
                # A verb's context may hold neither; it waits on the system clock.
                clock=getattr(ctx, "clock", None) or SystemClock(), events=getattr(ctx, "events", None),
                reach=reach(ctx))


# -- the release's kubernetes bundle -----------------------------------------


def unpack(data: bytes, into: Path) -> Path:
    """Unpack `kubernetes.tgz` into `into`, refusing a member that escapes it."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"{BUNDLE} member {member.name} escapes the bundle")
            if member.issym() or member.islnk():
                raise ValueError(f"{BUNDLE} member {member.name} is a link")
        tar.extractall(root, filter="data")
    missing = [m for m in BUNDLE_MUST_HOLD if not (root / m.rstrip("/")).exists()]
    if missing:
        raise ValueError(f"{BUNDLE} does not hold {', '.join(missing)}")
    return root


def bundle_dir(project: str, version: str) -> Path:
    return Path("~/.config/swarmcloud/install").expanduser() / project / "release" / version / "kubernetes"


@dataclass
class Bundle:
    """The release's Kubernetes files, from the release home, through
    `Release.fetch`: SHA256SUMS cosign-verified, then the bundle's digest."""

    gcp: Callable[[], Gcp]
    where: Callable[[str, str], Path] = bundle_dir
    release: Callable[[Callable[[str], bytes]], Release] = field(
        default=lambda read_public: Release(read_public=read_public))

    def read(self, ctx: Any) -> tuple[bytes, str]:
        """The bundle's bytes and digest, checked against the cosign-verified
        SHA256SUMS; cosign reads the sums and their signature from a temporary
        directory, removed after, so nothing stays written."""
        version = ctx.state.release
        if not version:
            raise LookupError("no release is recorded (preflight.release reads it)")
        with tempfile.TemporaryDirectory(prefix="swarm-bundle-") as where:
            data = self.release(self.gcp().read_public).fetch(str(version), BUNDLE, Path(where))
        return data, hashlib.sha256(data).hexdigest()

    def fetch(self, ctx: Any) -> Path:
        """The bundle unpacked into the engine's local cache (a render needs files)."""
        data, digest = self.read(ctx)
        target = self.where(ctx.project, str(ctx.state.release))
        if (target / ".sha256").is_file() and (target / ".sha256").read_text().strip() == digest:
            return target
        root = unpack(data, target)
        (root / ".sha256").write_text(digest + "\n")
        return root


def unique_ids(gcp: Gcp, project: str) -> dict[str, str]:
    """Each policy subject's numeric uniqueId, read from IAM (lesson 29)."""
    found: dict[str, str] = {}
    for role, account in POLICY_SUBJECTS:
        email = account_email(project, account)
        uid = str((gcp.service_account(email) or {}).get("uniqueId") or "")
        if not uid.isdigit():
            raise LookupError(f"{email} has no uniqueId in IAM; the RBAC is never rendered by email (lesson 29)")
        found[role] = uid
    return found


def documents(rendered: str) -> list[tuple[str, str]]:
    """(kind, name) of every document in a render, in order."""
    return [(str(d.get("kind")), str((d.get("metadata") or {}).get("name")))
            for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]


class _Base:
    stage = 10
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, gcp: Callable[[], ClusterGcp], ask: Ask, *,
                 kube: Callable[[Any], Kube] = kube_for,
                 bundle: Bundle | None = None,
                 run: Run = run_command) -> None:
        self._gcp = gcp
        self._ask = ask
        self._kube = kube
        self._bundle = bundle or Bundle(gcp)
        self._run = run
        self._kubes: dict[tuple[str, str | None, str], Kube] = {}

    @property
    def gcp(self) -> ClusterGcp:
        return self._gcp()

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return self._explain

    def kube(self, ctx: Any) -> Kube:
        """One `Kube` per install cluster, so its credentials are fetched once."""
        key = (ctx.project, ctx.state.region, cluster_name(ctx))
        if key not in self._kubes:
            self._kubes[key] = self._kube(ctx)
        return self._kubes[key]

    def render(self, ctx: Any, *args: str) -> str:
        """`render.py <args>` from the release bundle, never a checkout."""
        root = self._bundle.fetch(ctx)
        code, out, err = self._run([sys.executable, str(root / "render.py"), *args], None, None)
        if code != 0:
            raise RuntimeError(f"render.py {args[0]} failed: {err.strip()[:300]}")
        return out

    def differs(self, ctx: Any, rendered: str) -> str | None:
        """None when the cluster already holds `rendered`, else why not.
        `kubectl diff` exits 0 for no difference and 1 for a difference."""
        code, out, err = self.kube(ctx)("diff", "-f", "-", stdin=rendered)
        if code == 0:
            return None
        if code == 1:
            changed = sorted({line.split()[1] for line in out.splitlines() if line.startswith("diff ")})
            return "the cluster differs from the render" + (f" ({len(changed)} objects)" if changed else "")
        raise RuntimeError(f"kubectl diff failed: {err.strip()[:300]}")

    def apply_render(self, ctx: Any, rendered: str) -> None:
        code, _, err = self.kube(ctx)("apply", "--server-side", "--force-conflicts",
                                       "--field-manager=swarm-install", "-f", "-", stdin=rendered)
        if code != 0:
            raise RuntimeError(f"kubectl apply failed: {err.strip()[:300]}")


class BundleStep(_Base):
    id = "cluster.bundle"
    satisfies = ("other.release-kubernetes-bundle",)
    _explain = (
        "Stage 10, team add, upgrade and uninstall render the release's own Kubernetes files, checked "
        "against the release's SHA256SUMS, itself verified with cosign against the pinned release key: a "
        "checkout's files can be any version."
    )

    def check(self, ctx: Any) -> Observation:
        if ctx.dry_run:
            # A dry run checks the digest and writes nothing, not even the cache.
            self._bundle.read(ctx)
            return Observation(MET, f"{BUNDLE} of release {ctx.state.release} matches SHA256SUMS")
        root = self._bundle.fetch(ctx)
        return Observation(MET, f"{BUNDLE} of release {ctx.state.release}", {"kubernetes_bundle": str(root)})

    def plan(self, ctx: Any) -> Plan:
        return Plan(f"fetch {BUNDLE} of release {ctx.state.release} and check it against its signed SHA256SUMS")

    def apply(self, ctx: Any, plan: Plan) -> None:
        self._bundle.fetch(ctx)


class PoliciesStep(_Base):
    id = "cluster.policies"
    satisfies = (
        "cluster-policy.cache-storage-class",
        "cluster-policy.swarm-worker-job-constraints",
        "cluster-policy.swarm-worker-pod-constraints",
        "cluster-policy.swarm-worker-pod-constraints-binding",
        "cluster-policy.swarm-worker-pod-hardening",
        "cluster-policy.swarm-workspace-deployer-namespaces",
        "cluster-policy.swarm-workspace-deployer-rolebindings",
        "cluster-policy.swarm-workspace-deployer-roles",
        "cluster-policy.swarm-workspace-deployer-scope",
        "rbac.provisioner-clusterrole",
    )
    _explain = (
        "The admission policies that bound every worker pod and the workspace deployer's reach, and the "
        "deployer's ClusterRole, applied before anything a tenant runs (lesson 33). Their subjects are "
        "rendered with the uniqueIds IAM assigned (lesson 29)."
    )

    def rendered(self, ctx: Any) -> str:
        """No `--terraform-tfvars`: a Day One install has no Terraform tenants
        (the release bundle carries no terraform/ tree), so the workspace
        deployer's scope policy names no reserved tenant namespace; every team
        is a workspace tenant, made by swarm-workspace-apply."""
        uids = unique_ids(self.gcp, ctx.project)
        return self.render(ctx, "policies", f"--project={ctx.project}",
                           f"--scheduler-uid={uids['scheduler']}", f"--reconciler-uid={uids['reconciler']}",
                           f"--workspace-deployer-uid={uids['workspace-deployer']}")

    def check(self, ctx: Any) -> Observation:
        problem = self.differs(ctx, self.rendered(ctx))
        return Observation(UNMET, problem) if problem else Observation(MET, "the policies match the release")

    def plan(self, ctx: Any) -> Plan:
        return Plan("apply the cluster's admission policies and the workspace deployer's RBAC",
                    tuple(f"{k}/{n}" for k, n in documents(self.rendered(ctx))))

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.apply_render(ctx, self.rendered(ctx))


class SwarmSystemStep(_Base):
    id = "cluster.swarm-system"
    satisfies = ("cluster.swarm-system-namespace",)
    _explain = (
        "swarm-system holds the platform's in-cluster services; its default-deny ingress admits only the "
        "live relay's port from tenant pods and swarm-api's egress range. Terraform creates the namespace; "
        "this applies its policies, after the admission policies (lesson 33)."
    )

    def __init__(self, gcp: Callable[[], ClusterGcp], ask: Ask, *, policies: PoliciesStep, **kw: Any) -> None:
        super().__init__(gcp, ask, **kw)
        self._policies = policies

    def egress_cidr(self, ctx: Any) -> str:
        """swarm-api's Direct VPC egress range, read as kubernetes/apply.sh
        --swarm-system reads it: the service's subnetwork, then its range."""
        cidr = ctx.outputs.get("swarm_api_egress_cidr")
        if cidr:
            return str(cidr)
        region = str(ctx.state.region)
        service = self.gcp.run_service(ctx.project, region, "swarm-api") or {}
        interfaces = (service.get("template", {}).get("vpcAccess", {}) or {}).get("networkInterfaces") or []
        subnet = str((interfaces[0] if interfaces else {}).get("subnetwork") or "")
        if not subnet:
            raise LookupError("swarm-api has no Direct VPC egress subnetwork (infra pass 1 sets it)")
        name = subnet.rsplit("/", 1)[-1]
        found = self.gcp.subnetwork(ctx.project, region, name)
        cidr = str((found or {}).get("ipCidrRange") or "")
        if not cidr:
            raise LookupError(f"subnetwork {name} has no ipCidrRange")
        return cidr

    def rendered(self, ctx: Any) -> str:
        cidr = self.egress_cidr(ctx)
        return self.render(ctx, "swarm-system", f"--swarm-api-egress-cidr={cidr}",
                           f"--swarm-api-egress-source=install-state/{ctx.project}/swarm_api_egress_cidr")

    def check(self, ctx: Any) -> Observation:
        code, _, _ = self.kube(ctx)("get", "namespace", "swarm-system", "-o", "name")
        if code != 0:
            return Observation(UNMET, "namespace swarm-system does not exist (infra pass 1 creates it)")
        problem = self.differs(ctx, self.rendered(ctx))
        return Observation(UNMET, problem) if problem else Observation(MET, "swarm-system's ingress matches")

    def plan(self, ctx: Any) -> Plan:
        return Plan("apply swarm-system's ingress policies",
                    tuple(f"{k}/{n}" for k, n in documents(self.rendered(ctx))))

    def apply(self, ctx: Any, plan: Plan) -> None:
        problem = self._policies.differs(ctx, self._policies.rendered(ctx))
        if problem:
            raise RuntimeError(f"the admission policies go first (lesson 33): {problem}")
        code, _, _ = self.kube(ctx)("get", "namespace", "swarm-system", "-o", "name")
        if code != 0:
            raise RuntimeError("namespace swarm-system does not exist; infra pass 1 creates it")
        self.apply_render(ctx, self.rendered(ctx))


#: swarm-system's core workloads: (manifest entry, kind/name, Terraform output).
CORE_WORKLOADS = (
    ("workload.swarm-live", "statefulset/swarm-live", "swarm_live_manifest"),
    ("workload.swarm-graph", "deployment/swarm-graph", "swarm_graph_manifest"),
)
#: How long `kubectl rollout status` waits for one workload, inside
#: `run_command`'s own 600 s.
ROLLOUT_TIMEOUT = "300s"

#: `outputs(ctx) -> {name: value}`: terraform/infra's outputs.
Outputs = Callable[[Any], Mapping[str, Any]]


def infra_outputs(ctx: Any, *, deps: Any = None) -> dict[str, Any]:
    """The core workloads' manifests from terraform/infra's state: the
    release's infra root, initialised against the install's state bucket and
    read as the deployer, as every infra pass runs (`steps.bootstrap`)."""
    from .bootstrap import DEPS

    deps = deps or DEPS
    tf = Terraform(root="infra", root_dir=deps.module_root(ctx, "infra"), state_bucket=state_bucket(ctx.project),
                   environment=ctx.environment, exec_=deps.exec_)
    return read_outputs(tf, tuple(output for _, _, output in CORE_WORKLOADS),
                        impersonate=deployer_email(ctx.project))


class WorkloadsStep(_Base):
    id = "cluster.workloads"
    satisfies = tuple(entry for entry, _, _ in CORE_WORKLOADS)
    _explain = (
        "The live-browser relay and the shared code graph service run in swarm-system. Their manifests are "
        "terraform/infra's renders (image digest, bucket, key reference, address), read from its outputs and "
        "applied after the namespace's policies; stage 10 waits for their rollouts and checks every replica "
        "is ready. A workload Terraform has switched off renders null and is skipped."
    )

    def __init__(self, gcp: Callable[[], ClusterGcp], ask: Ask, *, outputs: Outputs = infra_outputs,
                 **kw: Any) -> None:
        super().__init__(gcp, ask, **kw)
        self._outputs = outputs
        self._read: dict[str, Mapping[str, Any]] = {}

    def manifests(self, ctx: Any) -> list[tuple[str, str, str | None]]:
        """(kind/name, output, manifest or None when switched off), read once per project."""
        if ctx.project not in self._read:
            self._read[ctx.project] = self._outputs(ctx)
        found = self._read[ctx.project]
        return [(name, output, str(found[output]) if found.get(output) else None)
                for _, name, output in CORE_WORKLOADS]

    def check(self, ctx: Any) -> Observation:
        problems: list[str] = []
        off: list[str] = []
        for name, output, manifest in self.manifests(ctx):
            if manifest is None:
                off.append(f"{name} (terraform output {output} is null: switched off)")
                continue
            code, out, err = self.kube(ctx)("get", name, "--namespace=swarm-system", "-o", "json")
            if code != 0:
                if "NotFound" not in err:
                    raise RuntimeError(f"kubectl get {name} failed: {err.strip()[:300]}")
                problems.append(f"{name} does not exist (terraform output {output})")
                continue
            problem = self.differs(ctx, manifest)
            if problem:
                problems.append(f"{name}: {problem} (terraform output {output})")
                continue
            found = yaml.safe_load(out) or {}
            wanted = int((found.get("spec") or {}).get("replicas") or 0)
            ready = int((found.get("status") or {}).get("readyReplicas") or 0)
            if wanted < 1 or ready < wanted:
                problems.append(f"{name} has {ready} of {wanted} replicas ready")
        skipped = f"; not applied: {', '.join(off)}" if off else ""
        if problems:
            return Observation(UNMET, "; ".join(problems) + skipped)
        running = [name for name, _, manifest in self.manifests(ctx) if manifest is not None]
        if not running:
            return Observation(MET, "no core workload is switched on" + skipped)
        return Observation(MET, f"{' and '.join(running)} ready in swarm-system{skipped}")

    def plan(self, ctx: Any) -> Plan:
        return Plan("apply swarm-system's core workloads from terraform/infra's outputs and wait for their rollouts",
                    tuple(f"{name}: terraform output {output}" + ("" if manifest else " (null: switched off)")
                          for name, output, manifest in self.manifests(ctx)))

    def apply(self, ctx: Any, plan: Plan) -> None:
        wanted = [(name, manifest) for name, _, manifest in self.manifests(ctx) if manifest is not None]
        for _, manifest in wanted:
            self.apply_render(ctx, manifest)
        for name, _ in wanted:
            code, _, err = self.kube(ctx)("rollout", "status", name, "--namespace=swarm-system",
                                          f"--timeout={ROLLOUT_TIMEOUT}")
            if code != 0:
                raise RuntimeError(f"{name} did not roll out in swarm-system: {err.strip()[:300]}; "
                                   f"kubectl -n swarm-system describe {name} says why")


def build_steps(gcp: Callable[[], ClusterGcp] = CLUSTER_GCP, ask: Ask = ASK, *,
                kube: Callable[[Any], Kube] = kube_for, bundle: Bundle | None = None,
                run: Run = run_command, outputs: Outputs = infra_outputs) -> list[Any]:
    """Stage 10 in order: the bundle, the policies, then what they admit."""
    shared = bundle or Bundle(gcp)
    policies = PoliciesStep(gcp, ask, kube=kube, bundle=shared, run=run)
    return [
        BundleStep(gcp, ask, kube=kube, bundle=shared, run=run),
        policies,
        SwarmSystemStep(gcp, ask, policies=policies, kube=kube, bundle=shared, run=run),
        WorkloadsStep(gcp, ask, outputs=outputs, kube=kube, bundle=shared, run=run),
    ]


STEPS = build_steps()
