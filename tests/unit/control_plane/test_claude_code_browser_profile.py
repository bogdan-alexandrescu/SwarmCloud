"""`claude-code-browser` is claude-code on agent-runtime-browser (contract
request 67, accepted by the owner 2026-10-11).

An agent could not render or screenshot a page: `browser` is a scripted runner
that needs input.url or actions, and claude-code's image has no Chromium. The
owner's decision: a profile that is claude-code in every field but its name,
its image and -- because of the image -- the `browser` resource class, on GKE
Autopilot, named by callers like any other. These hold that:

  * the catalogue entry is claude-code with only `name`, `image` and
    `resource_class` changed, and it resolves to GKE Autopilot;
  * it takes claude-code's `issue` input and refuses anything else, an image
    included, and a task cannot carry an image at all (invariant 10);
  * its GKE pod runs the browser image's pinned digest with the browser
    class's memory (requests == limits) and the memory-backed /dev/shm;
  * the claude-code runner hands the CLI `PLAYWRIGHT_BROWSERS_PATH` and tells
    the agent to launch Chromium with its sandbox off -- only on the image
    that sets it;
  * every restatement of the catalogue carries it: the Terraform mirror and
    model table, both agent-stream tables, the Submit form and the UI's
    fixtures, which scripts/lib/check-contract-parity.sh reads;
  * request 67 records the owner's acceptance and the rollback.

MUTATION: set its resource_class to "standard" in profiles.py and
`test_the_profile_is_claude_code_on_the_browser_image` and the pod test fail;
drop it from `cli_agent_spec` and the stream test fails; drop the
PLAYWRIGHT_BROWSERS_PATH pass-through in claude_code.body and
`test_the_runner_hands_the_agent_the_browser` fails.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from swarm_common.models import Tenant
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    RUNNER_PROFILES,
    Backend,
    InputRefused,
    check_inputs,
    resolve_backend,
)

from scheduler.dispatch import BackendRouter, GkeJobDispatcher, GkeTarget
from swarm_api.schemas import TaskCreate
from swarm_api.validation import FORBIDDEN_CALLER_FIELDS, validate_runner_profile

from .conftest import PROJECT, scheduler_settings
from .test_dispatch_manifests import NOW, FakeBatchApi, make_lease, make_task

REPO = Path(__file__).resolve().parents[3]
PROFILE = "claude-code-browser"
LOCALS = REPO / "terraform/infra/locals.tf"
REQUESTS = REPO / "docs/contract-change-requests.md"
SUBMIT = REPO / "apps/swarm-ui/src/Submit.tsx"
UI_API = REPO / "apps/swarm-ui/src/api.ts"
DOCKERFILE = REPO / "images/agent-runtime-browser/Dockerfile"


@pytest.fixture
def tenant() -> Tenant:
    return Tenant(
        tenant_id="eng",
        kind="group",
        principal="eng@saga.xyz",
        created_at=NOW,
        service_account=f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
        gcs_prefix=f"gs://{PROJECT}-swarm-artifacts/tenants/eng",
        namespace="swarm-tenant-eng",
    )


# --- the catalogue ------------------------------------------------------------


def test_the_profile_is_claude_code_on_the_browser_image():
    assert PROFILE in RUNNER_PROFILES, "contract request 67's profile is missing"
    profile = RUNNER_PROFILES[PROFILE]
    claude = RUNNER_PROFILES["claude-code"]
    differ = sorted(
        f.name for f in dataclasses.fields(profile)
        if getattr(profile, f.name) != getattr(claude, f.name)
    )
    # `live_browser` is contract request 71 (LB-D, #1030): the one live profile.
    assert differ == ["image", "live_browser", "name", "resource_class"], (
        f"the owner's decision was claude-code with its name, image and class changed, "
        f"and live_browser set (request 71); {differ} differ"
    )
    assert profile.live_browser is True
    assert profile.image == "agent-runtime-browser"
    assert profile.image == RUNNER_PROFILES["browser"].image
    assert profile.resource_class == "browser"
    assert RESOURCE_CLASSES["browser"].units == 2
    assert profile.runner_argv == ("python", "-m", "agent_worker.runners.claude_code")
    assert profile.available
    assert DOCKERFILE.is_file()


def test_it_resolves_to_gke_autopilot_and_the_router_sends_it_there():
    profile = RUNNER_PROFILES[PROFILE]
    assert profile.backend is Backend.GKE_AUTOPILOT
    assert resolve_backend(profile) is Backend.GKE_AUTOPILOT
    settings = scheduler_settings()
    gke = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=FakeBatchApi())
    router = BackendRouter(cloud_run=None, gke=gke, settings=settings)
    assert router.for_backend(resolve_backend(profile)) is gke


def test_it_is_selectable_by_name():
    assert validate_runner_profile(PROFILE) is RUNNER_PROFILES[PROFILE]


# --- inputs, and invariant 10 -------------------------------------------------


def test_it_accepts_the_issue_input_like_claude_code():
    assert set(RUNNER_PROFILES[PROFILE].inputs) == {"issue"}
    assert RUNNER_PROFILES[PROFILE].inputs == RUNNER_PROFILES["claude-code"].inputs
    assert check_inputs(RUNNER_PROFILES[PROFILE], {"issue": 42}) == {"issue": 42}
    assert check_inputs(RUNNER_PROFILES[PROFILE], {"issue": 42.0}) == {"issue": 42}
    with pytest.raises(InputRefused):
        check_inputs(RUNNER_PROFILES[PROFILE], {"issue": 0})


@pytest.mark.parametrize("key", ["image", "model", "command", "resources"])
def test_an_execution_detail_sent_as_an_input_is_refused(key):
    with pytest.raises(InputRefused) as refused:
        check_inputs(RUNNER_PROFILES[PROFILE], {key: "agent-runtime-browser"})
    assert refused.value.key == key


def test_a_task_naming_it_with_an_image_is_refused():
    body = {"runner_profile": PROFILE, "input": {"prompt": "screenshot the login page"}}
    assert TaskCreate.model_validate(body).runner_profile == PROFILE
    assert "image" in FORBIDDEN_CALLER_FIELDS
    for field in ("image", "backend", "resources"):
        with pytest.raises(ValidationError) as refused:
            TaskCreate.model_validate({**body, field: "agent-runtime-browser"})
        assert any(e["type"] == "extra_forbidden" for e in refused.value.errors()), field


# --- the GKE pod --------------------------------------------------------------


def test_the_pod_carries_what_the_browser_image_needs(tenant):
    image = RUNNER_PROFILES[PROFILE].image
    base = scheduler_settings(worker_models={PROFILE: "claude-opus-5-5"})
    digest = f"{base.artifact_registry_host}/{image}@sha256:{'d' * 64}"
    settings = scheduler_settings(worker_models={PROFILE: "claude-opus-5-5"}, worker_image_refs={image: digest})
    api = FakeBatchApi()
    dispatcher = GkeJobDispatcher(settings, target=GkeTarget("https://k8s", "/ca.pem"), batch_api=api)
    task = make_task(PROFILE)
    dispatcher.dispatch(task=task, lease=make_lease(task), profile=RUNNER_PROFILES[PROFILE], tenant=tenant)
    spec = api.created[0][1]["spec"]["template"]["spec"]
    worker = next(c for c in spec["containers"] if c["name"] == "worker")
    env = {e["name"]: e["value"] for e in worker["env"]}

    assert worker["image"] == digest, "the pod must run the browser image's pinned digest"
    assert "command" not in worker and "args" not in worker
    assert env["RUNNER_PROFILE"] == PROFILE
    assert env.get("MODEL") == "claude-opus-5-5"
    resources = worker["resources"]
    assert resources["requests"] == resources["limits"], "invariant 7: no bursting"
    assert resources["limits"]["memory"] == f"{RESOURCE_CLASSES['browser'].memory_gib}Gi"
    assert str(resources["limits"]["cpu"]) == str(RESOURCE_CLASSES["browser"].cpu)
    mounts = {m["name"]: m["mountPath"] for m in worker["volumeMounts"]}
    assert mounts.get("dshm") == "/dev/shm"
    dshm = next(v for v in spec["volumes"] if v["name"] == "dshm")
    assert dshm["emptyDir"]["medium"] == "Memory"
    assert worker["securityContext"]["capabilities"] == {"drop": ["ALL"]}


# --- the runner ---------------------------------------------------------------


def _run_body(monkeypatch, tmp_path, browsers: str | None) -> dict[str, Any]:
    from agent_worker.runners import claude_code
    from agent_worker.runners.base import RunnerContext

    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["env"] = dict(extra_env or {})
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    if browsers is None:
        monkeypatch.delenv(claude_code.BROWSERS_PATH_ENV, raising=False)
    else:
        monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, browsers)
    ctx = RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Screenshot the settings page."},
    )
    claude_code.body(ctx)
    return seen


def test_the_runner_hands_the_agent_the_browser(monkeypatch, tmp_path):
    from agent_worker.runners.claude_code import BROWSER_NOTE

    seen = _run_body(monkeypatch, tmp_path, "/opt/playwright")
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/playwright"
    assert seen["prompt"].startswith("Screenshot the settings page.")
    assert seen["prompt"].endswith(BROWSER_NOTE)
    assert "chromium_sandbox=False" in BROWSER_NOTE


def test_the_runner_says_nothing_of_a_browser_on_the_base_image(monkeypatch, tmp_path):
    seen = _run_body(monkeypatch, tmp_path, None)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in seen["env"]
    assert seen["prompt"] == "Screenshot the settings page."


def test_the_browser_image_sets_the_path_the_runner_reads():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright", text, re.M)


# --- the restatements ---------------------------------------------------------


def test_it_writes_claude_codes_agent_streams():
    from agent_worker.runners.streams import cli_agent_spec
    from swarm_api.agent_streams import AGENT_STREAM_FILES

    assert cli_agent_spec(PROFILE) is cli_agent_spec("claude-code")
    assert AGENT_STREAM_FILES[PROFILE] == AGENT_STREAM_FILES["claude-code"]


def test_the_terraform_mirror_carries_it_and_its_model():
    text = LOCALS.read_text(encoding="utf-8")
    block = re.search(r'^    "claude-code-browser" = \{\n(.*?)^    \}\n', text, re.M | re.S)
    assert block, 'terraform/infra/locals.tf runner_profiles has no "claude-code-browser" entry'
    body = block.group(1)
    assert re.search(r'^\s*image\s*=\s*"agent-runtime-browser"$', body, re.M), body
    assert re.search(r'^\s*resource_class\s*=\s*"browser"$', body, re.M), body
    assert re.search(r'^\s*backend\s*=\s*"GKE_AUTOPILOT"$', body, re.M), body
    assert re.search(r'^\s*provider\s*=\s*"anthropic"$', body, re.M), body
    assert re.search(r"^\s*timeout_seconds\s*=\s*7200$", body, re.M), body
    models = dict(re.findall(r'^\s*"([a-z-]+)"\s*=\s*"(claude-[a-z0-9-]+)"$', text, re.M))
    assert models.get(PROFILE) == models["claude-code"], models


def test_the_parity_checked_ui_tables_carry_it():
    """check-contract-parity.sh reads Submit.tsx's SUGGESTED for every profile
    in the catalogue; the UI fixtures are held to the catalogue by
    test_capacity_serves_availability.py and test_ui_fixture_input_contract.py."""
    submit = SUBMIT.read_text(encoding="utf-8")
    table = re.search(r"const SUGGESTED\b[^=]*=\s*[{]\n(.*?)\n[}]\n", submit, re.S)
    assert table and f"'{PROFILE}': [" in table.group(1)
    ui = UI_API.read_text(encoding="utf-8")
    for declaration in ("FIXTURE_INPUT_CONTRACTS", "FIXTURE_AVAILABILITY"):
        literal = re.search(declaration + r"\b[^=]*=\s*(\{.*?\n\})", ui, re.S)
        assert literal, declaration
        assert PROFILE in json.loads(literal.group(1)), declaration


def test_request_67_records_the_owners_acceptance_and_the_rollback():
    text = REQUESTS.read_text(encoding="utf-8")
    start = text.index("## 67. ")
    end = text.find("\n## 68. ", start)
    section = text[start:end if end != -1 else len(text)]
    # scripts/lib/check-frozen-contract.sh passes a diff under
    # apps/common/swarm_common/ only beside an added line holding this phrase.
    assert "accepted by the owner" in section.lower()
    assert "**Status:** open" not in section
    assert "### Rollback" in section and "Delete the entry" in section
