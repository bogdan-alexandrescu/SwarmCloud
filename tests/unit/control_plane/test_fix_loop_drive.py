"""swarm-api drives a `fix-loop` step's rounds on `task_finished` (FL-DRIVE, part of #2509).

docs/design/fix-loop.md §4.2-§4.5, §4.7, §4.8 and the owner's answers in §9
(2026-10-27). A `fix-loop` step is a control-plane step: the scheduler parks
it FIX_ROUNDS_RUNNING and never leases it (FL-SCHED). swarm-api's
`fixloop.on_task_finished`, called from the `task_finished` push beside
`verdictpublish.on_task_finished`, reads the loop's signed block and the
round that ended, and claims the next action under a transaction guarded on
how many rounds it saw. What is held here, one test per row of §4.4's table:

  * the first review's MERGE ends the loop SUCCEEDED with no round (round 0);
  * under `integrate` the loop's first action is `re-review:1`, a one-step
    `collect` workflow pinned to the 40-hex head the fix pushed (the §10
    measurement: submission accepts the full sha as the ref), with the lane's
    own changed paths signed into its dispatch (`rendered_review_paths`);
  * under `direct-pr` the first action is `review-fix:1`, a `continues_task`
    continuation of the implementer, merge off, no closing keyword, the
    blocker and major findings fenced by a nonce and the minors left out;
  * a re-review's MERGE ends the loop SUCCEEDED naming the final review and
    head; NOT_YET with rounds left submits the next fix; NOT_YET at
    `max_rounds` parks HUMAN_REQUIRED `fix_loop_exhausted`;
  * a fix that pushed nothing parks `fix_changed_nothing`, a failed round
    `round_failed`, an unreadable verdict `review_unreadable`, a submitter who
    left `round_refused`;
  * two wakes submit one round; `max_rounds` is read from the signed block and
    never from the tenant-writable state; a round of another loop is never
    read as this loop's;
  * every round is appended to the loop's workflow as `review-fix:<k>` /
    `re-review:<k>`, the id the sc plugin's `followedStepId` strips to the
    round workflow's own step id;
  * the per-tenant tick sweeps a PARKED loop whose last round ended over 5
    minutes ago, and leaves a younger one to its push.

No credentials, no network, no emulator: every agent step is `mock`, and
every token is built at runtime.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import cifix, fixloop, forgewrite
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.validation import FIX_LOOP_PROFILE
from swarm_api.waker import NullWaker
from swarm_common.models import EndCause
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason

from . import forge_fakes
from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .spec_signer import KEY_VERSION, LocalSpecSigner

SWEEPER = "swarm-rollup-sweeper@example-project.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}
BUCKET = f"swarm-artifacts-{PROJECT}"
REPO = "https://github.com/example-org/widgets"
NUMBER = 41
PR_URL = f"https://github.com/example-org/widgets/pull/{NUMBER}"
IMPL_HEAD = "1" * 40
FIX_HEAD = "2" * 40
ROUND_HEAD = "3" * 40
NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
SIGNER = LocalSpecSigner()
#: `plugin/workflows/run.js` `followedStepId`, restated: a trailing `:<digits>`
#: is stripped, so a row follows the round workflow's own step.
_FOLLOWED = re.compile(r"^(.+):([0-9]+)$")


def followed_step_id(step_id: str) -> str:
    matched = _FOLLOWED.match(step_id)
    return matched.group(1) if matched else step_id


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def loop_enabled(monkeypatch):
    """`fix-loop`, available, as it is since contract request 151."""
    monkeypatch.setitem(RUNNER_PROFILES, FIX_LOOP_PROFILE,
                        replace(RUNNER_PROFILES[FIX_LOOP_PROFILE], available=True))


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)


@pytest.fixture
def github() -> forge_fakes.GitHubWrites:
    gh = forge_fakes.GitHubWrites()
    gh.open_pull(NUMBER, FIX_HEAD, ref="swarm/x")
    return gh


@pytest.fixture
def loop_client(db, tokens, group_map, objects, github, clock, loop_enabled) -> TestClient:
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
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge_writer=forgewrite.GitHubWriter(send=github, locate=github.locate, fetch=github.fetch),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


# --------------------------------------------------------------------------
# Helpers: a submitted loop, and its agents ending
# --------------------------------------------------------------------------

def integrate_spec(max_rounds: int | None = None, **block: Any) -> dict[str, Any]:
    loop_block: dict[str, Any] = {"review": "review", "fix": "fix", **block}
    if max_rounds is not None:
        loop_block["max_rounds"] = max_rounds
    return {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review the change; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
            {"step_id": "loop", "runner_profile": FIX_LOOP_PROFILE, "depends_on": ["fix"],
             "fix_loop": loop_block},
        ],
    }


def direct_spec(max_rounds: int | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"review": "review"}
    if max_rounds is not None:
        block["max_rounds"] = max_rounds
    return {
        "strategy": "direct-pr",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "allow_empty_diff": True,
             "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review the change; write verdict.json"}},
            {"step_id": "loop", "runner_profile": FIX_LOOP_PROFILE, "depends_on": ["review"],
             "fix_loop": block},
        ],
    }


class Loop:
    """One submitted loop workflow: its step task ids and its workflow id."""

    def __init__(self, client: TestClient, db, spec: dict[str, Any]) -> None:
        response = client.post("/v1/workflows", headers=auth_header("alice"), json=spec)
        assert response.status_code == 201, response.text
        body = response.json()["workflow"]
        self.db = db
        self.workflow_id = body["workflow_id"]
        self.tasks = {s["step_id"]: s["task_id"] for s in body["steps"]}
        self.loop = self.tasks["loop"]

    def doc(self, step: str | None = None, task_id: str | None = None) -> dict[str, Any]:
        return self.db.docs[f"tasks/{task_id or self.tasks[step or 'loop']}"]

    def workflow(self) -> dict[str, Any]:
        return self.db.docs[f"workflows/{self.workflow_id}"]

    def round_steps(self) -> dict[str, dict[str, Any]]:
        return {s["step_id"]: s for s in self.workflow()["steps"] if ":" in s["step_id"]}

    def state(self) -> dict[str, Any]:
        return self.doc()["metadata"].get(fixloop.STATE_KEY) or {}


def park_running(loop: Loop) -> None:
    """What FL-SCHED writes: the loop agent parked, holding nothing."""
    doc = loop.doc()
    doc.update(state="PARKED", park_reason=ParkReason.FIX_ROUNDS_RUNNING.value,
               next_eligible_at=None)


def finish(db, objects, task_id: str, *, state: str = "SUCCEEDED",
           verdict: Any = None, git: dict[str, Any] | None = None,
           completed_at: datetime | None = None, end_cause: str | None = None,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = db.docs[f"tasks/{task_id}"]
    tenant = doc["tenant_id"]
    entries = []
    if verdict is not None:
        text = verdict if isinstance(verdict, str) else json.dumps(verdict)
        key = f"tenants/{tenant}/tasks/{task_id}/attempts/att_1/artifacts/verdict.json"
        objects.put(key, text.encode("utf-8"))
        entries.append({"name": "verdict.json", "bytes": len(text.encode("utf-8")),
                        "uri": f"gs://{BUCKET}/{key}"})
    summary: dict[str, Any] = {"artifacts": entries, "logs": {}}
    if git is not None:
        summary["git"] = git
    summary.update(extra or {})
    doc.update(state=state, result_summary=summary,
               completed_at=completed_at or NOW - timedelta(minutes=1),
               end_cause=end_cause)
    return doc


def pushed(task_id: str, head: str, files: list[str] | None = None) -> dict[str, Any]:
    return {"branch": f"swarm/{task_id}", "pushed_head": head,
            "pull_request": {"number": NUMBER, "url": PR_URL},
            "own_files": files if files is not None else ["apps/swarm-api/x.py"]}


NOT_YET = {"verdict": "NOT_YET", "findings": [
    {"summary": "the cache never expires entries", "severity": "blocker"},
    {"summary": "the expiry test asserts nothing", "severity": "major"},
    {"summary": "a docstring typo", "severity": "minor"},
]}
MERGE = {"verdict": "MERGE", "findings": []}


def push(client: TestClient, task_id: str, *, tenant_id: str = "eng"):
    fields = {"reason": "task_finished", "task_id": task_id, "tenant_id": tenant_id}
    message = {"messageId": "1",
               "data": base64.b64encode(json.dumps(fields).encode()).decode()}
    response = client.post("/v1/admin/tasks/finished", headers=SWEEPER_HEADERS,
                           json={"message": message, "subscription": "projects/p/subscriptions/s"})
    assert response.status_code == 200, response.text
    return response.json()


def integrate_to_first_round(client, db, objects, *, max_rounds: int | None = None,
                             review: Any = NOT_YET) -> Loop:
    """implement, review NOT_YET, fix pushed FIX_HEAD: the loop's first wake."""
    loop = Loop(client, db, integrate_spec(max_rounds))
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD, ["apps/swarm-ui/src/App.tsx"]))
    finish(db, objects, loop.tasks["review"], verdict=review)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD,
                                                       ["apps/swarm-api/cache.py"]))
    park_running(loop)
    push(client, loop.tasks["fix"])
    return loop


def round_task(loop: Loop, step_id: str) -> str:
    return loop.round_steps()[step_id]["task_id"]


# --------------------------------------------------------------------------
# Round 0: the first review
# --------------------------------------------------------------------------

def test_the_first_reviews_merge_ends_the_loop_with_no_round(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects, review=MERGE)

    doc = loop.doc()
    # MUTATION: start re-review:1 regardless and a MERGE costs a pod.
    assert doc["state"] == "SUCCEEDED", doc["state"]
    assert loop.round_steps() == {}
    summary = doc["result_summary"]["fix_loop"]
    assert summary["rounds"] == [] and summary["verdict"] == "MERGE"
    assert summary["final_review"] == loop.tasks["review"]


def test_a_loop_not_yet_parked_by_the_scheduler_is_left_to_the_sweep(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))

    push(loop_client, loop.tasks["fix"])

    assert loop.doc()["state"] not in ("SUCCEEDED", "FAILED")
    assert loop.round_steps() == {}


# --------------------------------------------------------------------------
# integrate: re-review:1 first
# --------------------------------------------------------------------------

def test_under_integrate_the_first_action_is_re_review_1_at_the_fixs_head(
        loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)

    steps = loop.round_steps()
    assert list(steps) == ["re-review:1"], steps
    review = loop.doc(task_id=steps["re-review:1"]["task_id"])
    # The §10 measurement: a collect step at the full 40-hex head is accepted.
    assert review["repository_ref"] == FIX_HEAD
    assert review["metadata"]["dispatch"]["strategy"] == "collect"
    assert review["runner_profile"] == "mock"
    assert review["submitted_by"] == "alice@example.com"
    signed = review["metadata"]["dispatch"]["fix_loop_round"]
    assert signed["loop_task"] == loop.loop and signed["round"] == 1
    assert signed["kind"] == "review" and signed["head"] == FIX_HEAD
    assert signed["pull_request"] == NUMBER
    # The lane's own changed paths, signed: the implementer's and the fix's.
    assert sorted(signed["rendered_review_paths"]) == [
        "apps/swarm-api/cache.py", "apps/swarm-ui/src/App.tsx"]
    assert SIGNER.verifies(review, steps["re-review:1"]["task_id"])
    prompt = review["input"]["prompt"]
    assert "the cache never expires entries" in prompt
    assert "review the change; write verdict.json" in prompt
    assert "verdict.json" in prompt
    # Round step ids are the plugin's expectation: `:<k>` stripped is the
    # round workflow's own step id.
    own = db.docs[f"workflows/{review['workflow_id']}"]["steps"][0]["step_id"]
    assert followed_step_id("re-review:1") == own == "re-review"
    (entry,) = loop.state()["rounds"]
    assert entry["kind"] == "review" and entry["round"] == 1
    assert entry["task_id"] == steps["re-review:1"]["task_id"]
    # Still parked, holding nothing, while the round runs.
    assert loop.doc()["state"] == "PARKED"
    assert loop.doc()["park_reason"] == ParkReason.FIX_ROUNDS_RUNNING.value


def test_a_re_review_that_says_merge_ends_the_loop_succeeded(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=MERGE, extra={"cost_usd": 1.25})

    push(loop_client, rereview)

    doc = loop.doc()
    assert doc["state"] == "SUCCEEDED", doc
    summary = doc["result_summary"]["fix_loop"]
    assert summary["final_review"] == rereview and summary["final_head"] == FIX_HEAD
    assert summary["verdict"] == "MERGE" and summary["phase"] is None
    assert summary["rounds"][0]["cost_usd"] == 1.25


def test_not_yet_with_rounds_left_submits_review_fix_k_plus_1(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)

    push(loop_client, rereview)

    steps = loop.round_steps()
    assert list(steps) == ["re-review:1", "review-fix:2"], steps
    fix_id = steps["review-fix:2"]["task_id"]
    fix = loop.doc(task_id=fix_id)
    dispatch = fix["metadata"]["dispatch"]
    assert dispatch["strategy"] == "direct-pr"
    assert dispatch["continues"] == loop.tasks["fix"], "a round continues the root"
    assert fix["metadata"]["merge"] == "off"
    assert dispatch["fix_loop_round"] == {
        "loop_task": loop.loop, "round": 2, "kind": "fix", "start_head": FIX_HEAD,
        "pull_request": NUMBER}
    assert fix["metadata"][fixloop.ROUND_METADATA_KEY]["pull_request"] == NUMBER
    prompt = fix["input"]["prompt"]
    assert "review-fix round 2 of at most 3" in prompt
    assert f"#{NUMBER}" in prompt and PR_URL in prompt and FIX_HEAD in prompt
    assert "the cache never expires entries" in prompt
    assert "the expiry test asserts nothing" in prompt
    # Minors stay out of the fix: they go to the wave epic (Q7).
    assert "a docstring typo" not in prompt
    assert "Closes" in prompt and "closing keyword" in prompt.lower()
    # The findings are fenced by a nonce line that opens and closes them.
    fences = re.findall(r"^=== REVIEW FINDINGS ([0-9a-f]{16}) ===$", prompt, re.M)
    assert len(fences) == 2 and fences[0] == fences[1]
    # One line per earlier round.
    assert "round 1:" in prompt and "NOT_YET" in prompt
    own = db.docs[f"workflows/{fix['workflow_id']}"]["steps"][0]["step_id"]
    assert followed_step_id("review-fix:2") == own == "review-fix"


def test_review_fix_kind_is_a_pull_request_writer_round():
    # The round is the pull request's one live writer while it runs.
    assert fixloop.ROUND_METADATA_KEY in cifix.ROUND_KINDS
    assert fixloop.ROUND_METADATA_KEY in cifix.PR_WRITER_KINDS


def _to_review_fix_2(client, db, objects, **kw) -> tuple[Loop, str]:
    loop = integrate_to_first_round(client, db, objects, **kw)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)
    push(client, rereview)
    return loop, round_task(loop, "review-fix:2")


def test_a_fix_that_pushed_a_new_head_gets_its_re_review(loop_client, db, objects):
    loop, fix = _to_review_fix_2(loop_client, db, objects)
    finish(db, objects, fix, git=pushed(loop.tasks["fix"], ROUND_HEAD, ["apps/swarm-api/ttl.py"]))

    push(loop_client, fix)

    steps = loop.round_steps()
    assert list(steps) == ["re-review:1", "review-fix:2", "re-review:2"], steps
    review = loop.doc(task_id=steps["re-review:2"]["task_id"])
    assert review["repository_ref"] == ROUND_HEAD
    assert "apps/swarm-api/ttl.py" in review["metadata"]["dispatch"]["fix_loop_round"][
        "rendered_review_paths"]


def test_a_fix_that_pushed_nothing_parks_fix_changed_nothing(loop_client, db, objects):
    loop, fix = _to_review_fix_2(loop_client, db, objects)
    finish(db, objects, fix, git=pushed(loop.tasks["fix"], FIX_HEAD))

    push(loop_client, fix)

    doc = loop.doc()
    assert doc["state"] == "PARKED" and doc["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert fixloop.park_phase_of(doc) == "fix_changed_nothing"
    assert "re-review:2" not in loop.round_steps()


def test_not_yet_at_max_rounds_parks_fix_loop_exhausted(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects, max_rounds=1)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)

    push(loop_client, rereview)

    doc = loop.doc()
    assert doc["state"] == "PARKED" and doc["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert fixloop.park_phase_of(doc) == "fix_loop_exhausted"
    assert doc["next_eligible_at"] == NOW, "the park's instant, which the 7-day expiry reads"
    summary = doc["result_summary"]["fix_loop"]
    assert summary["phase"] == "fix_loop_exhausted" and summary["verdict"] == "NOT_YET"
    assert summary["findings_count"] == 2
    assert list(loop.round_steps()) == ["re-review:1"]


@pytest.mark.parametrize("state,cause", [("FAILED", "agent_error"), ("CANCELLED", None),
                                         ("DEAD_LETTERED", "retries_exhausted")])
def test_a_round_that_did_not_succeed_parks_round_failed(loop_client, db, objects, state, cause):
    loop, fix = _to_review_fix_2(loop_client, db, objects)
    finish(db, objects, fix, state=state, end_cause=cause)

    push(loop_client, fix)

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) == "round_failed"
    detail = fixloop.park_detail_of(doc)
    assert detail["task_id"] == fix and detail["state"] == state
    assert detail["end_cause"] == cause


@pytest.mark.parametrize("verdict", ["not json", json.dumps({"verdict": "MAYBE"}),
                                     json.dumps(["MERGE"]), None])
def test_an_unreadable_re_review_parks_review_unreadable(loop_client, db, objects, verdict):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=verdict)

    push(loop_client, rereview)

    doc = loop.doc()
    # Never MERGE (it would merge unreviewed work), never NOT_YET.
    assert doc["state"] == "PARKED"
    assert fixloop.park_phase_of(doc) == "review_unreadable"
    assert list(loop.round_steps()) == ["re-review:1"]


def test_a_submitter_who_left_parks_round_refused(loop_client, db, objects, monkeypatch):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    # alice leaves eng after submitting (`submitted_by` is under the signature,
    # so the directory is what changes, as it does in production).
    groups = loop_client.app.state.ctx.authenticator._groups
    monkeypatch.setitem(groups._mapping, "alice@example.com", ())

    push(loop_client, loop.tasks["fix"])

    doc = loop.doc()
    assert fixloop.park_phase_of(doc) == "round_refused"
    assert loop.round_steps() == {}
    (entry,) = loop.state()["rounds"]
    assert "no longer a member" in entry["error"]


def test_two_wakes_submit_one_round(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)

    push(loop_client, loop.tasks["fix"])
    push(loop_client, loop.tasks["review"])

    assert list(loop.round_steps()) == ["re-review:1"]
    collects = [k for k, w in db.docs.items() if k.startswith("workflows/")
                and any(s.get("step_id") == "re-review" for s in w.get("steps") or [])]
    assert len(collects) == 1, collects


def test_the_claim_guard_refuses_a_stale_count(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    # A second driver that read the loop before the first claim.
    assert fixloop.claim_round(db, "eng", loop.loop, seen=0, entry={"kind": "review"},
                               now=NOW) is None
    assert len(loop.state()["rounds"]) == 1


def test_max_rounds_is_read_from_the_signed_block_not_the_state(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec(max_rounds=1))
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    # The tenant can write the state: it asks for five.
    loop.doc()["metadata"][fixloop.STATE_KEY] = {"max_rounds": 5, "rounds": []}
    push(loop_client, loop.tasks["fix"])
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)

    push(loop_client, rereview)

    assert fixloop.park_phase_of(loop.doc()) == "fix_loop_exhausted"
    assert "review-fix:2" not in loop.round_steps()


def test_a_rewritten_signed_block_drives_nothing(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec(max_rounds=1))
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)
    loop.doc()["metadata"]["dispatch"]["fix_loop"]["max_rounds"] = 5

    push(loop_client, loop.tasks["fix"])

    assert loop.round_steps() == {}
    assert loop.doc()["park_reason"] == ParkReason.FIX_ROUNDS_RUNNING.value


def test_a_review_of_another_loop_is_not_read_as_this_loops(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=MERGE)
    # Its unsigned copy says another loop; the signed one still says this one,
    # and only the signed one is read.
    loop.doc(task_id=rereview)["metadata"][fixloop.ROUND_METADATA_KEY] = {
        "loop_task": "task_" + "9" * 20}
    push(loop_client, rereview)
    assert loop.doc()["state"] == "SUCCEEDED"


def test_a_round_whose_signed_record_names_another_loop_is_refused(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=MERGE)
    loop.doc(task_id=rereview)["metadata"]["dispatch"]["fix_loop_round"]["loop_task"] = (
        "task_" + "9" * 20)

    push(loop_client, rereview)

    doc = loop.doc()
    assert doc["state"] == "PARKED", "a forged round never ends the loop SUCCEEDED"
    assert fixloop.park_phase_of(doc) == "review_unreadable"


# --------------------------------------------------------------------------
# direct-pr: review-fix:1 first
# --------------------------------------------------------------------------

def test_under_direct_pr_the_first_action_is_review_fix_1(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET,
           extra={"no_change": True})
    park_running(loop)

    push(loop_client, loop.tasks["review"])

    steps = loop.round_steps()
    assert list(steps) == ["review-fix:1"], steps
    fix = loop.doc(task_id=steps["review-fix:1"]["task_id"])
    assert fix["metadata"]["dispatch"]["continues"] == loop.tasks["implement"]
    assert fix["metadata"]["dispatch"]["fix_loop_round"]["start_head"] == IMPL_HEAD
    assert "review-fix round 1 of at most 3" in fix["input"]["prompt"]


def test_under_direct_pr_a_first_merge_ends_the_loop_at_once(loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"], verdict=MERGE)
    park_running(loop)

    push(loop_client, loop.tasks["review"])

    assert loop.doc()["state"] == "SUCCEEDED"
    assert loop.doc()["result_summary"]["fix_loop"]["final_head"] == IMPL_HEAD
    assert loop.round_steps() == {}


# --------------------------------------------------------------------------
# The pull request ended outside the workflow
# --------------------------------------------------------------------------

def test_a_pull_request_merged_outside_ends_the_loop_succeeded(loop_client, db, objects, github):
    loop, _ = _to_review_fix_2(loop_client, db, objects)
    fix = round_task(loop, "review-fix:2")
    finish(db, objects, fix, git=pushed(loop.tasks["fix"], ROUND_HEAD))
    github.pulls[NUMBER].update(state="closed", merged=True)

    push(loop_client, fix)

    doc = loop.doc()
    assert doc["state"] == "SUCCEEDED"
    assert doc["result_summary"]["fix_loop"]["phase"] == "merged_outside"
    assert "re-review:2" not in loop.round_steps()


def test_a_pull_request_closed_outside_cancels_the_loop(loop_client, db, objects, github):
    loop, _ = _to_review_fix_2(loop_client, db, objects)
    fix = round_task(loop, "review-fix:2")
    finish(db, objects, fix, git=pushed(loop.tasks["fix"], ROUND_HEAD))
    github.pulls[NUMBER].update(state="closed", merged=False)

    push(loop_client, fix)

    assert loop.doc()["state"] == "CANCELLED"


def test_a_head_a_person_pushed_is_the_next_rounds_start(loop_client, db, objects, github):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)
    moved = "4" * 40
    github.pulls[NUMBER]["head"]["sha"] = moved

    push(loop_client, rereview)

    fix = loop.doc(task_id=round_task(loop, "review-fix:2"))
    assert fix["metadata"]["dispatch"]["fix_loop_round"]["start_head"] == moved


# --------------------------------------------------------------------------
# The sweep, in the per-tenant tick
# --------------------------------------------------------------------------

def _tick(client: TestClient, tenant_id: str = "eng"):
    response = client.post(f"/v1/admin/merges/wake?tenant_id={tenant_id}",
                           headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_sweep_drives_a_loop_whose_last_round_ended_over_5_minutes_ago(
        loop_client, db, objects):
    # Every parent ended over 5 minutes ago: the newest end is what the sweep reads.
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET,
           completed_at=NOW - timedelta(seconds=fixloop.SWEEP_AFTER_SECONDS + 2))
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD),
           completed_at=NOW - timedelta(seconds=fixloop.SWEEP_AFTER_SECONDS + 1))
    park_running(loop)

    body = _tick(loop_client)

    assert list(loop.round_steps()) == ["re-review:1"]
    assert body["fix_loop"]["driven"] == 1


def test_the_sweep_leaves_a_round_that_ended_lately_to_its_push(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD),
           completed_at=NOW - timedelta(seconds=60))
    park_running(loop)

    _tick(loop_client)

    assert loop.round_steps() == {}


def test_the_sweep_reads_only_the_named_tenant(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD),
           completed_at=NOW - timedelta(hours=1))
    park_running(loop)

    _tick(loop_client, "research")

    assert loop.round_steps() == {}


def test_a_finished_push_for_another_tenant_drives_nothing(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["review"], verdict=NOT_YET)
    finish(db, objects, loop.tasks["fix"], git=pushed(loop.tasks["fix"], FIX_HEAD))
    park_running(loop)

    push(loop_client, loop.tasks["fix"], tenant_id="research")

    assert loop.round_steps() == {}


def test_an_exhausted_loop_ends_failed_fix_loop_exhausted_through_end(loop_client, db, objects):
    # The end route is test_fix_loop_routes.py's; the ending it writes is here.
    loop = integrate_to_first_round(loop_client, db, objects, max_rounds=1)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)
    push(loop_client, rereview)

    assert fixloop.end_failed(db, "eng", loop.loop, now=NOW, by="alice@example.com")
    doc = loop.doc()
    assert doc["state"] == "FAILED"
    assert doc["end_cause"] == EndCause.FIX_LOOP_EXHAUSTED.value


# --------------------------------------------------------------------------
# Later rounds' minors go to the wave epic (§6.5, the owner's Q7 (a))
# --------------------------------------------------------------------------

EPIC = 77
MINOR = {"severity": "minor", "file": "apps/swarm-api/ttl.py", "where": "expire()",
         "problem": "the TTL is a magic number", "fix": "name it"}
FILED = {"severity": "minor", "file": "apps/swarm-api/ttl.py", "call_site": "renew()",
         "summary": "renew() logs at INFO"}


def test_a_re_reviews_minors_are_filed_on_the_epic_once(loop_client, db, objects, github):
    loop_client.app.state.ctx.store.set_findings_epic("eng", EPIC)
    # A person filed one already, in the epic's shape: it is not posted again.
    github.add_comment(EPIC, "- [ ] **renew() logs at INFO** · `apps/swarm-api/ttl.py` "
                             "`renew()` · found by hand")
    # Round 0's minors are the gated integrator's: the loop never files them.
    review_minor = {**MINOR, "problem": "round zero's minor"}
    loop = integrate_to_first_round(loop_client, db, objects,
                                    review={**NOT_YET, "findings": [*NOT_YET["findings"],
                                                                    review_minor]})
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview,
           verdict={"verdict": "MERGE", "findings": [MINOR, FILED, "a string is no minor"]})

    push(loop_client, rereview)
    push(loop_client, rereview)

    assert loop.doc()["state"] == "SUCCEEDED"
    bodies = [c["body"] for c in github.on_issue(EPIC)]
    assert len(bodies) == 2, bodies
    posted = bodies[1]
    # MUTATION: dedup off posts FILED again; the round-0 minor would show here.
    assert "the TTL is a magic number" in posted and "`expire()`" in posted
    assert f"re-review `{rereview}`" in posted and "suggested fix: name it" in posted
    assert not any("round zero" in b for b in bodies)
    (entry,) = loop.state()["rounds"]
    assert entry["findings_epic"] == {"epic": EPIC, "minors": 2, "filed": 1,
                                      "already_filed": 1}


def test_with_no_epic_a_rounds_minors_are_recorded_not_filed(loop_client, db, objects, github):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict={"verdict": "MERGE", "findings": [MINOR]})

    push(loop_client, rereview)

    assert loop.doc()["state"] == "SUCCEEDED"
    assert github.on_issue(EPIC) == []
    (entry,) = loop.state()["rounds"]
    assert entry["findings_epic"]["not_filed"] == "the workspace names no findings epic"
