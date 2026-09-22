"""`sc` and `swarm` against the REAL swarm-api, and against the REAL edge.

WHY THIS FILE EXISTS SEPARATELY FROM test_bridge.py. `test_bridge`'s FakeClient
answers `task()` with a bare task document carrying a `task_id` key. The API
answers `{"task": {...}}` and the id field is `id`. Both ends were written,
nothing was built in the middle, and every test passed because the fake agreed
with the consumer rather than with the server -- the same shape as the three
missing seams found on 2026-09-19 (swarm-api had no broker URL, the worker had
no broker URL, the browser called a route swarm-api did not have), each of
which was found by a probe against the deployed service rather than by a suite.

So nothing here fakes the control plane. The real FastAPI application is built
over an in-memory Firestore -- the same fixtures `tests/unit/control_plane`
uses -- and `SwarmClient`'s own transport is pointed at it, so what is checked
is the bytes the server really emits flowing through the client the CLI really
uses. Offline: no credentials, no emulator, no network, nothing created.

The edge half cannot be built offline, so it is pinned from MEASUREMENTS taken
against the deployed front door on 2026-09-22 and recorded beside each test.
Those strings are Google's, not ours; the tests assert on the SHAPE that made
the old guards miss them -- a plain-text body under an HTML content type, and a
redirect that resolves to a 200 -- never on Google's exact wording.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from swarm_mcp import auth as mcp_auth
from swarm_mcp import cli
from swarm_mcp import client as mcp_client
from swarm_mcp import patches, server
from swarm_mcp.auth import REACHES, WHY_NOT, Tier
from swarm_mcp.client import SwarmClient, SwarmError

# `tests/unit` is on sys.path via this directory's conftest, which says why.
from control_plane.conftest import api_settings, seed_tenant
from control_plane.fakes import FakeFirestore

REPO = Path(__file__).resolve().parents[3]
AUTH = {"Authorization": "Bearer token-alice"}


# --------------------------------------------------------------------------
# The real application, and SwarmClient's real transport pointed at it
# --------------------------------------------------------------------------


@pytest.fixture()
def db() -> FakeFirestore:
    """The store behind the real application.

    Exposed because the only way to put a workflow's steps into the states that
    reproduce the rollup defect is to move the step TASKS -- there is no route
    that sets a task state, and there should not be one.
    """
    return FakeFirestore()


@pytest.fixture()
def api(db) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(
            {
                "token-alice": {
                    "email": "alice@saga.xyz",
                    "email_verified": True,
                    "sub": "sub-alice",
                    "hd": "saga.xyz",
                }
            }
        ),
        groups=StaticGroups({"alice@saga.xyz": ("eng@saga.xyz",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    seed_tenant(db, "eng")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


class _Response(io.BytesIO):
    """What an opener hands back: a file object plus a status."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.code = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _install(monkeypatch, opener) -> None:
    """Replace the transport wherever this version of the client keeps it.

    Both spellings are set so the file runs against the code before and after
    the no-redirect opener exists; a test that could only run against the fix
    cannot demonstrate the defect.
    """
    monkeypatch.setattr(mcp_client, "_open", opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", opener)


@pytest.fixture()
def swarm(api, monkeypatch) -> SwarmClient:
    """A real SwarmClient whose socket is the real application.

    Only the transport is replaced. `SwarmClient.request` -- its headers, its
    JSON handling, its error mapping -- and every method above it are the
    shipped ones, which is the half that was never exercised against a real
    response body.
    """

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid") :]
        # The client's OWN headers, Content-Type included -- sending only the
        # Authorization would make every POST a 422 about the body and hide
        # whatever the test was actually about.
        headers = {**dict(req.headers), **AUTH}
        response = api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                response.status_code,
                "error",
                response.headers,
                io.BytesIO(response.content),
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    _install(monkeypatch, _opener)
    return SwarmClient(base_url="http://api.invalid")


def submit(api: TestClient) -> str:
    """One real task, created through the real route. Returns its id.

    Setup goes through the API rather than through the bridge, so a test about
    the bridge cannot be set up by the very call it is checking.
    """
    created = api.post(
        "/v1/tasks",
        json={"runner_profile": "mock", "input": {"prompt": "hi"}},
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    return created.json()["task"]["id"]


# --------------------------------------------------------------------------
# Every route the bridge calls
# --------------------------------------------------------------------------


#: `client.request("GET", "/v1/...")` and `client.request("GET", f"/v1/...")`.
#: Read out of the source rather than from a list kept in this file, because a
#: list kept in this file is one more thing that only agrees with itself.
_CALL = re.compile(r'request\(\s*"(GET|POST|PUT|DELETE)"\s*,\s*f?"(/v1[^"]*)"')


def calls() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for name in ("client.py", "cli.py", "sc.py", "workflows.py"):
        source = (REPO / "apps/swarm-mcp/swarm_mcp" / name).read_text()
        for method, raw in _CALL.findall(source):
            # A query string is not part of the route, and an f-string hole is
            # the path parameter the router declares -- so the comparison is
            # about the SHAPE of the route rather than one instance of it.
            found.add((method, raw.split("?", 1)[0]))
    return found


def test_every_route_the_bridge_calls_exists_on_this_api(api):
    """THE SEAM TEST, in the shape test_account_api.py uses for the web UI.

    `sc` had never been run against the deployed platform, so nothing had ever
    checked that the routes it names are routes swarm-api serves. A string
    comparison against the real router, not against a mock: a mock agrees with
    whatever the client happens to ask for.
    """
    # The OpenAPI document rather than `app.routes`, which is a tree of
    # `_IncludedRouter` wrappers whose `path` is None -- iterating it yields an
    # empty set, and an empty set makes this assertion pass for every route in
    # the world. The document is also what the API actually publishes.
    spec = api.app.openapi()["paths"]
    served = {
        (method.upper(), path) for path, methods in spec.items() for method in methods
    }
    assert ("GET", "/v1/stats") in served, "the served set was not built"
    missing = sorted(call for call in calls() if call not in served)
    assert not missing, (
        f"the bridge calls routes this API does not serve: {missing}. "
        "Both ends exist and the seam does not."
    )


def test_the_route_scan_actually_found_the_calls():
    """A regex that matched nothing would make the test above pass by silence
    -- the same failure as a probe that prints PASS having never looked."""
    found = calls()
    assert len(found) >= 9, found
    for expected in (
        ("GET", "/v1/accounts"),
        ("GET", "/v1/capacity"),
        ("GET", "/v1/stats"),
        ("GET", "/v1/tenants/me"),
        ("POST", "/v1/tasks"),
        ("GET", "/v1/tasks/{task_id}/attempts"),
        # The workflow routes are the whole of G1 and they are spoken from a
        # fourth module; a scan that did not read it would pass by silence.
        ("POST", "/v1/workflows"),
        ("GET", "/v1/workflows/{workflow_id}"),
        ("POST", "/v1/workflows/{workflow_id}/cancel"),
    ):
        assert expected in found, f"{expected} was not scanned out of the source"


# --------------------------------------------------------------------------
# The envelope: what the API really answers
# --------------------------------------------------------------------------


def test_the_api_wraps_a_task_and_names_its_id_id(api):
    """The fact every test below is written against, pinned on its own so that
    if the API ever stops wrapping, the reason these tests change is visible
    rather than inferred from six failures at once."""
    created = api.post(
        "/v1/tasks",
        json={"runner_profile": "mock", "input": {"prompt": "hi"}},
        headers=AUTH,
    )
    assert created.status_code == 201
    assert set(created.json()) == {"task", "scheduler_woken"}
    assert "id" in created.json()["task"]
    assert "task_id" not in created.json()["task"]

    fetched = api.get(f"/v1/tasks/{created.json()['task']['id']}", headers=AUTH)
    assert set(fetched.json()) == {"task"}


def test_dispatch_prints_a_task_id_a_shell_can_use(swarm):
    """`swarm dispatch` prints this and a shell pipes it into `swarm tail`.

    It printed an empty line: the API answers `{"task": {...}}` and names the
    id `id`, and `cmd_dispatch` read `task_id` off the envelope.
    """
    args = type(
        "A",
        (),
        {
            "prompt": "hi",
            "profile": "mock",
            "repo": None,
            "ref": None,
            "label": None,
            "timeout": None,
            "model": None,
            "json": False,
        },
    )()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.cmd_dispatch(swarm, args) == cli.EXIT_OK
    printed = out.getvalue().strip()
    assert printed.startswith("task_"), f"printed {printed!r}, which is not a task id"


def test_dispatch_batch_returns_the_tasks_and_not_their_envelope(swarm):
    """`POST /v1/tasks/batch` answers `{"tasks": [...], "count": n, ...}`. A
    caller iterating the envelope iterates its KEYS -- three strings, one of
    which is "count" -- so a batch of two reads as a batch of three that have
    no ids."""
    created = swarm.dispatch_batch(
        [
            {"runner_profile": "mock", "input": {"prompt": "a"}},
            {"runner_profile": "mock", "input": {"prompt": "b"}},
        ]
    )
    assert len(created) == 2
    ids = [mcp_client.task_id_of(task) for task in created]
    assert all(task_id.startswith("task_") for task_id in ids), ids
    assert len(set(ids)) == 2


def test_status_reports_the_state_the_api_reported(swarm, api):
    task_id = submit(api)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_status(swarm, type("A", (), {"task_ids": [task_id]})())
    line = out.getvalue().strip()
    assert "None" not in line, f"the state read as None: {line!r}"
    assert task_id in line


def test_a_live_log_uri_can_be_built_from_a_real_task(swarm, api, monkeypatch):
    """`swarm tail` built this from `task['task_id']` and raised KeyError --
    not a SwarmError, so it escaped every handler in the CLI as a traceback,
    after spinning forever because `task.get('state')` was never terminal."""
    monkeypatch.setenv("SWARM_ARTIFACT_BUCKET", "bucket")
    task_id = submit(api)
    uri = cli._live_log_uri(swarm.task(task_id), "attempt_1", "stdout")
    assert uri == (
        f"gs://bucket/tenants/eng/tasks/{task_id}"
        "/attempts/attempt_1/logs/live/stdout.tail.log"
    )


def test_the_reason_there_is_no_patch_names_the_state_it_read(swarm, api):
    """`explain_absence` prefixes the TASK's state, and on the envelope there
    was no state: every task on the platform -- finished, failed, parked or
    never started -- came back as `unknown: no result summary was written`,
    which is the sentence for a parked attempt and sends the reader to an
    event detail that is not there."""
    task = swarm.task(submit(api))
    assert patches.patch_uri(task) is None
    why = patches.explain_absence(task)
    assert not why.startswith("unknown:"), why
    assert why.startswith(task["state"] + ":"), why


def test_a_tail_that_cannot_read_the_attempts_route_says_so(swarm, api, monkeypatch):
    """`_latest_attempt` swallowed the error and returned None, which `tail`
    reads as "no attempt has begun yet" -- so a 403 or a timeout on that route
    produced a tail that printed events forever and never a log line, with
    nothing on screen to say it had stopped being able to look."""
    task_id = submit(api)
    assert cli._latest_attempt(swarm, task_id) == (None, None), (
        "a task with no attempts yet is an absence, and must stay one"
    )

    def _refuse(method, path, **kwargs):  # noqa: ARG001
        raise SwarmError(f"{method} {path} -> 403: forbidden")

    monkeypatch.setattr(swarm, "request", _refuse)
    attempt, why = cli._latest_attempt(swarm, task_id)
    assert attempt is None
    assert why is not None and "403" in str(why)


def test_the_mcp_result_tool_reports_the_task_it_was_asked_about(swarm, api):
    """`swarm_result` answered `{"task_id": null, "state": null}` for every
    task on the platform, which a session reads as "it has not started yet"."""
    task_id = submit(api)
    described = patches.describe_task(swarm.task(task_id))
    assert described["task_id"] == task_id
    assert described["state"], "swarm_result reported no state"


def test_the_mcp_dispatch_tool_hands_back_a_followable_command(swarm):
    """`follow_live_with` came back as `swarm tail ` -- the command with the id
    missing, which is worse than absent because it still looks runnable."""
    payload = json.loads(
        server._call(swarm, "swarm_dispatch", {"prompt": "hi", "profile": "mock"})
    )
    assert payload["task_id"].startswith("task_")
    assert payload["follow_live_with"] == f"swarm tail {payload['task_id']}"
    assert payload["state"]


def test_the_mcp_wait_tool_notices_a_terminal_task(swarm, api):
    """`swarm_wait` compares `task.get("state")` against TERMINAL. On the
    envelope that is None, so a finished task was never finished and the tool
    blocked for its whole timeout -- an hour by default."""
    task_id = submit(api)
    api.post(f"/v1/tasks/{task_id}/cancel", headers=AUTH)
    payload = json.loads(
        server._call(swarm, "swarm_wait", {"task_ids": [task_id], "timeout_seconds": 0})
    )
    assert [f["task_id"] for f in payload["finished"]] == [task_id]
    assert "still_running" not in payload


def test_cancel_returns_the_task_and_not_the_envelope(swarm, api):
    task_id = submit(api)
    cancelled = swarm.cancel(task_id)
    assert cancelled.get("id") == task_id
    assert cancelled.get("state")


def test_integrate_skips_a_task_for_the_right_reason(swarm, api, tmp_path):
    """`integrate` reads `patch_uri(client.task(id))`. On the envelope that was
    None for every task, so it reported "skip" for all of them and exited 0 --
    a clean screen saying no agent had produced any code, forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "--quiet", "--initial-branch=main"],
        check=True,
        capture_output=True,
    )
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-c", "user.name=t", "-c", "user.email=t@e.com",
            "-C", str(repo), "commit", "--quiet", "-m", "base",
        ],
        check=True,
        capture_output=True,
    )

    task_id = submit(api)
    result = patches.integrate(swarm, [task_id], repo, branch="wip", base=None)
    assert result.skipped, "a task with no patch must be skipped, with a reason"
    skipped_id, why = result.skipped[0]
    assert skipped_id == task_id
    assert not why.startswith("unknown:"), (
        f"the reason describes the envelope, not the task: {why}"
    )


# --------------------------------------------------------------------------
# Workflows: the feature the bridge could not reach at all
# --------------------------------------------------------------------------
#
# `server.py` contained the string "workflow" zero times, so every test below is
# of something that had no caller. They run against the REAL application for the
# reason this whole file exists: the thing most likely to be wrong is the shape
# of the response, and a fake of the control plane agrees with whoever wrote it.

#: A fan-in in miniature: one step, then a second that depends on it AND stages
#: its artifact. `mock` because the catalogue gives it `provider=None`, so it
#: needs no credential and the suite stays offline.
DAG = {
    "steps": [
        {"step_id": "research", "prompt": "read the code", "runner_profile": "mock"},
        {
            "step_id": "draft",
            "prompt": "write it up",
            "runner_profile": "mock",
            "depends_on": ["research"],
            "input_from": {"research": "research.md"},
        },
    ]
}


def _submit_dag(swarm) -> dict:
    return json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))


def test_the_workflow_tool_submits_a_dag_the_real_api_accepts(swarm, api):
    created = _submit_dag(swarm)
    assert created["workflow_id"].startswith("wf_")
    steps = {s["step_id"]: s for s in created["steps"]}
    assert set(steps) == {"research", "draft"}
    assert steps["draft"]["depends_on"] == ["research"]
    assert steps["draft"]["input_from"] == {"research": "research.md"}
    for step in steps.values():
        # A step with no task id can be neither followed, applied nor cancelled,
        # and every tool downstream of this one takes that string.
        assert step["task_id"], f"{step['step_id']} came back with no task id"
    assert created["follow_live_with"].startswith("swarm tail task_")
    # A create response derives nothing, and the tool says where a state comes
    # from rather than quoting the stored QUEUED as though it were one.
    assert "state" not in created
    assert "swarm_workflow_status" in created["state_available_from"]


def test_the_step_prompt_reaches_the_task_the_api_created(swarm, api):
    """The submitted prompt must be in `input.prompt` on the step's own task.

    This is the W1 defect measured from the other end. The web UI's New Workflow
    screen sends steps with no `input` at all; the API accepts them and every
    CLI-agent step then fails at the agent, after admission and dispatch. A tool
    whose steps arrived with `input == {}` would be the same bug with a
    different keyboard in front of it.
    """
    created = _submit_dag(swarm)
    task_id = next(s["task_id"] for s in created["steps"] if s["step_id"] == "research")
    task = api.get(f"/v1/tasks/{task_id}", headers=AUTH).json()["task"]
    assert task["input"] == {"prompt": "read the code"}


def test_a_step_with_no_prompt_is_refused_before_a_workflow_exists(swarm, api, db):
    """Refused at the keyboard, where it costs nothing.

    The same submission four minutes later costs a dispatch, a lease and a pod,
    and fails at every step deterministically. The assertion that nothing was
    written matters as much as the refusal: a partial workflow would leave step
    tasks nobody is going to look for.
    """
    with pytest.raises(SwarmError) as exc:
        server._call(
            swarm,
            "swarm_workflow",
            {"steps": [{"step_id": "a", "runner_profile": "mock"}]},
        )
    assert "prompt" in str(exc.value)
    assert not [key for key in db.docs if key.startswith("workflows/")]
    assert not [key for key in db.docs if key.startswith("tasks/")]


def test_the_workflow_status_tool_reports_the_derived_state_not_the_stored_field(
    swarm, api, db
):
    """THE named test for G1(b), end to end through the real routes.

    `Workflow.state` in Firestore is written once at submission and nothing
    advances it: on 2026-09-22 `wf_bcdc9180e4fb4a209f31` read QUEUED while its
    three steps were SUCCEEDED, FAILED and CANCELLED, and a six-step workflow
    whose steps had ALL succeeded read QUEUED too. So the two steps below are
    moved to terminal states and the workflow document is deliberately NOT
    touched -- which is exactly what the platform does to itself.

    What the bridge may report is then the derived value and only the derived
    value. Serving `stored_state` here would put "QUEUED" in a developer's
    terminal directly above two steps that had already finished.
    """
    created = _submit_dag(swarm)
    workflow_id = created["workflow_id"]
    tasks = {s["step_id"]: s["task_id"] for s in created["steps"]}
    db.docs[f"tasks/{tasks['research']}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{tasks['draft']}"]["state"] = "FAILED"

    report = json.loads(
        server._call(swarm, "swarm_workflow_status", {"workflow_id": workflow_id})
    )

    assert report["stored_state"] == "QUEUED", "the fixture did not reproduce the defect"
    assert report["state_source"] == "derived"
    assert report["state"] == "FAILED", (
        "the bridge served something other than the derived rollup; the stored "
        f"field says {report['stored_state']!r}"
    )
    assert report["state"] != report["stored_state"]
    # The cache WAS wrong at the moment of this read, even though the route
    # repaired it on the way past. A caller needs that to distrust earlier reads.
    assert report["stored_state_was_wrong"] is True
    assert report["counts"] == {"SUCCEEDED": 1, "FAILED": 1}
    assert {s["step_id"]: s["state"] for s in report["steps"]} == {
        "research": "SUCCEEDED",
        "draft": "FAILED",
    }
    # And no refusal: the server DID derive on this read, so the bridge has a
    # state to give and must not also be reporting one as unavailable.
    assert "state_unavailable_because" not in report


def test_the_workflow_status_tool_reports_why_a_step_is_parked(swarm, api):
    """`draft` depends on `research`, so it parks. PARKED holds no capacity
    (invariant 1), and the reason is the whole content of that fact."""
    created = _submit_dag(swarm)
    report = json.loads(
        server._call(
            swarm, "swarm_workflow_status", {"workflow_id": created["workflow_id"]}
        )
    )
    draft = next(s for s in report["steps"] if s["step_id"] == "draft")
    assert draft["state"] == "PARKED"
    assert draft["park_reason"] == "DEPENDENCY_INCOMPLETE"
    assert draft["depends_on"] == ["research"]


def test_the_workflow_result_tool_says_why_a_step_has_no_patch(swarm, api, db):
    """A step that produced no patch is not a step that did nothing. The six
    causes `swarm_result` distinguishes have to survive the trip through a
    workflow, or the workflow view is the one place they collapse into one."""
    created = _submit_dag(swarm)
    tasks = {s["step_id"]: s["task_id"] for s in created["steps"]}
    db.docs[f"tasks/{tasks['research']}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{tasks['draft']}"]["state"] = "SUCCEEDED"

    report = json.loads(
        server._call(
            swarm, "swarm_workflow_result", {"workflow_id": created["workflow_id"]}
        )
    )
    assert report["state"] == "SUCCEEDED"
    produced = {s["step_id"]: s["produced"] for s in report["steps"]}
    assert produced["research"]["patch"] is None
    assert produced["research"]["no_patch_because"], "a missing patch with no reason"
    assert produced["research"]["pull_request"] is None


def test_the_workflow_cancel_tool_separates_cancelled_from_already_finished(
    swarm, api, db
):
    """A step that had already finished is the cancel arriving late, not a
    failed cancel. An operator who cannot see the difference runs it again."""
    created = _submit_dag(swarm)
    tasks = {s["step_id"]: s["task_id"] for s in created["steps"]}
    db.docs[f"tasks/{tasks['research']}"]["state"] = "SUCCEEDED"

    report = json.loads(
        server._call(
            swarm, "swarm_workflow_cancel", {"workflow_id": created["workflow_id"]}
        )
    )
    assert report["cancel_requested"] is True
    assert report["tasks_already_terminal"] == [tasks["research"]]
    assert report["tasks_cancelled"] == [tasks["draft"]]


def test_a_refused_dag_keeps_the_sentence_that_says_why(swarm, api):
    """`ApiError.to_payload` puts the sentence in `message` and the evidence in
    `detail`; `_explain` preferred `detail` and threw the sentence away, so a
    cycle came back as `{'cycle': [...]}` with no words at all."""
    with pytest.raises(SwarmError) as exc:
        server._call(
            swarm,
            "swarm_workflow",
            {
                "steps": [
                    {"step_id": "build", "prompt": "x", "runner_profile": "mock",
                     "depends_on": ["test"]},
                    {"step_id": "test", "prompt": "y", "runner_profile": "mock",
                     "depends_on": ["build"]},
                ]
            },
        )
    message = str(exc.value)
    # The SENTENCE, which only `message` carries. `detail` for this refusal is
    # `{"cycle": ["build", "test", "build"]}`, whose repr also contains the word
    # "cycle" and both step ids -- so asserting on those alone passes against
    # the defect. The arrow-joined path is in the message and nowhere else.
    assert "workflow dependency graph contains a cycle" in message
    assert "build -> test -> build" in message
    # ... and the evidence is kept too, rather than one half traded for the other.
    assert '"cycle"' in message


# --------------------------------------------------------------------------
# The edge: what Google really answers
# --------------------------------------------------------------------------
#
# MEASURED 2026-09-22 against https://swarm.saga.xyz -- an external ALB with
# IAP in front of swarm-api, whose Cloud Run ingress is
# internal-and-cloud-load-balancing:
#
#   no Authorization header  -> 302, body "Invalid IAP credentials: empty token"
#   Authorization: garbage   -> 401, body "Invalid IAP credentials: Unable to
#                               parse JWT"
#   a syntactically valid    -> 401, body "Invalid IAP credentials: JWT
#   but unsigned JWT            signature is invalid"
#
# All three carry `content-type: text/html` and NONE of them is HTML. HTML is
# what `_is_edge_page` looked for, so every real IAP refusal fell through to
# the "this is the application's own message" path and the remedy this module
# exists to print was never printed. The wrong-audience case -- the one the
# module is named for -- could not be produced without minting a real token,
# so these match on the family prefix rather than on a sentence.

IAP_302 = "Invalid IAP credentials: empty token"
IAP_401_UNPARSEABLE = "Invalid IAP credentials: Unable to parse JWT"
IAP_401_SIGNATURE = "Invalid IAP credentials: JWT signature is invalid"
IAP_ALL = (IAP_302, IAP_401_UNPARSEABLE, IAP_401_SIGNATURE)


@pytest.mark.parametrize("body", IAP_ALL)
def test_a_real_iap_refusal_is_recognised_as_the_edge_answering(body):
    """The `edge` flag is DATA the caller grades on -- `fetch_accounts` uses it
    to tell "this deployment has no /v1/accounts route" from "you never reached
    the API at all" -- so getting it wrong is not cosmetic."""
    assert mcp_client._is_edge_page(body) is True


@pytest.mark.parametrize("body", [IAP_401_UNPARSEABLE, IAP_401_SIGNATURE])
def test_a_real_iap_refusal_names_the_remedy(body):
    """The whole point of this module: `Invalid JWT audience` read as a bug in
    the platform rather than as "your laptop cannot mint that kind of token,
    and here is the one it can"."""
    message = mcp_client._explain(401, body)
    assert "SWARM_IAP_CLIENT_ID" in message
    # Google's own sentence says WHICH way the credential was wrong, which the
    # remedy does not; dropping it would trade one half-answer for another.
    assert body.split(": ", 1)[1] in message


def test_an_applications_own_json_error_is_still_passed_through_unchanged():
    """The guard above must not start claiming IAP for the API's own 401s --
    "your token is fine, your tenant is disabled" needs its own words."""
    assert mcp_client._explain(401, '{"detail": "tenant is disabled"}') == (
        "tenant is disabled"
    )


def test_a_redirect_to_a_sign_in_page_is_not_followed(monkeypatch):
    """urllib follows 30x on GET, so an unauthenticated call to the front door
    resolved to a 200 carrying Google's sign-in HTML. Two consequences, both
    real: `json.loads` raised JSONDecodeError -- not a SwarmError, so `swarm
    doctor`, the command whose entire job is to explain this, ended in a
    traceback -- and CPython's redirect handler carries the Authorization
    header to the new host, which here is accounts.google.com.
    """
    seen: list[str] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        seen.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url,
            302,
            "Found",
            {"Location": "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"},
            io.BytesIO(IAP_302.encode()),
        )

    monkeypatch.setenv("SWARM_ID_TOKEN", "a.b.c")
    _install(monkeypatch, _opener)
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="https://swarm.example.com").request("GET", "/v1/stats")

    assert seen == ["https://swarm.example.com/v1/stats"], (
        "the client followed the redirect and made a second request"
    )
    assert caught.value.edge is True
    assert "SWARM_IAP_CLIENT_ID" in str(caught.value)


def test_the_real_transport_refuses_a_redirect_and_does_not_forward_the_token():
    """The REAL urllib path, over loopback, in the shape test_account_api.py
    uses for the broker's redirect: two throwaway servers on 127.0.0.1,
    because the whole point is that the second connection is never opened.

    The test above can only observe that no second request came through the
    seam it controls. Here the redirect would be followed inside the opener,
    with the Authorization header attached, before any code in this package
    ran again -- so if the handler chain did not refuse it, this would record
    the bearer arriving at the other host.

    Loopback only: nothing leaves the machine, and the "credential" is the
    string `a.b.c`.
    """
    followed: list[str] = []

    class _Second(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            followed.append(self.headers.get("Authorization") or "(none)")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):  # noqa: ARG002
            pass

    second = ThreadingHTTPServer(("127.0.0.1", 0), _Second)
    threading.Thread(target=second.serve_forever, daemon=True).start()
    elsewhere = f"http://127.0.0.1:{second.server_address[1]}/"

    class _First(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", elsewhere)
            self.end_headers()
            self.wfile.write(IAP_302.encode())

        def log_message(self, *a):  # noqa: ARG002
            pass

    first = ThreadingHTTPServer(("127.0.0.1", 0), _First)
    threading.Thread(target=first.serve_forever, daemon=True).start()

    try:
        request = urllib.request.Request(f"http://127.0.0.1:{first.server_address[1]}/v1/stats")
        request.add_header("Authorization", "Bearer a.b.c")
        with pytest.raises(urllib.error.HTTPError) as caught:
            mcp_client._open(request, timeout=5)
        assert caught.value.code == 302
    finally:
        first.shutdown()
        second.shutdown()

    assert followed == [], (
        f"the redirect was followed and carried {followed} to the other host"
    )


def test_a_non_json_success_is_a_readable_error_not_a_traceback(monkeypatch):
    """Defence for every other way Google's HTML arrives with a 2xx: a caching
    proxy, a captive portal, an ALB with a path rule pointing somewhere else."""

    def _opener(req, timeout=None):  # noqa: ARG001
        return _Response(b"<!doctype html><html>sign in</html>", 200)

    monkeypatch.setenv("SWARM_ID_TOKEN", "a.b.c")
    _install(monkeypatch, _opener)
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="https://swarm.example.com").request("GET", "/v1/stats")
    assert "JSONDecodeError" not in str(caught.value)
    assert caught.value.edge is True


def test_a_truncated_json_body_says_so_rather_than_claiming_the_edge(monkeypatch):
    """The guard above must not relabel every parse failure as Google's doing:
    a body that begins like JSON came from the API, and "the API sent something
    this client could not read" is a different bug report."""

    def _opener(req, timeout=None):  # noqa: ARG001
        return _Response(b'{"tasks": [', 200)

    monkeypatch.setenv("SWARM_ID_TOKEN", "a.b.c")
    _install(monkeypatch, _opener)
    with pytest.raises(SwarmError) as caught:
        SwarmClient(base_url="https://swarm.example.com").request("GET", "/v1/tasks")
    assert caught.value.edge is False


# --------------------------------------------------------------------------
# What the tiers claim, against what is deployed
# --------------------------------------------------------------------------
#
# MEASURED 2026-09-22 against saga-agents-staging, read-only:
#
#   swarm-api ingress  internal-and-cloud-load-balancing
#   swarm-api invoker  allUsers, plus the IAP service agent and swarm-verify
#   swarm-ui-backend   an external ALB backend with iap.enabled = true
#   GET https://swarm-api-tonstldhta-uc.a.run.app/v1/tenants/me
#                      -> an HTML 404, WITH and WITHOUT an Authorization
#                         header: ingress answers before any token is read.
#
# That is the TEAM shape this table is about.


def test_the_service_account_tier_does_not_claim_a_team_deployment():
    """It claimed both, and the claim is unreachable in either direction.

    IMPERSONATE is only ever detected when the metadata server is NOT reachable
    -- that is the branch above it in `detect()` -- so the machine is outside
    GCP and therefore outside the VPC. A team deployment's Cloud Run ingress
    refuses it before reading the token (measured), and its load balancer
    accepts only a token whose audience is the IAP OAuth client id, which is
    the IAP tier. `swarm doctor` printed "reaches solo, team" and then an HTML
    404, and -- because WHY_NOT is consulted only for a profile the tier does
    NOT claim -- never named the one variable that was missing.
    """
    assert REACHES[Tier.IMPERSONATE] == ("solo",)
    assert "SWARM_IAP_CLIENT_ID" in WHY_NOT[(Tier.IMPERSONATE, "team")]


def test_the_metadata_tier_still_claims_a_team_deployment():
    """The contrast that makes the change above a fact rather than a mood: the
    deployed `swarm-verify` job runs INSIDE the VPC with `API_AUDIENCE` set to
    the service url (terraform/infra/verify.tf) and reaches exactly this
    deployment. Inside the VPC that audience works; outside it, none does."""
    assert "team" in REACHES[Tier.METADATA]


def test_doctor_names_the_missing_variable_for_a_profile_it_cannot_reach(monkeypatch):
    """`swarm doctor` is what a newcomer runs when nothing works, so a silent
    gap here is the failure this module was written to prevent."""
    for name in ("SWARM_ID_TOKEN", "SWARM_IAP_CLIENT_ID", "K_SERVICE", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    monkeypatch.setenv("PROJECT_ID", "saga-agents-staging")
    monkeypatch.setattr(mcp_auth, "_metadata_available", lambda timeout=0.3: False)
    # doctor's last step is a live read, and it is allowed to fail; nothing
    # here may touch the network or shell out to gcloud.
    monkeypatch.setattr(
        cli, "SwarmClient", lambda *a, **k: (_ for _ in ()).throw(SwarmError("no api"))
    )

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_doctor(None, None)
    printed = out.getvalue()
    assert "not team" in printed, printed
    assert "SWARM_IAP_CLIENT_ID" in printed, printed
    # These explanations are paragraphs. Unwrapped, this one is a single
    # 420-column line whose LAST sentence is the remedy, so the part worth
    # reading is the part that scrolls off.
    too_wide = [line for line in printed.splitlines() if len(line) > 80]
    assert not too_wide, too_wide
    # ...and wrapping must not have split the names it tells you to look up.
    assert "swarm-api" in printed and "SWARM_IAP_CLIENT_ID" in printed
