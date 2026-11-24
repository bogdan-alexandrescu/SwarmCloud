"""`swarm workflow-pause` / `workflow-resume` and the `swarm_workflow_pause` /
`swarm_workflow_resume` tools, against the REAL swarm-api routes.

docs/design/workflow-pause.md sections 3, 9 and 11 (lane P5, the CLI and
plugin half). The pair sits beside the cancel pair and is held to its shape:
the API's answer is passed through, its refusal is reported in the API's own
words, and nothing is retried behind the caller's back -- a 503 for contended
steps is the API saying "call again", and a call again is the caller's to make
(a retry is idempotent and never moves `paused_at`, section 3.1).

Built as `test_against_the_real_api.py` builds its application: the real
FastAPI app over an in-memory Firestore, and `SwarmClient`'s own transport
pointed at it, so the method, path and body checked are the bytes the client
really sends. Offline: no credentials, no emulator, no network.

`tests/unit` is on sys.path via this directory's conftest, which says why.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api import store as store_mod
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from swarm_mcp import cli, server
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient, SwarmError

from control_plane.conftest import api_settings, seed_pool, seed_tenant
from control_plane.fakes import FakeFirestore

REPO = Path(__file__).resolve().parents[3]
AUTH = {"Authorization": "Bearer token-alice"}
BASE = "http://api.invalid"
WORKFLOW = {
    "steps": [
        {"step_id": "a", "runner_profile": "mock", "input": {"prompt": "a"}},
        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"], "input": {"prompt": "b"}},
    ]
}


# --------------------------------------------------------------------------
# The real application, and SwarmClient's real transport pointed at it
# --------------------------------------------------------------------------


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def ctx(db):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(
            {
                "token-alice": {
                    "email": "alice@example.com",
                    "email_verified": True,
                    "sub": "sub-alice",
                    "hd": "example.com",
                }
            }
        ),
        groups=StaticGroups({"alice@example.com": ("eng@example.com",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )


@pytest.fixture()
def api(ctx, db) -> TestClient:
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=10)
    return TestClient(create_app(ctx), raise_server_exceptions=False)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.code = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture()
def sent(api, monkeypatch) -> list[tuple[str, str, object]]:
    """Every request the client really sent: (method, path, decoded body).

    Recorded at the transport, below `SwarmClient.request`, so a retry the
    client made on its own would show up here as a second entry."""
    log: list[tuple[str, str, object]] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len(BASE):]
        body = json.loads(req.data) if req.data else None
        log.append((req.get_method(), path, body))
        headers = {**dict(req.headers), **AUTH}
        response = api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error",
                response.headers, io.BytesIO(response.content),
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return log


@pytest.fixture()
def swarm(sent, monkeypatch) -> SwarmClient:
    client = SwarmClient(base_url=BASE)
    # `cli.main` builds its own client; hand it this one so the whole command
    # -- parse, call, and the error report on stderr -- is the shipped path.
    monkeypatch.setattr(cli, "SwarmClient", lambda **_kw: client)
    return client


def _create(api: TestClient) -> tuple[str, dict[str, str]]:
    """A workflow made through the real route, not through the code under test."""
    created = api.post("/v1/workflows", headers=AUTH, json=WORKFLOW)
    assert created.status_code in (200, 201), created.text
    workflow = created.json()["workflow"]
    return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


def _writes(sent) -> list[tuple[str, str, object]]:
    return [entry for entry in sent if entry[0] != "GET"]


# --------------------------------------------------------------------------
# The argument parse
# --------------------------------------------------------------------------


def test_workflow_pause_parses_a_workflow_id_and_an_optional_reason():
    args = cli.build_parser().parse_args(["workflow-pause", "wf_1"])
    assert args.workflow_id == "wf_1"
    assert args.reason is None
    assert args.func is cli.cmd_workflow_pause

    args = cli.build_parser().parse_args(
        ["workflow-pause", "wf_1", "--reason", "waiting on the schema review"]
    )
    assert args.reason == "waiting on the schema review"


def test_workflow_resume_parses_a_workflow_id_and_takes_no_reason(capsys):
    args = cli.build_parser().parse_args(["workflow-resume", "wf_1"])
    assert args.workflow_id == "wf_1"
    assert args.func is cli.cmd_workflow_resume
    # The resume route takes an empty body and refuses any field, so the
    # command offers none rather than sending one the API will refuse.
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["workflow-resume", "wf_1", "--reason", "x"])
    capsys.readouterr()


@pytest.mark.parametrize("command", ["workflow-pause", "workflow-resume"])
def test_both_commands_need_a_workflow_id(command, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command])
    capsys.readouterr()


def test_the_commands_sit_beside_workflow_cancel():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(actions[0].choices)
    assert {"workflow-cancel", "workflow-pause", "workflow-resume"} <= commands


# --------------------------------------------------------------------------
# The commands: method, path and body, and what they print
# --------------------------------------------------------------------------


def test_workflow_pause_posts_the_reason_and_says_what_it_signalled(swarm, api, sent, db, capsys):
    workflow_id, steps = _create(api)

    code = cli.main(["workflow-pause", workflow_id, "--reason", "waiting on the schema review"])

    assert code == cli.EXIT_OK
    assert _writes(sent) == [
        ("POST", f"/v1/workflows/{workflow_id}/pause", {"reason": "waiting on the schema review"})
    ]
    assert db.docs[f"workflows/{workflow_id}"]["paused"] is True
    assert db.docs[f"workflows/{workflow_id}"]["pause_reason"] == "waiting on the schema review"
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(f"{workflow_id} paused")
    for task_id in steps.values():
        assert task_id in out


def test_workflow_pause_without_a_reason_sends_no_reason(swarm, api, sent, capsys):
    workflow_id, _steps = _create(api)

    assert cli.main(["workflow-pause", workflow_id]) == cli.EXIT_OK

    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/pause", {})]
    capsys.readouterr()


def test_a_repeat_pause_says_it_was_already_paused_and_keeps_the_first_time(
    swarm, api, sent, db, capsys
):
    workflow_id, _steps = _create(api)
    assert api.post(f"/v1/workflows/{workflow_id}/pause", headers=AUTH).status_code == 200
    first = db.docs[f"workflows/{workflow_id}"]["paused_at"]

    assert cli.main(["workflow-pause", workflow_id]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "already paused" in out.splitlines()[0]
    assert db.docs[f"workflows/{workflow_id}"]["paused_at"] == first


def test_workflow_resume_posts_an_empty_body_and_says_it_resumed(swarm, api, sent, db, capsys):
    workflow_id, steps = _create(api)
    assert api.post(f"/v1/workflows/{workflow_id}/pause", headers=AUTH).status_code == 200

    assert cli.main(["workflow-resume", workflow_id]) == cli.EXIT_OK

    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/resume", {})]
    assert db.docs[f"workflows/{workflow_id}"]["paused"] is False
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith(f"{workflow_id} resumed")
    for task_id in steps.values():
        assert task_id in out


def test_resuming_a_workflow_that_was_not_paused_says_so(swarm, api, sent, capsys):
    workflow_id, _steps = _create(api)

    assert cli.main(["workflow-resume", workflow_id]) == cli.EXIT_OK

    assert "was not paused" in capsys.readouterr().out.splitlines()[0]


# --------------------------------------------------------------------------
# The tools: method, path and body, and the API's answer passed through
# --------------------------------------------------------------------------


def test_the_pause_tool_posts_the_reason_and_passes_the_answer_through(swarm, api, sent, db):
    workflow_id, steps = _create(api)

    report = json.loads(
        server._call(swarm, "swarm_workflow_pause", {"workflow_id": workflow_id, "reason": "hold"})
    )

    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/pause", {"reason": "hold"})]
    assert report["paused"] is True
    assert report["paused_at"]
    assert sorted(report["steps_signalled"]) == sorted(steps.values())
    assert db.docs[f"workflows/{workflow_id}"]["pause_reason"] == "hold"


def test_the_pause_tool_without_a_reason_sends_no_reason(swarm, api, sent):
    workflow_id, _steps = _create(api)

    server._call(swarm, "swarm_workflow_pause", {"workflow_id": workflow_id})

    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/pause", {})]


def test_the_resume_tool_posts_an_empty_body_and_passes_the_answer_through(swarm, api, sent):
    workflow_id, steps = _create(api)
    assert api.post(f"/v1/workflows/{workflow_id}/pause", headers=AUTH).status_code == 200

    report = json.loads(server._call(swarm, "swarm_workflow_resume", {"workflow_id": workflow_id}))

    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/resume", {})]
    assert report["paused"] is False
    assert sorted(report["steps_cleared"]) == sorted(steps.values())


# --------------------------------------------------------------------------
# Refusals: in the API's words, and never retried silently
# --------------------------------------------------------------------------


def _refusal(api: TestClient, path: str) -> dict:
    """What the API itself answers, read straight from it, to compare with."""
    response = api.post(path, headers=AUTH, json={})
    assert response.status_code >= 400, response.text
    return {"status": response.status_code, **response.json()}


def _finished(api, db) -> str:
    workflow_id, _steps = _create(api)
    db.docs[f"workflows/{workflow_id}"]["state"] = "SUCCEEDED"
    return workflow_id


def _being_cancelled(api, db) -> str:
    workflow_id, _steps = _create(api)
    db.docs[f"workflows/{workflow_id}"]["cancel_requested"] = True
    return workflow_id


def _other_tenants(api, db) -> str:
    workflow_id, _steps = _create(api)
    seed_tenant(db, "research")
    db.docs[f"workflows/{workflow_id}"]["tenant_id"] = "research"
    return workflow_id


REFUSALS = {
    "finished": (_finished, 409, "workflow_terminal"),
    "being-cancelled": (_being_cancelled, 409, "workflow_cancel_requested"),
    "another-tenants": (_other_tenants, 404, None),
}


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refused_command_prints_the_apis_words_and_sends_once(
    swarm, api, db, sent, capsys, action, case
):
    make, status, code = REFUSALS[case]
    workflow_id = make(api, db)
    expected = _refusal(api, f"/v1/workflows/{workflow_id}/{action}")
    assert expected["status"] == status
    if code is not None:
        assert expected["code"] == code

    exit_code = cli.main([f"workflow-{action}", workflow_id])

    assert exit_code == cli.EXIT_FAIL
    err = capsys.readouterr().err
    assert expected["message"] in err, err
    assert _writes(sent) == [
        ("POST", f"/v1/workflows/{workflow_id}/{action}", {})
    ], "a refusal was retried"


@pytest.mark.parametrize("action", ["pause", "resume"])
@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refused_tool_raises_the_apis_words_and_sends_once(
    swarm, api, db, sent, action, case
):
    make, status, code = REFUSALS[case]
    workflow_id = make(api, db)
    expected = _refusal(api, f"/v1/workflows/{workflow_id}/{action}")

    with pytest.raises(SwarmError) as exc:
        server._call(swarm, f"swarm_workflow_{action}", {"workflow_id": workflow_id})

    assert exc.value.status == status
    if code is not None:
        assert exc.value.code == code
    assert expected["message"] in server._tool_error_text(exc.value)
    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/{action}", {})]


def _contend(ctx, monkeypatch, action: str, task_id: str) -> None:
    """The store's step transaction for `task_id` stays contended, as the
    route's own test makes it (test_workflow_pause_routes.py)."""
    method = "_signal_step_pause" if action == "pause" else "_clear_step_pause"
    real = getattr(ctx.store, method)

    def contended(tenant_id, step_task_id, **kwargs):
        if step_task_id == task_id:
            raise store_mod._StepContended(step_task_id)
        return real(tenant_id, step_task_id, **kwargs)

    monkeypatch.setattr(ctx.store, method, contended)


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_a_contended_503_is_reported_in_the_apis_words_and_not_retried(
    swarm, api, ctx, db, sent, capsys, monkeypatch, action
):
    workflow_id, steps = _create(api)
    if action == "resume":
        assert api.post(f"/v1/workflows/{workflow_id}/pause", headers=AUTH).status_code == 200
    _contend(ctx, monkeypatch, action, steps["b"])

    exit_code = cli.main([f"workflow-{action}", workflow_id])

    assert exit_code == cli.EXIT_FAIL
    err = capsys.readouterr().err
    # The API's sentence, and the step it could not reach.
    assert "met concurrent writes on every attempt" in err, err
    assert steps["b"] in err, err
    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/{action}", {})], (
        "a 503 was retried behind the caller's back"
    )


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_a_contended_503_from_a_tool_is_the_apis_refusal_and_not_retried(
    swarm, api, ctx, db, sent, monkeypatch, action
):
    workflow_id, steps = _create(api)
    if action == "resume":
        assert api.post(f"/v1/workflows/{workflow_id}/pause", headers=AUTH).status_code == 200
    _contend(ctx, monkeypatch, action, steps["b"])

    with pytest.raises(SwarmError) as exc:
        server._call(swarm, f"swarm_workflow_{action}", {"workflow_id": workflow_id})

    assert exc.value.status == 503
    assert exc.value.code == "pause_contended"
    text = server._tool_error_text(exc.value)
    assert "met concurrent writes on every attempt" in text
    assert steps["b"] in text
    assert _writes(sent) == [("POST", f"/v1/workflows/{workflow_id}/{action}", {})]


# --------------------------------------------------------------------------
# The tools' descriptions and schemas, and every list that names the cancel pair
# --------------------------------------------------------------------------


def _tool(name: str) -> dict:
    return next(t for t in server.TOOLS if t["name"] == name)


@pytest.mark.parametrize("name", ["swarm_workflow_pause", "swarm_workflow_resume"])
def test_the_tools_say_they_write_and_act_only_on_the_developers_word(name):
    text = _tool(name)["description"]
    assert "WRITES" in text
    assert "developer's word" in text


def test_the_pause_tool_takes_a_workflow_id_and_an_optional_bounded_reason():
    schema = _tool("swarm_workflow_pause")["inputSchema"]
    assert set(schema["properties"]) == {"workflow_id", "reason"}
    assert schema["required"] == ["workflow_id"]
    assert schema["properties"]["reason"]["type"] == "string"
    assert schema["properties"]["reason"]["maxLength"] == 500


def test_the_resume_tool_takes_only_a_workflow_id():
    schema = _tool("swarm_workflow_resume")["inputSchema"]
    assert set(schema["properties"]) == {"workflow_id"}
    assert schema["required"] == ["workflow_id"]


def test_the_delegate_skill_grants_the_pair_under_both_registrations():
    head = (REPO / "plugin" / "skills" / "delegate" / "SKILL.md").read_text().split("---")[1]
    for name in ("swarm_workflow_pause", "swarm_workflow_resume"):
        for prefix in ("mcp__swarmcloud__", "mcp__plugin_sc_swarmcloud__"):
            assert re.search(rf"^\s*- {prefix}{name}\s*$", head, re.M), f"{prefix}{name}"
