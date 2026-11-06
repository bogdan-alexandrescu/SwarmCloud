"""An option's design preview is served with its question, bounded and redacted.

BGD's answer on the full-page interview lane, 6 November 2026 ('Serve
preview'). A plugin step's agent may give each option of a question a
`preview`: the design HTML it wrote. The worker records it in the wait's
bridge block (`agent_worker.runners.question_bridge.wait_record`), and
`GET /v1/tasks/{id}/questions` serves it on the option it belongs to:

  1. a preview is served on its option;
  2. one longer than `PREVIEW_MAX_CHARS`, or one that would take its
     question's previews past `PREVIEW_QUESTION_MAX_CHARS`, is dropped whole
     with a `preview_dropped` reason -- never cut mid-tag;
  3. a preview passes the same redaction as the question's text.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone

from swarm_api import agent_questions

from .conftest import auth_header, seed_task, seed_tenant

TASK = "task_previews"
OWNER = "alice@example.com"
LABELS = ("Cards", "Table", "Timeline")


def _parked(db, previews: dict[str, object], labels: tuple[str, ...] = LABELS) -> None:
    seed_tenant(db, "eng")
    moment = datetime.now(timezone.utc)
    options = [{"label": label, "description": f"the {label.lower()} layout"} for label in labels]
    wait = {
        "questions": [{
            "question": "Which layout should the dashboard use?",
            "options": options,
            "recommended": "Cards",
            "context": None,
            "blocking": True,
        }],
        "blocking": [0],
        "attempt_id": "att_1",
        "asked_at": moment.isoformat(),
        "bridge": {
            "session_id": str(uuid.uuid4()),
            "tool_use_id": "toolu_" + uuid.uuid4().hex[:12],
            "generation": 3,
            "source_revision": "a" * 40,
            "questions": [{
                "question": "Which layout should the dashboard use?",
                "header": "Layout",
                "multi_select": False,
                "recommended": "Cards",
                "options": [{**o, "preview": previews.get(o["label"])} for o in options],
            }],
        },
    }
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED", park_reason="HUMAN_REQUIRED",
                    next_eligible_at=moment)
    doc["submitted_by"] = OWNER
    doc["attempt_count"] = 1
    doc["updated_at"] = moment
    doc["blocked_by"] = [{"reason": "HUMAN_REQUIRED", "park_phase": "question_wait", "question_wait": wait}]


def _options(client) -> dict[str, dict]:
    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()
    assert body["status"] == "ok", body
    return {o["label"]: o for o in body["questions"][0]["options"]}


def _html(chars: int) -> str:
    """A well-formed design of exactly `chars` characters."""
    head, tail = "<section>", "</section>"
    return head + "x" * (chars - len(head) - len(tail)) + tail


def test_an_options_preview_is_served_on_that_option(client, db) -> None:
    design = '<div class="cards"><h2>Builds</h2><p>3 running</p></div>'
    _parked(db, {"Cards": design})

    options = _options(client)

    assert options["Cards"]["preview"] == design
    assert options["Cards"].get("preview_dropped") is None
    # An option the agent gave no preview has none.
    assert options["Table"].get("preview") is None


def test_a_preview_over_the_option_cap_is_dropped_whole_with_its_reason(client, db) -> None:
    big = _html(agent_questions.PREVIEW_MAX_CHARS + 1)
    fits = _html(agent_questions.PREVIEW_MAX_CHARS)
    _parked(db, {"Cards": big, "Table": fits})

    options = _options(client)

    assert options["Cards"]["preview"] is None
    assert "larger than" in options["Cards"]["preview_dropped"]
    # Dropped, never truncated: no part of it is served anywhere.
    assert "<section>xxxxxxxx" not in json.dumps(options["Cards"])
    assert options["Table"]["preview"] == fits


def test_previews_past_the_question_cap_are_dropped_with_their_reason(client, db) -> None:
    each = agent_questions.PREVIEW_MAX_CHARS
    fit = agent_questions.PREVIEW_QUESTION_MAX_CHARS // each
    labels = tuple(f"Layout {n}" for n in range(fit + 1))
    _parked(db, {label: _html(each) for label in labels}, labels)

    options = _options(client)

    for label in labels[:fit]:
        assert options[label]["preview"] == _html(each), label
    last = options[labels[fit]]
    assert last["preview"] is None
    assert "question" in last["preview_dropped"]


def test_a_preview_holding_a_token_is_redacted_like_the_question(client, db) -> None:
    token = "gh" + "p_" + secrets.token_hex(18)
    _parked(db, {"Cards": f"<pre>export GITHUB_TOKEN={token}</pre>"})

    body = client.get(f"/v1/tasks/{TASK}/questions", headers=auth_header("alice")).json()

    assert token not in json.dumps(body)
    assert body["questions"][0]["options"][0]["preview"].startswith("<pre>")
    assert body["redacted"] is True and body["redaction_count"] >= 1


def test_a_preview_that_is_not_text_is_not_served(client, db) -> None:
    _parked(db, {"Cards": {"html": "<b>x</b>"}, "Table": 7})

    options = _options(client)

    assert options["Cards"].get("preview") is None
    assert options["Table"].get("preview") is None


def test_a_preview_changes_nothing_an_answer_is_checked_against(client, db) -> None:
    # The digest an answer is stored under reads the text and the options'
    # labels and descriptions only, so a preview never re-asks a question.
    _parked(db, {"Cards": "<b>cards</b>"})
    response = client.post(f"/v1/tasks/{TASK}/answers", json={"question_index": 0, "choice": "Cards"},
                           headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    served = response.json()["questions"][0]
    assert served["answer"]["choice"] == "Cards"
    assert served["options"][0]["preview"] == "<b>cards</b>"
