"""What the docs of B18a and later lanes describe, held on the code alone.

These tests began as doc-accuracy pins (owner decisions of 2026-10-02 on): each
read a sentence from a document and the value it described from the code. Issue
71 retired the sentence half: no test here reads a document's wording. What
stays is the code-side property each pinned (a bound, a default, a route, a
security setting, a refusal code, how two modules' values relate), asserted on
the code, and the citation checks, which resolve every `path::qualname` and
`path` (`anchor`) a listed document cites, so a renamed or deleted symbol still
fails here as a link that no longer resolves.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from .test_docs_spec_amendments import _ROOTS, _assert_cites_resolve, _cited_anchor, _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
BUILD_PROMPT = DOCS / "BUILD_PROMPT_V2.md"
BACKENDS = DOCS / "execution-backends.md"
TENANCY = DOCS / "multi-tenancy.md"
CHECKPOINTING = DOCS / "checkpointing.md"
DESIGN = DOCS / "design" / "dispatch-and-integration.md"
UI_README = DOCS / "web-ui" / "README.md"
REDESIGN = DOCS / "web-ui" / "redesign-v2.md"
DISPATCH = REPO / "apps" / "scheduler" / "scheduler" / "dispatch.py"

TOUCHED = (BUILD_PROMPT, BACKENDS, TENANCY, CHECKPOINTING, DESIGN, UI_README, REDESIGN)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


#: dispatch.py, as the docs cite it: `path::qualname` (lane CITD).
DISPATCH_PY = "apps/scheduler/scheduler/dispatch.py"
MANIFEST = "GkeJobDispatcher._manifest"


# --------------------------------------------------------------------------
# D8 and D10: execution-backends.md
# --------------------------------------------------------------------------


def test_dispatcher_manifest_sets_the_safe_to_evict_annotation():
    """The GKE pod manifest disables eviction via cluster-autoscaler's
    safe-to-evict annotation: this IS the extended-run-time request that
    makes Spot impossible (invariant 6)."""
    assert '"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"' in _cited_symbol(DISPATCH_PY, MANIFEST)


def _dshm_gib() -> int:
    """The size the GKE dispatcher gives Chromium's /dev/shm, read from the code."""
    match = re.search(
        r'\{"name": "dshm", "emptyDir": \{\s*"medium": "Memory", "sizeLimit": "(\d+)Gi"\}',
        _text(DISPATCH),
    )
    assert match, "the dispatcher no longer mounts a memory-backed dshm volume"
    return int(match.group(1))


def test_dispatcher_dshm_mount_is_memory_backed():
    """The GKE pod's /dev/shm is a memory-backed emptyDir, sized by `_dshm_gib()`."""
    size = _dshm_gib()
    assert f'"sizeLimit": "{size}Gi"' in _cited_symbol(DISPATCH_PY, MANIFEST)


# --------------------------------------------------------------------------
# D10: multi-tenancy.md
# --------------------------------------------------------------------------


def test_gke_namespace_is_swarm_tenant_prefixed():
    """GkeJobDispatcher.namespace_for returns `swarm-tenant-{tenant}`, not `swarm-{id}`."""
    assert '"swarm-tenant-{tenant}"' in _text(DISPATCH)
    assert "swarm-tenant-{tenant}" in _cited_symbol(DISPATCH_PY, "GkeJobDispatcher.namespace_for")


# --------------------------------------------------------------------------
# D10: checkpointing.md
# --------------------------------------------------------------------------


def _profile_default_interval() -> int:
    field = next(f for f in dataclasses.fields(RunnerProfile) if f.name == "checkpoint_interval_seconds")
    return int(field.default)


def test_worker_default_checkpoint_interval_matches_the_catalogue():
    """The worker's own fallback checkpoint interval equals the profile
    catalogue's default, and dispatch passes it as CHECKPOINT_INTERVAL_SECONDS."""
    default = _profile_default_interval()
    config, field = "apps/agent-worker/agent_worker/config.py", "WorkerConfig.checkpoint_interval_seconds"
    assert f"checkpoint_interval_seconds: int = {default}" in _cited_symbol(config, field)
    assert '"CHECKPOINT_INTERVAL_SECONDS"' in _cited_symbol(DISPATCH_PY, "worker_env")


# --------------------------------------------------------------------------
# D10: dispatch-and-integration.md §6
# --------------------------------------------------------------------------


def test_dispatch_routes_serve_runtimes_and_resource_classes():
    """GET /v1/runtimes and GET /v1/resource-classes are real routes."""
    platform = "apps/swarm-api/swarm_api/routes/platform.py"
    assert _cited_symbol(platform, "runtimes").startswith('@router.get("/runtimes"')
    assert _cited_symbol(platform, "resource_classes").startswith('@router.get("/resource-classes")')


# --------------------------------------------------------------------------
# D10: web-ui README "Blocked" and redesign-v2 "Still open"
# --------------------------------------------------------------------------


def test_the_routes_the_ui_blocked_list_cites_serve_their_paths():
    """Admin leases, account authorize and task attempts routes exist at the paths the UI doc names."""
    for cite, needle in (
        (("apps/swarm-api/swarm_api/routes/admin.py", "list_leases"), "/leases"),
        (("apps/swarm-api/swarm_api/routes/accounts.py", "begin_sign_in"), "/authorize"),
        (("apps/swarm-api/swarm_api/routes/tasks.py", "list_attempts"), "/attempts"),
    ):
        assert needle in _cited_symbol(*cite).splitlines()[0], cite


# --------------------------------------------------------------------------
# S7 and S10: BUILD_PROMPT_V2 §2.4 and §2.6.1
# --------------------------------------------------------------------------


def test_the_remote_step_agent_call_is_sc_remote():
    """A remote workflow step dispatches via agentType 'sc:remote', the agent
    plugin/agents/remote.md registers as `remote`."""
    assert "name: remote" in _text(REPO / "plugin" / "agents" / "remote.md")
    anchor = "and its prompt is what the remote agent is told"
    assert "agentType: 'sc:remote'" in _cited_anchor("plugin/README.md", anchor)


def test_account_add_drives_the_oauth_authorize_and_exchange_routes():
    """`sc account add` calls the API's OAuth routes, not a vault upload."""
    sc, accounts = "apps/swarm-mcp/swarm_mcp/sc.py", "apps/swarm-api/swarm_api/routes/accounts.py"
    assert _cited_symbol(accounts, "begin_sign_in").startswith('@router.post("/authorize")')
    assert _cited_symbol(accounts, "finish_sign_in").startswith('@router.post("/exchange"')
    assert 'ac_sub.add_parser("add"' in _cited_symbol(sc, "build_parser")


# --------------------------------------------------------------------------
# OD-B18-2: the divergences nobody recorded
# --------------------------------------------------------------------------


def test_agent_runtime_image_does_not_install_claudeswitch():
    """No pod or service runs claudeswitch; accounts are added via the API's OAuth flow."""
    assert "claudeswitch" not in _text(REPO / "images" / "agent-runtime-base" / "Dockerfile")


def test_lease_has_no_account_field_and_the_worker_reads_the_pinned_env_var():
    """Capacity accounting never touches the account hold (invariant 2/3); the
    worker reads the token from the env var named by ACCOUNT_TOKEN_ENV."""
    from swarm_common.models import Lease

    assert not any("account" in f.name for f in dataclasses.fields(Lease))
    accountlease = "apps/agent-worker/agent_worker/accountlease.py"
    # The variable's NAME, built from pieces: no value is involved here.
    variable = "CLAUDE_CODE_OAUTH_" + "TOK" + "EN"
    name = "ACCOUNT_" + "TOK" + "EN_ENV"
    assert _cited_symbol(accountlease, name).splitlines()[-1] == f'{name} = "{variable}"'


def test_the_runtime_image_and_pod_security_context_run_as_non_root():
    """gVisor was not requested (CONTRACT.md amendments) because nothing in
    the runtime image or the GKE pod security context runs as root."""
    dockerfile = "images/agent-runtime-base/Dockerfile"
    assert _cited_anchor(dockerfile, "USER swarm:swarm").strip() == "USER swarm:swarm"
    assert '"runAsNonRoot": True' in _cited_symbol(DISPATCH_PY, "POD_SECURITY_CONTEXT")


# --------------------------------------------------------------------------
# Spend is recorded on every exit, and the coverage shape is the route's
# --------------------------------------------------------------------------


def test_spend_coverage_shape_has_exactly_these_fields():
    """The `coverage` block `list_attempts` returns, pinned so a field renamed here is caught."""
    from swarm_api.routes.attempts import spend_coverage

    assert set(spend_coverage([], {}).keys()) == {
        "scope", "attempts", "finished", "with_spend", "in_flight_unreported",
        "not_reported_by_profile", "not_recorded", "profile_unknown",
    }


# --------------------------------------------------------------------------
# every citation these documents make resolves, and none is a line number
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", TOUCHED, ids=lambda p: p.name)
def test_every_citation_is_a_symbol_or_an_anchor_that_resolves(doc: Path):
    """No `path:N` (a merge above the code moves it); every `path::qualname` and `path` (`text`) resolves."""
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# HW-DOCS: a workflow step waits for a tenant member's answer
# --------------------------------------------------------------------------
#
# docs/workflows.md and docs/notifications.md describe the await-input step
# that HW-SCHED, HW-API-SUBMIT, HW-WORKER, HW-API-ANSWER and HW-NOTIFY built.
# Every bound, type and name they state is read from the code here, so a
# change to the 24-hour bound, the 16 MiB answer, the ten files, the closed
# type list or the three forms is held on the code below;
# the documents' wording is not (issue 71).

WORKFLOWS = DOCS / "workflows.md"
NOTIFICATIONS = DOCS / "notifications.md"
HUMAN_STEPS = DOCS / "design" / "human-steps.md"
MIB = 1024 * 1024


def test_await_input_max_seconds_matches_the_schedulers_park_bound():
    """The API's await-input bound is the scheduler's HUMAN_PARK_MAX_SECONDS,
    and it is a whole number of hours."""
    from scheduler.loop import HUMAN_PARK_MAX_SECONDS
    from swarm_api.validation import AWAIT_INPUT_MAX_SECONDS

    assert AWAIT_INPUT_MAX_SECONDS == HUMAN_PARK_MAX_SECONDS
    assert AWAIT_INPUT_MAX_SECONDS % 3600 == 0


def test_answer_byte_bounds_nest_text_inside_form_inside_total():
    """A text payload fits inside the form body, which fits inside the overall
    answer: MAX_TEXT_BYTES <= MAX_FORM_BYTES <= MAX_ANSWER_BYTES, and the total
    is a whole number of MiB."""
    from swarm_api import awaitinput

    assert awaitinput.MAX_TEXT_BYTES <= awaitinput.MAX_FORM_BYTES <= awaitinput.MAX_ANSWER_BYTES
    assert awaitinput.MAX_ANSWER_BYTES % MIB == 0


def test_await_input_forms_and_refusal_codes_match_the_code():
    """AWAIT_INPUT_FORMS is exactly the notification titles' keys, and every
    refusal reason the workflow raises is a real code in the source."""
    from swarm_api import awaitinput
    from swarm_api.notifications import _INPUT_TITLES
    from swarm_api.validation import AWAIT_INPUT_FORMS

    assert set(AWAIT_INPUT_FORMS) == set(_INPUT_TITLES)
    source = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "awaitinput.py")
    source += _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "await_input.py")
    for reason in ("person_required", "not_a_member", "membership_unavailable", "not_open",
                   "already_answered", "spec_refused", "too_many_files", "invalid_file_name",
                   "file_type_refused", "file_contains_credential", "answer_too_large",
                   "answer_busy", "store_unavailable"):
        assert f'"{reason}"' in source, reason
    assert awaitinput.AWAIT_INPUT_EXPIRED_ERROR == "await_input_expired:"
    assert '{AWAIT_INPUT_EXPIRED_ERROR} nobody answered within' in source


def test_notifications_sources_registers_awaiting_input():
    """`awaiting_input` is a registered notification source kind."""
    from swarm_api.notifications import SOURCES

    assert "awaiting_input" in dict(SOURCES)


@pytest.mark.parametrize("doc", (WORKFLOWS, NOTIFICATIONS), ids=lambda p: p.name)
def test_await_input_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# MCP-DOCS: a task's agent queries its caller's MCP server
# --------------------------------------------------------------------------
#
# docs/design/registered-mcp-servers.md was written before any lane built it.
# Its §8 records what each build-plan lane landed, §4.8 the owner's amendment
# of 2026-10-18 to the relay's `credential_in_request` check, and §1.2 the
# `.mcp.json` question MCP-RUNNERS answered with a test. docs/security.md
# carries the threat model. Every rule name, route, purpose and flag they
# state is read from the code here.

MCP_DESIGN = DOCS / "design" / "registered-mcp-servers.md"
SECURITY = DOCS / "security.md"


def _rule_fires(rule, text: str) -> bool:
    if rule.apply is not None:
        return bool(rule.apply(text, True, False)[1])
    return bool(rule.pattern.search(text))


def test_admission_can_fail_ready_and_parked_tasks_without_a_lease():
    """READY -> FAILED and PARKED -> FAILED (contract request 99) let admission
    fail an unrunnable task before any lease, via Scheduler._fail_unrunnable."""
    from swarm_common.states import TaskState, can_transition

    assert can_transition(TaskState.READY, TaskState.FAILED)
    assert can_transition(TaskState.PARKED, TaskState.FAILED)
    fail = "apps/scheduler/scheduler/loop.py::Scheduler._fail_unrunnable"
    assert "self._store.fail_before_lease(" in _cited_symbol(*fail.split("::"))


def test_mcp_servers_flag_is_swarm_mcp_servers():
    """The MCP servers feature is gated by the SWARM_MCP_SERVERS flag."""
    assert '"SWARM_MCP_SERVERS"' in _cited_symbol("apps/swarm-api/swarm_api/mcp_servers.py", "FLAG")


def test_mcp_relay_shape_exempt_rules_pass_their_examples_but_other_credentials_still_fire():
    """SHAPE_EXEMPT names two swarm_redaction rules the relay never blocks on
    shape alone; a registered token and an Authorization-scheme value still
    fire the relay's own credential_rule check."""
    from agent_worker import mcp_relay
    from swarm_redaction import RULES

    by_name = {rule.name: rule for rule in RULES}
    assert len(mcp_relay.SHAPE_EXEMPT) == 2
    for name in mcp_relay.SHAPE_EXEMPT:
        assert name in by_name, f"{name} is not a swarm_redaction rule"
    prose = " ".join(("a", "basic", "configuration", "with", "a", "bearer", "authentication", "step"))
    for example, rule in (("token: 4000", "key_value_assignment"),
                          ("password: field", "key_value_assignment"),
                          (prose, "http_authorization")):
        assert _rule_fires(by_name[rule], example), (example, rule)
        assert mcp_relay.credential_rule(example) is None, example
    assert not any(_rule_fires(rule, "token_budget: 4000") for rule in RULES)
    assert mcp_relay.credential_rule("ghp_" + "a" * 36) == "github_token"
    assert mcp_relay.credential_rule("Authorization: token " + "b" * 8) == "http_authorization_scheme"


def test_claude_code_runner_never_passes_strict_mcp_config():
    """The claude-code runner launches without --strict-mcp-config, so an
    attached MCP server can still win a name the repository also declares."""
    runner = _text(REPO / "apps" / "agent-worker" / "agent_worker" / "runners" / "claude_code.py")
    assert '"--strict-mcp-config"' not in runner


def test_mcp_attach_purpose_is_pinned():
    """The MCP attach token's purpose string is swarm-mcp-attach/v1."""
    attach = "apps/swarm-api/swarm_api/routes/attempts_mcp.py::ATTACH_PURPOSE"
    assert '"swarm-mcp-attach/v1"' in _cited_symbol(*attach.split("::"))


def test_credential_serving_routes_count_is_two():
    """Exactly two routes serve credential material (CREDENTIAL_SERVING_ROUTES)."""
    routes = _cited_symbol("tests/unit/control_plane/test_no_route_serves_credentials.py",
                           "CREDENTIAL_SERVING_ROUTES")
    paths = re.findall(r'\("POST", "([^"]+)"\)', routes)
    assert len(paths) == 2, paths


@pytest.mark.parametrize("doc", (MCP_DESIGN, SECURITY), ids=lambda p: p.name)
def test_mcp_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# MODEL-DOCS: a workflow step picks its model class by name
# --------------------------------------------------------------------------
#
# docs/design/model-per-step.md was written before any lane built it. Its §8
# records what each build-plan lane landed and where the code departs from
# the design (format 5, not 4; no `model_classes_enabled` flag, owner decision
# R4). docs/agent-output.md, docs/workflows.md and docs/quota-management.md
# describe the built feature. Every class, pin, refusal code and format they
# state is read from the code here.

MODEL_DESIGN = DOCS / "design" / "model-per-step.md"
AGENT_OUTPUT = DOCS / "agent-output.md"
QUOTA = DOCS / "quota-management.md"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"


def _runner_models() -> dict[str, str]:
    """`local.runner_models` in terraform/infra/locals.tf: class -> pinned ID."""
    block = re.search(r"^  runner_models = \{\n(.*?)^  \}", _text(LOCALS_TF), flags=re.M | re.S)
    assert block, "terraform/infra/locals.tf has no runner_models block"
    return dict(re.findall(r'^\s+(\w+)\s+=\s+"([^"]+)"', block.group(1), flags=re.M))


def test_runner_models_are_keyed_by_exactly_the_frozen_classes():
    """The pin map every model test below reads, held to the vocabulary."""
    from swarm_common.profiles import MODEL_CLASSES

    models = _runner_models()
    assert set(models) == set(MODEL_CLASSES)
    for name, model_id in models.items():
        assert MODEL_CLASSES[name].admits(model_id), (name, model_id)


def test_model_class_signing_format_is_5_and_below_the_current_format():
    """A plan with model_class signs at format 5 (contract request 96); the
    live spec format has since moved past it (request 120, the budget)."""
    from swarm_common.specsign import SPEC_FORMAT, signing_format

    assert signing_format({"model_class": "haiku"}) == 5 < SPEC_FORMAT


def test_no_model_classes_enabled_flag_exists_anywhere():
    """R4: model classes ship with no feature flag; nothing under apps/ or
    terraform/ reads MODEL_CLASSES_ENABLED."""
    for root in ("apps", "terraform"):
        for path in (REPO / root).rglob("*"):
            if path.suffix in (".py", ".tf", ".ts", ".tsx") and "node_modules" not in path.parts:
                assert "MODEL_CLASSES_ENABLED" not in path.read_text(encoding="utf-8"), path


def test_model_classes_frozen_count_and_dispatch_pin_the_model():
    """There are exactly four model classes; dispatch, specverify and the
    worker's mismatch note and attempt-start record all key off the model."""
    from swarm_common.models import Attempt
    from swarm_common.profiles import MODEL_CLASSES

    assert len(MODEL_CLASSES) == 4
    step = "apps/scheduler/scheduler/dispatch.py::step_model"
    assert "step_model_class(" in _cited_symbol(*step.split("::"))
    check = "apps/agent-worker/agent_worker/specverify.py::check_model"
    assert ".admits(" in _cited_symbol(*check.split("::"))
    note = "apps/agent-worker/agent_worker/spend.py::SpendMixin._note_model_mismatch"
    assert '"model_mismatch' in _cited_symbol(*note.split("::"))
    assert "model" in {f.name for f in dataclasses.fields(Attempt)}
    start = "apps/agent-worker/agent_worker/control.py::ControlPlane.record_attempt_start"
    assert 'record["model"]' in _cited_symbol(*start.split("::"))


def test_profiles_that_support_every_model_class_and_the_choice_switch_default():
    """Exactly these profiles accept every model class; the model_class
    refusal switch (ModelIsChosenByModelClass) ships off, report-only."""
    from swarm_api import refusals
    from swarm_api.validation import ModelIsChosenByModelClass
    from swarm_common.profiles import MODEL_CLASSES

    every = [p.name for p in RUNNER_PROFILES.values() if p.model_classes == tuple(MODEL_CLASSES)]
    assert sorted(every) == [
        "claude-code", "claude-code-browser", "claude-code-large", "claude-code-lightpanda", "claude-code-medium",
        "indexer",
    ], every
    assert refusals.SWITCHES[ModelIsChosenByModelClass.code].default is False


def test_quota_window_binding_and_headroom_ignore_model_class():
    """Every quota window binds every attempt regardless of model class:
    Account._binding_remaining never looks at class, and headroom() doesn't
    take model_class as a parameter."""
    from quota_broker import accounts

    binding = "apps/quota-broker/quota_broker/accounts.py::Account._binding_remaining"
    source = _cited_symbol(*binding.split("::"))
    assert "self.windows.values()" in source and "class" not in source
    assert "model_class" not in accounts.Account.headroom.__code__.co_varnames


@pytest.mark.parametrize("doc", (MODEL_DESIGN, AGENT_OUTPUT, QUOTA), ids=lambda p: p.name)
def test_model_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# WK-DOCS: an admin creates a personal, group or service
# workspace from Admin › Tenants, and the in-cloud job builds it
# --------------------------------------------------------------------------
#
# docs/design/workspace-kinds.md was written before any lane built it. Its §8
# records what each build-plan lane landed and where the code departs from the
# design. docs/workspaces.md records WD3's reversal, the new failure codes and
# the owner's check O1 in §0; docs/multi-tenancy.md the three admission places
# a job-made service account now passes without Terraform;
# docs/runbooks/tenant-offboarding.md what disabling and removing a group or
# service workspace takes while no deprovisioning run exists (Q9 (a)). Every
# step, code, route and name they state is read from the code here.

KINDS_DESIGN = DOCS / "design" / "workspace-kinds.md"
WORKSPACES_DOC = DOCS / "workspaces.md"
OFFBOARDING = DOCS / "runbooks" / "tenant-offboarding.md"
REGISTER = "scripts/register-tenant.sh"
#: The failure codes §4.8 of the design added, each written by the job.
KINDS_CODES = ("SERVICE_ACCOUNT_REFUSED", "DIRECTORY_CHECK_STALE", "FRONT_DOOR_OWNER",
               "FRONT_DOOR_FAILED", "NAMESPACE_LABEL_MISSING")


def test_register_tenant_script_runs_the_service_kind_steps_in_order():
    """register-tenant.sh's service-kind job runs these steps in this order."""
    order = "printf '%s\\n' A1 S1 A2 A3 A4 A5 A6 A7 A8 S2 A9"
    _cited_anchor(REGISTER, order)


def test_platform_owner_check_reads_the_deployments_tfvars_not_entrypy():
    """Departure from the design: A1's owner check reads the deployment's own
    tfvars via deployment_setting, not entry.py's environment."""
    entry = _text(REPO / "images" / "workspace-apply" / "entry.py")
    assert "PLATFORM_OWNER" not in entry
    assert "platform_owner" in _cited_anchor(REGISTER, 'owner="$(deployment_setting platform_owner)"')


def test_service_admission_and_registry_revalidation_departures():
    """A service is admitted by its unique id, not its address alone, and the
    registry re-checks every ready record against the request's own rules."""
    admits = "apps/swarm-api/swarm_api/auth.py::Authenticator._admits_service"
    assert "subject != pinned" in _cited_symbol(*admits.split("::"))
    snap = "apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry"
    assert "validate_kind_request" in _cited_symbol(*snap.split("::"))


def test_every_new_workspace_kind_failure_code_has_copy():
    """Each of the five workspace-kinds failure codes has user-facing copy."""
    from swarm_api.workspaces import FAILURE_COPY

    for code in KINDS_CODES:
        assert code in FAILURE_COPY, f"{code} has no copy"


def test_tenant_groups_priority_order_puts_configured_groups_first():
    """Authenticator._tenant_groups orders configured groups before any discovered ones."""
    assert "configured + tuple(" in _cited_symbol("apps/swarm-api/swarm_api/auth.py", "Authenticator._tenant_groups")


def test_workspace_registry_read_counts_only_ready_records():
    """WorkspaceRegistry._read counts only READY records (why moving a record
    off READY is the immediate offboarding stop)."""
    assert "READY" in _cited_symbol("apps/swarm-api/swarm_api/registry.py", "WorkspaceRegistry._read")


@pytest.mark.parametrize("doc", (KINDS_DESIGN, WORKSPACES_DOC, TENANCY, OFFBOARDING), ids=lambda p: p.name)
def test_workspace_kinds_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# ROLES-DOCS: a workspace's people are viewers, members or
# admins, and a platform admin reaches every group workspace
# --------------------------------------------------------------------------
#
# docs/design/workspace-roles.md was written before any lane built it. Its §8
# records what each build-plan lane landed, the two lanes the owner added on
# 2026-10-19 (WK-REACH, ROLES-ADMIN-LIST), where the code departs from the
# design and what is still to do. human-steps.md §4.2 and workspace-secrets.md
# Q4 are the two decisions Q5 and Q6 superseded (workspace-roles.md §4.2.3,
# §4.1); docs/security.md carries the threat model and docs/workspaces.md the
# Admin › People card. Every code, switch, constant and name they state is
# read from the code here, so a switch turned on, a gap closed or a probe
# added is held on the code below;
# the documents' wording is not (issue 71).

ROLES_DESIGN = DOCS / "design" / "workspace-roles.md"
SECRETS_DESIGN = DOCS / "design" / "workspace-secrets.md"
ROLES_PY = "apps/swarm-api/swarm_api/roles.py"
SETTINGS_PY = "apps/swarm-api/swarm_api/settings.py"
ANSWER_GATE = "apps/swarm-api/swarm_api/awaitinput.py::member_refusal"
SECRETS_GATE = "apps/swarm-api/swarm_api/workspace_secrets.py::require_manager"
#: The two refusal codes on existing routes, one switch each (§4.9, Q10).
ROLE_CODES = ("role_member_required", "role_admin_required")


def test_role_refusal_switches_and_platform_admin_reach_ship_off():
    """role_member_required, role_admin_required and platform_admin_reach all default off."""
    from swarm_api import refusals

    for code in ROLE_CODES:
        assert refusals.SWITCHES[code].default is False, code
    assert "= False" in _cited_symbol(SETTINGS_PY, "ApiSettings.platform_admin_reach")


def test_role_grant_mac_domain_and_scan_limit():
    """The role-grant MAC's domain separator and the registered-groups scan limit (PAGE_SIZE * PAGES)."""
    from swarm_api import roles

    assert roles._MAC_DOMAIN == b"swarm-workspace-role-grant/v1\n"
    assert roles.SCAN_LIMIT == roles.PAGE_SIZE * roles.PAGES


def test_role_store_write_is_a_single_batch():
    """RoleStore.write commits its grants in one Firestore batch."""
    assert ".batch()" in _cited_symbol(ROLES_PY, "RoleStore.write")


def test_role_grant_required_fallback_code_was_removed():
    """role_grant_required was never needed and is gone from the role routes."""
    for path in (ROLES_PY, "apps/swarm-api/swarm_api/routes/roles.py"):
        assert "role_grant_required" not in _text(REPO / path)


def test_await_input_member_refusal_gate_checks_fresh_membership():
    """member_refusal requires a fresh MEMBER role check before a person may answer."""
    gate = _cited_symbol(*ANSWER_GATE.split("::"))
    assert "may_act_in(" in gate and "roles.MEMBER" in gate and "fresh=True" in gate


def test_workspace_secrets_require_manager_checks_admin_role_and_fresh_read():
    """require_manager gates on workspace_admin for non-personal workspaces, read fresh."""
    gate = _cited_symbol(*SECRETS_GATE.split("::"))
    assert "workspace_admin(" in gate and "is_personal(" in gate
    admin = "apps/swarm-api/swarm_api/workspace_secrets.py::workspace_admin"
    assert "fresh=True" in _cited_symbol(*admin.split("::"))


def test_reachable_groups_filters_to_tenant_prefix_and_current_auth_audits_admin():
    """_reachable_groups keeps only `u-` tenant groups; current_auth calls audit_platform_admin()."""
    reach = "apps/swarm-api/swarm_api/auth.py::Authenticator._reachable_groups"
    assert 'startswith("u-")' in _cited_symbol(*reach.split("::"))
    audit = "apps/swarm-api/swarm_api/deps.py::current_auth"
    assert "audit_platform_admin(" in _cited_symbol(*audit.split("::"))


@pytest.mark.parametrize("doc", (ROLES_DESIGN, HUMAN_STEPS, SECRETS_DESIGN, SECURITY, WORKSPACES_DOC),
                         ids=lambda p: p.name)
def test_workspace_roles_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# QI-DOCS: a browser agent signs in to the console as the QA
# identity, and the documents say what was built
# --------------------------------------------------------------------------
#
# docs/design/qa-identity.md was written before any lane built it. Its §8
# records what each build-plan lane landed and where the code departs from
# the design; §4.9 and §6 carry the owner's 2026-10-20 decision that M2 is
# not a gate before the build. docs/design/live-browser.md §5.5.1 notes the
# console host the session carry leaves out, and docs/workspaces.md §6.6 the
# QA account's service workspace. Every header, constant, route, code and
# name they state is read from the code here, so a lane landing or a value
# changing is held on the code below;
# the documents' wording is not (issue 71).

QI_DESIGN = DOCS / "design" / "qa-identity.md"
LIVE_BROWSER = DOCS / "design" / "live-browser.md"
QI_GATE_PY = "apps/agent-worker/agent_worker/livegate/qaidentity.py"
QI_WORKER_PY = "apps/agent-worker/agent_worker/qa_identity.py"
QI_API_PY = "apps/swarm-api/swarm_api/qa_identity.py"
QI_SESSION_PY = "apps/agent-worker/agent_worker/livegate/session.py"
QI_FRONT_DOOR = "apps/swarm-api/swarm_api/main.py::FrontDoor.__call__"
QI_INSTRUCT_PY = "apps/agent-worker/agent_worker/runners/claude_code.py"


def test_claude_code_runners_live_browser_note_mentions_qa_identity_host():
    """QA_IDENTITY_NOTE (the claude-code runner's live-browser instructions) names qa_identity_host."""
    note = f"{QI_INSTRUCT_PY}::QA_IDENTITY_NOTE"
    assert "qa_identity_host" in _cited_symbol(*note.split("::"))


def test_qa_gate_header_is_authorization_and_credential_headers_include_it():
    """The QA gate's injected header is `Authorization` (Chromium refuses
    Proxy-Authorization from script); swarm-api's CREDENTIAL_HEADERS strips it too."""
    header = f"{QI_GATE_PY}::HEADER"
    assert '"Authorization"' in _cited_symbol(*header.split("::"))
    removed = "apps/swarm-api/swarm_api/main.py::CREDENTIAL_HEADERS"
    assert 'b"authorization"' in _cited_symbol(*removed.split("::"))


def test_qa_identity_renews_at_one_third_of_its_lifetime():
    """The QA identity worker renews its token at RENEW_FRACTION = 1/3 of its lifetime."""
    renew = f"{QI_WORKER_PY}::RENEW_FRACTION"
    assert "1 / 3" in _cited_symbol(*renew.split("::"))


def test_front_door_logs_proxy_authorization_present():
    """FrontDoor.__call__ records whether a Proxy-Authorization header reached it (M2)."""
    assert "proxy_authorization_present=" in _cited_symbol(*QI_FRONT_DOOR.split("::"))


def test_session_carry_excludes_the_qa_identity_host_at_every_hop():
    """SessionCarry._excluded and import_jar enforce the exclusion; LiveGate.pause checks qa_identity_host too."""
    excluded = f"{QI_SESSION_PY}::SessionCarry._excluded"
    assert "sent_to" in _cited_symbol(*excluded.split("::"))
    imported = f"{QI_SESSION_PY}::SessionCarry.import_jar"
    assert "self._excluded" in _cited_symbol(*imported.split("::"))
    refused = "apps/agent-worker/agent_worker/livegate/__main__.py::LiveGate.pause"
    assert "qa_identity_host" in _cited_symbol(*refused.split("::"))


def test_qa_console_service_account_is_dev_only_and_lends_no_account():
    """The qa-console GSA is created only when var.environment == \"dev\"; a QA
    identity submission parks CREDENTIAL_MISSING at admission rather than being
    lent an account, and Workspaces.applies_to treats it as a service identity."""
    locals_tf = _text(LOCALS_TF)
    assert '"qa-console@${var.project_id}.iam.gserviceaccount.com"' in locals_tf
    assert 'var.environment == "dev"' in locals_tf
    gate = "apps/swarm-api/swarm_api/workspaces.py::Workspaces.applies_to"
    assert "is_service_identity" in _cited_symbol(*gate.split("::"))
    admit = "apps/scheduler/scheduler/loop.py::Scheduler._prepare_admission"
    assert "ParkReason.CREDENTIAL_MISSING" in _cited_symbol(*admit.split("::"))


def test_qa_identity_own_tenant_and_routes():
    """own_tenant resolves via tenant_id_for_user; the mine and grants routes exist."""
    own = f"{QI_API_PY}::own_tenant"
    assert "tenant_id_for_user" in _cited_symbol(*own.split("::"))
    routes = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "qa_identity.py")
    for path in ("/qa-identities/mine", "/admin/qa-identities/{identity}/grants/{tenant_id}"):
        assert f'"{path}"' in routes, path


@pytest.mark.parametrize("doc", (QI_DESIGN, LIVE_BROWSER, WORKSPACES_DOC), ids=lambda p: p.name)
def test_qa_identity_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)



# --------------------------------------------------------------------------
# Runner sizes: docs/design/runner-sizes.md §6 lane SIZES-DOCS
# --------------------------------------------------------------------------
#
# The design's status line said "designed" while nine lanes of §6 shipped;
# architecture.md still said "requests == limits, no bursting" and gave the
# browser class as 8 / 16; kubernetes/README.md said only `browser` reaches
# Autopilot; and locals.tf called its four-field table "byte-identical" to a
# catalogue that now has a CPU limit. Each test reads the value from the code.

ARCHITECTURE = DOCS / "architecture.md"
K8S_README = REPO / "kubernetes" / "README.md"


def test_gke_manifest_computes_cpu_via_cpu_quantity():
    """The GKE pod manifest renders CPU requests/limits through cpu_quantity(),
    which enforces CPU_BURST_MAX_RATIO (invariant 7)."""
    assert "cpu_quantity(" in _cited_symbol(DISPATCH_PY, MANIFEST)


def test_admin_runner_size_routes_and_dispatch_pin_points_exist():
    """The admin runner-sizes routes exist; the scheduler reads the current
    version at admission and pins a running task to the version it was
    admitted with."""
    admin = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "admin.py")
    for route in ("/runner-sizes", "/runner-sizes:preview", "/runner-sizes/restore", "/runner-sizes/versions"):
        assert f'"{route}"' in admin, route
    current = "apps/scheduler/scheduler/loop.py::Scheduler._size_version"
    pinned = "apps/scheduler/scheduler/loop.py::Scheduler._prepare_admission"
    assert "sizes_mod.current(" in _cited_symbol(*current.split("::"))
    assert "sizes_mod.for_task(" in _cited_symbol(*pinned.split("::"))


def test_render_job_and_tenant_values_use_cpu_limit_cores():
    """kubernetes/render.py renders the resource class's cpu_limit_cores, and
    tenant quota uses QUOTA_CPU_LIMIT."""
    quota = "kubernetes/render.py::tenant_values"
    assert "QUOTA_CPU_LIMIT" in _cited_symbol(*quota.split("::"))
    job = "kubernetes/render.py::render_job"
    assert "rc.cpu_limit_cores" in _cited_symbol(*job.split("::"))


def test_locals_tf_resource_classes_table_matches_the_catalogue():
    """terraform/infra/locals.tf's resource_classes table carries the same
    cpu/memory_gib/disk_gib/units as swarm_common.profiles.RESOURCE_CLASSES."""
    from swarm_common.profiles import RESOURCE_CLASSES

    text = _text(LOCALS_TF)
    for name, rc in RESOURCE_CLASSES.items():
        assert re.search(rf"\b{name}\s*=\s*\{{ cpu = {rc.cpu:g}, memory_gib = {rc.memory_gib}, "
                         rf"disk_gib = {rc.disk_gib}, units = {rc.units} \}}", text), name


@pytest.mark.parametrize("doc", (ARCHITECTURE, K8S_README), ids=lambda p: p.name)
def test_runner_sizes_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# Test services: docs/design/test-services.md §11 (TS-DOCS-GUARDS)
# --------------------------------------------------------------------------

TS_DESIGN = DOCS / "design" / "test-services.md"
TS_DOC = DOCS / "test-services.md"
TS_PREPUBLISH_PY = "apps/agent-worker/agent_worker/prepublish.py"
#: A backticked citation into the tree, with or without `::qualname`.
_TS_CITE = re.compile(rf"`({_ROOTS}[\w./-]*?)(?:::([\w.]+))?`")


def test_testservices_refuses_every_platform_owned_key():
    """Invariant 10 (the platform owns image, command, resources and backend
    parameters): a declared service may not override these keys."""
    from agent_worker.testservices import _REFUSED_BECAUSE

    assert set(_REFUSED_BECAUSE) == {
        "image", "command", "args", "port", "env", "memory", "cpu", "settings", "init_sql", "user",
    }


def test_testservices_catalogue_and_prepublish_constants():
    """postgres is the only catalogued service, with 3 built-in databases; the
    prepublish gate's check name, probe command and advisory text are these
    fixed values."""
    from agent_worker import prepublish, testservices as ts

    assert set(ts.CATALOGUE) == {"postgres"}
    assert len(ts.INITDB_DATABASES) == 3
    assert prepublish.TEST_SERVICES_CHECK == "test-services"
    assert "graph-pytest" in prepublish.SERVICE_CHECKS
    probe = " ".join(prepublish.probe_argv(ts.DeclaredService("postgres", 16, ("x",))))
    assert f"-h {ts.LISTEN_HOST} -p {ts.LISTEN_PORT}" in probe
    assert 'prefix="pp-svc-"' in _cited_symbol(TS_PREPUBLISH_PY, "Gate._services")
    assert "(error, not the change's)" in _cited_symbol(TS_PREPUBLISH_PY, "advisory_section")
    assert "and not declared in" in _cited_symbol(TS_PREPUBLISH_PY, "_pytest_project")


def test_pytest_project_skip_reason_message():
    """_pytest_project's skip reason for an unreachable declared service names
    the setup failure and that CI still runs it."""
    source = _cited_symbol(TS_PREPUBLISH_PY, "_pytest_project")
    assert '"every selected test errored at setup connecting to it; CI runs it"' in source


def _ts_agent_output_part(text: str) -> str:
    """The test-services paragraph of agent-output.md's pre-publish section."""
    section = _section(text, "## Before the worker publishes: a rebase and the repository's own checks (SPEED-D1)")
    start = section.index("**A service the tests need**")
    return section[start: section.index("\n\n", start)]


#: Each doc, and the part of it that is the test-services doc's.
_TS_CITED = {
    "design-test-services.md": (TS_DESIGN, lambda t: t),
    "test-services.md": (TS_DOC, lambda t: t),
    "agent-output.md": (AGENT_OUTPUT, _ts_agent_output_part),
}


def _ts_assert_cites_resolve(text: str, scratch: Path) -> None:
    """The file's own `_assert_cites_resolve`, on a scratch copy of `text`,
    after holding that the copy cites something: an empty pass is not one."""
    assert _TS_CITE.search(text), f"{scratch.name}: no citation found, so nothing was checked"
    scratch.write_text(text, encoding="utf-8")
    _assert_cites_resolve(scratch)


@pytest.mark.parametrize("doc", sorted(_TS_CITED))
def test_test_services_docs_cite_only_what_resolves(doc: str, tmp_path: Path):
    path, part = _TS_CITED[doc]
    _ts_assert_cites_resolve(part(_text(path)), tmp_path / doc)


# --------------------------------------------------------------------------
# SSC-DOCS: a secret is the workspace's, a person's or a
# repository's, and GitHub Actions is a write-only destination
# --------------------------------------------------------------------------
#
# docs/design/secrets-scopes.md was written before any lane built it, and its
# §11 (the owner's answers of 2026-10-23) overrode §3 on the App, the environments
# and the seal. Its status line and §9 say which lanes are built, with their
# PRs; §12 records what each built and where the code departs from §2-§6.
# workspace-secrets.md's status line points at the extension, security.md
# carries the threat model and the accepted residual risk (Q3), and
# provider-keys.md §7 what the scopes change for its keys. Every permission,
# switch, limit and binding they state is read from the code here, so a
# switch turned on, a permission widened or step 5 shipping is held on the code
# below; the documents' wording is not (issue 71).

SSC_DESIGN = DOCS / "design" / "secrets-scopes.md"
PROVIDER_KEYS = DOCS / "provider-keys.md"
#: The new refusals the lanes shipped off (refusals.SWITCHES).
SSC_SWITCHES = ("github_admin_required", "grant_key_required", "qa_bypass_link_required")


def test_no_second_github_secrets_app_exists():
    """Q3 declined a second GitHub App: no github_secrets_app.tf and no GITHUB_SECRETS_APP_ID setting."""
    from swarm_api import settings

    assert not (REPO / "terraform" / "infra" / "github_secrets_app.tf").exists()
    assert "GITHUB_SECRETS_APP_ID" not in _text(Path(settings.__file__))


def test_secrets_tsx_mounts_the_actions_secrets_pane():
    """Config -> Secrets mounts SecretsActions (lane SECRETS-ACTIONS-MOUNT)."""
    assert "./SecretsActions" in _text(REPO / "apps" / "swarm-ui" / "src" / "Secrets.tsx")


def test_github_actions_secret_mints_grant_one_permission_each_never_administration():
    """Each Actions installation-token mint in forge_installation.py grants
    exactly one permission, write on secrets and environments, never administration."""
    from swarm_api.forge_installation import ACTIONS_PERMISSIONS

    assert all(len(perm) == 1 for perm in ACTIONS_PERMISSIONS.values())
    granted = {(k, v) for perm in ACTIONS_PERMISSIONS.values() for k, v in perm.items()}
    assert not any(k == "administration" for k, _ in granted)
    assert ("secrets", "write") in granted and ("environments", "write") in granted


def test_secrets_are_sealed_with_pynacl_and_form_repository_is_v2():
    """swarm-api seals values with PyNaCl's SealedBox and the grant-attest form is v2."""
    from swarm_api.grant_attest import FORM_REPOSITORY

    assert "pynacl" in _text(REPO / "apps" / "swarm-api" / "pyproject.toml").lower()
    assert "SealedBox" in _cited_symbol("apps/swarm-api/swarm_api/actions_secrets.py", "seal")
    assert FORM_REPOSITORY == "v2"


def test_secrets_scopes_switches_and_limits_ship_off():
    """The three new refusal switches default off, a transaction tops out at
    500 writes, and the platform-secret-status Terraform variable defaults false."""
    from swarm_api import refusals, workspace_secrets

    for code in SSC_SWITCHES:
        assert refusals.SWITCHES[code].default is False, code
    assert workspace_secrets.MAX_TRANSACTION_WRITES == 500
    tf = _text(REPO / "terraform" / "bootstrap" / "platform_secret_status.tf")
    assert re.search(r'variable "enable_platform_secret_status" \{.*?default\s*=\s*false', tf, flags=re.S)


@pytest.mark.parametrize("doc", (SSC_DESIGN, SECRETS_DESIGN, SECURITY, PROVIDER_KEYS), ids=lambda p: p.name)
def test_secrets_scopes_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# -- QAENV-DOCS: one visual-QA dispatch over an app's environments ----------
#
# docs/design/qa-environments.md §8's phase 4, after QAENV-MODEL, -GATE,
# -CRAWL, -RUN, -SETTLE, -UI-RUNPAGE, -UI-FORM and -SC merged. The design's
# status said "DESIGN, not built", visual-qa-any-app.md still refused a
# discovering target with nothing stored (`qa_target_no_screens`, "press Run
# discovery"), and schedules.md §3.17 still gave the `visual-qa` caps as $80 a
# day and one at a time, which the owner changed on 2026-10-23 (§10, Q9 (a)). Each
# figure below is read from the code that holds it.

QAENV_DESIGN = DOCS / "design" / "qa-environments.md"
QAENV_ANY_APP = DOCS / "design" / "visual-qa-any-app.md"
QAENV_QASPEC = "apps/swarm-api/swarm_api/qaspec.py"
QAENV_VISUAL_QA = "apps/swarm-api/swarm_api/schedtypes/visual_qa.py"


def _qaenv_caps() -> tuple[int, int, int]:
    source = _cited_symbol(QAENV_VISUAL_QA, "BUDGET_CAPS")
    match = re.search(r"per_run_usd=(\d+), per_day_usd=(\d+), max_concurrent=(\d+)", source)
    assert match, "BUDGET_CAPS is not in the shape this test reads"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def test_qaspec_screens_reads_the_targets_screens_field():
    """_screens reads `screens` straight off the target when the target sets it."""
    assert 'raw = target.get("screens")' in _cited_symbol(QAENV_QASPEC, "_screens")


def test_visual_qa_budget_caps_are_80_160_2():
    """visual_qa's BUDGET_CAPS: $80 a leg, $160 a day, 2 concurrent (owner, 2026-10-23, Q9 (a))."""
    per_run, per_day, concurrent = _qaenv_caps()
    assert (per_run, per_day, concurrent) == (80, 160, 2)


@pytest.mark.parametrize("doc", (QAENV_DESIGN, QAENV_ANY_APP), ids=lambda p: p.name)
def test_qa_environments_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# -- The visual-QA sign-in gate's waits ------------------------------------
#
# livegate/qatarget.py and qa_target.py hold the gate's waits and retries as
# constants. FIRST_WAIT_SECONDS is built to cover the form, the slowest
# sign-in verdict and a fixed return margin; the reclick budget stays inside
# the form's own bound.


def test_qa_gate_wait_constants_relations():
    """FIRST_WAIT_SECONDS covers the form, the slowest sign-in verdict and a
    fixed return margin; the gate's reclicks stay inside the form's own bound."""
    from agent_worker.livegate.qatarget import FORM_SECONDS, MAX_RECLICKS, RECLICK_SECONDS, SIGNED_IN_MAX_SECONDS
    from agent_worker.qa_target import FIRST_WAIT_SECONDS

    assert FIRST_WAIT_SECONDS == FORM_SECONDS + SIGNED_IN_MAX_SECONDS + 5
    assert RECLICK_SECONDS * MAX_RECLICKS <= FORM_SECONDS
