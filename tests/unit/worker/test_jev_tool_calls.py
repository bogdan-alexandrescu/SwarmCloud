"""The quality hooks' and the Jev browser's calls, one record each (lane AQ-JEV-ACCOUNTING).

The owner's pick on #3111: jev-belay, jev-enforce and the live browser report
one record per Jev call through swarm-api's `state/jev-usage`, as the
judgments do. The image's launcher (`swarm-jev-gate.mjs`) appends each call's
record to `private/jev-gates.calls` or `private/jev-browser.calls`; on every
exit `judge.attempt.settle_at_exit` sends them (`account.record_tool_calls`)
before the settlement. Before this, those files did not exist and the route's
`quality_hook` and `browser` sources were never sent.

The launcher runs under node (skipped without it locally, failed without it
in CI, as `test_jev_outbound_filter.py`); the rest is offline: no
credentials, no emulator.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_worker import qualitygates
from agent_worker.judge import account, attempt, budget
from agent_worker.runners import jev_launch
from swarm_common.states import TaskState

from test_jev_outbound_filter import LAUNCHER, PYTHON, _run_enforce, fake_jev, node  # noqa: F401
from test_jev_state_accounting import (  # noqa: F401 -- fixtures
    ENV, QUESTIONS, _cap, _client, _opened, _row, _setup, api_db, no_firestore_client, opener)
from state_routes_harness import TENANT
from test_jev_wiring import FakeJev


def _node_run(node_bin: str, script: str) -> subprocess.CompletedProcess[str]:
    preamble = f"import * as gate from {json.dumps(LAUNCHER.as_uri())};\nconst PY = {json.dumps(PYTHON)};\n"
    return subprocess.run([node_bin, "--input-type=module", "-e", preamble + script],
                          capture_output=True, text=True, timeout=60, check=False)


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- the launcher writes one record per call ------------------------------------

def test_the_launchers_record_limits_are_the_workers():
    text = LAUNCHER.read_text()
    assert f"MEASURED_TOKENS_PER_CALL = {budget.MEASURED_TOKENS_PER_CALL:_}" in text
    assert f"const MODEL = /{account.MODEL.pattern}/;" in text
    assert f"const IDENT = /{account.IDENT.pattern}/;" in text
    names = dict(account.TOOL_CALL_FILES)
    assert names == {qualitygates.CALLS_FILE_NAME: account.SOURCE_QUALITY_HOOK,
                     jev_launch.BROWSER_CALLS_FILE_NAME: account.SOURCE_BROWSER}


def test_jev_enforce_writes_one_record_per_call(node, tmp_path, fake_jev):  # noqa: F811
    calls = tmp_path / "calls"
    # MUTATION: drop `calls_file` from the launcher's guardedFetch and no
    # record is written: the hooks' spend is in no account.
    _run_enforce(node, tmp_path, fake_jev, {str(tmp_path / "a.py"): "x = 1",
                                            str(tmp_path / "b.py"): "y = 2"},
                 config={"calls_file": str(calls)})
    records = _records(calls)
    assert len(records) == 2
    assert len({r["call_id"] for r in records}) == 2
    for record in records:
        assert account.CALL_ID.fullmatch(record["call_id"])
        assert (record["source"], record["kind"], record["backend"]) == \
            ("quality_hook", "enforce", "typesafe")
        assert record["requested_model"] == "jev-latest"
        # The fake answers with no usage: charged at least the measured
        # call, and flagged; never free.
        assert record["tokens_estimated"] is True
        assert record["input_tokens"] >= budget.MEASURED_TOKENS_PER_CALL
        assert (record["outcome"], record["output_tokens"]) == ("ok", None)
        # Names and counts only: never the edited text.
        assert "x = 1" not in json.dumps(record)


def test_the_browser_filter_records_each_call_with_its_stated_usage(node, tmp_path):  # noqa: F811
    calls = tmp_path / "jev-browser.calls"
    done = _node_run(node, f"""
globalThis.fetch = async () => new Response(JSON.stringify(
  {{ answers: {{ a: 1 }}, usage: {{ input_tokens: 1234, output_tokens: 56 }}, model: "jev-2" }}), {{ status: 200 }});
gate.installBrowserFilter({{ SWARM_JEV_BROWSER_CALLS: {json.dumps(str(calls))},
                             SWARM_JEV_REDACTION_PYTHON: PY, SWARM_JEV_BACKEND: "typesafe" }});
const res = await fetch("http://127.0.0.1:9/v1/systemone",
  {{ method: "POST", body: JSON.stringify({{ state: {{ kind: "page" }}, model: "jev-latest" }}) }});
console.log(JSON.stringify(await res.json()));
""")
    assert done.returncode == 0, done.stderr
    # The tool still reads the whole answer: the record read a clone.
    assert json.loads(done.stdout)["answers"] == {"a": 1}
    [record] = _records(calls)
    assert {k: v for k, v in record.items() if k != "call_id"} == {
        "source": "browser", "kind": "browser", "backend": "typesafe",
        "requested_model": "jev-latest", "served_model": "jev-2", "input_tokens": 1234,
        "output_tokens": 56, "tokens_estimated": False, "outcome": "ok"}


def test_a_browser_call_that_got_no_answer_is_recorded_failed_and_still_raises(node, tmp_path):  # noqa: F811
    calls = tmp_path / "jev-browser.calls"
    done = _node_run(node, f"""
globalThis.fetch = async () => {{ throw new Error("refused"); }};
gate.installBrowserFilter({{ SWARM_JEV_BROWSER_CALLS: {json.dumps(str(calls))}, SWARM_JEV_REDACTION_PYTHON: PY }});
let raised = false;
try {{ await fetch("http://127.0.0.1:9/v1/systemone", {{ method: "POST", body: "{{}}" }}); }}
catch {{ raised = true; }}
console.log(JSON.stringify(raised));
""")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) is True
    [record] = _records(calls)
    assert (record["outcome"], record["input_tokens"], record["backend"]) == ("failed", 0, None)


def test_the_browser_launch_names_the_calls_file_beside_the_counts():
    env: dict[str, str] = {}
    jev_launch.filter_env(env, "/w/private/jev-typesafe.key")
    assert env[jev_launch.CALLS_ENV] == "/w/private/jev-browser.calls"
    assert env[jev_launch.COUNTS_ENV] == "/w/private/jev-browser.counts"


# --- the worker sends them through state/jev-usage ------------------------------

class _State:
    def __init__(self, fail_after: int | None = None) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.fail_after = fail_after

    def post(self, op: str, body: dict[str, Any], *, deadline_seconds: float) -> dict[str, Any]:
        if self.fail_after is not None and len(self.posts) >= self.fail_after:
            raise OSError("lost")
        self.posts.append((op, body))
        return {"ok": True}


def _tool_record(source: str, kind: str, tokens: int = 1_000) -> dict[str, Any]:
    return {"call_id": f"{kind}-{secrets.token_hex(8)}", "source": source, "kind": kind,
            "backend": "typesafe", "requested_model": "jev-latest", "served_model": None,
            "input_tokens": tokens, "output_tokens": None, "tokens_estimated": False,
            "outcome": "ok"}


def _append(path: Path, *rows: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def test_record_tool_calls_sends_each_once_and_only_its_own_source(tmp_path):
    hooks, browser = tmp_path / "jev-gates.calls", tmp_path / "jev-browser.calls"
    _append(hooks, _tool_record("quality_hook", "belay"), "not json", [1],
            # A hook's file cannot speak for the browser, or the judgments.
            _tool_record("browser", "browser"), _tool_record("judgment", "J4"))
    _append(browser, _tool_record("browser", "browser"))
    state = _State()
    assert account.record_tool_calls(state, tmp_path) == 2
    assert [(op, body["source"], body["kind"]) for op, body in state.posts] == [
        ("jev-usage", "quality_hook", "belay"), ("jev-usage", "browser", "browser")]
    # MUTATION: drop the sent offset and a second exit sends them again.
    assert account.record_tool_calls(state, tmp_path) == 0
    _append(hooks, _tool_record("quality_hook", "enforce"))
    assert account.record_tool_calls(state, tmp_path) == 1
    assert state.posts[-1][1]["kind"] == "enforce"


def test_a_lost_answer_leaves_the_offset_so_the_records_go_again(tmp_path):
    hooks = tmp_path / "jev-gates.calls"
    first, second = _tool_record("quality_hook", "belay"), _tool_record("quality_hook", "belay")
    _append(hooks, first, second)
    with pytest.raises(OSError):
        account.record_tool_calls(_State(fail_after=1), tmp_path)
    state = _State()
    assert account.record_tool_calls(state, tmp_path) == 2
    assert [body["call_id"] for _, body in state.posts] == [first["call_id"], second["call_id"]]


def test_a_half_written_line_waits_for_its_newline(tmp_path):
    hooks = tmp_path / "jev-gates.calls"
    _append(hooks, _tool_record("quality_hook", "belay"))
    with hooks.open("a") as handle:
        handle.write('{"call_id": "belay-x", "sou')
    state = _State()
    assert account.record_tool_calls(state, tmp_path) == 1
    assert account.record_tool_calls(state, tmp_path) == 0


def test_a_symlinked_calls_file_is_not_read(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _append(elsewhere, _tool_record("quality_hook", "belay"))
    private = tmp_path / "private"
    private.mkdir()
    (private / "jev-gates.calls").symlink_to(elsewhere)
    assert account.record_tool_calls(_State(), private) == 0


# --- end to end, through swarm-api's real routes ---------------------------------

def _tool_docs(api_db) -> list[dict[str, Any]]:
    return [d for p, d in api_db.docs.items() if p.startswith("jev_usage/")
            and d.get("kind") == "call" and d.get("source") != "judgment"]


def test_the_tools_calls_are_recorded_at_exit_even_with_the_judgments_off(api_db, opener, tmp_path):  # noqa: F811
    state = _opened(api_db, opener)
    _cap(api_db, None)
    private = tmp_path / "private"
    private.mkdir()
    _append(private / "jev-gates.calls", _tool_record("quality_hook", "belay", 700),
            _tool_record("quality_hook", "enforce", 300))
    _append(private / "jev-browser.calls", _tool_record("browser", "browser", 2_000))
    # MUTATION: drop `record_tool_calls` from `settle_at_exit` and nothing of
    # the hooks' or the browser's spend reaches the account.
    attempt.settle_at_exit({}, db=state, committed_end=TaskState.SUCCEEDED, tenant_id=TENANT,
                           task_id=state.task_id, generation=state.generation,
                           private_dir=private)
    docs = _tool_docs(api_db)
    assert sorted((d["source"], d["call_kind"]) for d in docs) == [
        ("browser", "browser"), ("quality_hook", "belay"), ("quality_hook", "enforce")]
    row = _row(api_db)
    assert (row["calls"], row["input_tokens"]) == (3, 3_000)
    month = next(d for p, d in api_db.docs.items()
                 if p.startswith(f"jev_usage/{TENANT}_20") and "input_tokens" in d
                 and d.get("kind") is None)
    assert month["input_tokens"] == 3_000
    # A second exit sends nothing new and charges nothing twice.
    attempt.settle_at_exit({}, db=state, committed_end=TaskState.SUCCEEDED, tenant_id=TENANT,
                           task_id=state.task_id, generation=state.generation,
                           private_dir=private)
    assert (_row(api_db)["calls"], _row(api_db)["input_tokens"]) == (3, 3_000)


def test_the_tools_records_count_against_the_reservation_before_it_is_released(
        api_db, opener, tmp_path):  # noqa: F811
    state = _opened(api_db, opener)
    _cap(api_db, None)
    env, _ = _setup(tmp_path, state)
    assert _client(env, FakeJev()).decide("J4", {"diff": "x"}, QUESTIONS).outcome == "ok"
    private = tmp_path / "private"
    private.mkdir()
    _append(private / "jev-gates.calls", _tool_record("quality_hook", "belay", 5_000))
    attempt.settle_at_exit(env, db=state, committed_end=TaskState.PARKED, tenant_id=TENANT,
                           task_id=state.task_id, generation=state.generation,
                           private_dir=private)
    row = _row(api_db)
    assert row["calls"] == 2
    assert row["by_kind"]["belay"]["input_tokens"] == 5_000
    assert row["settlement"]["outcome"] == "parked"
    assert row["settlement"]["unrecorded_calls"] == 0
    assert api_db.docs[f"jev_usage/{TENANT}_reservations"]["held"] == {}
    assert ENV  # the judgments' environment the setup used
