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
from swarm_install.exitcodes import MET, UNMET, WAITING, ExitCode
from swarm_install.steps import Observation, Plan, identity
from swarm_install.steps.preflight import ApiError
from swarm_install.state import StepRecord

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
            "workspace-group.install-groups", "workspace-delegation.groups-reader-assignment",
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
    with pytest.raises(RuntimeError, match="DAYONE-ENGINE-INPUTS"):
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


# -- DAYONE-IDENTITY-STAGE: the groups and the role through the APIs -----------

DOMAIN = "example.com"
INSTALLER = "lea@example.com"
CUSTOMER = "C0abc12"
READER_ID = "91447453409034242"
UNIQUE_ID = "112233445566778899001"


class WorkspaceGcp(IdentityGcp):
    """The Directory and Cloud Identity Groups APIs on dictionaries. `refuse`
    makes a write answer 403; `on_poll` runs on every read, as a person in
    the Admin console would act between two polls."""

    def __init__(self) -> None:
        super().__init__()
        self.groups: dict[str, str] = {}
        self.assignments: list[dict[str, Any]] = []
        self.refuse: set[str] = set()
        self.on_poll: Any = None

    def _refused(self, name: str) -> None:
        if name in self.refuse:
            raise ApiError(403, "PERMISSION_DENIED", "Not Authorized to access this resource/api")

    def customer_id(self) -> str:
        self._call("customer_id")
        return CUSTOMER

    def admin_roles(self, customer: str) -> list[dict[str, Any]]:
        self._call("admin_roles", customer)
        return [{"roleId": "91447453409034241", "roleName": "_GROUPS_ADMIN_ROLE", "isSystemRole": True},
                {"roleId": READER_ID, "roleName": identity.GROUPS_READER_ROLE, "isSystemRole": True}]

    def role_assignments(self, customer: str, role_id: str) -> list[dict[str, Any]]:
        self._call("role_assignments", customer, role_id)
        if self.on_poll:
            self.on_poll()
        return [dict(a) for a in self.assignments if a["roleId"] == role_id]

    def assign_role(self, customer: str, role_id: str, assigned_to: str) -> dict[str, Any]:
        self._call("assign_role", customer, role_id, assigned_to)
        self._refused("assign_role")
        found = {"roleId": role_id, "assignedTo": assigned_to, "scopeType": "CUSTOMER"}
        self.assignments.append(found)
        return found

    def group_name(self, project: str, email: str) -> str | None:
        self._call("group_name", project, email)
        if self.on_poll:
            self.on_poll()
        return self.groups.get(email)

    def create_group(self, project: str, parent: str, email: str, display_name: str) -> dict[str, Any]:
        self._call("create_group", project, parent, email, display_name)
        self._refused("create_group")
        self.groups[email] = f"groups/{len(self.groups) + 1}"
        return {"done": True}


@pytest.fixture
def pages(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every page a step opens, with what it says to do there; no browser."""
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(identity, "open_page", lambda url, *, why: opened.append((url, why)))
    return opened


def _seed(rig: Rig, **outputs: Any) -> None:
    rig.store.update(lambda s: s.outputs.update({"org_domain": DOMAIN, "installer": INSTALLER, **outputs}))


def test_groups_created_through_the_groups_api(tmp_path: Path, pages: list[tuple[str, str]]) -> None:
    """The admin and first-team groups do not exist: one optional choice names
    both, and `groups.create` makes them under customers/<id> read from the
    Directory API. No page opens; `admin_groups` is still only recorded by
    identity.admin-group, after its probe."""
    gcp = WorkspaceGcp()
    ask = Answers(identity_groups=["yes"])
    rig = Rig(tmp_path)
    _seed(rig)
    result = rig.engine([identity.GroupsStep(lambda: gcp, ask, lambda c: FakeApi())]).run()

    assert result.exit_code == ExitCode.OK, result.message
    assert len(ask.asked) == 1
    question = ask.asked[0][1]
    assert f"swarm-admins@{DOMAIN}" in question and f"swarm-team@{DOMAIN}" in question
    created = gcp.called("create_group")
    assert [(c[1], c[2]) for c in created] == [(f"customers/{CUSTOMER}", f"swarm-admins@{DOMAIN}"),
                                               (f"customers/{CUSTOMER}", f"swarm-team@{DOMAIN}")]
    assert all(c[0] == PROJECT for c in created)
    assert pages == []
    outputs = rig.store.load().outputs
    assert outputs["workspace_groups"] == {"admin": f"swarm-admins@{DOMAIN}", "team": f"swarm-team@{DOMAIN}"}
    assert "admin_groups" not in outputs

    # The admin group is then registered as today: probed, with the installer
    # (each group's owner) as the member, and asked nothing.
    api = FakeApi()
    result = rig.engine([identity.AdminGroupStep(lambda: gcp, Answers(), lambda c: api)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.store.load().outputs["admin_groups"] == [f"swarm-admins@{DOMAIN}"]
    assert api.probes[0] == [{"group": f"swarm-admins@{DOMAIN}", "member": INSTALLER}]


def test_groups_waited_for_when_the_person_lacks_the_right(tmp_path: Path, pages: list[tuple[str, str]]) -> None:
    """`groups.create` is refused: the Admin console's groups page opens with
    what to create there, and the step polls until both groups exist. Nobody
    presses Enter."""
    gcp = WorkspaceGcp()
    gcp.refuse.add("create_group")
    polls = {"n": 0}

    def person_creates() -> None:
        if pages:
            polls["n"] += 1
            if polls["n"] > 4:
                gcp.groups.update({f"swarm-admins@{DOMAIN}": "groups/a", f"swarm-team@{DOMAIN}": "groups/t"})

    gcp.on_poll = person_creates
    ask = Answers(identity_groups=["yes"])
    rig = Rig(tmp_path)
    _seed(rig)
    result = rig.engine([identity.GroupsStep(lambda: gcp, ask, lambda c: FakeApi())]).run()

    assert result.exit_code == ExitCode.OK, result.message
    assert [url for url, _ in pages] == [identity.GROUPS_PAGE]
    why = pages[0][1]
    assert f"swarm-admins@{DOMAIN}" in why and f"swarm-team@{DOMAIN}" in why and "continues by itself" in why
    assert "Enter" not in why
    assert len(ask.asked) == 1
    assert rig.store.load().outputs["workspace_groups"]["team"] == f"swarm-team@{DOMAIN}"


def test_groups_reader_role_assigned_after_typed_approval(tmp_path: Path, pages: list[tuple[str, str]]) -> None:
    """The predefined Groups Reader role is assigned to swarm-api's numeric
    unique id with `roleAssignments.insert`, only after the typed approval;
    anything else assigns nothing."""
    gcp = WorkspaceGcp()
    rig = Rig(tmp_path / "refused")
    _seed(rig)
    result = rig.engine([identity.GroupsReaderRoleStep(
        lambda: gcp, Answers(identity_groups_reader_role=["yes"]), lambda c: FakeApi())]).run()
    assert result.exit_code == ExitCode.FAIL
    assert not gcp.called("assign_role")

    ask = Answers(identity_groups_reader_role=[identity.APPROVE])
    rig = Rig(tmp_path / "approved")
    _seed(rig)
    result = rig.engine([identity.GroupsReaderRoleStep(lambda: gcp, ask, lambda c: FakeApi())]).run()
    assert result.exit_code == ExitCode.OK, result.message
    question = ask.asked[0][1]
    assert f"swarm-api@{PROJECT}.iam.gserviceaccount.com" in question and identity.APPROVE in question
    assert gcp.called("assign_role") == [(CUSTOMER, READER_ID, UNIQUE_ID)]
    assert pages == []
    assert rig.store.load().outputs["groups_auth"] == ADMIN_ROLE

    # identity.group-read then takes the recorded path and the first team's
    # group, and asks nothing.
    rig.store.update(lambda s: s.outputs.update(
        {"workspace_groups": {"admin": f"swarm-admins@{DOMAIN}", "team": f"swarm-team@{DOMAIN}"}}))
    api = FakeApi(ADMIN_ROLE)
    result = rig.engine([identity.GroupReadStep(lambda: gcp, Answers(), lambda c: api)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert api.probes[0] == [{"group": f"swarm-team@{DOMAIN}", "member": INSTALLER}]


def test_groups_reader_role_refused_then_polled(tmp_path: Path, pages: list[tuple[str, str]]) -> None:
    """The Directory API refuses the assignment: the Admin console's admin
    roles page opens naming swarm-api's account, and `roleAssignments.list`
    is polled until the assignment shows."""
    gcp = WorkspaceGcp()
    gcp.refuse.add("assign_role")
    polls = {"n": 0}

    def person_assigns() -> None:
        if pages:
            polls["n"] += 1
            if polls["n"] > 3 and not gcp.assignments:
                gcp.assignments.append({"roleId": READER_ID, "assignedTo": UNIQUE_ID, "scopeType": "CUSTOMER"})

    gcp.on_poll = person_assigns
    rig = Rig(tmp_path)
    _seed(rig)
    ask = Answers(identity_groups_reader_role=[identity.APPROVE])
    result = rig.engine([identity.GroupsReaderRoleStep(lambda: gcp, ask, lambda c: FakeApi())]).run()

    assert result.exit_code == ExitCode.OK, result.message
    assert [url for url, _ in pages] == [identity.ADMIN_ROLES_PAGE]
    assert f"swarm-api@{PROJECT}.iam.gserviceaccount.com" in pages[0][1] and "Groups Reader" in pages[0][1]
    assert len(gcp.called("role_assignments")) > 1
    assert rig.store.load().outputs["groups_auth"] == ADMIN_ROLE


class FakeReapply:
    """ENGINE-INPUTS' stage-8 re-apply as the engine sees it: a stage-8
    Terraform pass step. Applying it writes swarm-api's ALLOWED_DOMAINS and
    qa-console's identity."""

    stage = 8
    kind = "engine"
    satisfies: tuple[str, ...] = ()
    pass_name = "infra-identity"

    def __init__(self, gcp: QaFakeGcp) -> None:
        self.id = "infra.stage-8-reapply"
        self.gcp = gcp
        self.applied = 0

    def check(self, ctx: Any) -> Observation:
        return Observation(MET if self.applied else UNMET, "")

    verify = check

    def plan(self, ctx: Any) -> Plan:
        return Plan("re-apply infra with the groups")

    def apply(self, ctx: Any, plan: Plan) -> None:
        self.applied += 1
        self.gcp.allowed = DOMAIN
        self.gcp.accounts.add(f"qa-console@{PROJECT}.iam.gserviceaccount.com")

    def explain(self) -> str:
        return ""


def test_read_only_checks_wait_for_the_stage_8_reapply(tmp_path: Path) -> None:
    """Before the stage-8 re-apply has run, qa-console and allowed-domains
    are `waiting` for it, never failing and never "run swarm install again";
    in the engine their apply runs that re-apply once, and they then pass."""
    gcp = QaFakeGcp()
    gcp.allowed = "another.org"
    gcp.accounts.clear()
    reapply = FakeReapply(gcp)
    qa, domains = identity.QaConsoleStep(lambda: gcp), identity.AllowedDomainsStep(lambda: gcp)

    ctx = ctx_for(outputs={"org_domain": DOMAIN, "admin_groups": [ADMINS]})
    ctx.engine = type("E", (), {"steps": [qa, domains, reapply]})()
    for step in (qa, domains):
        observed = step.check(ctx)
        assert observed.status == WAITING, observed
        assert "waiting for the stage-8 re-apply" in observed.detail and reapply.id in observed.detail
        assert "run swarm install again" not in observed.detail

    rig = Rig(tmp_path)
    rig.store.update(lambda s: (setattr(s, "region", "us-central1"),
                                s.outputs.update({"org_domain": DOMAIN, "admin_groups": [ADMINS]})))
    result = rig.engine([qa, domains, reapply]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert reapply.applied == 1
    state = rig.store.load()
    assert state.done(qa.id) and state.done(domains.id) and state.done(reapply.id)
    assert state.outputs["allowed_domains"] == [DOMAIN]


def test_read_only_checks_after_the_reapply_check_and_never_say_run_again() -> None:
    """After the re-apply, both check what it wrote: met when right, and a
    wrong value is unmet with a reason, never "run swarm install again"."""
    gcp = QaFakeGcp()
    reapply = FakeReapply(gcp)
    ctx = ctx_for(outputs={"org_domain": DOMAIN})
    ctx.engine = type("E", (), {"steps": [reapply]})()
    ctx.state.steps[reapply.id] = StepRecord(state="done", at=0.0, by=INSTALLER)
    qa, domains = identity.QaConsoleStep(lambda: gcp), identity.AllowedDomainsStep(lambda: gcp)
    assert qa.check(ctx).status == MET
    assert domains.check(ctx).status == MET  # org_domain alone names the organisation

    gcp.allowed = "another.org"
    observed = domains.check(ctx)
    assert observed.status == UNMET and DOMAIN in observed.detail
    with pytest.raises(RuntimeError) as raised:
        domains.apply(ctx, domains.plan(ctx))
    assert "run swarm install again" not in str(raised.value)
    assert reapply.applied == 0  # done already: not run a second time
