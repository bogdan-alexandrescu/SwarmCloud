"""Stage 3, bootstrap pass 1, and what every Terraform pass of the install
shares (design §4, §5; plan R7, R8; lessons 7-12, 22, 23).

| Step | Pass | Manifest entries |
|---|---|---|
| `bootstrap.pass-1` | `bootstrap-1`, as the person | `other.engine-terraform-rules` |
| `bootstrap.pass-2` | `bootstrap-2`, as the deployer (run in stage 7, from `infra.py`) | the IAP programmatic clients, the log view grant |

**The release's module.** `releases/<V>/module.tgz` is fetched from the
release home, its SHA-256 compared with `SHA256SUMS`, and `SHA256SUMS` itself
verified with cosign against the release key pinned in this wheel
(`release_key.pub`), before anything in it runs. It is unpacked into
`~/.cache/swarmcloud/install/<project>/<release>/`; no repository checkout is
needed (D12) and no committed tfvars is in it (design §5.1).

**A pass is met** when the inputs it would generate now are the inputs it last
applied (their digest is recorded in the state), so a changed input (a new
digest, a key that now has a version) is drift the engine repairs by applying
again, and a resume never re-applies an unchanged pass.

**Adoption before bootstrap pass 1** (`adopt.py`): a step-spec key ring or key
that exists and is not in the state is imported (they can never be deleted, so
a create would fail on the name); a custom role deleted in the last 7 days is
undeleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import adopt, tfvars, tools
from ..adopt import InstallGcp
from ..api_client import deployer_email
from ..exitcodes import MET, UNMET
from ..state import state_bucket
from ..terraform import Exec, Guards, Terraform, TerraformRefused, run_pass, subprocess_exec
from . import Observation, Plan
from .preflight import RELEASE_HOME, parse_sums

#: The cosign public key of `swarmcloud-release/cosign`, printed by BGD's run
#: of `scripts/release-home.sh` (its last step) and committed beside this file.
RELEASE_KEY = Path(__file__).resolve().parent.parent / "release_key.pub"

#: The platform accounts the deployer must be able to act as (lesson 23).
PLATFORM_ACCOUNTS = ("swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler", "swarm-verify")


class ReleaseKeyMissing(FileNotFoundError):
    pass


def release_key(path: Path | None = None) -> Path:
    """The pinned release key, or a refusal that says how it gets there."""
    key = path or RELEASE_KEY
    if not key.is_file() or b"BEGIN PUBLIC KEY" not in key.read_bytes():
        raise ReleaseKeyMissing(
            f"{key} is missing: it is the cosign public key of swarmcloud-release/cosign, printed at the end of "
            "BGD's run of scripts/release-home.sh (RELHOME-1). Without it no release signature can be verified, "
            "so nothing is copied or applied"
        )
    return key


def cosign_verify_blob(cosign: Path, key: Path, blob: Path, signature: Path, exec_: Exec) -> None:
    done = exec_([str(cosign), "verify-blob", "--key", str(key), "--signature", str(signature),
                  "--insecure-ignore-tlog=true", str(blob)], {}, blob.parent)
    if done.returncode != 0:
        raise TerraformRefused(f"the release's {blob.name} does not verify with the pinned release key: "
                               f"{done.stderr.strip()[-500:]}")


def workdir(project: str, release: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())
    return Path(base) / "swarmcloud" / "install" / project / release


@dataclass
class Release:
    """Fetches and verifies the release's module once per run."""

    read_public: Callable[[str], bytes]
    exec_: Exec = subprocess_exec
    cosign: Callable[[], Path] = field(default=lambda: tools.ensure("cosign"))
    key: Callable[[], Path] = release_key
    _dirs: dict[tuple[str, str], Path] = field(default_factory=dict)

    def sums(self, version: str, where: Path) -> dict[str, str]:
        """SHA256SUMS of the release, verified with the pinned key."""
        base = f"{RELEASE_HOME}/releases/{version}"
        where.mkdir(parents=True, exist_ok=True)
        (where / "SHA256SUMS").write_bytes(self.read_public(f"{base}/SHA256SUMS"))
        (where / "SHA256SUMS.sig").write_bytes(self.read_public(f"{base}/SHA256SUMS.sig"))
        cosign_verify_blob(self.cosign(), self.key(), where / "SHA256SUMS", where / "SHA256SUMS.sig", self.exec_)
        return parse_sums((where / "SHA256SUMS").read_bytes())

    def fetch(self, version: str, name: str, where: Path) -> bytes:
        sums = self.sums(version, where)
        data = self.read_public(f"{RELEASE_HOME}/releases/{version}/{name}")
        if sums.get(name) != hashlib.sha256(data).hexdigest():
            raise TerraformRefused(f"release {version}: {name} does not match its signed SHA256SUMS")
        return data

    def module_dir(self, project: str, version: str) -> Path:
        if (project, version) in self._dirs:
            return self._dirs[(project, version)]
        where = workdir(project, version)
        target = where / "module"
        if not (target / "terraform" / "infra").is_dir():
            data = self.fetch(version, "module.tgz", where)
            (where / "module.tgz").write_bytes(data)
            target.mkdir(parents=True, exist_ok=True)
            with tarfile.open(where / "module.tgz", "r:gz") as archive:
                archive.extractall(target, filter="data")
        self._dirs[(project, version)] = target
        return target


def _impersonated(email: str) -> InstallGcp:
    import google.auth
    from google.auth import impersonated_credentials
    from google.auth.transport.requests import AuthorizedSession

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    target = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=email,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"], lifetime=3600,
    )
    return InstallGcp(AuthorizedSession(target))


def account(name: str, project: str) -> str:
    return f"{name}@{project}.iam.gserviceaccount.com"


def guards_for(gcp: Any, as_deployer: Callable[[], Any], project: str) -> Guards:
    deployer = deployer_email(project)

    def act_as_missing() -> list[str]:
        missing = []
        for name in PLATFORM_ACCOUNTS:
            held = as_deployer().sa_permissions(account(name, project), ["iam.serviceAccounts.actAs"])
            if held is not None and "iam.serviceAccounts.actAs" not in held:
                missing.append(account(name, project))
        return missing

    return Guards(
        deployer_exists=lambda: gcp.service_account(deployer) is not None,
        can_impersonate=lambda: "iam.serviceAccounts.getAccessToken" in (
            gcp.sa_permissions(deployer, ["iam.serviceAccounts.getAccessToken"]) or set()),
        act_as_missing=act_as_missing,
        lint=lambda expression: gcp.lint_condition(f"//cloudresourcemanager.googleapis.com/projects/{project}",
                                                   expression),
    )


def key_versions(gcp: Any, project: str, names: Sequence[str]) -> dict[str, bool]:
    """secret -> it has an ENABLED version (metadata only; no value is read)."""
    found = {}
    for name in names:
        found[name] = gcp.secret(project, name) is not None and gcp.enabled_version(project, name) is not None
    return found


def inputs_digest(inputs: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(inputs), sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Deps:
    """What a pass step needs from the world, each replaceable in tests."""

    gcp: Callable[[], Any]
    as_deployer: Callable[[str], Any] = _impersonated
    exec_: Exec = subprocess_exec
    release: Release | None = None
    confirm: Callable[[str], str] = field(default=lambda prompt: _confirm_typed(prompt))
    root_dir: Callable[[Any, str], Path] | None = None

    def module_root(self, ctx: Any, root: str) -> Path:
        if self.root_dir is not None:
            return self.root_dir(ctx, root)
        release = self.release or Release(read_public=self.gcp().read_public, exec_=self.exec_)
        self.release = release
        if not ctx.state.release:
            raise LookupError("no release is recorded (preflight.release)")
        return release.module_dir(ctx.project, str(ctx.state.release)) / "terraform" / root


def _confirm_typed(prompt: str) -> str:
    """A typed answer at the terminal; SWARM_ASSUME_YES never answers it."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline()
    except OSError:
        return ""


def all_versions(gcp: Any, project: str) -> dict[str, bool]:
    return key_versions(gcp, project, (*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS))


def generate(ctx: Any, pass_name: str, gcp: Any) -> dict[str, Any]:
    installer = str(ctx.outputs.get("installer") or ctx.holder)
    if tfvars.ROOT_OF[pass_name] == "bootstrap":
        return tfvars.bootstrap_inputs(ctx.state, pass_name=pass_name, installer=installer)
    versions = all_versions(gcp, ctx.project) if tfvars.after(pass_name, "infra-2") else {}
    return tfvars.infra_inputs(ctx.state, pass_name=pass_name, installer=installer, versions=versions)


def applied_key(pass_name: str) -> str:
    return f"tf_applied_{pass_name.replace('-', '_')}"


def apply_pass(ctx: Any, deps: Deps, pass_name: str, *, targets: Sequence[str] = (),
               before_plan: Callable[[Terraform], None] | None = None) -> dict[str, Any]:
    """Generate, guard, plan, review and apply one pass; record what it applied."""
    from .preflight import remember

    gcp = deps.gcp()
    root = tfvars.ROOT_OF[pass_name]
    inputs = generate(ctx, pass_name, gcp)
    tf = Terraform(root=root, root_dir=deps.module_root(ctx, root), state_bucket=state_bucket(ctx.project),
                   environment=ctx.environment, exec_=deps.exec_)
    bucket = ctx.engine.store.bucket
    if bucket is None:
        raise LookupError("the state bucket is not attached (foundations.state-bucket)")
    deployer = deployer_email(ctx.project)
    guards = guards_for(gcp, lambda: deps.as_deployer(deployer), ctx.project)
    if before_plan is not None:
        original_plan = tf.plan

        def plan(**kwargs: Any) -> dict[str, Any]:
            before_plan(tf)
            return original_plan(**kwargs)

        tf.plan = plan  # type: ignore[method-assign]
    outputs = run_pass(tf, pass_name, inputs, project=ctx.project, bucket=bucket, guards=guards,
                       confirm=deps.confirm, targets=targets,
                       announce=lambda line: ctx.events.emit("step_apply", step=pass_name, message=line))
    recorded: dict[str, Any] = {applied_key(pass_name): inputs_digest(inputs)}
    audiences = outputs.get("frontend_iap_audiences") or []
    if audiences:
        recorded["api_iap_audience"] = str(audiences[0])
    if outputs.get("quota_broker_url"):
        recorded["quota_broker_url"] = str(outputs["quota_broker_url"])
    remember(ctx, outputs=recorded)
    return outputs


class PassStep:
    """One Terraform pass as an engine step."""

    kind = "engine"
    pass_name = ""
    targets: tuple[str, ...] = ()
    _explain = ""

    def __init__(self, deps: Deps, *, id: str, stage: int, satisfies: tuple[str, ...]) -> None:
        self.deps = deps
        self.id = id
        self.stage = stage
        self.satisfies = satisfies

    def explain(self) -> str:
        return self._explain

    def _pending(self, ctx: Any) -> str | None:
        """Why this pass is not applied as it would be now, or None."""
        recorded = ctx.outputs.get(applied_key(self.pass_name))
        if not recorded:
            return f"{self.pass_name} has not been applied"
        current = inputs_digest(generate(ctx, self.pass_name, self.deps.gcp()))
        if recorded != current:
            return f"{self.pass_name}'s inputs changed since it was applied"
        return None

    def check(self, ctx: Any) -> Observation:
        why = self._pending(ctx)
        return Observation(UNMET, why) if why else Observation(MET, f"{self.pass_name} applied")

    def verify(self, ctx: Any) -> Observation:
        return self.check(ctx)

    def plan(self, ctx: Any) -> Plan:
        root = tfvars.ROOT_OF[self.pass_name]
        who = "you" if self.pass_name == "bootstrap-1" else deployer_email(ctx.project)
        actions = [f"write the generated inputs to gs://{state_bucket(ctx.project)}/{tfvars.object_name(root)}",
                   f"terraform init, plan and apply {root} as {who}"]
        if self.targets:
            actions.append("targets: " + ", ".join(self.targets))
        return Plan(f"Terraform {self.pass_name}", tuple(actions))

    def apply(self, ctx: Any, plan: Plan) -> None:
        apply_pass(ctx, self.deps, self.pass_name, targets=self.targets)


class BootstrapPass1Step(PassStep):
    pass_name = "bootstrap-1"
    _explain = (
        "Bootstrap makes the custom roles, the deployer and the spec-signing key infra needs (lesson 8). It is "
        "the one apply that runs as you; every later one runs as swarm-tf-deployer (lessons 22, 23). Its "
        "state is in the state bucket from the first run, and an empty state is refused while the deployer "
        "exists (lesson 7)."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="bootstrap.pass-1", stage=3,
                         satisfies=("other.engine-terraform-rules",))

    def check(self, ctx: Any) -> Observation:
        if self.deps.gcp().service_account(deployer_email(ctx.project)) is None:
            return Observation(UNMET, f"{deployer_email(ctx.project)} does not exist yet")
        return super().check(ctx)

    def apply(self, ctx: Any, plan: Plan) -> None:
        gcp = self.deps.gcp()

        def adopt_first(tf: Terraform) -> None:
            listed = tf.state_list()
            for address, resource_id in adopt.kms_imports(gcp, ctx.project, str(ctx.state.region),
                                                          ctx.environment, listed):
                ctx.events.emit("step_apply", step=self.id, message=f"import {address} (it can never be deleted)")
                tf.run("import", "-input=false", address, resource_id)
            roles = adopt.undelete_roles(gcp, ctx.project, _swarm_roles(gcp, ctx.project))
            for role in roles.undeleted:
                ctx.events.emit("step_apply", step=self.id, message=f"undeleted custom role {role}")
            if roles.blocked:
                raise TerraformRefused(
                    f"custom roles {', '.join(roles.blocked)} were deleted more than 7 days ago; their ids are "
                    "held for 37 days and cannot be undeleted or reused. Wait, or install into a new project"
                )

        apply_pass(ctx, self.deps, self.pass_name, before_plan=adopt_first)


def _swarm_roles(gcp: Any, project: str) -> list[str]:
    """Every custom role id of the platform's (`swarm*`) the project holds, deleted ones included."""
    return [str(r.get("name", "")).rsplit("/", 1)[-1] for r in gcp.custom_roles(project)
            if str(r.get("name", "")).rsplit("/", 1)[-1].startswith("swarm")]


class BootstrapPass2Step(PassStep):
    pass_name = "bootstrap-2"
    _explain = (
        "The second bootstrap pass grants what needs infra's accounts and log view, and the IAP programmatic "
        "clients; it runs after infra pass 2, as the deployer."
    )

    def __init__(self, deps: Deps) -> None:
        super().__init__(deps, id="bootstrap.pass-2", stage=7, satisfies=())


def _lazy_gcp() -> Callable[[], InstallGcp]:
    made: list[InstallGcp] = []

    def get() -> InstallGcp:
        if not made:
            made.append(InstallGcp())
        return made[0]

    return get


DEPS = Deps(gcp=_lazy_gcp())


def build_steps(deps: Deps = DEPS) -> list[Any]:
    return [BootstrapPass1Step(deps)]


STEPS = build_steps()
