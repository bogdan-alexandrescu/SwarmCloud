"""The order the engine applies its generated inputs in (DAYONE-ENGINE-INPUTS;
decision 2; design §5): stage 8 re-applies infra pass 2 after the identity
steps, and a closing infra pass after the last kubectl the install runs from
the laptop (stage 14's verify reads) removes the installer's address from the
control plane's authorised networks."""

from __future__ import annotations

from typing import Any

import pytest

from swarm_install import record as install_record
from swarm_install import tfvars
from swarm_install.engine import Engine
from swarm_install.manifest import load as load_manifest
from swarm_install.steps import bootstrap, discover, infra
from swarm_install.steps.bootstrap import Deps, applied_key, generate, inputs_digest
from swarm_install.steps.infra import ClosingPassStep, IdentityPassStep

from .test_record_writers import FakeDocuments
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


def ordered_ids(steps: list[Any]) -> list[str]:
    """The order the engine runs the registry in, with the manifest and its fragments."""
    engine = Engine.__new__(Engine)
    engine.steps, engine.manifest = steps, load_manifest()
    engine._owner = {}
    for step in steps:
        for entry in step.satisfies:
            engine._owner.setdefault(entry, step)
    return [s.id for s in engine._ordered()]


def test_closing_pass_is_the_last_infra_pass() -> None:
    assert tfvars.PASSES[-1] == "infra-close"
    assert tfvars.after("infra-close", "infra-4") and not tfvars.after("infra-4", "infra-close")


def test_closing_pass_after_every_step_that_runs_kubectl() -> None:
    steps = discover()
    ordered = ordered_ids(steps)
    closing = ordered.index("infra.pass-close")
    # A step that runs kubectl from the laptop holds a Kube factory (cluster.py, verify.py).
    kubectl = [s.id for s in steps if hasattr(s, "_kube")]
    assert "verify.reconciler-jobs" in kubectl and any(i.startswith("cluster.") for i in kubectl)
    assert all(ordered.index(i) < closing for i in kubectl), "the address stays while kubectl runs"
    assert all(ordered.index(s.id) < closing for s in steps if s.id.startswith("verify."))
    assert closing < ordered.index("handoff.ready"), "closed before READY"
    handoff = next(s for s in steps if s.id == "handoff.console")
    assert handoff.stage < next(s for s in steps if s.id == "infra.pass-close").stage, "after passes 3 and 4"


@pytest.mark.parametrize("pass_name", ["infra-1", "infra-2", "infra-3", "infra-4"])
def test_every_pass_before_the_close_carries_the_address(pass_name: str) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    reach = generate(ctx, pass_name, gcp)["gke_master_authorized_cidrs"]
    assert reach == [{"cidr_block": ctx.state.outputs["installer_cidr"], "display_name": "swarm-install"}]


def test_closing_pass_removes_the_installer_address(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-1", "infra-2")
    step = ClosingPassStep(Deps(gcp=lambda: gcp))
    assert step.check(ctx).status == "unmet", "a run that stopped before the close left the address"
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


def test_closing_pass_follows_pass_4(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-2", "infra-3", "infra-4")
    step = ClosingPassStep(Deps(gcp=lambda: gcp))
    assert step.check(ctx).status == "unmet", "pass 4 still carries the address"
    step.apply(ctx, step.plan(ctx))
    assert [p for p, _ in applied] == ["infra-close"]
    closed = dict(applied[0][1])
    assert closed.pop("gke_master_authorized_cidrs") == []
    four = generate(ctx, "infra-4", gcp)
    four.pop("gke_master_authorized_cidrs")
    assert closed == four, "the close changes nothing else"
    assert step.check(ctx).status == "met"


# -- finding 6: pass 1 adopts the address stage 2 reserved --------------------------


class AddressGcp(SecretMetadata):
    """Secret Manager's metadata, and the global addresses the project holds."""

    def __init__(self, addresses: set[str]) -> None:
        super().__init__(set(), set())
        self.addresses = set(addresses)

    def global_address(self, project: str, name: str) -> dict | None:
        return {"name": name, "status": "RESERVED", "address": "34.1.2.3"} if name in self.addresses else None


class FakeTf:
    """The Terraform runner pass 1's `before_plan` sees: its state list, and each command it ran."""

    def __init__(self, listed: list[str]) -> None:
        self.listed = listed
        self.runs: list[tuple[tuple[str, ...], str | None]] = []
        self.listed_as: list[str | None] = []

    def state_list(self, *, impersonate: str | None = None) -> list[str]:
        self.listed_as.append(impersonate)
        return list(self.listed)

    def run(self, *args: str, impersonate: str | None = None, check: bool = True) -> None:
        self.runs.append((args, impersonate))


def _pass_1(monkeypatch: pytest.MonkeyPatch, gcp: Any, tf: FakeTf, ctx: FakeCtx | None = None) -> list[str]:
    """Pass 1 applied with `tf` as its runner: the passes apply_pass ran."""
    ran: list[str] = []
    monkeypatch.setattr(install_record, "open_record",
                        lambda project, env: install_record.InstallRecord(env=env, documents=FakeDocuments()))

    def apply_pass(ctx: Any, deps: Deps, pass_name: str, *, targets: Any = (), before_plan: Any = None) -> dict:
        assert before_plan is not None, "pass 1 adopts the reserved address before it plans"
        before_plan(tf)
        ran.append(pass_name)
        return {}

    monkeypatch.setattr(bootstrap, "apply_pass", apply_pass)
    step = infra.InfraPass1Step(Deps(gcp=lambda: gcp))
    ctx = ctx or FakeCtx()
    step.apply(ctx, step.plan(ctx))
    return ran


def test_pass_1_imports_the_address_stage_2_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding 6: edge.global-ip reserves swarm-ui-ip by API; the frontend module
    declares the same address. Pass 1 imports it, as the deployer, before it
    plans, so the apply never fails on 409 "already exists"."""
    from swarm_install.api_client import deployer_email
    from swarm_install.steps.edge import ADDRESS

    tf = FakeTf(["module.iam.google_service_account.this"])
    assert _pass_1(monkeypatch, AddressGcp({ADDRESS}), tf) == ["infra-1"]
    project = FakeCtx().project
    assert tf.runs == [(("import", "-input=false", "module.frontend[0].google_compute_global_address.this",
                         f"projects/{project}/global/addresses/{ADDRESS}"), deployer_email(project))]
    assert tf.listed_as == [deployer_email(project)]


def test_pass_1_skips_the_import_when_state_holds_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_install.steps.edge import ADDRESS

    tf = FakeTf([infra.FRONTEND_ADDRESS])
    assert _pass_1(monkeypatch, AddressGcp({ADDRESS}), tf) == ["infra-1"]
    assert tf.runs == [], "a resumed pass 1 imports nothing"

    absent = FakeTf([])
    assert _pass_1(monkeypatch, AddressGcp(set()), absent) == ["infra-1"]
    assert absent.runs == [], "nothing reserved: Terraform creates it"


def test_pass_1_attaches_the_record_after_its_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """N1: Firestore exists once pass 1 applied, so the step attaches the
    install record there and the rest of this first run mirrors to it."""
    ctx = FakeCtx()
    assert _pass_1(monkeypatch, AddressGcp(set()), FakeTf([]), ctx) == ["infra-1"]
    assert isinstance(ctx.record, install_record.InstallRecord)
    assert ctx.record.env == ctx.environment


def test_no_import_block_under_terraform_infra() -> None:
    """The live deployment's plan does not change: the import is the engine's."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "terraform" / "infra"
    assert not [p.name for p in root.glob("*.tf") if "import {" in p.read_text()]


# -- finding 16: the reach reopens after the closing pass, and closes again ---------


def _closed(gcp: SecretMetadata) -> FakeCtx:
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-2", "infra-3", "infra-4", "infra-close")
    return ctx


def test_reopen_after_the_close_applies_the_current_address(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = _closed(gcp)
    close = ClosingPassStep(Deps(gcp=lambda: gcp))
    assert close.check(ctx).status == "met"

    def moved(c: Any) -> None:
        c.state.outputs["installer_cidr"] = "203.0.113.9/32"

    reach = infra.ClusterReach(Deps(gcp=lambda: gcp), address=moved)
    assert reach.reopen(ctx) is True
    assert [p for p, _ in applied] == ["infra-4"]
    assert applied[0][1]["gke_master_authorized_cidrs"] == [
        {"cidr_block": "203.0.113.9/32", "display_name": "swarm-install"}], "this machine's address now"
    assert ctx.outputs[infra.REOPENED] == "203.0.113.9/32"
    assert reach.reopen(ctx) is False, "reopened once"
    assert len(applied) == 1

    # swarm install's stage 14 closes it again: the closing pass is unmet while reopened.
    assert close.check(ctx).status == "unmet"
    close.apply(ctx, close.plan(ctx))
    assert applied[-1][0] == "infra-close" and applied[-1][1]["gke_master_authorized_cidrs"] == []
    assert not ctx.outputs.get(infra.REOPENED)
    assert close.check(ctx).status == "met"


def test_reopen_is_nothing_before_the_close(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = FakeCtx()
    mark_applied(ctx, gcp, "infra-1", "infra-2")
    reach = infra.ClusterReach(Deps(gcp=lambda: gcp), address=lambda c: None)
    assert reach.reopen(ctx) is False and reach.close(ctx) is False
    assert applied == []


def test_reopen_refuses_a_dry_run(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = _closed(gcp)
    ctx.dry_run = True
    with pytest.raises(RuntimeError, match="dry run never applies"):
        infra.ClusterReach(Deps(gcp=lambda: gcp), address=lambda c: None).reopen(ctx)
    assert applied == []


def test_a_verb_closes_what_it_reopened(applied: list[tuple[str, dict]]) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = _closed(gcp)
    reach = infra.ClusterReach(Deps(gcp=lambda: gcp), address=lambda c: None)
    reach.reopen(ctx)
    assert reach.close_after(ctx, "swarm team add") is None
    assert [p for p, _ in applied] == ["infra-4", "infra-close"]
    assert not ctx.outputs.get(infra.REOPENED)


def test_a_failed_close_leaves_the_record_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    gcp = SecretMetadata(every_key(), every_key())
    ctx = _closed(gcp)
    ctx.state.outputs[infra.REOPENED] = "203.0.113.9/32"

    def refused(*args: Any, **kwargs: Any) -> dict:
        raise RuntimeError("terraform apply in infra failed")

    monkeypatch.setattr(bootstrap, "apply_pass", refused)
    line = infra.ClusterReach(Deps(gcp=lambda: gcp)).close_after(ctx, "swarm upgrade")
    assert line is not None and "203.0.113.9/32" in line and "swarm install removes it" in line
    assert ctx.outputs[infra.REOPENED] == "203.0.113.9/32", "the next swarm install closes it"


def test_verb_context_saves_through_the_store() -> None:
    from swarm_install.state import InstallState

    saved: list[InstallState] = []
    state = InstallState(project="p", environment="dev")

    def save(change: Any) -> InstallState:
        fresh = InstallState(project="p", environment="dev")
        change(fresh)
        saved.append(fresh)
        return fresh

    ctx = infra.VerbContext(project="p", environment="dev", holder="me@example.com", state=state,
                            events=None, bucket="bucket", save=save)
    from swarm_install.steps.preflight import remember

    remember(ctx, outputs={infra.REOPENED: "203.0.113.9/32"})
    assert ctx.outputs[infra.REOPENED] == "203.0.113.9/32" and saved[0] is ctx.state
    assert ctx.engine.store.bucket == "bucket"
