"""The worker's Jev client against a fake Jev (context-quality.md §5.2, §5.3, §5.5).

A Jev problem can make a pack less well ordered; it can never fail, park or
delay an attempt beyond its retries. Each test below holds one row of §5.5.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import budget, client, settings, shapes


class FakeJev:
    """Answers from a script: an int is an HTTP status, an exception is raised,
    a dict is a 200 body. Records every request it was sent."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float):
        self.requests.append({"url": url, "body": json.loads(body), "headers": headers,
                              "timeout": timeout})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, int):
            return step, b'{"error":"x"}'
        return 200, json.dumps(step).encode()


def _yes(names: list[str], p: float = 0.9, tokens: int = 1000) -> dict[str, Any]:
    return {"answers": {n: {"noul": p} for n in names}, "usage": {"input_tokens": tokens}}


QUESTIONS = {"q": {"type": "noul", "instructions": "Is `x` relevant?"}}


def _client(tmp_path: Path, fake: FakeJev, *, backend: str = "typesafe",
            key: str | None = None, logs: list | None = None, **ledger: Any) -> client.JudgeClient:
    environ = {settings.INTERNAL_URL_ENV: "http://decide.internal:8080/v1/decide"}
    chosen = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True, backend=backend),
                                      key=key if key is not None else "k-" + "x" * 30,
                                      environ=environ)
    assert isinstance(chosen, settings.Backend), chosen
    sink = logs if logs is not None else []
    return client.JudgeClient(
        chosen,
        ledger=budget.Ledger.create(tmp_path / "ledger.json", **ledger),
        transport=fake, sleep=lambda s: None, log=lambda event, **f: sink.append((event, f)),
    )


def test_a_good_answer_is_returned_and_counted(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"], tokens=2850))
    jev = _client(tmp_path, fake)
    got = jev.decide("J1", {"task": {"type": "implement"}}, QUESTIONS)
    assert got.ok and got.answers == {"q": {"noul": 0.9}}
    ledger = jev.ledger.read()
    assert ledger["calls"] == 1 and ledger["input_tokens"] == 2850
    (row,) = ledger["audit"]
    assert set(row) == {"call_id", "kind", "backend", "bytes", "input_tokens", "ms", "outcome"}
    assert row["kind"] == "J1" and row["outcome"] == "ok" and row["input_tokens"] == 2850
    # The audit never holds the payload.
    assert "implement" not in json.dumps(ledger["audit"])


def test_typesafe_sends_the_jev_mjs_shape_with_a_bearer_key(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"]))
    key = "k-" + "y" * 30
    _client(tmp_path, fake, key=key).decide("J1", {"a": 1}, QUESTIONS)
    (sent,) = fake.requests
    assert sent["url"] == "https://api.typesafe.ai/v1/systemone"
    assert sent["body"] == {"state": {"a": 1}, "model": "jev-latest", "questions": QUESTIONS}
    assert sent["headers"]["Authorization"] == f"Bearer {key}"
    assert sent["timeout"] == client.CALL_TIMEOUT_SECONDS == 10


@pytest.mark.parametrize("backend", ["internal-jev", "internal-laya"])
def test_the_internal_api_gets_a_string_state_and_no_credential(tmp_path: Path, backend: str) -> None:
    fake = FakeJev(_yes(["q"]))
    _client(tmp_path, fake, backend=backend).decide("J1", {"a": [1, 2]}, QUESTIONS)
    (sent,) = fake.requests
    assert sent["url"] == "http://decide.internal:8080/v1/decide"
    assert isinstance(sent["body"]["state"], str)
    assert json.loads(sent["body"]["state"]) == {"a": [1, 2]}
    assert "Authorization" not in sent["headers"]


@pytest.mark.parametrize("failure", [TimeoutError("slow"), 429, 503])
def test_timeout_429_and_5xx_retry_twice_then_fall_back(tmp_path: Path, failure: Any) -> None:
    fake = FakeJev(failure)
    waits: list[float] = []
    jev = _client(tmp_path, fake)
    jev.sleep = waits.append
    got = jev.decide("J1", {"a": 1}, QUESTIONS)
    assert not got.ok and got.answers is None and got.outcome == "failed"
    assert len(fake.requests) == 3  # one call and two retries, as jev.mjs does
    assert waits == pytest.approx([0.8, 1.6])
    assert jev.ledger.read()["audit"][-1]["outcome"] == "failed"


def test_a_retry_that_succeeds_is_a_success(tmp_path: Path) -> None:
    fake = FakeJev(503, _yes(["q"]))
    assert _client(tmp_path, fake).decide("J1", {"a": 1}, QUESTIONS).ok


def test_a_400_is_not_retried(tmp_path: Path) -> None:
    fake = FakeJev(400)
    assert not _client(tmp_path, fake).decide("J1", {"a": 1}, QUESTIONS).ok
    assert len(fake.requests) == 1


@pytest.mark.parametrize("body", [
    {"answers": {}},                                        # the question is missing
    {"answers": {"q": {"noul": 1.7}}},                      # not a probability
    {"answers": {"q": {"noul": "yes"}}},
    {"answers": "nope"},
    {"usage": {"input_tokens": 5}},
])
def test_a_malformed_answer_is_a_failure_with_no_partial_use(tmp_path: Path, body: dict) -> None:
    fake = FakeJev(body)
    got = _client(tmp_path, fake).decide("J1", {"a": 1}, QUESTIONS)
    assert not got.ok and got.answers is None and got.outcome == "malformed"


def test_a_choice_outside_its_options_is_malformed(tmp_path: Path) -> None:
    questions = {"c": {"type": "choice", "instructions": "which?",
                       "criteria": {"a": None, "b": None}}}
    fake = FakeJev({"answers": {"c": {"choice": "z", "probabilities": {"z": 1.0},
                                      "confidence": 0.9}}})
    assert _client(tmp_path, fake).decide("J0", {"a": 1}, questions).outcome == "malformed"


def test_two_consecutive_failures_open_the_breaker_and_log_once(tmp_path: Path) -> None:
    logs: list = []
    fake = FakeJev(500)
    jev = _client(tmp_path, fake, logs=logs)
    jev.decide("J1", {"a": 1}, QUESTIONS)
    jev.decide("J1", {"a": 1}, QUESTIONS)
    sent = len(fake.requests)
    third = jev.decide("J1", {"a": 1}, QUESTIONS)
    jev.decide("J1", {"a": 1}, QUESTIONS)
    assert third.outcome == "skipped" and third.reason == "breaker_open"
    assert len(fake.requests) == sent  # nothing more was sent
    assert [e for e, _ in logs].count("jev_unavailable") == 1


def test_a_success_between_failures_resets_the_breaker(tmp_path: Path) -> None:
    fake = FakeJev(400, _yes(["q"]), 400, _yes(["q"]))
    jev = _client(tmp_path, fake)
    outcomes = [jev.decide("J1", {"a": 1}, QUESTIONS).outcome for _ in range(4)]
    assert outcomes == ["failed", "ok", "failed", "ok"]


def test_the_breaker_is_shared_through_the_ledger_file(tmp_path: Path) -> None:
    """The worker and the agent's jev_decide process are two processes on one
    attempt: a breaker opened by one is open for the other."""
    jev = _client(tmp_path, FakeJev(500))
    jev.decide("J1", {"a": 1}, QUESTIONS)
    jev.decide("J1", {"a": 1}, QUESTIONS)
    other = budget.Ledger(tmp_path / "ledger.json")
    assert other.admit(estimate=10) == "breaker_open"


def test_the_ninth_call_is_skipped(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"], tokens=100))
    jev = _client(tmp_path, fake)
    got = [jev.decide("J1", {"a": 1}, QUESTIONS) for _ in range(9)]
    assert [g.ok for g in got] == [True] * 8 + [False]
    assert got[-1].reason == "budget_calls"
    assert len(fake.requests) == 8
    assert budget.MAX_CALLS_PER_TASK == 8


def test_the_token_budget_stops_calls_past_120k(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"], tokens=60_000))
    jev = _client(tmp_path, fake)
    outcomes = [jev.decide("J1", {"a": 1}, QUESTIONS) for _ in range(3)]
    assert [o.ok for o in outcomes] == [True, True, False]
    assert outcomes[-1].reason == "budget_tokens"
    assert budget.MAX_INPUT_TOKENS_PER_TASK == 120_000


def test_the_monthly_cap_stops_calls_before_the_task_budget(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"], tokens=3000))
    jev = _client(tmp_path, fake, monthly_cap_tokens=10_000, month_used_tokens=6_000)
    first = jev.decide("J1", {"a": 1}, QUESTIONS)
    second = jev.decide("J1", {"a": 1}, QUESTIONS)
    assert first.ok and not second.ok and second.reason == "budget_monthly"


def test_a_state_over_jev_input_cap_is_never_sent(tmp_path: Path) -> None:
    fake = FakeJev(_yes(["q"]))
    got = _client(tmp_path, fake).decide("J1", {"blob": "x" * 80_000}, QUESTIONS)
    assert got.reason == "oversize" and fake.requests == []


def test_no_key_for_typesafe_means_no_call() -> None:
    chosen = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True, backend="typesafe"),
                                      key=None, environ={})
    assert isinstance(chosen, settings.Off) and chosen.reason == "no_key"


@pytest.mark.parametrize("given,reason", [
    (settings.JudgeSettings(), "workspace_not_opted_in"),
    (settings.JudgeSettings(code_opt_in=True, repository_override="off"), "repository_off"),
    (settings.JudgeSettings(code_opt_in=False, repository_override="on"), "workspace_not_opted_in"),
])
def test_jev_is_off_unless_the_workspace_opted_in(given: settings.JudgeSettings, reason: str) -> None:
    chosen = settings.resolve_backend(given, key="k-" + "x" * 30,
                                      environ={settings.INTERNAL_URL_ENV: "http://d/v1"})
    assert isinstance(chosen, settings.Off) and chosen.reason == reason


def test_internal_jev_is_the_default_backend_and_needs_its_url() -> None:
    assert settings.JudgeSettings().backend == "internal-jev"
    off = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True), key=None, environ={})
    assert isinstance(off, settings.Off) and off.reason == "no_internal_url"
    on = settings.resolve_backend(settings.JudgeSettings(code_opt_in=True), key=None,
                                  environ={settings.INTERNAL_URL_ENV: "http://d/v1"})
    assert isinstance(on, settings.Backend) and on.key is None and on.stringify_state


def test_settings_parse_a_stored_document_tolerantly() -> None:
    parsed = settings.JudgeSettings.from_mapping(
        {"code_intelligence": {"jev": True, "judgment_backend": "internal-laya",
                               "jev_monthly_cap_tokens": 5_000_000}},
        repository={"jev": "inherit"})
    assert parsed.code_opt_in and parsed.backend == "internal-laya"
    assert parsed.monthly_cap_tokens == 5_000_000
    unknown = settings.JudgeSettings.from_mapping({"code_intelligence": {"jev": "yes",
                                                                         "judgment_backend": "x"}})
    assert not unknown.code_opt_in and unknown.backend == "internal-jev"


def test_a_credential_in_the_state_never_reaches_jev(tmp_path: Path) -> None:
    token = "ghp_" + "Z9y8X7w6V5" * 4
    fake = FakeJev(_yes(["c0", "c1"]))
    _client(tmp_path, fake).decide("J1", {"candidates": [
        {"i": 0, "path": "a.py", "symbol": "f", "excerpt": f"T = '{token}'"},
        {"i": 1, "path": ".env", "excerpt": "PLAIN=1"},
    ]}, {"c0": {"type": "noul", "instructions": "?"}, "c1": {"type": "noul", "instructions": "?"}})
    body = json.dumps(fake.requests[0]["body"])
    assert token not in body and "PLAIN=1" not in body
    assert fake.requests[0]["body"]["state"]["candidates"] == [
        {"i": 0, "path": "a.py", "symbol": "f"}, {"i": 1, "path": ".env"}]


@pytest.mark.parametrize("questions,problem", [
    ({"c": {"type": "choice", "instructions": "which?"}}, "criteria"),
    ({"s": {"type": "score", "instructions": "how good?", "criteria": {"a": None}}}, "list"),
    ({"x": {"type": "generate", "instructions": "write"}}, "type"),
    ({"c": {"type": "choice", "instructions": "w",
            "criteria": {str(i): None for i in range(256)}}}, "255"),
    ({}, "at least one"),
])
def test_questions_the_api_would_refuse_are_refused_before_sending(
        tmp_path: Path, questions: dict, problem: str) -> None:
    with pytest.raises(client.QuestionError, match=problem):
        client.validate_questions(questions)


def test_a_choice_given_a_list_of_options_becomes_criteria() -> None:
    fixed = client.validate_questions({"c": {"type": "choice", "instructions": "w",
                                             "criteria": ["a", "b"]}})
    assert fixed["c"]["criteria"] == {"a": None, "b": None}


def test_monthly_spend_settles_into_the_workspace_ledger(tmp_path: Path) -> None:
    jev = _client(tmp_path, FakeJev(_yes(["q"], tokens=700)))
    jev.decide("J1", {"a": 1}, QUESTIONS)
    jev.decide("J1", {"a": 1}, QUESTIONS)
    monthly = budget.InMemoryMonthlyLedger()
    assert budget.settle(jev.ledger, monthly, workspace="t1", month="2026-10") == 1400
    assert budget.settle(jev.ledger, monthly, workspace="t1", month="2026-10") == 0  # once
    assert monthly.used("t1", "2026-10") == 1400


# -- the §5.2 call shapes ---------------------------------------------------


def test_j1_asks_one_noul_per_candidate_and_reads_them_back() -> None:
    state, questions = shapes.j1_rerank("implement", "b" * 5000, [
        {"kind": "symbol", "path": f"p{i}.py", "symbol": "f", "excerpt": "e" * 900}
        for i in range(70)])
    assert len(state["task"]["brief"]) == 4000
    assert len(state["candidates"]) == 60
    assert all(len(c["excerpt"]) <= 600 for c in state["candidates"])
    assert set(questions) == {f"c{i}" for i in range(60)}
    assert {q["type"] for q in questions.values()} == {"noul"}
    scores = shapes.read_rerank({f"c{i}": {"noul": i / 100} for i in range(60)})
    assert scores[59] == pytest.approx(0.59)


def test_j0_keeps_implement_below_confidence() -> None:
    state, questions = shapes.j0_classify("do the thing")
    assert set(questions["role"]["criteria"]) == set(shapes.ROLES)
    low = {"role": {"choice": "review", "probabilities": {"review": 0.5}, "confidence": 0.4}}
    high = {"role": {"choice": "review", "probabilities": {"review": 0.9}, "confidence": 0.8}}
    assert shapes.read_classify(low) == "implement"
    assert shapes.read_classify(high) == "review"


def test_j2_flags_uncovered_requirements() -> None:
    _, questions = shapes.j2_plan_coverage(["r one", "r two"], [
        {"step_id": "s1", "title": "t", "files": ["a"], "tests": [], "prompt": "p" * 2000}])
    assert set(questions["r0"]["criteria"]) == {"s1", "none"}
    flagged = shapes.read_plan_coverage({
        "r0": {"choice": "s1", "probabilities": {"s1": 0.9}, "confidence": 0.9},
        "r1": {"choice": "none", "probabilities": {"none": 0.7}, "confidence": 0.7}})
    assert flagged == [1]


def test_j5_holds_only_at_or_above_085() -> None:
    _, questions = shapes.j5_duplicate("t", "b", [{"title": "x", "files": ["a"]}])
    assert set(questions["dup"]["criteria"]) == {"0", "none"}
    hold = {"dup": {"choice": "0", "probabilities": {"0": 0.85, "none": 0.15}, "confidence": 0.8}}
    keep = {"dup": {"choice": "0", "probabilities": {"0": 0.84, "none": 0.16}, "confidence": 0.8}}
    assert shapes.read_duplicate(hold) == 0
    assert shapes.read_duplicate(keep) is None
    assert shapes.DUPLICATE_HOLD_P == 0.85


def test_j3_and_j4_shapes() -> None:
    state, questions = shapes.j3_finding({"summary": "s", "file": "a.py", "line": 3},
                                         {"path": "a.py", "window": "w"}, "hunk")
    assert questions["real"]["type"] == "noul" and state["diff_hunk"] == "hunk"
    assert shapes.read_finding({"real": {"noul": 0.1}}) == pytest.approx(0.1)
    state, questions = shapes.j4_test_gap(
        [{"symbol": "f", "hunk": "h" * 2000}], [{"path": "t.py", "name": "test_f", "excerpt": "x"}])
    assert len(state["changed"][0]["hunk"]) == 1200
    assert set(questions["s0"]["criteria"]) == {"0", "none"}
    gaps = shapes.read_test_gap({"s0": {"choice": "none", "probabilities": {"none": 0.8},
                                        "confidence": 0.8}})
    assert gaps == [0]
