"""The engine's Terraform runner (DAYONE-ENGINE-3; design §5; manifest
`other.engine-terraform-rules`; lessons 7, 9, 10, 11, 12, 22, 23).

On fakes, with no credentials and no terraform binary: every command the
runner would run is recorded by a fake `exec_`, which answers like terraform.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_install import terraform as tfmod
from swarm_install import tfvars
from swarm_install.api_client import deployer_email
from swarm_install.events import Events
from swarm_install.state import InstallState
from swarm_install.steps import discover, triggered_of
from swarm_install.terraform import (
    IMPERSONATE_ENV,
    Completed,
    Guards,
    Terraform,
    TerraformRefused,
    run_pass,
)

from .test_state_store import FakeBucket, FakeClock

PROJECT = "p-dayone-test"


class FakeExec:
    """Answers like terraform; records (argv, env)."""

    def __init__(self, *, version: str = tfmod.TERRAFORM_VERSION, state: list[str] | None = None,
                 plan: dict | None = None, outputs: dict | None = None) -> None:
        self.version = version
        self.state = list(state or [])
        self.plan = plan or {"resource_changes": []}
        self.outputs = outputs or {}
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv, env, cwd) -> Completed:
        argv = list(argv)
        self.calls.append((argv, dict(env)))
        verb = argv[1]
        if verb == "version":
            return Completed(0, json.dumps({"terraform_version": self.version}))
        if verb == "state":
            return Completed(0, "\n".join(self.state))
        if verb == "show":
            return Completed(0, json.dumps(self.plan))
        if verb == "output":
            return Completed(0, json.dumps({k: {"value": v} for k, v in self.outputs.items()}))
        return Completed(0, "")

    def verbs(self) -> list[str]:
        return [argv[1] for argv, _ in self.calls if argv[0] == "terraform"]

    def env_of(self, verb: str) -> list[dict[str, str]]:
        return [env for argv, env in self.calls if argv[0] == "terraform" and argv[1] == verb]


def guards(*, deployer_exists: bool = True, can_impersonate: bool = True, missing: list[str] | None = None,
           lint=lambda expression: []) -> Guards:
    return Guards(deployer_exists=lambda: deployer_exists, can_impersonate=lambda: can_impersonate,
                  act_as_missing=lambda: list(missing or []), lint=lint)


#: An OAuth client id in the shape terraform/bootstrap validates (an id, never a secret).
DESKTOP_CLIENT_ID = "123456789012-" + "a1b2c3" + ".apps.googleusercontent.com"


def install_state(**outputs: Any) -> InstallState:
    # The stage 0 and stage 2 keys every generated input needs (DAYONE-ENGINE-INPUTS).
    base = {"alert_email": "alerts@example.com", "hostname": "swarm.example.com", "github_org": "example-org",
            "org_domain": "example.com", "installer_cidr": "203.0.113.7/32",
            "desktop_client_id": DESKTOP_CLIENT_ID}
    base.update(outputs)
    return InstallState(project=PROJECT, environment="dev", region="europe-west1", hostname="swarm.example.com",
                        release="2026.10.1", outputs=base)


def tf_for(tmp_path: Path, root: str, exec_: FakeExec) -> Terraform:
    root_dir = tmp_path / root
    root_dir.mkdir(parents=True, exist_ok=True)
    return Terraform(root=root, root_dir=root_dir, state_bucket=f"swarm-tfstate-{PROJECT}", exec_=exec_)


def run(tmp_path: Path, pass_name: str, exec_: FakeExec, *, bucket: FakeBucket | None = None,
        g: Guards | None = None, confirm=lambda prompt: "", inputs: dict | None = None) -> dict:
    root = tfvars.ROOT_OF[pass_name]
    tf = tf_for(tmp_path, root, exec_)
    return run_pass(tf, pass_name, inputs if inputs is not None else {"project_id": PROJECT}, project=PROJECT,
                    bucket=bucket or FakeBucket(), guards=g or guards(deployer_exists=False), confirm=confirm)


def change(address: str, rtype: str, actions: list[str], after: dict | None = None) -> dict:
    return {"address": address, "type": rtype, "change": {"actions": actions, "after": after}}


class FakeEngine:
    def __init__(self, ctx: Any, bucket: FakeBucket) -> None:
        self.ctx = ctx
        self.store = SimpleNamespace(bucket=bucket)

    def save(self, change) -> None:
        change(self.ctx.state)


class FakeCtx:
    """What a step sees, without the engine loop: the state, a bucket, events,
    a clock and the console documents."""

    def __init__(self, state: InstallState | None = None, *, console: dict | None = None,
                 clock: FakeClock | None = None) -> None:
        self.state = state or install_state()
        self.project = self.state.project
        self.environment = self.state.environment
        self.holder = "admin@example.com"
        self.clock = clock or FakeClock()
        self.events = Events(self.clock.now, json_lines=True, out=io.StringIO())
        self.dry_run = False
        self.bucket = FakeBucket()
        self.engine = FakeEngine(self, self.bucket)
        self.console = console if console is not None else {}

    @property
    def outputs(self) -> dict:
        return self.state.outputs

    def read_console(self, name: str):
        return self.console.get(name)


# -- the red-first tests ------------------------------------------------------------


def test_every_apply_after_pass1_impersonates_deployer(tmp_path: Path) -> None:
    for pass_name in tfvars.PASSES:
        exec_ = FakeExec()
        run(tmp_path / pass_name, pass_name, exec_)
        applies = exec_.env_of("apply")
        assert len(applies) == 1, pass_name
        if pass_name in ("bootstrap-1", "bootstrap-2"):
            # Pass 1 creates the deployer; pass 2 applies IAP membership, which the deployer cannot (N4).
            assert IMPERSONATE_ENV not in applies[0], f"{pass_name} runs as the person"
        else:
            assert applies[0][IMPERSONATE_ENV] == deployer_email(PROJECT), (
                f"{pass_name} must apply as swarm-tf-deployer (lessons 22, 23)"
            )
            assert all(env.get(IMPERSONATE_ENV) == deployer_email(PROJECT) for env in exec_.env_of("plan"))


def test_plan_with_destroy_refused_without_typed_approval(tmp_path: Path) -> None:
    plan = {"resource_changes": [change("google_storage_bucket.artifacts", "google_storage_bucket", ["delete"])]}
    for answer in ("", "yes", "y", "YES"):
        exec_ = FakeExec(plan=plan)
        with pytest.raises(TerraformRefused, match="not approved"):
            run(tmp_path, "infra-1", exec_, g=guards(), confirm=lambda prompt, a=answer: a)
        assert "apply" not in exec_.verbs()
    prompts: list[str] = []
    exec_ = FakeExec(plan=plan)
    run(tmp_path, "infra-1", exec_, g=guards(), confirm=lambda prompt: prompts.append(prompt) or PROJECT)
    assert "apply" in exec_.verbs()
    assert "google_storage_bucket.artifacts" in prompts[0] and PROJECT in prompts[0]


def test_iam_replace_without_create_before_destroy_refused(tmp_path: Path) -> None:
    destroy_first = {"resource_changes": [
        change("google_project_iam_member.ci", "google_project_iam_member", ["delete", "create"],
               {"role": "roles/resourcemanager.projectIamAdmin"})]}
    exec_ = FakeExec(plan=destroy_first)
    with pytest.raises(TerraformRefused, match="destroy-first"):
        run(tmp_path, "infra-1", exec_, g=guards(), confirm=lambda prompt: PROJECT)
    assert "apply" not in exec_.verbs(), "typing the project id never approves a destroy-first IAM swap"

    create_first = {"resource_changes": [
        change("google_project_iam_member.ci", "google_project_iam_member", ["create", "delete"],
               {"role": "roles/resourcemanager.projectIamAdmin"})]}
    exec_ = FakeExec(plan=create_first)
    run(tmp_path, "infra-1", exec_, g=guards(), confirm=lambda prompt: PROJECT)
    assert "apply" in exec_.verbs()


def test_conditions_linted_before_apply(tmp_path: Path) -> None:
    eleven = ", ".join(f"'roles/r{i}'" for i in range(11))
    too_many = {"resource_changes": [change(
        "google_project_iam_member.admin", "google_project_iam_member", ["create"],
        {"condition": [{"expression": f"api.getAttribute('iam.googleapis.com/modifiedGrantsByRole', [])"
                                      f".hasOnly([{eleven}])", "title": "t"}]})]}
    exec_ = FakeExec(plan=too_many)
    with pytest.raises(TerraformRefused, match="hasOnly"):
        run(tmp_path, "infra-1", exec_, g=guards())
    assert "apply" not in exec_.verbs()

    storage = {"resource_changes": [change(
        "google_storage_bucket_iam_member.r", "google_storage_bucket_iam_member", ["create"],
        {"condition": [{"expression": "resource.name.matches('x')", "title": "t"}]})]}
    with pytest.raises(TerraformRefused, match="matches"):
        run(tmp_path, "infra-1", FakeExec(plan=storage), g=guards())

    operators = " && ".join(["a == 'b'"] * 14)
    many = {"resource_changes": [change(
        "google_project_iam_member.m", "google_project_iam_member", ["create"],
        {"condition": [{"expression": operators, "title": "t"}]})]}
    with pytest.raises(TerraformRefused, match="logical operators"):
        run(tmp_path, "infra-1", FakeExec(plan=many), g=guards())

    linted: list[str] = []
    fine = {"resource_changes": [change(
        "google_project_iam_member.f", "google_project_iam_member", ["create"],
        {"condition": [{"expression": "request.time < timestamp('2030-01-01T00:00:00Z')", "title": "t"}]})]}
    exec_ = FakeExec(plan=fine)
    with pytest.raises(TerraformRefused, match="lintPolicy says no"):
        run(tmp_path, "infra-1", exec_, g=guards(lint=lambda e: linted.append(e) or ["lintPolicy says no"]))
    assert linted == ["request.time < timestamp('2030-01-01T00:00:00Z')"]
    assert "apply" not in exec_.verbs()


def test_terraform_version_checked(tmp_path: Path) -> None:
    exec_ = FakeExec(version="1.3.6")
    with pytest.raises(TerraformRefused, match="1.3.6 is not the pinned"):
        run(tmp_path, "bootstrap-1", exec_)
    assert exec_.verbs() == ["version"], "nothing runs on a terraform that is not the pinned one (lesson 12)"


def test_init_before_plan(tmp_path: Path) -> None:
    exec_ = FakeExec()
    tf = tf_for(tmp_path, "infra", exec_)
    with pytest.raises(TerraformRefused, match="init first"):
        tf.plan()
    run(tmp_path, "infra-1", exec_, g=guards())
    verbs = exec_.verbs()
    assert verbs.index("version") < verbs.index("init") < verbs.index("plan") < verbs.index("apply")
    init = next(argv for argv, _ in exec_.calls if argv[1] == "init")
    assert f"-backend-config=bucket=swarm-tfstate-{PROJECT}" in init
    assert "-backend-config=prefix=infra/dev" in init


def test_freshness_from_state_not_registry(tmp_path: Path) -> None:
    from swarm_install.steps.bootstrap import Deps
    from swarm_install.steps.images import RegistryStep

    class NoRegistry:
        def __getattr__(self, name):
            raise AssertionError(f"freshness read the registry ({name}); it is decided by Terraform state")

    for listed, expected in (([], "unmet"), (["data.google_project.this"], "unmet"),
                             (["module.artifact_registry.google_artifact_registry_repository.this"], "met")):
        exec_ = FakeExec(state=listed)
        deps = Deps(gcp=lambda: NoRegistry(), exec_=exec_, root_dir=lambda ctx, root: tmp_path)
        assert RegistryStep(deps).check(FakeCtx()).status == expected
        assert "state" in exec_.verbs()
    assert tfmod.is_fresh(_initialized(tmp_path, FakeExec(state=[]))) is True
    assert tfmod.is_fresh(_initialized(tmp_path, FakeExec(state=["google_x.y"]))) is False


def _initialized(tmp_path: Path, exec_: FakeExec) -> Terraform:
    tf = tf_for(tmp_path, "infra", exec_)
    tf.init()
    return tf


def test_empty_state_refused_when_deployer_exists(tmp_path: Path) -> None:
    exec_ = FakeExec(state=[])
    with pytest.raises(TerraformRefused, match="lesson 7"):
        run(tmp_path, "bootstrap-1", exec_, g=guards(deployer_exists=True))
    assert "plan" not in exec_.verbs() and "apply" not in exec_.verbs()

    exec_ = FakeExec(state=[])
    run(tmp_path, "bootstrap-1", exec_, g=guards(deployer_exists=False))
    assert "apply" in exec_.verbs(), "a first install (no deployer yet) starts from an empty state"

    exec_ = FakeExec(state=["google_service_account.deployer"])
    run(tmp_path, "bootstrap-1", exec_, g=guards(deployer_exists=True))
    assert "apply" in exec_.verbs()


def test_deployer_act_as_checked_before_apply(tmp_path: Path) -> None:
    exec_ = FakeExec()
    with pytest.raises(TerraformRefused, match="may not act as swarm-api@"):
        run(tmp_path, "infra-1", exec_, g=guards(missing=[f"swarm-api@{PROJECT}.iam.gserviceaccount.com"]))
    assert "plan" not in exec_.verbs() and "apply" not in exec_.verbs()

    exec_ = FakeExec()
    with pytest.raises(TerraformRefused, match="may not impersonate"):
        run(tmp_path, "infra-2", exec_, g=guards(can_impersonate=False))
    assert "apply" not in exec_.verbs()

    checked: list[str] = []
    g = Guards(deployer_exists=lambda: True, can_impersonate=lambda: checked.append("impersonate") or True,
               act_as_missing=lambda: checked.append("act_as") or [])
    run(tmp_path, "bootstrap-1", FakeExec(state=["x"]), g=g)
    assert checked == [], "bootstrap pass 1 runs as the person; there is no deployer to check yet"


def test_every_entry_an_install_step_satisfies_has_its_dependencies_owned() -> None:
    """The engine's dependency gate refuses a step whose entries depend on an
    entry no step satisfies and no step owns (`entry.step`); no step in the
    real registry, triggered steps included, may trip it against the real
    merged manifest (audit finding 1: it checked only this lane's steps)."""
    from swarm_install.manifest import load

    by_id = load().by_id()
    steps, pending = [], list(discover())
    while pending:
        step = pending.pop(0)
        steps.append(step)
        pending.extend(triggered_of(step))
    owner = {}
    for step in steps:
        for entry in step.satisfies:
            owner.setdefault(entry, step)
    mine = [s for s in steps if s.id.split(".")[0] in
            ("bootstrap", "images", "infra", "secrets", "github", "alerts", "handoff")]
    assert {s.id for s in mine} >= {"bootstrap.pass-1", "images.registry", "images.copy", "infra.pass-1",
                                    "secrets.platform-keys", "infra.pass-2", "bootstrap.pass-2",
                                    "alerts.policies", "handoff.console", "github.pass-3", "github.hook",
                                    "github.pass-4", "handoff.ready"}
    by_step = {s.id: s for s in steps}
    unowned = []
    for step in steps:
        for entry in step.satisfies:
            assert entry in by_id, f"{step.id} satisfies {entry}, which the manifest does not hold"
            for dep in by_id[entry].depends_on:
                if dep in step.satisfies:
                    continue
                # The engine also counts a dependency met once the step its
                # entry names (`entry.step`) is done: how a terraform pass,
                # which lists nothing in `satisfies`, owns what it makes.
                named = by_id[dep].step if dep in by_id else None
                made_by = owner.get(dep) or by_step.get(named or "")
                if made_by is None:
                    unowned.append(f"{step.id}: {entry} depends on {dep}, which no step satisfies or owns")
                elif made_by.stage > step.stage:
                    unowned.append(f"{step.id}: {dep} is made later, by {made_by.id} in stage {made_by.stage}")
    assert unowned == [], "\n".join(unowned)


def test_resume_after_pass3_reapplies_no_earlier_pass(tmp_path: Path) -> None:
    """Later stages change what an earlier pass would generate (bootstrap-1
    gains the workspace-apply image, infra-1 and infra-2 gain the alerts and
    the admin groups). On a resume an earlier pass whose root a later pass has
    since applied is met: re-applying it would turn the key switches and the
    App's switches off again (lesson 39). Only the latest applied pass of each
    root compares its inputs."""
    from swarm_install.steps.bootstrap import BootstrapPass1Step, Deps, applied_key, generate, inputs_digest
    from swarm_install.steps.github_switches import Pass3Step, Pass4Step
    from swarm_install.steps.infra import BootstrapPass2Step, InfraPass1Step, InfraPass2Step

    class Project:
        def service_account(self, email):
            return {"email": email}

        def secret(self, project, name):
            return {"name": name}

        def enabled_version(self, project, name):
            return 1

    deps = Deps(gcp=lambda: Project(), exec_=FakeExec(), root_dir=lambda ctx, root: tmp_path)
    ctx = FakeCtx(install_state(github_app={"id": "42", "slug": "swarmcloud"}))
    for pass_name in ("bootstrap-1", "infra-1", "infra-2", "bootstrap-2", "infra-3"):
        ctx.outputs[applied_key(pass_name)] = inputs_digest(generate(ctx, pass_name, Project()))
    ctx.outputs["image_digests"] = {"workspace-apply": "sha256:" + "a" * 64}
    ctx.outputs["metric_descriptors_ready"] = True
    ctx.outputs["admin_groups"] = ["admins@example.com"]

    for step in (BootstrapPass1Step(deps), InfraPass1Step(deps), InfraPass2Step(deps)):
        assert step.check(ctx).status == "met", f"{step.pass_name} would be re-applied on a resume"
    assert BootstrapPass2Step(deps).check(ctx).status == "unmet", "bootstrap's latest pass still sees drift"
    assert Pass3Step(deps).check(ctx).status == "unmet", "infra's latest pass still sees drift"
    assert Pass4Step(deps).check(ctx).status == "unmet", "infra-4 has not been applied"
