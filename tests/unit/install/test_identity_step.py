"""Stage 8, identity wiring (DAYONE-ENGINE-4; design §8, Q2; B04; lesson 20;
review finding 8).

On fakes, with no credentials: the probe goes through swarm-api (a fake
`api_client.ApiClient`) and never to the Groups API; a group is registered
only after its probe passes; an `unauthorized_client` points at the Admin
console, never at IAM; and the admin-role path is offered before delegation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from swarm_install.api_client import ADMIN_ROLE, DELEGATION, GroupResult, GroupsProbe
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.steps import identity

from .test_preflight import PROJECT, Answers, FakeGcp, Rig, ctx_for

GROUP = "eng@example.com"
MEMBER = "lea@example.com"
ADMINS = "platform-admins@example.com"


class FakeApi:
    """swarm-api's groups-probe route, in one mode; `answer(group)` decides each result."""

    def __init__(self, mode: str = ADMIN_ROLE, answer: Any = None) -> None:
        self.mode = mode
        self.answer = answer or (lambda group: GroupResult(group, MET))
        self.probes: list[list[dict[str, str]]] = []

    def groups_probe(self, probes: list[dict[str, str]]) -> GroupsProbe:
        self.probes.append([dict(p) for p in probes])
        return GroupsProbe(self.mode, tuple(self.answer(p["group"]) for p in probes))


class IdentityGcp(FakeGcp):
    """IAM knows swarm-api's numeric unique id; nothing here reads groups."""

    def service_account(self, email: str) -> dict[str, Any] | None:
        self._call("service_account", email)
        return {"email": email, "uniqueId": "112233445566778899001"}


def _steps(gcp: FakeGcp, ask: Answers, api: FakeApi) -> list[Any]:
    return identity.build_steps(lambda: gcp, ask, lambda ctx: api)


def test_group_registered_only_after_probe_passes(tmp_path: Path) -> None:
    """Lesson 20, the 503 trap: a group swarm-api cannot read is never
    registered; the same group, once readable, is."""
    readable = {"yes": False}
    api = FakeApi(answer=lambda g: GroupResult(g, MET) if readable["yes"] else GroupResult(g, UNMET, "not_found"))
    rig = Rig(tmp_path)
    rig.store.update(lambda s: s.outputs.update({"probe_group": GROUP, "probe_member": MEMBER,
                                                 "groups_auth": ADMIN_ROLE}))
    gcp = IdentityGcp()
    ask = Answers(identity_admin_group=[ADMINS, MEMBER])
    admin = identity.AdminGroupStep(lambda: gcp, ask, lambda ctx: api)

    result = rig.engine([admin]).run()
    assert result.exit_code == ExitCode.FAIL
    assert "not registered" in result.message and ADMINS in result.message
    assert "admin_groups" not in rig.store.load().outputs

    readable["yes"] = True
    ask.by_step["identity.admin-group"] = [ADMINS, MEMBER]
    result = rig.engine([admin]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.store.load().outputs["admin_groups"] == [ADMINS]
    # The probe came before the registration, for the very group registered.
    assert {"group": ADMINS, "member": MEMBER} in api.probes[0]


def test_unauthorized_client_names_admin_console_not_iam() -> None:
    """B04: `unauthorized_client` is the Admin console (the grant was never
    made there), not an IAM binding; only a signJwt refusal is a GCP grant."""
    gcp = IdentityGcp()
    for mode, path in ((DELEGATION, identity.DELEGATION_PATH), (ADMIN_ROLE, identity.ADMIN_ROLE_PATH)):
        api = FakeApi(mode, lambda g: GroupResult(g, UNMET, "unauthorized_client"))
        ctx = ctx_for(outputs={"probe_group": GROUP, "probe_member": MEMBER, "groups_auth": mode})
        observed = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: api).check(ctx)
        assert observed.status == UNMET
        assert "admin.google.com" in observed.detail and path in observed.detail
        assert "not IAM" in observed.detail
        for iam_word in ("roles/", "add-iam-policy-binding", "gcloud"):
            assert iam_word not in observed.detail
    # Delegation names swarm-api's numeric client id, read from IAM.
    api = FakeApi(DELEGATION, lambda g: GroupResult(g, UNMET, "unauthorized_client"))
    ctx = ctx_for(outputs={"probe_group": GROUP, "probe_member": MEMBER, "groups_auth": DELEGATION})
    detail = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: api).check(ctx).detail
    assert "112233445566778899001" in detail and identity.GROUPS_SCOPE in detail
    # A signJwt refusal is the one GCP grant.
    api = FakeApi(DELEGATION, lambda g: GroupResult(g, UNMET, "sign_jwt_denied"))
    detail = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: api).check(ctx).detail
    assert "signJwt" in detail and "admin.google.com" not in detail


def test_admin_role_path_offered_first(tmp_path: Path) -> None:
    """Q2: the plan prints the Groups Reader admin role first, delegation as
    the fallback; the person's default answer is the admin-role path, and
    delegation asks for its mailbox only when chosen."""
    gcp = IdentityGcp()
    step = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: FakeApi())
    plan = step.plan(ctx_for())
    assert "admin role" in plan.actions[0] and "recommended" in plan.actions[0]
    assert "delegation" in plan.actions[1] and "fallback" in plan.actions[1]
    assert plan.actions[0].index(identity.ADMIN_ROLE_PATH) >= 0

    rig = Rig(tmp_path)
    ask = Answers(identity_group_read=["admin-role", GROUP, MEMBER])
    api = FakeApi(ADMIN_ROLE)
    result = rig.engine([identity.GroupReadStep(lambda: gcp, ask, lambda c: api)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    asked = [q for _, q in ask.asked]
    assert "admin-role (recommended)" in asked[0]
    assert not any("mailbox" in q for q in asked)
    outputs = rig.store.load().outputs
    assert outputs["groups_auth"] == ADMIN_ROLE and "groups_impersonate_user" not in outputs

    rig = Rig(tmp_path / "fallback")
    ask = Answers(identity_group_read=["delegation", "groups-reader@example.com", GROUP, MEMBER])
    result = rig.engine([identity.GroupReadStep(lambda: gcp, ask, lambda c: FakeApi(DELEGATION))]).run()
    assert result.exit_code == ExitCode.OK, result.message
    outputs = rig.store.load().outputs
    assert outputs["groups_auth"] == DELEGATION
    assert outputs["groups_impersonate_user"] == "groups-reader@example.com"


def test_probe_runs_through_swarm_api_in_its_mode() -> None:
    """Review finding 8: the probe is swarm-api's own read, in the mode
    swarm-api runs. The engine calls no Groups API itself, and a swarm-api
    still in another mode than the install chose is not met."""
    gcp = IdentityGcp()
    ctx = ctx_for(outputs={"probe_group": GROUP, "probe_member": MEMBER, "groups_auth": ADMIN_ROLE})

    api = FakeApi(ADMIN_ROLE)
    observed = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: api).check(ctx)
    assert observed.status == MET
    assert observed.outputs["groups_auth"] == ADMIN_ROLE
    assert api.probes == [[{"group": GROUP, "member": MEMBER}]]
    assert not any("cloudidentity" in repr(args) for _, args in gcp.calls)

    stale = FakeApi(DELEGATION)
    observed = identity.GroupReadStep(lambda: gcp, Answers(), lambda c: stale).check(ctx)
    assert observed.status == UNMET
    assert "delegation mode" in observed.detail and "GROUPS_AUTH" in observed.detail


def test_steps_satisfy_every_stage_8_identity_entry() -> None:
    from swarm_install.manifest import load

    ids = {e.id for e in load().entries}
    covered = {entry for step in identity.STEPS for entry in step.satisfies}
    assert covered <= ids
    assert {"workspace-delegation.groups-reader-admin-role", "workspace-delegation.groups-dwd",
            "workspace-group.admin-surface-swarm-admins", "workspace-group.tenant-groups",
            "workspace-group.cloud-identity-presence", "switch.enable_qa_identity", "sa.qa-console",
            "role.custom.swarmQaJwtSigner", "iam-binding.qa-console-authoritative-policy",
            "declared-value.allowed-domains-solo"} == covered
    # Review finding 5: every stage 8 entry of the manifest has a step.
    stage_8 = {e.id for e in load().entries if e.stage == 8}
    assert stage_8 <= covered


class QaFakeGcp(FakeGcp):
    """IAM's view of qa-console, its role and its policy, and swarm-api's env."""

    def __init__(self) -> None:
        super().__init__()
        self.accounts: set[str] = {f"qa-console@{PROJECT}.iam.gserviceaccount.com"}
        self.permissions = ["iam.serviceAccounts.signJwt"]
        self.bindings = [{"role": f"projects/{PROJECT}/roles/swarmQaJwtSigner",
                          "members": [f"serviceAccount:swarm-api@{PROJECT}.iam.gserviceaccount.com"]}]
        self.allowed = "example.com"

    def service_account(self, email: str) -> dict[str, Any] | None:
        self._call("service_account", email)
        return {"email": email} if email in self.accounts else None

    def custom_role(self, project: str, role: str) -> dict[str, Any] | None:
        self._call("custom_role", project, role)
        return {"name": f"projects/{project}/roles/{role}", "includedPermissions": list(self.permissions)}

    def service_account_policy(self, email: str) -> dict[str, Any]:
        self._call("service_account_policy", email)
        return {"bindings": [dict(b) for b in self.bindings]}

    def run_service(self, project: str, region: str, name: str) -> dict[str, Any] | None:
        self._call("run_service", project, region, name)
        return {"template": {"containers": [{"env": [{"name": "ALLOWED_DOMAINS", "value": self.allowed}]}]}}


def test_qa_console_identity_read_exactly_as_designed() -> None:
    """Review finding 5 (stage 8): qa-console exists, its role carries signJwt
    alone and its policy binds that role to swarm-api alone. Stage 8 reads it;
    its apply names the bootstrap pass that writes it and writes nothing."""
    gcp = QaFakeGcp()
    step = identity.QaConsoleStep(lambda: gcp)
    ctx = ctx_for()
    assert step.check(ctx).status == MET

    gcp.bindings[0]["members"].append("user:someone@example.com")
    observed = step.check(ctx)
    assert observed.status == UNMET and "swarm-api alone" in observed.detail
    with pytest.raises(RuntimeError, match="DAYONE-ENGINE-3"):
        step.apply(ctx, step.plan(ctx))
    assert not gcp.called("set_iam_policy")

    gcp.bindings[0]["members"].pop()
    gcp.permissions = ["iam.serviceAccounts.signJwt", "iam.serviceAccounts.getAccessToken"]
    assert step.check(ctx).status == UNMET

    gcp.accounts.clear()
    assert "does not exist" in step.check(ctx).detail


def test_allowed_domains_hold_the_organisation_never_gmail() -> None:
    """`declared-value.allowed-domains-solo`: swarm-api's ALLOWED_DOMAINS
    holds the admin group's domain and no consumer domain."""
    gcp = QaFakeGcp()
    step = identity.AllowedDomainsStep(lambda: gcp)
    ctx = ctx_for(outputs={"admin_groups": [ADMINS]})
    observed = step.check(ctx)
    assert observed.status == MET and observed.outputs["allowed_domains"] == ["example.com"]

    gcp.allowed = "example.com,gmail.com"
    assert "every Google account" in step.check(ctx).detail

    gcp.allowed = "another.org"
    observed = step.check(ctx)
    assert observed.status == UNMET and "example.com" in observed.detail

    assert step.check(ctx_for()).status == UNMET  # no admin group yet: the domain is not known
