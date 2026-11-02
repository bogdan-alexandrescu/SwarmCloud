"""Reviews only where they matter (#2524, BGD 2026-10-27: "Risk paths + over 300 lines").

docs/workflows.md "Reviews only where they matter, and no-op steps refused".
A workflow step carrying `metadata.review_gate` is held PARKED by the
scheduler, as the #748 integrator is, and decided by swarm-api on its
parent's `task_finished` wake (`swarm_api.reviewgate`, BGD 2026-10-28: no
contract change, swarm-api ends it PARKED -> SUCCEEDED). It runs when its
parents' change touches a risk path or more than `min_lines` lines, or
cannot be told; otherwise it ends SUCCEEDED without an agent, and its
verdict.json -- written inline in its manifest, since no attempt ran --
reads MERGE to every gate downstream and through the API's artifact read.

What these tests hold:

  1. The scheduler holds a gated review, and writes no SUCCEEDED itself; a
     small non-risk diff is skipped by swarm-api, with the reason, and no
     lease or pod.
  2. A 3-line diff on a risk path runs; a 301-line diff runs; 300 skips. A
     review that runs is declined and the scheduler then promotes it.
  3. An unreadable or cut change runs.
  4. The skipped review's verdict, staged by the worker as the integrator
     would stage it, reads MERGE: the integrator's NOT_YET gate stays shut;
     swarm-api serves it through the redacted artifact read, so its own
     publish of the integrator opens the pull request without a pod.
  5. swarm-api refuses a malformed gate with a 422 and stores a good one
     (the compiled issue-run review's default: test_review_gate_default.py).
  6. The restatements across images are equal.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker import verdict as verdict_mod
from agent_worker.logs import build_logger
from scheduler import loop
from scheduler import reviewgate as scheduler_gate
from scheduler.store import SchedulerStore
from swarm_api import refusals, reviewgate, validation, verdictpublish

from .conftest import scheduler_settings, seed_pool, seed_task, seed_tenant
# The verdict-publish world, reused whole: its client, fakes and workflow.
from .test_verdict_publish import (  # noqa: F401 -- fixtures are used by name
    FIX,
    HEAD,
    _push,
    _sign,
    _workflow,
    client,
    contract,
    github,
    store,
    waker,
)


IMPL = "task_impl000001"
REVIEW = "task_review0001"
SMALL_FILE = "apps/scheduler/scheduler/loop.py"
RISK_FILE = "apps/agent-worker/agent_worker/secrets.py"


def _git(*paths: str, insertions: int, deletions: int = 0, truncated: bool = False,
         shots: tuple[str, ...] = ()) -> dict[str, Any]:
    summary: dict[str, Any] = {"git": {
        "files": [{"path": p, "status": "M"} for p in paths],
        "files_truncated": truncated,
        "insertions": insertions,
        "deletions": deletions,
    }}
    if shots:
        summary["artifacts"] = [{"name": name, "bytes": 10, "uri": f"gs://b/{name}"}
                                for name in shots]
    return summary


def _parent(db, summary: dict[str, Any] | None, task_id: str = IMPL) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED",
                    runner_profile="claude-code")
    doc["result_summary"] = summary
    doc["completed_at"] = datetime.now(timezone.utc)


def _review(db, gate: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = seed_task(db, task_id=REVIEW, tenant_id="eng", state="PARKED",
                    park_reason="DEPENDENCY_INCOMPLETE", runner_profile="claude-code",
                    provider="anthropic", depends_on=(IMPL,))
    doc["metadata"] = {"review_gate": {"min_lines": 300} if gate is None else gate,
                       "keep": "me"}
    return doc


def _base(db) -> None:
    seed_tenant(db, "eng", max_active=10, credentials=("anthropic",))
    seed_pool(db, "global", hard_limit=10)


def _leases(db) -> list[dict[str, Any]]:
    return [d for k, d in db.docs.items() if k.startswith("leases/")]


def _scheduler(make_scheduler, *, hold: float = 60.0):
    return make_scheduler(settings=scheduler_settings(control_publish_hold_seconds=hold))


def _decide(client) -> dict[str, Any]:
    """The parent's `task_finished` wake, as swarm-api's push subscription receives it."""
    response = _push(client, IMPL)
    assert response.status_code == 200, response.text
    return response.json()["report"]


def _review_doc(db) -> dict[str, Any]:
    return db.docs[f"tasks/{REVIEW}"]


# ---------------------------------------------------------------------------
# 1-3. the hold, and the decision
# ---------------------------------------------------------------------------


def test_the_scheduler_holds_a_gated_review_and_never_ends_it(db, make_scheduler, dispatcher):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12, deletions=3))
    _review(db)

    report = _scheduler(make_scheduler).release_dependants(IMPL)

    # MUTATION: drop the review gate from `held_for_control_publish` and the
    # review is promoted and leased before swarm-api can decide it.
    assert report.held_dependencies == 1, report.to_dict()
    assert _review_doc(db)["state"] == "PARKED"
    assert _leases(db) == [] and dispatcher.dispatched == []
    # BGD 2026-10-28: the scheduler is not a second writer of PARKED -> SUCCEEDED.
    assert not hasattr(SchedulerStore, "succeed_without_worker")
    assert not hasattr(scheduler_gate, "end_if_skipped")


def test_a_small_non_risk_diff_skips_the_review_with_the_reason(
    db, make_scheduler, dispatcher, client, waker
):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12, deletions=3))
    _review(db)

    report = _decide(client)

    doc = _review_doc(db)
    # MUTATION: decline every gated review in `_consider_review` and it stays PARKED.
    assert report["reviews_skipped"] == [REVIEW], report
    assert doc["state"] == "SUCCEEDED" and doc["park_reason"] is None
    assert _leases(db) == [] and dispatcher.dispatched == []
    reason = doc["result_summary"]["review_skipped"]
    assert reason.startswith("review skipped: 15 changed lines")
    (entry,) = doc["result_summary"]["artifacts"]
    assert entry["name"] == "verdict.json" and "uri" not in entry
    assert entry["written_by"] == "control_plane"
    assert json.loads(entry["inline"]) == {"verdict": "MERGE", "findings": [], "skipped": reason}
    assert entry["bytes"] == len(entry["inline"].encode("utf-8"))
    # Not `skipped`: that would make the integrator, which stages from it, skip too.
    assert "skipped" not in doc["result_summary"]
    assert doc["metadata"]["review_gate"]["resolved"]["run"] is False
    assert doc["metadata"]["keep"] == "me"
    assert doc["metadata"]["control_publish"]["state"] == "published"
    events = [d for k, d in db.docs.items() if k.startswith(f"tasks/{REVIEW}/events/")]
    assert [e["type"] for e in events] == ["succeeded"]
    # The review's own wake, so its dependants are released.
    assert ("task_finished", {"task_id": REVIEW, "tenant_id": "eng",
                              "state": "SUCCEEDED"}) in waker.rung


def _runs_after_decline(db, make_scheduler, dispatcher, client, waker) -> None:
    report = _decide(client)
    doc = _review_doc(db)
    assert report["reviews_skipped"] == [], report
    assert doc["state"] == "PARKED"
    assert doc["metadata"]["control_publish"]["state"] == "declined"
    # The parent's wake again: the scheduler resolves the review, now unheld.
    assert ("task_finished", {"task_id": IMPL, "tenant_id": "eng",
                              "state": "SUCCEEDED"}) in waker.rung
    _scheduler(make_scheduler).release_dependants(IMPL)
    assert _review_doc(db)["state"] != "SUCCEEDED"
    assert len(_leases(db)) == 1 and len(dispatcher.dispatched) == 1, _review_doc(db)


def test_a_three_line_diff_on_a_risk_path_runs(db, make_scheduler, dispatcher, client, waker):
    _base(db)
    _parent(db, _git(RISK_FILE, insertions=2, deletions=1))
    _review(db)

    _runs_after_decline(db, make_scheduler, dispatcher, client, waker)
    assert _review_doc(db)["metadata"]["control_publish"]["code"] == "review_runs"


@pytest.mark.parametrize("lines, runs", [(301, True), (300, False)])
def test_more_than_min_lines_runs_and_exactly_min_lines_skips(db, client, lines, runs):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=lines - 100, deletions=100))
    _review(db)

    _decide(client)

    assert (_review_doc(db)["state"] != "SUCCEEDED") is runs


@pytest.mark.parametrize("summary", [
    None,
    {"git": {"error": "could not harvest"}},
    {"git": {"files": [{"path": SMALL_FILE}]}},  # no line counts
    _git(SMALL_FILE, insertions=4, truncated=True),
])
def test_a_change_that_cannot_be_told_runs(db, client, summary):
    _base(db)
    _parent(db, summary)
    _review(db)

    _decide(client)

    assert _review_doc(db)["state"] == "PARKED"


def test_an_unreadable_gate_is_declined_and_runs(db, make_scheduler, dispatcher, client, waker):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=1))
    _review(db, gate={"min_lines": "lots"})

    _runs_after_decline(db, make_scheduler, dispatcher, client, waker)
    assert _review_doc(db)["metadata"]["control_publish"]["code"] == "review_gate_unreadable"


def test_another_tenants_parent_is_not_read(db, client):
    _base(db)
    seed_tenant(db, "research", max_active=10)
    _parent(db, _git(SMALL_FILE, insertions=1))
    db.docs[f"tasks/{IMPL}"]["tenant_id"] = "research"
    _review(db)

    _decide(client)

    assert _review_doc(db)["state"] == "PARKED"


def test_a_chain_sums_its_lines_and_reads_risk_along_builds_on():
    docs = {
        "b": {**_git(SMALL_FILE, insertions=200), "metadata": {"dispatch": {"builds_on": "a"}}},
        "a": _git("terraform/infra/iam.tf", insertions=1),
    }
    docs = {k: {"result_summary": {"git": v["git"]}, "metadata": v.get("metadata")}
            for k, v in docs.items()}
    decision = reviewgate.resolve(["b"], reviewgate.Gate(300, reviewgate.DEFAULT_RISK_PATHS),
                                  docs.get)
    assert decision.run and decision.reason == reviewgate.REASON_RISK
    assert decision.risk_hits == ("terraform/infra/iam.tf",)
    assert decision.changed_lines == 201


@pytest.mark.parametrize("path, risky", [
    ("apps/common/swarm_common/models.py", True),
    ("apps/redaction/swarm_redaction/rules.py", True),
    ("apps/swarm-api/swarm_api/redaction.py", True),
    ("apps/swarm-api/swarm_api/gittokens.py", True),
    ("apps/agent-worker/agent_worker/forgebroker.py", True),
    ("apps/swarm-api/swarm_api/auth.py", True),
    ("terraform/modules/scheduler/iam.tf", True),
    ("terraform/bootstrap/main.tf", True),
    ("scripts/create-secrets.sh", True),
    ("apps/scheduler/scheduler/loop.py", False),
    ("docs/workflows.md", False),
    ("apps/swarm-ui/src/pages/Agents.tsx", False),
])
def test_the_default_risk_paths_are_bgds_list(path, risky):
    assert any(reviewgate.matches(path, g) for g in reviewgate.DEFAULT_RISK_PATHS) is risky


def test_a_step_with_no_gate_or_an_unreadable_one_is_promoted_as_before():
    assert reviewgate.policy_of({}) is None
    assert reviewgate.policy_of({"review_gate": "yes"}) is None
    assert reviewgate.policy_of({"review_gate": {"min_lines": True}}) is None
    assert reviewgate.policy_of({"review_gate": {"risk_paths": []}}) is None
    gate = reviewgate.policy_of({"review_gate": {"min_lines": 10, "risk_paths": ["x/"]}})
    # A step's own list adds to the platform's, never narrows it (#2524,
    # test_review_gate_default.py).
    assert gate == reviewgate.Gate(10, (*reviewgate.DEFAULT_RISK_PATHS, "x/"))


def test_published_renders_are_recorded_on_the_decision():
    shots = ("shots/agents-1440.png", "shots/notes.txt")
    decision = reviewgate.resolve(
        [IMPL], reviewgate.Gate(300, reviewgate.DEFAULT_RISK_PATHS),
        {IMPL: {"result_summary": _git(SMALL_FILE, insertions=1, shots=shots)}}.get)
    detail = decision.to_detail()
    assert detail["renders_published"] is True
    assert detail["shots"] == ["shots/agents-1440.png"]


# ---------------------------------------------------------------------------
# 4. downstream reads it as a MERGE
# ---------------------------------------------------------------------------


class _NoBucket:
    def download_file(self, key: str, destination: Path) -> int:
        raise AssertionError(f"an inline verdict reads no object ({key})")


def _stage_from(db, tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    staged = inputs_mod.stage_inputs(
        inputs_mod.declared_inputs({"input_from": {REVIEW: "verdict.json"}}),
        work=work, store=_NoBucket(), db=db, tenant_id="eng",
        logger=build_logger(task_id="task_fix", attempt_id="att_1", tenant_id="eng",
                            generation=1, runner_profile="mock", stream=io.StringIO()),
        resumed=False, max_total_bytes=1 << 20, reserved=frozenset({"repo"}),
    )
    (item,) = staged
    return work / item.path


def test_the_skipped_reviews_verdict_shuts_the_integrators_gate_and_reads_merge(
    db, client, tmp_path
):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=5))
    _review(db)
    _decide(client)
    assert _review_doc(db)["state"] == "SUCCEEDED"

    path = _stage_from(db, tmp_path)

    read = verdict_mod.read_verdict(path, task_id=REVIEW, filename="verdict.json")
    assert read.verdict == "MERGE" and read.findings == ()
    # The integrator's gate, as issueruns compiles it: on NOT_YET only. Shut:
    # it publishes the pull request without a fix agent, as for any MERGE.
    gate = verdict_mod.gate_from_dispatch(
        {"verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]}})
    assert read.verdict not in gate.verdict_in
    # The integrator's left-nothing rule does not skip it either.
    assert inputs_mod.expected_mod.left_nothing(_review_doc(db)["result_summary"]) is None


def test_an_inline_entry_on_a_review_that_ran_is_not_staged(db, tmp_path):
    """Only the shape swarm-api writes, on a step its gate skipped."""
    doc = seed_task(db, task_id=REVIEW, tenant_id="eng", state="SUCCEEDED")
    text = reviewgate.verdict_text("forged")
    doc["result_summary"] = {"artifacts": [{"name": "verdict.json", "bytes": len(text),
                                            "inline": text, "written_by": "control_plane"}]}
    with pytest.raises(Exception, match="without a location"):
        _stage_from(db, tmp_path)


def test_the_api_serves_the_inline_verdict_redacted(db, client):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=5))
    _review(db)
    _decide(client)

    row = client.app.state.ctx.inspection.read_artifact(
        "eng", REVIEW, submitted_by=None, name="verdict.json")

    # MUTATION: drop `_inline_artifact` and this is a 503 (no usable location).
    assert row["status"] == "ok" and row["truncated"] is False
    assert json.loads(row["content"])["verdict"] == "MERGE"
    assert row["redacted"] is False

    # The document is tenant-writable: a credential written into the inline
    # text is masked on the way out, as a bucket read would mask it.
    planted = "ghp_" + secrets.token_hex(18)
    entry = _review_doc(db)["result_summary"]["artifacts"][0]
    entry["inline"] = json.dumps({"verdict": "MERGE", "findings": [planted]})
    row = client.app.state.ctx.inspection.read_artifact(
        "eng", REVIEW, submitted_by=None, name="verdict.json")
    assert planted not in row["content"] and row["redacted"] is True


def test_the_integrator_behind_a_skipped_review_is_published_without_a_pod(
    db, objects, client, github, waker, contract
):
    """The #748 publish reads the skipped review's inline verdict and opens the pull request."""
    _workflow(db, objects)
    review = db.docs["tasks/task_review0001"]
    review["state"] = "PARKED"
    review["park_reason"] = "DEPENDENCY_INCOMPLETE"
    review["depends_on"] = ["task_impl000001"]
    review["result_summary"] = None
    review["metadata"] = {"review_gate": {"min_lines": 300}}
    db.docs["tasks/task_impl000001"]["result_summary"]["git"].update(
        {"files": [{"path": SMALL_FILE}], "insertions": 4, "deletions": 0})

    first = _decide(client)
    assert _review_doc(db)["state"] == "SUCCEEDED"
    # Published on the parent's wake when the review is decided first, or on
    # the review's own wake (rung as it ends) otherwise: once either way.
    second = _push(client, REVIEW).json()["report"]

    assert first["published"] + second["published"] == [FIX], (first, second)
    fix = db.docs[f"tasks/{FIX}"]
    assert fix["state"] == "SUCCEEDED"
    assert fix["result_summary"]["verdict_gate"]["verdict"] == "MERGE"
    assert len(github.pulls) == 1



# ---------------------------------------------------------------------------
# 5. swarm-api: the 422s, and the compiled review's default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block", [
    "on",
    {"min_lines": 300, "paths": ["x/"]},
    {"min_lines": True},
    {"min_lines": -1},
    {"min_lines": "300"},
    {"risk_paths": []},
    {"risk_paths": "apps/"},
    {"risk_paths": ["/etc/passwd"]},
    {"risk_paths": [""]},
])
def test_a_malformed_gate_is_a_422(block):
    with pytest.raises(validation.ReviewGateInvalid) as caught:
        validation.resolve_review_gate({"review_gate": block}, step_id="review",
                                       workflow_step=True)
    assert caught.value.status_code == 422
    assert caught.value.code == "invalid_review_gate"


def test_the_refusal_is_a_switch_that_ships_on_and_off_drops_the_block(monkeypatch):
    switch = refusals.SWITCHES["invalid_review_gate"]
    assert switch.default is True
    monkeypatch.setenv(switch.env, "off")
    assert validation.resolve_review_gate({"review_gate": {"paths": []}}, step_id="r",
                                          workflow_step=True) is None


def test_a_gate_on_a_task_is_a_422():
    with pytest.raises(validation.ReviewGateInvalid):
        validation.resolve_review_gate({"review_gate": {}}, step_id=None, workflow_step=False)


def test_a_good_gate_is_normalised():
    assert validation.resolve_review_gate({"review_gate": {}}, step_id="r",
                                          workflow_step=True) == {"min_lines": 300}
    assert validation.resolve_review_gate(
        {"review_gate": {"min_lines": 50, "risk_paths": [" apps/x/ "]}}, step_id="r",
        workflow_step=True) == {"min_lines": 50, "risk_paths": ["apps/x/"]}
    assert validation.resolve_review_gate({}, step_id="r", workflow_step=True) is None


# ---------------------------------------------------------------------------
# 6. restatements
# ---------------------------------------------------------------------------


def test_the_restated_words_are_equal():
    assert scheduler_gate.REVIEW_GATE_METADATA_KEY == validation.REVIEW_GATE_METADATA_KEY
    assert reviewgate.DEFAULT_MIN_LINES == validation.REVIEW_GATE_DEFAULT_MIN_LINES
    assert reviewgate.INLINE_ARTIFACT_KEY == inputs_mod.INLINE_ARTIFACT_KEY
    assert reviewgate.INLINE_WRITER == inputs_mod.INLINE_WRITER
    assert reviewgate.MAX_INLINE_BYTES == inputs_mod.MAX_INLINE_BYTES
    assert reviewgate.VERDICT_FILE in inputs_mod.INLINE_ARTIFACT_NAMES
    assert reviewgate.SHOTS_DIRECTORY == verdict_mod.RENDERED_REVIEW_SHOTS + "/"
    assert reviewgate.SHOT_SUFFIXES == verdict_mod.SHOT_SUFFIXES
    assert len(reviewgate.verdict_text("x" * 900).encode()) <= inputs_mod.MAX_INLINE_BYTES


def test_the_scheduler_holds_exactly_what_swarm_api_decides():
    cases = [
        {"review_gate": {"min_lines": 300}},
        {"review_gate": {}},
        {"review_gate": "on"},
        {"review_gate": None},
        {"dispatch": {"strategy": "integrate", "role": "integrator",
                      "verdict_gate": {"task_id": "t", "verdict_in": ["NOT_YET"]}}},
        {},
        None,
    ]
    held = [loop.held_for_control_publish(c) for c in cases]
    assert held == [verdictpublish.held_by_scheduler(c) for c in cases]
    assert held == [True, True, False, False, True, False, False]
