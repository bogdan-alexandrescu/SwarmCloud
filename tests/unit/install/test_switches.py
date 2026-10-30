"""Stage 9's switches after the console's GitHub step (DAYONE-ENGINE-3; plan
R8, §5.5; review findings 2 and 10; lesson 39)."""

from __future__ import annotations

import ast
from pathlib import Path

from swarm_install import tfvars
from swarm_install.state import StepRecord
from swarm_install.steps import discover, github_switches, handoff
from swarm_install.steps.bootstrap import Deps
from swarm_install.steps.github_switches import HookStep, Pass3Step, Pass4Step

from .test_state_store import FakeClock
from .test_terraform_runner import FakeCtx, install_state

PACKAGE = Path(github_switches.__file__).resolve().parent.parent


class SecretMetadata:
    """Secret Manager as the engine may see it: metadata only, and a record of every call."""

    def __init__(self, enabled: set[str] | None = None) -> None:
        self.enabled = set(enabled or ())
        self.calls: list[tuple[str, str]] = []

    def secret(self, project, name):
        self.calls.append(("secret", name))
        return {"name": name} if name in self.enabled else None

    def enabled_version(self, project, name):
        self.calls.append(("enabled_version", name))
        return 1 if name in self.enabled else None

    def __getattr__(self, name):
        raise AssertionError(f"the engine called {name} on Secret Manager")


def infra(pass_name: str, versions: dict, **outputs) -> dict:
    state = install_state(**outputs)
    return tfvars.infra_inputs(state, pass_name=pass_name, installer="admin@example.com", versions=versions)


def test_switch_true_only_with_enabled_version() -> None:
    every = {name: True for name in (*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS)}
    app = {"github_app": {"id": "42", "slug": "swarmcloud"}}
    on = infra("infra-4", every, **app)
    assert on["enable_child_tasks"] is True and on["enable_github_app"] is True
    assert on["enable_forge_refresh"] is True and on["enable_github_hooks"] is True
    assert on["workspace_grant_attestation"]["serve"] is True

    for missing in (*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS):
        versions = dict(every, **{missing: False})
        inputs = infra("infra-4", versions, **app)
        if missing in tfvars.KEY_SWITCHES:
            switch = tfvars.KEY_SWITCHES[missing]
            value = inputs[switch.split(".")[0]][switch.split(".")[1]] if "." in switch else inputs[switch]
            assert value is False, f"{switch} is on without a version of {missing}"
        else:
            assert inputs["enable_github_app"] is False, f"the App is on without a version of {missing}"
            assert inputs["enable_github_hooks"] is False

    early = infra("infra-1", every, **app)
    assert early["enable_child_tasks"] is False, "before infra pass 2 every key switch is off"
    assert early["enable_github_app"] is False


def test_app_switches_after_console_app_lands() -> None:
    gcp = SecretMetadata()
    step = Pass3Step(Deps(gcp=lambda: gcp))
    ctx = FakeCtx()
    assert step.trigger(ctx) is False, "no App document yet"
    ctx.console["github_app"] = {"id": "42", "slug": "swarmcloud", "secrets_written_at": 1.0}
    assert step.trigger(ctx) is False, "the App's secrets have no version yet (key before switch)"
    gcp.enabled = set(tfvars.APP_SECRETS[:2])
    assert step.trigger(ctx) is False, "every one of the three needs a version"
    gcp.enabled = set(tfvars.APP_SECRETS)
    assert step.trigger(ctx) is True

    # The three run as triggered steps of the hand-off wait, never on their own (R8).
    top = {s.id for s in discover()}
    assert not top & {"github.pass-3", "github.hook", "github.pass-4"}
    wait = next(s for s in discover() if s.id == "handoff.console")
    assert [s.id for s in wait.triggered] == ["github.pass-3", "github.hook", "github.pass-4"]
    assert isinstance(wait, handoff.HandoffStep)
    assert "enable_github_app" not in {k for k, v in infra("infra-2", {}).items() if v is True}


def test_hooks_switch_after_self_check() -> None:
    ctx = FakeCtx(clock=FakeClock())
    calls: list[str] = []

    class Api:
        def activate_hook(self):
            calls.append("hook")
            return {"active": True}

    hook = HookStep(Deps(gcp=lambda: SecretMetadata()), api=lambda c: Api())
    pass4 = Pass4Step(Deps(gcp=lambda: SecretMetadata()))
    ctx.console["github_app"] = {"id": "42", "self_check": {"ok": True}}
    assert hook.trigger(ctx) is False, "pass 3 has not run"
    ctx.state.steps["github.pass-3"] = StepRecord(state="done", at=1.0, by="admin@example.com")
    ctx.console["github_app"]["self_check"] = {"ok": False}
    assert hook.trigger(ctx) is False, "the self-check has not passed"
    ctx.console["github_app"]["self_check"] = {"ok": True}
    assert hook.trigger(ctx) is True
    assert pass4.trigger(ctx) is False, "pass 4 waits on the hook"
    hook.apply(ctx, hook.plan(ctx))
    assert calls == ["hook"] and hook.check(ctx).status == "met"

    every = {name: True for name in (*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS)}
    assert infra("infra-3", every, github_app={"id": "42"})["enable_github_hooks"] is False
    assert infra("infra-4", every, github_app={"id": "42"})["enable_github_hooks"] is True


def test_engine_never_reads_an_app_secret() -> None:
    gcp = SecretMetadata(set(tfvars.APP_SECRETS))
    ctx = FakeCtx()
    ctx.console["github_app"] = {"id": "42", "secrets_written_at": 1.0}
    assert Pass3Step(Deps(gcp=lambda: gcp)).trigger(ctx) is True
    assert {verb for verb, _ in gcp.calls} == {"secret", "enabled_version"}, "metadata only"

    # No module of the installer names a secret-value read (`:access`, `access_secret_version`).
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert ":access" not in node.value, f"{path.name} reads a secret's value"
            if isinstance(node, (ast.Attribute, ast.Name)):
                name = node.attr if isinstance(node, ast.Attribute) else node.id
                assert "access_secret" not in name, f"{path.name} reads a secret's value"
