"""The `server` constants group: the server values the console restates.

The console's tests used to open Python and Terraform files and regex them;
they now read `apps/swarm-ui/src/generated/constants/server.json`, which
`scripts/api_constants/server.py` builds from the live objects (part of #72).
This file holds the group's values equal to those objects, its one text
parser (jobs.tf) on a sample and on the real file, and the server-side
behaviour the console tests were the only holders of.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GROUP = REPO / "scripts/api_constants/server.py"
COMMITTED = REPO / "apps/swarm-ui/src/generated/constants/server.json"
JOBS_TF = REPO / "terraform/modules/scheduler/jobs.tf"

_spec = importlib.util.spec_from_file_location("api_constants_server_under_test", GROUP)
server = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = server
_spec.loader.exec_module(server)


@pytest.fixture(scope="module")
def built() -> dict:
    return server.build()


# --------------------------------------------------------------------------
# the jobs.tf parser

SAMPLE = '''\
# Cloud Scheduler: the ticks.

resource "google_cloud_scheduler_job" "first_tick" {
  name    = "${var.name_prefix}-first-tick"
  pubsub_target {
    topic_name = google_pubsub_topic.wake.id
  }
}

resource "google_pubsub_topic" "not_a_job" {
  name = "${var.name_prefix}-topic"
}

resource "google_cloud_scheduler_job" "per_tenant" {
  for_each = var.tenants
  name     = "${var.name_prefix}-per-tenant-${each.key}"
  http_target {
    uri = "${trimsuffix(var.api_endpoint, "/")}/v1/admin/x?tenant_id=${urlencode(each.key)}&y=true"
    oidc_token {
      service_account_email = var.sa
    }
  }
}
'''


def test_the_parser_reads_jobs_in_file_order_from_a_sample():
    assert server.scheduler_jobs(SAMPLE) == [
        {"resource": "first_tick", "job": "first-tick", "uri": None},
        {"resource": "per_tenant", "job": "per-tenant-<tenant>",
         "uri": '${trimsuffix(var.api_endpoint, "/")}/v1/admin/x'
                "?tenant_id=${urlencode(each.key)}&y=true"},
    ]


def test_the_parser_refuses_a_job_with_no_prefixed_name():
    with pytest.raises(ValueError, match="bare"):
        server.scheduler_jobs('resource "google_cloud_scheduler_job" "bare" {\n  name = "x"\n}\n')


def test_the_parser_reads_the_real_file_in_order_and_non_empty():
    text = JOBS_TF.read_text()
    jobs = server.scheduler_jobs(text)
    assert len(jobs) >= 11
    names = [j["resource"] for j in jobs]
    # File order: each resource's declaration offset increases.
    offsets = [text.index(f'resource "google_cloud_scheduler_job" "{n}"') for n in names]
    assert offsets == sorted(offsets)
    assert text.count('resource "google_cloud_scheduler_job"') == len(jobs)
    assert names[0] == "safety_tick"
    assert "schedule_tick" in names and "workflow_rollup" in names


def test_the_rollup_job_asks_the_route_to_seal(built):
    rollup = next(j for j in built["scheduler_jobs"] if j["resource"] == "workflow_rollup")
    assert rollup["uri"].endswith(
        "/v1/admin/workflows/rollup?tenant_id=${urlencode(each.key)}&seal_outcomes=true")


# --------------------------------------------------------------------------
# each value is the live object

def test_the_committed_file_is_what_the_group_builds(built):
    on_disk = json.loads(COMMITTED.read_text())
    on_disk.pop("_generated")
    assert on_disk == json.loads(json.dumps(built))


def test_the_workspace_secret_values_are_the_validators(built):
    from swarm_api import workspace_secrets as ws
    from swarm_api.errors import ValidationFailed

    got = built["workspace_secrets"]
    assert got["slot_host_pattern"] == ws.SLOT_HOST.pattern
    assert got["max_slot_hosts"] == ws.MAX_SLOT_HOSTS
    assert got["host_scoped_slots"] == list(ws.HOST_SCOPED_SLOTS)
    slot = ws.HOST_SCOPED_SLOTS[0]
    # Host number MAX_SLOT_HOSTS + 1 is refused, with the exported message.
    many = [f"h{i}.example.com" for i in range(ws.MAX_SLOT_HOSTS + 1)]
    with pytest.raises(ValidationFailed) as too_many:
        ws.slot_hosts(slot, many, enabled=True)
    assert too_many.value.message == got["hosts_too_many"]
    assert ws.slot_hosts(slot, many[:-1], enabled=True) == sorted(many[:-1])
    with pytest.raises(ValidationFailed) as refused:
        ws.slot_hosts(slot, ["https://example.com"], enabled=True)
    assert refused.value.message == got["host_refused"]
    for name in ws.HOST_SCOPED_SLOTS:
        with pytest.raises(ValidationFailed) as needed:
            ws.slot_hosts(name, [], enabled=True)
        assert needed.value.message == got["hosts_needed"][name]


def test_the_repository_prefixes_are_validations_constant(built):
    from swarm_api.validation import REPOSITORY_URL_PREFIXES

    assert built["repository_url_prefixes"] == list(REPOSITORY_URL_PREFIXES)
    assert sorted(built["repository_url_prefixes"]) == ["git@", "https://", "ssh://"]


def test_merge_cap_is_the_workers(built):
    from agent_worker.merge import MERGE_MAX_HEAD_UPDATES

    assert built["merge_max_head_updates"] == MERGE_MAX_HEAD_UPDATES


def test_the_doctor_values_are_swarm_installs(built):
    from swarm_install import exitcodes
    from swarm_install.doctor import Finding
    from swarm_install.tfvars import SCHEDULED_DOCTOR_SHIPPED

    sample = Finding(id="api.example", status=exitcodes.MET, detail="seen")
    assert built["doctor"]["finding_keys"] == sorted(sample.to_json())
    assert built["doctor"]["statuses"] == [exitcodes.MET, exitcodes.UNMET, exitcodes.UNKNOWN]
    assert built["doctor"]["scheduled_doctor_shipped"] is SCHEDULED_DOCTOR_SHIPPED


def test_the_team_add_flags_are_its_parsers(built):
    from swarm_install.cli import main

    flags = built["team_add_flags"]
    assert "--project" in flags and "--display-name" in flags and "--first-admin" in flags
    # Each exported flag parses; an unexported one is refused.
    with pytest.raises(SystemExit) as refused:
        main(["team", "add", "eng@example.com", "--project=example-project", "--no-such-flag"])
    assert refused.value.code == 2


def test_the_refusal_tables_are_the_two_modules(built):
    from swarm_api.pluginstore import REFUSALS as api

    intake = server.intake_refusals()
    assert built["plugin_refusals"]["api"] == api
    assert built["plugin_refusals"]["intake"] == intake
    assert len(api) > 30 and len(intake) > 40


def test_the_schema_fields_are_the_models(built):
    from swarm_api.schemas import StepPluginCreate, WorkflowStepCreate

    assert built["schema_fields"]["WorkflowStepCreate"] == list(WorkflowStepCreate.model_fields)
    assert built["schema_fields"]["StepPluginCreate"] == list(StepPluginCreate.model_fields)
    assert {"plugins", "plugins_off"} <= set(built["schema_fields"]["WorkflowStepCreate"])


def test_the_events_route_pages(built):
    # Its paging behaviour is tests/unit/control_plane/test_event_paging.py's.
    assert built["events_route_pages"] is True


def test_the_read_scope_runs_the_collision_check_only(built):
    # A disabled tenant still reading its work is
    # test_tenant_isolation.py::test_a_disabled_tenant_can_still_read_and_cancel_its_own_work.
    assert built["read_scope_checks_collision_only"] is True


# --------------------------------------------------------------------------
# server-side behaviour the console tests were the only holders of

def test_check_repository_url_accepts_each_exported_prefix_and_refuses_another(built):
    from swarm_api.validation import check_repository_url

    for prefix in built["repository_url_prefixes"]:
        url = f"{prefix}github.com/example-org/example-repo.git"
        if prefix == "git@":
            url = "git@github.com:example-org/example-repo.git"
        assert check_repository_url(url) == url
    for refused in ("http://github.com/example-org/r", "ftp://example.com/r",
                    "file:///srv/repo", "github.com/example-org/r"):
        with pytest.raises(ValueError, match="https://, ssh:// or git@"):
            check_repository_url(refused)
    assert check_repository_url(None) is None


@pytest.mark.parametrize("url", ["http://github.com/example-org/r", "file:///srv/repo"])
def test_both_request_models_refuse_what_check_repository_url_refuses(url):
    from pydantic import ValidationError

    from swarm_api.schemas import TaskCreate, WorkflowCreate
    from swarm_api.validation import check_repository_url

    with pytest.raises(ValueError) as direct:
        check_repository_url(url)
    for model in (TaskCreate, WorkflowCreate):
        validator = model.__pydantic_decorators__.field_validators["_repo_scheme"]
        assert validator.info.fields == ("repository_url",)
        with pytest.raises(ValueError) as via:
            validator.func.__func__(model, url)
        assert str(via.value) == str(direct.value)
    with pytest.raises(ValidationError, match="https://, ssh:// or git@"):
        TaskCreate.model_validate({"repository_url": url})


def test_publishing_the_values_reveals_no_deployment(built):
    text = json.dumps(built)
    for word in ("googleapis.com/v1/projects", "run.app", "@gserviceaccount", "ghp_", "BEGIN "):
        assert word not in text
