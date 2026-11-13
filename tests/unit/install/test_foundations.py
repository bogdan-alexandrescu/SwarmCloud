"""Stage 1, project foundations (DAYONE-ENGINE-2; lessons 3, 4, 6, 7).

On a fake Google Cloud with no credentials: the state bucket is created with
bootstrap.sh's settings and then the local install state is uploaded into it;
the default compute service account loses Editor; the APIs enabled are exactly
the manifest's one list; an IAM write-rate limit is retried, never reported as
a denial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from swarm_install import sizing
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.manifest import Entry, Manifest
from swarm_install.manifest import load as load_manifest
from swarm_install.state import STATE_OBJECT, InstallState, StepRecord, state_bucket
from swarm_install.steps import edge, foundations, preflight
from swarm_install.steps.preflight import ApiError

from .test_preflight import (ACCOUNT, ORG, NUMBER, PROJECT, Answers, FakeGcp, Rig, answers, ctx_for, stage0,
                             stage1)

COMPUTE_SA = f"serviceAccount:{NUMBER}-compute@developer.gserviceaccount.com"


def test_state_bucket_created_with_settings_then_state_uploaded(tmp_path: Path) -> None:
    """Lesson 7: the bucket is created once with versioning, uniform access and
    public access prevention, in the confirmed region; then the state kept
    locally through stages 0 and 1 is uploaded into it, and it is authoritative."""
    log: list = []
    gcp = FakeGcp(log)
    rig = Rig(tmp_path, local_only=True)
    earlier = InstallState(project=PROJECT, environment="dev", region="europe-west1",
                           steps={"preflight.region": StepRecord(state="done", at=1.0, by="admin@example.com")})
    (tmp_path / "state.json").write_bytes(earlier.to_bytes())

    step = foundations.StateBucketStep(lambda: gcp, Answers())
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message

    name = state_bucket(PROJECT)
    created = json.loads(gcp.called("create_bucket")[0][1])
    assert created["name"] == name
    assert created["location"] == "europe-west1"
    assert created["versioning"] == {"enabled": True}
    assert created["iamConfiguration"] == {"uniformBucketLevelAccess": {"enabled": True},
                                           "publicAccessPrevention": "enforced"}
    assert created["labels"]["managed-by"] == "swarm-bootstrap"

    uploaded = gcp.stores[name].read(STATE_OBJECT)
    assert uploaded is not None
    state = InstallState.from_bytes(uploaded[0])
    assert state.steps["preflight.region"].state == "done"  # the local state, carried over
    assert state.steps["foundations.state-bucket"].state == "done"
    calls = [n for n, _ in gcp.calls]
    assert calls.index("create_bucket") < calls.index("object_store")
    assert rig.store.bucket is gcp.stores[name]


def test_default_compute_editor_removed(tmp_path: Path) -> None:
    """Lesson 6 (#1020): roles/editor is removed from the default compute
    service account after a typed confirmation; every other member stays."""
    gcp = FakeGcp()
    gcp.iam["bindings"] = [
        {"role": "roles/editor", "members": [COMPUTE_SA, "user:owner@example.com"]},
        {"role": "roles/viewer", "members": [COMPUTE_SA]},
    ]
    refused = foundations.DefaultComputeEditorStep(
        lambda: gcp, Answers(foundations_default_compute_editor=["yes"] * preflight.CONFIRM_TRIES)
    )
    ctx = ctx_for(outputs={"project_number": NUMBER}, dry_run=False)
    assert refused.check(ctx).status == UNMET
    with pytest.raises(ValueError, match="does not match 'remove-editor'.*nothing was changed"):
        refused.apply(ctx, refused.plan(ctx))
    assert gcp.mutations == []

    step = foundations.DefaultComputeEditorStep(
        lambda: gcp, Answers(foundations_default_compute_editor=["remove-editor"])
    )
    result = Rig(tmp_path).engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert gcp.iam["bindings"] == [
        {"role": "roles/editor", "members": ["user:owner@example.com"]},
        {"role": "roles/viewer", "members": [COMPUTE_SA]},
    ]
    assert step.check(ctx).status == MET


def test_one_api_list_from_manifest() -> None:
    """Lesson 4: one canonical API list, the manifest's core api.* entries;
    containerscanning is never enabled; add-on APIs are not core."""
    manifest = load_manifest()
    step = foundations.ApisStep(lambda: FakeGcp(), Answers(), manifest)
    expected = {
        e.id: e.probe.model_extra["name"] for e in manifest.entries
        if e.id.startswith("api.") and e.scope == "core" and e.probe is not None
        and e.probe.type == "service_enabled"
    }
    assert step.apis == expected
    assert "policytroubleshooter.googleapis.com" in step.apis.values()  # lesson 48
    assert "containerscanning.googleapis.com" not in step.apis.values()
    assert "cloudbuild.googleapis.com" not in step.apis.values()  # addon:ci
    assert "api.containerscanning-NOT-enabled" in step.satisfies

    gcp = FakeGcp()
    gcp.enabled = {"storage.googleapis.com"}
    step = foundations.ApisStep(lambda: gcp, Answers(), manifest)
    ctx = ctx_for(dry_run=False)
    step.apply(ctx, step.plan(ctx))
    (_, names), = gcp.called("enable_services")
    assert set(names) == set(expected.values()) - {"storage.googleapis.com"}
    assert step.check(ctx).status == MET

    small = Manifest(entries=[Entry(id="api.run", kind="gcp-api", scope="core", stage=1, provider="engine",
                                    probe={"type": "service_enabled", "name": "run.googleapis.com"})],
                     ignored={}, env={}, sources={})
    assert foundations.ApisStep(lambda: gcp, Answers(), small).apis == {"api.run": "run.googleapis.com"}

    gcp.enabled.add("containerscanning.googleapis.com")
    assert step.check(ctx).status == UNMET


def test_iam_write_rate_limit_retried_not_reported_as_denial(tmp_path: Path) -> None:
    """Lesson 3: back-to-back IAM writes hit a write-rate limit that reads as a
    quota error; it is retried and the step succeeds. A real denial is not retried."""
    gcp = FakeGcp()
    gcp.iam["bindings"] = [{"role": "roles/editor", "members": [COMPUTE_SA]}]
    gcp.set_iam_failures = [ApiError(429, "RESOURCE_EXHAUSTED", "Quota exceeded for quota metric 'Write requests'"),
                            ApiError(429, "RESOURCE_EXHAUSTED", "Quota exceeded for quota metric 'Write requests'")]
    rig = Rig(tmp_path)
    step = foundations.DefaultComputeEditorStep(
        lambda: gcp, Answers(foundations_default_compute_editor=["remove-editor"])
    )
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(gcp.called("set_iam_policy")) == 3
    assert rig.clock.sleeps[:2] == [2.0, 4.0]
    assert not rig.of_kind("step_failed")
    assert "denied" not in rig.out.getvalue().lower() and "PERMISSION_DENIED" not in rig.out.getvalue()
    assert gcp.iam["bindings"] == []

    denied = FakeGcp()
    denied.iam["bindings"] = [{"role": "roles/editor", "members": [COMPUTE_SA]}]
    denied.set_iam_failures = [ApiError(403, "PERMISSION_DENIED", "caller lacks resourcemanager.projects.setIamPolicy")]
    rig = Rig(tmp_path / "denied")
    step = foundations.DefaultComputeEditorStep(
        lambda: denied, Answers(foundations_default_compute_editor=["remove-editor"])
    )
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.FAIL
    assert len(denied.called("set_iam_policy")) == 1
    assert "PERMISSION_DENIED" in result.message


def test_dry_run_on_an_empty_project_writes_nothing_and_knows_every_check(tmp_path: Path) -> None:
    """Design §2.4: `--dry-run` against an empty project runs stage 0, checks
    stages 1 and 2 and prints their plans. Before the project exists every
    check is `unmet`, not `unknown` (nothing failed to run), and nothing is written."""
    gcp = FakeGcp(exists=False, billed=False)
    ask = answers(preflight_rights=[ORG])
    rig = Rig(tmp_path)
    steps = stage0(gcp, ask) + stage1(gcp, ask) + edge.build_steps(lambda: gcp, ask)
    result = rig.engine(steps, manifest=load_manifest(), dry_run=True).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert gcp.mutations == []
    assert [e["step"] for e in rig.of_kind("step_check") if e["state"] == "unknown"] == []
    planned = {e["step"] for e in rig.of_kind("step_plan")}
    # The brand and the Desktop client are waits after the hand-off (issue #2964).
    assert {"foundations.project", "foundations.apis", "foundations.state-bucket",
            "edge.client-secret-container"} <= planned


# -- DAYONE-PROJECT-CREATE: decision 6, the project created through the APIs ---


class FakeProjectApi:
    """`foundations.ProjectApi` on the fake Google Cloud: `projects.create`
    answers with an operation that is done on its first poll, then the project
    appears; `updateBillingInfo` links the account. Every write is a mutation."""

    def __init__(self, gcp: FakeGcp, *, link_error: ApiError | None = None,
                 create_error: ApiError | None = None, taken: tuple[str, ...] = (),
                 terms_accepted_on_call: int | None = 1, link_failures: list[ApiError] | None = None) -> None:
        self.gcp = gcp
        self.link_error = link_error
        self.create_error = create_error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.pending: dict[str, str] = {}
        self.granted_on_poll: int | None = None
        #: Project ids are global: these belong to someone the caller cannot
        #: see, so `projects:search` finds none of them and create answers 409.
        self.taken = set(taken)
        #: The account accepts the Google Cloud Terms of Service in the console
        #: before this create call (never when None); until then Google
        #: refuses every create with 400 FAILED_PRECONDITION, as it does.
        self.terms_accepted_on_call = terms_accepted_on_call
        #: Errors updateBillingInfo answers first, one per call.
        self.link_failures = list(link_failures or [])

    def _write(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        self.gcp.log.append(("gcp", name))
        self.gcp.mutations.append((name, args))

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]

    def create_project(self, project, parent, labels):
        self._write("create_project", project, parent, tuple(sorted(labels.items())))
        accepted_on = self.terms_accepted_on_call
        if accepted_on is None or len(self.called("create_project")) < accepted_on:
            raise ApiError(400, "FAILED_PRECONDITION", "Callers must accept Terms of Service")
        if self.create_error is not None:
            raise self.create_error
        if project in self.taken:
            raise ApiError(409, "ALREADY_EXISTS", "requested entity already exists")
        self.pending["operations/cp.1"] = project
        self.gcp.projects[project] = {"name": f"projects/{NUMBER}", "state": "ACTIVE", "parent": parent}
        return {"name": "operations/cp.1", "done": False}

    def can_create_projects(self, parent):
        """testIamPermissions on the parent: the grant arrives on poll
        `granted_on_poll` (never when it is None), and creating then works."""
        self.calls.append(("can_create_projects", (parent,)))
        polls = len(self.called("can_create_projects"))
        if self.granted_on_poll is not None and polls >= self.granted_on_poll:
            self.create_error = None
        return self.create_error is None

    def operation(self, name):
        self.calls.append(("operation", (name,)))
        return {"name": name, "done": True, "response": {"projectId": self.pending[name]}}

    def link_billing(self, project, account):
        self._write("link_billing", project, account)
        if self.link_failures:
            raise self.link_failures.pop(0)
        if self.link_error is not None:
            raise self.link_error
        self.gcp.billing = {"billingEnabled": True, "billingAccountName": f"billingAccounts/{account}"}
        return dict(self.gcp.billing)

    def disable_service(self, project, service):
        self._write("disable_service", project, service)
        self.gcp.enabled.discard(service)
        return {}


def _recorded(tmp_path: Path) -> Rig:
    """A rig whose state holds what stage 0 records: the parent and the billing account."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    rig = Rig(tmp_path, local_only=True)
    earlier = InstallState(project=PROJECT, environment="dev", region="us-central1",
                           outputs={"parent": ORG, "billing_account": ACCOUNT})
    (tmp_path / "state.json").write_bytes(earlier.to_bytes())
    return rig


def _project_and_budget(gcp: FakeGcp, ask: Answers, api: FakeProjectApi) -> list[Any]:
    return [foundations.ProjectStep(lambda: gcp, ask, api), foundations.BudgetStep(lambda: gcp, ask)]


def test_project_created_and_billing_linked_after_one_typed_approval(tmp_path: Path, monkeypatch) -> None:
    """Decision 6: when the project does not exist, ONE approval shows the
    project, its parent, the billing account, the budget and the monthly cost
    estimate; the project id typed approves it (SWARM_ASSUME_YES never does).
    Then projects.create (its operation polled) and updateBillingInfo, and the
    budget is created with the amount chosen."""
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    log: list = []
    gcp = FakeGcp(log, exists=False, billed=False)
    api = FakeProjectApi(gcp)
    ask = Answers(log, foundations_project=["$1,500", PROJECT])
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message

    assert [step for step, _ in ask.asked] == ["foundations.project", "foundations.project"]
    approval = ask.asked[1][1]
    assert f"Type the project id ({PROJECT})" in approval
    for shown in (PROJECT, ORG, f"billingAccounts/{ACCOUNT}", "1500 USD a month",
                  f"about {foundations.monthly_estimate()} USD a month"):
        assert shown in approval, shown
    for what, usd in foundations.MONTHLY_FLOOR_USD:
        assert f"{usd} USD  {what}" in approval  # the estimate states its basis

    assert api.called("create_project") == [(PROJECT, ORG, (("environment", "dev"), ("managed-by", "swarm-install")))]
    assert api.called("operation") == [("operations/cp.1",)]
    assert foundations.OPERATION_INTERVAL_S in rig.clock.sleeps
    assert api.called("link_billing") == [(PROJECT, ACCOUNT)]
    approved = max(i for i, item in enumerate(log) if item[:2] == ("ask", "foundations.project"))
    first_write = min(i for i, item in enumerate(log) if item[0] == "gcp" and item[1] == "create_project")
    assert approved < first_write
    names = [n for n, _ in api.calls]
    assert names.index("create_project") < names.index("link_billing")

    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "1500"
    assert budget["budgetFilter"] == {"projects": [f"projects/{NUMBER}"]}
    state = rig.store.load()
    assert state.outputs["budget_amount"] == 1500
    assert state.outputs["project_number"] == NUMBER and state.outputs["billing_account"] == ACCOUNT
    assert state.steps["foundations.project"].state == "done"


def test_project_that_exists_and_is_linked_is_left_alone(tmp_path: Path) -> None:
    """Decision 6, item 4: an existing, linked project is reported and left
    alone: no approval and no write. There was no approval to choose the
    budget at, so foundations.budget asks the amount itself (owner pick,
    2026-10-30), and the budget is created with the amount typed."""
    gcp = FakeGcp()
    api = FakeProjectApi(gcp)
    rig = _recorded(tmp_path)
    ask = Answers(foundations_budget=["$2,000"])
    step = foundations.ProjectStep(lambda: gcp, ask, api)
    result = rig.engine([step, foundations.BudgetStep(lambda: gcp, ask)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert [step_id for step_id, _ in ask.asked] == ["foundations.budget"]
    assert "Monthly budget" in ask.asked[0][1]
    assert api.calls == []
    assert not rig.of_kind("step_apply", "foundations.project")
    assert rig.of_kind("step_check", "foundations.project")[0]["state"] == MET
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "2000"
    assert rig.store.load().outputs["budget_amount"] == 2000


def test_budget_asked_on_an_existing_project_keeps_the_default_on_enter(tmp_path: Path) -> None:
    """Enter at foundations.budget's question keeps the default; an answer that
    is not a whole dollar amount refuses and creates no budget."""
    gcp = FakeGcp()
    rig = _recorded(tmp_path)
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=[""]))]).run()
    assert result.exit_code == ExitCode.OK, result.message
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == str(foundations.DEFAULT_BUDGET_USD)

    gcp = FakeGcp()
    rig = _recorded(tmp_path / "second")
    wrong = ["lots"] * preflight.CONFIRM_TRIES
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=wrong))]).run()
    assert result.exit_code == ExitCode.FAIL
    assert gcp.budget_list == []
    assert "budget_amount" not in rig.store.load().outputs


def test_a_mistyped_budget_is_asked_again(tmp_path: Path) -> None:
    """Epic comment 61: an answer that is not a whole dollar amount is asked
    again, saying why, instead of ending the run."""
    gcp = FakeGcp()
    rig = _recorded(tmp_path)
    ask = Answers(foundations_budget=["lots", "$1,200"])
    result = rig.engine([foundations.BudgetStep(lambda: gcp, ask)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(ask.asked) == 2 and "'lots'" in ask.asked[1][1] and "Monthly budget" in ask.asked[1][1]
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "1200"


def test_wrong_confirmation_creates_and_links_nothing(tmp_path: Path, monkeypatch) -> None:
    """A typed answer that is not the project id refuses, even with
    SWARM_ASSUME_YES set; so does a budget that is not a whole dollar amount.
    Neither writes anything or records the budget."""
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    refused = ["", "", *["yes"] * (preflight.CONFIRM_TRIES - 1)]  # the budget's Enter, then every try wrong
    for answers_given, said in ((refused, "nothing was created or linked"), (["a lot"] * preflight.CONFIRM_TRIES, "whole number of US dollars")):
        gcp = FakeGcp(exists=False, billed=False)
        api = FakeProjectApi(gcp)
        rig = _recorded(tmp_path / said.replace(" ", "-"))
        result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=answers_given), api)).run()
        assert result.exit_code == ExitCode.FAIL
        assert result.step == "foundations.project"
        assert said in result.message
        assert api.calls == [] and gcp.mutations == []
        assert "budget_amount" not in rig.store.load().outputs


def test_billing_link_failure_leaves_the_project_and_the_rerun_links_it(tmp_path: Path, monkeypatch) -> None:
    """When updateBillingInfo is refused the project's billing page opens and
    is polled; when nobody links it there the step fails naming the right it
    needs and saying the project was created; the next run does not create it
    again: it only links billing, after its own approval."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    denied = ApiError(403, "PERMISSION_DENIED", "caller lacks billing.resourceAssociations.create")
    api = FakeProjectApi(gcp, link_error=denied)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.FAIL and result.step == "foundations.project"
    assert "billing.resourceAssociations.create" in result.message
    assert f"roles/billing.user on billingAccounts/{ACCOUNT}" in result.message
    assert f"project {PROJECT} was created and is left in place" in result.message
    assert PROJECT in gcp.projects and gcp.budget_list == []
    assert [url for url, _ in opened] == [foundations.billing_page(PROJECT)]
    assert rig.of_kind("wait_gave_up", "foundations.project:billing")

    api.link_error = None
    ask = Answers(foundations_project=["", PROJECT])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(api.called("create_project")) == 1
    assert "link billing for" in ask.asked[1][1] and "parent" not in ask.asked[1][1]
    assert api.called("link_billing") == [(PROJECT, ACCOUNT), (PROJECT, ACCOUNT)]
    (budget,) = gcp.budget_list  # Enter kept the default
    assert budget["amount"]["specifiedAmount"]["units"] == str(foundations.DEFAULT_BUDGET_USD)


def test_a_billing_link_refused_for_another_reason_opens_the_billing_page(tmp_path: Path, monkeypatch) -> None:
    """Epic comment 28: a refusal that is not a 403 (the account's
    linked-project limit) would refuse every rerun with the same account, so
    the project's billing page opens, where another account can be linked,
    and the project's billing is polled, as for a 403."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    limit = ApiError(400, "FAILED_PRECONDITION", "the billing account has reached its linked-project limit")
    api = FakeProjectApi(gcp, link_error=limit)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.FAIL and result.step == "foundations.project"
    assert [url for url, _ in opened] == [foundations.billing_page(PROJECT)]
    assert "linked-project limit" in opened[0][1] and "another open billing account" in opened[0][1]
    assert rig.of_kind("wait_gave_up", "foundations.project:billing")
    assert f"project {PROJECT} was created and is left in place" in result.message


def test_the_cost_estimate_cites_only_public_pages() -> None:
    """Epic comment 78: the approval screen shows each line of the estimate to
    the person approving, who has no access to the repository's planning
    documents; the cluster fee cites GKE's public pricing page."""
    lines = " ".join(what for what, _ in foundations.MONTHLY_FLOOR_USD)
    assert "BUILD_PROMPT" not in lines and "docs/" not in lines
    assert "https://cloud.google.com/kubernetes-engine/pricing" in lines


def test_project_id_taken_says_so(tmp_path: Path) -> None:
    """Project ids are global: a 409 on create is someone else's project, said
    as that, and another id is asked; when none is typed nothing is created or
    linked, and the message says how to go on."""
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, taken=(PROJECT,))
    rig = _recorded(tmp_path)
    ask = Answers(foundations_project=["", PROJECT, ""])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.FAIL
    assert f"project id {PROJECT} is taken" in result.message
    assert "type another id when it asks" in result.message
    assert f"Project id {PROJECT} is taken" in ask.asked[2][1]
    assert api.called("link_billing") == [] and gcp.projects == {}
    assert rig.store.project == PROJECT


# -- DAYONE-FOUNDATIONS-4: audit 4a F1, F3, F4 ------------------------------------


def _stage0_answers(tmp_path: Path) -> Rig:
    """`_recorded`, plus the answers stage 0 records that a new project id
    must not lose (audit 4a F1: the organisation, inputs, region, rights,
    org-domain and sizing)."""
    rig = _recorded(tmp_path)
    state = InstallState.from_bytes((tmp_path / "state.json").read_bytes())
    state.hostname = "swarm.acme.example"
    state.outputs.update({"github_org": "acme", "org_domain": "acme.example", "installer": "admin@acme.example",
                          "sizing": {"mode": "individual", "members": 1, "budget_usd": 300}})
    state.steps["preflight.inputs"] = StepRecord(state="done", at=1.0, by="admin@acme.example")
    (tmp_path / "state.json").write_bytes(state.to_bytes())
    return rig


def test_taken_project_id_asks_another_in_the_same_run_and_keeps_every_answer(tmp_path: Path) -> None:
    """Audit 4a F1: another owner's project id is invisible to projects:search,
    so it is only found taken when projects.create answers 409. The engine
    asks for another id in the same run (a malformed one is asked again),
    moves the local state, with every stage-0 answer, to the new id's file,
    and creates, links and budgets the new project; nothing is asked twice."""
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, taken=(PROJECT,))
    rig = _stage0_answers(tmp_path)
    new = "p-test-two"
    ask = Answers(foundations_project=[PROJECT, "Not_An_Id", new])  # the sizing's budget is not asked again
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message

    assert [step for step, _ in ask.asked] == ["foundations.project"] * 3
    assert "'Not_An_Id' is not a Google Cloud project id" in ask.asked[2][1]
    assert [args[0] for args in api.called("create_project")] == [PROJECT, new]
    assert api.called("link_billing") == [(new, ACCOUNT)]
    (budget,) = gcp.budget_list
    assert budget["displayName"] == f"swarm-{new}"
    assert budget["amount"]["specifiedAmount"]["units"] == "300"  # the sizing's budget, kept

    assert rig.store.project == new
    assert rig.store.local == tmp_path / f"{new}.json"
    assert not (tmp_path / "state.json").exists()
    moved = InstallState.from_bytes((tmp_path / f"{new}.json").read_bytes())
    assert moved.project == new and moved.hostname == "swarm.acme.example"
    for key in ("github_org", "org_domain", "installer", "sizing", "parent", "billing_account"):
        assert key in moved.outputs, key
    assert moved.steps["preflight.inputs"].state == "done"
    assert moved.steps["foundations.project"].state == "done"
    assert moved.steps["foundations.budget"].state == "done"
    assert gcp.quota_project == new  # the billing-budget calls name the install's project


def test_taken_project_id_replaced_by_an_existing_project_uses_it(tmp_path: Path) -> None:
    """The id typed after a 409 may be a project the person already has: it is
    used as found (its billing linked), never created; an id already holding
    another install's local state is refused and asked again."""
    gcp = FakeGcp(exists=False, billed=False)
    mine, busy = "p-mine-one", "p-busy-one"
    gcp.projects[mine] = {"name": f"projects/{NUMBER}", "state": "ACTIVE", "parent": ORG}
    api = FakeProjectApi(gcp, taken=(PROJECT,))
    rig = _stage0_answers(tmp_path)
    (tmp_path / f"{busy}.json").write_bytes(InstallState(project=busy, environment="dev").to_bytes())
    ask = Answers(foundations_project=["", PROJECT, busy, mine])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert f"an install of {busy} is already recorded" in ask.asked[3][1]
    assert [args[0] for args in api.called("create_project")] == [PROJECT]
    assert api.called("link_billing") == [(mine, ACCOUNT)]
    assert rig.store.project == mine
    assert InstallState.from_bytes((tmp_path / f"{busy}.json").read_bytes()).project == busy  # untouched


def test_terms_of_service_not_accepted_opens_the_console_and_creates_once_accepted(tmp_path: Path,
                                                                                   monkeypatch) -> None:
    """Audit 4a F3: an account that never accepted the Google Cloud Terms of
    Service gets 400 FAILED_PRECONDITION from projects.create. The engine
    opens the Cloud console, which asks for them, says why, and tries the
    create again until Google takes it; it never re-raises the raw 400."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, terms_accepted_on_call=4)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    ((url, why),) = opened
    assert url == "https://console.cloud.google.com/"
    assert "Terms of Service" in why
    assert len(api.called("create_project")) == 4
    assert rig.of_kind("wait_countdown", "foundations.project:terms-of-service")
    assert PROJECT in gcp.projects and api.called("link_billing") == [(PROJECT, ACCOUNT)]


def test_terms_of_service_never_accepted_pauses_the_run(tmp_path: Path, monkeypatch) -> None:
    """Past the wait's bound the run pauses (exit 4) naming the Terms of
    Service; `swarm install` resumes there. Nothing is created or linked."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, terms_accepted_on_call=None)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert "Terms of Service" in result.message
    assert [url for url, _ in opened] == [foundations.TERMS_PAGE]
    assert gcp.projects == {} and api.called("link_billing") == []


class _Answer:
    """One HTTP answer, as `requests` gives it to `Gcp._call`."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self.reason = "Forbidden"
        self._body = body
        self.content = json.dumps(body).encode()

    def json(self) -> dict[str, Any]:
        return self._body


def _google_says(status: int, error: dict[str, Any]) -> ApiError:
    """The ApiError the real `Gcp._call` raises for Google's error body: the
    fakes raise exactly what the REST client makes of the real answer."""
    session = type("Session", (), {"request": lambda self, *a, **k: _Answer(status, {"error": error})})()
    with pytest.raises(ApiError) as raised:
        preflight.Gcp(session)._call("POST", "https://billingbudgets.googleapis.com/v1/billingAccounts/x/budgets")
    return raised.value


def _service_disabled(api: str = "Cloud Billing Budget API") -> ApiError:
    """Google's answer for minutes after `services.batchEnable` (audit 4a F4):
    a 403 whose status is PERMISSION_DENIED and whose ErrorInfo is SERVICE_DISABLED."""
    return _google_says(403, {
        "code": 403, "status": "PERMISSION_DENIED",
        "message": f"{api} has not been used in project {NUMBER} before or it is disabled. Enable it by visiting "
                   "https://console.developers.google.com/apis/api/billingbudgets.googleapis.com/overview then "
                   "retry. If you enabled this API recently, wait a few minutes for the action to propagate to our "
                   "systems and retry.",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                     "domain": "googleapis.com"}],
    })


def _iam_denied() -> ApiError:
    return _google_says(403, {"code": 403, "status": "PERMISSION_DENIED",
                              "message": "The caller does not have permission"})


def test_service_disabled_is_told_apart_from_an_iam_refusal() -> None:
    """Both are 403 PERMISSION_DENIED as `_call` builds them; only the
    message tells the API still propagating from a missing right."""
    assert foundations.service_disabled(_service_disabled())
    assert not foundations.service_disabled(_iam_denied())
    assert _iam_denied().denied and _service_disabled().denied  # why `denied` alone misroutes it
    assert not foundations.service_disabled(ApiError(429, "RESOURCE_EXHAUSTED", "it is disabled"))


def test_budget_api_still_propagating_is_waited_out_not_sent_to_the_budgets_page(tmp_path: Path,
                                                                                monkeypatch) -> None:
    """Audit 4a F4: right after stage 1 enables the Budgets API, budgets.create
    answers SERVICE_DISABLED for minutes. That is a wait: the create is tried
    again until it works, and the Budgets page is never opened."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp()
    real = gcp.create_budget
    tries = {"n": 0}

    def create_budget(account, body):
        tries["n"] += 1
        if tries["n"] <= 3:
            raise _service_disabled()
        return real(account, body)

    monkeypatch.setattr(gcp, "create_budget", create_budget)
    rig = _recorded(tmp_path)
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=["900"]))]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert opened == []
    assert tries["n"] == 4
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "900"
    assert rig.of_kind("wait_countdown", "foundations.budget:api-propagation")


def test_budget_api_never_propagating_pauses_without_opening_a_page(tmp_path: Path, monkeypatch) -> None:
    opened = _pages(monkeypatch)
    gcp = FakeGcp()

    def create_budget(account, body):
        raise _service_disabled()

    monkeypatch.setattr(gcp, "create_budget", create_budget)
    rig = _recorded(tmp_path)
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=[""]))]).run()
    assert result.exit_code == ExitCode.PAUSED, result.message
    assert opened == [] and gcp.budget_list == []
    assert rig.of_kind("wait_gave_up", "foundations.budget:api-propagation")


def test_billing_link_while_the_api_propagates_is_waited_out(tmp_path: Path, monkeypatch) -> None:
    """Audit 4a F4, item 4: the same distinction in the billing link. A
    SERVICE_DISABLED answer is retried and the billing page is not opened."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, link_failures=[_service_disabled("Cloud Billing API")] * 2)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert opened == []
    assert api.called("link_billing") == [(PROJECT, ACCOUNT)] * 3


def test_containerscanning_disabled_through_service_usage(tmp_path: Path) -> None:
    """containerscanning is disabled through the Service Usage API, never by a
    printed command, and the step then sees every API enabled."""
    manifest = load_manifest()
    gcp = FakeGcp()
    gcp.enabled = set(foundations.core_apis(manifest).values()) | {foundations.NEVER_ENABLED}
    api = FakeProjectApi(gcp)
    step = foundations.ApisStep(lambda: gcp, Answers(), manifest, api)
    ctx = ctx_for(dry_run=False)
    assert foundations.NEVER_ENABLED in " ".join(step.plan(ctx).actions)
    result = Rig(tmp_path).engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert api.called("disable_service") == [(PROJECT, foundations.NEVER_ENABLED)]
    assert gcp.called("enable_services") == []
    assert step.check(ctx).status == MET


# -- DAYONE-FOUNDATIONS-2: the sizing's budget, and the pages stage 1 opens ------


def _pages(monkeypatch) -> list[tuple[str, str]]:
    """Every page stage 1 opens, as (url, why); no browser is started."""
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(foundations, "open_page", lambda url, *, why: opened.append((url, why)))
    return opened


def _sized(tmp_path: Path, record: dict[str, Any]) -> Rig:
    """`_recorded`, plus the sizing stage 0 recorded (lane SIZING-ASK)."""
    rig = _recorded(tmp_path)
    state = InstallState.from_bytes((tmp_path / "state.json").read_bytes())
    state.outputs["sizing"] = record
    (tmp_path / "state.json").write_bytes(state.to_bytes())
    return rig


def test_budget_question_defaults_to_the_sizing_proposal(tmp_path: Path) -> None:
    """The sizing's budget_usd, accepted in stage 0, is not asked again at the
    approval: the approval shows it and the budget is created with it (epic
    comment 80); foundations.budget, on a project that needed no approval,
    offers the same amount."""
    proposal = dict(sizing.propose(sizing.TEAM, 20))
    proposal["budget_usd"] = 1700
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp)
    ask = Answers(foundations_project=[PROJECT])
    rig = _sized(tmp_path / "new", proposal)
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert not any("Monthly budget" in question for _, question in ask.asked), ask.asked
    assert len(ask.asked) == 1 and "1700 USD a month" in ask.asked[0][1]
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "1700"
    assert rig.store.load().outputs["budget_amount"] == 1700

    gcp = FakeGcp()
    ask = Answers(foundations_budget=[""])
    rig = _sized(tmp_path / "existing", proposal)
    step = foundations.BudgetStep(lambda: gcp, ask)
    assert "Enter keeps 1700 USD a month" in step.plan(ctx_for(outputs=rig.store.load().outputs)).summary
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert ask.defaults == ["1700"]
    (budget,) = gcp.budget_list
    assert budget["amount"]["specifiedAmount"]["units"] == "1700"


def test_budget_question_keeps_its_own_default_without_a_sizing(tmp_path: Path) -> None:
    """No sizing recorded (or one with no usable budget_usd): both questions
    offer DEFAULT_BUDGET_USD, as before."""
    for name, record in (("none", None), ("no-budget", {"mode": sizing.INDIVIDUAL, "budget_usd": 0})):
        gcp = FakeGcp(exists=False, billed=False)
        ask = Answers(foundations_project=["", PROJECT])
        rig = _recorded(tmp_path / name) if record is None else _sized(tmp_path / name, record)
        result = rig.engine(_project_and_budget(gcp, ask, FakeProjectApi(gcp))).run()
        assert result.exit_code == ExitCode.OK, result.message
        assert ask.defaults[0] == str(foundations.DEFAULT_BUDGET_USD)
        (budget,) = gcp.budget_list
        assert budget["amount"]["specifiedAmount"]["units"] == str(foundations.DEFAULT_BUDGET_USD)

        gcp = FakeGcp()
        ask = Answers(foundations_budget=[""])
        rig = _recorded(tmp_path / f"{name}-existing") if record is None else _sized(
            tmp_path / f"{name}-existing", record)
        assert rig.engine([foundations.BudgetStep(lambda: gcp, ask)]).run().exit_code == ExitCode.OK
        assert ask.defaults == [str(foundations.DEFAULT_BUDGET_USD)]


def test_project_create_denied_opens_the_parents_iam_page_and_polls_the_grant(tmp_path: Path,
                                                                              monkeypatch) -> None:
    """A refused projects.create opens the parent's IAM page, says the role to
    grant, and polls testIamPermissions on the parent; once the grant is seen
    the project is created, with no question asked again."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    denied = ApiError(403, "PERMISSION_DENIED", "caller lacks resourcemanager.projects.create")
    api = FakeProjectApi(gcp, create_error=denied)
    api.granted_on_poll = 3
    ask = Answers(foundations_project=["", PROJECT])
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    ((url, why),) = opened
    assert url == "https://console.cloud.google.com/iam-admin/iam?organizationId=4242"
    assert url == foundations.iam_page(ORG)
    assert "roles/resourcemanager.projectCreator" in why and ORG in why
    assert len(api.called("can_create_projects")) == 3
    assert len(api.called("create_project")) == 2 and PROJECT in gcp.projects
    assert [step for step, _ in ask.asked] == ["foundations.project", "foundations.project"]
    assert foundations.iam_page("folders/77") == "https://console.cloud.google.com/iam-admin/iam?folderId=77"


def test_project_create_denied_and_never_granted_fails_naming_the_role(tmp_path: Path, monkeypatch) -> None:
    """When the grant never arrives within the bound the step fails naming the
    role, after the page was opened; nothing is created."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, create_error=ApiError(403, "PERMISSION_DENIED", "denied"))
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.FAIL and result.step == "foundations.project"
    assert "roles/resourcemanager.projectCreator" in result.message
    assert [url for url, _ in opened] == [foundations.iam_page(ORG)]
    assert PROJECT not in gcp.projects and api.called("link_billing") == []


def test_billing_link_denied_opens_the_projects_billing_page_and_polls_it(tmp_path: Path, monkeypatch) -> None:
    """A refused updateBillingInfo opens the project's billing page, naming the
    account to link, and polls the project's billing info; once it is linked
    there the step carries on to the budget."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, link_error=ApiError(403, "PERMISSION_DENIED", "denied"))
    polled = {"n": 0}
    real = gcp.billing_info

    def billing_info(project):
        if api.called("link_billing"):
            polled["n"] += 1
            if polled["n"] == 2:  # the person links it on the page
                gcp.billing = {"billingEnabled": True, "billingAccountName": f"billingAccounts/{ACCOUNT}"}
        return real(project)

    monkeypatch.setattr(gcp, "billing_info", billing_info)
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    ((url, why),) = opened
    assert url == f"https://console.cloud.google.com/billing/linkedaccount?project={PROJECT}"
    assert f"billingAccounts/{ACCOUNT}" in why
    assert api.called("link_billing") == [(PROJECT, ACCOUNT)]
    assert len(gcp.budget_list) == 1


def test_budget_create_denied_opens_the_budgets_page_and_polls_for_it(tmp_path: Path, monkeypatch) -> None:
    """A refused budgets.create opens the billing account's budgets page with
    the budget to create there, and polls the account's budgets until one of
    that name appears."""
    opened = _pages(monkeypatch)
    gcp = FakeGcp()
    name = f"swarm-{PROJECT}"

    def create_budget(account, body):
        raise ApiError(403, "PERMISSION_DENIED", "caller lacks billing.budgets.create")

    listed = {"n": 0}
    real = gcp.budgets

    def budgets(account):
        listed["n"] += 1
        if opened and listed["n"] >= 4:  # the person made it on the page
            gcp.budget_list = [{"displayName": name}]
        return real(account)

    monkeypatch.setattr(gcp, "create_budget", create_budget)
    monkeypatch.setattr(gcp, "budgets", budgets)
    rig = _recorded(tmp_path)
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=["900"]))]).run()
    assert result.exit_code == ExitCode.OK, result.message
    ((url, why),) = opened
    assert url == f"https://console.cloud.google.com/billing/{ACCOUNT}/budgets?project={PROJECT}"
    for said in (name, "900", "50/90/100"):
        assert said in why, said


def test_budget_create_denied_and_never_made_fails_naming_the_right(tmp_path: Path, monkeypatch) -> None:
    opened = _pages(monkeypatch)
    gcp = FakeGcp()

    def create_budget(account, body):
        raise ApiError(403, "PERMISSION_DENIED", "denied")

    monkeypatch.setattr(gcp, "create_budget", create_budget)
    rig = _recorded(tmp_path)
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=[""]))]).run()
    assert result.exit_code == ExitCode.FAIL
    assert "billing.budgets.create" in result.message
    assert [url for url, _ in opened] == [foundations.budgets_page(ACCOUNT, PROJECT)]


# -- foundations.config: the never-touch list (generic-repository design §2.4) --

DENY_OBJECT = "config/dev/deny-list.json"
OTHER_SA = f"peer-runner@{PROJECT}.iam.gserviceaccount.com"


def _configured(tmp_path: Path, deny_list: list[str] | None = None) -> Rig:
    """A rig whose bucket is the attached state bucket, holding the state stage
    0 records (the signed-in account reviews the list)."""
    rig = Rig(tmp_path)
    earlier = InstallState(project=PROJECT, environment="dev", region="us-central1",
                           outputs={"installer": "admin@example.com"}, deny_list=list(deny_list or []))
    rig.bucket.write(STATE_OBJECT, earlier.to_bytes(), if_generation_match=0)
    return rig


def _neighbourly(gcp: FakeGcp) -> None:
    """A project another team already uses, beside what the platform owns."""
    gcp.held = {
        "clusters": [{"name": "peer-cluster"},
                     {"name": "swarm-dev"},
                     {"name": "ops", "resourceLabels": {"managed-by": "swarm-terraform"}}],
        "networks": [{"name": "default"}, {"name": "peer-vpc"}, {"name": "swarm-dev-vpc"}],
        "subnetworks": [{"name": "default", "network": f"projects/{PROJECT}/global/networks/default"},
                        {"name": "peer-subnet", "network": f"projects/{PROJECT}/global/networks/peer-vpc"},
                        {"name": "swarm-dev-subnet", "network": f"projects/{PROJECT}/global/networks/swarm-dev-vpc"}],
        "buckets": [{"name": "peer-bucket"}, {"name": "logs-archive", "labels": {"managed-by": "swarm-bootstrap"}}],
        "service_accounts": [{"email": OTHER_SA},
                             {"email": f"swarm-api@{PROJECT}.iam.gserviceaccount.com"},
                             {"email": f"{NUMBER}-compute@developer.gserviceaccount.com"}],
    }


def test_existing_project_list_is_what_it_holds_and_written_after_approval(tmp_path: Path) -> None:
    """Design §2.3: for a project that existed, the list is what preflight
    finds that the deployment does not own, approved by typing the project id;
    written with each kind's floor at the count found, then read into the state."""
    gcp = FakeGcp()
    _neighbourly(gcp)
    ask = Answers(foundations_config=[PROJECT])
    rig = _configured(tmp_path)
    step = foundations.ConfigStep(lambda: gcp, ask)
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message

    ((asked_step, question),) = ask.asked
    assert asked_step == "foundations.config"
    for name in ("peer-cluster", "peer-vpc", "peer-subnet", "peer-bucket", OTHER_SA, DENY_OBJECT):
        assert name in question, name
    for owned in ("swarm-dev", "ops", "swarm-api@", "-compute@", "logs-archive"):
        assert owned not in question, owned

    data, generation = rig.bucket.read(DENY_OBJECT)
    assert (DENY_OBJECT, 0) in rig.bucket.writes, "written create-only"
    doc = json.loads(data)
    entries = ["peer-bucket", "peer-cluster", "default", "peer-vpc", OTHER_SA, "peer-subnet"]
    assert doc["project"] == PROJECT
    assert doc["entries"] == entries
    assert {r["glob"]: r["kind"] for r in doc["classify"]} == {
        "peer-bucket": "bucket", "peer-cluster": "cluster", "default": "default-network",
        "peer-vpc": "network", OTHER_SA: "sa", "peer-subnet": "subnet"}
    assert doc["floors"] == {"total": 6, "cluster": 1, "network": 1, "default-network": 1, "subnet": 1,
                             "bucket": 1, "sa": 1}
    assert doc["reviewed_by"] == "admin@example.com" and doc["reviewed_at"]
    assert rig.store.load().deny_list == entries
    assert foundations.deny_list_entries(data, PROJECT) == entries

    # A second run finds it written and read: no question, no write.
    again = Answers()
    result = rig.engine([foundations.ConfigStep(lambda: gcp, again)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert again.asked == [] and rig.bucket.read(DENY_OBJECT)[1] == generation


def test_existing_project_list_refused_writes_nothing(tmp_path: Path) -> None:
    gcp = FakeGcp()
    _neighbourly(gcp)
    rig = _configured(tmp_path)
    step = foundations.ConfigStep(lambda: gcp, Answers(foundations_config=["yes"] * preflight.CONFIRM_TRIES))
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.FAIL
    assert "no never-touch list was written" in result.message
    assert rig.bucket.read(DENY_OBJECT) is None
    assert rig.store.load().deny_list == []


def test_existing_project_holding_nothing_still_asks_and_writes_an_explicit_empty_list(tmp_path: Path) -> None:
    """An existing project with nothing of anyone else's is still approved:
    the empty list is a statement someone made, never a default."""
    gcp = FakeGcp()
    ask = Answers(foundations_config=[PROJECT])
    rig = _configured(tmp_path)
    result = rig.engine([foundations.ConfigStep(lambda: gcp, ask)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(ask.asked) == 1 and "(nothing)" in ask.asked[0][1]
    doc = json.loads(rig.bucket.read(DENY_OBJECT)[0])
    assert doc["entries"] == [] and set(doc["floors"].values()) == {0}


def test_created_project_gets_an_explicitly_empty_reviewed_list(tmp_path: Path) -> None:
    """Design §2.3: a project the installer created holds nobody else's
    resources; the list is written empty with every floor zero, unasked."""
    gcp = FakeGcp()
    gcp.projects[PROJECT]["labels"] = {"managed-by": "swarm-install", "environment": "dev"}
    _neighbourly(gcp)  # never listed: the project is the installer's own
    ask = Answers()
    rig = _configured(tmp_path)
    result = rig.engine([foundations.ConfigStep(lambda: gcp, ask)]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert ask.asked == []
    assert not gcp.called("clusters") and not gcp.called("buckets")
    doc = json.loads(rig.bucket.read(DENY_OBJECT)[0])
    assert doc["entries"] == [] and doc["classify"] == []
    assert doc["floors"] == {"total": 0, **{kind: 0 for kind in foundations.DENY_LIST_KINDS}}
    assert doc["reviewed_by"] == "admin@example.com"
    assert rig.store.load().deny_list == []


def test_a_list_already_there_is_read_never_replaced(tmp_path: Path) -> None:
    """The owner's list (or an earlier run's) is the one read into the state."""
    gcp = FakeGcp()
    rig = _configured(tmp_path)
    owner = {"project": PROJECT, "entries": ["peer-cluster"], "floors": {"total": 1},
             "reviewed_by": "owner@example.com", "reviewed_at": "2026-11-07"}
    rig.bucket.write(DENY_OBJECT, json.dumps(owner).encode(), if_generation_match=0)
    before = rig.bucket.read(DENY_OBJECT)
    step = foundations.ConfigStep(lambda: gcp, Answers())
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert rig.bucket.read(DENY_OBJECT) == before
    assert rig.store.load().deny_list == ["peer-cluster"]
    assert not gcp.called("clusters")


@pytest.mark.parametrize("written, why", [
    (b"{not json", "not valid JSON"),
    (b"[]", "not a JSON object"),
    (json.dumps({"project": "another-project", "entries": [], "floors": {},
                 "reviewed_by": "o@example.com", "reviewed_at": "2026-11-07"}).encode(), "another-project"),
    (json.dumps({"project": PROJECT, "floors": {}, "reviewed_by": "o@example.com",
                 "reviewed_at": "2026-11-07"}).encode(), "entries is not a list"),
    (json.dumps({"project": PROJECT, "entries": [], "floors": {"total": -1},
                 "reviewed_by": "o@example.com", "reviewed_at": "2026-11-07"}).encode(), "whole numbers"),
    (json.dumps({"project": PROJECT, "entries": [], "floors": {}}).encode(), "who reviewed it"),
    (json.dumps({"project": PROJECT, "entries": ["peer-cluster"], "floors": {"total": 2},
                 "reviewed_by": "o@example.com", "reviewed_at": "2026-11-07"}).encode(), "fewer entries"),
])
def test_an_unusable_list_stops_the_step_and_is_never_read_as_empty(tmp_path: Path, written: bytes,
                                                                    why: str) -> None:
    gcp = FakeGcp()
    rig = _configured(tmp_path, deny_list=["kept"])
    rig.bucket.write(DENY_OBJECT, written, if_generation_match=0)
    step = foundations.ConfigStep(lambda: gcp, Answers())
    result = rig.engine([step]).run()
    assert result.exit_code == ExitCode.FAIL
    assert why in result.message and "never replaces a never-touch list" in result.message
    assert rig.bucket.read(DENY_OBJECT)[0] == written
    assert rig.store.load().deny_list == ["kept"]


def test_a_listing_refused_stops_the_step_but_an_api_that_is_off_lists_nothing(tmp_path: Path) -> None:
    """A list that missed a kind is never offered as the whole one; with the
    Kubernetes Engine API off there is simply no cluster."""
    gcp = FakeGcp()
    _neighbourly(gcp)
    gcp.held["buckets"] = ApiError(403, "PERMISSION_DENIED", "storage.buckets.list denied")
    rig = _configured(tmp_path)
    result = rig.engine([foundations.ConfigStep(lambda: gcp, Answers())]).run()
    assert result.exit_code == ExitCode.FAIL
    assert rig.bucket.read(DENY_OBJECT) is None

    _neighbourly(gcp)
    gcp.held["clusters"] = ApiError(403, "PERMISSION_DENIED",
                                    "Kubernetes Engine API has not been used in project 1 before or it is disabled")
    found = preflight.neighbours(gcp, PROJECT)
    assert "cluster" not in {item["kind"] for item in found}
    assert {"name": "peer-bucket", "kind": "bucket"} in found


def test_a_state_bucket_name_another_project_holds_is_said_as_taken(tmp_path: Path) -> None:
    """Epic comment 64: bucket names are global; one held by another project
    (a 403 on its read, a 409 on its create) is said as taken, with what to do,
    rather than as a raw API error. Moving to a suffixed name is a question in
    the PR: every verb derives the name from the project id."""

    class Taken(FakeGcp):
        def __init__(self, *, read: int | None, create: int | None) -> None:
            super().__init__()
            self.read_status, self.create_status = read, create

        def bucket(self, name):
            if self.read_status:
                raise ApiError(self.read_status, "", name)
            return super().bucket(name)

        def create_bucket(self, project, body):
            if self.create_status:
                raise ApiError(self.create_status, "", body["name"])
            return super().create_bucket(project, body)

    for read, create in ((403, None), (None, 409)):
        gcp = Taken(read=read, create=create)
        (tmp_path / f"{read}-{create}").mkdir()
        rig = Rig(tmp_path / f"{read}-{create}", local_only=True)
        earlier = InstallState(project=PROJECT, environment="dev", region="europe-west1")
        (tmp_path / f"{read}-{create}" / "state.json").write_bytes(earlier.to_bytes())
        result = rig.engine([foundations.StateBucketStep(lambda: gcp, Answers())]).run()
        assert result.exit_code == ExitCode.FAIL
        assert f"gs://{state_bucket(PROJECT)} is taken by another project" in result.message, result.message
        assert "install into a project with another id" in result.message
