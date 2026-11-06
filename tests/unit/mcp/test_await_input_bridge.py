"""The MCP bridge's half of a human step (docs/design/human-steps.md §4.2, §4.5, §4.8; lane HW-MCP).

What is held here, over recorded swarm-api answers (the shapes
`swarm_api.awaitinput.step_view`, `list_waiting` and `answer` serve):

* `swarm_workflow_input` sends ONLY what the person typed into the host's
  elicitation. A tool call whose arguments carry a verdict is refused and
  sends nothing; an elicitation that is declined sends nothing; the payload
  posted is the elicited content, never the caller's;
* a host that cannot elicit is refused with the step's console link, and
  nothing is sent;
* files the person declines are not sent, and files they accept go as one
  multipart answer carrying exactly those bytes;
* a file list over the bound (count or bytes) is refused before anything is
  read from the API or sent to it;
* `swarm_inputs` lists the tenant's waits;
* `swarm_workflow_status` shows a waiting step's window and an answered
  step's record;
* `/sc check` lists a fresh wait as `awaiting_input`, not STUCK, and as STUCK
  once `expires_at` has passed by more than two ticks of the expiry sweep.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from swarm_mcp import awaitinput, check, server, workflows
from swarm_mcp.client import SwarmClient, SwarmError

WID = "wf_review"
SID = "review"
TID = "task_review"
INPUT_PATH = f"/v1/workflows/{WID}/steps/{SID}/input"
CONSOLE = f"https://console.test/workflows/{WID}"

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _view(*, form="verdict", max_files=10, status="waiting", questions=None) -> dict:
    """`GET .../steps/{step}/input`, as `awaitinput.step_view` serves it."""
    return {
        "workflow_id": WID, "step_id": SID, "task_id": TID, "status": status,
        "spec_verified": True,
        "prompt": "Read draft.md. Approve it, or list what must change.",
        "form": form, "artifact": "review.json", "max_files": max_files,
        "max_answer_bytes": 16 * 1024 * 1024, "tenant": "eng", "may_answer": True,
        "remind": ["owner@example.test"], "opened_at": _iso(NOW - timedelta(hours=1)),
        "expires_at": _iso(NOW + timedelta(hours=3)), "on_expire": "fail",
        "shows": [{"step_id": "draft", "task_id": "task_draft", "name": "draft.md"}],
        "questions": questions, "answer": None,
    }


def _answered() -> dict:
    return {"workflow_id": WID, "step_id": SID, "task_id": TID, "state": "SUCCEEDED",
            "artifact": {"name": "review.json", "bytes": 40, "sha256": "0" * 64}, "files": [],
            "answered_by": "dev@example.test", "answered_at": _iso(NOW), "via": "mcp",
            "redaction_count": 0, "replayed": False}


class Api:
    """swarm-api's await-input routes and the workflow read, recorded."""

    tenant = "eng"

    def __init__(self, view: dict | None = None):
        self.view = view or _view()
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        self.calls.append((method, path, payload))
        route = urlsplit(path).path
        if method == "GET" and route == INPUT_PATH:
            return json.loads(json.dumps(self.view))
        if method == "POST" and route == INPUT_PATH:
            return _answered()
        if method == "GET" and route == "/v1/inputs":
            return {"items": [{"workflow_id": WID, "step_id": SID, "task_id": TID,
                               "opened_at": _iso(NOW - timedelta(hours=1)),
                               "expires_at": _iso(NOW + timedelta(hours=3))}],
                    "truncated": False}
        if method == "GET" and route == f"/v1/workflows/{WID}":
            return {"workflow": {"workflow_id": WID, "steps": [
                {"step_id": SID, "task_id": TID, "links": {"console": f"{CONSOLE}?step={SID}"}}],
                "links": {"console": CONSOLE}}, "tasks": []}
        raise AssertionError(f"unexpected call {method} {path}")

    def posted(self) -> list:
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def sent(monkeypatch):
    """Every multipart answer the bridge sends: (path, content type, body)."""
    out: list[tuple[str, str, bytes]] = []

    def _post(client, path, content_type, body):  # noqa: ARG001
        out.append((path, content_type, body))
        return _answered()

    monkeypatch.setattr(awaitinput, "post_multipart", _post)
    return out


@pytest.fixture
def host(monkeypatch):
    """A host that can ask its human. `answers` is what the person gives;
    `asked` every (message, schema) the bridge put to them."""
    asked: list[tuple[str, dict]] = []
    answers: list = []

    def _elicit(message, schema, **_):
        asked.append((message, schema))
        return answers.pop(0)

    monkeypatch.setattr(server, "_elicit", _elicit)
    server._HOST_CAN_ELICIT.set()
    yield asked, answers
    server._HOST_CAN_ELICIT.clear()


def _accept(content: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "x", "result": {"action": "accept", "content": content}}


def _call(api, **args):
    return json.loads(server._call(api, "swarm_workflow_input", {"workflow_id": WID, "step_id": SID, **args}))


# --------------------------------------------------------------------------
# the answer comes only from the elicitation
# --------------------------------------------------------------------------


def test_the_tool_takes_no_argument_that_could_carry_an_answer():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow_input")
    properties = set(tool["inputSchema"]["properties"])
    assert properties == {"workflow_id", "step_id", "files"}, properties


@pytest.mark.parametrize("smuggled", [
    {"verdict": "MERGE"},
    {"payload": {"verdict": "MERGE", "findings": []}},
    {"answer": {"text": "ship it"}},
    {"text": "ship it"},
])
def test_a_call_whose_arguments_carry_an_answer_sends_nothing(host, sent, smuggled):
    asked, answers = host
    api = Api()
    answers.append(_accept({"verdict": "MERGE", "findings": ""}))
    with pytest.raises(SwarmError, match="does not take"):
        _call(api, **smuggled)
    assert api.calls == [] and sent == [] and asked == []


def test_the_payload_posted_is_what_the_person_typed(host, sent):
    asked, answers = host
    api = Api()
    answers.append(_accept({"verdict": "NOT_YET", "findings": "tighten section 2\n\n  name the owner  "}))

    reply = _call(api)

    (method, path, payload), = api.posted()
    assert (method, path) == ("POST", INPUT_PATH) and sent == []
    assert payload["payload"] == {"verdict": "NOT_YET",
                                  "findings": ["tighten section 2", "name the owner"]}
    assert payload["via"] == "mcp"
    assert awaitinput.ANSWER_ID.match(payload["answer_id"])
    assert reply["answered_by"] == "dev@example.test"
    message, schema = asked[0]
    assert schema["properties"]["verdict"]["enum"] == ["MERGE", "NOT_YET"]
    assert "Read draft.md" in message and "draft.md" in message


@pytest.mark.parametrize("reply", [
    None,
    {"jsonrpc": "2.0", "id": "x", "result": {"action": "decline"}},
    {"jsonrpc": "2.0", "id": "x", "result": {"action": "cancel"}},
    {"jsonrpc": "2.0", "id": "x", "error": {"code": -1, "message": "no"}},
])
def test_an_unanswered_or_declined_elicitation_sends_nothing(host, sent, reply):
    _, answers = host
    api = Api()
    answers.append(reply)
    with pytest.raises(SwarmError, match="nothing was sent"):
        _call(api)
    assert api.posted() == [] and sent == []


def test_a_text_form_sends_the_typed_text(host, sent):
    _, answers = host
    api = Api(_view(form="text"))
    answers.append(_accept({"text": "Use the second draft."}))
    _call(api)
    (_, _, payload), = api.posted()
    assert payload["payload"] == {"text": "Use the second draft."}


def test_an_answers_form_asks_each_question_and_sends_every_answer(host, sent):
    asked, answers = host
    questions = [
        {"question": "Which database?", "options": [{"label": "Firestore"}, {"label": "Postgres"}]},
        {"question": "Anything else?", "options": []},
    ]
    api = Api(_view(form="answers", questions=questions))
    answers.append(_accept({"q1": "Postgres", "q2_text": "no"}))
    _call(api)
    (_, _, payload), = api.posted()
    assert payload["payload"] == {"answers": [
        {"question_index": 0, "choice": "Postgres", "text": None},
        {"question_index": 1, "choice": None, "text": "no"},
    ]}
    _, schema = asked[0]
    assert schema["properties"]["q1"]["enum"] == ["Firestore", "Postgres"]


def test_an_answers_form_with_a_question_left_blank_sends_nothing(host, sent):
    _, answers = host
    questions = [{"question": "Which database?", "options": [{"label": "Firestore"}]}]
    api = Api(_view(form="answers", questions=questions))
    answers.append(_accept({}))
    with pytest.raises(SwarmError, match="nothing was sent"):
        _call(api)
    assert api.posted() == []


def test_a_step_that_is_not_waiting_is_refused_before_anyone_is_asked(host, sent):
    asked, _ = host
    api = Api(_view(status="answered"))
    with pytest.raises(SwarmError, match="answered"):
        _call(api)
    assert asked == [] and api.posted() == []


# --------------------------------------------------------------------------
# a host that cannot ask its human
# --------------------------------------------------------------------------


def test_a_host_that_cannot_elicit_is_refused_with_the_console_link(sent):
    server._HOST_CAN_ELICIT.clear()
    api = Api()
    with pytest.raises(SwarmError) as refused:
        _call(api)
    text = str(refused.value)
    assert f"{CONSOLE}?step={SID}" in text and "nothing was sent" in text
    assert api.posted() == [] and sent == []


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


def _files(tmp_path, count: int, size: int = 16) -> list[str]:
    paths = []
    for index in range(count):
        path = tmp_path / f"note{index}.md"
        path.write_bytes(secrets.token_hex(size // 2).encode())
        paths.append(str(path))
    return paths


def test_files_the_person_declines_are_not_sent(host, sent, tmp_path):
    asked, answers = host
    api = Api()
    paths = _files(tmp_path, 2)
    answers.append(_accept({"verdict": "MERGE", "findings": "", "send_files": False}))

    reply = _call(api, files=paths)

    assert sent == [], "a declined file went out"
    (method, _, payload), = api.posted()
    assert method == "POST" and payload["payload"]["verdict"] == "MERGE"
    assert reply.get("files_sent") == []
    message, schema = asked[0]
    assert "send_files" in schema["properties"]
    assert "note0.md" in message and "note1.md" in message
    assert awaitinput.FILE_WARNING in message


def test_files_the_person_accepts_are_sent_byte_for_byte(host, sent, tmp_path):
    asked, answers = host
    api = Api()
    paths = _files(tmp_path, 2)
    answers.append(_accept({"verdict": "MERGE", "findings": "", "send_files": True}))

    _call(api, files=paths)

    assert api.posted() == []
    (path, content_type, body), = sent
    assert path == INPUT_PATH and content_type.startswith("multipart/form-data; boundary=")
    for name in paths:
        with open(name, "rb") as handle:
            assert handle.read() in body
    import hashlib

    digest = hashlib.sha256(open(paths[0], "rb").read()).hexdigest()
    assert digest in asked[0][0], "the person was not shown what they were sending"
    assert b'name="answer"' in body and body.count(b'name="file"') == 2


def test_a_file_list_over_the_count_bound_is_refused_before_anything_is_sent(host, sent, tmp_path):
    asked, _ = host
    api = Api()
    with pytest.raises(SwarmError, match="nothing was sent"):
        _call(api, files=_files(tmp_path, awaitinput.MAX_FILES + 1))
    assert api.calls == [] and sent == [] and asked == []


def test_a_file_list_over_the_steps_own_bound_is_refused_before_anyone_is_asked(host, sent, tmp_path):
    asked, _ = host
    api = Api(_view(max_files=1))
    with pytest.raises(SwarmError, match="at most 1"):
        _call(api, files=_files(tmp_path, 2))
    assert api.posted() == [] and sent == [] and asked == []


def test_a_file_list_over_the_byte_bound_is_refused_before_anything_is_sent(host, sent, tmp_path, monkeypatch):
    asked, _ = host
    monkeypatch.setattr(awaitinput, "MAX_ANSWER_BYTES", 20)
    api = Api()
    with pytest.raises(SwarmError, match="nothing was sent"):
        _call(api, files=_files(tmp_path, 2, size=16))
    assert api.calls == [] and sent == [] and asked == []


def test_a_missing_file_is_refused_before_anything_is_sent(host, sent, tmp_path):
    api = Api()
    with pytest.raises(SwarmError, match="nothing was sent"):
        _call(api, files=[str(tmp_path / "absent.md")])
    assert api.calls == [] and sent == []


def test_the_bridges_bounds_are_the_apis():
    from swarm_api import awaitinput as api_awaitinput
    from swarm_api.validation import AWAIT_INPUT_MAX_FILES

    assert awaitinput.MAX_FILES == AWAIT_INPUT_MAX_FILES
    assert awaitinput.MAX_ANSWER_BYTES == api_awaitinput.MAX_ANSWER_BYTES
    assert awaitinput.ANSWER_ID.pattern == api_awaitinput.ANSWER_ID_RE.pattern
    assert "mcp" in api_awaitinput.VIA


def test_the_multipart_body_is_one_the_api_parses(tmp_path):
    from swarm_api.routes.await_input import parse_multipart

    answer = {"answer_id": "mcp-0123", "payload": {"text": "hi"}, "via": "mcp"}
    data = secrets.token_bytes(64)
    content_type, body = awaitinput.multipart(answer, [("a.png", data), ("b.md", b"# b\n")])
    parsed, files = parse_multipart(content_type, body)
    assert parsed == answer
    assert files == [("a.png", data), ("b.md", b"# b\n")]


# --------------------------------------------------------------------------
# the tenant's waits, and the status rows
# --------------------------------------------------------------------------


def test_swarm_inputs_lists_the_tenants_waits():
    api = Api()
    reply = json.loads(server._call(api, "swarm_inputs", {}))
    (method, path, _), = api.calls
    assert method == "GET" and urlsplit(path).path == "/v1/inputs"
    assert parse_qs(urlsplit(path).query)["status"] == ["waiting"]
    (item,) = reply["items"]
    assert (item["workflow_id"], item["step_id"]) == (WID, SID)
    assert "swarm_workflow_input" in item["answer_with"]


def _await_task(**metadata) -> dict:
    return {"id": TID, "state": "PARKED", "park_reason": "AWAITING_INPUT",
            "runner_profile": "await-input", "metadata": metadata}


def test_a_waiting_step_row_carries_its_window_and_an_answered_one_its_record():
    declaration = {"form": "verdict", "artifact": "review.json", "max_files": 10,
                   "remind": ["owner@example.test"], "expires_after_seconds": 86400}
    waiting = _await_task(
        dispatch={"await_input": declaration},
        await_input={"opened_at": _iso(NOW), "expires_at": _iso(NOW + timedelta(days=1))},
    )
    answered = {**waiting, "state": "SUCCEEDED", "park_reason": None, "metadata": {
        "dispatch": {"await_input": declaration},
        "await_input": {"opened_at": _iso(NOW), "expires_at": _iso(NOW + timedelta(days=1)),
                        "answer": {"answered_by": "dev@example.test", "answered_at": _iso(NOW),
                                   "via": "mcp", "files": [{"name": "review.files/a.md",
                                                            "bytes": 3, "sha256": "1" * 64}]}}}}
    envelope = {"tenant_id": "eng", "workflow": {"workflow_id": WID, "steps": [
        {"step_id": SID, "task_id": TID, "runner_profile": "await-input"}]}}

    (row,) = workflows.step_rows({**envelope, "tasks": [waiting]})
    assert row["awaiting"] == {"tenant": "eng", "opened_at": _iso(NOW),
                               "expires_at": _iso(NOW + timedelta(days=1)), "form": "verdict",
                               "artifact": "review.json", "max_files": 10,
                               "remind": ["owner@example.test"]}
    assert "answered" not in row

    (row,) = workflows.step_rows({**envelope, "tasks": [answered]})
    assert row["answered"] == {"answered_by": "dev@example.test", "answered_at": _iso(NOW),
                               "via": "mcp", "files": ["review.files/a.md"]}
    assert "awaiting" not in row


def test_a_spec_step_may_declare_await_input_and_it_is_sent_as_given():
    block = {"form": "text", "expires_after_seconds": 3600, "on_expire": "fail"}
    (step,) = workflows.build_steps([{
        "step_id": SID, "prompt": "Say which draft.", "runner_profile": "claude-code",
        "await_input": block,
    }])
    assert step["await_input"] == block
    with pytest.raises(SwarmError, match="await_input"):
        workflows.build_steps([{"step_id": SID, "prompt": "x", "await_input": "yes"}])


# --------------------------------------------------------------------------
# /sc check
# --------------------------------------------------------------------------


class _Reads:
    """`check._Reads`, answering the park event an hour ago."""

    def events(self, task_id):  # noqa: ARG002
        return [{"type": "parked", "at": _iso(NOW - timedelta(hours=1))}]

    def task(self, task_id):  # noqa: ARG002
        return None


def _wait(expires_at: datetime) -> dict:
    return {**_await_task(await_input={"opened_at": _iso(NOW - timedelta(hours=1)),
                                       "expires_at": _iso(expires_at)}),
            "created_at": _iso(NOW - timedelta(hours=2))}


def test_check_lists_a_fresh_wait_as_awaiting_input_and_not_stuck():
    flag = check.classify_task(_wait(NOW + timedelta(hours=3)), _Reads(), now=NOW, window=20)
    assert flag is not None and flag["kind"] == check.AWAITING_INPUT
    assert flag["kind"] != check.STUCK
    assert flag["evidence"]["expires_at"] == _iso(NOW + timedelta(hours=3))


def test_check_lists_a_wait_just_past_expiry_as_awaiting_input_still():
    within = NOW - timedelta(seconds=2 * check.EXPIRY_TICK_SECONDS - 1)
    flag = check.classify_task(_wait(within), _Reads(), now=NOW, window=20)
    assert flag["kind"] == check.AWAITING_INPUT


def test_check_flags_a_wait_two_ticks_past_expiry_as_stuck():
    past = NOW - timedelta(seconds=2 * check.EXPIRY_TICK_SECONDS + 1)
    flag = check.classify_task(_wait(past), _Reads(), now=NOW, window=20)
    assert flag["kind"] == check.STUCK
    assert "expiry" in flag["reason"]


SESSION = "11111111-2222-4333-8444-555555555555"
REPO = "https://github.com/acme/app"


class _CheckApi:
    """The reads `/sc check` makes of one running workflow with a waiting step."""

    tenant = None
    # The check lists through the client's own `workflows` (CHECK-FILTER),
    # which builds the query and calls `request` below.
    workflows = SwarmClient.workflows

    def __init__(self, expires_at: datetime):
        self.task = {**_wait(expires_at), "tenant_id": "eng", "repository_url": REPO,
                     "claude_session": SESSION,
                     "links": {"console": f"https://console.test/agents/{TID}"}}
        self.task["metadata"]["claude_session"] = SESSION
        self.workflow = {
            "workflow_id": WID, "tenant_id": "eng", "state": "RUNNING", "stored_state": "RUNNING",
            "state_source": "derived", "claude_session": SESSION, "created_at": _iso(NOW),
            "steps": [{"step_id": SID, "task_id": TID, "runner_profile": "await-input",
                       "depends_on": []}],
            "links": {"console": CONSOLE},
        }

    def request(self, method, path, **_):
        assert method == "GET", path
        route, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if route == "/v1/workflows":
            rows = [self.workflow] if "active" in query else []
            return {"workflows": json.loads(json.dumps(rows)), "next_page_token": None,
                    "tenant_id": "eng"}
        if route == f"/v1/workflows/{WID}":
            return {"workflow": json.loads(json.dumps(self.workflow)), "tenant_id": "eng",
                    "tasks": [json.loads(json.dumps(self.task))]}
        if route == "/v1/tasks":
            return {"tasks": [], "next_page_token": None, "tenant_id": "eng"}
        if route == check.OPEN_QUESTIONS_ROUTE:
            return {"tasks": [], "complete": True, "tenant_id": "eng"}
        if route.endswith("/events"):
            return {"events": _Reads().events(TID), "next_page_token": None}
        if route == "/v1/tenants/mine":
            return []
        raise AssertionError(path)


def _check(expires_at: datetime) -> dict:
    return check.run(_CheckApi(expires_at), repository=REPO, now=NOW, names=lambda _c: {})


def test_a_fresh_wait_is_reported_apart_from_what_is_flagged(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    result = _check(NOW + timedelta(hours=3))
    assert result["flagged"] == [] and result["count"]["stuck"] == 0
    (item,) = result["awaiting_input"]
    assert (item["kind"], item["workflow_id"], item["step_id"]) == (check.AWAITING_INPUT, WID, SID)
    assert "waits for a member" in item["reason"]


def test_an_overdue_wait_is_flagged_stuck(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    result = _check(NOW - timedelta(minutes=10))
    assert result["awaiting_input"] == []
    (item,) = result["flagged"]
    assert item["kind"] == check.STUCK and item["rule"] == "awaiting_input"


def test_human_required_stays_stuck():
    task = {**_await_task(), "park_reason": "HUMAN_REQUIRED", "runner_profile": "claude-code",
            "created_at": _iso(NOW - timedelta(hours=2))}
    flag = check.classify_task(task, _Reads(), now=NOW, window=20)
    assert flag["kind"] == check.STUCK and flag["rule"] == "parked"
