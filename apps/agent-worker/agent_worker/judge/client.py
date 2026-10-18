"""One Jev call, made safely: validated, sanitised, budgeted, retried, audited.

The wire shape is jev-browser 0.2.0's `src/jev.mjs`, the one Jev client the
platform ships: `POST {state, model, questions}`, where `questions` maps a
name to `{type: "noul" | "choice" | "score", instructions, criteria?}`, and
the answer is `{answers: {<name>: ...}, usage: {input_tokens}}`.

Measured quirks of the Internal decision API (owner brief, 2026-10-14):
`state` must be a STRING, so the Internal backends get it JSON-stringified;
`choice` and `score` refuse a question without `criteria`, and `score` wants
a list. Those are checked here, before anything is sent, because a refused
question still costs a call against the budget.

§5.5 is the contract: whatever happens, `decide` RETURNS. A timeout, a 429,
a 5xx, a refusal, a malformed answer, an open breaker or a spent budget is a
`Decision` with no answers, and the caller keeps its local order.
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from agent_worker.judge import sanitize
from agent_worker.judge.budget import Ledger
from agent_worker.judge.settings import Backend

#: §5.5: 10 s per call, against jev.mjs's 60 s default. A lane waits on these.
CALL_TIMEOUT_SECONDS = 10
#: §5.5: up to two retries, 0.8 s x n apart, on a timeout, 429 or 5xx.
RETRIES = 2
BACKOFF_SECONDS = 0.8
#: jev-browser's `session.mjs` MAX_STATE_CHARS: the request cap is 32,768
#: input tokens, and a state past this is refused rather than truncated.
MAX_STATE_CHARS = 70_000
#: The API's own limit on a choice's options.
MAX_CHOICE_OPTIONS = 255
MAX_QUESTIONS = 64
MAX_INSTRUCTIONS_CHARS = 4_000
QUESTION_TYPES = ("noul", "choice", "score")
_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")

Transport = Callable[[str, bytes, Mapping[str, str], float], "tuple[int, bytes]"]


class QuestionError(ValueError):
    """A question the API would refuse. Raised before anything is sent; the
    message never quotes the question's text."""


@dataclass(frozen=True)
class Decision:
    #: "ok", "failed" (transport or HTTP), "malformed" or "skipped".
    outcome: str
    answers: dict[str, Any] | None = None
    #: Why a call was skipped or failed: `breaker_open`, `budget_calls`,
    #: `budget_tokens`, `budget_monthly`, `oversize`, `empty_state`,
    #: `http_<status>`, `transport`, `malformed`.
    reason: str | None = None
    input_tokens: int = 0
    call_id: str = ""
    #: Strings §5.4 dropped and objects it reduced to their names.
    dropped: int = 0
    denied: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


#: The worker's TypeSafe shim is on loopback: never through a proxy the
#: environment names (urllib reads HTTP_PROXY for http:// with no NO_PROXY).
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(url: str) -> Callable[..., Any]:
    return _DIRECT.open if urllib.parse.urlsplit(url).hostname == "127.0.0.1" \
        else urllib.request.urlopen


def urllib_transport(url: str, body: bytes, headers: Mapping[str, str],
                     timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with _opener(url)(request, timeout=timeout) as response:  # noqa: S310 -- a fixed https URL, the deployment's Internal URL or the worker's loopback shim
            return response.status, response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _stderr_log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr)


def validate_questions(questions: Any) -> dict[str, dict[str, Any]]:
    """The questions in the API's shape, or a QuestionError naming the problem."""
    if not isinstance(questions, Mapping) or not questions:
        raise QuestionError("questions must be an object with at least one question")
    if len(questions) > MAX_QUESTIONS:
        raise QuestionError(f"at most {MAX_QUESTIONS} questions per call")
    out: dict[str, dict[str, Any]] = {}
    for name, question in questions.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise QuestionError("a question name is 1-64 letters, digits or _")
        if not isinstance(question, Mapping):
            raise QuestionError(f"question {name} must be an object")
        kind = question.get("type")
        if kind not in QUESTION_TYPES:
            raise QuestionError(f"question {name}: type must be one of {', '.join(QUESTION_TYPES)}")
        instructions = question.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise QuestionError(f"question {name} needs instructions")
        if len(instructions) > MAX_INSTRUCTIONS_CHARS:
            raise QuestionError(f"question {name}: instructions over {MAX_INSTRUCTIONS_CHARS} chars")
        fixed: dict[str, Any] = {"type": kind, "instructions": instructions}
        criteria = question.get("criteria")
        if kind == "choice":
            if isinstance(criteria, list):
                criteria = {o: None for o in criteria}
            if not isinstance(criteria, Mapping) or not criteria:
                raise QuestionError(f"choice question {name} needs criteria: its options")
            if len(criteria) > MAX_CHOICE_OPTIONS:
                raise QuestionError(f"choice question {name}: at most {MAX_CHOICE_OPTIONS} options")
            if not all(isinstance(k, str) and k and (v is None or isinstance(v, str))
                       for k, v in criteria.items()):
                raise QuestionError(f"choice question {name}: criteria map option to text or null")
            fixed["criteria"] = dict(criteria)
        elif kind == "score":
            if not isinstance(criteria, list) or not criteria or \
                    not all(isinstance(c, str) and c.strip() for c in criteria):
                raise QuestionError(f"score question {name} needs criteria as a list of strings")
            fixed["criteria"] = list(criteria)
        out[name] = fixed
    return out


def _question_strings(questions: Mapping[str, Mapping[str, Any]]) -> Iterable[tuple[str, str]]:
    for name, question in questions.items():
        yield name, question["instructions"]
        criteria = question.get("criteria")
        if isinstance(criteria, Mapping):
            for option, text in criteria.items():
                yield name, option
                if text:
                    yield name, text
        elif isinstance(criteria, list):
            for text in criteria:
                yield name, text


def _probability(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and 0.0 <= value <= 1.0


def _numbers_only(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return bool(value) and all(_numbers_only(v) for v in value.values())
    if isinstance(value, list):
        return bool(value) and all(_numbers_only(v) for v in value)
    return False


def _valid_answer(question: Mapping[str, Any], answer: Any) -> bool:
    if not isinstance(answer, Mapping):
        return False
    kind = question["type"]
    if kind == "noul":
        return _probability(answer.get("noul"))
    if kind == "choice":
        probabilities = answer.get("probabilities")
        return (answer.get("choice") in question["criteria"]
                and isinstance(probabilities, Mapping)
                and all(_probability(p) for p in probabilities.values())
                and _probability(answer.get("confidence")))
    # score: the answer's exact field names are not pinned by jev.mjs, so a
    # score answer is accepted when every value in it is a number.
    return _numbers_only(dict(answer))


@dataclass
class JudgeClient:
    backend: Backend
    ledger: Ledger
    transport: Transport = urllib_transport
    sleep: Callable[[float], None] = time.sleep
    log: Callable[..., None] = _stderr_log
    #: The repository's `hard_stop_paths` (§5.4 step 1).
    hard_stop_paths: tuple[str, ...] = ()
    #: The task's registered secret values (§5.4 step 3).
    secrets: tuple[str, ...] = field(default=(), repr=False)
    clock: Callable[[], float] = time.monotonic

    def decide(self, kind: str, state: Any, questions: Any) -> Decision:
        """Ask Jev; never raise for anything but a QuestionError."""
        asked = validate_questions(questions)
        secrets = self.secrets + ((self.backend.key,) if self.backend.key else ())
        for name, text in _question_strings(asked):
            if sanitize.is_flagged(text, secrets):
                raise QuestionError(f"question {name} holds credential-shaped text; nothing was sent")
        clean = sanitize.sanitize_state(state, hard_stop_paths=self.hard_stop_paths,
                                        secrets=secrets)
        call_id = uuid.uuid4().hex[:12]

        def finish(outcome: str, *, reason: str | None = None, answers: Any = None,
                   tokens: int = 0, size: int = 0, ms: int = 0,
                   succeeded: bool | None = None) -> Decision:
            opened = self.ledger.record({"call_id": call_id, "kind": kind,
                                         "backend": self.backend.name, "bytes": size,
                                         "input_tokens": tokens, "ms": ms,
                                         # a skip says why, so the pack can
                                         "outcome": f"skipped:{reason}" if outcome == "skipped"
                                         else outcome},
                                        succeeded=succeeded)
            if opened:
                self.log("jev_unavailable", backend=self.backend.name, call_id=call_id)
            return Decision(outcome, answers, reason, tokens, call_id, clean.dropped, clean.denied)

        if clean.state is None or clean.state == {} or clean.state == []:
            return finish("skipped", reason="empty_state")
        if self.backend.stringify_state and not isinstance(clean.state, str):
            sent_state: Any = json.dumps(clean.state, separators=(",", ":"), sort_keys=True)
        else:
            sent_state = clean.state
        state_chars = len(sent_state) if isinstance(sent_state, str) else \
            len(json.dumps(sent_state, separators=(",", ":")))
        if state_chars > MAX_STATE_CHARS:
            return finish("skipped", reason="oversize")
        body = json.dumps({"state": sent_state, "model": self.backend.model,
                           "questions": asked}, separators=(",", ":")).encode()
        refused = self.ledger.admit(estimate=len(body) // 3)
        if refused is not None:
            return finish("skipped", reason=refused)

        headers = {"Content-Type": "application/json"}
        if self.backend.key:
            headers["Authorization"] = f"Bearer {self.backend.key}"
        started = self.clock()
        status, raw = 0, b""
        for attempt in range(RETRIES + 1):
            try:
                status, raw = self.transport(self.backend.url, body, headers,
                                             CALL_TIMEOUT_SECONDS)
            except Exception:  # a timeout, a refused connection, a reset: retryable
                status, raw = 0, b""
            if 200 <= status < 300:
                break
            if status and status != 429 and status < 500:
                break  # the API refused the request; asking again changes nothing
            if attempt < RETRIES:
                self.sleep(BACKOFF_SECONDS * (attempt + 1))
        ms = int((self.clock() - started) * 1000)
        if not 200 <= status < 300:
            return finish("failed", reason=f"http_{status}" if status else "transport",
                          size=len(body), ms=ms, succeeded=False)
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        usage = parsed.get("usage") if isinstance(parsed, dict) else None
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) \
            and tokens >= 0 else 0
        answers = parsed.get("answers") if isinstance(parsed, dict) else None
        if not isinstance(answers, dict) or \
                not all(_valid_answer(q, answers.get(n)) for n, q in asked.items()):
            return finish("malformed", reason="malformed", tokens=tokens, size=len(body),
                          ms=ms, succeeded=False)
        return finish("ok", answers={n: answers[n] for n in asked}, tokens=tokens,
                      size=len(body), ms=ms, succeeded=True)
