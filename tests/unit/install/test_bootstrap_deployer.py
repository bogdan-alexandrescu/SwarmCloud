"""A fresh install's bootstrap creates the deployer, lets the installer
impersonate it, and applies IAP membership as the person
(DAYONE-BOOTSTRAP-DEPLOYER, N2, N3, N4; part of #2688).

On fakes, with no credentials and no terraform binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_install import tfvars
from swarm_install.api_client import deployer_email
from swarm_install.exitcodes import MET, UNMET
from swarm_install.steps import bootstrap
from swarm_install.steps.bootstrap import (
    IMPERSONABLE_KEY,
    IMPERSONATION_POLL_S,
    IMPERSONATION_WAIT_S,
    BootstrapPass1Step,
    BootstrapPass2Step,
    Deps,
    ImpersonationPending,
    applied_key,
    await_impersonation,
    generate,
    inputs_digest,
)
from swarm_install.terraform import AS_PERSON, IMPERSONATE_ENV, Guards

from .test_terraform_runner import PROJECT, FakeCtx, FakeExec, install_state, run

INSTALLER = "admin@example.com"


class Project:
    """The project as the step reads it: the deployer exists."""

    def service_account(self, email):
        return {"email": email}

    def secret(self, project, name):
        return {"name": name}

    def enabled_version(self, project, name):
        return 1

    def key_ring(self, project, region, name):
        return None

    def custom_roles(self, project):
        return []

    def sa_permissions(self, email, permissions):
        return set(permissions)

    def lint_condition(self, resource, expression):
        return []


class Minter:
    """generateAccessToken that refuses `refusals` times, then issues a token."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls: list[str] = []

    def __call__(self, email: str) -> bool:
        self.calls.append(email)
        return len(self.calls) > self.refusals


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def deps_for(tmp_path: Path, minter: Minter, clock: Clock, exec_: FakeExec | None = None) -> Deps:
    return Deps(gcp=lambda: Project(), exec_=exec_ or FakeExec(), root_dir=lambda ctx, root: tmp_path,
                mint_token=minter, sleep=clock.sleep, monotonic=clock.monotonic)


# -- N2, N3: the bootstrap inputs ---------------------------------------------------


@pytest.mark.parametrize("pass_name", ["bootstrap-1", "bootstrap-2"])
def test_an_install_turns_the_deployer_on_without_the_github_pool(pass_name: str) -> None:
    inputs = tfvars.bootstrap_inputs(install_state(), pass_name=pass_name, installer=INSTALLER)
    assert inputs["enable_deployer"] is True, "every pass after bootstrap-1 runs as the deployer (N2)"
    assert inputs["enable_github_wif"] is False, "an install federates nothing from GitHub without the CI add-on"


@pytest.mark.parametrize("pass_name", ["bootstrap-1", "bootstrap-2"])
def test_the_installer_is_granted_impersonation_of_the_deployer(pass_name: str) -> None:
    inputs = tfvars.bootstrap_inputs(install_state(), pass_name=pass_name, installer=INSTALLER)
    # The member `swarm uninstall` removes (uninstall.remove_impersonation_grant).
    assert inputs["deployer_impersonator"] == f"user:{INSTALLER}"


def test_the_impersonator_comes_from_the_recorded_installer() -> None:
    ctx = FakeCtx(install_state(installer="owner@example.com"))
    assert generate(ctx, "bootstrap-1", Project())["deployer_impersonator"] == "user:owner@example.com"


# -- N4: bootstrap pass 2 runs as the person ------------------------------------


def test_bootstrap_pass_2_runs_as_the_person(tmp_path: Path) -> None:
    assert "bootstrap-2" in AS_PERSON
    exec_ = FakeExec()
    checked: list[str] = []
    g = Guards(deployer_exists=lambda: True, can_impersonate=lambda: checked.append("impersonate") or False,
               act_as_missing=lambda: checked.append("act_as") or [])
    run(tmp_path, "bootstrap-2", exec_, g=g)
    applies = exec_.env_of("apply")
    assert len(applies) == 1
    assert all(IMPERSONATE_ENV not in env for env in exec_.env_of("plan") + applies), (
        "bootstrap pass 2 applies IAP membership, which the deployer by design cannot"
    )
    assert checked == [], "a pass run as the person checks no impersonation of the deployer"


def test_bootstrap_pass_2_plan_says_it_runs_as_you() -> None:
    step = BootstrapPass2Step(Deps(gcp=lambda: Project()))
    actions = step.plan(FakeCtx()).actions
    assert any(a.endswith("bootstrap as you") for a in actions), actions
    assert not any(deployer_email(PROJECT) in a for a in actions), actions
    assert "as the deployer" not in step.explain()


# -- N3: the poll after bootstrap pass 1 ----------------------------------------


def test_the_poll_waits_until_the_grant_has_spread(tmp_path: Path) -> None:
    minter, clock = Minter(refusals=3), Clock()
    ctx = FakeCtx()
    await_impersonation(ctx, deps_for(tmp_path, minter, clock), deployer_email(PROJECT), step="bootstrap.pass-1")
    assert minter.calls == [deployer_email(PROJECT)] * 4
    assert clock.slept == [IMPERSONATION_POLL_S] * 3
    assert ctx.outputs[IMPERSONABLE_KEY] is True


def test_the_poll_gives_up_after_the_wait_with_what_to_do(tmp_path: Path) -> None:
    minter, clock = Minter(refusals=10_000), Clock()
    ctx = FakeCtx()
    with pytest.raises(ImpersonationPending, match="run swarm install again"):
        await_impersonation(ctx, deps_for(tmp_path, minter, clock), deployer_email(PROJECT), step="bootstrap.pass-1")
    assert clock.now >= IMPERSONATION_WAIT_S
    assert IMPERSONABLE_KEY not in ctx.outputs


def test_pass_1_polls_after_its_apply(tmp_path: Path) -> None:
    minter, clock, exec_ = Minter(refusals=2), Clock(), FakeExec(state=["google_service_account.deployer"])
    deps = deps_for(tmp_path, minter, clock, exec_)
    ctx = FakeCtx()
    BootstrapPass1Step(deps).apply(ctx, None)  # type: ignore[arg-type]
    assert exec_.verbs().count("apply") == 1
    assert len(minter.calls) == 3
    assert ctx.outputs[applied_key("bootstrap-1")] and ctx.outputs[IMPERSONABLE_KEY] is True


def test_pass_1_is_unmet_until_the_installer_can_impersonate(tmp_path: Path) -> None:
    deps = deps_for(tmp_path, Minter(refusals=0), Clock())
    ctx = FakeCtx()
    ctx.outputs[applied_key("bootstrap-1")] = inputs_digest(generate(ctx, "bootstrap-1", Project()))
    seen = BootstrapPass1Step(deps).check(ctx)
    assert seen.status == UNMET and "impersonate" in seen.detail
    ctx.outputs[IMPERSONABLE_KEY] = True
    assert BootstrapPass1Step(deps).check(ctx).status == MET


def test_a_resume_after_a_timed_out_poll_polls_without_reapplying(tmp_path: Path) -> None:
    minter, clock, exec_ = Minter(refusals=1), Clock(), FakeExec()
    deps = deps_for(tmp_path, minter, clock, exec_)
    ctx = FakeCtx()
    ctx.outputs[applied_key("bootstrap-1")] = inputs_digest(generate(ctx, "bootstrap-1", Project()))
    BootstrapPass1Step(deps).apply(ctx, None)  # type: ignore[arg-type]
    assert "apply" not in exec_.verbs(), "the pass's inputs are unchanged: only the poll runs again"
    assert ctx.outputs[IMPERSONABLE_KEY] is True


def test_the_real_minter_reads_a_refusal_as_not_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth
    from google.auth import exceptions, impersonated_credentials

    monkeypatch.setattr(google.auth, "default", lambda scopes: (object(), PROJECT))
    seen: dict = {}

    class Refused:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

        def refresh(self, request) -> None:
            raise exceptions.RefreshError("iam.serviceAccounts.getAccessToken denied")

    monkeypatch.setattr(impersonated_credentials, "Credentials", Refused)
    assert bootstrap.can_mint_token(deployer_email(PROJECT)) is False
    assert seen["target_principal"] == deployer_email(PROJECT)

    class Issued(Refused):
        def refresh(self, request) -> None:
            return None

    monkeypatch.setattr(impersonated_credentials, "Credentials", Issued)
    assert bootstrap.can_mint_token(deployer_email(PROJECT)) is True
