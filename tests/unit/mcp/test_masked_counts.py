"""`swarm result`, `status` and `workflow-status`, and the MCP tools, say `masked N` (owner decision, 2026-09-26).

The owner decided on #184 that the input is masked everywhere: `GET
/v1/tasks/{id}` serves a read-time-redacted input with its count, "so the CLI
and plugin show it masked too". The bridge never printed the input; what it
owes is the count -- how many credential-shaped strings the API masked in the
task's input and metadata -- so a reader of `swarm result` knows a prompt read
back is the masked copy, and that there was something to mask.

AGAINST THE REAL swarm-api, as `test_against_the_real_api.py` argues at
length: the application is built over an in-memory Firestore, and the shipped
`SwarmClient` is pointed at it, so the counts are the ones the server really
emits. Offline: no credentials, no emulator, no network.

A count the API did not send is `—` in a terminal and `null` in a tool's JSON,
never 0: a deployment older than the change serves the input unmasked, and 0
would say it looked.

MUTATIONS: drop `masked N` from any of the three commands; print 0 for a count
the API did not send; read only the input's count and not the metadata's;
drop `masked` from `swarm_status`, `swarm_result` or a workflow step's row.
"""

from __future__ import annotations

import argparse
import io
import json
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from swarm_mcp import cli, patches, server
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient

from control_plane.conftest import api_settings, seed_task, seed_tenant
from control_plane.fakes import FakeFirestore

AUTH = {"Authorization": "Bearer token-alice"}
#: No recognisable prefix, and one that has one. Not real credentials.
BARE = "correct-horse-battery-staple-8812"
OPENAI = "sk-proj0123456789abcdefghijklmnopqrstuv"


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
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def api(db) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(
            {"token-alice": {"email": "alice@saga.xyz", "email_verified": True, "sub": "sub-alice", "hd": "saga.xyz"}}
        ),
        groups=StaticGroups({"alice@saga.xyz": ("eng@saga.xyz",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    seed_tenant(db, "eng")
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture()
def swarm(api, monkeypatch) -> SwarmClient:
    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        headers = {**dict(req.headers), **AUTH}
        response = api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers, io.BytesIO(response.content)
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return SwarmClient(base_url="http://api.invalid")


def _seed(db, task_id="task_000000000000masked", *, state="SUCCEEDED") -> str:
    seed_task(db, task_id=task_id, tenant_id="eng", state=state)
    db.docs[f"tasks/{task_id}"]["input"] = {"prompt": f"deploy with DB_PASSWORD={BARE} and {OPENAI}"}
    db.docs[f"tasks/{task_id}"]["metadata"] = {"ci_token": BARE}
    return task_id


def test_status_says_masked_n_for_the_input_and_the_metadata(swarm, db, capsys):
    task_id = _seed(db)
    cli.cmd_status(swarm, argparse.Namespace(task_ids=[task_id]))
    printed = capsys.readouterr().out
    assert BARE not in printed and OPENAI not in printed
    # Two in the prompt, one in the metadata.
    assert printed.rstrip().endswith("masked 3"), printed


def test_result_says_masked_n_and_what_it_was_in(swarm, db, capsys):
    task_id = _seed(db)
    cli.cmd_result(swarm, argparse.Namespace(task_id=task_id, json=False))
    printed = capsys.readouterr().out
    assert BARE not in printed and OPENAI not in printed
    # The line that STARTS with the words: the task id above it spells
    # `...masked` too, which a bare `in` matched on the first run.
    line = next((ln for ln in printed.splitlines() if ln.strip().startswith("masked ")), None)
    assert line is not None, printed
    assert line.strip() == "masked 3  (input 2 · metadata 1)", line


def test_a_clean_task_says_masked_0_which_is_a_count(swarm, db, capsys):
    seed_task(db, task_id="task_00000000000000clean", tenant_id="eng", state="SUCCEEDED")
    db.docs["tasks/task_00000000000000clean"]["input"] = {"prompt": "audit the capacity code"}
    cli.cmd_status(swarm, argparse.Namespace(task_ids=["task_00000000000000clean"]))
    assert capsys.readouterr().out.rstrip().endswith("masked 0")


def test_a_count_the_api_did_not_send_is_a_dash_never_zero():
    """An older deployment sends neither count, and serves the input unmasked."""
    old = {"id": "task_x", "state": "SUCCEEDED", "input": {"prompt": "p"}}
    assert patches.masked_counts(old) == {"input": None, "metadata": None}
    assert patches.masked_words(old) == "masked —"
    only_input = {**old, "input_redaction_count": 2}
    assert patches.masked_words(only_input) == "masked 2"


def test_workflow_status_says_masked_n_on_each_step(swarm, api, capsys):
    created = api.post(
        "/v1/workflows",
        json={
            "steps": [
                {"step_id": "research", "runner_profile": "mock",
                 "input": {"prompt": f"read it with DB_PASSWORD={BARE}"}},
                {"step_id": "draft", "runner_profile": "mock", "input": {"prompt": "write it up"},
                 "depends_on": ["research"]},
            ]
        },
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]

    cli.cmd_workflow_status(swarm, argparse.Namespace(workflow_id=workflow_id, result=False, json=False))
    printed = capsys.readouterr().out
    assert BARE not in printed
    rows = {ln.split()[0]: ln for ln in printed.splitlines() if ln.startswith("  ") and ln.split()[0] in ("research", "draft")}
    assert "masked 1" in rows["research"], printed
    assert "masked 0" in rows["draft"], printed


def test_the_mcp_tools_carry_the_counts(swarm, db, api):
    task_id = _seed(db)
    status = json.loads(server._call(swarm, "swarm_status", {"task_ids": [task_id]}))
    assert status[0]["masked"] == {"input": 2, "metadata": 1}

    result = json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))
    assert result["masked"] == {"input": 2, "metadata": 1}

    created = api.post(
        "/v1/workflows",
        json={"steps": [{"step_id": "only", "runner_profile": "mock", "input": {"prompt": f"PASSWORD={BARE}"}}]},
        headers=AUTH,
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    report = json.loads(server._call(swarm, "swarm_workflow_status", {"workflow_id": workflow_id}))
    (row,) = report["steps"]
    assert row["masked"] == {"input": 1, "metadata": 0}
    assert BARE not in json.dumps(report)
