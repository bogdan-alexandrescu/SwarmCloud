"""The agent's notice when the gate could not make a start click
(lane QA-DISCOVER, part of #1933; VQ8-START-CLICKS's question,
task_f16173840fc34010b91d).

The live gate ends a sign-in `qa_sign_in_start_click_missing` when a start
click's text does not name exactly one visible control, and its status
carries a `sentence` naming that text and how many it found. The notice the
agent reads (and `swarm-handoff sign-in`'s refusal) says that sentence,
because "the sign-in is unavailable" sends the agent, and the person reading
its answer, looking for the wrong fault.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_worker import qa_target
from agent_worker.livegate import qatarget as gate_qt

CAUSE = "qa_sign_in_start_click_missing"
SENTENCE = ("the sign-in's start click 'Staging' found none visible controls with exactly that "
            "text in 15 s, and the gate clicks only when there is exactly one")


class _Log:
    def info(self, *a, **k) -> None:
        pass

    def warning(self, *a, **k) -> None:
        pass


def _signin() -> qa_target.QaTargetSignIn:
    cfg = SimpleNamespace(runner_profile=qa_target.PROFILE, tenant_id="eng", task_id="task_1",
                          attempt_id="att_1", lease_id="lease_1", generation=1)
    return qa_target.QaTargetSignIn(cfg, logger=_Log())


def test_the_cause_is_the_gates_own_code():
    # The worker's table and the gate's constant are one word.
    assert gate_qt.START_CLICK_MISSING == CAUSE


def test_the_start_click_cause_has_its_own_reason():
    # MUTATION: drop the `_WHY` entry and this is the generic "sign-in is unavailable".
    assert qa_target.reason(CAUSE) != qa_target.reason("qa_no_such_cause")
    assert "start click" in qa_target.reason(CAUSE)


def test_the_notice_says_the_gates_sentence_when_its_status_carries_one():
    signin = _signin()
    signin.has_target = True
    signin._apply({"qa_sign_in": "failed", "cause": CAUSE, "sentence": SENTENCE})
    text = signin.notice()
    assert text is not None
    # MUTATION: ignore the status's `sentence` and the notice says only the table's reason.
    assert SENTENCE in text
    assert CAUSE in text


def test_without_a_sentence_the_notice_says_the_tables_reason():
    signin = _signin()
    signin.has_target = True
    signin._apply({"qa_sign_in": "failed", "cause": CAUSE})
    text = signin.notice()
    assert text is not None
    # MUTATION: keep a sentence from an earlier status and it would show here.
    assert qa_target.reason(CAUSE) in text


def test_a_sentence_that_is_not_one_plain_line_is_not_said():
    signin = _signin()
    signin.has_target = True
    signin._apply({"qa_sign_in": "failed", "cause": CAUSE, "sentence": "two\nlines `x`"})
    text = signin.notice() or ""
    # MUTATION: take any string as the sentence and a second line reaches the agent's notice.
    assert "two\nlines" not in text
    assert qa_target.reason(CAUSE) in text


def test_a_new_status_without_a_sentence_drops_the_old_one():
    signin = _signin()
    signin.has_target = True
    signin._apply({"qa_sign_in": "failed", "cause": CAUSE, "sentence": SENTENCE})
    signin._apply({"qa_sign_in": "failed", "cause": "qa_sign_in_failed"})
    # MUTATION: keep `sentence` across statuses and another cause's notice names a start click.
    assert SENTENCE not in (signin.notice() or "")
