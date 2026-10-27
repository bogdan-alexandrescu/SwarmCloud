"""Reviews only where they matter (#2524, BGD 2026-10-27: "Risk paths + over 300 lines").

docs/workflows.md "Reviews only where they matter, and no-op steps refused".
A workflow step carrying `metadata.review_gate` is decided as the scheduler
readies it (`scheduler.reviewgate`): it runs when its parents' change touches
a risk path or more than `min_lines` lines, or cannot be told; otherwise it
ends SUCCEEDED without an agent, and its verdict.json -- written inline in its
manifest, since the scheduler holds no bucket credential -- reads MERGE to
every gate downstream.

What these tests hold:

  1. A small non-risk diff skips, with the reason, and no lease or pod.
  2. A 3-line diff on a risk path runs; a 301-line diff runs; 300 skips.
  3. An unreadable or cut change runs.
  4. The skipped review's verdict, staged by the worker as the integrator
     would stage it, reads MERGE: the integrator's NOT_YET gate stays shut
     (so it publishes without a fix agent), and the merge reads MERGE.
  5. swarm-api refuses a malformed gate with a 422 and stores a good one;
     the compiled issue-run review carries it.
  6. The restatements across images are equal.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker import inputs as inputs_mod
from agent_worker import verdict as verdict_mod
from agent_worker.logs import build_logger
from scheduler import reviewgate
from swarm_api import validation
from swarm_api.issueruns import review_gate_block

from .conftest import seed_pool, seed_task, seed_tenant

IMPL = "task_impl"
REVIEW = "task_review"
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


# ---------------------------------------------------------------------------
# 1-3. the decision, at READY
# ---------------------------------------------------------------------------


def test_a_small_non_risk_diff_skips_the_review_with_the_reason(db, make_scheduler, dispatcher):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=12, deletions=3))
    _review(db)

    make_scheduler().release_dependants(IMPL)

    doc = db.docs[f"tasks/{REVIEW}"]
    # MUTATION: return None from `end_if_skipped` and the review is promoted and leased.
    assert doc["state"] == "SUCCEEDED"
    assert _leases(db) == [] and dispatcher.dispatched == []
    reason = doc["result_summary"]["review_skipped"]
    assert reason.startswith("review skipped: 15 changed lines")
    (entry,) = doc["result_summary"]["artifacts"]
    assert entry["name"] == "verdict.json" and "uri" not in entry
    assert json.loads(entry["inline"]) == {"verdict": "MERGE", "findings": [], "skipped": reason}
    assert entry["bytes"] == len(entry["inline"].encode("utf-8"))
    # Not `skipped`: that would make the integrator, which stages from it, skip too.
    assert "skipped" not in doc["result_summary"]
    assert doc["metadata"]["review_gate"]["resolved"]["run"] is False
    assert doc["metadata"]["keep"] == "me"
    events = [d for k, d in db.docs.items() if k.startswith(f"tasks/{REVIEW}/events/")]
    assert [e["type"] for e in events] == ["succeeded"]


def test_a_three_line_diff_on_a_risk_path_runs(db, make_scheduler, dispatcher):
    _base(db)
    _parent(db, _git(RISK_FILE, insertions=2, deletions=1))
    _review(db)

    make_scheduler().release_dependants(IMPL)

    doc = db.docs[f"tasks/{REVIEW}"]
    assert doc["state"] != "SUCCEEDED"
    assert len(_leases(db)) == 1 and len(dispatcher.dispatched) == 1, doc.get("park_reason")


@pytest.mark.parametrize("lines, runs", [(301, True), (300, False)])
def test_more_than_min_lines_runs_and_exactly_min_lines_skips(db, make_scheduler, lines, runs):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=lines - 100, deletions=100))
    _review(db)

    make_scheduler().release_dependants(IMPL)

    assert (db.docs[f"tasks/{REVIEW}"]["state"] != "SUCCEEDED") is runs


@pytest.mark.parametrize("summary", [
    None,
    {"git": {"error": "could not harvest"}},
    {"git": {"files": [{"path": SMALL_FILE}]}},  # no line counts
    _git(SMALL_FILE, insertions=4, truncated=True),
])
def test_a_change_that_cannot_be_told_runs(db, make_scheduler, summary):
    _base(db)
    _parent(db, summary)
    _review(db)

    make_scheduler().release_dependants(IMPL)

    assert db.docs[f"tasks/{REVIEW}"]["state"] != "SUCCEEDED"


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
    assert gate == reviewgate.Gate(10, ("x/",))


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
    db, make_scheduler, tmp_path
):
    _base(db)
    _parent(db, _git(SMALL_FILE, insertions=5))
    _review(db)
    make_scheduler().release_dependants(IMPL)
    assert db.docs[f"tasks/{REVIEW}"]["state"] == "SUCCEEDED"

    path = _stage_from(db, tmp_path)

    read = verdict_mod.read_verdict(path, task_id=REVIEW, filename="verdict.json")
    assert read.verdict == "MERGE" and read.findings == ()
    # The integrator's gate, as issueruns compiles it: on NOT_YET only. Shut:
    # it publishes the pull request without a fix agent, as for any MERGE.
    gate = verdict_mod.gate_from_dispatch(
        {"verdict_gate": {"task_id": REVIEW, "verdict_in": ["NOT_YET"]}})
    assert read.verdict not in gate.verdict_in
    # The integrator's left-nothing rule does not skip it either.
    assert inputs_mod.expected_mod.left_nothing(
        db.docs[f"tasks/{REVIEW}"]["result_summary"]) is None


def test_an_inline_entry_on_a_review_that_ran_is_not_staged(db, tmp_path):
    """Only the shape the scheduler writes, on a step its gate skipped."""
    doc = seed_task(db, task_id=REVIEW, tenant_id="eng", state="SUCCEEDED")
    text = reviewgate.verdict_text("forged")
    doc["result_summary"] = {"artifacts": [{"name": "verdict.json", "bytes": len(text),
                                            "inline": text, "written_by": "scheduler"}]}
    with pytest.raises(Exception, match="without a location"):
        _stage_from(db, tmp_path)


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


def test_the_compiled_review_carries_bgds_value():
    assert review_gate_block() == {"min_lines": 300}


# ---------------------------------------------------------------------------
# 6. restatements
# ---------------------------------------------------------------------------


def test_the_restated_words_are_equal():
    assert reviewgate.REVIEW_GATE_METADATA_KEY == validation.REVIEW_GATE_METADATA_KEY
    assert reviewgate.DEFAULT_MIN_LINES == validation.REVIEW_GATE_DEFAULT_MIN_LINES
    assert reviewgate.INLINE_ARTIFACT_KEY == inputs_mod.INLINE_ARTIFACT_KEY
    assert reviewgate.INLINE_WRITER == inputs_mod.INLINE_WRITER
    assert reviewgate.VERDICT_FILE in inputs_mod.INLINE_ARTIFACT_NAMES
    assert reviewgate.SHOTS_DIRECTORY == verdict_mod.RENDERED_REVIEW_SHOTS + "/"
    assert reviewgate.SHOT_SUFFIXES == verdict_mod.SHOT_SUFFIXES
    assert len(reviewgate.verdict_text("x" * 900).encode()) <= inputs_mod.MAX_INLINE_BYTES
