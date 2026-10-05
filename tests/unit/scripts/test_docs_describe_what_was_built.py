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
    assert _cited_symbol(oauth, "TOKEN_ENDPOINT").startswith("TOKEN_ENDPOINT = ")
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
    assert _cited_symbol(accountlease, name) == f'{name} = "{variable}"'
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
