"""The docs say what the code does: B18a, owner decisions of 2026-10-02.

WHY THIS EXISTS. Each test below pins one sentence a document got wrong, and
reads the value it describes from the code wherever it can, so the next change
to that value fails here instead of leaving the document quietly wrong again:

  * D8   docs/execution-backends.md named `gke-extended-run-time`; the pod
         carries `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`.
  * D10  /dev/shm was "1 GiB" (the dispatcher sizes it 2Gi); the GKE namespace
         was `swarm-<id>` (it is `swarm-tenant-<id>`); the checkpoint interval
         was "60" (the catalogue's default is 120); dispatch-and-integration §6
         said no route serves the topology (`GET /v1/runtimes` does); and the
         web-ui "Blocked" and "Still open" lists predate what shipped.
  * S7   BUILD_PROMPT_V2 §2.4: a step is `agent({agentType: 'sc:remote'})`.
  * S10  BUILD_PROMPT_V2 §2.6.1: `account add` drives the API's OAuth
         `/authorize` and `/exchange`, not claudeswitch plus a vault upload.
  * OD-B18-2: the spec's unrecorded divergences, each with the reason the code
         gives; and S35/S36 (§8's GKE benchmarks) marked not planned.

The section bodies are read with the same `_section` rule as
test_docs_spec_amendments.py, so a heading added inside a section would cut it.
"""

from __future__ import annotations

import dataclasses
import re
import sys
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
UI_AUDIT = DOCS / "web-ui" / "ui-audit-and-build-prompt.md"
DISPATCH = REPO / "apps" / "scheduler" / "scheduler" / "dispatch.py"

TOUCHED = (BUILD_PROMPT, BACKENDS, TENANCY, CHECKPOINTING, DESIGN, UI_README, REDESIGN)

STAMP = "2026-10-02"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


#: dispatch.py, as the docs cite it: `path::qualname` (#647, lane CITD).
DISPATCH_PY = "apps/scheduler/scheduler/dispatch.py"
MANIFEST = "GkeJobDispatcher._manifest"


def _heading(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


def _flat(markdown: str) -> str:
    """Prose with its wrapping and blockquote markers removed, so a phrase a
    test asserts on is found wherever the paragraph happens to break."""
    return " ".join(re.sub(r"^[ \t]*>[ \t]?", "", markdown, flags=re.M).split())


def _spec(prefix: str) -> str:
    text = _text(BUILD_PROMPT)
    return _flat(_section(text, _heading(text, prefix)))


# --------------------------------------------------------------------------
# D8 and D10: execution-backends.md
# --------------------------------------------------------------------------


def test_d8_the_gke_pod_annotation_is_the_one_the_dispatcher_sets():
    text = _text(BACKENDS)
    # The made-up key survives only as the record of what the line used to say.
    assert "`cloud.google.com/gke-extended-run-time: \"true\"`" not in text
    assert "is not a real annotation" in _flat(text)
    assert '`cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`' in text
    assert f"`{DISPATCH_PY}::{MANIFEST}`" in text
    # The annotation IS the extended-run-time request (the reason Spot is
    # impossible, invariant 6): the doc must not say the platform lacks it.
    flat = _flat(text)
    assert "This annotation IS Autopilot's extended-run-time request" in flat
    assert "node auto-upgrade for up to seven days" in flat
    assert "only on on-demand capacity" in flat
    assert "Autopilot extended run time" in _cited_symbol(DISPATCH_PY, MANIFEST)
    assert "and this platform sets it" in flat
    assert '"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"' in _cited_symbol(DISPATCH_PY, MANIFEST)


def _dshm_gib() -> int:
    """The size the GKE dispatcher gives Chromium's /dev/shm, read from the code."""
    match = re.search(
        r'\{"name": "dshm", "emptyDir": \{\s*"medium": "Memory", "sizeLimit": "(\d+)Gi"\}',
        _text(DISPATCH),
    )
    assert match, "the dispatcher no longer mounts a memory-backed dshm volume"
    return int(match.group(1))


def test_d10_dev_shm_is_the_size_the_dispatcher_renders():
    text = _text(BACKENDS)
    size = _dshm_gib()
    assert "1 GiB tmpfs" not in text
    assert f"{size} GiB tmpfs" in text
    assert f"`{DISPATCH_PY}::{MANIFEST}`" in text
    assert f'"sizeLimit": "{size}Gi"' in _cited_symbol(DISPATCH_PY, MANIFEST)


# --------------------------------------------------------------------------
# D10: multi-tenancy.md
# --------------------------------------------------------------------------


def test_d10_the_compute_row_names_the_namespace_the_dispatcher_uses():
    compute = _section(_text(TENANCY), "### Compute")
    assert "namespace `swarm-<id>`" not in compute
    assert "namespace `swarm-tenant-<id>`" in compute
    assert '"swarm-tenant-{tenant}"' in _text(DISPATCH)
    assert f"`{DISPATCH_PY}::GkeJobDispatcher.namespace_for`" in compute
    assert "swarm-tenant-{tenant}" in _cited_symbol(DISPATCH_PY, "GkeJobDispatcher.namespace_for")


# --------------------------------------------------------------------------
# D10: checkpointing.md
# --------------------------------------------------------------------------


def _profile_default_interval() -> int:
    field = next(f for f in dataclasses.fields(RunnerProfile) if f.name == "checkpoint_interval_seconds")
    return int(field.default)


def test_d10_the_checkpoint_interval_is_the_catalogues_not_sixty():
    text = _text(CHECKPOINTING)
    default = _profile_default_interval()
    assert "CHECKPOINT_INTERVAL_SECONDS=60 " not in text
    assert f"CHECKPOINT_INTERVAL_SECONDS={default}" in text
    # Every profile that overrides the default is named with its value.
    for name, profile in RUNNER_PROFILES.items():
        if profile.checkpoint_interval_seconds != default:
            assert f"`{name}`" in text and f"{profile.checkpoint_interval_seconds} s" in text, name
    # The worker's own fallback agrees with the catalogue, and the doc cites both.
    config, field = "apps/agent-worker/agent_worker/config.py", "WorkerConfig.checkpoint_interval_seconds"
    assert f"checkpoint_interval_seconds: int = {default}" in _cited_symbol(config, field)
    assert f"`{config}::{field}`" in text
    assert '"CHECKPOINT_INTERVAL_SECONDS"' in _cited_symbol(DISPATCH_PY, "worker_env")
    assert f"`{DISPATCH_PY}::worker_env`" in text


# --------------------------------------------------------------------------
# D10: dispatch-and-integration.md §6
# --------------------------------------------------------------------------


def test_d10_design_section_6_names_the_route_that_serves_the_topology():
    body = _flat(_section(_text(DESIGN), "## 6. Still to map"))
    assert "neither of which any route" not in body
    assert "`GET /v1/runtimes`" in body
    assert "`GET /v1/resource-classes`" in body
    platform = "apps/swarm-api/swarm_api/routes/platform.py"
    assert _cited_symbol(platform, "runtimes").startswith('@router.get("/runtimes")')
    assert _cited_symbol(platform, "resource_classes").startswith('@router.get("/resource-classes")')
    assert f"`{platform}::runtimes`" in body
    assert "apps/swarm-ui/src/Runtimes.tsx" in body
    assert (REPO / "apps" / "swarm-ui" / "src" / "Runtimes.tsx").is_file()
    assert STAMP in body


# --------------------------------------------------------------------------
# D10: web-ui README "Blocked" and redesign-v2 "Still open"
# --------------------------------------------------------------------------


def test_d10_web_ui_blocked_list_carries_a_dated_recheck():
    body = _section(_text(UI_README), "## Blocked, with what blocks them")
    assert f"Re-checked {STAMP}" in body
    # Rows that shipped are marked so, each with the route that unblocked it.
    for row, route, cite, needle in (
        ("Screen C", "GET /v1/admin/leases", ("apps/swarm-api/swarm_api/routes/admin.py", "list_leases"), "/leases"),
        ("ACC-3", "POST /v1/accounts/authorize", ("apps/swarm-api/swarm_api/routes/accounts.py", "begin_sign_in"), "/authorize"),
        ("Agent timeline", "GET /v1/tasks/{id}/attempts", ("apps/swarm-api/swarm_api/routes/tasks.py", "list_attempts"), "/attempts"),
    ):
        line = next((ln for ln in body.splitlines() if ln.startswith(f"| {row}")), None)
        assert line is not None, row
        assert "shipped" in line.lower(), line
        assert route in line, line
        assert f"`{cite[0]}::{cite[1]}`" in body
        assert needle in _cited_symbol(*cite).splitlines()[0], cite
    # And what is still blocked says so: nothing persists a reconciler pass.
    assert "reconciler_runs" not in _text(REPO / "apps" / "reconciler" / "reconciler" / "service.py")
    reconciler = next(ln for ln in body.splitlines() if ln.startswith("| Screen G"))
    assert "still blocked" in reconciler.lower()


def test_d10_redesign_still_open_list_is_rechecked():
    text = _text(REDESIGN)
    status = text[: text.index("## ", text.index("## Status") + 3)]
    assert f"re-checked {STAMP}" in status
    styles = _text(REPO / "apps" / "swarm-ui" / "src" / "styles.css")
    # The two duplicate-rule items are fixed on main; the block must say so.
    # Rules, not mentions: the stylesheet's comments record both old names.
    assert not re.search(r"^\s*@keyframes pulse\s*\{", styles, flags=re.M)
    assert not re.search(r"^\.filters\s*\{", styles, flags=re.M)
    since = status[status.index(f"re-checked {STAMP}"):]
    for item in ("`@keyframes pulse`", "`.filters`", "timeline and table views",
                 "Peak RSS over time", "checkpoint strip", "diffstat", "Scrubbers"):
        assert item in since, item
    # The two that are only partly done, and the UI half still open, are named.
    for item in ("duration bar", "`input_from` edges", "`order=desc`", "`GET /v1/attempts`"):
        assert item in since, item


# --------------------------------------------------------------------------
# S7 and S10: BUILD_PROMPT_V2 §2.4 and §2.6.1
# --------------------------------------------------------------------------


def test_s7_a_remote_step_is_an_sc_remote_agent_call():
    body = _spec("### 2.4 ")
    assert f"Amended {STAMP}" in body
    assert "agentType: 'sc:remote'" in body
    assert "schema" in body
    assert "plugin/agents/remote.md" in body
    assert "name: remote" in _text(REPO / "plugin" / "agents" / "remote.md")
    anchor = "and its prompt is what the remote agent is told"
    assert "agentType: 'sc:remote'" in _cited_anchor("plugin/README.md", anchor)
    assert f"`plugin/README.md` (`{anchor}`)" in body
    # The batch and collect half that does exist is cited.
    server = "apps/swarm-mcp/swarm_mcp/server.py"
    assert _cited_symbol(server, "_dispatch_batch").startswith("def _dispatch_batch(")
    assert f"`{server}::_dispatch_batch`" in body


def test_s10_account_add_is_the_apis_oauth_flow_not_claudeswitch():
    body = _spec("#### 2.6.1 ")
    assert f"Amended {STAMP}" in body
    assert "/v1/accounts/authorize" in body and "/v1/accounts/exchange" in body
    sc, accounts = "apps/swarm-mcp/swarm_mcp/sc.py", "apps/swarm-api/swarm_api/routes/accounts.py"
    assert _cited_symbol(sc, "cmd_account_add").startswith("def cmd_account_add(")
    assert _cited_symbol(accounts, "begin_sign_in").startswith('@router.post("/authorize")')
    assert _cited_symbol(accounts, "finish_sign_in").startswith('@router.post("/exchange"')
    for cite in (f"{sc}::cmd_account_add", f"{accounts}::begin_sign_in", f"{accounts}::finish_sign_in"):
        assert f"`{cite}`" in body, cite
    # The command really is `sc account add --label`, and it is what the spec shows.
    assert 'ac_sub.add_parser("add"' in _cited_symbol(sc, "build_parser")
    assert "sc account add --label" in body


# --------------------------------------------------------------------------
# OD-B18-2: the divergences nobody recorded
# --------------------------------------------------------------------------


def test_storage_section_says_the_workspace_is_tmpfs_and_the_cache_unbuilt():
    body = _spec("### 2.3 ")
    assert f"Amended {STAMP}" in body
    assert "tmpfs" in body
    assert "not built" in body


def test_accounts_section_says_no_pod_runs_claudeswitch():
    body = _spec("### 2.6 ")
    assert f"Amended {STAMP}" in body
    assert "claudeswitch" not in _text(REPO / "images" / "agent-runtime-base" / "Dockerfile")
    assert "No pod and no service runs claudeswitch" in body
    oauth = "apps/quota-broker/quota_broker/oauth.py"
    # The cited source opens with the name's `#:` block; the assignment is its last line.
    assert _cited_symbol(oauth, "TOKEN_ENDPOINT").splitlines()[-1].startswith("TOKEN_ENDPOINT = ")
    assert f"`{oauth}::TOKEN_ENDPOINT`" in body


def test_dispatch_section_records_the_hold_not_the_lease():
    body = _spec("#### 2.6.3 ")
    assert f"Amended {STAMP}" in body
    from swarm_common.models import Lease

    assert not any("account" in f.name for f in dataclasses.fields(Lease))
    broker, accountlease = "apps/quota-broker/quota_broker/accounts.py", "apps/agent-worker/agent_worker/accountlease.py"
    assert "holds:" in _cited_symbol(broker, "Account")
    # The variable's NAME, built from pieces: no value is involved here.
    variable = "CLAUDE_CODE_OAUTH_" + "TOK" + "EN"
    name = "ACCOUNT_" + "TOK" + "EN_ENV"
    assert _cited_symbol(accountlease, name).splitlines()[-1] == f'{name} = "{variable}"'
    for cite in (f"{broker}::Account", f"{accountlease}::{name}"):
        assert f"`{cite}`" in body, cite
    assert "HOLD" in body and "request 13" in body
    assert variable in body


def test_dashboard_section_says_it_polls_through_an_external_alb():
    body = _spec("### 2.8 ")
    assert f"Amended {STAMP}" in body
    cites = (
        ("apps/swarm-ui/src/capacityPoll.ts", "export const POOLS_POLL_MS"),
        ("terraform/modules/frontend/main.tf", "An external Application Load Balancer"),
    )
    for path, anchor in cites:
        _cited_anchor(path, anchor)
        assert f"`{path}` (`{anchor}`)" in body, path
    assert "no server-sent events" in body
    assert "Kubernetes API" in body


def test_scale_section_says_the_counters_are_not_sharded():
    body = _spec("### 2.11 ")
    assert f"Amended {STAMP}" in body
    models = "apps/common/swarm_common/models.py"
    assert _cited_symbol(models, "pool_names_for").startswith("def pool_names_for(")
    assert f"`{models}::pool_names_for`" in body
    assert "not sharded" in body
    assert "docs/scaling.md" in body


def test_what_gets_built_marks_the_sidecar_and_the_profile_path():
    body = _spec("## 5. What gets built")
    assert f"Amended {STAMP}" in body
    assert "no sidecar" in body
    assert "contract change request" in body


def test_s35_s36_the_gke_benchmarks_are_not_planned():
    body = _spec("## 8. Verify before building")
    assert f"Amended {STAMP}" in body
    assert "S35" in body and "S36" in body
    assert "not planned" in body
    assert "B16" not in body
    assert "GKE benchmark lane" in body
    assert "`browser`" in body
    assert "Cloud Run Jobs" in body


def test_contract_amendments_section_records_gvisor_and_the_account_pool():
    body = _spec("## 9. CONTRACT.md amendments required")
    assert f"Amended {STAMP}" in body
    # gVisor: not requested, because nothing runs as root.
    dockerfile = "images/agent-runtime-base/Dockerfile"
    assert _cited_anchor(dockerfile, "USER swarm:swarm").strip() == "USER swarm:swarm"
    assert '"runAsNonRoot": True' in _cited_symbol(DISPATCH_PY, "POD_SECURITY_CONTEXT")
    assert f"`{dockerfile}` (`USER swarm:swarm`)" in body
    assert f"`{DISPATCH_PY}::POD_SECURITY_CONTEXT`" in body
    # The account pool: one account held per attempt, no mid-run swap in the worker.
    # A module docstring has no symbol to name, so it is cited by its anchor.
    accountlease = "apps/agent-worker/agent_worker/accountlease.py"
    assert "exactly one secret" in _cited_anchor(accountlease, "WHAT CHANGES HERE. Until now a worker read")
    assert f"`{accountlease}` (`WHAT CHANGES HERE. Until now a worker read`)" in body
    assert "lend" in body


# --------------------------------------------------------------------------
# #72: spend is recorded on every exit, and the coverage shape is the route's
# --------------------------------------------------------------------------

_STALE_SPEND_CLAIMS = ("clean-exit path only", "`lifecycle.py:694`")


def test_no_web_ui_doc_says_spend_is_recorded_on_the_clean_exit_path_only():
    """`_upload_outputs` records spend on every exit that writes a terminal or
    parked state, and `_cleanup` records again for a crash with a live runner
    and a mid-run fence. A paragraph may quote the old claim only beside the
    note that corrects it."""
    docs = sorted((DOCS / "web-ui").glob("*.md"))
    assert docs, "no docs under docs/web-ui"
    stale = []
    for doc in docs:
        for paragraph in re.split(r"\n[ \t]*\n", _text(doc)):
            if "Corrected for #72" in paragraph:
                continue
            stale += [f"{doc.name}: {c}" for c in _STALE_SPEND_CLAIMS if c in paragraph]
    assert not stale, f"the clean-exit-only spend claim is still stated: {stale}"


def test_ui_audit_attempts_coverage_shape_matches_the_route():
    from swarm_api.routes.attempts import spend_coverage

    fences = re.findall(r"^```[^\n]*\n(.*?)^```", _text(UI_AUDIT), flags=re.M | re.S)
    fence = next(f for f in fences if "GET /v1/attempts?" in f)
    block = re.search(r'"coverage":\s*\{(.*?)\}', fence, flags=re.S)
    assert block, "the GET /v1/attempts shape carries no coverage block"
    keys = set(re.findall(r'"(\w+)":', block.group(1)))
    assert keys == set(spend_coverage([], {}).keys())


# --------------------------------------------------------------------------
# every citation these documents make resolves, and none is a line number
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", TOUCHED, ids=lambda p: p.name)
def test_every_citation_is_a_symbol_or_an_anchor_that_resolves(doc: Path):
    """No `path:N` (a merge above the code moves it); every `path::qualname` and `path` (`text`) resolves."""
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# HW-DOCS (part of #1436): a workflow step waits for a tenant member's answer
# --------------------------------------------------------------------------
#
# docs/workflows.md and docs/notifications.md describe the await-input step
# that HW-SCHED, HW-API-SUBMIT, HW-WORKER, HW-API-ANSWER and HW-NOTIFY built.
# Every bound, type and name they state is read from the code here, so a
# change to the 24-hour bound, the 16 MiB answer, the ten files, the closed
# type list or the three forms fails until the documents follow.

WORKFLOWS = DOCS / "workflows.md"
NOTIFICATIONS = DOCS / "notifications.md"
HUMAN_STEPS = DOCS / "design" / "human-steps.md"
AWAIT_HEADING = "## Waiting for a person: `await-input`"
MIB = 1024 * 1024
KIB = 1024


def _await_section() -> str:
    return _flat(_section(_text(WORKFLOWS), AWAIT_HEADING))


def _await_body() -> str:
    """The workflows section with every subsection under it, up to the next `## `."""
    text = _text(WORKFLOWS)
    start = text.index(AWAIT_HEADING)
    rest = text[start + len(AWAIT_HEADING):]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return _flat(rest[: nxt.start()] if nxt else rest)


def test_await_input_section_states_the_24_hour_bound_the_code_holds():
    from scheduler.loop import HUMAN_PARK_MAX_SECONDS
    from swarm_api.validation import (
        AWAIT_INPUT_MAX_SECONDS,
        AWAIT_INPUT_MIN_SECONDS,
        AWAIT_INPUT_REMIND_MIN_SECONDS,
    )

    body = _await_body()
    # The API restates the scheduler's bound; the doc states the one number.
    assert AWAIT_INPUT_MAX_SECONDS == HUMAN_PARK_MAX_SECONDS
    assert AWAIT_INPUT_MAX_SECONDS % 3600 == 0
    assert f"{AWAIT_INPUT_MAX_SECONDS:,}" in body
    assert f"{AWAIT_INPUT_MAX_SECONDS // 3600} hours" in body
    assert f"{AWAIT_INPUT_MIN_SECONDS:,} to {AWAIT_INPUT_MAX_SECONDS:,}" in body
    assert f"{AWAIT_INPUT_REMIND_MIN_SECONDS:,}" in body
    validation = "apps/swarm-api/swarm_api/validation.py"
    assert f"`{validation}::AWAIT_INPUT_MAX_SECONDS`" in body
    assert "`apps/scheduler/scheduler/loop.py::HUMAN_PARK_MAX_SECONDS`" in body
    # A wait is free (invariant 1): the doc says the step holds no lease.
    assert "PARKED(AWAITING_INPUT)" in body
    assert "no lease" in body


def test_await_input_section_states_the_answer_and_file_bounds():
    from swarm_api import awaitinput
    from swarm_api.validation import AWAIT_INPUT_MAX_FILES

    body = _await_body()
    assert awaitinput.MAX_ANSWER_BYTES % MIB == 0
    assert f"{awaitinput.MAX_ANSWER_BYTES // MIB} MiB" in body
    assert "`apps/swarm-api/swarm_api/awaitinput.py::MAX_ANSWER_BYTES`" in body
    assert f"at most {AWAIT_INPUT_MAX_FILES} files" in body
    assert f"`max_files` | 0 to {AWAIT_INPUT_MAX_FILES}" in _text(WORKFLOWS)
    assert "`apps/swarm-api/swarm_api/validation.py::AWAIT_INPUT_MAX_FILES`" in body
    assert f"{awaitinput.MAX_FORM_BYTES // KIB} KiB" in body
    assert f"{awaitinput.MAX_TEXT_BYTES // KIB} KiB" in body
    assert f"{awaitinput.MAX_CONCURRENT_MULTIPART} multipart answers" in body


def test_await_input_section_lists_exactly_the_allowed_types():
    from swarm_api import awaitinput

    body = _await_body()
    for extension in awaitinput.TEXT_EXTENSIONS:
        assert f"`{extension}`" in body, extension
    for content_type, extensions in awaitinput.BINARY_TYPES:
        assert f"`{content_type}`" in body, content_type
        for extension in extensions:
            assert f"`{extension}`" in body, extension
    assert f"`{awaitinput.TEXT_CONTENT_TYPE}`" in body
    assert "`apps/swarm-api/swarm_api/awaitinput.py::TEXT_EXTENSIONS`" in body
    assert "`apps/swarm-api/swarm_api/awaitinput.py::sniff`" in body
    # The list is closed: the extensions the doc names in its type list are
    # the code's, no more, so an extension dropped from the code fails here.
    types = re.search(r"\*\*Allowed types\.\*\*(.*?)Everything else", body)
    assert types, "the section has no **Allowed types.** paragraph ending in 'Everything else'"
    named = set(re.findall(r"`(\.[a-z]+)`", types.group(1)))
    binary = {e for _, exts in awaitinput.BINARY_TYPES for e in exts}
    assert named == set(awaitinput.TEXT_EXTENSIONS) | binary
    assert "file_type_refused" in body


def test_await_input_section_names_the_forms_and_their_artifacts():
    from swarm_api import awaitinput
    from swarm_api.notifications import _INPUT_TITLES
    from swarm_api.validation import (
        AWAIT_INPUT_ARTIFACTS,
        AWAIT_INPUT_FORMS,
        AWAIT_INPUT_ON_EXPIRE,
        AWAIT_INPUT_PROFILE,
        QUESTIONS_FILENAME,
    )

    text = _text(WORKFLOWS)
    body = _await_body()
    assert set(AWAIT_INPUT_FORMS) == set(_INPUT_TITLES)
    for form in AWAIT_INPUT_FORMS:
        row = next((ln for ln in text.splitlines() if ln.startswith(f"| `{form}` |")), None)
        assert row is not None, f"no form row for {form!r}"
        assert f"`{AWAIT_INPUT_ARTIFACTS[form]}`" in row, row
    for value in AWAIT_INPUT_ON_EXPIRE:
        assert f'`"{value}"`' in body, value
    assert f"`{AWAIT_INPUT_PROFILE}`" in body
    assert f"`{QUESTIONS_FILENAME}`" in body
    for via in awaitinput.VIA:
        assert f'`"{via}"`' in body, via
    # The routes the section documents are the routes that exist.
    from swarm_api.routes.await_input import INPUT_ROUTE, INPUTS_ROUTE

    assert f"POST {INPUT_ROUTE}" in body and f"GET {INPUT_ROUTE}" in body
    assert f"GET {INPUTS_ROUTE}?status=waiting" in body
    # The reasons a refusal carries, each one the code raises.
    source = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "awaitinput.py")
    source += _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "await_input.py")
    for reason in ("person_required", "not_a_member", "membership_unavailable", "not_open",
                   "already_answered", "spec_refused", "too_many_files", "invalid_file_name",
                   "file_type_refused", "file_contains_credential", "answer_too_large",
                   "answer_busy", "store_unavailable"):
        assert f"`{reason}`" in body, reason
        assert f'"{reason}"' in source, reason
    # The expiry's marker is one constant (failure-review reads it too), and the
    # sweep's text is built from it.
    assert "await_input_expired" in body
    assert awaitinput.AWAIT_INPUT_EXPIRED_ERROR == "await_input_expired:"
    assert '{AWAIT_INPUT_EXPIRED_ERROR} nobody answered within' in source
    # The worker stages the files beside the artifact.
    assert "`review.files/`" in body
    assert "`apps/agent-worker/agent_worker/inputs.py::attachments_dir`" in body


def test_await_input_section_says_whether_the_catalogue_offers_the_step():
    """The section states the catalogue's answer, read from the catalogue: before
    request D it said the step ships dark; since request D (entry 105) it says
    the step is available, and still what unavailable means."""
    body = _await_section()
    assert "request D" in body
    assert "`apps/common/swarm_common/profiles.py::RUNNER_PROFILES`" in body
    assert "lists it as unavailable, a submission that names it is refused" in body
    if RUNNER_PROFILES["await-input"].available:
        assert "The catalogue lists `await-input` as available" in body
    else:
        assert "The catalogue lists `await-input` as available" not in body


def test_step_fields_table_names_await_input():
    fields = _section(_text(WORKFLOWS), "## Step fields")
    row = next((ln for ln in fields.splitlines() if ln.startswith("| `await_input` |")), None)
    assert row is not None
    assert "#waiting-for-a-person-await-input" in row


def test_notifications_doc_describes_the_waiting_step_item_and_the_inbox():
    from swarm_api import approvals
    from swarm_api.notifications import _INPUT_TITLES, SOURCES

    assert "awaiting_input" in dict(SOURCES)
    text = _text(NOTIFICATIONS)
    body = _flat(_section(text, "## A workflow step waiting for a person"))
    assert "`task:input:<task>:<k>`" in body
    assert f"`{approvals.INPUT_PREFIX}<task_id>`" in body
    assert f"kind `{approvals.INPUT}`" in body
    for title in _INPUT_TITLES.values():
        assert f"\"{title}\"" in body, title
    assert "`waiting_step`" in body
    assert "`remind`" in body
    for cite in ("apps/swarm-api/swarm_api/notifications.py::awaiting_input",
                 "apps/swarm-api/swarm_api/approvals.py::project_input",
                 "apps/swarm-api/swarm_api/approvals.py::awaiting_input"):
        assert f"`{cite}`" in body, cite
    # The groups table names the source in the `task` row.
    task_row = next(ln for ln in text.splitlines() if ln.startswith("| `task` |"))
    assert "`AWAITING_INPUT`" in task_row


def test_human_steps_design_records_what_hw_docs_built():
    body = _flat(_section(_text(HUMAN_STEPS), "## 8. What was built"))
    assert "HW-DOCS" in body
    assert "docs/workflows.md" in body and "docs/notifications.md" in body
    assert "HW-UI" in body and "HW-MCP" in body


@pytest.mark.parametrize("doc", (WORKFLOWS, NOTIFICATIONS), ids=lambda p: p.name)
def test_await_input_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# MCP-DOCS (part of #1433): a task's agent queries its caller's MCP server
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
MCP_RELAY_PY = "apps/agent-worker/agent_worker/mcp_relay.py"
MCP_RUNNER_TEST = "tests/unit/worker/test_mcp_runner_config.py"
MCP_RUNNER_CASE = "test_the_pinned_claude_code_loads_the_repositorys_mcp_json_and_ours_wins_a_name"
MCP_BUILT = "## 8. What was built"
MCP_SECURITY = "## Registered MCP servers: the caller's credential is never in the agent's reach"
MCP_STAMP = "2026-10-18"


def _rule_fires(rule, text: str) -> bool:
    if rule.apply is not None:
        return bool(rule.apply(text, True, False)[1])
    return bool(rule.pattern.search(text))


def _mcp_lanes() -> list[str]:
    plan = _section(_text(MCP_DESIGN), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(MCP-[A-Z-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 10 and "MCP-DOCS" in lanes, lanes
    return lanes


def test_mcp_design_no_longer_says_nothing_is_built():
    head = _flat(_text(MCP_DESIGN).split("\n---\n", 1)[0])
    assert "Nothing described here is built" not in head
    assert "§8" in head and "SWARM_MCP_SERVERS" in head


def test_mcp_design_records_what_every_lane_built():
    raw = _section(_text(MCP_DESIGN), MCP_BUILT)
    body = _flat(raw)
    for lane in _mcp_lanes():
        assert f"**{lane} (" in raw, f"§8 has no entry for {lane}"
    # Dark until the switch, read from the code that reads it.
    assert '"SWARM_MCP_SERVERS"' in _cited_symbol("apps/swarm-api/swarm_api/mcp_servers.py", "FLAG")
    assert "`apps/swarm-api/swarm_api/mcp_servers.py::FLAG`" in raw
    assert "`mcp_unavailable`" in body
    # §4.10's FAILED needed edges the frozen state machine lacked: contract
    # request 99 added READY -> FAILED and PARKED -> FAILED, and admission
    # writes them with no lease.
    from swarm_common.states import TaskState, can_transition

    fail = "apps/scheduler/scheduler/loop.py::Scheduler._fail_unrunnable"
    assert f"`{fail}`" in raw
    assert "self._store.fail_before_lease(" in _cited_symbol(*fail.split("::"))
    assert can_transition(TaskState.READY, TaskState.FAILED)
    assert can_transition(TaskState.PARKED, TaskState.FAILED)
    assert "`READY` -> `FAILED`" in body and "`PARKED` -> `FAILED`" in body and "request 99" in body


def test_mcp_design_4_8_records_the_owners_shape_exemption():
    from agent_worker import mcp_relay
    from swarm_redaction import RULES

    raw = _section(_text(MCP_DESIGN), "### 4.8 The relay: what the agent may and may not send")
    body = _flat(raw)
    assert f"Amended {MCP_STAMP}" in body and "the owner approved" in body
    assert f"`{MCP_RELAY_PY}::SHAPE_EXEMPT`" in raw
    by_name = {rule.name: rule for rule in RULES}
    assert len(mcp_relay.SHAPE_EXEMPT) == 2 and "two of" in body
    for name in mcp_relay.SHAPE_EXEMPT:
        assert name in by_name, f"{name} is not a swarm_redaction rule"
        assert f"`{name}`" in body, name
    # The examples the amendment gives fire an exempt rule in a log's
    # redaction and pass the relay's check; `token_budget: 4000` fires neither.
    # The `http_authorization` example is described, not quoted: a literal
    # one is a line the publish scan refuses (scripts/publish-scan.sh).
    prose = " ".join(("a", "basic", "configuration", "with", "a", "bearer", "authentication", "step"))
    for example, rule, quoted in (("token: 4000", "key_value_assignment", True),
                                  ("password: field", "key_value_assignment", True),
                                  (prose, "http_authorization", False)):
        if quoted:
            assert f"`{example}`" in body, example
        assert _rule_fires(by_name[rule], example), (example, rule)
        assert mcp_relay.credential_rule(example) is None, example
    assert "basic or bearer followed by any word of twelve letters or more" in body
    assert "`token_budget: 4000`" in body
    assert not any(_rule_fires(rule, "token_budget: 4000") for rule in RULES)
    # What stays refused: a registered value by value, a token by its shape,
    # a value after an `Authorization` header's scheme.
    assert mcp_relay.credential_rule("ghp_" + "a" * 36) == "github_token"
    assert "`http_authorization_scheme`" in body
    assert mcp_relay.credential_rule("Authorization: token " + "b" * 8) == "http_authorization_scheme"
    row = next(ln for ln in raw.splitlines() if "`credential_in_request`" in ln and ln.startswith("|"))
    assert "`SHAPE_EXEMPT`" in row


def test_mcp_design_1_2_records_that_claude_code_runs_without_strict_mcp_config():
    raw = _section(_text(MCP_DESIGN), "### 1.2 What the code allows today (read 2026-10-17 at `35f2a61`)")
    body = _flat(raw)
    assert "Decided by MCP-RUNNERS" in body
    assert "without `--strict-mcp-config`" in body
    assert "nobody has verified" not in body
    assert "an attached server wins a name the repository also declares" in body
    assert "never within reach of a repository-declared server" in body
    assert f"`{MCP_RUNNER_TEST}::{MCP_RUNNER_CASE}`" in raw
    assert "--strict-mcp-config" in _cited_symbol(MCP_RUNNER_TEST, MCP_RUNNER_CASE)
    runner = _text(REPO / "apps" / "agent-worker" / "agent_worker" / "runners" / "claude_code.py")
    assert '"--strict-mcp-config"' not in runner


def test_security_doc_describes_the_mcp_relay_from_the_code():
    from agent_worker import mcp_relay

    raw = _section(_text(SECURITY), MCP_SECURITY)
    body = _flat(raw)
    attach = "apps/swarm-api/swarm_api/routes/attempts_mcp.py::ATTACH_PURPOSE"
    assert '"swarm-mcp-attach/v1"' in _cited_symbol(*attach.split("::"))
    assert "`swarm-mcp-attach/v1`" in body and f"`{attach}`" in raw
    assert "`POST /v1/attempts/{attempt_id}/mcp:attach`" in body
    assert "`swarm-tenant-<tenant>-mcpc-<16 hex>`" in body
    assert "`SWARM_MCP_SERVERS`" in body
    for name in mcp_relay.SHAPE_EXEMPT:
        assert f"`{name}`" in body, name
    assert f"`{MCP_RELAY_PY}::Relay`" in raw
    assert "prompt injection" in body
    assert "registered-mcp-servers.md" in body


def test_security_doc_credential_theft_names_every_route_that_serves_one():
    routes = _cited_symbol("tests/unit/control_plane/test_no_route_serves_credentials.py",
                           "CREDENTIAL_SERVING_ROUTES")
    paths = re.findall(r'\("POST", "([^"]+)"\)', routes)
    assert len(paths) == 2, paths
    theft = _flat(_section(_text(SECURITY), "## Credential theft"))
    for path in paths:
        assert f"`POST {path}`" in theft, path
    assert "there is no read path in the API" not in theft


@pytest.mark.parametrize("doc", (MCP_DESIGN, SECURITY), ids=lambda p: p.name)
def test_mcp_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# MODEL-DOCS (part of #1435): a workflow step picks its model class by name
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
MODEL_BUILT = "## 8. What was built"
MODEL_ENV = "## A step's environment: where the agent starts, HOME, and the model"
MODEL_STEP = "## Choosing a step's model: `model_class`"
MODEL_QUOTA = "## 4. Park reasons"


def _runner_models() -> dict[str, str]:
    """`local.runner_models` in terraform/infra/locals.tf: class -> pinned ID."""
    block = re.search(r"^  runner_models = \{\n(.*?)^  \}", _text(LOCALS_TF), flags=re.M | re.S)
    assert block, "terraform/infra/locals.tf has no runner_models block"
    return dict(re.findall(r'^\s+(\w+)\s+=\s+"([^"]+)"', block.group(1), flags=re.M))


def _model_lanes() -> list[str]:
    plan = _section(_text(MODEL_DESIGN), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(MODEL-[A-Z-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 10 and "MODEL-DOCS" in lanes, lanes
    return lanes


def test_runner_models_are_keyed_by_exactly_the_frozen_classes():
    """The pin map every model test below reads, held to the vocabulary."""
    from swarm_common.profiles import MODEL_CLASSES

    models = _runner_models()
    assert set(models) == set(MODEL_CLASSES)
    for name, model_id in models.items():
        assert MODEL_CLASSES[name].admits(model_id), (name, model_id)


def test_model_design_no_longer_says_nothing_is_built():
    head = _flat(_text(MODEL_DESIGN).split("\n---\n", 1)[0])
    assert "Nothing is built" not in head
    assert "§8" in head and "built" in head


def test_model_design_records_what_every_lane_built():
    from swarm_common.specsign import SPEC_FORMAT, signing_format

    raw = _section(_text(MODEL_DESIGN), MODEL_BUILT)
    body = _flat(raw)
    for lane in _model_lanes() + ["MODEL-ACCOUNTS"]:
        assert f"**{lane} (" in raw, f"§8 has no entry for {lane}"
    # The class is signed at format 5: request 96 took 4 first.
    # Format 6 (request 120, the budget) came after it.
    assert signing_format({"model_class": "haiku"}) == 5 < SPEC_FORMAT
    assert "format 5" in body and "request 96" in body and "request 104" in body
    # R4: no flag. Nothing in the code or the Terraform reads one.
    for root in ("apps", "terraform"):
        for path in (REPO / root).rglob("*"):
            if path.suffix in (".py", ".tf", ".ts", ".tsx") and "node_modules" not in path.parts:
                assert "MODEL_CLASSES_ENABLED" not in path.read_text(encoding="utf-8"), path
    assert "R4" in body and "no flag" in body
    # What `model_class_not_yet_available` means now that there is no flag.
    from swarm_api.validation import ModelClassNotYetAvailable

    assert f"`{ModelClassNotYetAvailable.code}`" in body and "`WORKER_CLASS_MODELS`" in body
    # The derived WORKER_MODELS is a follow-up while terraform still writes it.
    if "WORKER_MODELS = jsonencode(local.profile_default_models)" in _text(LOCALS_TF):
        assert "`WORKER_MODELS`" in body and "follow-up" in body


def test_model_design_4_3_and_6_say_what_replaced_the_flag():
    text = _text(MODEL_DESIGN)
    signed = _flat(_section(text, _heading(text, "### 4.3 ")))
    assert "Amended 2026-10-19" in signed and "format 5" in signed and "no flag" in signed
    plan = _flat(_section(text, "## 6. Build plan"))
    assert "MODEL-ENABLE was dropped" in plan


def test_agent_output_describes_the_step_model_from_the_code():
    from agent_worker.specverify import MODEL_CLASS_MISMATCH, MODEL_NOT_PINNED
    from swarm_common.models import Attempt
    from swarm_common.profiles import MODEL_CLASSES

    raw = _section(_text(AGENT_OUTPUT), MODEL_ENV)
    body = _flat(raw)
    assert "has no model field" not in body
    assert "| the Job's `MODEL`: `claude-opus-5-5` |" not in raw
    for name, model_id in _runner_models().items():
        assert f"`{name}`" in body and f"`{model_id}`" in body, name
    assert len(MODEL_CLASSES) == 4 and "four classes" in body
    step = "apps/scheduler/scheduler/dispatch.py::step_model"
    assert f"`{step}`" in raw and "step_model_class(" in _cited_symbol(*step.split("::"))
    assert "`terraform/infra/locals.tf` (`WORKER_CLASS_MODELS = jsonencode(local.runner_models)`)" in raw
    check = "apps/agent-worker/agent_worker/specverify.py::check_model"
    assert f"`{check}`" in raw and ".admits(" in _cited_symbol(*check.split("::"))
    assert f"`{MODEL_NOT_PINNED}`" in body and f"`{MODEL_CLASS_MISMATCH}`" in body
    note = "apps/agent-worker/agent_worker/lifecycle.py::Worker._note_model_mismatch"
    assert f"`{note}`" in raw and '"model_mismatch' in _cited_symbol(*note.split("::"))
    assert "model" in {f.name for f in dataclasses.fields(Attempt)}
    start = "apps/agent-worker/agent_worker/control.py::ControlPlane.record_attempt_start"
    assert f"`{start}`" in raw and 'record["model"]' in _cited_symbol(*start.split("::"))
    assert "`Attempt.model`" in body


def test_workflows_step_fields_table_names_model_class():
    table = _section(_text(WORKFLOWS), "## Step fields")
    row = next((ln for ln in table.splitlines() if ln.startswith("| `model_class` |")), None)
    assert row, "the step fields table has no model_class row"
    assert "#choosing-a-steps-model-model_class" in row


def test_workflows_model_class_section_states_what_the_code_holds():
    from swarm_api import refusals
    from swarm_api.validation import (
        ModelClassNotAccepted,
        ModelClassNotYetAvailable,
        ModelIsChosenByModelClass,
        UnknownModelClass,
    )
    from swarm_common.profiles import MODEL_CLASSES

    raw = _section(_text(WORKFLOWS), MODEL_STEP)
    body = _flat(raw)
    for name, model_id in _runner_models().items():
        assert f"`{name}`" in body and f"`{model_id}`" in body, name
    default = RUNNER_PROFILES["claude-code"].default_model_class
    assert f"`{default}` is the default" in body
    every = [p.name for p in RUNNER_PROFILES.values() if p.model_classes == tuple(MODEL_CLASSES)]
    assert sorted(every) == [
        "claude-code", "claude-code-browser", "claude-code-large", "claude-code-lightpanda", "indexer",
    ], every
    for name in every:
        assert f"`{name}`" in body, name
    for error in (UnknownModelClass, ModelClassNotAccepted, ModelClassNotYetAvailable,
                  ModelIsChosenByModelClass):
        assert f"`{error.code}`" in body, error.code
    switch = refusals.SWITCHES[ModelIsChosenByModelClass.code]
    assert switch.default is False and "report-only" in body and f"`{switch.env}`" in body
    # R3, R1 (a), R6: the child's own default, the plan's digest, the pin at dispatch.
    assert "its own profile's default, never the parent's" in body
    assert "inside `plan_digest`" in body
    assert "the class's pin at dispatch" in body
    assert "`input.model`" in body and "never a model ID" in body


def test_quota_doc_says_every_window_binds_every_class():
    from quota_broker import accounts
    from scheduler.credentials import PoolAnswer

    raw = _section(_text(QUOTA), MODEL_QUOTA)
    body = _flat(raw)
    assert "Every quota window binds every attempt, whatever its model class" in body
    binding = "apps/quota-broker/quota_broker/accounts.py::Account._binding_remaining"
    source = _cited_symbol(*binding.split("::"))
    assert f"`{binding}`" in raw and "self.windows.values()" in source and "class" not in source
    assert "model_class" not in accounts.Account.headroom.__code__.co_varnames
    assert "`seven_day_opus`" in body
    serves = "apps/quota-broker/quota_broker/accounts.py::Account.serves_class"
    assert f"`{serves}`" in raw and f"`{PoolAnswer.NO_ACCOUNT_SERVES_CLASS.value}`" in body
    assert "model-per-step.md" in body


@pytest.mark.parametrize("doc", (MODEL_DESIGN, AGENT_OUTPUT, QUOTA), ids=lambda p: p.name)
def test_model_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# WK-DOCS (part of #847): an admin creates a personal, group or service
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
KINDS_BUILT = "## 8. What was built"
KINDS_O1 = "### O1, the owner's three live reads of the front door (workspace-kinds §4.5)"
KINDS_OFFBOARD = "## A group or service workspace, made by the workspace job"
#: The failure codes §4.8 of the design added, each written by the job.
KINDS_CODES = ("SERVICE_ACCOUNT_REFUSED", "DIRECTORY_CHECK_STALE", "FRONT_DOOR_OWNER",
               "FRONT_DOOR_FAILED", "NAMESPACE_LABEL_MISSING")


def _kinds_lanes() -> list[str]:
    plan = _section(_text(KINDS_DESIGN), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(WK-[A-Z-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 9 and "WK-DOCS" in lanes, lanes
    return lanes


def _whole_section(text: str, heading: str) -> str:
    """The body under a `## ` heading with every subsection, up to the next `## `."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^## ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def test_kinds_design_no_longer_says_nothing_is_built():
    head = _flat(_text(KINDS_DESIGN).split("\n---\n", 1)[0])
    assert "Nothing here is built" not in head
    assert "**Status: built" in head and "§8" in head
    assert "`enable_workspace_kinds`" in head


def test_kinds_design_records_what_every_lane_built():
    raw = _section(_text(KINDS_DESIGN), KINDS_BUILT)
    body = _flat(raw)
    for lane in _kinds_lanes():
        assert f"**{lane} (" in raw, f"§8 has no entry for {lane}"
    # The service kind's steps, in the order the job runs them.
    order = "printf '%s\\n' A1 S1 A2 A3 A4 A5 A6 A7 A8 S2 A9"
    assert f"`{REGISTER}` (`{order}`)" in raw
    _cited_anchor(REGISTER, order)
    # Departure 1: the platform owner A1 checks is read from the guard's
    # rules, keyed by project, not from entry.py's environment.
    entry = _text(REPO / "images" / "workspace-apply" / "entry.py")
    assert "PLATFORM_OWNER" not in entry
    assert ".platform_owner" in _cited_anchor(REGISTER, "jq -r --arg p \"${PROJECT_ID}\" '(.platform_owner // {})[$p]")
    assert "`scripts/lib/workspace-calls.json`" in body and "`platform_owner`" in body
    assert "not from `entry.py`" in body
    # Departure 2: a service is admitted by the unique id S1 recorded, not
    # by its address alone.
    admits = "apps/swarm-api/swarm_api/auth.py::Authenticator._admits_service"
    assert f"`{admits}`" in raw and "subject != pinned" in _cited_symbol(*admits.split("::"))
    assert "`service.unique_id`" in body
    # Departure 3: the registry re-checks every ready record against the
    # request's own rules.
    snap = "apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry"
    assert f"`{snap}`" in raw and "validate_kind_request" in _cited_symbol(*snap.split("::"))
    # Departure 4: the five new codes got copy of their own (WK-FAILCOPY).
    from swarm_api.workspaces import FAILURE_COPY

    for code in KINDS_CODES:
        assert code in FAILURE_COPY, f"{code} has no copy: say so in §8 and workspaces.md §4.3"
        assert f"`{code}`" in body, code
    assert "Lane WK-FAILCOPY" in body
    # The switch every new IAM row sits behind, and the frozen tenants map.
    assert "`enable_workspace_kinds`" in body and "`front_door_admit_role_bound`" in body
    assert "`terraform/infra/variables.tf` (`VAR.TENANTS IS FROZEN`)" in raw
    _cited_anchor("terraform/infra/variables.tf", "VAR.TENANTS IS FROZEN")


def test_workspaces_doc_records_wd3_reversed_as_built():
    text = _text(WORKSPACES_DOC)
    head = _flat(text.split("\n---\n", 1)[0])
    assert "workspace-kinds.md" in head and "Admin › Tenants" in head
    split = _flat(_section(text, "### 3.2 The split (decided, WD3 with WD4)"))
    assert "not yet built" not in split
    assert "`POST /v1/admin/workspaces`" in split
    assert "frozen" in split and "`var.tenants`" in split
    from swarm_api.routes.people import create_workspace

    assert create_workspace.__doc__ and "workspace-kinds.md §4.2" in create_workspace.__doc__


def test_workspaces_doc_section_0_holds_the_owners_check_o1():
    """O1 is the owner's, not an agent's: §0 says what it reads, what each
    answer turns on, and, while the switch it gates is still off, either
    that it is not yet recorded or a dated record whose step 2 failed and
    that leaves the switch off. It never reads as passed while K1 is off."""
    text = _text(WORKSPACES_DOC)
    section0 = _whole_section(text, "## 0. Today")
    assert KINDS_O1 in section0
    body = _flat(_section(text, KINDS_O1))
    for read in ("`gcloud iam roles describe roles/iap.admin`", "`-ui-backend`", "`modifiedGrantsByRole`"):
        assert read in body, read
    variables = _text(REPO / "terraform" / "bootstrap" / "variables.tf")
    bound = re.search(r'variable "front_door_admit_role_bound" \{.*?default\s+=\s+(\w+)', variables, flags=re.S)
    assert bound, "terraform/bootstrap/variables.tf has no front_door_admit_role_bound"
    assert "`front_door_admit_role_bound`" in body and "`FRONT_DOOR_OWNER`" in body
    if bound.group(1) == "false":
        # O1 was run 2026-10-19 and step 2 failed (IAP refused the number-form
        # condition), so the switch stays off with a dated record, not the
        # "Not yet recorded" placeholder. A record whose step 2 passed would
        # mean the switch should be on: that is the contradiction held here.
        recorded = re.search(r"\*\*Recorded \d{4}-\d{2}-\d{2}", body)
        if recorded:
            assert "2. **Fails.**" in body, "O1 is recorded, step 2 did not fail, and front_door_admit_role_bound is still off"
            assert "stays at its default, off" in body
        else:
            assert "Not yet recorded" in body


def test_workspaces_doc_failure_copy_is_the_codes():
    from swarm_api.workspaces import _UNKNOWN_FAILURE_COPY, FAILURE_COPY

    raw = _section(_text(WORKSPACES_DOC), "### 4.3 Failure copy, word for word")
    rows = [ln for ln in raw.splitlines() if ln.startswith("| `")]
    quoted = {}
    for row in rows:
        codes = re.findall(r"`([A-Z_]+)`", row.split("|")[1])
        copy = re.search(r"“(.+)”", row)
        for code in codes:
            quoted[code] = copy.group(1) if copy else None
    for code, copy in FAILURE_COPY.items():
        assert quoted.get(code) == copy, code
    body = _flat(raw)
    assert f"“{_UNKNOWN_FAILURE_COPY}”" in body
    for code in KINDS_CODES:
        assert f"`{code}`" in body, code
        assert code not in FAILURE_COPY or quoted.get(code) == FAILURE_COPY[code], code


def test_multi_tenancy_admits_a_job_made_service_without_terraform():
    text = _text(TENANCY)
    service = _flat(_section(text, "### Service tenants: a service account's own `u-` tenant"))
    assert "admitted in three places" in service
    assert f"`{REGISTER}` (`ws_s2() {{`)" in _section(text, "### Service tenants: a service account's own `u-` tenant")
    _cited_anchor(REGISTER, "ws_s2() {")
    registry = "apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry.admitted_services"
    assert f"`{registry}`" in service
    _cited_symbol(*registry.split("::"))
    assert "Admin › Tenants" in service and "platform owner" in service
    workspace = _flat(_whole_section(text, "## 7. A person's personal workspace"))
    assert "Terraform owns group and service tenants only" not in workspace
    steps = _section(text, "### 7.1 Request, approve, apply")
    for step in ("S1", "S2"):
        assert f"| {step} |" in steps, step
    groups = _flat(_section(text, "### Priority order matters"))
    assert "`apps/swarm-api/swarm_api/auth.py::Authenticator._tenant_groups`" in groups
    assert "configured + tuple(" in _cited_symbol("apps/swarm-api/swarm_api/auth.py", "Authenticator._tenant_groups")


def test_offboarding_runbook_covers_group_and_service_workspaces():
    text = _text(OFFBOARDING)
    raw = _section(text, KINDS_OFFBOARD)
    body = _flat(raw)
    assert "`enabled = false`" in body and "`deprovisioned`" in body
    assert "`gcloud iap web remove-iam-policy-binding`" in body
    assert "roles/iap.httpsResourceAccessor" in body
    # The registry counts only `ready`, which is why moving the record off
    # `ready` is the immediate stop.
    assert "READY" in _cited_symbol("apps/swarm-api/swarm_api/registry.py", "WorkspaceRegistry._read")
    assert "`apps/swarm-api/swarm_api/registry.py::WorkspaceRegistry._read`" in raw
    assert "swarm-tenant-<tenant>-git" in body
    assert "Not verified" in body
    first = _flat(text.split("\n## ", 1)[0])
    assert "service" in first and "workspace" in first


@pytest.mark.parametrize("doc", (KINDS_DESIGN, WORKSPACES_DOC, TENANCY, OFFBOARDING), ids=lambda p: p.name)
def test_workspace_kinds_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# ROLES-DOCS (part of #1518): a workspace's people are viewers, members or
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
# added fails until the documents follow.

ROLES_DESIGN = DOCS / "design" / "workspace-roles.md"
SECRETS_DESIGN = DOCS / "design" / "workspace-secrets.md"
ROLES_BUILT = "## 8. What was built"
ROLES_SECURITY = "## Workspace roles and a platform admin's reach"
ROLES_PY = "apps/swarm-api/swarm_api/roles.py"
SETTINGS_PY = "apps/swarm-api/swarm_api/settings.py"
ANSWER_GATE = "apps/swarm-api/swarm_api/awaitinput.py::member_refusal"
SECRETS_GATE = "apps/swarm-api/swarm_api/workspace_secrets.py::require_manager"
#: The two refusal codes on existing routes, one switch each (§4.9, Q10).
ROLE_CODES = ("role_member_required", "role_admin_required")
#: Lanes the owner added after §6 was written (#1518, comment of 2026-10-19).
ROLES_ADDED_LANES = ("WK-REACH", "ROLES-ADMIN-LIST")


def _roles_lanes() -> list[str]:
    plan = _section(_text(ROLES_DESIGN), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(ROLES-[A-Z-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 13 and "ROLES-DOCS" in lanes, lanes
    return lanes + list(ROLES_ADDED_LANES)


def _mentions(root: Path, needle: str, pattern: str) -> bool:
    return any(needle in p.read_text(encoding="utf-8", errors="ignore") for p in root.rglob(pattern))


def test_roles_design_no_longer_says_nothing_is_built():
    head = _flat(_text(ROLES_DESIGN).split("\n---\n", 1)[0])
    assert "Nothing here is built" not in head
    assert "**Status: built" in head and "§8" in head
    assert "`PLATFORM_ADMIN_REACH`" in head


def test_roles_design_records_what_every_lane_built():
    from swarm_api import refusals, roles

    raw = _section(_text(ROLES_DESIGN), ROLES_BUILT)
    body = _flat(raw)
    for lane in _roles_lanes():
        assert f"**{lane} (" in raw, f"§8 has no entry for {lane}"
    # Both switches ship off, and the environment variable each reads.
    for code in ROLE_CODES:
        assert refusals.SWITCHES[code].default is False, code
        assert f"`{code}`" in body and f"`{refusals.env_name(code)}`" in body, code
    dev = _text(REPO / "terraform" / "environments" / "dev" / "dev.tfvars")
    if not any(code in dev for code in ROLE_CODES):
        assert "neither switch is on in dev" in body
    # The reach ships off, and while no Terraform sets it, it is off everywhere.
    reach = f"{SETTINGS_PY}::ApiSettings.platform_admin_reach"
    assert f"`{reach}`" in raw and "= False" in _cited_symbol(*reach.split("::"))
    if not _mentions(REPO / "terraform", "PLATFORM_ADMIN_REACH", "*.tf*"):
        assert "no Terraform variable sets `PLATFORM_ADMIN_REACH`" in body
    # The acceptance probe: until one asserts the code, its lane is not built.
    if not _mentions(REPO / "tests" / "acceptance", "role_member_required", "*.py"):
        assert "**ROLES-ACCEPT (not built" in raw
    # Storage, read from roles.py.
    assert roles._MAC_DOMAIN == b"swarm-workspace-role-grant/v1\n"
    assert f"`{ROLES_PY}::_MAC_DOMAIN`" in raw and "`swarm-workspace-role-grant/v1`" in body
    assert f"{roles.SCAN_LIMIT} grants" in body and roles.SCAN_LIMIT == roles.PAGE_SIZE * roles.PAGES
    write = f"{ROLES_PY}::RoleStore.write"
    assert f"`{write}`" in raw and ".batch()" in _cited_symbol(*write.split("::"))
    # Departure: the fallback code was never needed; the role routes raise.
    for path in (ROLES_PY, "apps/swarm-api/swarm_api/routes/roles.py"):
        assert "role_grant_required" not in _text(REPO / path)
    assert "`role_grant_required`" in body and "was not needed" in body
    # Departure: the admin role routes see Terraform's groups only, while a
    # job-made group is in the reach. Say so until the code reads the registry.
    groups = f"{ROLES_PY}::registered_group_tenants"
    assert f"`{groups}`" in raw
    if "registry" not in _cited_symbol(*groups.split("::")).split('"""')[-1]:
        assert "a group workspace the workspace job made" in body and "404" in body
    # The human-step answer route is built and asks the role.
    assert f"`{ANSWER_GATE}`" in raw
    whole = _flat(_text(ROLES_DESIGN))
    assert "`human-steps.md` §4.2, not built yet" not in whole
    assert "(`human-steps.md` §4.2, not built)" not in whole


def test_human_steps_4_2_lets_a_platform_admin_answer_in_a_group_workspace():
    raw = _section(_text(HUMAN_STEPS), "### 4.2 Who may answer")
    body = _flat(raw)
    assert "Amended 2026-10-19" in body and "workspace-roles.md" in body
    assert "superseded for group workspaces" in body
    assert f"`{ANSWER_GATE}`" in raw
    gate = _cited_symbol(*ANSWER_GATE.split("::"))
    assert "may_act_in(" in gate and "roles.MEMBER" in gate and "fresh=True" in gate
    assert "`role_member_required`" in body and "personal workspace" in body
    built = _flat(_section(_text(HUMAN_STEPS), "## 8. What was built"))
    assert "ROLES-DOCS" in built


def test_secrets_design_q4_is_the_workspace_admin_now():
    text = _text(SECRETS_DESIGN)
    head = _flat(text.split("\n---\n", 1)[0])
    assert "workspace-roles.md" in head and "Q4" in head
    console = _section(text, "## 5. Console: Settings › Workspace › Secrets")
    assert f"`{SECRETS_GATE}`" in console
    gate = _cited_symbol(*SECRETS_GATE.split("::"))
    assert "workspace_admin(" in gate and "is_personal(" in gate
    admin = "apps/swarm-api/swarm_api/workspace_secrets.py::workspace_admin"
    assert f"`{admin}`" in console and "fresh=True" in _cited_symbol(*admin.split("::"))
    assert "`role_admin_required`" in _flat(console)
    q4 = next(ln for ln in _section(text, "## 10. Owner decisions (2026-10-14, ~22:40–23:05 PDT)").splitlines()
              if ln.startswith("| Q4"))
    assert "superseded" in q4 and "Q6" in q4


def test_security_doc_describes_workspace_roles_from_the_code():
    text = _text(SECURITY)
    raw = _section(text, ROLES_SECURITY)
    body = _flat(raw)
    assert "workspace-roles.md" in body
    reach = "apps/swarm-api/swarm_api/auth.py::Authenticator._reachable_groups"
    assert f"`{reach}`" in raw and 'startswith("u-")' in _cited_symbol(*reach.split("::"))
    audit = "apps/swarm-api/swarm_api/deps.py::current_auth"
    assert f"`{audit}`" in raw and "audit_platform_admin(" in _cited_symbol(*audit.split("::"))
    assert "`platform_admin_audit_unavailable`" in body and "fail closed" in body
    assert "`swarm-workspace-role-grant/v1`" in body
    for code in ROLE_CODES:
        assert f"`{code}`" in body, code
    assert "`PLATFORM_ADMIN_REACH`" in body
    auth = _flat(_section(text, "## Authentication"))
    assert "`PLATFORM_ADMIN_REACH`" in auth and "never anyone else's personal workspace" in auth


def test_workspaces_doc_names_the_workspace_roles_card():
    text = _text(WORKSPACES_DOC)
    head = _flat(text.split("\n---\n", 1)[0])
    assert "design/workspace-roles.md" in head
    people = _section(text, "### 6.4 Admin → People")
    assert "**5. Workspace roles.**" in people
    assert "`apps/swarm-ui/src/WorkspaceRolesAdmin.tsx` (`Name an admin`)" in people
    assert "`GET /v1/admin/workspaces/roles`" in _flat(people)
    admins = _flat(_section(text, "### 6.5 Admin roles move to Firestore"))
    assert "`PLATFORM_ADMIN_REACH`" in admins and "personal workspace" in admins


@pytest.mark.parametrize("doc", (ROLES_DESIGN, HUMAN_STEPS, SECRETS_DESIGN, SECURITY, WORKSPACES_DOC),
                         ids=lambda p: p.name)
def test_workspace_roles_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# QI-DOCS (part of #1649): a browser agent signs in to the console as the QA
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
# changing fails until the documents follow.

QI_DESIGN = DOCS / "design" / "qa-identity.md"
QI_MEASURED = DOCS / "design" / "qa-identity-measurements.md"
LIVE_BROWSER = DOCS / "design" / "live-browser.md"
QI_BUILT = "## 8. Measurements and what was built"
QI_M = "### 4.9 Measurements before the build (M1–M5) and the owner's steps"
QI_CARRY = "### 5.5.1 Reusing a signed-in session (LB-7)"
QI_WORKSPACE = "### 6.6 The QA identity's service workspace (qa-identity, #1649)"
QI_GATE_PY = "apps/agent-worker/agent_worker/livegate/qaidentity.py"
QI_WORKER_PY = "apps/agent-worker/agent_worker/qa_identity.py"
QI_API_PY = "apps/swarm-api/swarm_api/qa_identity.py"
QI_SESSION_PY = "apps/agent-worker/agent_worker/livegate/session.py"
QI_FRONT_DOOR = "apps/swarm-api/swarm_api/main.py::FrontDoor.__call__"
QI_DECIDED = "Amended 2026-10-20"
QI_INSTRUCT_PY = "apps/agent-worker/agent_worker/runners/claude_code.py"


def _qi_lanes() -> list[str]:
    plan = _section(_text(QI_DESIGN), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(QI-[A-Z]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 10 and "QI-DOCS" in lanes, lanes
    return lanes


def _qi_entry(raw: str, lane: str) -> str:
    """§8's entry for one lane: from `**<lane> (` to the next lane's entry."""
    start = raw.index(f"**{lane} (")
    nxt = re.search(r"^\* \*\*QI-", raw[start + 1:], flags=re.M)
    return _flat(raw[start: start + 1 + nxt.start()] if nxt else raw[start:])


def _qi_built(lane: str) -> bool:
    """Whether the lane's work is on this tree, read from the files that hold it.

    QI-INSTRUCT landed in the claude-code runner's live-browser note
    (`QA_IDENTITY_NOTE`), not in the browser image's `swarm_browser.py` its
    §6 row names, so it is read where it was built (#1649)."""
    runner = _text(REPO / QI_INSTRUCT_PY)
    accept = _text(REPO / "tests" / "acceptance" / "test_live_browser.py")
    marks = {
        "QI-CONTRACT": (REPO / "tests/unit/common/test_qa_identity_contract.py").is_file(),
        "QI-IAM": (REPO / "terraform/bootstrap/qa_identity.tf").is_file(),
        "QI-API": (REPO / QI_API_PY).is_file(),
        "QI-GATE": (REPO / QI_GATE_PY).is_file(),
        "QI-WORKER": (REPO / QI_WORKER_PY).is_file(),
        "QI-UI": (REPO / "apps/swarm-ui/src/QaIdentityGrants.tsx").is_file(),
        "QI-MCP": (REPO / "tests/unit/mcp/test_qa_identity_details.py").is_file(),
        "QI-INSTRUCT": "QA_IDENTITY_NOTE = (" in runner and "qa_identity_host" in runner,
        "QI-DOCS": True,
        "QI-ACCEPT": "QA_IDENTITY_CHECKS = (" in accept,
    }
    assert lane in marks, f"§6 names a lane {lane} this test does not know how to read"
    return marks[lane]


def test_qa_identity_design_no_longer_says_nothing_is_built():
    head = _flat(_text(QI_DESIGN).split("\n---\n", 1)[0])
    assert "Nothing in it is built" not in head
    assert "**Status: built" in head and "§8" in head
    # The header names what is still missing from the tree, and nothing else.
    missing = [lane for lane in _qi_lanes() if not _qi_built(lane)]
    if not missing:
        assert "except the agent's instructions" not in head, "every lane is built; the header says not"
        assert "acceptance case" in head and "accept.yml" in head


def test_qa_identity_design_8_cites_where_qi_instruct_was_built():
    entry = _qi_entry(_section(_text(QI_DESIGN), QI_BUILT), "QI-INSTRUCT")
    note = f"{QI_INSTRUCT_PY}::QA_IDENTITY_NOTE"
    assert f"`{note}`" in entry
    assert "qa_identity_host" in _cited_symbol(*note.split("::"))
    assert "swarm_browser.py" in entry, "§8 says why the code is not where §6's row put it"


def test_qa_identity_design_8_says_where_qi_accept_runs():
    """The case runs in accept.yml after a release, never in a pull request's
    CI, so §8 says so and names its checks as the acceptance file holds them."""
    entry = _qi_entry(_section(_text(QI_DESIGN), QI_BUILT), "QI-ACCEPT")
    if not _qi_built("QI-ACCEPT"):
        return
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "acceptance_live_browser", REPO / "tests" / "acceptance" / "test_live_browser.py")
    lb = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lb  # its dataclasses resolve their module by name
    try:
        spec.loader.exec_module(lb)
    finally:
        sys.modules.pop(spec.name, None)
    assert "accept.yml" in entry
    for check in lb.QA_IDENTITY_CHECKS:
        assert f"`{check}`" in entry, check


def test_qa_identity_design_records_what_every_lane_built():
    raw = _section(_text(QI_DESIGN), QI_BUILT)
    body = _flat(raw)
    assert "Nothing yet" not in body
    for lane in _qi_lanes():
        assert f"**{lane} (" in raw, f"§8 has no entry for {lane}"
        entry = _qi_entry(raw, lane)
        if _qi_built(lane):
            assert "not built" not in entry, f"{lane} is on this tree; §8 says it is not built"
        else:
            assert f"**{lane} (not built" in entry, f"{lane} is not on this tree; §8 must say so"
    # Departure 1: the header is `Authorization`, never `Proxy-Authorization` (M3).
    header = f"{QI_GATE_PY}::HEADER"
    assert f"`{header}`" in raw and '"Authorization"' in _cited_symbol(*header.split("::"))
    assert "Chromium refuses" in body
    # Departure 2: guard 2 strips a header by the credential in its value, not by name.
    strip = f"{QI_GATE_PY}::QaInjector"
    assert f"`{strip}`" in raw
    assert "by its value" in body
    # Departure 3: swarm-api removes the credential headers beside an assertion.
    removed = "apps/swarm-api/swarm_api/main.py::CREDENTIAL_HEADERS"
    assert f"`{removed}`" in raw and 'b"authorization"' in _cited_symbol(*removed.split("::"))
    # Departure 4: M4's fix lists the service tenant when it is already the default.
    listed = "apps/swarm-api/swarm_api/auth.py::Authenticator._list_own_service_default"
    assert f"`{listed}`" in raw
    _cited_symbol(*listed.split("::"))
    # The renewal point, read from the worker.
    renew = f"{QI_WORKER_PY}::RENEW_FRACTION"
    assert f"`{renew}`" in raw and "1 / 3" in _cited_symbol(*renew.split("::"))
    # The QA workspace cannot be granted its own identity.
    from swarm_api.qa_identity import QaIdentitySelf

    assert f"`{QaIdentitySelf.code}`" in body


def test_qa_identity_design_4_9_records_that_m2_is_not_a_gate():
    """The owner's 2026-10-20 decision, and either the M2 reading or that it is
    still to come, matching what the measurements document holds."""
    raw = _section(_text(QI_DESIGN), QI_M)
    body = _flat(raw)
    assert QI_DECIDED in body and "M2 is not a gate before the build" in body
    assert "`proxy_authorization_present`" in body
    assert f"`{QI_FRONT_DOOR}`" in raw
    assert "proxy_authorization_present=" in _cited_symbol(*QI_FRONT_DOOR.split("::"))
    measured = _text(QI_MEASURED)
    m2 = _flat(_section(measured, _heading(measured, "### M2")))
    if re.search(r"proxy_authorization_present=(true|false)", m2):
        assert "M2 was read" in body and "qa-identity-measurements.md" in body
    else:
        assert "still to come" in body and "qa-identity-measurements.md" in body


def test_qa_identity_design_6_records_what_qi_api_and_qi_gate_wait_for():
    body = _flat(_section(_text(QI_DESIGN), "## 6. Build plan"))
    assert QI_DECIDED in body and "M2 is not a gate before the build" in body
    assert "QI-API waits for M1, M3 and M4" in body
    assert "QI-GATE waits for M1, M3 and M5" in body


def test_live_browser_5_5_1_notes_the_qa_identity_host_is_never_carried():
    raw = _section(_text(LIVE_BROWSER), QI_CARRY)
    body = _flat(raw)
    assert "The QA identity's console host is never carried" in body
    assert "qa-identity.md" in body
    for scope in ("carry", "login", "park"):
        assert scope in body, scope
    excluded = f"{QI_SESSION_PY}::SessionCarry._excluded"
    assert f"`{excluded}`" in raw and "sent_to" in _cited_symbol(*excluded.split("::"))
    imported = f"{QI_SESSION_PY}::SessionCarry.import_jar"
    assert f"`{imported}`" in raw and "self._excluded" in _cited_symbol(*imported.split("::"))
    refused = "apps/agent-worker/agent_worker/livegate/__main__.py::LiveGate.pause"
    assert f"`{refused}`" in raw and "qa_identity_host" in _cited_symbol(*refused.split("::"))
    assert "`qa_identity_host`" in body


def test_workspaces_doc_describes_the_qa_identitys_service_workspace():
    from swarm_api.qa_identity import GRANTS_COLLECTION, QaIdentitySelf

    text = _text(WORKSPACES_DOC)
    head = _flat(text.split("\n---\n", 1)[0])
    assert "design/qa-identity.md" in head and "§6.6" in head
    raw = _section(text, QI_WORKSPACE)
    body = _flat(raw)
    locals_tf = _text(LOCALS_TF)
    assert '"qa-console@${var.project_id}.iam.gserviceaccount.com"' in locals_tf
    assert "`qa-console@<project>.iam.gserviceaccount.com`" in body
    assert 'var.environment == "dev"' in locals_tf and "dev only" in body
    assert "Admin › Tenants › New workspace › Service" in body
    assert "no account is lent" in body
    # What the QA account submits parks at admission; the WD6 gate exempts it.
    gate = "apps/swarm-api/swarm_api/workspaces.py::Workspaces.applies_to"
    assert f"`{gate}`" in raw and "is_service_identity" in _cited_symbol(*gate.split("::"))
    admit = "apps/scheduler/scheduler/loop.py::Scheduler._admit_one"
    assert f"`{admit}`" in raw
    assert "ParkReason.CREDENTIAL_MISSING" in _cited_symbol(*admit.split("::"))
    assert "parks `CREDENTIAL_MISSING` at scheduler admission, before any lease" in body
    assert "stays `QUEUED`" not in body
    assert f"`{GRANTS_COLLECTION}`" in body and f"`{QaIdentitySelf.code}`" in body
    own = f"{QI_API_PY}::own_tenant"
    assert f"`{own}`" in raw and "tenant_id_for_user" in _cited_symbol(*own.split("::"))
    routes = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "qa_identity.py")
    for path in ("/qa-identities/mine", "/admin/qa-identities/{identity}/grants/{tenant_id}"):
        assert f'"{path}"' in routes, path
        assert f"`/v1{path}`" in body, path
    assert "`WORKSPACE_SERVICE_PLATFORM`" in body


@pytest.mark.parametrize("doc", (QI_DESIGN, LIVE_BROWSER, WORKSPACES_DOC), ids=lambda p: p.name)
def test_qa_identity_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# Stale status headers (#1384, #1649): graph-service.md and visual-qa.md
# --------------------------------------------------------------------------
#
# Both documents were written as designs and still opened with "Nothing
# described here is built" / "Nothing here is built" after their lanes had
# landed. Each header now lists the lanes of its build plan, and a lane is
# called built or not built by reading the files its row names, so the next
# lane landing (GS-RETIRE) fails here until the header follows.

GRAPH_SERVICE = DOCS / "design" / "graph-service.md"
VISUAL_QA = DOCS / "design" / "visual-qa.md"


def _head(path: Path) -> str:
    return _flat(_text(path).split("\n---\n", 1)[0])


def _gs_lanes() -> list[str]:
    plan = _section(_text(GRAPH_SERVICE), "## 11. Lanes")
    lanes = re.findall(r"^\| \*\*(GS-[A-Z]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 12 and "GS-RETIRE" in lanes, lanes
    return lanes


def _gs_built(lane: str) -> bool:
    """Whether the lane's work is on this tree, read from the files its §11 row names."""
    def has(rel: str, needle: str | None = None) -> bool:
        path = REPO / rel
        if not path.exists():
            return False
        return needle is None or needle in _text(path)

    marks = {
        "GS-MEASURE": has("tests/unit/worker/test_graph_ready_mark.py"),
        "GS-CORE": has("tests/unit/worker/test_graphmcp_call_graph_index.py"),
        "GS-TICKET": has("apps/swarm-api/swarm_api/routes/graph_ticket.py"),
        "GS-INDEXDOC": has("images/agent-runtime-indexer/repo-index/repo_graph_shards.py",
                           "document_shards"),
        "GS-PUSHAPI": has("tests/unit/control_plane/test_repository_index_push.py"),
        "GS-SETTING": has("apps/swarm-api/swarm_api/codeintel.py", "graph_delivery"),
        "GS-SVC": has("apps/swarm-graph/swarm_graph"),
        "GS-HOOKS": has("apps/swarm-github-hooks"),
        "GS-SHIM": has("apps/agent-worker/agent_worker/graphmcp/shim.py"),
        "GS-PACK": has("apps/agent-worker/agent_worker/contextpack/build.py", "class ServiceSource"),
        "GS-INFRA": has("terraform/infra/graph_service.tf"),
        "GS-WORKER": has("apps/agent-worker/agent_worker/graph_ticket.py"),
        # GS-RETIRE deletes staging: built once `write_snapshot` is gone.
        "GS-RETIRE": not has("apps/agent-worker/agent_worker/graphstage.py", "def write_snapshot("),
    }
    assert lane in marks, f"§11 names a lane {lane} this test does not know how to read"
    return marks[lane]


def test_graph_service_header_no_longer_says_nothing_is_built():
    head = _head(GRAPH_SERVICE)
    assert "Nothing described here is built" not in head
    assert "**Status: built" in head and "§11" in head
    for lane in _gs_lanes():
        assert lane in head, f"the status header does not name {lane}"
        if _gs_built(lane):
            assert f"{lane} (not built" not in head, f"{lane} is on this tree; the header says not"
        else:
            assert f"{lane} (not built" in head, f"{lane} is not on this tree; the header must say so"
    # What remains past the lanes: §8.2 step 5's widening, which is the owner's.
    assert "Widening" in head and "§8.2" in head


def _vq_lanes() -> list[str]:
    plan = _section(_text(VISUAL_QA), "## 6. Build plan")
    lanes = re.findall(r"^\|[^|\n]*\| \*\*([A-Z]+-[A-Z]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 7 and "QA-CAPTURE" in lanes and "QA-PROOF" in lanes, lanes
    return lanes


def test_visual_qa_header_no_longer_says_nothing_is_built():
    head = _head(VISUAL_QA)
    assert "Nothing here is built" not in head
    assert "**Status: built" in head
    for lane in _vq_lanes():
        assert lane in head, f"the status header does not name {lane}"
    built = {
        "QA-CAPTURE": "images/agent-runtime-browser/swarm_qa.py",
        "QA-TEMPLATE": "docs/qa/console-qa.workflow.json",
        "QA-KIT": "tests/unit/worker/test_qa_kit.py",
        "QA-FLOWS": "tests/unit/worker/test_qa_flows.py",
    }
    for lane, rel in built.items():
        assert (REPO / rel).is_file(), f"{lane}'s {rel} is gone; the header calls it built"
        assert f"`{rel}`" in head, f"the header does not cite where {lane} was built"
    # The signed-in arm is QA-IDENTITY's build, and the header points there.
    assert "qa-identity.md" in head and "§8" in head


# --------------------------------------------------------------------------
# LIVE-NOMASK (part of #2298): the live gate paints nothing over the screen
# --------------------------------------------------------------------------
#
# BGD decided on 2026-10-25 that no part of what the live browser shows is
# redacted on screen. live-browser.md §5.3 described a fail-closed mask that
# painted boxes and blanked pages; it now records the decision, says what the
# gate still does, and keeps the text redaction and the no-logging rule. The
# rest of the document, the relay's recorder and the other docs follow.

LB_53 = "### 5.3 The screen leaves the pod as Chromium drew it (LB-3, amended 2026-10-25)"
LB_DECISION = (
    "Stop redacting elements or sections of the visual content that a browser agent or "
    "session displays via the live browser. There is no need to redact parts of the screen "
    "in any situation."
)


def _lb_53() -> str:
    text = _text(LIVE_BROWSER)
    start = text.index(LB_53)
    return text[start: text.index("### 5.4 ", start)]


def test_live_browser_5_3_records_bgds_decision_and_why():
    body = _flat(_lb_53())
    assert "BGD's decision, 2026-10-25" in body and LB_DECISION in body
    for issue in ("#1030", "#1340", "#1344"):
        assert issue in body, issue
    # What the gate still does with the page, as built.
    for kept in ("`SHOWN_IN_CONTROL`", "`LiveGate._on_challenge`", "`Target.setAutoAttach`",
                 "`REASON_BROWSER_CLOSED`", "`FrameHub.publish`", "`report.py`", "`pointer.py`"):
        assert kept in body, kept
    assert "`page_blanked_ms` is gone" in body
    assert "Every **text** redaction on the path is unchanged" in body
    assert "#### 5.3.3 Keys never logged, anywhere" in _lb_53()
    assert "**No hop logs RFB payloads**" in body


def test_live_browser_no_longer_says_frames_are_masked():
    text = _flat(_text(LIVE_BROWSER))
    for stale in (
        "capture -> MASK -> encode",
        "leave it only through the tunnel, already masked",
        "The mask needs its own CDP session",
        "Watching shows masked pixels",
        "The masked frames, and nothing unmasked",
        "already receives every masked frame",
        "as a player over the masked segments",
        "Masking (§5.3) is device-independent",
        "the mask still covers its credential fields",
        "MaskState.on_challenge",
        "So the gate **fails closed**",
    ):
        assert stale not in text, stale
    # The one remaining mention of LB-3's masking says it is superseded.
    lb3 = next(line for line in _text(LIVE_BROWSER).splitlines() if line.startswith("| **LB-3** |"))
    assert "Superseded for pixels by BGD, 2026-10-25" in lb3


def test_the_recorder_and_other_docs_no_longer_say_frames_are_masked():
    recorder = (REPO / "apps" / "swarm-live" / "swarm_live" / "recorder.py").read_text(encoding="utf-8")
    head = recorder.split('"""', 2)[1]
    assert "masked frame" not in head and "unmasked" not in head
    assert "2026-10-25" in head
    egress = _flat(_text(DOCS / "browser-egress.md"))
    assert "The mask and the input path are unchanged" not in egress
    assert "(the mask needs it)" not in egress
    any_app = _flat(_text(DOCS / "design" / "visual-qa-any-app.md"))
    assert "the gate's mask paints every input" not in any_app
    assert "screencast's mask already paints" not in any_app


# --------------------------------------------------------------------------
# Runner sizes: docs/design/runner-sizes.md §6 lane SIZES-DOCS (part of #1689)
# --------------------------------------------------------------------------
#
# The design's status line said "designed" while nine lanes of §6 shipped;
# architecture.md still said "requests == limits, no bursting" and gave the
# browser class as 8 / 16; kubernetes/README.md said only `browser` reaches
# Autopilot; and locals.tf called its four-field table "byte-identical" to a
# catalogue that now has a CPU limit. Each test reads the value from the code.

SIZES_DESIGN = DOCS / "design" / "runner-sizes.md"
ARCHITECTURE = DOCS / "architecture.md"
K8S_README = REPO / "kubernetes" / "README.md"
SIZES_INVARIANT = "### Memory never bursts; CPU may burst past its request"
SIZES_SETTING = "### Runner sizes are a versioned setting, pinned at admission"
SIZES_CLASSES = "### Resource classes were measured, not guessed"
SIZES_K8S = "**Memory is one number; CPU bursts to its class's limit.**"
SIZES_K8S_NOT = "## What this directory does NOT cover"
SIZES_POLICY_TEXT = "a cpu limit must be between its request and twice it (invariant 7, RS-1)"


def _sizes_lanes() -> list[str]:
    text = _text(SIZES_DESIGN)
    # Both phases: §6's `### Phase` headings would end `_section` at the first.
    plan = text[text.index("## 6. Build plan"): text.index("## 7. Owner questions")]
    lanes = re.findall(r"^\*\*(SIZES-[A-Z0-9-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 10 and "SIZES-DOCS" in lanes, lanes
    return lanes


def _sizes_built(lane: str) -> bool:
    """Whether the lane's work is on this tree, read from the files its §6 entry names."""
    detail = _text(REPO / "apps" / "swarm-ui" / "src" / "AgentDetail.tsx")
    marks = {
        "SIZES-CONTRACT-1": (REPO / "tests/unit/common/test_cpu_burst_contract.py").is_file(),
        "SIZES-SCHED-1": (REPO / "tests/unit/scheduler/test_dispatch_cpu_burst.py").is_file(),
        "SIZES-WORKER-1": (REPO / "tests/unit/worker/test_sized_limits.py").is_file(),
        "SIZES-CONTRACT-2": (REPO / "tests/unit/common/test_runner_size_bounds.py").is_file(),
        "SIZES-API": (REPO / "apps/swarm-api/swarm_api/runnersizes.py").is_file(),
        "SIZES-SCHED-2": (REPO / "apps/scheduler/scheduler/sizes.py").is_file(),
        "SIZES-UI": (REPO / "apps/swarm-ui/src/RunnerSizes.tsx").is_file(),
        "SIZES-DETAIL": "bursts to" in detail,
        "SIZES-MCP": (REPO / "tests/unit/mcp/test_profiles_sizes_served.py").is_file(),
        "SIZES-DOCS": True,
    }
    assert lane in marks, f"§6 names a lane {lane} this test does not know how to read"
    return marks[lane]


def _sizes_status() -> str:
    head = _text(SIZES_DESIGN).split("\n---\n", 1)[0]
    status = next((p for p in head.split("\n\n") if p.startswith("**Status:")), None)
    assert status is not None, "runner-sizes.md has no **Status: paragraph above its first rule"
    return _flat(status)


def _profiles_on(backend) -> list[str]:
    from swarm_common.profiles import resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is backend)


def test_runner_sizes_status_line_names_every_merged_lane():
    status = _sizes_status()
    # MUTATION: leave the status as "designed 2026-10-20" -> red.
    assert status.startswith("**Status: built"), status[:120]
    for lane in _sizes_lanes():
        if _sizes_built(lane):
            # MUTATION: drop any lane's name from the status line -> red.
            assert f"`{lane}`" in status, f"{lane} is on this tree; the status line does not name it"
            assert f"`{lane}` (not built" not in status, lane
        else:
            assert f"`{lane}` (not built" in status, f"{lane} is not on this tree; the status must say so"
    # MUTATION: drop the canary's record (issue #1689's comment) -> red.
    assert "canary passed 2026-10-20" in status
    # MUTATION: drop the history the build lanes cite -> red.
    assert "designed 2026-10-20" in status and "every §7 question took its recommendation" in status


def test_architecture_states_invariant_7_as_amended_from_the_code():
    from swarm_common.profiles import CPU_BURST_MAX_RATIO

    text = _text(ARCHITECTURE)
    # MUTATION: restore the "### requests == limits, no bursting" heading -> red.
    assert "### requests == limits, no bursting" not in text
    raw = _section(text, SIZES_INVARIANT)
    body = _flat(raw)
    # MUTATION: write a ratio other than the frozen constant's -> red.
    assert f"`CPU_BURST_MAX_RATIO` ({CPU_BURST_MAX_RATIO:g})" in body
    assert "`apps/common/swarm_common/profiles.py::CPU_BURST_MAX_RATIO`" in raw
    # MUTATION: drop "contract request 108" (RS-1's filed number) -> red.
    assert "contract request 108" in body and "RS-1" in body
    for phrase in ("throttled, never killed", "Autopilot bills requests", "Burstable",
                   "Cloud Run renders the request alone", "invariant 8"):
        # MUTATION: delete any one of these reasons -> red.
        assert phrase in body, phrase
    # The renderer and the admission policy, cited where they hold it.
    manifest = f"{DISPATCH_PY}::{MANIFEST}"
    assert f"`{manifest}`" in raw and "cpu_quantity(" in _cited_symbol(DISPATCH_PY, MANIFEST)
    assert f"`kubernetes/policies/pod-security.yaml` (`{SIZES_POLICY_TEXT}`)" in raw
    # The invariant table's row 7 names where the amended rule is held.
    row = next(line for line in _section(text, "## 6. Where the code enforces each invariant").splitlines()
               if line.startswith("| 7."))
    # MUTATION: leave row 7 as "| 7. requests == limits |" -> red.
    assert "| 7. requests == limits |" not in row
    for where in ("scheduler/dispatch.py", "scheduler/sizes.py", "kubernetes/policies/pod-security.yaml",
                  "check_size"):
        assert where in row, where


def test_architecture_resource_classes_are_the_catalogues():
    from swarm_common.profiles import RESOURCE_CLASSES

    text = _text(ARCHITECTURE)
    raw = _section(text, SIZES_CLASSES)
    for name, rc in RESOURCE_CLASSES.items():
        # MUTATION: write browser as 8 / 16 again, or any number off the catalogue -> red.
        row = (f"| `{name}` | {rc.cpu:g} | {rc.cpu_limit_cores:g} | {rc.memory_gib} GiB "
               f"| {rc.disk_gib} GiB | {rc.units} |")
        assert row in raw, f"architecture.md's class table has no row {row}"
    # The retracted-risk section gives the same tmpfs slices.
    # MUTATION: restore "`browser` ~8 of 16" -> red.
    assert "~8 of 16" not in text
    for name, rc in RESOURCE_CLASSES.items():
        assert f"`{name}` ~{rc.disk_gib} GiB of its {rc.memory_gib}" in _flat(text), name


def test_architecture_says_where_runner_sizes_live():
    from swarm_api import runnersizes
    from swarm_common.profiles import Backend

    text = _text(ARCHITECTURE)
    raw = _section(text, SIZES_SETTING)
    body = _flat(raw)
    # MUTATION: rename either document in the doc (or in runnersizes.py) -> red.
    assert f"`control/{runnersizes.POINTER_DOC}`" in body
    assert f"`{runnersizes.VERSIONS}/{{version}}`" in body
    admin = _text(REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "admin.py")
    for route in ("/runner-sizes", "/runner-sizes:preview", "/runner-sizes/restore", "/runner-sizes/versions"):
        assert f'"{route}"' in admin, route
    assert "`PUT /v1/admin/runner-sizes`" in body and "Admin › Runner sizes" in body
    # Pinned at admission, cited where the scheduler does it.
    current = "apps/scheduler/scheduler/loop.py::Scheduler._size_version"
    pinned = "apps/scheduler/scheduler/loop.py::Scheduler._admit_one"
    # MUTATION: cite a method that no longer reads the version -> red.
    assert f"`{current}`" in raw and "sizes_mod.current(" in _cited_symbol(*current.split("::"))
    assert f"`{pinned}`" in raw and "sizes_mod.for_task(" in _cited_symbol(*pinned.split("::"))
    assert "`size_version`" in body and "a running agent keeps the size it was admitted with" in body
    # Which profiles follow and which cannot, from the catalogue.
    for name in _profiles_on(Backend.GKE_AUTOPILOT) + _profiles_on(Backend.CLOUD_RUN_JOB):
        # MUTATION: drop a profile from either list -> red.
        assert f"`{name}`" in body, name
    # The owner's answers that bind an admin (§7 Q4-Q6).
    assert "`disk_gib` is shown, not edited" in body
    assert f"`{runnersizes.BelowRecentPeak.code}`" in body and "typed confirmation" in body
    assert "a reason" in body and "restore" in body
    # The Firestore table lists the two documents.
    model = _section(text, "## 4. Data model")
    assert f"| `control/{runnersizes.POINTER_DOC}` |" in model
    assert f"| `{runnersizes.VERSIONS}/{{version}}` |" in model


def test_kubernetes_readme_describes_cpu_burst_and_where_sizes_live():
    from swarm_common.profiles import CPU_BURST_MAX_RATIO, Backend

    text = _text(K8S_README)
    start = text.index(SIZES_K8S)
    raw = text[start: text.index("\n\n", start)]
    body = _flat(raw)
    # MUTATION: drop the paragraph, or name a ratio other than the constant -> red.
    assert f"`CPU_BURST_MAX_RATIO` ({CPU_BURST_MAX_RATIO:g})" in body
    assert f"`kubernetes/policies/pod-security.yaml` (`{SIZES_POLICY_TEXT}`)" in raw
    quota = "kubernetes/render.py::tenant_values"
    assert f"`{quota}`" in raw and "QUOTA_CPU_LIMIT" in _cited_symbol(*quota.split("::"))
    job = "kubernetes/render.py::render_job"
    # MUTATION: say render.py renders the admin-set size -> red (it renders the code default).
    assert f"`{job}`" in raw and "rc.cpu_limit_cores" in _cited_symbol(*job.split("::"))
    assert "code defaults" in body and "runner-sizes.md" in body
    assert "PR 1747" in body
    # The layout row no longer says sizing comes from "the frozen catalogue" alone.
    layout = next(line for line in text.splitlines() if line.startswith("| `render.py` |"))
    assert "code defaults" in layout, layout
    # Which profiles reach Autopilot, from the catalogue.
    gke = _profiles_on(Backend.GKE_AUTOPILOT)
    cloud_run = _profiles_on(Backend.CLOUD_RUN_JOB)
    section = _flat(_section(text, SIZES_K8S_NOT))
    # MUTATION: restore "only `browser` ... reaches Autopilot" -> red.
    assert "are all pinned to `Backend.CLOUD_RUN_JOB`" not in section
    assert "only `browser`" not in section
    for name in gke + cloud_run:
        assert f"`{name}`" in section, name
    intro = _flat(text.split("## Layout", 1)[0])
    assert "Cloud Run Jobs is the primary backend" not in intro


def test_locals_tf_says_its_class_table_is_the_cloud_run_default():
    text = _text(LOCALS_TF)
    start = text.index("# --- swarm_common.profiles.RESOURCE_CLASSES")
    comment = _flat(text[start: text.index("resource_classes = {", start)].replace("#", " "))
    # MUTATION: leave "Keep this table byte-identical" (it has no cpu_limit) -> red.
    assert "byte-identical" not in comment
    for phrase in ("code defaults", "Cloud Run", "renders the request alone", "cpu_limit",
                   "never reaches Terraform", "runner-sizes.md §4.7"):
        assert phrase in comment, phrase
    # Values unchanged: the four fields still equal the catalogue's (the tftest holds them too).
    from swarm_common.profiles import RESOURCE_CLASSES

    for name, rc in RESOURCE_CLASSES.items():
        assert re.search(rf"\b{name}\s*=\s*\{{ cpu = {rc.cpu:g}, memory_gib = {rc.memory_gib}, "
                         rf"disk_gib = {rc.disk_gib}, units = {rc.units} \}}", text), name


@pytest.mark.parametrize("doc", (ARCHITECTURE, K8S_README), ids=lambda p: p.name)
def test_runner_sizes_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# Test services: docs/design/test-services.md §11 (TS-DOCS-GUARDS, part of #1627)
# --------------------------------------------------------------------------
#
# TS-DOCS (PR 2214) wrote §11, what each lane of §7 built and where the code
# departs from the design, docs/test-services.md, the page a repository reads
# to write `.swarm/test-services.toml`, and the test-services paragraph of
# docs/agent-output.md's pre-publish section. Every file, symbol, test, value
# and message they state is read from the tree here. Each check takes the
# document's text, so the mutation tests below hand it a one-line edit of the
# real text and hold that it fails: the proof a check can go red, kept in CI.

TS_DESIGN = DOCS / "design" / "test-services.md"
TS_DOC = DOCS / "test-services.md"
TS_PY = "apps/agent-worker/agent_worker/testservices.py"
TS_PREPUBLISH_PY = "apps/agent-worker/agent_worker/prepublish.py"
TS_DOCKERFILE = "images/agent-runtime-base/Dockerfile"
TS_PLAN = "## 7. Cost and the build plan"
TS_BUILT = "## 11. What was built"
TS_NOT_BUILT = "### 11.8 Not built"
TS_ROWS = "### 5.3 `skipped`, `failed`, `error` once a service is declared"
TS_AGENT_OUTPUT = "**A service the tests need**"
#: Lanes §10 and §11 added after §7's table was written (BGD, 2026-10-24).
TS_ADDED_LANES = ("TS-CLOUDRUN-REFUSE",)
#: A backticked citation into the tree, with or without `::qualname`.
_TS_CITE = re.compile(rf"`({_ROOTS}[\w./-]*?)(?:::([\w.]+))?`")


def _ts_lanes(text: str) -> list[str]:
    plan = text[text.index(TS_PLAN): text.index("\n## 8. ")]
    lanes = re.findall(r"^\|[^|\n]*\| \*\*(TS-[A-Z-]+)\*\*", plan, flags=re.M)
    assert len(lanes) >= 7 and "TS-DOCS" in lanes, lanes
    return lanes + list(TS_ADDED_LANES)


def _ts_built(lane: str) -> bool:
    """Whether the lane's work is on this tree, read from the files its §7 row names."""
    from agent_worker import testservices

    accept = [p for p in (REPO / "tests" / "acceptance").rglob("*")
              if p.is_file() and testservices.DECLARATION_PATH in p.read_text(errors="replace")]
    marks = {
        "TS-IMAGE": "ARG POSTGRES_MAJOR=16" in _text(REPO / TS_DOCKERFILE),
        "TS-CATALOGUE": (REPO / "tests/unit/worker/test_testservices_declaration.py").is_file(),
        "TS-RUN": (REPO / "tests/unit/worker/test_testservices_process.py").is_file(),
        "TS-CLOUDRUN-REFUSE": hasattr(testservices, "NO_CACHE_VOLUME"),
        "TS-GATE": (REPO / "tests/unit/worker/test_prepublish_test_services.py").is_file(),
        "TS-ACCEPT": bool(accept),
        "TS-DOCS": True,
        # §7: "the pre-publish record's skipped addresses counted by port".
        "TS-SIGNAL": "skipped_addresses" in _text(REPO / TS_PREPUBLISH_PY),
    }
    assert lane in marks, f"§7 names a lane {lane} this test does not know how to read"
    return marks[lane]


def _ts_section_11(text: str) -> str:
    start = text.index(TS_BUILT)
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _ts_subsections(text: str) -> dict[str, str]:
    """§11's `### 11.N` subsections, by heading."""
    parts = re.split(r"^(### 11\.\d+ .*)$", _ts_section_11(text), flags=re.M)
    subs = dict(zip(parts[1::2], parts[2::2]))
    assert len(subs) >= 8, list(subs)
    return subs


def _ts_entry(text: str, lane: str) -> str:
    return next(body for heading, body in _ts_subsections(text).items()
                if heading.endswith(f" What {lane} built"))


def _check_ts_lanes(text: str) -> None:
    head = _flat(text.split("\n---\n", 1)[0])
    subs = _ts_subsections(text)
    not_built = subs[TS_NOT_BUILT]
    lanes = _ts_lanes(text)
    built = [lane for lane in lanes if _ts_built(lane)]
    unbuilt = [lane for lane in lanes if lane not in built]
    for lane in built:
        assert any(h.endswith(f" What {lane} built") for h in subs), (
            f"{lane} is on this tree; §11 has no 'What {lane} built'")
        assert f"**{lane} (not built" not in not_built, f"{lane} is on this tree; §11.8 says it is not"
        if lane != "TS-DOCS":
            assert lane in head, f"{lane} is on this tree; the status line does not name it"
    for lane in unbuilt:
        assert f"**{lane} (not built" in not_built, f"{lane} is not on this tree; §11.8 must say so"
        assert not any(h.endswith(f" What {lane} built") for h in subs), lane
    named = " and ".join(unbuilt)
    assert f"{named} are not built" in head or f"{named} is not built" in head, head[:400]
    assert "**Status: designed 2026-10-23" in head and "§11 records" in head


def test_test_services_design_records_what_every_lane_built():
    _check_ts_lanes(_text(TS_DESIGN))


def _cited_source(path: str) -> str:
    target = REPO / path
    return target.read_text(encoding="utf-8") if target.is_file() else ""


def _check_ts_entries_name_what_exists(text: str) -> None:
    from agent_worker import cachedirs, prepublish, testservices

    env = set(prepublish.service_env([testservices.DeclaredService("postgres", 16, ("x",))]))
    cited: set[str] = set()
    for heading, body in _ts_subsections(text).items():
        for path, qualname in _TS_CITE.findall(body):
            assert (REPO / path).exists(), f"{heading} names {path}, which does not exist"
            if qualname:
                _cited_symbol(path, qualname)
            cited.add(path)
    # A bare name (`unreachable_services`, `PGDG_KEY_FINGERPRINT`) is one a file
    # §11 cites holds, or a variable the code sets.
    sources = "\n".join(_cited_source(path) for path in sorted(cited))
    for name in set(re.findall(r"`([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|[A-Z]{4,})`", _ts_section_11(text))):
        assert name in sources or name in env or name == cachedirs.CACHE_ROOT_ENV, (
            f"§11 names `{name}`, which no file it cites holds")
    subs = _ts_subsections(text)
    # The tests each entry says hold its rule are tests on main.
    image = _text(REPO / "tests/unit/scripts/test_agent_toolbox_image.py")
    image_tests = re.findall(r"^def (test_\w+)", image, flags=re.M)
    entry = _flat(_ts_entry(text, "TS-IMAGE"))
    assert "the PGDG, fingerprint, layer and smoke-step tests in" in entry
    for word in ("pgdg", "fingerprint", "layer", "smoke_step"):
        assert any(word in t for t in image_tests), word
    process = _text(REPO / "tests/unit/worker/test_testservices_process.py")
    assert "the cache-volume tests in" in _flat(_ts_entry(text, "TS-CLOUDRUN-REFUSE"))
    assert re.search(r"^def test_\w*cache_volume", process, flags=re.M)
    gate = _text(REPO / "tests/unit/worker/test_prepublish_test_services.py")
    rows = [ln for ln in _section(text, TS_ROWS).splitlines() if ln.startswith("| ") and "---" not in ln][1:]
    assert len(rows) == 8, rows
    assert "one test per row and the issue's acceptance pair" in _flat(_ts_entry(text, "TS-GATE"))
    for n in range(1, len(rows) + 1):
        assert re.search(rf"^def test_row{n}_", gate, flags=re.M), f"no test for §5.3 row {n}"
    assert re.search(r"^def test_acceptance_", gate, flags=re.M)
    assert "offline acceptance pair in `tests/unit/worker/test_prepublish_test_services.py`" in _flat(
        subs[TS_NOT_BUILT])


def test_test_services_design_11_names_only_what_is_on_main():
    _check_ts_entries_name_what_exists(_text(TS_DESIGN))


def _check_ts_entries_state_the_code(text: str) -> None:
    from pathlib import PurePath

    from agent_worker import prepublish, testservices as ts

    catalogue = _flat(_ts_entry(text, "TS-CATALOGUE"))
    assert set(ts.CATALOGUE) == {"postgres"} and f"`postgres`, versions `{ts.CATALOGUE['postgres'].versions}`" in catalogue
    keys = ", ".join(f"`{k}`" for k in ts.ALLOWED_KEYS)
    assert f"`{TS_PY}::ALLOWED_KEYS` ({keys})" in catalogue
    run = _flat(_ts_entry(text, "TS-RUN"))
    argv = ts.initdb_argv(16, PurePath("data"))
    for flag in re.findall(r"`(--[\w=-]+)`", run):
        assert flag in argv, f"§11.3 names {flag}; initdb_argv does not pass it"
    assert f"`{TS_PY}::OOM_SCORE_ADJ` ({ts.OOM_SCORE_ADJ})" in run
    assert f"every {ts.READY_INTERVAL_SECONDS:g} s" in run
    assert f"last {ts.LOG_TAIL_LINES} log lines" in run
    assert f"kills after {ts.STOP_GRACE_SECONDS:g} s" in run
    assert len(ts.INITDB_DATABASES) == 3 and "the three `initdb` makes itself" in run
    assert f"`listen_addresses={ts.LISTEN_HOST}`" in run
    refuse = _flat(_ts_entry(text, "TS-CLOUDRUN-REFUSE"))
    assert f"`postgres: service not started: {ts.NO_CACHE_VOLUME}`" in refuse
    gate = _flat(_ts_entry(text, "TS-GATE"))
    assert prepublish.TEST_SERVICES_CHECK == "test-services" and "a `test-services` check" in gate
    assert "graph-pytest" in prepublish.SERVICE_CHECKS and "(`graph-pytest` and the guards script)" in gate
    probe = " ".join(prepublish.probe_argv(ts.DeclaredService("postgres", 16, ("x",))))
    assert f"-h {ts.LISTEN_HOST} -p {ts.LISTEN_PORT}" in probe
    assert f"`pg_isready` over TCP on `{ts.LISTEN_HOST}:{ts.LISTEN_PORT}`" in gate
    env = prepublish.service_env([ts.DeclaredService("postgres", 16, ("x",))])
    named = ", ".join(f"`{k}`" for k in env)
    assert f"{named.rsplit(', ', 1)[0]} and {named.rsplit(', ', 1)[1]}" in gate, named
    assert 'prefix="pp-svc-"' in _cited_symbol(TS_PREPUBLISH_PY, "Gate._services")
    assert "under a `pp-svc-` directory" in gate
    assert "(error, not the change's)" in _cited_symbol(TS_PREPUBLISH_PY, "advisory_section")
    assert '"test-services (error, not the change\'s)"' in gate
    assert "and not declared in" in _cited_symbol(TS_PREPUBLISH_PY, "_pytest_project")
    assert f'"and not declared in {ts.DECLARATION_PATH}"' in gate


def test_test_services_design_11_states_what_the_code_holds():
    _check_ts_entries_state_the_code(_text(TS_DESIGN))


def _check_ts_doc(text: str) -> None:
    from agent_worker import prepublish, testservices as ts

    assert text.startswith(f"# Declaring the services your tests need: `{ts.DECLARATION_PATH}`")
    body = _flat(text)
    # Sliced, not `_section`: the example's own `# ...` comments read as headings.
    the_file = text[text.index("## The file"): text.index("## The catalogue")]
    example = re.search(r"^```toml\n(.*?)^```", the_file, flags=re.M | re.S)
    assert example, "the page has no example declaration"
    declared = ts.parse_declaration(example.group(1).encode())
    assert declared.valid and declared.declared, declared.report()
    assert f"a list of 1 to {ts.MAX_DATABASES} names, each matching `{ts.DATABASE_NAME.pattern}`" in body
    assert f"at most {ts.MAX_DECLARATION_BYTES} bytes" in body
    assert f"with versions `{ts.CATALOGUE['postgres'].versions}` and nothing else" in body
    settings = dict(ts.POSTGRES_SETTINGS)
    for name, value in re.findall(r"`(\w+)=(\w+)`", _section(text, "## The catalogue: PostgreSQL 16, one entry")):
        assert settings.get(name) == value, f"the page says {name}={value}; POSTGRES_SETTINGS says {settings.get(name)}"
    # The refused keys: the same set, each row the code's reason without its § reference.
    refused = _section(text, "## The keys that are refused")
    rows = re.findall(r"^\| ((?:`\w+`(?:, )?)+) \| (.+) \|$", refused, flags=re.M)
    seen: set[str] = set()
    for keys, why in rows:
        names = re.findall(r"`(\w+)`", keys)
        seen |= set(names)
        reasons = {re.sub(r" \(§[\d.]+\)", "", ts._REFUSED_BECAUSE[k]).replace("127.0.0.1:5432", "`127.0.0.1:5432`")
                   for k in names if k in ts._REFUSED_BECAUSE}
        assert why in reasons, f"the page refuses {names} because {why!r}; the code says {reasons}"
    assert seen == set(ts._REFUSED_BECAUSE), sorted(seen ^ set(ts._REFUSED_BECAUSE))
    # The example refusals are what the worker prints.
    shown = re.search(r"^```text\n(.*?)^```", refused, flags=re.M | re.S)
    assert shown, "the page has no example refusal"
    printed = ts.validate({"postgres": {"image": "x", "version": 15, "databases": ["app_test"]}}).report()
    assert shown.group(1).strip() == printed
    # The checks' environment.
    env = prepublish.service_env([ts.DeclaredService("postgres", 16, ("<first database>",))])
    table = {k: v for k, v in re.findall(r"^\| `(\w+)` \| (.+) \|$", _section(text, "## What your repository gets"),
                                         flags=re.M)}
    assert set(table) == set(env), sorted(set(table) ^ set(env))
    for name in ("PGHOST", "PGPORT", "PGUSER", "DATABASE_URL"):
        assert table[name] == f"`{env[name]}`", (name, table[name])
    assert "the first database in `databases`" in table["PGDATABASE"]
    assert f"postgres: service not started: {ts.NO_CACHE_VOLUME}" in body


def test_test_services_page_states_what_the_code_holds():
    _check_ts_doc(_text(TS_DOC))


def _ts_agent_output_part(text: str) -> str:
    """The test-services paragraph of agent-output.md's pre-publish section."""
    section = _section(text, "## Before the worker publishes: a rebase and the repository's own checks (SPEED-D1)")
    start = section.index(TS_AGENT_OUTPUT)
    return section[start: section.index("\n\n", start)]


def _check_ts_agent_output(text: str) -> None:
    from agent_worker import prepublish, testservices as ts

    raw = _ts_agent_output_part(text)
    body = _flat(raw)
    assert "(#1605, #1627)" in body
    assert "[test-services.md](test-services.md)" in raw
    assert "[design/test-services.md](design/test-services.md) §11" in raw
    # The skipped text, as `_pytest_project` builds it.
    source = _cited_symbol(TS_PREPUBLISH_PY, "_pytest_project")
    assert '"every selected test errored at setup connecting to it; CI runs it"' in source
    assert ("a service the tests need is unreachable (127.0.0.1:5432); every selected test "
            "errored at setup connecting to it; CI runs it") in body
    assert f'"and not declared in {ts.DECLARATION_PATH}"' in body
    env = list(prepublish.service_env([ts.DeclaredService("postgres", 16, ("x",))]))
    assert ", ".join(f"`{k}`" for k in env[:-1]) + f" and `{env[-1]}`" in body, env
    assert f"`{ts.DECLARATION_PATH}`" in body and f"`{prepublish.TEST_SERVICES_CHECK}` check" in body


def test_agent_output_describes_test_services_from_the_code():
    _check_ts_agent_output(_text(AGENT_OUTPUT))


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


# The proof each check above can fail: a one-line edit of the real text,
# handed to the same check, must be refused. The docs are never edited.
_TS_MUTATIONS = [
    # check, the doc it reads, the one line to change, what to change it to
    ("lanes", TS_DESIGN, "### 11.3 What TS-RUN built", "### 11.3 What TS-RUN started"),
    ("lanes", TS_DESIGN, "* **TS-SIGNAL (not built).** Nothing counts skipped addresses by port yet;",
     "* **TS-SIGNAL.** Nothing counts skipped addresses by port yet;"),
    ("lanes", TS_DESIGN, "and where the code departs from this design; TS-SIGNAL is not built.**",
     "and where the code departs from this design.**"),
    ("exists", TS_DESIGN, "`tests/unit/worker/test_testservices_declaration.py`.",
     "`tests/unit/worker/test_testservices_declarations.py`."),
    ("exists", TS_DESIGN, "`apps/agent-worker/agent_worker/prepublish.py::declared_services`): one a",
     "`apps/agent-worker/agent_worker/prepublish.py::declare_services`): one a"),
    ("exists", TS_DESIGN, "refused addresses `unreachable_services` returns against the ports of the",
     "refused addresses `unreachable_addresses` returns against the ports of the"),
    ("exists", TS_DESIGN, "`tests/unit/scripts/test_agent_toolbox_image.py`.",
     "`tests/unit/scripts/test_agent_image.py`."),
    ("code", TS_DESIGN, "5. readiness by `pg_isready` on the socket every 0.1 s, up to",
     "5. readiness by `pg_isready` on the socket every 0.5 s, up to"),
    ("code", TS_DESIGN, "`apps/agent-worker/agent_worker/testservices.py::OOM_SCORE_ADJ` (1000) on the",
     "`apps/agent-worker/agent_worker/testservices.py::OOM_SCORE_ADJ` (500) on the"),
    ("code", TS_DESIGN, "  `PGPORT`, `PGUSER`, `PGDATABASE`, `DATABASE_URL` and `SWARM_TEST_SERVICES`.",
     "  `PGPORT`, `PGUSER`, `PGDATABASE` and `DATABASE_URL`."),
    ("page", TS_DOC, "| `databases` | a list of 1 to 8 names, each matching `[a-z_][a-z0-9_]{0,62}`, no duplicates;"
     " each is created empty, owned by `postgres`. The first is the default database |",
     "| `databases` | a list of 1 to 16 names, each matching `[a-z_][a-z0-9_]{0,62}`, no duplicates;"
     " each is created empty, owned by `postgres`. The first is the default database |"),
    ("page", TS_DOC, "| `user` | the platform owns the service's user |", ""),
    ("page", TS_DOC, "postgres.version: 15 is not offered: version is an integer, one of 16",
     "postgres.version: 15 is not offered"),
    ("page", TS_DOC, "  memory (`shared_buffers=64MB`, `max_connections=50`) and no durability",
     "  memory (`shared_buffers=128MB`, `max_connections=50`) and no durability"),
    ("page", TS_DOC, "| `PGPORT` | `5432` |", "| `PGPORT` | `5433` |"),
    ("agent-output", AGENT_OUTPUT, "   .swarm/test-services.toml\"; and a server that stopped during the run makes",
     "   .swarm/test-services.yml\"; and a server that stopped during the run makes"),
    ("agent-output", AGENT_OUTPUT, "   `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `DATABASE_URL` and",
     "   `PGHOST`, `PGPORT`, `PGUSER`, `DATABASE_URL` and"),
    ("cites", TS_DESIGN, "(`apps/agent-worker/agent_worker/testservices.py::child_env`): no worker",
     "(`apps/agent-worker/agent_worker/testservices.py::child_environ`): no worker"),
    ("cites", TS_DESIGN, "(`images/agent-runtime-base/Dockerfile` (`ARG POSTGRES_MAJOR=16`)):",
     "(`images/agent-runtime-base/Dockerfile` (`ARG POSTGRES_MAJOR=17`)):"),
    ("cites", TS_DOC, "(`apps/scheduler/scheduler/dispatch.py::CACHE_MOUNT`); every profile that runs",
     "(`apps/scheduler/scheduler/dispatch.py::CACHE_MOUNTS`); every profile that runs"),
    ("cites", AGENT_OUTPUT, "   (`apps/agent-worker/agent_worker/prepublish.py::unreachable_services`). A",
     "   (`apps/agent-worker/agent_worker/prepublish.py::unreachable_service`). A"),
]


def _ts_run_check(check: str, doc: Path, text: str, tmp_path: Path) -> None:
    if check == "cites":
        name = next(k for k, (path, _) in _TS_CITED.items() if path == doc)
        _ts_assert_cites_resolve(_TS_CITED[name][1](text), tmp_path / name)
        return
    {"lanes": _check_ts_lanes, "exists": _check_ts_entries_name_what_exists,
     "code": _check_ts_entries_state_the_code, "page": _check_ts_doc,
     "agent-output": _check_ts_agent_output}[check](text)


@pytest.mark.parametrize("check,doc,old,new", _TS_MUTATIONS,
                         ids=[f"{m[0]}-{i}" for i, m in enumerate(_TS_MUTATIONS)])
def test_test_services_checks_fail_on_a_one_line_mutation(check, doc, old, new, tmp_path: Path):
    text = _text(doc)
    assert "\n" not in old and text.count(old) == 1, f"the mutation's line is not once in {doc.name}: {old!r}"
    # The unmutated text passes, so the failure below is the mutation's.
    _ts_run_check(check, doc, text, tmp_path)
    with pytest.raises(AssertionError):
        _ts_run_check(check, doc, text.replace(old, new), tmp_path)


# --------------------------------------------------------------------------
# SSC-DOCS (part of #2158): a secret is the workspace's, a person's or a
# repository's, and GitHub Actions is a write-only destination
# --------------------------------------------------------------------------
#
# docs/design/secrets-scopes.md was written before any lane built it, and its
# §11 (BGD's answers of 2026-10-23) overrode §3 on the App, the environments
# and the seal. Its status line and §9 say which lanes are built, with their
# PRs; §12 records what each built and where the code departs from §2-§6.
# workspace-secrets.md's status line points at the extension, security.md
# carries the threat model and the accepted residual risk (Q3), and
# provider-keys.md §7 what the scopes change for its keys. Every permission,
# switch, limit and binding they state is read from the code here, so a
# switch turned on, a permission widened or step 5 shipping fails until the
# documents follow.

SSC_DESIGN = DOCS / "design" / "secrets-scopes.md"
PROVIDER_KEYS = DOCS / "provider-keys.md"
SSC_BUILT = "## 12. What was built (2026-10-24, lane SSC-DOCS, part of #2158)"
SSC_SECURITY = "## Secrets scopes and GitHub Actions secrets"
SSC_KEYS = "## 7. Secrets scopes: what changes for these keys, and what does not (#2158)"
#: §9's lanes that are not built, each for a reason the status line gives:
#: phase 0 because Q3 declined the second App.
SSC_UNBUILT = ("SSC-SECRETS-APP",)
#: Lanes marked built by their own lane, before their PR had a number: §9
#: says "(built, lane <name>" and §12 has a "**<name> (lane <name>" entry.
SSC_BUILT_BY_LANE = ("SSC-UI",)
#: The lane that mounted SSC-UI-ACTIONS' pane on Config › Secrets (part of #2158).
SSC_MOUNT = "SECRETS-ACTIONS-MOUNT"
#: Lanes the build added, each answered on #2158 from §2.1's rule.
SSC_ADDED = {"SSC-REPO-DELETE": 2239, "SSC-REPO-DELETE-TXN": 2245, "SSC-UNREGISTER-TXN": 2249,
             "LINK-ADMIN-WRITES": 2229, "PLATFORM-SECRETS-500": 2221}
#: The new refusals the lanes shipped off (refusals.SWITCHES).
SSC_SWITCHES = ("github_admin_required", "grant_key_required", "qa_bypass_link_required")
SSC_ACTIONS_PY = "apps/swarm-api/swarm_api/forge_installation.py"
SSC_STEP5 = 'resource "google_project_iam_member" "forge_slot_reader" {'


def _ssc_head(text: str) -> str:
    return _flat(text.split("\n---\n", 1)[0])


def _ssc_plan(text: str) -> dict[str, str]:
    """§9's rows: lane -> the text of its lane cell after the name."""
    plan = _section(text, "## 9. Build plan")
    rows = dict(re.findall(r"^\|[^|\n]*\| \*\*(SSC-[A-Z-]+)\*\*([^|\n]*)\|", plan, flags=re.M))
    assert len(rows) >= 11 and "SSC-DOCS" in rows, rows
    return rows


def _check_ssc_status(text: str) -> None:
    head = _ssc_head(text)
    assert "Nothing in this document is built" not in head
    assert "**Status: built 2026-10-24" in head and "§12" in head
    assert "§11 overrides this document" in head
    for lane in SSC_UNBUILT:
        assert lane in head, f"the status line does not say {lane} is not built"
    # Q3: no second App, so neither its key nor its setting exists.
    assert "no `swarm-github-secrets-app-private-key`" in head
    assert not (REPO / "terraform" / "infra" / "github_secrets_app.tf").exists()
    from swarm_api import settings

    assert "GITHUB_SECRETS_APP_ID" not in _text(Path(settings.__file__))


def _check_ssc_lanes(text: str) -> None:
    rows = _ssc_plan(text)
    built = _section(text, SSC_BUILT)
    for lane, cell in rows.items():
        if lane in SSC_UNBUILT:
            assert cell.strip().startswith("(not built"), f"§9 does not mark {lane} not built"
            continue
        if lane == "SSC-DOCS":
            assert cell.strip().startswith("(built"), cell
            continue
        if lane in SSC_BUILT_BY_LANE:
            assert cell.strip().startswith(f"(built, lane {lane}"), f"§9 does not mark {lane} built: {cell!r}"
            assert f"**{lane} (lane {lane}" in built, f"§12 has no entry for {lane}"
            continue
        pr = re.match(r"\s*\(built, PR (\d+)", cell)
        assert pr, f"§9 does not mark {lane} built with its PR: {cell!r}"
        assert f"**{lane} (PR {pr.group(1)}" in built, f"§12 has no entry for {lane} with PR {pr.group(1)}"
    for lane, pr in SSC_ADDED.items():
        assert f"**{lane} (PR {pr})" in built or f"PR {pr} ({lane})" in built, lane
    # SECRETS-ACTIONS-MOUNT mounted SSC-UI-ACTIONS' pane on Config › Secrets:
    # the status, §9 and §12 say so, and nothing says it is still unmounted.
    assert "./SecretsActions" in _text(REPO / "apps" / "swarm-ui" / "src" / "Secrets.tsx"), \
        "Secrets.tsx no longer mounts SSC-UI-ACTIONS' pane: the documents say it does"
    flat = _flat(text)
    for stale in ("does not mount", "not mounted", "not yet mounted", "not written yet", "does not offer that form yet"):
        assert stale not in flat, f"secrets-scopes.md still says the pane is unmounted: {stale!r}"
    assert f"**{SSC_MOUNT} (lane {SSC_MOUNT}" in built, f"§12 has no entry for {SSC_MOUNT}"
    assert SSC_MOUNT in rows["SSC-UI-ACTIONS"], f"§9's SSC-UI-ACTIONS row does not name {SSC_MOUNT}"
    assert SSC_MOUNT in _ssc_head(text), f"the status line does not name {SSC_MOUNT}"


def _check_ssc_code(text: str) -> None:
    from swarm_api import refusals, workspace_secrets
    from swarm_api.forge_installation import ACTIONS_PERMISSIONS
    from swarm_api.grant_attest import FORM_REPOSITORY

    raw = _section(text, SSC_BUILT)
    body = _flat(raw)
    # Q3/Q4: one permission per mint, the ones §12 names, never administration.
    assert f"`{SSC_ACTIONS_PY}::ACTIONS_PERMISSIONS`" in raw
    granted = {(k, v) for perm in ACTIONS_PERMISSIONS.values() for k, v in perm.items()}
    assert all(len(perm) == 1 for perm in ACTIONS_PERMISSIONS.values())
    assert not any(k == "administration" for k, _ in granted) and "Never `administration`" in body
    for name, level in granted:
        assert f"`{name}: {level}`" in body or "read levels for the name lists" in body, (name, level)
    assert ("secrets", "write") in granted and ("environments", "write") in granted
    assert "`secrets: write`" in body and "`environments: write`" in body and "`actions: read`" in body
    # Q5: sealed in swarm-api, with PyNaCl, which swarm-api depends on.
    assert "pynacl" in _text(REPO / "apps" / "swarm-api" / "pyproject.toml").lower()
    assert "SealedBox" in _cited_symbol("apps/swarm-api/swarm_api/actions_secrets.py", "seal")
    assert FORM_REPOSITORY == "v2" and 'form `"v2"`' in body
    # One transaction, refused whole above the limit.
    assert workspace_secrets.MAX_TRANSACTION_WRITES == 500 and "about 33" in body
    for code in SSC_SWITCHES:
        assert refusals.SWITCHES[code].default is False, code
        assert f"| `{code}` |" in raw, f"§12 does not say {code} ships off"
    # Platform secrets: off until BGD's bootstrap apply.
    tf = _text(REPO / "terraform" / "bootstrap" / "platform_secret_status.tf")
    assert re.search(r'variable "enable_platform_secret_status" \{.*?default\s*=\s*false', tf, flags=re.S)
    if "enable_platform_secret_status" not in _text(REPO / "terraform" / "bootstrap" / "terraform.tfvars"):
        assert "**It is off:**" in body
    # Q3's residual risk is open exactly while workers can read the slots.
    if SSC_STEP5 in _text(REPO / "terraform" / "bootstrap" / "forge_user_slots.tf"):
        assert "**The residual risk BGD accepted (Q3) is live.**" in body
        assert f"`terraform/bootstrap/forge_user_slots.tf`\n(`{SSC_STEP5}`)" in raw
    assert "**The done check (§9) is not met yet.**" in body


def _check_ssc_workspace_secrets(text: str) -> None:
    head = _ssc_head(text)
    assert "The release (SEC-RELEASE), the console (SEC-UI) and the migrations are not built" not in head
    assert "turns on when SEC-RELEASE merges" not in head
    assert "[secrets-scopes.md](secrets-scopes.md)" in head and "**repository**" in head
    assert "SEC-UI-LIST" in head and "SEC-RELEASE" in head
    tfvars = _text(REPO / "terraform" / "bootstrap" / "terraform.tfvars")
    assert "enable_workspace_secrets = true" in tfvars and "`enable_workspace_secrets = true`" in head


def _check_ssc_security(text: str) -> None:
    raw = _section(text, SSC_SECURITY)
    body = _flat(raw)
    assert "design/secrets-scopes.md" in body
    assert "`apps/swarm-api/swarm_api/workspace_secrets.py::attempt_repositories`" in raw
    assert "`apps/swarm-api/swarm_api/grant_attest.py::FORM_REPOSITORY`" in raw
    assert f"`{SSC_ACTIONS_PY}::ACTIONS_PERMISSIONS`" in raw and "No mint asks `administration`" in body
    assert "**Accepted residual risk (BGD, Q3" in body and "step 5" in body
    assert "`MERGE_APP_PRIVATE_KEY`" in body and "cannot read one" in body
    if SSC_STEP5 in _text(REPO / "terraform" / "bootstrap" / "forge_user_slots.tf"):
        assert "tenant workers can still read" in body
    assert "keeps no value, no ciphertext and no token" in body
    assert "`secretmanager.secrets.get` and `secretmanager.versions.list`" in body
    # Credential theft still counts two payload-serving routes.
    theft = _flat(_section(text, "## Credential theft"))
    assert "Exactly two routes serve credential material" in theft
    assert "take a value in and serve none, so they are not a third" in theft


def _check_ssc_provider_keys(text: str) -> None:
    assert "**A workspace secrets store is proposed, not built**" not in text
    head = _flat(text.split("## 1. Routes", 1)[0])
    assert "`apps/swarm-api/swarm_api/secrets_overview.py::_provider_key_rows`" in head
    assert "lane SSC-UI, built" in head
    body = _flat(_section(text, SSC_KEYS))
    assert "the person's wins, then the repository's, then the workspace's" in body
    assert "PyNaCl" in body and "security.md#secrets-scopes-and-github-actions-secrets" in body


_SSC_CHECKS = {
    "status": (SSC_DESIGN, _check_ssc_status), "lanes": (SSC_DESIGN, _check_ssc_lanes),
    "code": (SSC_DESIGN, _check_ssc_code), "workspace-secrets": (SECRETS_DESIGN, _check_ssc_workspace_secrets),
    "security": (SECURITY, _check_ssc_security), "provider-keys": (PROVIDER_KEYS, _check_ssc_provider_keys),
}


@pytest.mark.parametrize("check", sorted(_SSC_CHECKS))
def test_secrets_scopes_docs_describe_what_was_built(check: str):
    doc, run = _SSC_CHECKS[check]
    run(_text(doc))


@pytest.mark.parametrize("doc", (SSC_DESIGN, SECRETS_DESIGN, SECURITY, PROVIDER_KEYS), ids=lambda p: p.name)
def test_secrets_scopes_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)


#: One-line mutations, each a way the documents could drift: each must fail
#: its check, so the checks above are shown to catch what they claim to.
_SSC_MUTATIONS = [
    ("lanes", "| 1 | **SSC-OVERVIEW** (built, PR 2248) |", "| 1 | **SSC-OVERVIEW** |"),
    ("lanes", "**SSC-OVERVIEW (PR 2248).** §4 and §6.2: `GET /v1/secrets/overview` reads",
     "**SSC-OVERVIEW.** §4 and §6.2: `GET /v1/secrets/overview` reads"),
    ("status", "**Status: built 2026-10-24 (lane SSC-DOCS, part of #2158), except phase 0.** Designed 2026-10-23",
     "**Status: built 2026-10-24 (lane SSC-DOCS, part of #2158), except phase 0.** Nothing in this document is built. 2026-10-23"),
    ("lanes", "| 3 | **SSC-UI-ACTIONS** (built, PR 2300; mounted on Config › Secrets by lane SECRETS-ACTIONS-MOUNT, §12) |",
     "| 3 | **SSC-UI-ACTIONS** (built, PR 2300; `Secrets.tsx` does not mount it yet, §12) |"),
    ("code", "| `grant_key_required` | a repository grant is written unattested, and the release drops it, "
     "because a repository grant is released only when attested (§2.2) | every environment holds the grant key |",
     ""),
    ("code", "**The residual risk BGD accepted (Q3) is live.** User-scoped-secrets step 5",
     "**The residual risk BGD accepted (Q3) is closed.** User-scoped-secrets step 5"),
]


@pytest.mark.parametrize("check,old,new", _SSC_MUTATIONS, ids=[f"{m[0]}-{i}" for i, m in enumerate(_SSC_MUTATIONS)])
def test_secrets_scopes_checks_fail_on_a_one_line_mutation(check: str, old: str, new: str):
    doc, run = _SSC_CHECKS[check]
    text = _text(doc)
    assert "\n" not in old and text.count(old) == 1, f"the mutation's line is not once in {doc.name}: {old!r}"
    run(text)
    with pytest.raises(AssertionError):
        run(text.replace(old, new))


# -- QAENV-DOCS: one visual-QA dispatch over an app's environments (#2136) ----
#
# docs/design/qa-environments.md §8's phase 4, after QAENV-MODEL, -GATE,
# -CRAWL, -RUN, -SETTLE, -UI-RUNPAGE, -UI-FORM and -SC merged. The design's
# status said "DESIGN, not built", visual-qa-any-app.md still refused a
# discovering target with nothing stored (`qa_target_no_screens`, "press Run
# discovery"), and schedules.md §3.17 still gave the `visual-qa` caps as $80 a
# day and one at a time, which BGD changed on 2026-10-23 (§10, Q9 (a)). Each
# figure below is read from the code that holds it.

QAENV_DESIGN = DOCS / "design" / "qa-environments.md"
QAENV_ANY_APP = DOCS / "design" / "visual-qa-any-app.md"
SCHEDULES = DOCS / "schedules.md"
QAENV_BUILT = "## 11. What was built"
QAENV_ANY_APP_BUILT = "## 10. Environments, the run group and discovery during QA (built, #2136)"
QAENV_SCHEDULE = "### 3.17 `visual-qa`"
QAENV_QASPEC = "apps/swarm-api/swarm_api/qaspec.py"
QAENV_TARGETS = "apps/swarm-api/swarm_api/qatargets.py"
QAENV_VISUAL_QA = "apps/swarm-api/swarm_api/schedtypes/visual_qa.py"
QAENV_SWARM_QA = "images/agent-runtime-browser/swarm_qa.py"

#: Every merged lane of §8, with its pull request (read on GitHub 2026-10-25).
QAENV_PRS = {
    "QAENV-GATE": 2161, "QAENV-CRAWL": 2162, "QAENV-MODEL": 2163, "QAENV-RUN": 2175,
    "QAENV-SC": 2177, "QAENV-SETTLE": 2181, "QAENV-UI-RUNPAGE": 2261, "QAENV-UI-FORM": 2304,
}


def _qaenv_int(path: str, name: str) -> int:
    """The integer a module-level `NAME = <int>` assigns, read from its source."""
    match = re.search(rf"^{name} = (\d+)$", _cited_symbol(path, name), flags=re.M)
    assert match, f"{path} assigns no integer {name}"
    return int(match.group(1))


def _qaenv_caps() -> tuple[int, int, int]:
    source = _cited_symbol(QAENV_VISUAL_QA, "BUDGET_CAPS")
    match = re.search(r"per_run_usd=(\d+), per_day_usd=(\d+), max_concurrent=(\d+)", source)
    assert match, "BUDGET_CAPS is not in the shape this test reads"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _check_qaenv_status(text: str) -> None:
    head = _flat(text.split("\n---\n", 1)[0])
    assert "DESIGN, not built" not in head and "Nothing here changes behaviour" not in head
    assert "**Status: BUILT.**" in head and "§11" in head
    for lane, number in QAENV_PRS.items():
        assert f"{lane} (PR {number})" in head, f"the status names no PR for {lane}"
    assert "QA-ENVIRONMENTS-DESIGN (PR 2144)" in head


def _check_qaenv_built(text: str) -> None:
    raw = _section(text, QAENV_BUILT)
    body = _flat(raw)
    for lane, number in QAENV_PRS.items():
        assert f"**{lane} (PR {number}).**" in raw, f"§11 has no entry for {lane}"
    # Where the build differs from the design's text, said where a reader looks.
    assert f"`{QAENV_QASPEC}::_screens`" in raw
    assert 'raw = target.get("screens")' in _cited_symbol(QAENV_QASPEC, "_screens")
    assert "uses `screens` alone when a target sets it" in body
    assert "`write_attempting` is a list of `{path, text}`" in body
    assert "a missing name is a value, not a wildcard" in body
    assert "docs/qa/any-app-qa.md" in body and "questions.json" in body
    # §5's sentence that promised more than was built carries its correction.
    five = _flat(_section(text, "## 5. Where found routes go"))
    assert "only on a leg without `screens` (§11)" in five


def _check_qaenv_any_app(text: str) -> None:
    raw = _section(text, QAENV_ANY_APP_BUILT)
    body = _flat(raw)
    assert body.count("[qa-environments.md](qa-environments.md)") >= 7
    paragraphs = {
        "**Environments.**": ("`environments[]`", f"at most {_qaenv_int(QAENV_QASPEC, 'MAX_ENVIRONMENTS')}",
                              '`values["<target_id>/<name>"]`', "`POST /v1/qa-targets:migrate`"),
        "**The run group.**": ("`qa_run_groups/{group_id}`", "`GET /v1/qa-runs/{group_id}`", "207",
                               "/qa-runs/<group_id>"),
        "**Capture steps crawl.**": (f"`found_per_step` defaults to {_qaenv_int(QAENV_QASPEC, 'FOUND_PER_STEP')}",
                                     f"`discover.slots` (default {_qaenv_int(QAENV_QASPEC, 'DEFAULT_SLOTS')}",
                                     f"{_qaenv_int(QAENV_QASPEC, 'CRAWL_CAPTURE_TIMEOUT'):,} s", "`found.json`"),
        "**Disclosure clicks.**": ("`aria-expanded=\"false\"`", "inside a `<form>`", "`write_attempting`",
                                   f"{_qaenv_int(QAENV_SWARM_QA, 'MAX_CLICKS_PAGE')} clicks a page",
                                   f"{_qaenv_int(QAENV_SWARM_QA, 'MAX_CLICKS_CRAWL')} a crawl"),
        "**Per-leg `discovered`, and Promote.**": ("**that leg's** `discovered`", "`first_seen`",
                                                    f"{_qaenv_int(QAENV_TARGETS, 'MISSING_DROP_AFTER')} complete crawls",
                                                    "`new_since_last_run`", "**Promote**"),
        "**Filing per environment.**": ("`visual-qa:<target_id>:<environment>:<screen>:<title>`", "`[<leg>]`"),
        "**The shared-origin rule.**": (f"`{QAENV_QASPEC}::shares_production_origin`", "`target_changed`",
                                        "`(target_id, environment_name)`"),
    }
    for lead, phrases in paragraphs.items():
        assert lead in body, f"§10 has no paragraph {lead}"
        # The paragraph the lead opens: up to its blank line.
        para = _flat(raw.split(lead, 1)[1].split("\n\n", 1)[0])
        assert "qa-environments.md" in para, f"{lead} does not point at the design"
        for phrase in phrases:
            assert phrase in para, f"{lead} does not say {phrase!r}"
    assert "Nothing of §3 is softened" in body
    # §4.1 no longer refuses a discovering target with nothing stored.
    steps = _flat(_section(text, "### 4.1 The steps"))
    assert "press Run discovery on the QA targets page" not in steps
    assert "renders `discover.slots` crawling capture steps" in steps
    # §5's dedupe key and §6's triggers follow the per-leg build.
    assert "`dedupe_key: \"visual-qa:<target_id>:<environment>:<screen>:<normalised title>\"`" in _flat(
        _section(text, "## 5. Filing"))
    triggers = _flat(_section(text, "## 6. Triggers"))
    assert "`environments`" in triggers and "one work item per leg" in triggers


def _check_qaenv_safety(text: str) -> None:
    """§3, the owner's safety rules, keep every sentence the build relied on."""
    three = _flat(_section(text, "## 3. Safety on someone else's application"))
    for sentence in ("Read-only by default.", "Nothing lifts the second.",
                     "its answer carries `mutating: true` only when the stored target is `non_production`",
                     "**any** request but `GET` and `HEAD` to every other origin",
                     "(default 2 s, floor 1 s)"):
        assert sentence in three, f"§3 lost {sentence!r}"


def _check_qaenv_schedule(text: str) -> None:
    body = _flat(_section(text, QAENV_SCHEDULE))
    per_run, per_day, concurrent = _qaenv_caps()
    assert (per_run, per_day, concurrent) == (80, 160, 2)
    assert f"`per_run_usd` {per_run} a leg" in body
    # BGD raised the leg from $40 to $80 on 2026-10-26 (QA-TRIPWIRE-MAC), from
    # the bench median of $0.14 a flow run: 132 flows at 2 widths are about $37.
    assert "BGD raised it to $80 on 2026-10-26" in body and "$0.14" in body
    assert f"`per_day_usd` {per_day}" in body and f"`max_concurrent` {concurrent}" in body
    assert "BGD on 2026-10-23" in body and "Q9 (a)" in body and "VQ7-CAPS" in body
    assert "`max_concurrent` 1, set by BGD" not in body
    assert "`environments`" in body and "one work item per leg" in body
    assert f"`{_qaenv_refusal()}`" in body


def _qaenv_refusal() -> str:
    match = re.search(r'^ENVIRONMENT_NOT_FOUND = "([a-z_]+)"$',
                      _cited_symbol(QAENV_VISUAL_QA, "ENVIRONMENT_NOT_FOUND"), flags=re.M)
    assert match
    return match.group(1)


_QAENV_CHECKS = {
    "status": (QAENV_DESIGN, _check_qaenv_status), "built": (QAENV_DESIGN, _check_qaenv_built),
    "any-app": (QAENV_ANY_APP, _check_qaenv_any_app), "safety": (QAENV_ANY_APP, _check_qaenv_safety),
    "schedule": (SCHEDULES, _check_qaenv_schedule),
}


@pytest.mark.parametrize("check", sorted(_QAENV_CHECKS))
def test_qa_environments_docs_describe_what_was_built(check: str):
    doc, run = _QAENV_CHECKS[check]
    run(_text(doc))


@pytest.mark.parametrize("doc", (QAENV_DESIGN, QAENV_ANY_APP), ids=lambda p: p.name)
def test_qa_environments_docs_cite_only_what_resolves(doc: Path):
    _assert_cites_resolve(doc)
