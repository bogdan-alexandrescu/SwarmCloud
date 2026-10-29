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

import pytest

from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.manifest import Entry, Manifest
from swarm_install.manifest import load as load_manifest
from swarm_install.state import STATE_OBJECT, InstallState, StepRecord, state_bucket
from swarm_install.steps import edge, foundations
from swarm_install.steps.preflight import ApiError

from .test_preflight import ORG, NUMBER, PROJECT, Answers, FakeGcp, Rig, answers, ctx_for, stage0, stage1

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
