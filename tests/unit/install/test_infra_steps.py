"""The order the engine applies its generated inputs in (DAYONE-ENGINE-INPUTS;
decision 2; design §5): stage 8 re-applies infra pass 2 after the identity
steps, and a closing infra pass after the cluster stages removes the
installer's address from the control plane's authorised networks."""

from __future__ import annotations

from typing import Any

import pytest

from swarm_install import tfvars
from swarm_install.engine import Engine
from swarm_install.steps import bootstrap, discover, infra
from swarm_install.steps.bootstrap import Deps, applied_key, generate, inputs_digest
from swarm_install.steps.infra import ClosingPassStep, IdentityPassStep

from .test_terraform_runner import FakeCtx


class SecretMetadata:
    """Secret Manager's metadata, and the versions the engine adds."""

    def __init__(self, containers: set[str], versioned: set[str]) -> None:
        self.containers = set(containers)
        self.versioned = set(versioned)
        self.added: list[str] = []

    def secret(self, project: str, name: str) -> dict | None:
        return {"name": name} if name in self.containers else None

    def enabled_version(self, project: str, name: str) -> int | None:
        return 1 if name in self.versioned else None

    def add_secret_version(self, project: str, name: str, data: bytes) -> int:
        self.added.append(name)
        self.versioned.add(name)
        return 1


def every_key() -> set[str]:
    return {*tfvars.KEY_SWITCHES, *tfvars.APP_SECRETS, tfvars.DESKTOP_SECRET}


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """bootstrap.apply_pass, faked: it records the pass and the inputs it would write."""
    log: list[tuple[str, dict]] = []

    def apply_pass(ctx: Any, deps: Deps, pass_name: str, **kwargs: Any) -> dict:
        inputs = generate(ctx, pass_name, deps.gcp())
        log.append((pass_name, inputs))
        ctx.state.outputs[applied_key(pass_name)] = inputs_digest(inputs)
        return {}

    monkeypatch.setattr(bootstrap, "apply_pass", apply_pass)
    return log


def mark_applied(ctx: FakeCtx, gcp: SecretMetadata, *passes: str) -> None:
    for pass_name in passes:
        ctx.state.outputs[applied_key(pass_name)] = inputs_digest(generate(ctx, pass_name, gcp))


def test_identity_pass_runs_after_every_stage_8_identity_step() -> None:
    steps = discover()
    ids = [s.id for s in steps]
    reapply = ids.index("infra.pass-identity")
    stage_8 = [s for s in steps if s.stage == 8 and s.id.startswith("identity.")]
    assert stage_8, "stage 8's identity steps are registered"
    assert all(ids.index(s.id) < reapply for s in stage_8), "the re-apply follows every identity step"
    assert steps[reapply].stage == 8
    # The engine keeps that order: within a stage, registration order unless an entry says otherwise.
    engine = Engine.__new__(Engine)
    engine.steps, engine.manifest, engine._owner = steps, None, {}
    ordered = [s.id for s in engine._ordered()]
    assert all(ordered.index(s.id) < ordered.index("infra.pass-identity") for s in stage_8)
    assert ordered.index("infra.pass-2") < ordered.index("infra.pass-identity")


def test_identity_pass_applies_the_admin_groups(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    step = IdentityPassStep(Deps(gcp=lambda: gcp))
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-1", "infra-2")
    assert step.check(ctx).status == "met", "nothing recorded since pass 2"

    ctx.state.outputs["admin_groups"] = ["swarm-admins@example.com"]
    assert step.check(ctx).status == "unmet", "stage 8 recorded a group pass 2 has not applied"
    step.apply(ctx, step.plan(ctx))
    assert [p for p, _ in applied] == ["infra-2"]
    assert applied[0][1]["admin_groups"] == ["swarm-admins@example.com"]
    assert applied[0][1]["allowed_domains"] == ["example.com"]
    assert step.check(ctx).status == "met"


def test_identity_pass_versions_a_key_pass_2_made_before_applying(applied: list[tuple[str, dict]]) -> None:
    graph = "swarm-graph-ticket-key"
    gcp = SecretMetadata(every_key(), every_key() - {graph})
    ctx = FakeCtx()
    ctx.state.outputs["image_digests"] = {"swarm-graph": "sha256:" + "b" * 64}
    mark_applied(ctx, gcp, "infra-2")
    step = IdentityPassStep(Deps(gcp=lambda: gcp))
    assert step.check(ctx).status == "unmet", "the ticket key's container has no version"
    step.apply(ctx, step.plan(ctx))
    assert gcp.added == [graph], "the key gets its version first"
    assert applied[-1][1]["graph_service"] == {"serve": True, "enabled": True}, "then the switch"
    assert step.check(ctx).status == "met"


def test_closing_pass_after_the_cluster_stages() -> None:
    steps = discover()
    closing = next(s for s in steps if s.id == "infra.pass-close")
    cluster = [s for s in steps if s.id.startswith("cluster.")]
    assert cluster and all(s.stage < closing.stage for s in cluster), "after stages 10 and 11"
    handoff = next(s for s in steps if s.id == "handoff.console")
    assert closing.stage < handoff.stage, "before the hand-off's passes 3 and 4"
    assert tfvars.after("infra-3", "infra-close") and tfvars.after("infra-close", "infra-2")


def test_closing_pass_removes_the_installer_address(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-1", "infra-2")
    step = ClosingPassStep(Deps(gcp=lambda: gcp))
    assert step.check(ctx).status == "unmet", "a run that stopped before stage 12 left the address"
    step.apply(ctx, step.plan(ctx))
    assert [p for p, _ in applied] == ["infra-close"]
    assert applied[0][1]["gke_master_authorized_cidrs"] == []
    assert step.check(ctx).status == "met", "idempotent: the next run applies nothing"

    # Pass 2 is superseded now, so neither it nor stage 8's re-apply puts the address back.
    assert infra.InfraPass2Step(Deps(gcp=lambda: gcp)).check(ctx).status == "met"
    assert IdentityPassStep(Deps(gcp=lambda: gcp)).check(ctx).status == "met"

    ctx.state.outputs["admin_groups"] = ["swarm-admins@example.com"]
    assert step.check(ctx).status == "unmet", "a changed input is drift the closing pass repairs"
    step.apply(ctx, step.plan(ctx))
    assert applied[-1][1]["gke_master_authorized_cidrs"] == []


def test_closing_pass_met_once_pass_3_applied(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-2", "infra-3")
    assert ClosingPassStep(Deps(gcp=lambda: gcp)).check(ctx).status == "met", "pass 3 carries no address"
    assert generate(ctx, "infra-3", gcp)["gke_master_authorized_cidrs"] == []
    assert applied == []
