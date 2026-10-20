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
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, RunnerProfile

from .test_docs_spec_amendments import _assert_cites_resolve, _cited_anchor, _cited_symbol

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
        ("Task timeline", "GET /v1/tasks/{id}/attempts", ("apps/swarm-api/swarm_api/routes/tasks.py", "list_attempts"), "/attempts"),
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
    assert "await_input_expired" in body and '"await_input_expired: ' in source
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
    assert signing_format({"model_class": "haiku"}) == 5 == SPEC_FORMAT
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
    assert sorted(every) == ["claude-code", "claude-code-browser", "indexer"], every
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
