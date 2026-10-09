"""A MERGE verdict's pull request, opened by swarm-api without a worker (#748).

docs/workflows.md "When swarm-api opens it without a worker". The gated
integrator of an implement -> review -> fix workflow runs no agent on MERGE,
yet it started a container, cloned and pushed only to open the pull request:
116 s and one execution per workflow, measured 2026-10-06. swarm-api now hears
the same `task_finished` wake (`POST /v1/admin/tasks/finished`), creates
`swarm/<fix>` at the commit the implementer pushed, opens the pull request
with the implementer's own text and ends the step SUCCEEDED, while the
scheduler holds the step PARKED for it.

What these tests hold:

  1. The restated rules are the worker's and the scheduler's: file names,
     bounds, attribution markers and stripping, the verdict lines, the
     marker key, the claim timeout, and which steps are held. One table of
     titles goes through the worker's `_agent_title`, the merge step's
     `title_is_placeholder` and `title_refusal`, outcomes held equal.
  2. Off until contract request 52: with no PARKED -> SUCCEEDED edge,
     nothing is read, claimed or written.
  3. MERGE with one contributor: one branch at the implementer's pushed
     commit, one pull request titled and described by the implementer, the
     step SUCCEEDED with `published_by: control_plane` and
     `verdict_gate.agent_ran: false`, and the step's own wake rung. The
     merge step (MS1-MS4) then merges that pull request through the
     worker's own `merge.run_merge`, exactly as it merges a worker's.
  4. Every other case declines, writes only the marker, opens nothing and
     rings the parent's wake so the scheduler promotes the step at once.
  5. Twice delivered, published once; another tenant's wake is a no-op; an
     open pull request already there is adopted.
  6. The scheduler holds the step only while it should, and never while
     the contract or its setting says no.
  7. The forge credential is trusted only as signed: a spec that does not
     verify declines before any secret is read, and a user slot's grant is
     read again and must still be `write`.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import access, forge, forgeapp, forgewrite, gittokens, verdictpublish
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_common import specsign
from swarm_common.states import ParkReason, TaskState, can_transition

from . import forge_fakes
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .conftest import (
    PROJECT,
    api_settings,
    auth_header,
    scheduler_settings,
    seed_task,
    seed_tenant,
)

SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
BUCKET = f"swarm-artifacts-{PROJECT}"
REPO_URL = "https://github.com/saga-xyz/widgets"
HEAD = "c" * 40
IMPL = "task_impl000001"
REVIEW = "task_review0001"
FIX = "task_fix0000001"
WORKFLOW = "wf_0000000001"
TITLE = "The widget cache expires entries on read"
BODY = "Expire on read.\n\nCloses #12\n\nGenerated with [Claude Code](https://claude.com/claude-code)"

#: The step-spec key, made at import: the one submission signs the fix step
#: with, and the one whose public half is swarm-api's SPEC_VERIFY_KEYS.
SIGNER = LocalSpecSigner()


class RecordingWaker:
    enabled = True

    def __init__(self) -> None:
        self.rung: list[tuple[str, dict[str, str]]] = []

    def wake(self, reason: str, **attributes: str) -> bool:
        self.rung.append((reason, dict(attributes)))
        return True


class SecretStore:
    """Secret Manager's git-token slots, read through `read_slot` as swarm-api reads them.

    Every tenant's `-git` slot holds a token made on first read; a narrower
    `git-r-`/`git-u-` slot holds only what a test `put` there. `asked` is
    every secret name read, so a test can say which credential was used.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.asked: list[str] = []

    def put(self, secret_id: str, value: str) -> None:
        self.values[secret_id] = value

    def token_for(self, tenant) -> str:
        return self.read_slot(tenant, forge.GIT_PROVIDER).value

    def read_slot(self, tenant, provider: str) -> forge.SlotValue:
        secret_id = tenant.secret_name(provider)
        self.asked.append(secret_id)
        if provider == forge.GIT_PROVIDER:
            self.values.setdefault(secret_id, forge_fakes.make_token())
        if secret_id not in self.values:
            raise forge.NoForgeCredential(f"no value stored in {secret_id}")
        return forge.SlotValue(self.values[secret_id], "1")


@pytest.fixture
def store() -> SecretStore:
    return SecretStore()


@pytest.fixture
def contract(monkeypatch):
    """Contract request 52 applied, for this test only."""
    monkeypatch.setattr(verdictpublish, "contract_allows", lambda: True)


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = forge_fakes.GitHubWrites()
    gh.branches[f"swarm/{IMPL}"] = HEAD
    return gh


@pytest.fixture
def waker() -> RecordingWaker:
    return RecordingWaker()


@pytest.fixture
def client(db, tokens, group_map, objects, github, waker, store) -> TestClient:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(
            rollup_sweeper_users=(SWEEPER,),
            spec_signing_key_version=KEY_VERSION,
            spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
        ),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=waker,
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=store,
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate, fetch=github.fetch),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _finished(db, objects, task_id: str, *, files: dict[str, str], tenant_id: str = "eng",
              git: dict[str, Any] | None = None) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED",
                    workflow_id=WORKFLOW)
    entries = []
    for name, text in files.items():
        key = f"tenants/{tenant_id}/tasks/{task_id}/attempts/att_1/artifacts/{name}"
        objects.put(key, text.encode("utf-8"))
        entries.append({"name": name, "bytes": len(text.encode("utf-8")),
                        "uri": f"gs://{BUCKET}/{key}"})
    doc["result_summary"] = {"artifacts": entries, "logs": {}, **({"git": git} if git else {})}
    doc["completed_at"] = datetime.now(timezone.utc)
    return doc


def _workflow(db, objects, *, verdict: dict | str | None = None, title: str | None = TITLE,
              body: str | None = BODY, integrates: list[str] | None = None,
              dispatch: dict[str, Any] | None = None, tenant_id: str = "eng") -> dict:
    files = {}
    if title is not None:
        files["pr-title.txt"] = title + "\n"
    if body is not None:
        files["pr-body.md"] = body
    _finished(db, objects, IMPL, files=files, tenant_id=tenant_id,
              git={"branch": f"swarm/{IMPL}", "pushed_head": HEAD, "published": True})
    verdict = {"verdict": "MERGE", "findings": ["naming is fine"]} if verdict is None else verdict
    _finished(db, objects, REVIEW, tenant_id=tenant_id,
              files={"verdict.json": verdict if isinstance(verdict, str) else json.dumps(verdict)})
    fix = seed_task(db, task_id=FIX, tenant_id=tenant_id, state="PARKED", workflow_id=WORKFLOW,
                    park_reason=ParkReason.DEPENDENCY_INCOMPLETE.value,
                    depends_on=(IMPL, REVIEW), runner_profile="claude-code")
    fix["repository_url"] = REPO_URL
    block = {
        "strategy": "integrate", "carrier": "patches", "role": "integrator",
        "integrates": [IMPL] if integrates is None else integrates,
        "builds_on": IMPL,
        "verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]},
        **(dispatch or {}),
    }
    fix["metadata"] = {"dispatch": block, "input_from": {REVIEW: "verdict.json"}}
    return _sign(fix)


def _sign(doc: dict) -> dict:
    """Sign the stored document in place, as submission does: call again after
    a test changes a signed field, or the publish declines it as rewritten."""
    return SIGNER.sign_document(doc, doc["id"])


def _push(client: TestClient, task_id: str = REVIEW, *, tenant_id: str = "eng",
          reason: str = "task_finished", in_data: bool = False):
    fields = {"reason": reason, "task_id": task_id, "tenant_id": tenant_id, "state": "succeeded"}
    message: dict[str, Any] = {"messageId": "1"}
    if in_data:
        message["data"] = base64.b64encode(json.dumps(fields).encode()).decode()
    else:
        message["attributes"] = fields
    return client.post("/v1/admin/tasks/finished", headers=SWEEPER_HEADERS,
                       json={"message": message, "subscription": "projects/p/subscriptions/s"})


def _fix(db) -> dict:
    return db.docs[f"tasks/{FIX}"]


def _writes(github) -> list[tuple[str, str]]:
    return github.writes()


# --------------------------------------------------------------------------
# 1. the restated rules are the worker's and the scheduler's
# --------------------------------------------------------------------------

#: One table of titles, driven through the worker's own `Worker._agent_title`,
#: the merge step's `merge.title_is_placeholder` and `title_refusal` alike, so
#: a rule added on one side and not the other fails here rather than in a
#: pull request. The implementer's worker checked the title against its OWN
#: task id, so IMPL stands in for it below.
SHARED_TITLES = (
    TITLE,
    "Fix the [swarm] dashboard",
    "Tabs\tare allowed",
    "x" * 400,
    "",
    "   ",
    "two\nlines",
    "carriage\rreturn",
    "bell\x07",
    "Fix it (Generated with a tool)",
    "Co-Authored-By: someone",
    "See claude.ai/code",
    f"Finish {IMPL}",
    "[swarm] task_abc",
    "  [SWARM] Task_0123abcd",
    "Fix [swarm] task_ handling",
    "[Swarm]   TASK_retired",
    "[swarm]\ttask_9be4128488d342208947",
    "Undo what [swarm] task_ did",
)

#: Titles the API refuses and the implementer's worker accepted: any task id
#: at all, where the worker refuses only its own. The title was written by
#: another task, so every id in it is one the owner's rule (2026-09-28)
#: forbids. The API may be the stricter; it is never the looser.
API_STRICTER_TITLES = (
    "Follow up on task_review0001",
    "Revert TASK_abcdef12",
)


def _worker_title_refusal(title: str) -> str | None:
    """The worker's own `_agent_title` over `title`, as `title_refusal` reports."""
    from agent_worker import lifecycle

    worker = lifecycle.Worker.__new__(lifecycle.Worker)
    worker.cfg = SimpleNamespace(task_id=IMPL)
    worker._read_agent_text = lambda name, refused: title
    worker._scrub = lambda value: value
    refused: list[str] = []
    accepted = lifecycle.Worker._agent_title(worker, refused)
    if accepted is not None:
        assert refused == [], refused
        return None
    assert len(refused) == 1, refused
    prefix = f"{lifecycle.PR_TITLE_FILE}: "
    assert refused[0].startswith(prefix), refused
    return refused[0][len(prefix):]


def test_the_restated_worker_rules_are_the_workers():
    from agent_worker import gitops, lifecycle, merge, verdict

    for title in SHARED_TITLES:
        assert verdictpublish.title_refusal(title) == _worker_title_refusal(title), repr(title)
        assert verdictpublish.title_is_placeholder(title) is merge.title_is_placeholder(title), (
            repr(title))
        if merge.title_is_placeholder(title):
            assert verdictpublish.title_refusal(title) is not None, repr(title)
    for title in API_STRICTER_TITLES:
        assert _worker_title_refusal(title) is None, repr(title)
        assert verdictpublish.title_refusal(title) == "names a task id", repr(title)
    # The table reaches every refusal and acceptance, so it cannot pass empty.
    outcomes = {verdictpublish.title_refusal(t) for t in SHARED_TITLES}
    assert outcomes == {None, "blank", "more than one line", "holds control characters",
                        "carries attribution", "names a task id"}, outcomes
    assert any(merge.title_is_placeholder(t) for t in SHARED_TITLES)
    assert verdictpublish.PLACEHOLDER_TITLE_PREFIX == merge.PLACEHOLDER_TITLE_PREFIX
    assert verdictpublish._RETIRED_TITLE_RE.pattern == merge.RETIRED_TITLE_RE.pattern
    assert verdictpublish._RETIRED_TITLE_RE.flags == lifecycle._RETIRED_TITLE_RE.flags

    assert verdictpublish.ATTRIBUTION_MARKERS == gitops.ATTRIBUTION_MARKERS
    assert verdictpublish.PR_TITLE_FILE == lifecycle.PR_TITLE_FILE
    assert verdictpublish.PR_BODY_FILE == lifecycle.PR_BODY_FILE
    assert verdictpublish.PR_TITLE_MAX_CHARS == lifecycle.PR_TITLE_MAX_CHARS
    assert verdictpublish.PR_BODY_MAX_BYTES == lifecycle.PR_BODY_MAX_BYTES
    assert verdictpublish.PR_READ_LIMIT_BYTES == lifecycle.PR_READ_LIMIT_BYTES
    assert verdictpublish._ROBOT_FACE == lifecycle._ROBOT_FACE
    assert verdictpublish._RETIRED_TITLE_RE.pattern == lifecycle._RETIRED_TITLE_RE.pattern
    assert verdictpublish.MAX_VERDICT_BYTES == verdict.MAX_VERDICT_BYTES
    assert verdictpublish.MAX_FINDINGS == verdict.MAX_FINDINGS
    assert verdictpublish.MAX_FINDING_CHARS == verdict.MAX_FINDING_CHARS
    assert verdictpublish.REVIEW_VERDICTS == verdict.REVIEW_VERDICTS

    for text in (BODY, "plain", "Co-Authored-By: x <x@y>", "\U0001f916 Generated with x",
                 "a\n\n\nCo-authored-by: b\n\n\nc", "---\nGenerated with x"):
        assert verdictpublish.strip_attribution(text) == lifecycle.strip_attribution(text), text

    record = {"task_id": REVIEW, "file": "verdict.json", "verdict": "MERGE", "agent_ran": False,
              "findings": ["one ``` two", "three"], "findings_dropped": 2}
    assert verdictpublish.verdict_lines(record) == verdict.pull_request_lines(record)

    findings = [{"summary": "s" * 2000}, {"x": 1}, "  spaced   out  ", 7]
    worker = [verdict._finding_text(f) for f in findings]
    assert [verdictpublish._finding_text(f) for f in findings] == worker


def test_the_scheduler_restates_the_marker_the_claim_and_who_is_held():
    from scheduler import loop

    assert loop.CONTROL_PUBLISH_METADATA_KEY == verdictpublish.CONTROL_PUBLISH_METADATA_KEY
    assert loop.CONTROL_PUBLISH_CLAIMED == verdictpublish.CLAIMED
    assert loop.CONTROL_PUBLISH_CLAIM_SECONDS == verdictpublish.CLAIM_TIMEOUT_SECONDS
    gate = {"task_id": REVIEW, "verdict_in": ["NOT_YET"]}
    cases = [
        {"dispatch": {"strategy": "integrate", "role": "integrator", "verdict_gate": gate}},
        {"dispatch": {"strategy": "integrate", "role": "contributor", "verdict_gate": gate}},
        {"dispatch": {"strategy": "direct-pr", "role": "integrator", "verdict_gate": gate}},
        {"dispatch": {"strategy": "integrate", "role": "integrator"}},
        {"dispatch": {"strategy": "integrate", "role": "integrator", "verdict_gate": "x"}},
        {"dispatch": "not a block"},
        {},
        None,
    ]
    held = [loop.held_for_control_publish(c) for c in cases]
    assert held == [verdictpublish.held_by_scheduler(c) for c in cases]
    assert held[0] is True and not any(held[1:])


def test_contract_allows_reads_the_frozen_state_machine():
    assert verdictpublish.contract_allows() is can_transition(TaskState.PARKED, TaskState.SUCCEEDED)


@pytest.mark.parametrize("title,why", [
    ("", "blank"),
    ("two\nlines", "more than one line"),
    ("bell\x07", "holds control characters"),
    ("Fix it (Generated with a tool)", "carries attribution"),
    (f"Finish {IMPL}", "names a task id"),
    ("[swarm] task_abc", "names a task id"),
    (TITLE, None),
])
def test_title_refusal_is_the_workers_list(title, why):
    assert verdictpublish.title_refusal(title) == why


@pytest.mark.parametrize("title", [
    "[swarm] task_",
    "[swarm] task_abc",
    "   [SWARM] TASK_x",
    "Fix [swarm] task_ handling",
    "fix [Swarm]task_ spacing",
    "Undo [swarm]    task_ in the middle",
    "[swarm]\ttask_tabbed",
])
def test_title_refusal_refuses_every_title_the_merge_step_calls_a_placeholder(title):
    """Issue #748: "the same refusal scans the worker runs (... title
    placeholder)". The merge step (`agent_worker.merge.title_is_placeholder`)
    refuses to merge a pull request so titled; one the API opened with it
    would sit unmergeable, so the API refuses it before opening anything."""
    from agent_worker import merge

    assert merge.title_is_placeholder(title) is True
    assert verdictpublish.title_is_placeholder(title) is True
    assert verdictpublish.title_refusal(title) == "names a task id"


# --------------------------------------------------------------------------
# 2. off until contract request 52
# --------------------------------------------------------------------------

def test_off_until_contract_request_52(db, objects, client, github, waker, monkeypatch):
    monkeypatch.setattr(verdictpublish, "contract_allows", lambda: False)
    _workflow(db, objects)
    before = json.dumps(_fix(db), default=str, sort_keys=True)

    response = _push(client)

    assert response.status_code == 200, response.text
    assert response.json()["report"]["skipped"] == "contract_request_52"
    assert github.calls == []
    assert waker.rung == []
    assert json.dumps(_fix(db), default=str, sort_keys=True) == before


# --------------------------------------------------------------------------
# 3. MERGE with one contributor
# --------------------------------------------------------------------------

def test_merge_opens_the_pull_request_and_ends_the_step_without_a_worker(
    db, objects, client, github, waker, contract
):
    _workflow(db, objects)

    response = _push(client)

    assert response.status_code == 200, response.text
    assert response.json()["report"]["published"] == [FIX], response.json()
    # One branch, at the commit the implementer recorded it pushed.
    assert github.branches[f"swarm/{FIX}"] == HEAD
    pulls = list(github.pulls.values())
    assert len(pulls) == 1
    pull = pulls[0]
    assert pull["head"]["ref"] == f"swarm/{FIX}" and pull["base"]["ref"] == "main"
    assert pull["title"] == TITLE
    assert pull["body"].startswith("Expire on read.\n\nCloses #12")
    assert "Generated with" not in pull["body"] and "claude.com" not in pull["body"]
    assert "Review verdict: **MERGE** (from `verdict.json`" in pull["body"]
    assert f"- merged: `swarm/{IMPL}`" in pull["body"]

    fix = _fix(db)
    assert fix["state"] == TaskState.SUCCEEDED.value
    assert fix["park_reason"] is None and fix["current_lease_id"] is None
    summary = fix["result_summary"]
    assert summary["published_by"] == "control_plane"
    assert summary["verdict_gate"]["agent_ran"] is False
    assert summary["verdict_gate"]["verdict"] == "MERGE"
    assert summary["skipped_agent"] == "review verdict MERGE"
    assert summary["pull_request_text_from"] == {"title": "implementer", "body": "implementer"}
    # The shape issueci, cifix and the UI read from a worker's summary.
    assert summary["git"]["pull_request"]["number"] == pull["number"]
    assert summary["git"]["pull_request"]["url"] == pull["html_url"]
    assert summary["git"]["pushed_head"] == HEAD
    assert summary["git"]["branch"] == f"swarm/{FIX}"
    assert fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]["state"] == "published"
    # The step's own wake, so its dependants (the merge step) are released.
    assert waker.rung == [("task_finished",
                           {"task_id": FIX, "tenant_id": "eng", "state": "SUCCEEDED"})]


def _merge_world(tmp_path):
    """tests/unit/worker/merge_world.py: the merge step's own test world.

    It lives beside the worker's tests, which pytest puts on `sys.path` only
    when it collects them, so a run of this file alone puts it there.
    """
    worker_tests = str(Path(__file__).resolve().parents[1] / "worker")
    if worker_tests not in sys.path:
        sys.path.insert(0, worker_tests)
    import merge_world

    return merge_world, merge_world.MergeWorld(tmp_path)


def test_the_merge_step_finds_a_control_published_pull_request_by_its_branch(
    db, objects, client, github, contract, tmp_path
):
    """The merge step (docs/merge-step.md, MS1-MS4) merges the pull request
    this step published as it merges a worker's.

    It reads the integrator's `result_summary.git` for the number and the
    pushed head it pins, then requires the live pull request to be
    `swarm/<integrator>` from the same repository at that head
    (`agent_worker.merge.run_merge`). So the published document and the
    pull request are handed to the worker's own merge action unchanged. The
    second half is the control: the same pull request on the implementer's
    branch, which a publish that reused `swarm/<implementer>` would leave, is
    refused, so a pass here is the branch matching, not a check that is
    never made.
    """
    from agent_worker import merge

    mw, world = _merge_world(tmp_path)
    # The fake numbers the pull request it opens next after `_next`: the
    # number the merge world serves, so its routes answer for this one.
    github._next = mw.NUMBER - 1
    assert HEAD == mw.PINNED, "the merge world serves green checks at its pinned head"
    _workflow(db, objects)
    assert _push(client).json()["report"]["published"] == [FIX]
    fix = _fix(db)
    (pull,) = github.pulls.values()

    world.docs[FIX] = {
        "tenant_id": fix["tenant_id"],
        "workflow_id": mw.WORKFLOW,
        "metadata": {"dispatch": dict(fix["metadata"]["dispatch"])},
        "result_summary": fix["result_summary"],
    }
    world.target = {**world.target, "pull_request": FIX}
    world.pr.update(
        number=pull["number"], title=pull["title"], body=pull["body"],
        head={**world.pr["head"], "ref": pull["head"]["ref"], "sha": pull["head"]["sha"]},
    )

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["pull_request_task"] == FIX
    assert outcome.summary["pull_request"] == pull["number"] == mw.NUMBER
    assert outcome.summary["pinned"] == HEAD
    assert outcome.summary["merged_by_this_task"] is True
    (call,) = world.merge_calls()
    assert call.body["sha"] == HEAD
    assert call.body["commit_title"] == f"{TITLE} (#{mw.NUMBER})"

    # The control: the implementer's branch is not this workflow's pull request.
    _, other = _merge_world(tmp_path / "control")
    other.docs[FIX] = world.docs[FIX]
    other.target = dict(world.target)
    other.pr.update(number=pull["number"], title=pull["title"], body=pull["body"],
                    head={**world.pr["head"], "ref": f"swarm/{IMPL}"})

    refused = merge.run_merge(other.context())

    assert refused.state is not TaskState.SUCCEEDED
    assert refused.summary["refusal"]["code"] == "pull_request_not_this_workflows"
    assert other.merge_calls() == []


def test_the_wake_is_read_from_the_data_when_it_carries_no_attributes(
    db, objects, client, github, contract
):
    _workflow(db, objects)
    assert _push(client, in_data=True).json()["report"]["published"] == [FIX]


def test_a_body_is_optional_and_the_generated_one_stands_alone(
    db, objects, client, github, contract
):
    _workflow(db, objects, body=None)
    _push(client)
    pull = next(iter(github.pulls.values()))
    assert pull["body"].startswith("Opened by SwarmCloud's control plane.")
    assert _fix(db)["result_summary"]["pull_request_text_from"]["body"] is None


def test_mentions_in_the_implementers_text_page_no_one(db, objects, client, github, contract):
    _workflow(db, objects, title="Ask @octocat about it", body="cc @octocat")
    _push(client)
    pull = next(iter(github.pulls.values()))
    assert "@octocat" not in pull["title"] and "@octocat" not in pull["body"]


# --------------------------------------------------------------------------
# 4. every other case declines, and the scheduler takes it at once
# --------------------------------------------------------------------------

def _credential() -> str:
    return "ghp_" + secrets.token_hex(18)


@pytest.mark.parametrize("case,code", [
    ("not_yet", "agent_runs"),
    ("unreadable_verdict", "verdict_unreadable"),
    ("minor_finding", "minor_findings"),
    ("two_contributors", "contributors"),
    ("continues", "continuation"),
    ("no_title", "title_absent"),
    ("task_id_title", "title_unusable"),
    ("attribution_title", "title_unusable"),
    ("credential_in_body", "credential"),
    ("branch_moved", "implementer_branch_moved"),
    ("github_refuses", "forge"),
])
def test_every_other_case_declines_and_rings_the_parent(
    db, objects, client, github, waker, contract, case, code
):
    kwargs: dict[str, Any] = {}
    if case == "not_yet":
        kwargs["verdict"] = {"verdict": "NOT_YET", "findings": ["fix it"]}
    elif case == "unreadable_verdict":
        kwargs["verdict"] = "not json"
    elif case == "minor_finding":
        kwargs["verdict"] = {"verdict": "MERGE",
                             "findings": [{"severity": "minor", "summary": "a nit"}]}
    elif case == "two_contributors":
        kwargs["integrates"] = [IMPL, "task_other00001"]
    elif case == "continues":
        kwargs["dispatch"] = {"continues": "task_root000001"}
    elif case == "no_title":
        kwargs["title"] = None
    elif case == "task_id_title":
        kwargs["title"] = f"Finish {IMPL}"
    elif case == "attribution_title":
        kwargs["title"] = "Fix (Co-Authored-By: someone)"
    elif case == "credential_in_body":
        kwargs["body"] = f"token: {_credential()}"
    _workflow(db, objects, **kwargs)
    if case == "branch_moved":
        github.branches[f"swarm/{IMPL}"] = "d" * 40
    if case == "github_refuses":
        github.refuse[("POST", "/repos/saga-xyz/widgets/pulls")] = 422

    body = _push(client).json()

    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": code}], body
    fix = _fix(db)
    assert fix["state"] == TaskState.PARKED.value
    assert fix["park_reason"] == ParkReason.DEPENDENCY_INCOMPLETE.value
    marker = fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]
    assert marker["state"] == "declined" and marker["code"] == code
    assert github.pulls == {}
    if case != "github_refuses":
        assert ("POST", "/repos/saga-xyz/widgets/pulls") not in _writes(github)
    # The parent's wake: the scheduler resolves the step again, now unheld.
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]


# --------------------------------------------------------------------------
# 4b. the step's own forge credential (contract request 54)
# --------------------------------------------------------------------------

#: A `git-r-<hex>` slot: the repository's narrower credential, the one the
#: gated step's own worker would read (`agent_worker.secrets.forge_suffix`).
REPO_SLOT = "git-r-" + "0123456789abcdef"


def _authorisations(github) -> set[str]:
    return {headers.get("Authorization", "") for _, _, headers, _ in github.calls}


def test_the_steps_own_forge_credential_opens_the_pull_request(
    db, objects, client, github, waker, store, contract
):
    fix = _workflow(db, objects)
    fix["forge_credential"] = REPO_SLOT
    fix["forge_access"] = "write"
    _sign(fix)
    narrow = "ghp_" + secrets.token_hex(18)
    store.put(gittokens.secret_name_for("eng", REPO_SLOT), narrow)

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    # Read through the step's own slot, never the tenant's `-git`.
    assert store.asked == [gittokens.secret_name_for("eng", REPO_SLOT)]
    auth = _authorisations(github)
    assert auth and all(narrow in value for value in auth), "every GitHub call used the slot's token"
    tenant_token = store.values.get(gittokens.secret_name_for("eng", forge.GIT_PROVIDER))
    assert tenant_token is None
    # The token is in no stored field, no summary and no marker.
    assert narrow not in json.dumps(_fix(db), default=str)
    assert narrow not in json.dumps(body)
    assert _fix(db)["state"] == TaskState.SUCCEEDED.value


@pytest.mark.parametrize("access,credential,stored", [
    ("read", None, False),
    ("read", REPO_SLOT, True),
    ("write", "git-x-" + "0" * 16, False),
    ("write", REPO_SLOT, False),
])
def test_a_read_only_forge_credential_declines_to_the_worker(
    db, objects, client, github, waker, store, contract, access, credential, stored
):
    """Read-only, a malformed suffix or an unreadable slot: the worker's path, as today."""
    fix = _workflow(db, objects)
    fix["forge_access"] = access
    if credential is not None:
        fix["forge_credential"] = credential
    _sign(fix)
    if stored:
        store.put(gittokens.secret_name_for("eng", REPO_SLOT), "ghp_" + secrets.token_hex(18))

    body = _push(client).json()

    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": "credential"}], body
    fix = _fix(db)
    assert fix["state"] == TaskState.PARKED.value
    assert fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]["code"] == "credential"
    assert github.calls == [] and github.pulls == {}
    if access == "read" or credential != REPO_SLOT:
        # Declined before any secret was read: a read-only step never has a
        # push credential fetched for it, and a malformed suffix names none.
        assert store.asked == []
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]


def test_the_token_in_the_body_declines(db, objects, client, github, waker, store, contract):
    """The step's own token in pr-body.md, in a shape the redaction does not mask.

    So it is the token-value scan, not `_read_text`'s redaction, that declines.
    """
    token = "widgetkey" + "Q" * 6 + "zebra"
    _workflow(db, objects, body=f"Expire on read.\n\nUse {token} to test.")
    fix = _fix(db)
    fix["forge_credential"] = REPO_SLOT
    _sign(fix)
    store.put(gittokens.secret_name_for("eng", REPO_SLOT), token)

    body = _push(client).json()

    assert body["report"]["declined"] == [{"task_id": FIX, "code": "credential"}], body
    marker = _fix(db)["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]
    assert "redaction" not in marker["reason"] and "token" in marker["reason"]
    assert token not in json.dumps(_fix(db), default=str)
    assert token not in json.dumps(body)
    assert github.calls == [] and github.pulls == {}


# --------------------------------------------------------------------------
# 4c. the credential is trusted only as signed, and a person's grant is re-read
# --------------------------------------------------------------------------

#: Another member of the tenant: their user slot, and the grant behind it.
MEMBER = "dana@saga.xyz"
USER_SLOT = gittokens.provider_suffix(gittokens.Scope.USER, user=MEMBER)


def _grant(db, *, mode: str = "write", tenant_id: str = "eng") -> str:
    """The member's grant on the step's repository, by swarm-api's own id recipe."""
    repo_id = repo_id_for("eng", "saga-xyz", "widgets")
    doc_id = access.grant_id_for("eng", MEMBER, repo_id)
    db.docs[f"{forgeapp.GRANTS}/{doc_id}"] = {
        "tenant_id": tenant_id, "user": MEMBER, "user_hash": forgeapp.user_hash(MEMBER),
        "repo_id": repo_id, "repository": "saga-xyz/widgets", "mode": mode,
    }
    return f"{forgeapp.GRANTS}/{doc_id}"


def _user_slot_step(db, objects, store) -> tuple[dict, str]:
    """A step submitted with the member's slot, their grant on file, their token stored."""
    fix = _workflow(db, objects)
    fix["forge_credential"] = USER_SLOT
    _sign(fix)
    _grant(db)
    token = "ghp_" + secrets.token_hex(18)
    store.put(gittokens.secret_name_for("eng", USER_SLOT), token)
    return fix, token


def _declined_unread(body: dict, db, github, store) -> None:
    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": "credential"}], body
    assert _fix(db)["state"] == TaskState.PARKED.value
    # Declined before any secret was read and before GitHub was asked anything.
    assert store.asked == []
    assert github.calls == [] and github.pulls == {}


def _rewrite(fix: dict, case: str) -> None:
    """The tenant's rewrite: always `forge_access` cleared, which is what buys
    a push, plus one more change that the signature check has to catch."""
    fix["forge_access"] = None
    if case == "unsigned":
        for key in ("spec_signature", "spec_key_version", "spec_format"):
            fix.pop(key, None)
    elif case == "credential_swapped":
        fix["forge_credential"] = USER_SLOT
    elif case == "repository_swapped":
        fix["repository_url"] = "https://github.com/saga-xyz/other"
    elif case == "foreign_key":
        LocalSpecSigner().sign_document(fix, fix["id"])
    elif case == "unknown_format":
        fix["spec_format"] = max(specsign.SPEC_FORMATS) + 1
    elif case == "signature_garbled":
        fix["spec_signature"] = base64.b64encode(b"\x00" * 70).decode("ascii")


@pytest.mark.parametrize("case", [
    "unsigned", "access_cleared", "credential_swapped", "repository_swapped",
    "foreign_key", "unknown_format", "signature_garbled",
])
def test_a_spec_that_does_not_verify_declines_before_any_credential_is_read(
    db, objects, client, github, waker, store, contract, case
):
    """A tenant's agents can write the step's document; the push credential
    it names is trusted only as swarm-api signed it.

    The step is signed read-only. Every rewrite clears `forge_access`, which
    alone would buy a push with the tenant's `-git` token; `credential_swapped`
    also points `forge_credential` at another member's slot, whose grant and
    token are both in place. `test_the_control_a_signed_rewrite_publishes`
    shows the same document, signed, does publish, so each decline here is
    the signature check's.
    """
    fix = _workflow(db, objects)
    fix["forge_access"] = "read"
    _sign(fix)
    _grant(db)
    store.put(gittokens.secret_name_for("eng", USER_SLOT), "ghp_" + secrets.token_hex(18))
    _rewrite(fix, case)

    body = _push(client).json()

    _declined_unread(body, db, github, store)
    marker = _fix(db)["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]
    assert "spec" in marker["reason"], marker
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]


def test_the_control_a_signed_rewrite_publishes(db, objects, client, github, store, contract):
    """The control for the test above: the same write-capable document,
    signed as swarm-api signs it, publishes, so a decline there is the
    signature and not the rewrite's content."""
    fix, token = _user_slot_step(db, objects, store)
    fix["forge_access"] = None
    _sign(fix)

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    assert store.asked == [gittokens.secret_name_for("eng", USER_SLOT)]
    assert all(token in value for value in _authorisations(github))


def test_the_spec_is_verified_by_the_id_it_was_read_by(db, objects, client, github, contract):
    """A rewritten `id` field names neither the signed spec nor the branch."""
    fix = _workflow(db, objects)
    fix["id"] = "task_other00001"

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    assert f"swarm/{FIX}" in github.branches and "swarm/task_other00001" not in github.branches


@pytest.mark.parametrize("keys,signing", [
    ("", KEY_VERSION),
    ("not json", KEY_VERSION),
    ("[]", KEY_VERSION),
    (None, ""),
    (None, KEY_VERSION.replace("step-spec", "other-key")),
])
def test_no_trusted_key_verifies_nothing(db, objects, keys, signing):
    """Without SPEC_VERIFY_KEYS, or a version outside the signing key, nothing
    verifies, so nothing is pushed. The control: the configured pair verifies."""
    fix = _workflow(db, objects)
    good = json.dumps({KEY_VERSION: SIGNER.public_pem()})
    trusted = SimpleNamespace(spec_verify_keys=good, spec_signing_key_version=KEY_VERSION)
    assert verdictpublish.spec_refusal(trusted, fix, FIX) is None
    settings = SimpleNamespace(spec_verify_keys=good if keys is None else keys,
                               spec_signing_key_version=signing)
    assert verdictpublish.spec_refusal(settings, fix, FIX) is not None


@pytest.mark.parametrize("change", ["removed", "read", "other_tenant"])
def test_a_user_slots_grant_is_read_again_and_must_still_be_write(
    db, objects, client, github, waker, store, contract, change
):
    """agent_worker.secrets.grant_refusal(write=True), at the control-plane
    push: a revoked or downgraded grant pushes nothing with that person's
    token, which is never even read."""
    _user_slot_step(db, objects, store)
    if change == "removed":
        del db.docs[_grant(db)]
    elif change == "read":
        _grant(db, mode="read")
    else:
        _grant(db, tenant_id="research")

    body = _push(client).json()

    _declined_unread(body, db, github, store)
    reason = _fix(db)["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY]["reason"]
    assert "grant" in reason, reason
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]


def test_a_user_slot_with_its_grant_in_place_publishes(db, objects, client, github, store,
                                                       contract):
    """The control for the grant test: the same step with the grant unchanged."""
    _, token = _user_slot_step(db, objects, store)

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    assert all(token in value for value in _authorisations(github))


def test_a_repository_or_tenant_slot_reads_no_grant():
    """A repository or tenant token is not a person's: no grant is read (db is None)."""
    ref = verdictpublish._repository(REPO_URL)
    assert verdictpublish.grant_refusal(None, "eng", REPO_SLOT, ref) is None
    assert verdictpublish.grant_refusal(None, "eng", forge.GIT_PROVIDER, ref) is None


def test_a_parent_that_has_not_succeeded_leaves_the_step_alone(
    db, objects, client, github, waker, contract
):
    _workflow(db, objects)
    db.docs[f"tasks/{IMPL}"]["state"] = "RUNNING"
    body = _push(client).json()
    assert body["report"]["considered"] == 0
    assert verdictpublish.CONTROL_PUBLISH_METADATA_KEY not in _fix(db)["metadata"]
    assert github.calls == [] and waker.rung == []


# --------------------------------------------------------------------------
# 5. once, in its own tenant, adopting what is there
# --------------------------------------------------------------------------

def test_a_second_delivery_publishes_once(db, objects, client, github, waker, contract):
    _workflow(db, objects)
    _push(client)
    again = _push(client).json()
    assert again["report"]["published"] == [] and again["report"]["declined"] == []
    assert len(github.pulls) == 1
    assert [w for w in _writes(github) if w[0] == "POST"] == [
        ("POST", "/repos/saga-xyz/widgets/git/refs"),
        ("POST", "/repos/saga-xyz/widgets/pulls"),
    ]
    assert len(waker.rung) == 1


def test_another_tenants_wake_is_a_no_op(db, objects, client, github, waker, contract):
    _workflow(db, objects)
    body = _push(client, tenant_id="research").json()
    assert body["report"]["skipped"] == "tenant_mismatch"
    assert github.calls == [] and _fix(db)["state"] == "PARKED"


def test_only_task_finished_is_handled(db, objects, client, github, contract):
    _workflow(db, objects)
    body = _push(client, reason="task_submitted").json()
    assert body == {"handled": False, "reason": "task_submitted"}
    assert github.calls == []


def test_an_open_pull_request_from_the_branch_is_adopted(db, objects, client, github, contract):
    _workflow(db, objects)
    github.branches[f"swarm/{FIX}"] = HEAD
    github.open_pull(77, HEAD, ref=f"swarm/{FIX}")
    _push(client)
    summary = _fix(db)["result_summary"]["git"]
    assert summary["pull_request"]["number"] == 77
    assert summary["pull_request"]["created"] is False
    assert len(github.pulls) == 1


def test_the_route_is_the_sweepers(db, objects, client, contract):
    _workflow(db, objects)
    response = client.post("/v1/admin/tasks/finished", headers=auth_header("alice"),
                           json={"message": {"attributes": {"reason": "task_finished",
                                                            "task_id": REVIEW}}})
    assert response.status_code == 403, response.text


# --------------------------------------------------------------------------
# 6. the scheduler's hold
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def allow_in_scheduler(monkeypatch):
    from scheduler import loop

    monkeypatch.setattr(loop, "can_transition", lambda frm, to: True)


def _held_step(db, *, ended_ago: float, marker: dict | None = None) -> None:
    seed_tenant(db, "eng", max_active=10)
    for parent in (IMPL, REVIEW):
        doc = seed_task(db, task_id=parent, tenant_id="eng", state="SUCCEEDED",
                        workflow_id=WORKFLOW)
        doc["completed_at"] = NOW - timedelta(seconds=ended_ago)
    fix = seed_task(db, task_id=FIX, tenant_id="eng", state="PARKED", workflow_id=WORKFLOW,
                    park_reason=ParkReason.DEPENDENCY_INCOMPLETE.value, depends_on=(IMPL, REVIEW))
    fix["metadata"] = {"dispatch": {"strategy": "integrate", "role": "integrator",
                                    "integrates": [IMPL], "builds_on": IMPL,
                                    "verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]}}}
    if marker is not None:
        fix["metadata"][verdictpublish.CONTROL_PUBLISH_METADATA_KEY] = marker


def _release(make_scheduler, *, hold: float = 60.0):
    scheduler = make_scheduler(settings=scheduler_settings(control_publish_hold_seconds=hold),
                               now=lambda: NOW)
    return scheduler.release_dependants(REVIEW)


@pytest.mark.parametrize("ended_ago,marker,held", [
    (5, None, True),
    (61, None, False),
    (5, {"state": "claimed", "claimed_at": NOW - timedelta(seconds=10)}, True),
    (400, {"state": "claimed", "claimed_at": NOW - timedelta(seconds=301)}, False),
    (5, {"state": "claimed", "claimed_at": NOW + timedelta(seconds=30)}, False),
    (5, {"state": "declined", "code": "agent_runs"}, False),
    (5, "not a marker", False),
])
def test_the_scheduler_holds_the_step_only_while_swarm_api_may_publish_it(
    db, make_scheduler, dispatcher, allow_in_scheduler, ended_ago, marker, held
):
    _held_step(db, ended_ago=ended_ago, marker=marker)
    report = _release(make_scheduler)
    if held:
        assert report.held_dependencies == 1, report.to_dict()
        assert db.docs[f"tasks/{FIX}"]["state"] == "PARKED"
        assert dispatcher.dispatched == []
    else:
        assert report.held_dependencies == 0, report.to_dict()
        assert db.docs[f"tasks/{FIX}"]["state"] != "PARKED"


def test_no_hold_while_the_setting_is_zero(db, make_scheduler, dispatcher, allow_in_scheduler):
    _held_step(db, ended_ago=1)
    report = _release(make_scheduler, hold=0)
    assert report.held_dependencies == 0
    assert db.docs[f"tasks/{FIX}"]["state"] != "PARKED"


def test_no_hold_while_the_contract_has_no_parked_to_succeeded(db, make_scheduler, dispatcher,
                                                               monkeypatch):
    from scheduler import loop

    monkeypatch.setattr(loop, "can_transition", lambda frm, to: False)
    _held_step(db, ended_ago=1)
    report = _release(make_scheduler)
    assert report.held_dependencies == 0
    assert db.docs[f"tasks/{FIX}"]["state"] != "PARKED"


def test_a_step_that_is_not_a_gated_integrator_is_never_held(db, make_scheduler, dispatcher,
                                                             allow_in_scheduler):
    _held_step(db, ended_ago=1)
    db.docs[f"tasks/{FIX}"]["metadata"]["dispatch"]["role"] = "contributor"
    report = _release(make_scheduler)
    assert report.held_dependencies == 0
    assert db.docs[f"tasks/{FIX}"]["state"] != "PARKED"


def test_the_fake_github_routes_are_the_ones_the_writer_calls(github):
    """The fake's new routes answer at the paths `GitHubWriter` builds."""
    writer = forgewrite.GitHubWriter(send=github, locate=github.locate, fetch=github.fetch)
    ref = forgewrite.IssueRef(owner="saga-xyz", repo="widgets", number=0)
    token = "t" + secrets.token_hex(8)
    assert writer.default_branch(ref, token) == "main"
    assert writer.branch_head(ref, f"swarm/{IMPL}", token) == HEAD
    assert writer.branch_head(ref, "swarm/nothing", token) is None
    assert writer.create_branch(ref, "swarm/new", HEAD, token) is True
    assert writer.create_branch(ref, "swarm/new", HEAD, token) is False
    with pytest.raises(forgewrite.ForgeWriteError):
        writer.create_branch(ref, "swarm/new", "e" * 40, token)
    paths = {urlparse(url).path for _, url, _, _ in github.calls}
    assert "/repos/saga-xyz/widgets" in paths


# --------------------------------------------------------------------------
# 8. contract request 52 applied: the frozen state machine itself, no stand-in
# --------------------------------------------------------------------------
#
# Every test above that publishes swaps `contract_allows` (or the scheduler's
# `can_transition`) for a stand-in, because the code shipped before the owner
# accepted request 52 on 2026-10-09. These read the real `swarm_common.states`,
# so they hold what main now does.


def test_the_frozen_contract_now_switches_the_publish_on():
    assert can_transition(TaskState.PARKED, TaskState.SUCCEEDED) is True
    assert verdictpublish.contract_allows() is True


def test_merge_with_one_contributor_is_published_by_the_control_plane_and_never_leased(
    db, objects, client, github, waker
):
    _workflow(db, objects)

    body = _push(client).json()

    assert body["report"]["published"] == [FIX], body
    assert len(github.pulls) == 1
    fix = _fix(db)
    assert fix["state"] == TaskState.SUCCEEDED.value
    assert fix["result_summary"]["published_by"] == "control_plane"
    assert fix["result_summary"]["verdict_gate"]["agent_ran"] is False
    # Never leased (invariants 1 to 3): no lease, no attempt, no pool touched.
    assert fix["current_lease_id"] is None
    assert fix["attempt_count"] == 0
    assert not [key for key in db.docs if key.startswith("leases/")]
    assert not [key for key in db.docs if key.startswith(f"tasks/{FIX}/attempts/")]


@pytest.mark.parametrize("case,code", [
    ("not_yet", "agent_runs"),
    ("two_contributors", "contributors"),
    ("credential_in_body", "credential"),
    ("task_id_title", "title_unusable"),
])
def test_every_other_case_still_takes_the_worker_path(db, objects, client, github, waker,
                                                      case, code):
    kwargs: dict[str, Any] = {
        "not_yet": {"verdict": {"verdict": "NOT_YET", "findings": ["fix it"]}},
        "two_contributors": {"integrates": [IMPL, "task_other00001"]},
        "credential_in_body": {"body": f"token: {_credential()}"},
        "task_id_title": {"title": f"Finish {IMPL}"},
    }[case]
    _workflow(db, objects, **kwargs)

    body = _push(client).json()

    assert body["report"]["published"] == [], body
    assert body["report"]["declined"] == [{"task_id": FIX, "code": code}], body
    fix = _fix(db)
    assert fix["state"] == TaskState.PARKED.value
    assert "published_by" not in (fix.get("result_summary") or {})
    assert github.pulls == {}
    # The parent's wake: the scheduler promotes the step to a worker at once.
    assert waker.rung == [("task_finished",
                           {"task_id": REVIEW, "tenant_id": "eng", "state": "SUCCEEDED"})]


@pytest.mark.parametrize("claim_age,held", [
    (10, True),
    (299, True),
    (301, False),
])
def test_the_scheduler_leaves_a_claimed_step_until_the_claim_times_out(
    db, make_scheduler, dispatcher, claim_age, held
):
    from scheduler import loop

    assert loop.CONTROL_PUBLISH_CLAIM_SECONDS == 300
    _held_step(db, ended_ago=400,
               marker={"state": "claimed", "claimed_at": NOW - timedelta(seconds=claim_age)})

    report = _release(make_scheduler)

    if held:
        assert report.held_dependencies == 1, report.to_dict()
        assert db.docs[f"tasks/{FIX}"]["state"] == "PARKED"
        assert dispatcher.dispatched == []
    else:
        assert report.held_dependencies == 0, report.to_dict()
        assert db.docs[f"tasks/{FIX}"]["state"] != "PARKED"
