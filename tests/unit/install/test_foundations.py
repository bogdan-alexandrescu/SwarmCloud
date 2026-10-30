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

from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.manifest import Entry, Manifest
from swarm_install.manifest import load as load_manifest
from swarm_install.state import STATE_OBJECT, InstallState, StepRecord, state_bucket
from swarm_install.steps import edge, foundations
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
    refused = foundations.DefaultComputeEditorStep(lambda: gcp, Answers(foundations_default_compute_editor=["yes"]))
    ctx = ctx_for(outputs={"project_number": NUMBER}, dry_run=False)
    assert refused.check(ctx).status == UNMET
    with pytest.raises(PermissionError, match="not confirmed"):
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
    assert {"foundations.project", "foundations.apis", "foundations.state-bucket", "edge.brand",
            "edge.desktop-client"} <= planned


# -- DAYONE-PROJECT-CREATE: decision 6, the project created through the APIs ---


class FakeProjectApi:
    """`foundations.ProjectApi` on the fake Google Cloud: `projects.create`
    answers with an operation that is done on its first poll, then the project
    appears; `updateBillingInfo` links the account. Every write is a mutation."""

    def __init__(self, gcp: FakeGcp, *, link_error: ApiError | None = None,
                 create_error: ApiError | None = None) -> None:
        self.gcp = gcp
        self.link_error = link_error
        self.create_error = create_error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.pending: dict[str, str] = {}

    def _write(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        self.gcp.log.append(("gcp", name))
        self.gcp.mutations.append((name, args))

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]

    def create_project(self, project, parent, labels):
        self._write("create_project", project, parent, tuple(sorted(labels.items())))
        if self.create_error is not None:
            raise self.create_error
        self.pending["operations/cp.1"] = project
        self.gcp.projects[project] = {"name": f"projects/{NUMBER}", "state": "ACTIVE", "parent": parent}
        return {"name": "operations/cp.1", "done": False}

    def operation(self, name):
        self.calls.append(("operation", (name,)))
        return {"name": name, "done": True, "response": {"projectId": self.pending[name]}}

    def link_billing(self, project, account):
        self._write("link_billing", project, account)
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
    result = rig.engine([foundations.BudgetStep(lambda: gcp, Answers(foundations_budget=["lots"]))]).run()
    assert result.exit_code == ExitCode.FAIL
    assert gcp.budget_list == []
    assert "budget_amount" not in rig.store.load().outputs


def test_wrong_confirmation_creates_and_links_nothing(tmp_path: Path, monkeypatch) -> None:
    """A typed answer that is not the project id refuses, even with
    SWARM_ASSUME_YES set; so does a budget that is not a whole dollar amount.
    Neither writes anything or records the budget."""
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")
    for answers_given, said in ((["", "yes"], "not confirmed"), (["a lot"], "whole number of US dollars")):
        gcp = FakeGcp(exists=False, billed=False)
        api = FakeProjectApi(gcp)
        rig = _recorded(tmp_path / said.replace(" ", "-"))
        result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=answers_given), api)).run()
        assert result.exit_code == ExitCode.FAIL
        assert result.step == "foundations.project"
        assert said in result.message
        assert api.calls == [] and gcp.mutations == []
        assert "budget_amount" not in rig.store.load().outputs


def test_billing_link_failure_leaves_the_project_and_the_rerun_links_it(tmp_path: Path) -> None:
    """When updateBillingInfo is refused the step fails naming the right it
    needs and saying the project was created; the next run does not create it
    again: it only links billing, after its own approval."""
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

    api.link_error = None
    ask = Answers(foundations_project=["", PROJECT])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    assert len(api.called("create_project")) == 1
    assert "link billing for" in ask.asked[1][1] and "parent" not in ask.asked[1][1]
    assert api.called("link_billing") == [(PROJECT, ACCOUNT), (PROJECT, ACCOUNT)]
    (budget,) = gcp.budget_list  # Enter kept the default
    assert budget["amount"]["specifiedAmount"]["units"] == str(foundations.DEFAULT_BUDGET_USD)


def test_project_id_taken_says_so(tmp_path: Path) -> None:
    """Project ids are global: a 409 on create is someone else's project, said
    as that, and nothing is linked."""
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp, create_error=ApiError(409, "ALREADY_EXISTS", "requested entity already exists"))
    rig = _recorded(tmp_path)
    result = rig.engine(_project_and_budget(gcp, Answers(foundations_project=["", PROJECT]), api)).run()
    assert result.exit_code == ExitCode.FAIL
    assert f"project id {PROJECT} is taken" in result.message
    assert api.called("link_billing") == []


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
