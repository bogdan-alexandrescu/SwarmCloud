"""The QA target's scrub fails closed (QAGATE-SCRUB-DEPTH; part of #2116).

`QaTarget._walk` used to return any dict or list nested deeper than 64
levels unchanged, so a held password wrapped in 65 arrays reached the agent
in plain text. The scrub now replaces an over-deep container with
`SCRUB_TOO_DEEP`, and a scrub that raises refuses the whole message: the
agent gets `SCRUB_REFUSED`, never the original. Neither record carries the
value or its length.

The gate is the real `QaTarget` and `CdpProxy` against test_qa_target_signin's
scripted Chromium. No Chromium, network or cloud credential. Every password is
built at run time.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_worker.livegate import qatarget as gate_qt

import test_livegate_qa_identity as qi
import test_qa_target_signin as ts


@pytest.fixture
def tmp() -> Any:
    path = Path(tempfile.mkdtemp(prefix="qs"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _password() -> str:
    return "pw-" + secrets.token_hex(8)


def _nested(value: Any, levels: int) -> Any:
    for _ in range(levels):
        value = [value]
    return value


def _target(password: str) -> tuple[gate_qt.QaTarget, list[tuple[str, dict[str, Any]]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    target = gate_qt.QaTarget(lambda e, f: records.append((e, dict(f))))
    target.handle(ts._args(ts._target_id(), password))
    return target, records


def _readable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=repr)


def test_a_password_nested_70_levels_deep_in_a_cdp_result_never_reaches_the_agent(tmp):
    async def body() -> None:
        rig = ts.Rig(tmp)
        rig.fake.start(rig.sink)
        password = _password()
        rig.target.handle(ts._args(ts._target_id(), password))
        link = qi.FakeLink()
        conn = await rig.proxy.connect_agent(link)
        assert conn is not None
        rig.fake.results["Network.getResponseBody"] = {"body": "fine", "base64Encoded": False,
                                                       "deep": _nested(password, 70)}
        await rig.proxy.on_agent_message(conn, {"id": 11, "method": "Network.getResponseBody",
                                                "params": {"requestId": "D1"}})
        await rig.settle()
        text = _readable(link.sent)
        # MUTATION: restore `return value` past depth 64 in QaTarget._walk -> the password is in the output: red.
        assert password not in text
        # MUTATION: return None (or drop the container) instead of the marker -> the agent cannot tell why: red.
        assert gate_qt.SCRUB_TOO_DEEP in text
        reply = next(m for m in link.sent if m.get("id") == 11)
        # MUTATION: refuse the whole message instead of marking the container -> the rest of the result is lost: red.
        assert reply["result"]["body"] == "fine"
        marked = [f for e, f in rig.records if e == "qa_target_scrub_marked"]
        # MUTATION: skip the qa_target_scrub_marked record -> the marked message is unlogged: red.
        assert marked and all(isinstance(f["count"], int) and f["count"] > 0 for f in marked)
        await rig.fake.stop()

    ts.run(body)


def test_a_password_in_an_over_deep_dict_or_under_a_key_is_never_passed_through():
    password = _password()
    target, _ = _target(password)
    deep: Any = {password: password}
    for _ in range(80):
        deep = {"k": deep}
    out = target.scrub({"method": "Runtime.consoleAPICalled", "params": {"deep": deep}})
    # MUTATION: restore `return value` past depth 64 for dicts -> the key and value reach the agent: red.
    assert password not in _readable(out)
    assert gate_qt.SCRUB_TOO_DEEP in _readable(out)


def test_a_password_at_depth_3_is_still_replaced_by_the_placeholder():
    password = _password()
    target, records = _target(password)
    message = {"id": 3, "result": {"a": {"b": [f"echo <{password}>"]}}}
    out = target.scrub(message)
    # MUTATION: mark every container instead of walking it -> normal scrubbing changes: red.
    assert out == {"id": 3, "result": {"a": {"b": [f"echo <{gate_qt.PLACEHOLDER}>"]}}}
    assert gate_qt.SCRUB_TOO_DEEP not in _readable(out)
    # MUTATION: record a marked event for an ordinary scrub -> the count of refusals is wrong: red.
    assert [e for e, _ in records if e.startswith("qa_target_scrub")] == ["qa_target_scrubbed"]
    clean = {"id": 4, "result": {"a": _nested("nothing here", 64)}}
    # MUTATION: lower the depth limit -> a clean 64-level result is marked: red.
    assert target.scrub(clean) is clean


def test_a_scrub_that_raises_while_walking_forwards_no_part_of_the_original_message():
    password = _password()
    target, records = _target(password)
    calls = [0]
    original = target._replace

    def failing(text: str, counter: list[int]) -> str:
        calls[0] += 1
        if calls[0] > 2:
            # The exception itself names the value: it must reach neither the agent nor the log.
            raise RuntimeError(f"scrub failed on {text}")
        return original(text, counter)

    target._replace = failing  # type: ignore[method-assign]
    message = {"id": 21, "sessionId": "S1",
               "result": {"result": {"type": "string", "value": f"<{password}>", "extra": "visible-part"}}}
    out = target.scrub(message)
    text = _readable(out)
    # MUTATION: let the exception propagate from QaTarget.scrub -> this call raises: red.
    # MUTATION: return `message` from the except branch -> the password is in the output: red.
    assert password not in text and "visible-part" not in text
    # MUTATION: drop the error from the refusal -> the agent's command gets no answer: red.
    assert out["id"] == 21 and gate_qt.SCRUB_REFUSED in out["error"]["message"] and "result" not in out
    event = target.scrub({"method": "Runtime.consoleAPICalled", "params": {"args": [{"value": password}]}})
    # MUTATION: refuse an event by sending it on whole -> the password is in the output: red.
    assert password not in _readable(event) and gate_qt.SCRUB_REFUSED in _readable(event)
    assert event["method"] == "Runtime.consoleAPICalled"
    refused = [f for e, f in records if e == "qa_target_scrub_refused"]
    # MUTATION: skip the qa_target_scrub_refused record -> the refusal is unlogged: red.
    assert [f["route"] for f in refused] == ["response", "Runtime.consoleAPICalled"]
    assert [f["count"] for f in refused] == [1, 2]


def test_a_refusal_or_marked_record_holds_neither_the_value_nor_its_length():
    password = _password()
    target, records = _target(password)
    target.scrub({"method": "Runtime.consoleAPICalled", "params": {"deep": _nested(password, 70)}})

    def failing(text: str, counter: list[int]) -> str:
        raise ValueError(text)

    target._replace = failing  # type: ignore[method-assign]
    target.scrub({"id": 5, "result": {"value": password}})
    logged = [(e, f) for e, f in records if e in ("qa_target_scrub_marked", "qa_target_scrub_refused")]
    assert [e for e, _ in logged] == ["qa_target_scrub_marked", "qa_target_scrub_refused"]
    text = _readable(logged)
    # MUTATION: log the exception text or the container in either record -> the password is in the log: red.
    assert password not in text
    for _, fields in logged:
        # MUTATION: add a `length` or `size` field to either record -> it names the value's length: red.
        assert set(fields) <= {"route", "count", "error"}
        assert str(len(password)) not in [str(v) for v in fields.values()]


def test_a_number_a_non_json_value_and_a_postdata_chain_are_never_passed_through():
    digits = str(secrets.randbelow(10**12) + 10**12)
    records: list[tuple[str, dict[str, Any]]] = []
    target = gate_qt.QaTarget(lambda e, f: records.append((e, dict(f))))
    target.handle(ts._args(ts._target_id(), digits))
    out = target.scrub({"id": 6, "result": {"result": {"type": "number", "value": int(digits)}}})
    # MUTATION: pass an int through unscrubbed -> an all-digit password reaches the agent as a number: red.
    assert digits not in _readable(out)
    password = _password()
    target, _ = _target(password)
    odd = target.scrub({"id": 7, "result": {"t": (password,), "b": password.encode()}})
    # MUTATION: return a non-JSON value unchanged -> the tuple or bytes carry the password: red.
    assert password not in _readable(odd)
    chain: Any = {"bytes": "not base64 !!", "note": password}
    for _ in range(80):
        chain = {"postDataEntries": [chain]}
    chained = target.scrub({"method": "Network.requestWillBeSent", "params": {"request": chain}})
    # MUTATION: restart postDataEntries entries at depth 1 -> the chain is never held to the limit: red.
    assert password not in _readable(chained) and gate_qt.SCRUB_TOO_DEEP in _readable(chained)
