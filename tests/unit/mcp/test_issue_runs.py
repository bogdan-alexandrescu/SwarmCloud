"""`sc run --issue`, `sc plan approve|edit|reject` and their MCP tools (#454).

The API routes (`POST /v1/runs`, `GET /v1/runs[/{id}]`, `plan:approve|edit|
reject`) existed and nothing in the bridge called them. These hold the bridge
to the one rule the plan gate is for: AN APPROVAL SENDS THE DIGEST OF THE PLAN
IT SHOWED. Fetch, display, approve with that digest -- never re-fetch and
approve blindly -- and a plan that changed in between comes back as
`plan_changed`, said plainly, with nothing retried.

The fake records every request it is sent, so "nothing was sent" is the
measurement of an empty list, not an absence of evidence.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import runs, sc, server
from swarm_mcp.client import RunRefused, SwarmClient, SwarmError

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

PLAN = {
    "summary": "Add the thing the issue asks for.",
    "mode": "single",
    "requirements": ["the thing exists", "the thing is tested"],
    "overlaps": [{"ref": "o/r#7", "kind": "pull_request", "note": "touches the same file"}],
    "steps": [
        {
            "step_id": "build",
            "title": "Build it",
            "prompt": "Write the test first, then the thing.",
            "files": ["apps/x.py"],
            "tests": ["test_the_thing"],
            "estimate": "1h",
        }
    ],
}


def _run(state="PLANNED", **extra):
    run = {
        "id": "run_1",
        "tenant_id": "acme",
        "state": state,
        "terminal": state in ("DONE", "FAILED", "REJECTED", "CANCELLED"),
        "issue": {"ref": "o/r#5", "url": "https://github.com/o/r/issues/5"},
        "plan_approval": "required",
        "auto_merge": False,
        "fix_rounds": 3,
        "planner_task_id": "task_planner",
        "plan": PLAN if state != "PLANNING" else None,
        "plan_digest": DIGEST_A if state != "PLANNING" else None,
        "plan_revision": 1,
        "workflow_id": None,
        "ci_fix_round": 0,
        "ci_fix_workflows": [],
        "pull_request": None,
    }
    run.update(extra)
    return run


def _refusal(status, code, message, detail=None):
    return SwarmError(
        f"POST /v1/runs -> {status}: {message}", status=status, code=code, detail=detail
    )


class FakeApi:
    """Answers the run routes from a script; records everything it is sent."""

    def __init__(self, *, run=None, approve_error=None, create_error=None, reads=None):
        self.sent: list[tuple[str, str, object]] = []
        self.run = run or _run()
        self.approve_error = approve_error
        self.create_error = create_error
        #: A sequence of runs successive GETs answer, for --follow.
        self.reads = list(reads or [])

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if (method, path) == ("POST", "/v1/runs"):
            if self.create_error is not None:
                raise self.create_error
            return {"run": _run("PLANNING", auto_merge=payload["auto_merge"])}
        if method == "GET" and path.startswith("/v1/runs?"):
            return {"runs": [self.run], "next_page_token": None, "tenant_id": "acme"}
        if method == "GET" and path.startswith("/v1/runs/"):
            if self.reads:
                return {"run": self.reads.pop(0)}
            return {"run": self.run}
        if method == "POST" and path.endswith("/plan:approve"):
            if self.approve_error is not None:
                raise self.approve_error
            return {"run": _run("RUNNING", workflow_id="wf_1", approved_by="dev@example.com")}
        if method == "POST" and path.endswith("/plan:edit"):
            return {"run": _run(plan=payload["plan"], plan_digest=DIGEST_B, plan_revision=2)}
        if method == "POST" and path.endswith("/plan:reject"):
            return {"run": _run("REJECTED", rejected_by="dev@example.com",
                                rejection_reason=payload.get("reason"))}
        if method == "GET" and path.startswith("/v1/workflows/"):
            return {
                "workflow": {
                    "workflow_id": path.rsplit("/", 1)[-1],
                    "state": "RUNNING",
                    "state_source": "derived",
                    "steps": [{"step_id": "build", "task_id": "task_b"},
                              {"step_id": "review", "task_id": "task_r"}],
                },
                "tasks": [{"id": "task_b", "state": "RUNNING"}, {"id": "task_r", "state": "QUEUED"}],
            }
        if method == "GET" and path.startswith("/v1/tasks/"):
            return {"task": {"id": path.rsplit("/", 1)[-1], "state": "RUNNING"}}
        raise AssertionError(f"unexpected request {method} {path}")

    def posts(self):
        return [s for s in self.sent if s[0] == "POST"]


def _client(api: FakeApi) -> SwarmClient:
    """A SwarmClient whose transport is `api`: the methods under test are real."""
    client = object.__new__(SwarmClient)
    client.request = api.request  # type: ignore[method-assign]
    return client


def _args(**kw):
    base = {"json": False, "width": 100, "color": False, "ascii": True}
    base.update(kw)
    return type("Args", (), base)()


@pytest.fixture()
def typed(monkeypatch):
    """What the developer types at the prompt; the prompt itself is recorded."""
    asked: list[str] = []

    def install(answer: str):
        def _ask(prompt):
            asked.append(prompt)
            return answer

        monkeypatch.setattr(sc, "_ask", _ask)
        return asked

    return install


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

def test_create_run_sends_data_only_and_unwraps_the_run():
    api = FakeApi()
    run = _client(api).create_run(issue="o/r#5", plan_approval="auto", auto_merge=False, fix_rounds=2)
    assert run["id"] == "run_1"
    assert api.sent == [("POST", "/v1/runs", {
        "issue": "o/r#5", "plan_approval": "auto", "auto_merge": False, "fix_rounds": 2,
    })]
    # Invariant 10: nothing in the payload could select an image or a command.
    assert not {"runner_profile", "image", "command", "token"} & set(api.sent[0][2])


def test_the_client_calls_the_routes_the_api_serves():
    api = FakeApi()
    c = _client(api)
    c.runs(limit=5)
    c.run("run_1")
    c.approve_plan("run_1", plan_digest=DIGEST_A)
    c.edit_plan("run_1", plan_digest=DIGEST_A, plan=PLAN)
    c.reject_plan("run_1", plan_digest=DIGEST_A, reason="not now")
    assert [(m, p) for m, p, _ in api.sent] == [
        ("GET", "/v1/runs?limit=5"),
        ("GET", "/v1/runs/run_1"),
        ("POST", "/v1/runs/run_1/plan:approve"),
        ("POST", "/v1/runs/run_1/plan:edit"),
        ("POST", "/v1/runs/run_1/plan:reject"),
    ]
    assert api.sent[2][2] == {"plan_digest": DIGEST_A}
    assert api.sent[3][2] == {"plan_digest": DIGEST_A, "plan": PLAN}
    assert api.sent[4][2] == {"plan_digest": DIGEST_A, "reason": "not now"}


def test_a_response_without_a_run_is_an_error_not_an_empty_run():
    class Empty(FakeApi):
        def request(self, method, path, payload=None, **kw):
            return {"something": "else"}

    with pytest.raises(SwarmError, match="no `run` object"):
        _client(Empty()).run("run_1")


def test_the_api_error_code_and_detail_are_carried_on_the_error(monkeypatch):
    """`plan_changed` must be told apart by its code, not by its sentence."""
    body = json.dumps({
        "code": "plan_changed",
        "message": "the plan of run 'run_1' has changed since it was shown",
        "detail": {"plan_digest": DIGEST_B, "sent": DIGEST_A},
    }).encode()

    def _opener(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(body))

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener)
    c = SwarmClient(base_url="http://api.invalid")
    with pytest.raises(RunRefused) as exc:
        c.approve_plan("run_1", plan_digest=DIGEST_A)
    assert exc.value.status == 409 and exc.value.code == "plan_changed"
    assert exc.value.detail == {"plan_digest": DIGEST_B, "sent": DIGEST_A}
    text = str(exc.value)
    assert DIGEST_A in text and DIGEST_B in text
    assert "Nothing was done" in text and "sc plan show run_1" in text


@pytest.mark.parametrize(
    "code,status,expected",
    [
        ("auto_merge_unavailable", 422, "auto-merge is visible but not available yet"),
        ("invalid_plan", 422, "still holds the plan it had"),
        ("not_found", 404, "is not one of your tenant's runs"),
    ],
)
def test_each_refusal_is_said_plainly(code, status, expected):
    exc = mcp_client.run_refusal(_refusal(status, code, "the API's own sentence"), run_id="run_1")
    assert isinstance(exc, RunRefused)
    assert expected in str(exc)
    assert "the API's own sentence" in str(exc), "the API's reason is kept, not replaced"
    assert exc.code == code and exc.status == status


def test_an_edge_refusal_is_passed_through_unchanged():
    edge = SwarmError("an HTML 404 from Google's edge", status=404, edge=True)
    assert mcp_client.run_refusal(edge, run_id="run_1") is edge


# --------------------------------------------------------------------------
# the CLI parser
# --------------------------------------------------------------------------

def test_the_parser_reaches_every_run_command():
    p = sc.build_parser()
    create = p.parse_args(["run", "--issue", "o/r#5", "--plan", "auto", "--auto-merge",
                           "--fix-rounds", "2", "--follow"])
    assert create.func is sc.cmd_run
    assert (create.issue, create.plan_approval, create.auto_merge, create.fix_rounds, create.follow) == (
        "o/r#5", "auto", True, 2, True)
    assert p.parse_args(["run", "--issue", "o/r#5"]).plan_approval == "required"
    assert p.parse_args(["run", "show", "run_1"]).func is sc.cmd_run_show
    assert p.parse_args(["runs"]).func is sc.cmd_runs
    assert p.parse_args(["plan", "show", "run_1"]).func is sc.cmd_plan_show
    assert p.parse_args(["plan", "approve", "run_1"]).func is sc.cmd_plan_approve
    assert p.parse_args(["plan", "approve", "run_1", "--digest", DIGEST_A]).digest == DIGEST_A
    assert p.parse_args(["plan", "edit", "run_1", "--file", "p.json"]).file == "p.json"
    assert p.parse_args(["plan", "reject", "run_1", "--reason", "no"]).reason == "no"
    with pytest.raises(SystemExit):
        p.parse_args(["plan", "reject", "run_1"])  # --reason is required
    with pytest.raises(SystemExit):
        p.parse_args(["run", "--issue", "o/r#5", "--plan", "sometimes"])


def test_sc_run_without_an_issue_creates_nothing():
    api = FakeApi()
    args = sc.build_parser().parse_args(["run"])
    with pytest.raises(SwarmError, match="--issue"):
        sc.cmd_run(_client(api), args, io.StringIO())
    assert api.sent == []


# --------------------------------------------------------------------------
# approve with the digest that was SHOWN
# --------------------------------------------------------------------------

def test_approve_prints_the_plan_and_sends_the_digest_it_printed(typed):
    asked = typed("approve")
    api = FakeApi()
    out = io.StringIO()
    args = sc.build_parser().parse_args(["plan", "approve", "run_1", "--width", "100", "--ascii"])
    assert sc.cmd_plan_approve(_client(api), args, out) == sc.EXIT_OK
    printed = out.getvalue()
    # The plan, whole, and its digest, BEFORE anything was sent.
    assert "Write the test first, then the thing." in printed
    assert "the thing is tested" in printed and "o/r#7" in printed
    assert DIGEST_A in printed and DIGEST_A in asked[0]
    # One read to show, one approval with what was shown -- never a re-read.
    assert [(m, p) for m, p, _ in api.sent][:2] == [
        ("GET", "/v1/runs/run_1"), ("POST", "/v1/runs/run_1/plan:approve"),
    ]
    assert api.posts() == [("POST", "/v1/runs/run_1/plan:approve", {"plan_digest": DIGEST_A})]
    assert "/sc attach wf_1" in printed, "a RUNNING run is attachable through its workflow"


def test_approve_sends_nothing_unless_approve_is_typed(typed, monkeypatch):
    typed("yes")
    monkeypatch.setenv("SWARM_ASSUME_YES", "1")  # ignored: the confirmation is typed
    api = FakeApi()
    args = sc.build_parser().parse_args(["plan", "approve", "run_1"])
    with pytest.raises(SwarmError, match="nothing was sent"):
        sc.cmd_plan_approve(_client(api), args, io.StringIO())
    assert api.posts() == []


def test_approve_with_digest_sends_that_digest_without_reading_or_asking(typed):
    asked = typed("never read")
    api = FakeApi()
    args = sc.build_parser().parse_args(["plan", "approve", "run_1", "--digest", DIGEST_B])
    sc.cmd_plan_approve(_client(api), args, io.StringIO())
    assert asked == []
    assert api.sent[0] == ("POST", "/v1/runs/run_1/plan:approve", {"plan_digest": DIGEST_B})


def test_a_plan_changed_since_it_was_shown_is_refused_plainly_and_not_retried(typed):
    typed("approve")
    api = FakeApi(approve_error=SwarmError(
        "POST /v1/runs/run_1/plan:approve -> 409: changed", status=409, code="plan_changed",
        detail={"plan_digest": DIGEST_B, "sent": DIGEST_A},
    ))
    args = sc.build_parser().parse_args(["plan", "approve", "run_1"])
    with pytest.raises(RunRefused) as exc:
        sc.cmd_plan_approve(_client(api), args, io.StringIO())
    assert "Nothing was done" in str(exc.value) and DIGEST_B in str(exc.value)
    # Exactly one approval, with the SHOWN digest; the new one was never sent.
    assert api.posts() == [("POST", "/v1/runs/run_1/plan:approve", {"plan_digest": DIGEST_A})]


def test_only_a_planned_run_is_offered_for_approval(typed):
    asked = typed("approve")
    api = FakeApi(run=_run("PLANNING"))
    args = sc.build_parser().parse_args(["plan", "approve", "run_1"])
    with pytest.raises(SwarmError, match="only a PLANNED run"):
        sc.cmd_plan_approve(_client(api), args, io.StringIO())
    assert asked == [] and api.posts() == []


# --------------------------------------------------------------------------
# edit and reject
# --------------------------------------------------------------------------

def test_edit_from_a_file_sends_the_opened_digest(tmp_path):
    edited = {**PLAN, "summary": "A narrower change."}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(edited))
    api = FakeApi()
    args = sc.build_parser().parse_args(["plan", "edit", "run_1", "--file", str(path)])
    sc.cmd_plan_edit(_client(api), args, io.StringIO())
    assert api.posts() == [("POST", "/v1/runs/run_1/plan:edit", {"plan_digest": DIGEST_A, "plan": edited})]


def test_edit_in_the_editor_sends_nothing_when_the_plan_is_unchanged(monkeypatch):
    monkeypatch.setenv("EDITOR", "true")  # exits 0 and leaves the file as written
    monkeypatch.delenv("VISUAL", raising=False)
    api = FakeApi()
    out = io.StringIO()
    args = sc.build_parser().parse_args(["plan", "edit", "run_1"])
    assert sc.cmd_plan_edit(_client(api), args, out) == sc.EXIT_OK
    assert "unchanged; nothing was sent" in out.getvalue()
    assert api.posts() == []


def test_edit_refuses_text_that_is_not_json(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("{not json")
    api = FakeApi()
    args = sc.build_parser().parse_args(["plan", "edit", "run_1", "--file", str(path)])
    with pytest.raises(SwarmError, match="not JSON"):
        sc.cmd_plan_edit(_client(api), args, io.StringIO())
    assert api.posts() == []


def test_reject_sends_the_shown_digest_and_the_reason(typed):
    typed("reject")
    api = FakeApi()
    args = sc.build_parser().parse_args(["plan", "reject", "run_1", "--reason", "too broad"])
    sc.cmd_plan_reject(_client(api), args, io.StringIO())
    assert api.posts() == [
        ("POST", "/v1/runs/run_1/plan:reject", {"plan_digest": DIGEST_A, "reason": "too broad"})
    ]


# --------------------------------------------------------------------------
# auto-merge: visible but disabled, refused plainly
# --------------------------------------------------------------------------

def test_auto_merge_is_passed_through_and_its_refusal_printed_plainly(monkeypatch, capsys):
    api = FakeApi(create_error=SwarmError(
        "POST /v1/runs -> 422: auto_merge requires the merge chain (#295)",
        status=422, code="auto_merge_unavailable", detail={"requires": "#295"},
    ))

    class _Ctx:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return _client(api)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(sc, "SwarmClient", _Ctx)
    code = sc.main(["run", "--issue", "o/r#5", "--auto-merge"])
    err = capsys.readouterr().err
    assert code == sc.EXIT_FAIL
    assert api.sent[0][2]["auto_merge"] is True, "the flag is sent; the API decides"
    assert "auto-merge is visible but not available yet" in err
    assert "#295" in err and "nothing was created" in err


# --------------------------------------------------------------------------
# --follow: one row per compiled step, stops where only a person moves it
# --------------------------------------------------------------------------

def test_follow_stops_at_a_plan_waiting_for_approval():
    api = FakeApi(reads=[_run("PLANNING"), _run("PLANNED")])
    out = io.StringIO()
    slept: list[float] = []
    run = runs.follow(_client(api), "run_1", out, sc.render.Style(width=100, unicode=False),
                      interval=7, sleep=slept.append)
    printed = out.getvalue()
    assert run["state"] == "PLANNED"
    assert "PLANNING" in printed and "planner task_planner" in printed
    assert "waiting for your approval" in printed and "sc plan approve run_1" in printed
    assert slept == [7]


def test_follow_prints_one_row_per_step_and_ends_with_the_run():
    running = _run("RUNNING", workflow_id="wf_1")
    done = _run("DONE", workflow_id="wf_1", green_sha="abc123")
    api = FakeApi(reads=[running, running, done])
    out = io.StringIO()
    run = runs.follow(_client(api), "run_1", out, sc.render.Style(width=100, unicode=False),
                      sleep=lambda _s: None)
    printed = out.getvalue()
    assert run["state"] == "DONE"
    assert printed.count("build  RUNNING") == 1, "a step row is printed when it changes, once"
    assert "review  QUEUED" in printed and "DONE" in printed


def test_a_fixing_run_attaches_to_its_newest_fix_round():
    run = _run("FIXING", workflow_id="wf_1", ci_fix_workflows=["wf_fix1", "wf_fix2"])
    assert runs.active_workflow_id(run) == "wf_fix2"
    assert runs.attach_with(run) == "/sc attach wf_fix2"
    for state in ("PLANNING", "PLANNED", "CHECKING", "DONE"):
        assert runs.attach_with(_run(state, workflow_id="wf_1")) is None


# --------------------------------------------------------------------------
# the MCP tools
# --------------------------------------------------------------------------

RUN_TOOLS = {
    "swarm_run_issue": {"issue"},
    "swarm_runs": set(),
    "swarm_run": {"run_id"},
    "swarm_plan_approve": {"run_id", "plan_digest"},
    "swarm_plan_edit": {"run_id", "plan_digest", "plan"},
    "swarm_plan_reject": {"run_id", "reason"},
}


@pytest.mark.parametrize("name,required", sorted(RUN_TOOLS.items()))
def test_the_run_tools_are_advertised_with_their_schemas(name, required):
    tool = next((t for t in server.TOOLS if t["name"] == name), None)
    assert tool is not None, f"{name} is not advertised"
    schema = tool["inputSchema"]
    assert set(schema.get("required") or []) == required
    # Invariant 10: no tool takes a backend parameter or a credential.
    assert not {"image", "command", "runner_profile", "token", "forge_token"} & set(schema["properties"])


@pytest.mark.parametrize("name", sorted(RUN_TOOLS))
def test_an_unknown_argument_is_refused_and_nothing_is_sent(name):
    api = FakeApi()
    with pytest.raises(SwarmError, match="does not take"):
        server._call(_client(api), name, {"run_id": "run_1", "issue": "o/r#5", "token": "x"})
    assert api.sent == []


def test_swarm_plan_approve_sends_the_callers_digest_and_reads_nothing_first():
    api = FakeApi()
    answer = json.loads(server._call(_client(api), "swarm_plan_approve",
                                     {"run_id": "run_1", "plan_digest": DIGEST_A}))
    assert api.sent[0] == ("POST", "/v1/runs/run_1/plan:approve", {"plan_digest": DIGEST_A})
    assert answer["run"]["state"] == "RUNNING"
    assert answer["attach_with"] == "/sc attach wf_1"
    assert [r["step_id"] for r in answer["steps"]["steps"]] == ["build", "review"]


def test_swarm_plan_approve_without_a_digest_sends_nothing():
    api = FakeApi()
    with pytest.raises(SwarmError, match="plan_digest"):
        server._call(_client(api), "swarm_plan_approve", {"run_id": "run_1"})
    assert api.sent == []


def test_a_plan_changed_refusal_reaches_the_tool_caller_in_words():
    api = FakeApi(approve_error=SwarmError(
        "409", status=409, code="plan_changed", detail={"plan_digest": DIGEST_B},
    ))
    with pytest.raises(SwarmError) as exc:
        server._call(_client(api), "swarm_plan_approve", {"run_id": "run_1", "plan_digest": DIGEST_A})
    text = server._tool_error_text(exc.value)
    assert "changed since it was shown" in text and "Nothing was done" in text


def test_swarm_run_on_a_planned_run_says_to_show_the_plan_before_approving():
    api = FakeApi()
    answer = json.loads(server._call(_client(api), "swarm_run", {"run_id": "run_1"}))
    assert answer["run"]["plan_digest"] == DIGEST_A
    assert "THAT digest" in answer["next"]
    assert "attach_with" not in answer


def test_swarm_run_issue_passes_auto_merge_strictly():
    api = FakeApi()
    server._call(_client(api), "swarm_run_issue", {"issue": "o/r#5", "auto_merge": "false"})
    assert api.sent[0][2]["auto_merge"] is False, "the string 'false' is not true"
