"""`jev_decide`'s malformed answers (the owner's pick of 6 November 2026, after the
24-hour Jev usage report: 4 of 529 agent answers were recorded malformed, each
with a short output of 17 to 36 tokens).

The shapes below are rebuilt from the rejection paths of the shared parser
(`swarm_jev.client._valid_answer` and the `answers` check in `decide`): a
choice label the criteria do not hold verbatim (quoted, wrapped in text,
another case), a choice without its probabilities or with a confidence as a
string, a `noul` that is not a number in a mapping, and an answer that is
missing or not JSON at all. Each is either parsed, when the answer is
unambiguous, or asked again once; a second malformed answer is a clear
"Jev could not decide" result, and the ledger keeps a row for both calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import budget, mcp, register, settings

INTERNAL = {settings.INTERNAL_URL_ENV: "http://decide.internal:8080/v1/decide"}
OPTIONS = {"use_cache": "keep the cache", "drop_cache": None, "ask_owner": None}
CHOICE = {"pick": {"type": "choice", "instructions": "Which fits the brief?",
                   "criteria": OPTIONS}}
NOUL = {"ok": {"type": "noul", "instructions": "Is the requirement met?"}}
GOOD = {"choice": "use_cache", "confidence": 0.8,
        "probabilities": {"use_cache": 0.8, "drop_cache": 0.1, "ask_owner": 0.1}}


class Jev:
    """Answers each call with the next body; bytes are sent as they are."""

    def __init__(self, *bodies: Any) -> None:
        self.bodies = list(bodies)
        self.sent: list[dict] = []

    def __call__(self, url: str, body: bytes, headers: dict, timeout: float):
        self.sent.append(json.loads(body))
        reply = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        return 200, raw


def _server(tmp_path: Path, jev: Jev) -> mcp.JevServer:
    done = register.register_jev_tool({}, attempt_dir=tmp_path,
                                      settings=settings.JudgeSettings(code_opt_in=True), key=None,
                                      environ=INTERNAL)
    return mcp.JevServer.from_config(done.config_path, transport=jev, sleep=lambda s: None,
                                     environ={})


def _call(server: mcp.JevServer, questions: dict) -> tuple[dict, bool]:
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": "jev_decide",
                                      "arguments": {"state": {"diff": "x"},
                                                    "questions": questions}}})
    result = reply["result"]
    return json.loads(result["content"][0]["text"]), result["isError"]


def _answered(body: Any, tokens: int = 20) -> dict:
    return {"answers": body, "usage": {"input_tokens": 600, "output_tokens": tokens}}


# -- parsed: the label is unambiguous ------------------------------------------


@pytest.mark.parametrize("shape", [
    {**GOOD, "choice": '"use_cache"'},                       # the label in quotes
    {**GOOD, "choice": "`use_cache`."},                      # backticks and a full stop
    {**GOOD, "choice": " Use_Cache\n"},                      # another case, whitespace
    {**GOOD, "choice": "I choose use_cache: it fits."},      # the label inside text
    {"choice": "use_cache", "confidence": 0.8},              # no probabilities
    {**GOOD, "confidence": "0.8"},                           # confidence as a string
    {"choice": "use_cache", "probabilities": GOOD["probabilities"]},  # no confidence
], ids=["quoted", "backticked", "case", "in_text", "no_probabilities", "string_confidence",
        "confidence_from_probabilities"])
def test_an_unambiguous_choice_is_parsed_in_one_call(tmp_path: Path, shape: dict) -> None:
    jev = Jev(_answered({"pick": shape}))
    server = _server(tmp_path, jev)
    answer, failed = _call(server, CHOICE)
    assert not failed, answer
    assert answer["answers"]["pick"]["choice"] == "use_cache"
    assert answer["answers"]["pick"]["confidence"] == pytest.approx(0.8)
    assert len(jev.sent) == 1
    (row,) = server.client.ledger.read()["audit"]
    assert row["outcome"] == "ok" and row["kind"] == "agent"


@pytest.mark.parametrize("shape", [{"noul": "0.7"}, 0.7, "0.7"],
                         ids=["string_in_mapping", "bare_number", "bare_string"])
def test_an_unambiguous_noul_is_parsed_in_one_call(tmp_path: Path, shape: Any) -> None:
    jev = Jev(_answered({"ok": shape}))
    server = _server(tmp_path, jev)
    answer, failed = _call(server, NOUL)
    assert not failed and answer["answers"]["ok"]["noul"] == pytest.approx(0.7)
    assert len(jev.sent) == 1


def test_the_request_asks_for_one_label_and_a_confidence(tmp_path: Path) -> None:
    jev = Jev(_answered({"pick": GOOD}))
    _call(_server(tmp_path, jev), CHOICE)
    sent = jev.sent[0]["questions"]["pick"]
    assert sent["instructions"].startswith("Which fits the brief?")
    assert "exactly one option label" in sent["instructions"]
    assert "confidence" in sent["instructions"]
    assert sent["criteria"] == OPTIONS


# -- retried: nothing unambiguous to read --------------------------------------


@pytest.mark.parametrize("first", [
    _answered({"pick": {**GOOD, "choice": "use_cache or drop_cache"}}),  # two labels
    _answered({"pick": {**GOOD, "choice": "neither"}}),                  # no label
    _answered({"pick": "use_cache"}),                                    # no confidence
    _answered({"pick": {**GOOD, "confidence": 80}}),                     # out of range
    _answered({}),                                                       # answer missing
    {"usage": {"input_tokens": 600}},                                    # no answers
    b"use_cache",                                                        # not JSON
], ids=["two_labels", "no_label", "bare_label", "confidence_80", "missing", "no_answers",
        "not_json"])
def test_an_ambiguous_answer_is_asked_again_once(tmp_path: Path, first: Any) -> None:
    jev = Jev(first, _answered({"pick": GOOD}))
    server = _server(tmp_path, jev)
    answer, failed = _call(server, CHOICE)
    assert not failed, answer
    assert answer["answers"]["pick"]["choice"] == "use_cache"
    assert len(jev.sent) == 2
    ledger = server.client.ledger.read()
    first_row, retry_row = ledger["audit"]
    assert first_row["outcome"] == "malformed" and retry_row["outcome"] == "ok"
    assert retry_row["retry_of"] == first_row["call_id"] != retry_row["call_id"]
    assert "retry_of" not in first_row
    assert ledger["calls"] == 2


def test_a_second_malformed_answer_is_a_clear_could_not_decide(tmp_path: Path) -> None:
    bad = _answered({"pick": {**GOOD, "choice": "neither"}}, tokens=17)
    jev = Jev(bad, bad)
    server = _server(tmp_path, jev)
    answer, failed = _call(server, CHOICE)
    assert failed and answer["decided"] is False
    assert answer["result"] == "Jev could not decide"
    assert answer["outcome"] == "malformed" and "own judgement" in answer["note"]
    assert "answers" not in answer
    assert len(jev.sent) == 2
    ledger = server.client.ledger.read()
    assert ledger["calls"] == 2
    first_row, retry_row = ledger["audit"]
    assert first_row["outcome"] == retry_row["outcome"] == "malformed"
    assert retry_row["retry_of"] == first_row["call_id"]
    assert first_row["output_tokens"] == retry_row["output_tokens"] == 17
    # one question answered badly twice is one failure, not Jev being down
    assert not ledger["breaker_open"]
    assert budget.Ledger(server.client.ledger.path).admit(estimate=1) is None


def test_a_retry_the_budget_refuses_is_still_could_not_decide(tmp_path: Path) -> None:
    jev = Jev(_answered({"pick": {**GOOD, "choice": "neither"}}))
    server = _server(tmp_path, jev)
    ledger = server.client.ledger
    with ledger._locked() as state:  # one call left of the task's shared eight
        state["calls"] = budget.SHARED_CALLS_PER_TASK - 1
    answer, failed = _call(server, CHOICE)
    assert failed and answer["result"] == "Jev could not decide"
    assert len(jev.sent) == 1
    first_row, retry_row = ledger.read()["audit"]
    assert first_row["outcome"] == "malformed"
    assert retry_row["outcome"] == "skipped:budget_calls"
    assert retry_row["retry_of"] == first_row["call_id"]
