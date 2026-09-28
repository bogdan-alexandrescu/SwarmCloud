"""Which saved plans stop a dev release for the owner: those that change IAM.

Owner decision, 2026-09-28 (#268; docs/ci.md, "A dev release that changes IAM
waits for the owner"). dev stays un-gated for routine releases -- an image
digest moving, a service's env changing -- but a plan that creates, updates,
replaces, deletes or forgets a custom role or an IAM member, binding or policy
is applied only in the `dev-iam` environment, behind the owner's review.

The classification is `scripts/lib/iam-plan.jq`, reached through
`scripts/lib/plan-guard.sh --classify-iam`, which release.yml runs on the JSON
of the very plan it will apply. This file runs that entry point, offline, on
the shapes `terraform show -json` writes.

THE CASE THAT MOTIVATED THE FIXTURE. terraform/infra/
custom_roles_moved_to_bootstrap.tf FORGETS eight custom roles: `removed` with
`destroy = false`, which the plan spells as the action "forget". Nothing live
is deleted, and the change still decides who administers those roles -- so a
filter that knew only create/update/delete would have waved it through dev.

WHICH WAY IT FAILS. A plan the classifier cannot read is a refusal, not a
"no": an unreadable plan answered "false" would be applied in `dev` with
nobody asked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUARD = REPO / "scripts" / "lib" / "plan-guard.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "plans"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

# The families the owner named, one real type each (terraform/infra and
# terraform/modules use every one of these but the policy).
IAM_TYPES = (
    "google_project_iam_custom_role",
    "google_project_iam_member",
    "google_storage_bucket_iam_member",
    "google_secret_manager_secret_iam_binding",
    "google_cloud_run_v2_service_iam_member",
    "google_service_account_iam_member",
    "google_pubsub_topic_iam_policy",
)

# create, update, replace (both orders Terraform writes), delete, forget.
GATED_ACTIONS = (
    ["create"],
    ["update"],
    ["delete", "create"],
    ["create", "delete"],
    ["delete"],
    ["forget"],
)


def _classify(plan_path: Path, tmp_path: Path, *, summary: bool = True) -> subprocess.CompletedProcess:
    args = [str(GUARD), "--plan", str(plan_path), "--classify-iam"]
    if summary:
        args += ["--summary", str(tmp_path / "summary.md")]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def _plan(tmp_path: Path, *changes: dict, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({"format_version": "1.2", "terraform_version": "1.16.2", "resource_changes": list(changes)}))
    return path


def _change(rtype: str, actions: list[str], *, address: str | None = None, before=None, after=None) -> dict:
    values = {"project": "saga-agents-staging", "role": "roles/viewer",
              "member": "serviceAccount:swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com"}
    return {
        "address": address or f"{rtype}.edge",
        "mode": "managed",
        "type": rtype,
        "name": "edge",
        "change": {
            "actions": actions,
            "before": before if before is not None else (None if actions == ["create"] else values),
            "after": after if after is not None else (None if actions in (["delete"], ["forget"]) else values),
            "after_unknown": {},
        },
    }


def _answer(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, f"--classify-iam exited {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


def test_a_plan_that_forgets_a_custom_role_is_an_iam_plan(tmp_path):
    """The fixture is the shape custom_roles_moved_to_bootstrap.tf produced.
    MUTATION: drop "forget" from the gated actions in iam-plan.jq."""
    proc = _classify(FIXTURES / "iam-forgotten-custom-role.json", tmp_path)
    assert _answer(proc) == "true"
    summary = (tmp_path / "summary.md").read_text()
    assert "module.iam.google_project_iam_custom_role.job_dispatcher" in summary, summary
    assert "forget" in summary, summary
    assert "swarmJobDispatcher" in summary, summary
    # The unchanged service beside it is not an IAM row.
    assert "google_cloud_run_v2_service" not in summary, summary


def test_an_image_digest_only_plan_is_not_an_iam_plan(tmp_path):
    """A routine release: two digests move, an IAM member is planned no-op and
    a google_iam_policy DATA source is read. None of it changes IAM.
    MUTATION: gate "no-op" or "read", or match `iam_policy` without the
    resource family in front of it."""
    proc = _classify(FIXTURES / "image-digest-only.json", tmp_path)
    assert _answer(proc) == "false"
    summary = (tmp_path / "summary.md").read_text()
    assert "google_cloud_run_v2_service" not in summary, summary
    assert "google_project_iam_member" not in summary, summary


@pytest.mark.parametrize("actions", GATED_ACTIONS, ids=lambda a: "+".join(a))
@pytest.mark.parametrize("rtype", IAM_TYPES)
def test_every_iam_family_and_every_changing_action_is_gated(tmp_path, rtype, actions):
    proc = _classify(_plan(tmp_path, _change(rtype, actions)), tmp_path)
    assert _answer(proc) == "true", f"{rtype} {'+'.join(actions)} was not classified as an IAM change"
    assert f"{rtype}.edge" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("actions", (["no-op"], ["read"]), ids=lambda a: "+".join(a))
@pytest.mark.parametrize("rtype", IAM_TYPES)
def test_an_iam_resource_that_does_not_change_is_not_gated(tmp_path, rtype, actions):
    proc = _classify(_plan(tmp_path, _change(rtype, actions, before={}, after={})), tmp_path)
    assert _answer(proc) == "false"


@pytest.mark.parametrize(
    "rtype",
    (
        # "iam" in the name is not the rule; the owner's families are.
        "google_iam_workload_identity_pool",
        # What this platform changes on every release.
        "google_cloud_run_v2_service",
        "google_cloud_run_v2_job",
    ),
)
def test_types_outside_the_owners_list_are_not_gated(tmp_path, rtype):
    proc = _classify(_plan(tmp_path, _change(rtype, ["update"])), tmp_path)
    assert _answer(proc) == "false", f"{rtype} was classified as an IAM change"


def test_a_plan_with_no_changes_is_not_gated(tmp_path):
    """Terraform omits `resource_changes` when there are none."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"format_version": "1.2", "terraform_version": "1.16.2"}))
    assert _answer(_classify(path, tmp_path)) == "false"


def test_every_iam_row_is_written_to_the_summary(tmp_path):
    """The reviewer reads these at the gate. Every row, with the role and the
    member, and nothing else."""
    plan = _plan(
        tmp_path,
        _change("google_project_iam_member", ["create"], address="module.iam.google_project_iam_member.plain[\"a\"]"),
        _change("google_storage_bucket_iam_member", ["delete"], address="module.storage.google_storage_bucket_iam_member.b"),
        _change("google_cloud_run_v2_service", ["update"], address="module.cloud_run.google_cloud_run_v2_service.this"),
    )
    assert _answer(_classify(plan, tmp_path)) == "true"
    summary = (tmp_path / "summary.md").read_text()
    assert 'module.iam.google_project_iam_member.plain["a"]' in summary, summary
    assert "module.storage.google_storage_bucket_iam_member.b" in summary, summary
    assert "roles/viewer" in summary, summary
    assert "swarm-scheduler@saga-agents-staging.iam.gserviceaccount.com" in summary, summary
    assert "google_cloud_run_v2_service.this" not in summary, summary


def test_a_value_holding_a_pipe_does_not_break_the_summary_table(tmp_path):
    plan = _plan(
        tmp_path,
        _change(
            "google_project_iam_member", ["create"],
            after={"project": "saga-agents-staging", "role": "roles/a|b",
                   "member": "serviceAccount:x@saga-agents-staging.iam.gserviceaccount.com"},
        ),
    )
    assert _answer(_classify(plan, tmp_path)) == "true"
    rows = [l for l in (tmp_path / "summary.md").read_text().splitlines() if "google_project_iam_member.edge" in l]
    assert len(rows) == 1, rows
    assert "roles/a\\|b" in rows[0], rows[0]


def test_the_answer_is_the_only_thing_on_stdout(tmp_path):
    """release.yml writes stdout to $GITHUB_OUTPUT as `iam=<stdout>`."""
    proc = _classify(FIXTURES / "iam-forgotten-custom-role.json", tmp_path, summary=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "true\n", repr(proc.stdout)


@pytest.mark.parametrize(
    "content",
    (
        "",
        "{",
        '"nope"',
        "{}",
        '{"format_version": "1.2", "resource_changes": {}}',
        '{"format_version": "1.2", "resource_changes": [{"address": "x", "type": "google_project_iam_member"}]}',
        '{"format_version": "1.2", "resource_changes": [{"address": "x", "change": {"actions": ["create"]}}]}',
        '{"format_version": "1.2", "resource_changes": [{"address": "x", "type": "google_project_iam_member", '
        '"change": {"actions": "create"}}]}',
    ),
    ids=("empty", "truncated", "scalar", "not-a-plan", "changes-not-a-list", "no-actions", "no-type", "actions-not-a-list"),
)
def test_a_plan_that_cannot_be_read_is_refused_not_answered_false(tmp_path, content):
    """An unreadable plan must not come back "false": false means "apply in
    dev, ask nobody". MUTATION: default a missing `.change.actions` to [] or
    a missing `format_version` to a plan with no changes."""
    path = tmp_path / "plan.json"
    path.write_text(content)
    proc = _classify(path, tmp_path)
    assert proc.returncode != 0, f"--classify-iam answered {proc.stdout!r} for {content!r}"
    assert proc.stdout.strip() not in ("true", "false"), proc.stdout


def test_classifying_requires_a_plan(tmp_path):
    proc = subprocess.run([str(GUARD), "--classify-iam"], capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


def test_the_self_test_covers_the_classification():
    """`make test` and application.yml run `plan-guard.sh --self-test`; it must
    exercise the classifier too, not only the shared-project guard."""
    proc = subprocess.run([str(GUARD), "--self-test"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "forgotten custom role" in proc.stderr, proc.stderr
    assert "image digest" in proc.stderr, proc.stderr
