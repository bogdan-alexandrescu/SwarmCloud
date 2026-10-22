"""`/answer <n> <option>` on the agent's issue (lane AU-7, owner decision Q5).

docs/design/agent-autonomy.md §3.9. An agent parked on a blocking question
whose task is an issue run's gets ONE comment on that issue: each question
with its options, recommended first, which block, and the reply syntax. A
reply `/answer <n> <option>` is the console's answer when -- and only when --
`ghapprove` maps its author to a member of the task's workspace who may
answer it, and the option is one of that question's labels. Anything else
is ignored and recorded (`answer_comment_ignored`), never acted on.

No credentials, no network, no emulator: the GitHub fakes S5's tests use.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import agent_questions, forgeapp, ghapprove
from swarm_api.validation import OWNER_ANSWERS_METADATA_KEY

from .test_approvals import (  # noqa: F401 -- S5's fixtures, used by name
    _grants,
    _planned,
    api_context,
    built_types,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .conftest import seed_tenant

TASK = "task_asks"
ISSUE = 42


def _connect(db, email: str, login: str, user_id: int) -> None:
    conn_id = forgeapp.connection_id_for("eng", email)
    db.docs[f"{forgeapp.CONNECTIONS}/{conn_id}"] = {
        "connection_id": conn_id, "tenant_id": "eng", "user": email, "forge": "github",
        "forge_login": login, "forge_user_id": user_id, "state": forgeapp.ACTIVE,
    }


def _question(text: str, *, blocking: bool) -> dict:
    return {
        "question": text,
        "options": [
            {"label": "drop", "description": "one route"},
            {"label": "keep", "description": "the old route stays a release"},
        ],
        "recommended": "keep",
        "context": None,
        "blocking": blocking,
    }


@pytest.fixture
def parked(client, db, objects):
    seed_tenant(db, "eng")
    _connect(db, "alice@example.com", "alice-gh", 1001)
    _connect(db, "dave@example.com", "dave-gh", 1004)
    run = _planned(client, db, objects)
    doc = {
        "id": TASK, "tenant_id": "eng", "state": "PARKED", "park_reason": "HUMAN_REQUIRED",
        "submitted_by": "alice@example.com", "runner_profile": "mock", "resource_class": "standard",
        "metadata": {"issue_run": run["id"]}, "attempt_count": 1, "max_attempts": 3,
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "next_eligible_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "current_lease_id": None, "cancel_requested": False, "depends_on": [],
        "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": {
            "questions": [_question("Keep the old route?", blocking=True),
                          _question("Rename the flag?", blocking=False)],
            "blocking": [0], "attempt_id": "att_1", "asked_at": "2026-10-01T00:00:00+00:00"}}],
    }
    db.docs[f"tasks/{TASK}"] = doc
    return run


def _reply(writes, body: str, *, login: str, user_id: int | None) -> int:
    cid = writes.add_comment(ISSUE, body, login=login)
    stamp = "2026-10-03T13:00:00Z"
    writes.comments[cid].update({"user": {"login": login, "id": user_id, "type": "User"},
                                 "created_at": stamp, "updated_at": stamp,
                                 "performed_via_github_app": None})
    return cid


def _ours(writes) -> list[str]:
    return [c["body"] for c in writes.on_issue(ISSUE) if c["body"].startswith("<!-- swarmcloud-question:")]


def _task(db) -> dict:
    return db.docs[f"tasks/{TASK}"]


def _wait(db) -> dict:
    return _task(db)["metadata"][agent_questions.QUESTION_WAIT_KEY]


def test_the_questions_are_posted_once_with_options_and_the_reply_syntax(api_context, db, writes, parked):
    counts = ghapprove.poll_answers(api_context)
    ghapprove.poll_answers(api_context)

    # MUTATION: post on every tick and the issue fills with copies.
    (body,) = _ours(writes)
    assert counts["questions_posted"] == 1
    assert "1. Keep the old route? **(blocking)**" in body
    # MUTATION: list options in file order and the recommended one is not first.
    first, second = [line for line in body.splitlines() if line.strip().startswith("- `")][:2]
    assert "`keep` _(recommended)_" in first and "`drop`" in second
    # MUTATION: drop the syntax line and nobody knows how to reply.
    assert f"{ghapprove.ANSWER_COMMAND} <question number> <option>" in body
    # The owner is named by GitHub login, never by address.
    assert "@alice-gh" in body and "alice@example.com" not in body
    assert _wait(db)["comment"]["id"]


def test_a_members_answer_is_stored_and_the_last_blocking_one_resumes_the_agent(api_context, db, writes, parked):
    ghapprove.poll_answers(api_context)
    _reply(writes, "/answer 1 drop", login="alice-gh", user_id=1001)

    counts = ghapprove.poll_answers(api_context)

    # MUTATION: never call `answer_question` and the reply changes nothing.
    assert counts["answered"] == 1, counts
    (stored,) = _task(db)["metadata"][OWNER_ANSWERS_METADATA_KEY]
    assert stored["choice"] == "drop" and stored["via"] == ghapprove.ANSWER_VIA
    assert stored["by"] == "alice@example.com"
    # The one blocking question is answered: the agent is READY, nothing leased.
    assert _task(db)["state"] == "READY"
    assert _task(db)["current_lease_id"] is None


def test_an_answer_is_read_once(api_context, db, writes, parked):
    ghapprove.poll_answers(api_context)
    _reply(writes, "/answer 2 keep", login="alice-gh", user_id=1001)
    ghapprove.poll_answers(api_context)

    # MUTATION: never move the cursor and the same reply is answered every tick.
    assert ghapprove.poll_answers(api_context)["answered"] == 0


def test_an_answer_from_a_login_no_member_connected_is_ignored_and_recorded(api_context, db, writes, parked):
    ghapprove.poll_answers(api_context)
    cid = _reply(writes, "/answer 1 drop", login="stranger-gh", user_id=99)

    counts = ghapprove.poll_answers(api_context)

    # MUTATION: accept any login and a stranger resumes the agent.
    assert counts["answered"] == 0 and counts["answers_ignored"] == 1
    assert _task(db)["state"] == "PARKED"
    assert not _task(db)["metadata"].get(OWNER_ANSWERS_METADATA_KEY)
    # MUTATION: skip `_ignore` and nothing says the reply was seen.
    (ignored,) = _wait(db)["ignored"]
    assert ignored["comment_id"] == cid and ignored["login"] == "stranger-gh"
    assert "connected" in ignored["reason"]


def test_a_member_who_may_not_answer_this_agent_is_ignored(api_context, db, writes, parked):
    ghapprove.poll_answers(api_context)
    _reply(writes, "/answer 1 drop", login="dave-gh", user_id=1004)

    counts = ghapprove.poll_answers(api_context)

    # MUTATION: make `agent_questions.answer_refusal` return None and any member answers
    # another's agent (`_verify_answerer` and `answer_question` both ask it).
    assert counts["answers_ignored"] == 1
    assert _task(db)["state"] == "PARKED"
    assert "launched" in _wait(db)["ignored"][0]["reason"]


@pytest.mark.parametrize("body", ["/answer 1 maybe", "/answer 3 keep", "/answer 1 Drop"])
def test_an_option_that_is_not_one_of_the_labels_is_ignored(api_context, db, writes, parked, body):
    ghapprove.poll_answers(api_context)
    _reply(writes, body, login="alice-gh", user_id=1001)

    counts = ghapprove.poll_answers(api_context)

    # MUTATION: take the nearest label and an option the owner never chose is stored.
    assert counts["answers_ignored"] == 1, counts
    assert _task(db)["state"] == "PARKED"
    assert not _task(db)["metadata"].get(OWNER_ANSWERS_METADATA_KEY)


def test_a_label_redaction_masks_is_ignored_as_not_a_served_label(api_context, db, writes, parked):
    masked = "password=" + "q" * 12
    wait = _task(db)["blocked_by"][0]["question_wait"]
    wait["questions"][0]["options"][0]["label"] = masked
    ghapprove.poll_answers(api_context)
    _reply(writes, f"/answer 1 {masked}", login="alice-gh", user_id=1001)

    counts = ghapprove.poll_answers(api_context)

    assert counts["answers_ignored"] == 1, counts
    assert not _task(db)["metadata"].get(OWNER_ANSWERS_METADATA_KEY)
    # MUTATION: check the raw labels and the store refuses it instead, with its own sentence.
    assert _wait(db)["ignored"][0]["reason"] == "the option is not one of question 1's labels"


def test_only_a_first_line_that_is_the_command_is_an_answer():
    assert ghapprove.parse_answer("/answer 1 keep") == (1, "keep")
    assert ghapprove.parse_answer('\n /ANSWER 2 "per task" \nthanks') == (2, "per task")
    # MUTATION: search every line and a quoted reply answers again.
    assert ghapprove.parse_answer("> /answer 1 keep") is None
    assert ghapprove.parse_answer("lgtm\n/answer 1 keep") is None
    assert ghapprove.parse_answer("/answer one keep") is None
    assert ghapprove.parse_answer(f"{ghapprove.question_marker('t', 'a')}\n/answer 1 keep") is None
