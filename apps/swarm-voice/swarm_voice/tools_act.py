"""The planner's act tools (docs/design/voice-it.md §3.9: "File, start, approve,
answer, schedule, cancel ... act tools, each after the ledger reserves it").

  answer_question  answer one of an agent's questions: an option label, the
                   person's own words, or both

BGD's pick, 6 November 2026 (the option-designs lane): Voice it answers an
agent's question through its own act tool, sending `via: "voice-it"`.

READ BACK, THEN ANSWER. The first call for an answer sends nothing: it reads
the question through the grant (`GET /v1/tasks/{task_id}/questions`, on the
read list), checks the index, the option label and whether the person may
answer, holds the answer in the session's act ledger (actledger.py) and hands
the planner the read-back to speak ("Question 2, 'Keep the old route?': answer
'drop'. Shall I send it?"). Only a call in a LATER turn, with exactly that
answer, reserves it in the ledger and posts it; the outcome is recorded. A
question this session already answered is refused unless the person said to
change it (actledger.says_change).

ONE ROUTE. The answer goes to `POST /v1/tasks/{task_id}/answers?reply=short`,
the one act route the session grant reaches for it
(swarm_api/voice/grant.py `WRITE_ROUTES`), with `via: "voice-it"` and the
voice session's id as `session`. Who may answer is the route's rule, unchanged:
the task's submitter or an admin, never a viewer; the grant carries the
session's person and none of their platform-admin powers. An error names the
route and the HTTP status, never a body.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import actledger
from .tools_read import ApiRefused, BadInput, SwarmApi

ANSWER_QUESTION = "answer_question"
NAMES: tuple[str, ...] = (ANSWER_QUESTION,)

QUESTIONS_ROUTE = "/v1/tasks/{task_id}/questions"
ANSWER_ROUTE = "/v1/tasks/{task_id}/answers"

#: What the answers route stores as where it was answered (agent_questions.AnswerBody.via).
VIA = "voice-it"

#: The route's own limits (agent_questions.MAX_ANSWER_CHARS), checked here first.
TEXT_MAX = 2_000
CHOICE_MAX = 200
MAX_INDEX = 99

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

#: The refusals are fixed sentences: the planner returns a refusal outside the
#: <data> fence, so no option label, may_answer_because or count an agent or
#: swarm-api wrote may reach it. The planner re-reads the fenced agent_questions.
NOT_AN_OPTION = "choice is not one of the question's option labels; read agent_questions"
MAY_NOT_ANSWER = "the person may not answer this agent's questions"
NO_QUESTION = "question_index names no question; read agent_questions"

READ_BACK_FIRST = "read_back_first"
RECORDED = "recorded"


def tool_schemas() -> list[dict[str, Any]]:
    return [{
        "name": ANSWER_QUESTION,
        "description": (
            "Answer one of an agent's questions for the person. First call: nothing is sent; "
            "you get the read-back to say with `speak` and must wait for the person to confirm. "
            "After they confirm, call again in your next turn with exactly the same answer to "
            "send it. choice is one of the question's option labels, text the person's own "
            "words; either may be null, not both. change is true only when the person said to "
            "change an answer given earlier in this conversation."),
        "strict": True,
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "task_id": {"type": "string", "description": "The agent (task) id."},
                "question_index": {"type": "integer",
                                   "description": "The question's index, from 0."},
                "choice": {"type": ["string", "null"],
                           "description": "One of the question's option labels, or null."},
                "text": {"type": ["string", "null"],
                         "description": "The person's own words, or null."},
                "change": {"type": "boolean",
                           "description": "True only when the person said to change their answer."},
            },
            "required": ["task_id", "question_index", "choice", "text", "change"],
        },
    }]


def is_act_tool(name: str) -> bool:
    return name in NAMES


def _answer_of(args: Any) -> tuple[str, int, str | None, str | None, bool]:
    if not isinstance(args, dict):
        raise BadInput("a tool's input is an object")
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id) or ".." in task_id:
        raise BadInput("task_id is an id")
    index = args.get("question_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_INDEX:
        raise BadInput(f"question_index is 0 to {MAX_INDEX}")
    choice, text = args.get("choice"), args.get("text")
    if choice is not None and (not isinstance(choice, str) or len(choice) > CHOICE_MAX):
        raise BadInput("choice is null or one of the question's option labels")
    if text is not None and (not isinstance(text, str) or len(text) > TEXT_MAX):
        raise BadInput(f"text is null or at most {TEXT_MAX} characters")
    choice = choice.strip() or None if isinstance(choice, str) else None
    text = text.strip() or None if isinstance(text, str) else None
    if choice is None and text is None:
        raise BadInput("an answer is a choice, the person's words, or both")
    change = args.get("change")
    if not isinstance(change, bool):
        raise BadInput("change is true or false")
    return task_id, index, choice, text, change


async def _question(api: SwarmApi, task_id: str, index: int, choice: str | None) -> dict[str, Any]:
    path = QUESTIONS_ROUTE.format(task_id=task_id)
    body = await api.get(QUESTIONS_ROUTE, path)
    body = body if isinstance(body, dict) else {}
    if body.get("may_answer") is False:
        raise BadInput(MAY_NOT_ANSWER)
    questions = body.get("questions") if isinstance(body.get("questions"), list) else []
    if not 0 <= index < len(questions) or not isinstance(questions[index], dict):
        raise BadInput(NO_QUESTION)
    question = questions[index]
    labels = [str(o.get("label")) for o in question.get("options") or []
              if isinstance(o, dict) and o.get("label")]
    if choice is not None and choice not in labels:
        raise BadInput(NOT_AN_OPTION)
    return {"question": str(question.get("question") or ""), "options": labels,
            "answered": question.get("answer") is not None}


def _read_back(index: int, question: str, choice: str | None, text: str | None) -> str:
    said = " and ".join(part for part in (
        f"'{choice}'" if choice else "", f"\"{text}\"" if text else "") if part)
    return f"Question {index + 1}, '{question}': answer {said}. Shall I send it?"


async def answer_question(api: SwarmApi, ledger: actledger.ActLedger, args: Any, *,
                          transcript: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """Read back, or send, one answer (module docstring). `BadInput`,
    `ApiRefused` or `actledger.ActRefused` when nothing was sent."""
    task_id, index, choice, text, change = _answer_of(args)
    key, what = (task_id, str(index)), (choice, text)
    transcript = list(transcript)
    if not ledger.confirmable(actledger.ANSWER, key, what, transcript):
        question = await _question(api, task_id, index, choice)
        read_back = _read_back(index, question["question"], choice, text)
        ledger.read_back(actledger.ANSWER, key, what, change=change, transcript=transcript,
                         detail={"task_id": task_id, "question_index": index})
        return {"status": READ_BACK_FIRST, "task_id": task_id, "question_index": index,
                "read_back": read_back,
                "already_answered": question["answered"],
                "next": "Say the read-back with speak and end the turn. Send it only after the "
                        "person confirms, by calling again with exactly this answer."}
    act = ledger.reserve(actledger.ANSWER, key, what, transcript)
    path = ANSWER_ROUTE.format(task_id=task_id)
    payload = {"question_index": index, "choice": choice, "text": text, "via": VIA,
               "session": api.session_id}
    try:
        body = await api._call("POST", ANSWER_ROUTE, path, params={"reply": "short"},
                               json_body=payload)
    except ApiRefused as exc:
        ledger.record(act, ok=False, detail={"status": exc.status})
        raise
    body = body if isinstance(body, dict) else {}
    ledger.record(act, ok=True, detail={"recorded_at": body.get("recorded_at")})
    return {"status": RECORDED, "task_id": task_id, "question_index": index,
            "recorded_at": body.get("recorded_at"), "by": body.get("by"), "via": VIA}
