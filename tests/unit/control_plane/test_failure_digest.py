"""The failure digest `failure-review` hands its investigator (docs/design/failure-review.md
§2, §3; lane FR-2 DIGEST, part of #1933).

Each test names the rule of the row it holds:

  * each skip rule skips exactly its case and names itself;
  * a person's cancel and its cascade are skipped, a system cancel is not;
  * an expired await-input step and the steps its sweep cancelled are skipped,
    a sweep a FAILED step caused is not;
  * content fields appear only for the platform's own agents (owner decision Q1);
  * the digest is redacted, bounded and carries no prompt;
  * the same failure in two overlapping windows is one entry;
  * a fingerprint is stable across reruns (here: the digest itself; the
    fingerprint's own test is in test_failure_causes.py).

Offline: `build` is pure over the documents it is handed, and `gather` runs
over the in-memory Firestore. Every fake credential is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest
from swarm_common.profiles import RUNNER_PROFILES

from swarm_api import failuredigest as fd
from swarm_api.awaitinput import is_await_profile
from swarm_api.failuredigest import Documents, Scope

from .fakes import FakeFirestore

FIRE = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SINCE, UNTIL = fd.window(FIRE)
PLATFORM = "plat"
OTHER = "acme"
SA_DOMAIN = "example-proj.iam.gserviceaccount.com"
#: The await-input step's profile, read from the catalogue rather than guessed.
AWAIT_PROFILE = next(name for name in RUNNER_PROFILES if is_await_profile(name))


def at(hours_before: float) -> datetime:
    return FIRE - timedelta(hours=hours_before)


def task(task_id: str, *, tenant: str = OTHER, state: str = "FAILED", ended: float = 1.0,
         **fields: Any) -> dict[str, Any]:
    doc = {
        "id": task_id, "tenant_id": tenant, "state": state,
        "created_at": at(ended + 1), "updated_at": at(ended), "completed_at": at(ended),
        "runner_profile": "claude-code", "resource_class": "standard",
        "submitted_by": "dev@example.com", "input": {"prompt": "do the thing"},
        "metadata": {}, "end_cause": "runner_error",
        "last_error": "runner exited 1 after 812s",
    }
    doc.update(fields)
    return doc


def cancel_event(task_id: str, by: str, *, tenant: str = OTHER) -> dict[str, Any]:
    return {"event_id": f"ev_{task_id}", "task_id": task_id, "tenant_id": tenant,
            "type": "cancelled", "at": at(1.5), "detail": {"requested_by": by, "phase": "cancelled"}}


def docs_of(*tasks: Mapping[str, Any], events: Sequence[Mapping[str, Any]] = (),
            runs: Sequence[Mapping[str, Any]] = (), workflows: Sequence[Mapping[str, Any]] = (),
            attempts: Sequence[Mapping[str, Any]] = ()) -> Documents:
    docs = Documents()
    for t in tasks:
        docs.tasks[t["id"]] = dict(t)
    for e in events:
        docs.events.setdefault(e["task_id"], []).append(dict(e))
    for r in runs:
        docs.runs[r["id"]] = dict(r)
    for w in workflows:
        docs.workflows[w["workflow_id"]] = dict(w)
    for a in attempts:
        docs.attempts.setdefault(a["task_id"], []).append(dict(a))
    return docs


def build(docs: Documents, *, scope: Scope = Scope(platform_tenant=PLATFORM), content: Any = None,
          since: datetime = SINCE, until: datetime = UNTIL, known: Sequence[Mapping[str, Any]] = ()
          ) -> fd.Digest:
    return fd.build(docs, since=since, until=until, firing_id="fr_test", scope=scope,
                    content=content, known=known)


def reviewed_ids(digest: fd.Digest) -> set[str]:
    return {ref["id"] for ref in digest.refs.values()}


class RecordingContent:
    """Content that records whom it was asked about, and answers with a credential in it."""

    def __init__(self, *, logs_view: bool = True, raises: bool = False) -> None:
        self.asked: list[str] = []
        self.secret = "ghp_" + secrets.token_hex(18)
        self.logs_view = logs_view
        self.raises = raises

    def log_tail(self, task: Mapping[str, Any], stream: str) -> str | None:
        self.asked.append(task["id"])
        if self.raises:
            raise RuntimeError("bucket unreachable")
        return f"line one\nexport GITHUB_TOKEN={self.secret}\nTraceback: boom in {stream}"

    def ci_excerpt(self, task: Mapping[str, Any]) -> str | None:
        self.asked.append(task["id"])
        return "CI is red at abc: unit"

    def platform_logs(self, task: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> str | None:
        self.asked.append(task["id"])
        # Lane AU-5's view unset: the reader answers none, never fails the firing.
        return "reconciler: lease silent for 912s" if self.logs_view else None


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


def test_the_window_is_seven_hours_back_and_reaches_to_the_last_success_capped_at_48():
    # MUTATION: WINDOW = timedelta(hours=6) leaves no overlap and this fails.
    assert fd.window(FIRE) == (FIRE - timedelta(hours=7), FIRE)
    # MUTATION: ignore last_succeeded_end and a failed firing's hours are reviewed by no one.
    assert fd.window(FIRE, FIRE - timedelta(hours=18))[0] == FIRE - timedelta(hours=19)
    # MUTATION: drop the MAX_LOOKBACK cap and a long-failed schedule reads days.
    assert fd.window(FIRE, FIRE - timedelta(days=5))[0] == FIRE - timedelta(hours=48)
    # A last success inside the normal window never narrows it.
    assert fd.window(FIRE, FIRE - timedelta(hours=2))[0] == FIRE - timedelta(hours=7)


def test_only_what_ended_inside_the_window_and_failed_or_was_cancelled_is_read():
    docs = docs_of(task("in"), task("old", ended=8), task("ok", state="SUCCEEDED"),
                   task("running", state="RUNNING", completed_at=None),
                   task("dead", state="DEAD_LETTERED"), task("sys-cancel", state="CANCELLED",
                                                                end_cause="cancel_requested"))
    # MUTATION: a window test on created_at instead of completed_at reads "old".
    assert reviewed_ids(build(docs)) == {"in", "dead", "sys-cancel"}


# --------------------------------------------------------------------------
# Each skip rule skips exactly its case and names itself
# --------------------------------------------------------------------------

CASES: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]] = {}


def _case(rule: str, skipped: list[dict[str, Any]], control: list[dict[str, Any]], **extra: Any) -> None:
    CASES[rule] = (skipped, control, extra)


_case(fd.SELF,
      [task("me", metadata={"schedule": {"type": "failure-review", "firing_id": "fr_1"}}),
       task("my-child", parent_task_id="me-running")],
      [task("fix-run", metadata={"schedule": {"type": "failure-review"}, "issue_run": "run_1"}),
       task("observer", metadata={"schedule": {"type": "observer"}})],
      relatives=[task("me-running", state="RUNNING", completed_at=None,
                      metadata={"schedule": {"type": "failure-review"}})])
_case(fd.ACCEPTANCE_DELIBERATE,
      [task("acc-fail", runner_profile="mock", input={"fail": True},
            metadata={"source": "acceptance", "acceptance_run": "r1", "check": "mock_fail"})],
      [task("acc-real", runner_profile="mock", input={},
            metadata={"source": "acceptance", "check": "mock_exit"}),
       task("not-acc", runner_profile="mock", input={"fail": True})])
_case(fd.VERIFY_FENCE,
      [task("verify", submitted_by=f"swarm-verify@{SA_DOMAIN}")],
      [task("other-sa", submitted_by=f"swarm-robots@{SA_DOMAIN}")])
_case(fd.PERSON_CANCEL,
      [task("person", state="CANCELLED", end_cause="cancel_requested")],
      [task("by-sa", state="CANCELLED", end_cause="cancel_requested"),
       task("by-nobody", state="CANCELLED", end_cause="cancel_requested")],
      events=[cancel_event("person", "dev@example.com"),
              cancel_event("by-sa", f"swarm-scheduler@{SA_DOMAIN}")])
_case(fd.QUESTION_UNANSWERED,
      [task("silent", state="DEAD_LETTERED", last_error="owner question unanswered after 24h"),
       task("await-expired", state="DEAD_LETTERED", runner_profile=AWAIT_PROFILE, end_cause=None,
            last_error="await_input_expired: nobody answered within 72000 seconds")],
      [task("loud", state="DEAD_LETTERED", last_error="the owner answered no"),
       # A step whose signed declaration did not verify: a defect, not silence.
       task("await-refused", state="DEAD_LETTERED", runner_profile=AWAIT_PROFILE, end_cause=None,
            last_error="await_input_spec_refused: signature does not verify"),
       # Only the await-input profile's expiry sweep writes the marker; an agent
       # printing it is not that sweep.
       task("not-await", state="DEAD_LETTERED",
            last_error="await_input_expired: nobody answered within 60 seconds")])
_case(fd.FIX_ROUND_LIVE,
      [task("in-checking", metadata={"issue_run": "run_live"}),
       task("merge-sibling", workflow_id="wf_m")],
      [task("in-failed-run", metadata={"issue_run": "run_dead"}),
       task("spent-sibling", workflow_id="wf_spent")],
      runs=[{"id": "run_live", "tenant_id": OTHER, "state": "CHECKING"},
            {"id": "run_dead", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1)}],
      relatives=[task("merge", state="PARKED", completed_at=None, workflow_id="wf_m",
                      park_reason="CI_PENDING", metadata={"merge_fix_rounds": 2}),
                 task("merge2", state="PARKED", completed_at=None, workflow_id="wf_spent",
                      park_reason="CI_PENDING",
                      metadata={"merge_fix_rounds": 1, "merge_fix": {"rounds": [{"round": 1}]}})])


_case(fd.PERSON_PUSH,
      # #2505, BGD 2026-10-27: a merge step a person's push superseded. Until
      # contract request SUPERSEDED_BY_PERSON_PUSH lands the worker ends it
      # FAILED merge_refused with this code; after, CANCELLED with the cause.
      [task("pushed-over", runner_profile="merge", end_cause="merge_refused",
            result_summary={"merge": {"refusal": {"code": "superseded_by_person_push",
                                                  "message": "pat-person pushed it"}}}),
       task("pushed-over-typed", state="CANCELLED", runner_profile="merge",
            end_cause="superseded_by_person_push")],
      # An unknown branch-mate's head is still head_moved, and reviewed; so
      # is any other step that only names the code somewhere it is not the
      # merge's refusal.
      [task("branch-mate", runner_profile="merge", end_cause="merge_refused",
            result_summary={"merge": {"refusal": {"code": "head_moved",
                                                  "message": "superseded_by_person_push"}}}),
       task("verdict", runner_profile="post-verdict", end_cause="verdict_refused",
            result_summary={"verdict": {"refusal": {"code": "superseded_by_person_push"}}}),
       task("agent-said-so", last_error="superseded_by_person_push")])


@pytest.mark.parametrize("rule", fd.SKIP_RULES)
def test_each_skip_rule_skips_exactly_its_case_and_names_itself(rule: str):
    skipped, control, extra = CASES[rule]
    docs = docs_of(*skipped, *control, *extra.get("relatives", []),
                   events=extra.get("events", []), runs=extra.get("runs", []))
    digest = build(docs)
    ids = {t["id"] for t in skipped}
    # MUTATION: remove the rule's branch from `skip_rule` and its cases are reviewed.
    assert set(digest.skipped[rule]) >= ids
    # MUTATION: make any rule broader (e.g. `self` without the issue_run exception,
    # acceptance without `fail: true`) and a control lands in `skipped`.
    assert {t["id"] for t in control} <= reviewed_ids(digest)
    # Named, and counted under its own name only.
    assert digest.doc["skipped"][rule] == len(digest.skipped[rule])
    others = {r: n for r, n in digest.doc["skipped"].items() if r != rule}
    assert not any(others.values()), others
    assert not ids & reviewed_ids(digest)


def test_the_rules_are_the_designs_seven_and_no_others():
    # MUTATION: a rule added (or one the agent could add) changes this tuple.
    assert fd.SKIP_RULES == ("self", "acceptance_deliberate", "verify_fence", "person_cancel",
                             "question_unanswered", "fix_round_live", "person_push")


def test_a_workflow_failed_only_by_a_person_push_is_skipped_with_its_step():
    """The merge step is a workflow's last step: the workflow FAILED only
    because a person's push superseded it is not reviewed either."""
    step = task("merge-step", runner_profile="merge", end_cause="merge_refused",
                workflow_id="wf_push",
                result_summary={"merge": {"refusal": {"code": "superseded_by_person_push"}}})
    workflow = {"workflow_id": "wf_push", "tenant_id": OTHER, "state": "FAILED",
                "updated_at": at(1), "steps": [{"step_id": "merge", "task_id": "merge-step"}]}
    digest = build(docs_of(step, workflows=[workflow]))
    # MUTATION: drop the PERSON_PUSH branch of `skip_rule` and both are reviewed.
    assert reviewed_ids(digest) == set()
    assert set(digest.skipped[fd.PERSON_PUSH]) == {"merge-step", "wf_push"}


# --------------------------------------------------------------------------
# A person's cancel and its cascade are skipped; a system cancel is not
# --------------------------------------------------------------------------


def test_a_persons_cancel_and_its_cascade_are_skipped_and_a_system_cancel_is_not():
    parent = task("parent", state="CANCELLED", end_cause="cancel_requested")
    child = task("child", state="CANCELLED", end_cause="child_cascade", parent_task_id="parent",
                 metadata={"child_cascade": {"why": "parent_cancelled", "parent_task_id": "parent"}})
    downstream = task("step2", state="CANCELLED", end_cause="cancelled_parent", depends_on=["parent"],
                      workflow_id="wf_p")
    sys_parent = task("sys-parent", state="CANCELLED", end_cause="cancel_requested")
    sys_child = task("sys-child", state="CANCELLED", end_cause="child_cascade",
                     parent_task_id="sys-parent",
                     metadata={"child_cascade": {"why": "parent_cancelled", "parent_task_id": "sys-parent"}})
    events = [cancel_event("parent", "Dev@Example.com"),
              {**cancel_event("child", "cascade:parent"), "detail": {"requested_by": "cascade:parent"}},
              {**cancel_event("sys-child", "cascade:sys-parent"),
               "detail": {"requested_by": "cascade:sys-parent"}},
              cancel_event("sys-parent", f"swarm-reconciler@{SA_DOMAIN}")]
    workflow = {"workflow_id": "wf_p", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
                "steps": [{"step_id": "a", "task_id": "parent"}, {"step_id": "b", "task_id": "step2"}]}
    digest = build(docs_of(parent, child, downstream, sys_parent, sys_child, events=events,
                           workflows=[workflow]))
    # MUTATION: `is_person` true for service accounts and the system cancel is skipped too.
    assert reviewed_ids(digest) == {"sys-parent", "sys-child"}
    # MUTATION: drop the cascade branch of `person_cancelled` and "child" is reviewed.
    # MUTATION: drop the CANCELLED_PARENT branch and "step2" is reviewed.
    # MUTATION: drop the workflow fold and wf_p (FAILED only because of the cancel) is reviewed.
    assert set(digest.skipped[fd.PERSON_CANCEL]) == {"parent", "child", "step2", "wf_p"}


def sweep_event(task_id: str, workflow_id: str, *failed: tuple[str, str, str]) -> dict[str, Any]:
    """The CANCELLED event `SchedulerStore.cancel_if_not_started` writes for the fail_workflow sweep."""
    steps = [{"step_id": s, "task_id": t, "state": st} for s, t, st in failed]
    return {"event_id": f"ev_{task_id}", "task_id": task_id, "tenant_id": OTHER, "type": "cancelled",
            "at": at(1.5),
            "detail": {"reason": f"workflow step {steps[0]['step_id']} is {steps[0]['state']} and "
                                 "on_step_failure is fail_workflow, so steps that had not started "
                                 "were cancelled",
                       "from_state": "PARKED", "workflow_id": workflow_id,
                       "on_step_failure": "fail_workflow", "failed_steps": steps}}


def swept(task_id: str, workflow_id: str) -> dict[str, Any]:
    return task(task_id, state="CANCELLED", end_cause="workflow_sweep", workflow_id=workflow_id,
                last_error="workflow step x is DEAD_LETTERED and on_step_failure is fail_workflow, "
                           "so steps that had not started were cancelled")


def test_an_expired_await_input_step_and_the_steps_its_sweep_cancelled_are_skipped_and_a_failed_steps_sweep_is_not():
    ask = task("ask", state="DEAD_LETTERED", runner_profile=AWAIT_PROFILE, end_cause=None,
               workflow_id="wf_q",
               last_error="await_input_expired: nobody answered within 72000 seconds")
    sib1, sib2, sib3 = swept("sib1", "wf_q"), swept("sib2", "wf_q"), swept("sib3", "wf_q")
    after = task("after", state="CANCELLED", end_cause="cancelled_parent", depends_on=["ask"],
                 workflow_id="wf_q", last_error="parent ask ended DEAD_LETTERED")
    crash = task("crash", workflow_id="wf_f")
    fsib1, fsib2 = swept("fsib1", "wf_f"), swept("fsib2", "wf_f")
    ask2 = task("ask2", state="DEAD_LETTERED", runner_profile=AWAIT_PROFILE, end_cause=None,
                workflow_id="wf_mix",
                last_error="await_input_expired: nobody answered within 600 seconds")
    crash2 = task("crash2", workflow_id="wf_mix")
    msib = swept("msib", "wf_mix")
    events = [sweep_event("sib1", "wf_q", ("ask", "ask", "DEAD_LETTERED")),
              sweep_event("sib2", "wf_q", ("ask", "ask", "DEAD_LETTERED")),
              # sib3 has no event in the read: its causes are the workflow's failed steps.
              sweep_event("fsib1", "wf_f", ("build", "crash", "FAILED")),
              sweep_event("msib", "wf_mix", ("ask", "ask2", "DEAD_LETTERED"),
                          ("build", "crash2", "FAILED"))]
    workflows = [
        {"workflow_id": "wf_q", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
         "steps": [{"step_id": s, "task_id": s} for s in ("ask", "sib1", "sib2", "sib3", "after")]},
        {"workflow_id": "wf_f", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
         "steps": [{"step_id": "build", "task_id": "crash"}, {"step_id": "a", "task_id": "fsib1"},
                   {"step_id": "b", "task_id": "fsib2"}]},
    ]
    digest = build(docs_of(ask, sib1, sib2, sib3, after, crash, fsib1, fsib2, ask2, crash2, msib,
                           events=events, workflows=workflows))
    # MUTATION: drop the await-input expiry branch of `_question_unanswered` and "ask"
    # (and with it everything below) is reviewed.
    # MUTATION: drop the WORKFLOW_SWEEP branch and "sib1", "sib2", "sib3" are reviewed.
    # MUTATION: read only the event's failed_steps, no fallback, and "sib3" is reviewed.
    # MUTATION: drop the CANCELLED_PARENT branch and "after" is reviewed.
    # MUTATION: drop the workflow fold and wf_q (FAILED only because of the silence) is reviewed.
    assert set(digest.skipped[fd.QUESTION_UNANSWERED]) == {"ask", "sib1", "sib2", "sib3", "after",
                                                            "ask2", "wf_q"}
    # MUTATION: a sweep counted as silence whatever caused it (`any` for `all`, or no
    # cause check at all), and the FAILED claude-code step's sweep is skipped too.
    assert {"crash", "fsib1", "fsib2", "crash2", "msib"} <= reviewed_ids(digest)
    assert "wf_f" not in digest.skipped[fd.QUESTION_UNANSWERED]
    assert not any(n for r, n in digest.doc["skipped"].items() if r != fd.QUESTION_UNANSWERED)


def test_is_person_names_people_only():
    assert fd.is_person("dev@example.com")
    # MUTATION: any "@" is a person, and these four are read as people.
    assert not fd.is_person(f"swarm-api@{SA_DOMAIN}")
    assert not fd.is_person("service-123@gcp-sa-cloudbuild.iam.gserviceaccount.com")
    assert not fd.is_person("cascade:task_abc")
    assert not fd.is_person(None)


# --------------------------------------------------------------------------
# Content fields appear only for the platform's own agents (Q1)
# --------------------------------------------------------------------------

CONTENT_KEYS = {"agent_id", "workflow_id", "step_id", "issue_run_id", "repository", "attempt_ids",
                "logs", "ci_excerpt", "platform_logs"}


def _entries(digest: fd.Digest) -> dict[str, dict[str, Any]]:
    out = {}
    for group in digest.doc["groups"]:
        for entry in group["entries"]:
            out[digest.refs[entry["ref"]]["id"]] = entry
    return out


def test_content_fields_appear_only_for_the_platform_tenant_and_platform_repositories():
    content = RecordingContent()
    docs = docs_of(
        task("ours", tenant=PLATFORM, repository_url="https://github.com/swarm-org/swarmcloud"),
        task("theirs", repository_url="https://github.com/acme/private-thing.git"),
        task("plat-repo-agent", tenant="other2",
             repository_url="https://github.com/Swarm-Org/SwarmCloud.git"),
    )
    scope = Scope(platform_tenant=PLATFORM, platform_repositories=frozenset({"swarm-org/swarmcloud"}))
    digest = build(docs, scope=scope, content=content)
    entries = _entries(digest)
    # MUTATION: `own=True` for every entry and "theirs" carries content.
    assert not CONTENT_KEYS & set(entries["theirs"])
    # MUTATION: content asked for every agent: another tenant's log is read at all.
    assert "theirs" not in content.asked
    # MUTATION: `Scope.owns` ignores platform repositories and this one carries none.
    assert CONTENT_KEYS <= set(entries["plat-repo-agent"])
    ours = entries["ours"]
    assert ours["agent_id"] == "ours" and ours["ci_excerpt"] and ours["platform_logs"]
    assert set(ours["logs"]) == set(fd.STREAMS)
    text, _ = fd.bounded(digest.doc)
    # Another tenant's id, name and repository never cross; the labels are opaque.
    assert OTHER not in text and "private-thing" not in text and "theirs" not in text
    assert entries["theirs"]["tenant"].startswith("t") and ours["tenant"] == "platform"
    assert digest.tenants[entries["theirs"]["tenant"]] == OTHER


def test_platform_log_lines_degrade_to_none_without_the_log_view():
    content = RecordingContent(logs_view=False)
    digest = build(docs_of(task("ours", tenant=PLATFORM)), content=content)
    entry = _entries(digest)["ours"]
    # MUTATION: a missing view raising, or failing the digest, loses every other field.
    assert entry["platform_logs"] is None and entry["logs"]


def test_a_content_reader_that_raises_is_a_missing_piece_not_a_failed_digest():
    digest = build(docs_of(task("ours", tenant=PLATFORM)), content=RecordingContent(raises=True))
    # MUTATION: drop `_safe` and the read's exception escapes `build`.
    assert _entries(digest)["ours"]["logs"] == {}


# --------------------------------------------------------------------------
# Redacted, bounded, no prompt
# --------------------------------------------------------------------------


def test_the_digest_is_redacted_and_carries_no_prompt_output_or_title():
    secret = "ghp_" + secrets.token_hex(18)
    prompt_marker = "PROMPT-" + secrets.token_hex(6)
    output_marker = "OUTPUT-" + secrets.token_hex(6)
    content = RecordingContent()
    ours = task("ours", tenant=PLATFORM, last_error=f"push refused: token {secret} rejected",
                input={"prompt": prompt_marker, "title": prompt_marker},
                result_summary={"text": output_marker})
    theirs = task("theirs", last_error=f"auth failed with {secret}",
                  input={"prompt": prompt_marker}, result_summary={"text": output_marker})
    attempts = [{"attempt_id": "att_1", "task_id": "theirs", "tenant_id": OTHER, "generation": 1,
                 "lease_id": "lease_1", "backend": "gke", "created_at": at(2),
                 "error": f"exit 1: {secret}", "peak_rss_bytes": 7 * 1024 ** 3}]
    digest = build(docs_of(ours, theirs, attempts=attempts), content=content)
    text = fd.as_data(fd.bounded(digest.doc)[0])
    # MUTATION: skip `redact` on last_error, an attempt's error or a log tail.
    assert secret not in text and content.secret not in text
    # MUTATION: project `input` or `result_summary` into an entry.
    assert prompt_marker not in text and output_marker not in text
    assert "do the thing" not in text
    theirs_entry = _entries(digest)["theirs"]
    assert theirs_entry["attempts"][0]["peak_rss_bytes"] == 7 * 1024 ** 3
    assert theirs_entry["memory_limit_bytes"] == 8 * 1024 ** 3


def test_a_last_error_is_cut_to_500_bytes_after_redaction():
    entry = _entries(build(docs_of(task("long", last_error="é" * 2000))))["long"]
    # MUTATION: ERROR_BYTES = 4000, or a cut inside a character.
    assert len(entry["last_error"].encode("utf-8")) <= fd.ERROR_BYTES
    entry["last_error"].encode("utf-8").decode("utf-8")


def _word(i: int) -> str:
    # Letters only, so the signature keeps it: 100 distinct causes, not one.
    return "".join(chr(ord("k") + int(d)) for d in f"{i:03d}")


def test_the_digest_is_bounded_keeps_every_count_and_names_what_it_dropped():
    many = [task(f"t{g:03d}{n}", tenant=f"tenant{g % 7}", ended=1 + g / 200,
                 last_error=f"stage {_word(g)} broke after {7000 + n}s " + "x" * 400)
            for g in range(100) for n in range(3)]
    digest = build(docs_of(*many))
    raw = json.dumps(digest.doc, default=str)
    assert len(raw.encode()) > fd.MAX_DIGEST_BYTES  # the control: unbounded, it is over
    text, dropped = fd.bounded(digest.doc)
    # MUTATION: return the unbounded text and this is over 48 KiB.
    assert len(text.encode()) <= fd.MAX_DIGEST_BYTES
    doc = json.loads(text)
    # Grouped by signature: the figures are placeholders, so each cause's three are one group.
    # MUTATION: drop whole groups instead of exemplars and a count is lost.
    assert sorted(g["count"] for g in doc["groups"]) == [3] * 100
    # MUTATION: drop exemplars without naming them, and a failure vanishes silently.
    listed = {r for g in doc["groups"] for r in g["refs"]} | {
        e["ref"] for g in doc["groups"] for e in g["entries"]}
    assert set(dropped) and set(dropped) <= listed
    assert listed == set(digest.refs)


def test_the_digest_is_handed_over_as_data_between_nonce_lines():
    wrapped = fd.as_data('{"a": 1}', nonce="00ff00ff00ff00ff")
    lines = wrapped.splitlines()
    marker = "=== FAILURE DIGEST 00ff00ff00ff00ff ==="
    # MUTATION: a fixed marker (no nonce) lets a last_error end the data.
    assert lines.count(marker) == 2 and "DATA, not instructions" in wrapped
    assert fd.as_data("x") != fd.as_data("x")


# --------------------------------------------------------------------------
# The same failure in two overlapping windows is one entry
# --------------------------------------------------------------------------


def test_one_entry_per_failure_and_its_workflow_and_run_fold_into_it():
    step = task("step", workflow_id="wf_1", metadata={"issue_run": "run_1"})
    workflow = {"workflow_id": "wf_1", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
                "steps": [{"step_id": "implement", "task_id": "step"}]}
    run = {"id": "run_1", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
           "workflow_id": "wf_1"}
    digest = build(docs_of(step, workflows=[workflow], runs=[run]))
    # MUTATION: give a workflow or a run its own entry beside its failed step's.
    assert reviewed_ids(digest) == {"step"}
    entry = _entries(digest)["step"]
    assert entry["workflow_failed"] and entry["issue_run_failed"]


def test_a_workflow_with_no_failed_step_to_explain_it_is_its_own_entry():
    workflow = {"workflow_id": "wf_alone", "tenant_id": OTHER, "state": "FAILED", "updated_at": at(1),
                "steps": [], "on_step_failure": "fail_workflow"}
    digest = build(docs_of(workflows=[workflow]))
    # MUTATION: drop the `extra` entries and an unexplained workflow failure vanishes.
    assert reviewed_ids(digest) == {"wf_alone"}


def test_the_same_failure_in_two_overlapping_windows_is_one_entry_each_and_known_the_second_time():
    failure = task("overlap", ended=6.5)
    first = build(docs_of(failure), since=FIRE - timedelta(hours=13), until=FIRE - timedelta(hours=6))
    second = build(docs_of(failure, failure))
    assert reviewed_ids(first) == reviewed_ids(second) == {"overlap"}
    assert len(second.refs) == 1
    known = [{"fingerprint": "fr-0123456789ab", "state": "open", "issue": 7,
              "failure_ids": ["overlap"], "signatures": ["runner exited <n>"]}]
    entry = _entries(build(docs_of(failure), known=known))["overlap"]
    # MUTATION: ignore the known causes' failure ids and the overlap reads as new.
    assert entry["known"] == "fr-0123456789ab"


def test_a_rerun_over_the_same_documents_is_the_same_digest():
    docs = docs_of(task("a", tenant="z"), task("b", tenant="y"), task("c", tenant=PLATFORM),
                   task("d", tenant="y", last_error="lease silent for 913s (grace 300s, expired=True)",
                        end_cause="lost_worker"),
                   task("e", tenant="z", last_error="lease silent for 18s (grace 300s, expired=False)",
                        end_cause="lost_worker"))
    one, two = build(docs), build(docs)
    # MUTATION: labels or references from set order or a counter outside build.
    assert fd.bounded(one.doc) == fd.bounded(two.doc) and one.refs == two.refs
    sigs = {g["signature"]["error"] for g in one.doc["groups"]}
    # MUTATION: no placeholders and the two lease-silent lines are two groups.
    assert "lease silent for <n>s (grace <n>s, expired=<b>)" in sigs
    assert len(one.doc["groups"]) == 2


# --------------------------------------------------------------------------
# The read
# --------------------------------------------------------------------------


def test_gather_reads_the_window_across_tenants_and_never_mixes_a_tenants_documents():
    db = FakeFirestore()
    for doc in (task("in-a", tenant="a"), task("in-b", tenant="b"), task("old", ended=9),
                task("ok", state="SUCCEEDED"),
                task("child", tenant="a", state="CANCELLED", end_cause="child_cascade",
                     parent_task_id="parent",
                     metadata={"child_cascade": {"why": "parent_cancelled", "parent_task_id": "parent"}}),
                task("parent", tenant="a", state="CANCELLED", ended=30)):
        db.collection("tasks").document(doc["id"]).set(doc)
    db.collection("tasks").document("parent").collection("events").document("ev1").set(
        cancel_event("parent", "dev@example.com", tenant="a"))
    db.collection("attempts").document("att_a").set(
        {"attempt_id": "att_a", "task_id": "in-a", "tenant_id": "a", "generation": 1,
         "lease_id": "lease_a", "backend": "gke", "created_at": at(2)})
    db.collection("attempts").document("att_x").set(
        {"attempt_id": "att_x", "task_id": "in-a", "tenant_id": "b", "generation": 1,
         "lease_id": "lease_x", "backend": "gke", "created_at": at(2)})
    db.collection("leases").document("lease_a").set(
        {"lease_id": "lease_a", "tenant_id": "a", "release_reason": "lease_silent"})
    docs = fd.gather(db, since=SINCE, until=UNTIL)
    # MUTATION: range on created_at, or no state filter, reads "old" and "ok".
    assert {"in-a", "in-b", "child"} <= set(docs.tasks) and "ok" not in docs.tasks
    assert "old" not in docs.tasks
    # MUTATION: drop the tenant filter on attempts and tenant b's attempt joins a's task.
    assert [a["attempt_id"] for a in docs.attempts["in-a"]] == ["att_a"]
    # The parent is read outside the window, so the cascade rule can follow it.
    assert "parent" in docs.tasks and docs.events["parent"]
    digest = build(docs)
    assert "child" in digest.skipped[fd.PERSON_CANCEL]
    assert _entries(digest)["in-a"]["heartbeat"]["release_reason"] == "lease_silent"


# --------------------------------------------------------------------------
# The content reader: log tails, and platform log lines through the view (AU-5)
# --------------------------------------------------------------------------


class FakeInspection:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def read_logs(self, tenant_id: str, task_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"tenant_id": tenant_id, "task_id": task_id, **kwargs})
        offset, length = kwargs["offset"], kwargs["limit_bytes"]
        raw = self.text.encode()
        return {"streams": [{"status": "ok", "total_bytes": len(raw),
                             "content": raw[offset: offset + length].decode()}]}


class FakeLogs:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def entries(self, view: str, filter_text: str, *, limit: int) -> list[dict[str, Any]]:
        self.calls.append((view, filter_text, limit))
        return [{"timestamp": "2026-10-22T11:00:00Z", "severity": "WARNING",
                 "resource": {"type": "cloud_run_revision"},
                 "textPayload": "reclaimed lease of att_1: lease silent"}]


VIEW = "projects/swarm-proj/locations/global/buckets/_Default/views/swarm-workers"


def test_the_content_reader_reads_a_streams_last_bytes_in_the_tasks_own_tenant():
    inspection = FakeInspection("a" * 20000 + "THE END")
    content = fd.ApiContent(inspection=inspection, log_view=None)
    tail = content.log_tail(task("ours", tenant=PLATFORM), "agent_stderr")
    # MUTATION: read from offset 0 only and the end of the log, which explains it, is lost.
    assert tail.endswith("THE END") and len(tail.encode()) <= fd.LOG_TAIL_BYTES
    assert {c["tenant_id"] for c in inspection.calls} == {PLATFORM}
    assert all(c["stream"] == ["agent_stderr"] for c in inspection.calls)


def test_platform_log_lines_are_none_without_the_view_and_never_read():
    logs = FakeLogs()
    content = fd.ApiContent(inspection=FakeInspection(""), log_view=None, logs=logs)
    attempts = [{"attempt_id": "att_1"}]
    # MUTATION: query Cloud Logging with no view configured (no L-1 binding: a 403 per firing).
    assert content.platform_logs(task("ours", tenant=PLATFORM, completed_at=at(1)), attempts) is None
    assert logs.calls == []


def test_platform_log_lines_read_only_swarm_services_for_the_agents_own_attempts():
    logs = FakeLogs()
    content = fd.ApiContent(inspection=FakeInspection(""), log_view=VIEW, logs=logs)
    attempts = [{"attempt_id": "att_1"}, {"attempt_id": 'att_2" OR true'}]
    lines = content.platform_logs(task("ours", tenant=PLATFORM, completed_at=at(1)), attempts)
    assert "lease silent" in lines
    view, filter_text, _ = logs.calls[0]
    assert view == VIEW
    # MUTATION: a filter without the service clause reads every container in the view.
    assert 'resource.type="cloud_run_revision"' in filter_text
    assert 'resource.labels.service_name=("swarm-api" OR "swarm-scheduler" OR "swarm-reconciler")' \
        in filter_text
    # MUTATION: pass an attempt id through unchecked and it rewrites the filter.
    assert '"att_1"' in filter_text and "OR true" not in filter_text


def test_a_ci_excerpt_comes_only_from_the_reader_the_caller_names():
    content = fd.ApiContent(inspection=FakeInspection(""), log_view=None)
    assert content.ci_excerpt(task("ours", tenant=PLATFORM)) is None
    named = fd.ApiContent(inspection=FakeInspection(""), log_view=None,
                          ci_excerpt=lambda t: f"CI red for {t['id']}")
    assert named.ci_excerpt(task("ours", tenant=PLATFORM)) == "CI red for ours"
