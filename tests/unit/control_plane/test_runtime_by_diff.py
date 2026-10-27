"""A review's runtime is chosen from its parents' diff as it is readied (#2322).

BGD's decision, 2026-10-25: only a review that needs to look at UI gets a
browser runtime. A workflow step may say `metadata.runtime = {"policy":
"browser_if_ui", "browser": "claude-code-browser", "code": "claude-code"}`;
swarm-api stores it on the browser and signs the `claude-code` spec beside it
(`metadata.runtime.alternate`), and the scheduler, as it promotes the step to
READY, swaps to `claude-code` -- fields and signature together -- unless a
parent changed a UI path. Test files are not UI, for the scheduler and for the
worker's gate alike. What is held here:

    a parent that changed a .tsx file resolves the review to the browser
    a parent that changed only scheduler code resolves it to claude-code,
        and the swapped document still verifies as swarm-api signed it
    an unreadable or a cut file list resolves to the browser
    a test-only UI change is not UI, for the resolver and the worker's gate
    the re-review inherits its review's RESOLVED profile
    a review pinned to claude-code gets a browser_if_ui re-review (#2275),
        signed under both profiles and resolved from the lane's own files
    the resolution is recorded in the READY event and on the task
    the step is admitted on the resolved profile's pools, never the other's
    swarm-api refuses any pair but the one, and any shape but the block
    the issue-run planner's review asks for the policy
    the restated rule and constants match the worker's and swarm-api's
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_worker import verdict as verdict_mod
from scheduler import runtime as runtime_mod
from swarm_api import validation
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_pool, seed_task, seed_tenant
from .spec_signer import LocalSpecSigner

BROWSER = "claude-code-browser"
CODE = "claude-code"
POLICY = {"policy": "browser_if_ui", "browser": BROWSER, "code": CODE}

UI_FILE = "apps/swarm-ui/src/pages/Agents.tsx"
SCHEDULER_FILE = "apps/scheduler/scheduler/loop.py"
UI_TEST_FILES = ("apps/swarm-ui/src/__tests__/Agents.test.tsx", "apps/swarm-ui/tests/e2e/a.ts")


def _git(*paths: str, truncated: bool = False) -> dict[str, Any]:
    return {"git": {"files": [{"path": p, "status": "M"} for p in paths],
                    "files_truncated": truncated}}


def _alternate(**overrides: Any) -> dict[str, Any]:
    return {
        "runner_profile": CODE, "resource_class": "standard", "timeout_seconds": 3600,
        "provider": "anthropic", "model": None, "model_class": None,
        "spec_signature": "c2lnbmVkLWNvZGU=", "spec_key_version": "kv/1", "spec_format": 1,
        **overrides,
    }


def _parent(db, task_id: str, summary: dict[str, Any] | None, *, tenant_id: str = "eng",
            builds_on: str | None = None) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED",
                    runner_profile=CODE)
    doc["result_summary"] = summary
    if builds_on:
        doc["metadata"] = {"dispatch": {"builds_on": builds_on}}


def _review(db, task_id: str = "task_review", *, depends_on=("task_impl",),
            policy: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="PARKED",
                    park_reason="DEPENDENCY_INCOMPLETE", runner_profile=BROWSER,
                    resource_class="browser", provider="anthropic", depends_on=depends_on)
    doc["spec_signature"] = "c2lnbmVkLWJyb3dzZXI="
    doc["spec_key_version"] = "kv/1"
    doc["spec_format"] = 1
    doc["metadata"] = {"runtime": {**(policy or POLICY), "alternate": _alternate()},
                       "keep": "me"}
    return doc


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10)
    seed_tenant(db, "research", max_active=10)
    seed_pool(db, "global", hard_limit=10)


def _events(db, task_id: str) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [doc for key, doc in db.docs.items() if key.startswith(prefix)]


def _ready_event(db, task_id: str) -> dict[str, Any]:
    (event,) = [e for e in _events(db, task_id) if e["type"] == "ready"]
    return event


# ---------------------------------------------------------------------------
# The resolution, at READY
# ---------------------------------------------------------------------------


def test_a_review_whose_parent_changed_a_tsx_file_resolves_to_the_browser(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE, UI_FILE))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    # Promoted (admission may then park it on a credential: this tenant has none).
    assert _ready_event(db, "task_review")["detail"]["runtime"]["profile"] == BROWSER
    assert doc["runner_profile"] == BROWSER
    assert doc["resource_class"] == "browser"
    assert doc["spec_signature"] == "c2lnbmVkLWJyb3dzZXI="
    resolved = doc["metadata"]["runtime"]["resolved"]
    assert resolved["profile"] == BROWSER and resolved["reason"] == "ui_paths"
    assert resolved["ui_paths"] == [UI_FILE]


def test_a_review_whose_parent_changed_only_scheduler_code_resolves_to_claude_code(
    db, make_scheduler
):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    # MUTATION: skip the swap and this stays claude-code-browser.
    assert doc["runner_profile"] == CODE
    assert doc["resource_class"] == "standard"
    assert doc["timeout_seconds"] == 3600
    # The signature swarm-api made for the claude-code spec, swapped in with it.
    assert doc["spec_signature"] == "c2lnbmVkLWNvZGU="
    # And the browser's fields are kept as the alternate: the pair stays whole.
    alternate = doc["metadata"]["runtime"]["alternate"]
    assert alternate["runner_profile"] == BROWSER
    assert alternate["resource_class"] == "browser"
    assert alternate["spec_signature"] == "c2lnbmVkLWJyb3dzZXI="
    # A metadata key the promotion did not write is still there.
    assert doc["metadata"]["keep"] == "me"


@pytest.mark.parametrize("summary", [
    None,
    {},
    {"git": {"error": "could not harvest"}},
    {"git": {"files": "not a list"}},
])
def test_an_unreadable_file_list_resolves_to_the_browser(db, make_scheduler, summary):
    _base(db)
    _parent(db, "task_impl", summary)
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    # MUTATION: read a missing list as "no UI" and this is claude-code.
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "file_list_unreadable"


def test_a_cut_file_list_with_no_ui_path_resolves_to_the_browser(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE, truncated=True))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "file_list_cut"


def test_another_tenants_parent_is_unreadable_and_resolves_to_the_browser():
    documents = {"task_other": {"tenant_id": "research", **_git(SCHEDULER_FILE)}}

    def read(task_id: str) -> dict[str, Any] | None:
        doc = documents.get(task_id)
        return doc if doc is not None and doc["tenant_id"] == "eng" else None

    resolution = runtime_mod.resolve(["task_other"], POLICY, read)
    assert resolution.profile == BROWSER
    assert resolution.unreadable == ("task_other",)


def test_a_chain_is_read_back_along_builds_on(db, make_scheduler):
    """A chain's last step lists only its own diff: the UI change an earlier step
    made is found along `builds_on`."""
    _base(db)
    _parent(db, "task_impl1", _git(UI_FILE))
    _parent(db, "task_impl", _git(SCHEDULER_FILE), builds_on="task_impl1")
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    assert doc["runner_profile"] == BROWSER
    assert doc["metadata"]["runtime"]["resolved"]["parents_read"] == ["task_impl", "task_impl1"]


def test_a_test_only_ui_change_is_not_ui_for_the_resolver(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE, *UI_TEST_FILES))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    # MUTATION: count `__tests__/` or `tests/` as UI and this is the browser.
    assert db.docs["tasks/task_review"]["runner_profile"] == CODE


def test_a_test_only_ui_change_is_not_ui_for_the_gate():
    patch = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n" for p in UI_TEST_FILES)
    assert verdict_mod.ui_paths_in_patch(patch) == []
    assert verdict_mod.ui_paths_in_files(_git(*UI_TEST_FILES)) == ([], False)


def test_the_resolution_is_recorded_as_a_ready_event(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    event = _ready_event(db, "task_review")
    runtime = event["detail"]["runtime"]
    assert runtime["policy"] == "browser_if_ui"
    assert runtime["profile"] == CODE
    assert runtime["reason"] == "no_ui_paths"
    assert runtime["swapped"] is True
    assert runtime["parents_read"] == ["task_impl"]


def test_a_step_with_no_policy_is_promoted_exactly_as_before(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE))
    doc = _review(db)
    doc["metadata"] = {}

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    assert doc["runner_profile"] == BROWSER
    assert "runtime" not in doc["metadata"]
    assert "runtime" not in _ready_event(db, "task_review")["detail"]


def test_a_policy_naming_another_pair_is_not_acted_on(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(SCHEDULER_FILE))
    doc = _review(db, policy={**POLICY, "code": "codex"})
    doc["metadata"]["runtime"]["alternate"]["runner_profile"] = "codex"

    make_scheduler().release_dependants("task_impl")

    assert db.docs["tasks/task_review"]["runner_profile"] == BROWSER


def test_the_re_review_inherits_its_reviews_resolved_profile(db, make_scheduler):
    _base(db)
    # The review resolved to claude-code; the integrator's own diff touched UI.
    _parent(db, "task_review", _git())
    db.docs["tasks/task_review"]["runner_profile"] = CODE
    _parent(db, "task_integrator", _git(UI_FILE))
    rereview = _review(db, "task_rereview", depends_on=("task_integrator", "task_review"))
    rereview["metadata"]["runtime"]["inherit"] = {"step": "review", "task_id": "task_review"}

    make_scheduler().release_dependants("task_review")

    doc = db.docs["tasks/task_rereview"]
    # MUTATION: re-decide from the integrator's diff and this is the browser.
    assert doc["runner_profile"] == CODE
    assert doc["metadata"]["runtime"]["resolved"]["reason"] == "inherited"


def test_a_re_review_whose_review_cannot_be_read_runs_on_the_browser():
    resolution = runtime_mod.resolve(
        ["task_review"], {**POLICY, "inherit": {"task_id": "task_review"}}, lambda _id: None)
    assert resolution.profile == BROWSER
    assert resolution.reason == "inherited_unreadable"


def test_an_inherit_naming_a_task_outside_depends_on_is_not_read():
    seen: list[str] = []

    def read(task_id: str) -> dict[str, Any]:
        seen.append(task_id)
        return {"runner_profile": CODE}

    resolution = runtime_mod.resolve(
        ["task_review"], {**POLICY, "inherit": {"task_id": "task_elsewhere"}}, read)
    assert resolution.profile == BROWSER and seen == []


def test_a_resolved_step_is_never_resolved_again(db, make_scheduler):
    _base(db)
    _parent(db, "task_impl", _git(UI_FILE))
    doc = _review(db)
    doc["metadata"]["runtime"]["resolved"] = {"profile": CODE}
    doc["runner_profile"] = CODE

    make_scheduler().release_dependants("task_impl")

    assert db.docs["tasks/task_review"]["runner_profile"] == CODE


def test_the_step_is_admitted_on_the_resolved_profiles_pools(db, make_scheduler, dispatcher):
    """Invariants 2 and 3: the profile is resolved in the promotion's write, so
    the one admission transaction reserves the resolved profile's pools."""
    seed_tenant(db, "eng", max_active=10, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=10)
    _parent(db, "task_impl", _git(SCHEDULER_FILE))
    _review(db)

    make_scheduler().release_dependants("task_impl")

    doc = db.docs["tasks/task_review"]
    assert doc["runner_profile"] == CODE
    leases = [d for k, d in db.docs.items() if k.startswith("leases/")]
    # It WAS admitted: an empty loop below would prove nothing.
    assert len(leases) == 1 and len(dispatcher.dispatched) == 1, doc.get("park_reason")
    assert "runner:claude-code" in leases[0]["pools"]
    for lease in leases:
        assert "runner:claude-code-browser" not in lease.get("pools", [])
        assert "resource:browser" not in lease.get("pools", [])
    for record in dispatcher.dispatched:
        assert record["profile"] == CODE


# ---------------------------------------------------------------------------
# swarm-api: the policy is accepted, refused and signed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block, match", [
    ({**POLICY, "code": "codex"}, "resolves only between"),
    ({**POLICY, "browser": "mock"}, "resolves only between"),
    ({**POLICY, "policy": "browser_always"}, "not one this platform runs"),
    ("browser_if_ui", "must be exactly"),
    ({**POLICY, "image": "evil"}, "must be exactly"),
    ({**POLICY, "alternate": {"runner_profile": "mock"}}, "must be exactly"),
])
def test_swarm_api_refuses_a_policy_outside_the_one_pair(block, match):
    with pytest.raises(validation.RuntimePolicyInvalid, match=match):
        validation.resolve_runtime_policy(
            {"runtime": block}, runner_profile=BROWSER, step_id="review", workflow_step=True)


def test_swarm_api_refuses_a_policy_step_not_submitted_on_the_browser():
    with pytest.raises(validation.RuntimePolicyInvalid, match="is submitted on"):
        validation.resolve_runtime_policy(
            {"runtime": POLICY}, runner_profile=CODE, step_id="review", workflow_step=True)


def test_swarm_api_refuses_a_policy_on_a_task():
    with pytest.raises(validation.RuntimePolicyInvalid, match="workflow step"):
        validation.resolve_runtime_policy(
            {"runtime": POLICY}, runner_profile=BROWSER, step_id=None, workflow_step=False)


def test_a_step_without_the_key_has_no_policy():
    assert validation.resolve_runtime_policy(
        {"other": 1}, runner_profile=CODE, step_id="x", workflow_step=True) is None


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def signed_client(db, tokens, group_map, signer) -> TestClient:
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _policy_workflow(review: dict[str, Any] | None = None, **top: Any) -> dict[str, Any]:
    return {
        "on_step_failure": "continue",
        "strategy": "direct-pr",
        "repository_url": "https://github.com/acme/widgets.git",
        "steps": [
            {"step_id": "impl", "runner_profile": CODE, "input": {"prompt": "implement"}},
            {
                "step_id": "review", "runner_profile": BROWSER,
                "input": {"prompt": "review"}, "depends_on": ["impl"],
                "metadata": {"runtime": dict(POLICY)}, **(review or {}),
            },
        ],
        **top,
    }


def _tasks_by_step(db) -> dict[str, tuple[str, dict[str, Any]]]:
    return {
        doc["step_id"]: (key.split("/", 1)[1], doc)
        for key, doc in db.docs.items()
        if key.startswith("tasks/") and key.count("/") == 1
    }


def test_a_policy_step_is_stored_on_the_browser_with_a_signed_alternate(
    signed_client, db, signer
):
    response = signed_client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_policy_workflow())
    assert response.status_code == 201, response.text
    task_id, doc = _tasks_by_step(db)["review"]
    assert doc["runner_profile"] == BROWSER
    assert signer.verifies(doc, task_id)
    block = doc["metadata"]["runtime"]
    assert {k: block[k] for k in POLICY} == POLICY
    alternate = block["alternate"]
    assert alternate["runner_profile"] == CODE
    # The alternate is the claude-code spec of THIS task, signed by swarm-api:
    # the document with its fields swapped in verifies, under the same id.
    swapped = {**doc, **{k: alternate[k] for k in (
        *validation.RUNTIME_SWAP_FIELDS, "spec_signature", "spec_key_version", "spec_format")}}
    assert signer.verifies(swapped, task_id)
    # And the scheduler's swap lands on exactly that document.
    patch = runtime_mod.swap_patch(
        doc, runtime_mod.Resolution(CODE, runtime_mod.REASON_NO_UI), at=None)
    assert signer.verifies({**doc, **patch}, task_id)
    impl_id, impl = _tasks_by_step(db)["impl"]
    assert "runtime" not in impl["metadata"]


def test_swarm_api_refuses_a_submitted_pair_outside_the_two(signed_client, db):
    body = _policy_workflow({"metadata": {"runtime": {**POLICY, "code": "mock"}}})
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_runtime"
    assert _tasks_by_step(db) == {}


def test_swarm_api_refuses_a_workflow_level_policy(signed_client, db):
    body = _policy_workflow(metadata={"runtime": dict(POLICY)})
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_runtime"


def test_a_policy_step_carrying_a_browser_only_input_is_refused(signed_client, db):
    body = _policy_workflow({"input": {"prompt": "review", "browser_tool": "auto"}})
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 422, response.text
    assert _tasks_by_step(db) == {}


# ---------------------------------------------------------------------------
# A review pinned to claude-code: its re-review asks for the policy (#2275)
# ---------------------------------------------------------------------------
# BGD, 2026-10-27 (Q-2275): the appended re-review of an on_merge_verdict
# workflow whose review is pinned to claude-code carries browser_if_ui, by
# catalogue names only, so a UI lane's fix round can reach a rendered MERGE
# and a non-UI lane's still runs on claude-code.


def _pinned_review_workflow(review_metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    review: dict[str, Any] = {
        "step_id": "review", "runner_profile": CODE, "depends_on": ["implement"],
        "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
        "input": {"prompt": "review the change"},
    }
    if review_metadata is not None:
        review = {**review, "runner_profile": BROWSER, "metadata": review_metadata}
    return {
        "strategy": "integrate",
        "repository_url": "https://github.com/acme/widgets.git",
        "metadata": {"merge": "on_merge_verdict"},
        "steps": [
            {"step_id": "implement", "runner_profile": CODE,
             "input": {"prompt": "implement the change"}},
            review,
            {"step_id": "fix", "runner_profile": CODE, "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
        ],
    }


def _own(*paths: str) -> dict[str, Any]:
    return {"git": {"own_files": list(paths), "files": [{"path": p} for p in paths]}}


def test_a_claude_code_reviews_rereview_carries_the_browser_if_ui_block(
    signed_client, db, signer
):
    response = signed_client.post(
        "/v1/workflows", headers=auth_header("alice"), json=_pinned_review_workflow())
    assert response.status_code == 201, response.text
    tasks = _tasks_by_step(db)
    # The review is what the caller pinned: claude-code, no policy.
    _, review = tasks["review"]
    assert review["runner_profile"] == CODE
    assert "runtime" not in review["metadata"]

    task_id, doc = tasks["re-review"]
    # MUTATION: copy the review's profile alone (today's main) and this is
    # claude-code with no block: no fix round of a UI lane can reach MERGE.
    assert doc["runner_profile"] == BROWSER
    block = doc["metadata"].get("runtime")
    assert block is not None, "the re-review of a claude-code review carries no runtime"
    assert {k: block[k] for k in POLICY} == POLICY
    # Not inherited: the review it would inherit from was pinned, never resolved.
    assert "inherit" not in block
    # Signed under both profiles: as stored, and with the claude-code spec swapped in.
    assert signer.verifies(doc, task_id)
    alternate = block["alternate"]
    assert alternate["runner_profile"] == CODE
    swapped = {**doc, **{k: alternate[k] for k in (
        *validation.RUNTIME_SWAP_FIELDS, "spec_signature", "spec_key_version", "spec_format")}}
    assert signer.verifies(swapped, task_id)

    # The scheduler decides from the lane's own files.
    by_task = {tid: step for step, (tid, _) in tasks.items()}

    def resolve(files: dict[str, tuple[str, ...]]) -> str:
        def read(tid: str) -> dict[str, Any]:
            return {"result_summary": _own(*files.get(by_task[tid], ()))}
        return runtime_mod.resolve(list(doc["depends_on"]), block, read).profile

    assert resolve({"implement": (SCHEDULER_FILE,), "fix": (SCHEDULER_FILE,)}) == CODE
    assert resolve({"implement": (UI_FILE,), "fix": (SCHEDULER_FILE,)}) == BROWSER
    assert resolve({"implement": (SCHEDULER_FILE,), "fix": (UI_FILE,)}) == BROWSER


def test_a_browser_if_ui_reviews_rereview_still_inherits_its_resolution(signed_client, db):
    response = signed_client.post(
        "/v1/workflows", headers=auth_header("alice"),
        json=_pinned_review_workflow({"runtime": dict(POLICY)}))
    assert response.status_code == 201, response.text
    tasks = _tasks_by_step(db)
    review_id, _ = tasks["review"]
    _, doc = tasks["re-review"]
    assert doc["metadata"]["runtime"]["inherit"] == {"step": "review", "task_id": review_id}


def test_rereview_step_for_adds_the_policy_only_to_a_claude_code_review():
    steps = [
        validation.StepSpec(step_id="implement", depends_on=(), runner_profile=CODE),
        validation.StepSpec(step_id="review", depends_on=("implement",), builds_on="implement",
                            input_from={"implement": "swarm-work.patch"}, runner_profile=CODE),
        validation.StepSpec(step_id="fix", depends_on=("review",), builds_on="implement",
                            input_from={"review": "verdict.json"}, when_step="review",
                            when_verdicts=("NOT_YET",), runner_profile=CODE),
    ]
    review = {"step_id": "review", "runner_profile": CODE, "input": {"prompt": "r"},
              "input_from": {"implement": "swarm-work.patch"}, "metadata": {"keep": "me"}}
    rereview = validation.rereview_step_for(steps, review, "integrate")
    assert rereview["runner_profile"] == BROWSER
    assert rereview["metadata"]["runtime"] == POLICY
    assert rereview["metadata"]["keep"] == "me"
    # Any other profile is the caller's choice and is copied as it was.
    for profile in ("mock", "codex", BROWSER):
        other = validation.rereview_step_for(
            steps, {**review, "runner_profile": profile}, "integrate")
        assert other["runner_profile"] == profile
        assert "runtime" not in other["metadata"]
    # A review that already asked for the policy passes its own block on.
    asked = validation.rereview_step_for(
        steps, {**review, "runner_profile": BROWSER, "metadata": {"runtime": dict(POLICY)}},
        "integrate")
    assert asked["runner_profile"] == BROWSER and asked["metadata"]["runtime"] == POLICY


# ---------------------------------------------------------------------------
# The issue-run planner, and parity
# ---------------------------------------------------------------------------


def test_the_issue_run_planners_review_asks_for_the_policy():
    from swarm_api import issueruns

    assert issueruns.REVIEW_PROFILE == BROWSER
    assert dict(issueruns.REVIEW_RUNTIME) == POLICY
    assert issueruns.STEP_PROFILE == CODE


def test_the_restated_constants_match_swarm_api():
    assert runtime_mod.RUNTIME_METADATA_KEY == validation.RUNTIME_METADATA_KEY
    assert runtime_mod.POLICY_BROWSER_IF_UI == validation.RUNTIME_POLICY_BROWSER_IF_UI
    assert {"browser": runtime_mod.BROWSER_PROFILE, "code": runtime_mod.CODE_PROFILE} == dict(
        validation.RUNTIME_POLICY_PROFILES)
    assert runtime_mod.RUNTIME_SWAP_FIELDS == validation.RUNTIME_SWAP_FIELDS


@pytest.mark.parametrize("path", [
    UI_FILE, *UI_TEST_FILES, SCHEDULER_FILE, "docs/web-ui/index.html", "/apps/swarm-ui/x.css",
    "apps/swarm-ui/src/testsuite/Panel.tsx", "apps/swarm-ui/src/App.test.tsx",
    "docs/web-ui/tests/a.html", "apps/swarm-ui-old/a.tsx", "tests/apps/swarm-ui/a.tsx", "",
    "apps/swarm-ui/src/views/Accounts.spec.tsx", "docs/web-ui/accounts.test.html",
])
def test_the_scheduler_and_the_gate_share_one_ui_path_rule(path):
    assert runtime_mod.UI_PATH_PREFIXES == verdict_mod.UI_PATH_PREFIXES
    assert runtime_mod.TEST_DIRECTORIES == verdict_mod.TEST_DIRECTORIES
    assert runtime_mod.TEST_FILE_MARKS == verdict_mod.TEST_FILE_MARKS
    assert runtime_mod.TEST_FILE_MARK_PREFIX == verdict_mod.TEST_FILE_MARK_PREFIX
    assert runtime_mod.is_ui_path(path) == verdict_mod.is_ui_path(path)


def test_browser_if_ui_keeps_the_gates_vitest_rule_and_its_docs_page_rule():
    # The rendered-review gate's rule from #2379 F03 (#2396), held for the
    # scheduler too: a `.spec.` vitest file under apps/swarm-ui is no UI path,
    # while a docs/web-ui page stays one whatever its name.
    # MUTATION: drop ".spec." from TEST_FILE_MARKS, or the apps/swarm-ui scope
    # of the filename mark, and this fails.
    assert not runtime_mod.is_ui_path("apps/swarm-ui/src/views/Accounts.spec.tsx")
    assert runtime_mod.is_ui_path("docs/web-ui/accounts.test.html")
    assert not runtime_mod.is_ui_path("docs/web-ui/tests/a.html")
