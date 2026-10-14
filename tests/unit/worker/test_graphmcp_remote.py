"""swarm-graph's one remote tool, `in_flight` (docs/design/context-quality.md §4.2 layer I,
§4.4, §7 row CTXQ-I).

The agent's side writes one request into the spool and reads the worker's
answer; the worker's side (`InFlightRelay`) signs it with the attempt key and
POSTs it to swarm-api's attempt-proved route. Both are driven here in
process: the agent's side with the real spool, the worker's with a fake API
in place of `children.ChildApi`, so nothing reaches the network.

What each case pins, from §7's row CTXQ-I and §4.4:

* no network gives UNKNOWN, never an empty list -- and so do no spool, no
  answer from the worker and a refusal;
* a complete read with nothing in flight is a count of zero, not UNKNOWN;
* a 409 is the fence: the agent is answered `fenced` and the relay raises a
  `ChildSubmitFenced`, so the lifecycle ends the attempt through its one
  fenced exit (invariant 5);
* the signed body carries the attempt's own fencing fields and the files,
  never a repository (invariant 10);
* the tool's schema keeps swarm-graph under its 2k-token budget, and every
  answer under the 4 KiB cap, paged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_worker.children import ChildApiError, ChildSubmitFenced
from agent_worker.graphmcp import remote, server, tools

ATTEMPT = "att_parent_1"
ROUTES = "apps/api/routes.py"


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append(("info", message, fields))

    def warning(self, message: str, **fields: Any) -> None:
        self.lines.append(("warning", message, fields))


class _Key:
    def headers(self, method: str, path: str, body: bytes, *, now: float) -> dict[str, str]:
        return {"X-Swarm-Attempt-Proof": f"signed:{method}:{path}:{len(body)}",
                "X-Swarm-Attempt-Timestamp": str(int(now))}


class _Api:
    """`children.ChildApi`, faked: records each call, answers or raises."""

    def __init__(self, answer: dict[str, Any] | None = None,
                 raises: Exception | None = None) -> None:
        self.answer = answer if answer is not None else complete_answer()
        self.raises = raises
        self.calls: list[tuple[str, str, bytes, dict[str, str]]] = []

    def call(self, method: str, path: str, *, body: bytes, headers: dict[str, str]):
        self.calls.append((method, path, body, headers))
        if self.raises is not None:
            raise self.raises
        return 200, self.answer


def complete_answer(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "repository": "acme/widgets", "read_at": "2026-10-14T12:00:00+00:00",
        "files": [ROUTES],
        "territory": {"expanded": False, "depth": 1, "paths": 1, "reason": "no graph"},
        "pull_requests_read": {"ok": True, "code": None, "read_at": "2026-10-14T12:00:00+00:00"},
        "pull_requests": [], "pull_requests_unread": [], "pull_requests_truncated": False,
        "lanes": [], "lanes_undeclared": [], "lanes_truncated": False,
        "tasks_undeclared": [], "tasks_truncated": False,
        "excluded": {"run_id": None, "pull_request": None}, "complete": True,
    }
    answer.update(overrides)
    return answer


def relay(api: _Api) -> remote.InFlightRelay:
    return remote.InFlightRelay(
        remote.InFlightConfig(tenant_id="eng", task_id="task_parent", attempt_id=ATTEMPT,
                              lease_id="lease_parent_1", generation=3),
        key=_Key(), api=api, logger=_Log(), wall=lambda: 1_760_000_000.0)


def ask(work: Path, worker: remote.InFlightRelay | None, arguments: dict[str, Any],
        *, spool: Path | None = None) -> tuple[dict[str, Any], bool]:
    """The agent's call through the real spool, the worker ticking while it waits."""
    if worker is not None:
        env = worker.prepare(work)
        spool = Path(env[remote.SPOOL_ENV])

    def sleep(_seconds: float) -> None:
        if worker is not None:
            worker.tick(work)

    text, failed = remote.in_flight(arguments, spool=spool, wait_seconds=5.0,
                                    poll_seconds=0.0, sleep=sleep)
    assert len(text.encode()) <= tools.ANSWER_CAP, len(text)
    return json.loads(text), failed


# --------------------------------------------------------------------------
# UNKNOWN, never an empty list
# --------------------------------------------------------------------------


def test_no_network_gives_unknown_never_an_empty_list(tmp_path):
    api = _Api(raises=ChildApiError(None, "api_unavailable", "connection refused",
                                    retryable=True))
    answer, failed = ask(tmp_path, relay(api), {"files": [ROUTES]})
    assert len(api.calls) == 1, "the relay did not try the route"
    assert answer["in_flight"] == "UNKNOWN"
    assert "rows" not in answer and answer.get("total") is None
    assert answer["reason"]["code"] == "api_unavailable"
    assert "never" in answer["note"]
    assert failed is True


def test_no_spool_gives_unknown(tmp_path):
    answer, failed = ask(tmp_path, None, {"files": [ROUTES]}, spool=None)
    assert answer["in_flight"] == "UNKNOWN" and "rows" not in answer
    assert answer["reason"]["code"] == "no_spool"
    answer, _ = ask(tmp_path, None, {"files": [ROUTES]}, spool=tmp_path / "missing")
    assert answer["in_flight"] == "UNKNOWN"


def test_a_worker_that_never_answers_gives_unknown(tmp_path):
    spool = tmp_path / remote.SPOOL_DIR_NAME
    (spool / "requests").mkdir(parents=True)
    (spool / "responses").mkdir()
    text, failed = remote.in_flight({"files": [ROUTES]}, spool=spool, wait_seconds=0.0,
                                    poll_seconds=0.0, sleep=lambda _s: None)
    answer = json.loads(text)
    assert answer["in_flight"] == "UNKNOWN"
    assert answer["reason"]["code"] == "worker_did_not_answer"


@pytest.mark.parametrize("status,code", [(404, "not_found"), (422, "spec_unverified"),
                                         (503, "unavailable")])
def test_a_refusal_gives_unknown(tmp_path, status, code):
    api = _Api(raises=ChildApiError(status, code, "refused", retryable=status >= 500))
    answer, _ = ask(tmp_path, relay(api), {"files": [ROUTES]})
    assert answer["in_flight"] == "UNKNOWN" and "rows" not in answer


def test_a_complete_read_with_nothing_in_flight_is_zero_not_unknown(tmp_path):
    answer, failed = ask(tmp_path, relay(_Api()), {"files": [ROUTES]})
    assert failed is False
    assert answer["in_flight"] == 0 and answer["complete"] is True
    assert answer["rows"] == [] and answer["total"] == 0


def test_a_forge_that_was_not_read_is_unknown_for_its_part(tmp_path):
    api = _Api(complete_answer(
        pull_requests_read={"ok": False, "code": "issue_read_failed", "read_at": None},
        complete=False))
    answer, _ = ask(tmp_path, relay(api), {"files": [ROUTES]})
    assert answer["pull_requests"] == "UNKNOWN"
    assert answer["complete"] is False
    assert answer["in_flight"] != 0


# --------------------------------------------------------------------------
# what is listed
# --------------------------------------------------------------------------


def test_overlapping_lanes_pull_requests_and_tasks_are_rows(tmp_path):
    shared = [{"path": ROUTES, "entry": ROUTES, "why": "named", "seam": False}]
    api = _Api(complete_answer(
        pull_requests=[{"number": 11, "title": "edits the routes", "shared": shared,
                        "shared_total": 1, "files_truncated": False}],
        pull_requests_unread=[13],
        lanes=[{"run_id": "run_ours", "issue": 40, "state": "RUNNING", "step_id": "s1",
                "pull_request": None, "shared": shared, "shared_total": 1}],
        tasks_undeclared=[{"task_id": "task_standalone", "runner_profile": "claude-code",
                           "workflow_id": None, "step_id": None}],
        complete=False))
    answer, failed = ask(tmp_path, relay(api), {"files": [ROUTES]})
    assert failed is False
    kinds = [(row["kind"], row.get("number") or row.get("run_id") or row.get("task_id"))
             for row in answer["rows"]]
    assert kinds == [("pull_request", 11), ("lane", "run_ours"),
                     ("unread_pull_request", 13), ("undeclared_task", "task_standalone")]
    assert answer["in_flight"] == 2
    assert answer["complete"] is False


def test_a_long_answer_pages_under_the_cap(tmp_path):
    shared = [{"path": ROUTES, "entry": ROUTES, "why": "named", "seam": False}]
    pulls = [{"number": n, "title": "x" * 150, "shared": shared, "shared_total": 1,
              "files_truncated": False} for n in range(1, 80)]
    api = _Api(complete_answer(pull_requests=pulls))
    worker = relay(api)
    first, _ = ask(tmp_path, worker, {"files": [ROUTES]})
    assert first["next_cursor"] is not None and first["total"] == 79
    second, _ = ask(tmp_path, worker, {"files": [ROUTES], "cursor": first["next_cursor"]})
    assert second["rows"][0]["number"] == len(first["rows"]) + 1


# --------------------------------------------------------------------------
# the signed call and the fence
# --------------------------------------------------------------------------


def test_the_signed_body_carries_the_attempts_fields_and_the_files_only(tmp_path):
    api = _Api()
    ask(tmp_path, relay(api), {"files": ["./" + ROUTES, ROUTES]})
    method, path, body, headers = api.calls[0]
    assert (method, path) == ("POST", f"/v1/attempts/{ATTEMPT}/context/inflight")
    sent = json.loads(body)
    assert set(sent) == {"tenant_id", "task_id", "attempt_id", "lease_id", "generation",
                         "request_id", "files"}
    assert (sent["tenant_id"], sent["attempt_id"], sent["generation"]) == ("eng", ATTEMPT, 3)
    assert sent["files"] == [ROUTES]
    assert headers["X-Swarm-Attempt-Proof"].startswith("signed:POST:")


@pytest.mark.parametrize("arguments", [{}, {"files": []}, {"files": ["/etc/passwd"]},
                                       {"files": ["../up.py"]}, {"files": [ROUTES],
                                                                 "repository": "a/b"}])
def test_bad_arguments_are_refused_before_anything_is_spooled(tmp_path, arguments):
    api = _Api()
    text, failed = remote.in_flight(arguments, spool=tmp_path, wait_seconds=0.0,
                                    poll_seconds=0.0, sleep=lambda _s: None)
    assert failed is True and "error" in json.loads(text)
    assert not (tmp_path / "requests").exists()
    assert api.calls == []


def test_a_409_is_the_fence(tmp_path):
    api = _Api(raises=ChildApiError(409, "context_fenced", "superseded", retryable=False))
    worker = relay(api)
    spool = Path(worker.prepare(tmp_path)[remote.SPOOL_ENV])
    request = remote.write_request(spool, "graph-1-abc", remote.TOOL_NAME, {"files": [ROUTES]})
    with pytest.raises(ChildSubmitFenced):
        worker.tick(tmp_path)
    assert not request.exists()
    answer = json.loads((spool / "responses" / "graph-1-abc.json").read_text())
    assert answer["refused"]["code"] == "fenced"


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------


def test_the_server_lists_and_answers_in_flight_within_the_schema_budget(tmp_path):
    listed = remote.TOOLS_WITH_REMOTE
    assert [t["name"] for t in listed] == [*tools.TOOL_NAMES, remote.TOOL_NAME]
    spent = len(json.dumps({"tools": listed}, separators=(",", ":")).encode()) \
        + len(server.INSTRUCTIONS.encode())
    assert spent <= 2000 * 3, spent

    graph = remote.RemoteGraphServer(snapshot=None, freshness=None, spool=None)
    response = graph.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert remote.TOOL_NAME in [t["name"] for t in response["result"]["tools"]]
    called = graph.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": remote.TOOL_NAME,
                                      "arguments": {"files": [ROUTES]}}})
    body = json.loads(called["result"]["content"][0]["text"])
    assert body["in_flight"] == "UNKNOWN"
    assert called["result"]["isError"] is True
