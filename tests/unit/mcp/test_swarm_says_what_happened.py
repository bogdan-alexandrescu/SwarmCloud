"""What `swarm` prints, held to what actually happened -- the 2026-09-25 plugin QA.

The epic is #88: a CLI test of the plugin's bridge against the team deployment,
with Firestore read-only beside it as ground truth. Every box in it is a place
where the terminal said something the platform had not done, or did not say
something it had. Each test below names its box (SC-Fn), and each was
committed BEFORE its fix, so the first CI run on the branch shows it red for
the reason it names.

AGAINST THE REAL swarm-api wherever the defect was a disagreement with it --
the cancel that was only a request, the identity route's nesting, the workflow
a step belongs to. `test_against_the_real_api.py` says why at length: a fake
agrees with whoever wrote it. The application is built over an in-memory
Firestore and an in-memory object store, exactly as `test_follow_cursor.py`
builds it, and `SwarmClient`'s own transport is pointed at it. Offline: no
credentials, no emulator, no network, nothing created.

`tests/unit` is on sys.path via this directory's conftest, which says why.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from swarm_mcp import cli, patches, sc, workflows
from swarm_mcp import client as mcp_client
from swarm_mcp import follow as follow_module
from swarm_mcp.auth import Detection, Tier
from swarm_mcp.client import SwarmClient, SwarmError

from control_plane.conftest import PROJECT, api_settings, seed_task, seed_tenant
from control_plane.fakes import FakeFirestore

AUTH = {"Authorization": "Bearer token-alice"}
TENANT = "eng"
EMAIL = "alice@saga.xyz"
AT = datetime(2026, 9, 25, 3, 52, 50, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The application, and the real client wired to it
# --------------------------------------------------------------------------


class World:
    """The real application over an in-memory store, and the seeding it needs."""

    def __init__(self) -> None:
        self.db = FakeFirestore()
        self.objects = InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}")
        self.ctx = build_context(
            settings=api_settings(),
            db=self.db,
            verifier=StaticTokenVerifier(
                {
                    "token-alice": {
                        "email": EMAIL,
                        "email_verified": True,
                        "sub": "sub-alice",
                        "hd": "saga.xyz",
                    }
                }
            ),
            groups=StaticGroups({EMAIL: (f"{TENANT}@saga.xyz",)}),
            credentials=InMemoryCredentials(),
            waker=NullWaker(),
            metrics=ApiMetrics(),
            objects=self.objects,
        )
        seed_tenant(self.db, TENANT)
        self.api = TestClient(create_app(self.ctx), raise_server_exceptions=False)

    def task(self, task_id: str, *, state: str = "RUNNING", **extra) -> str:
        seed_task(self.db, task_id=task_id, tenant_id=TENANT, state=state, **extra)
        return task_id

    def set(self, task_id: str, **fields) -> None:
        self.db.docs[f"tasks/{task_id}"].update(fields)

    def state(self, task_id: str) -> str:
        """Ground truth, read through the API rather than through the bridge."""
        response = self.api.get(f"/v1/tasks/{task_id}", headers=AUTH)
        assert response.status_code == 200, response.text
        return response.json()["task"]["state"]

    def attempt(self, task_id: str, attempt_id: str) -> None:
        created = AT - timedelta(minutes=5)
        self.db.collection("attempts").document(attempt_id).set(
            {
                "attempt_id": attempt_id,
                "task_id": task_id,
                "tenant_id": TENANT,
                "generation": 1,
                "lease_id": f"lease_{attempt_id}",
                "backend": "CLOUD_RUN_JOB",
                "execution_name": f"swarm-job-{TENANT}-mock-{attempt_id}",
                "created_at": created,
                "started_at": created + timedelta(seconds=5),
                "completed_at": None,
                "exit_code": None,
                "error": None,
                "peak_rss_bytes": None,
                "oom_near_miss": False,
                "checkpoints": [],
            }
        )

    def event(self, task_id: str, event_id: str, kind: str, at: datetime, detail=None) -> None:
        """One stored event, with the time the test says -- which `append_event` cannot."""
        self.db.docs[f"tasks/{task_id}/events/{event_id}"] = {
            "event_id": event_id,
            "task_id": task_id,
            "tenant_id": TENANT,
            "type": kind,
            "at": at,
            "attempt_id": None,
            "lease_id": None,
            "generation": None,
            "detail": detail,
        }

    def final(self, task_id: str, attempt_id: str, text: str, *, stream: str = "stdout") -> None:
        key = f"tenants/{TENANT}/tasks/{task_id}/attempts/{attempt_id}/logs/{stream}.log"
        self.objects.put(key, text)

    def workflow(self) -> tuple[str, dict[str, str]]:
        """A two-step workflow through the real route: `research`, then `draft`.

        Setup goes through the API rather than through the bridge, so a test
        about the bridge is not set up by the code it is checking.
        """
        created = self.api.post(
            "/v1/workflows",
            json={
                "steps": [
                    {"step_id": "research", "runner_profile": "mock",
                     "input": {"prompt": "read the code"}},
                    {"step_id": "draft", "runner_profile": "mock",
                     "input": {"prompt": "write it up"},
                     "depends_on": ["research"],
                     "input_from": {"research": "notes.md"}},
                ]
            },
            headers=AUTH,
        )
        assert created.status_code == 201, created.text
        workflow = created.json()["workflow"]
        return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


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


@pytest.fixture()
def world() -> World:
    return World()


@pytest.fixture()
def swarm(world: World, monkeypatch) -> SwarmClient:
    """A real SwarmClient whose socket is the real application."""

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        headers = {**dict(req.headers), **AUTH}
        response = world.api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers,
                io.BytesIO(response.content),
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return SwarmClient(base_url="http://api.invalid")


def _sleeps(monkeypatch, on_sleep) -> list[float]:
    """Replace `cli`'s clock-driven sleep with a callback, and count the calls.

    `cli.time` is replaced rather than `time.sleep`, so nothing outside the
    command under test -- the TestClient's own threads included -- sleeps
    differently.
    """
    calls: list[float] = []

    def _sleep(seconds: float) -> None:
        calls.append(seconds)
        on_sleep(len(calls))

    monkeypatch.setattr(cli, "time", SimpleNamespace(sleep=_sleep, monotonic=time.monotonic))
    return calls


def _args(**values) -> argparse.Namespace:
    base = {"interval": 0.0, "verbose": False, "once": False, "max_log_bytes": 20_000}
    base.update(values)
    return argparse.Namespace(**base)


# ==========================================================================
# SC-F1: a cancel that is only a request is not printed as a cancel
# ==========================================================================


def test_cancelling_a_task_that_holds_capacity_says_requested_not_cancelled(swarm, world, capsys):
    """Measured 2026-09-25: `swarm cancel` printed "<id> cancelled" for
    task_dae7476c9c3548609857 while `swarm status` said RUNNING in the same
    second. The route only FLAGS a task holding capacity; the worker or the
    reconciler writes CANCELLED later (invariant 1)."""
    task_id = world.task("task_0000000000000000busy", state="DISPATCHED")

    assert cli.cmd_cancel(swarm, _args(task_ids=[task_id], wait=0.0)) == cli.EXIT_OK

    printed = capsys.readouterr().out
    assert world.state(task_id) == "DISPATCHED", "the fixture did not reproduce a flag-only cancel"
    assert f"{task_id} cancelled" not in printed, printed
    assert "cancel requested" in printed, printed
    assert "DISPATCHED" in printed, "the line must say what the task still is"


def test_cancelling_an_idle_task_still_says_cancelled(swarm, world, capsys):
    """The control: an idle task IS cancelled by the call, and saying so is right."""
    task_id = world.task("task_0000000000000000idle", state="QUEUED")

    cli.cmd_cancel(swarm, _args(task_ids=[task_id], wait=0.0))

    assert world.state(task_id) == "CANCELLED"
    assert f"{task_id} cancelled" in capsys.readouterr().out


def test_cancel_wait_reports_the_stop_when_the_worker_releases_it(swarm, world, capsys, monkeypatch):
    """`--wait` polls until the task has stopped, and only then says cancelled."""
    task_id = world.task("task_0000000000000000wait", state="RUNNING")
    _sleeps(monkeypatch, lambda n: world.set(task_id, state="CANCELLED") if n == 2 else None)

    code = cli.cmd_cancel(swarm, _args(task_ids=[task_id], wait=60.0))

    lines = capsys.readouterr().out.splitlines()
    assert code == cli.EXIT_OK
    assert "cancel requested" in lines[0], lines
    assert lines[-1] == f"{task_id} cancelled", lines


# ==========================================================================
# SC-F2: workflow-cancel and workflow-status
# ==========================================================================


def test_workflow_cancel_does_not_call_a_dispatched_step_cancelled(swarm, world, capsys):
    """wf_f1b6509ff31c48a2a7c1 step c stayed DISPATCHED for about 70s after
    `swarm workflow-cancel` had listed it as cancelled."""
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="DISPATCHED")

    cli.cmd_workflow_cancel(swarm, argparse.Namespace(workflow_id=workflow_id))

    lines = capsys.readouterr().out.splitlines()
    assert world.state(steps["research"]) == "DISPATCHED"
    research = [line for line in lines if steps["research"] in line]
    assert research, lines
    assert "cancel requested" in research[0], lines
    assert "cancelled" not in research[0], lines
    # The PARKED step held nothing, so the call really did cancel it.
    assert world.state(steps["draft"]) == "CANCELLED"
    draft = [line for line in lines if steps["draft"] in line]
    assert draft and "cancelled" in draft[0], lines


def test_workflow_status_shows_the_cancel_request_until_the_step_stops(swarm, world, capsys):
    """After a cancel, `workflow-status` showed RUNNING with no marker at the
    workflow or the step level: nothing on screen said a cancel was pending."""
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="DISPATCHED")
    world.api.post(f"/v1/workflows/{workflow_id}/cancel", headers=AUTH)

    cli.cmd_workflow_status(
        swarm, argparse.Namespace(workflow_id=workflow_id, result=False, json=False)
    )

    lines = capsys.readouterr().out.splitlines()
    assert "cancel requested" in lines[0], lines
    research = next(line for line in lines if line.strip().startswith("research"))
    assert "cancel requested" in research, lines
    # A step that HAS stopped carries no marker: it is not pending anything.
    draft = next(line for line in lines if line.strip().startswith("draft"))
    assert "CANCELLED" in draft and "cancel requested" not in draft, lines


# ==========================================================================
# SC-F3: doctor, whoami and login read identity from where the route puts it
# ==========================================================================


def test_doctor_reads_identity_from_the_nested_route_and_trusts_the_measurement(
    swarm, monkeypatch, capsys
):
    """In the run that reached the team deployment, doctor printed tenant
    None, admin None, "identity (not reported)" -- and the pre-grant "not team
    ... missing authorisation" paragraph, contradicting the read it had just
    made."""
    for name in ("API_HOST", "SWARM_API_HOST", "SWARM_API_URL", "API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROJECT_ID", "a-project")
    monkeypatch.setattr(
        cli,
        "detect",
        lambda deployment=None: Detection(
            Tier.IMPERSONATE, "impersonating sa@example.iam.gserviceaccount.com", []
        ),
    )
    monkeypatch.setattr(cli, "SwarmClient", lambda *a, **k: swarm)

    assert cli.cmd_doctor(None, None) == cli.EXIT_OK

    lines = capsys.readouterr().out.splitlines()
    assert f"identity    {EMAIL}" in lines, lines
    assert f"tenant      {TENANT}" in lines, lines
    assert "admin       False" in lines, lines
    assert any(line.startswith("groups") and f"{TENANT}@saga.xyz" in line for line in lines), lines
    reaches = [line for line in lines if line.startswith("reaches")]
    assert reaches and "measured" in reaches[0], lines
    assert not any(line.startswith(("not team", "not solo")) for line in lines), lines
    assert all(len(line) <= 80 for line in lines), [line for line in lines if len(line) > 80]


def _a_deployment() -> SimpleNamespace:
    return SimpleNamespace(
        context="qa", url="http://api.invalid", source="test", current=True,
        client_id="", front_door=False,
    )


def test_whoami_reads_the_tenant_and_principal_from_the_nested_route(swarm, monkeypatch):
    monkeypatch.setattr(sc, "_resolve", lambda args: _a_deployment())
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: swarm)
    out = io.StringIO()

    assert sc.cmd_whoami(None, argparse.Namespace(json=True, context=None), out) == sc.EXIT_OK

    shown = json.loads(out.getvalue())
    assert shown["tenant"] == TENANT, shown
    assert shown["principal"] == EMAIL, shown


def test_login_reports_the_tenant_the_route_answered(swarm, monkeypatch):
    from swarm_mcp import signin

    monkeypatch.setattr(sc, "_resolve", lambda args: _a_deployment())
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: swarm)
    monkeypatch.setattr(signin, "login", lambda deployment, **kwargs: {"email": EMAIL})
    out = io.StringIO()

    code = sc.cmd_login(None, argparse.Namespace(client_secret_stdin=False, context=None), out)

    assert code == sc.EXIT_OK, out.getvalue()
    assert f"tenant      {TENANT}" in out.getvalue().splitlines(), out.getvalue()


# ==========================================================================
# SC-F6: the refusal of an unknown step key agrees with its own list
# ==========================================================================


def test_the_step_key_refusal_does_not_deny_what_it_lists_as_accepted():
    """It said "a resource spec cannot be supplied at all" directly after
    listing `resource_class` as accepted."""
    with pytest.raises(SwarmError) as caught:
        workflows.build_steps([{"step_id": "a", "prompt": "x", "sleep_seconds": 5}])
    message = str(caught.value)
    assert "sleep_seconds" in message
    assert "cannot be supplied at all" not in message, message
    assert "resource_class" in message


# ==========================================================================
# SC-F8: follow prints a finished task once and stops reading it
# ==========================================================================


def test_follow_prints_a_finished_task_once_and_stops_polling_it(swarm, world, capsys, monkeypatch):
    """Measured: 111 lines for 5 steps, one terminal line repeated 20 times."""
    done = world.task("task_0000000000000000done", state="SUCCEEDED")
    world.attempt(done, "att_done")
    world.final(done, "att_done", "all done\n")
    slow = world.task("task_0000000000000000slow", state="RUNNING")
    _sleeps(monkeypatch, lambda n: world.set(slow, state="SUCCEEDED") if n == 3 else None)
    reads: list[str] = []
    real_task = swarm.task
    monkeypatch.setattr(swarm, "task", lambda task_id: reads.append(task_id) or real_task(task_id))

    code = cli.cmd_follow(swarm, _args(task_ids=[done, slow]))

    lines = capsys.readouterr().out.splitlines()
    assert code == cli.EXIT_OK
    finished = [line for line in lines if line.startswith(f"[{done[-8:]}]") and line.endswith("SUCCEEDED")]
    assert len(finished) == 1, lines
    assert reads.count(done) == 1, f"a finished task was re-read on every poll: {reads}"
    assert "all done" in "\n".join(lines)


# ==========================================================================
# SC-F9: the access token is minted once, and tail orders what it prints
# ==========================================================================


def test_the_access_token_is_minted_once_and_reused(monkeypatch):
    """Every front-door request ran `gcloud auth print-access-token
    --impersonate-service-account`, about a second each, until tail timed out."""
    minted: list[list[str]] = []
    monkeypatch.setattr(mcp_client, "_run", lambda argv, **kw: minted.append(argv) or "TOKEN")
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    monkeypatch.delenv("SWARM_ACCESS_TOKEN", raising=False)
    client = SwarmClient(base_url="https://swarm.example.com", tier=Tier.IMPERSONATE)

    for _ in range(5):
        assert client.access_token() == "TOKEN"

    assert len([argv for argv in minted if "print-access-token" in argv]) == 1, minted


def test_a_cached_token_refused_with_401_is_reminted_once(monkeypatch):
    """The cache's one risk: gcloud can hand back a token with less life than
    the cache assumes. Refused with 401, the request re-mints and retries ONCE."""
    for name in ("API_HOST", "SWARM_API_HOST"):
        monkeypatch.delenv(name, raising=False)
    minted: list[list[str]] = []
    monkeypatch.setattr(
        mcp_client, "_run", lambda argv, **kw: minted.append(argv) or f"TOKEN{len(minted)}"
    )
    monkeypatch.setenv("SWARM_IMPERSONATE_SA", "sa@example.iam.gserviceaccount.com")
    monkeypatch.delenv("SWARM_ACCESS_TOKEN", raising=False)
    sent: list[str] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        sent.append(req.get_header("Authorization"))
        if len(sent) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {},
                io.BytesIO(b"Invalid IAP credentials: Expired JWT"),
            )
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(mcp_client, "_open", _opener)
    client = SwarmClient(base_url="https://swarm.example.com", tier=Tier.IMPERSONATE)
    client.access_token()  # minted earlier in the session, and cached

    assert client.request("GET", "/v1/stats") == {"ok": True}
    assert sent == ["Bearer TOKEN1", "Bearer TOKEN2"], sent


def test_tail_prints_one_polls_events_in_the_order_they_happened(swarm, world, capsys):
    """A dependent's cancel printed above its upstream's: tail printed task by
    task, in argument order. Sorted by `at`, and each line carries its time."""
    upstream = world.task("task_000000000000000000up", state="CANCELLED")
    dependent = world.task("task_0000000000000000down", state="CANCELLED")
    world.event(upstream, "ev_up", "cancelled", AT + timedelta(seconds=1))
    world.event(dependent, "ev_down", "cancelled", AT + timedelta(seconds=2))

    cli.cmd_tail(swarm, _args(task_ids=[dependent, upstream]))

    lines = capsys.readouterr().out.splitlines()
    up = next(i for i, line in enumerate(lines) if upstream[-8:] in line and "· cancelled" in line)
    down = next(i for i, line in enumerate(lines) if dependent[-8:] in line and "· cancelled" in line)
    assert up < down, lines
    assert "03:52:51" in lines[up], lines


# ==========================================================================
# SC-F10: swarm result says why a step was cancelled
# ==========================================================================


def test_result_prints_why_a_task_ended(swarm, world, capsys):
    task_id = world.task("task_000000000000000ended", state="CANCELLED")
    world.set(task_id, last_error="cancelled by alice@saga.xyz before it started")

    cli.cmd_result(swarm, argparse.Namespace(task_id=task_id, json=False))

    printed = capsys.readouterr().out
    assert "cancelled by alice@saga.xyz before it started" in printed, printed
    # Never attempted, so there is no parked attempt to go and read.
    assert "parked attempt" not in printed, printed


def test_result_names_the_upstream_step_a_cascade_cancel_was_for(swarm, world, capsys):
    """For a cascade-cancelled step that was never attempted, `swarm result`
    printed the parked-attempt sentence and nothing about the step that failed."""
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="FAILED", last_error="the agent exited 1")
    world.set(steps["draft"], state="CANCELLED", park_reason=None,
              last_error="an upstream workflow step did not succeed")
    world.event(
        steps["draft"], "ev_cascade", "cancelled", AT,
        detail={"reason": "an upstream workflow step did not succeed",
                "failed_parents": [steps["research"]]},
    )

    cli.cmd_result(swarm, argparse.Namespace(task_id=steps["draft"], json=False))

    printed = capsys.readouterr().out
    upstream = [line for line in printed.splitlines() if "upstream" in line and "research" in line]
    assert upstream, printed
    assert "FAILED" in upstream[0], printed
    assert "parked attempt" not in printed, printed


# ==========================================================================
# SC-F12: tail says when it cannot read the logs
# ==========================================================================


def test_tail_says_it_cannot_locate_the_logs_rather_than_printing_nothing(
    swarm, world, capsys, monkeypatch
):
    """No bucket and no project was swallowed as "not published yet"."""
    for name in ("SWARM_ARTIFACT_BUCKET", "PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)

    def _no_gcloud(argv, **kwargs):  # noqa: ARG001
        raise SwarmError("gcloud is not installed or not on PATH")

    monkeypatch.setattr(mcp_client, "_run", _no_gcloud)
    task_id = world.task("task_000000000000nobucket", state="SUCCEEDED")
    world.attempt(task_id, "att_nobucket")

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    printed = capsys.readouterr().out
    assert "SWARM_ARTIFACT_BUCKET" in printed, printed


def test_tail_warns_once_on_a_log_read_that_failed(swarm, world, capsys, monkeypatch):
    """A 403 on the tenant's prefix read exactly like an agent that printed nothing."""
    monkeypatch.setenv("SWARM_ARTIFACT_BUCKET", "a-bucket")

    def _forbidden(client, uri, timeout=120):  # noqa: ARG001
        raise SwarmError(f"could not read {uri}: 403 forbidden", status=403)

    monkeypatch.setattr(cli, "download", _forbidden)
    task_id = world.task("task_00000000000forbidden", state="SUCCEEDED")
    world.attempt(task_id, "att_403")

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    warnings = [line for line in lines if "403" in line]
    assert len(warnings) == 1, warnings
    # A read that failed says nothing about the output, so the finished line
    # must not either. It said "its output is MISSING, not empty" right after
    # the 403, which is a verdict on objects the tail could not look at.
    assert not any("MISSING" in line for line in lines), lines
    assert any("unknown" in line for line in lines), lines


def test_tail_stays_quiet_about_a_log_that_is_simply_not_there_yet(swarm, world, capsys, monkeypatch):
    """The control: a 404 is "not published yet", and is not a failed read."""
    monkeypatch.setenv("SWARM_ARTIFACT_BUCKET", "a-bucket")

    def _absent(client, uri, timeout=120):  # noqa: ARG001
        raise SwarmError(f"could not read {uri}: 404 No such object", status=404)

    monkeypatch.setattr(cli, "download", _absent)
    # The completed log is looked up by its size once the task has finished;
    # it is absent here too. `raising=False` so this test states its world the
    # same way on a commit where the lookup does not exist yet.
    monkeypatch.setattr(cli, "object_size", _absent, raising=False)
    task_id = world.task("task_000000000000notthere", state="SUCCEEDED")
    world.attempt(task_id, "att_404")

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    assert "unreadable" not in capsys.readouterr().out


def test_a_gcs_read_failure_carries_its_status(monkeypatch):
    """`tail` can only tell 404 from 403 if the error says which it was."""

    def _not_found(req, timeout=None):  # noqa: ARG001
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b"No such object")
        )

    monkeypatch.setattr(urllib.request, "urlopen", _not_found)
    with pytest.raises(SwarmError) as caught:
        patches.download(SimpleNamespace(access_token=lambda: "a.b.c"), "gs://b/k.log")
    assert caught.value.status == 404


def test_an_objects_size_is_read_from_its_metadata_not_its_bytes(monkeypatch):
    """A completed log can be 32 MB (`max_stdout_bytes`); `tail` needs one number."""
    asked: list[str] = []

    def _metadata(req, timeout=None):  # noqa: ARG001
        asked.append(req.full_url)
        return _Response(json.dumps({"name": "k.log", "size": "42"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _metadata)

    assert patches.object_size(SimpleNamespace(access_token=lambda: "a.b.c"), "gs://b/k.log") == 42
    assert asked and "alt=media" not in asked[0], asked


# ==========================================================================
# SC-F15: a workflow step is named by its step, and one truncation is used
# ==========================================================================


def test_follow_names_a_workflow_step_by_its_step_id(swarm, world):
    workflow_id, steps = world.workflow()

    report = follow_module.follow(swarm, [steps["research"]])

    assert report["tasks"][0].get("step_id") == "research"


def test_follow_labels_a_step_line_with_its_step_and_eight_characters():
    task_id = "task_0123456789ab4674b39f"
    report = {
        "tasks": [{
            "task_id": task_id, "step_id": "b", "read": "ok", "state": "RUNNING",
            "terminal": False,
            "events": {"status": "ok", "new": [{"type": "started", "at": AT.isoformat()}]},
            "logs": {"status": "ok", "streams": [], "notes": []},
        }],
        "cursor": {},
        "truncation": [],
    }
    lines = follow_module.render(report)
    assert lines and lines[0].startswith("[b 4674b39f] "), lines


def test_tail_labels_a_workflow_step_with_its_step_id(swarm, world, capsys):
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="SUCCEEDED")

    cli.cmd_tail(swarm, _args(task_ids=[steps["research"]]))

    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith(f"[research {steps['research'][-8:]}]") for line in lines), lines


# ==========================================================================
# SC-F16: workflow-status reads as sentences, once
# ==========================================================================


class _WorkflowClient:
    """Answers one workflow envelope; `workflows.fetch` speaks `request`."""

    def __init__(self, envelope):
        self.envelope = envelope

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        return self.envelope


def _envelope(tasks: list[dict]) -> dict:
    return {
        "workflow": {
            "workflow_id": "wf_x",
            "state": "SUCCEEDED" if all(t["state"] == "SUCCEEDED" for t in tasks) else "RUNNING",
            "stored_state": "QUEUED",
            "state_source": "derived",
            "cancel_requested": False,
            "on_step_failure": "fail_workflow",
            "steps": [
                {"step_id": t["id"].removeprefix("task_"), "task_id": t["id"],
                 "runner_profile": "mock", "depends_on": [], "input_from": {}}
                for t in tasks
            ],
        },
        "tasks": tasks,
    }


def test_workflow_status_prints_blocked_by_as_words_not_a_repr(capsys):
    envelope = _envelope([
        {"id": "task_a", "state": "READY",
         "blocked_by": [{"pool": "provider:anthropic", "reason": "PROVIDER_CONCURRENCY_LIMIT"}]},
    ])

    cli.cmd_workflow_status(
        _WorkflowClient(envelope), argparse.Namespace(workflow_id="wf_x", result=False, json=False)
    )

    printed = capsys.readouterr().out
    assert "provider concurrency limit (provider:anthropic)" in printed, printed
    assert "{'pool'" not in printed, printed
    # Still running, so following it is still worth offering.
    assert "follow:" in printed


def test_workflow_status_says_a_shared_no_patch_reason_once_and_offers_no_tail_when_finished(capsys):
    envelope = _envelope([
        {"id": "task_a", "state": "SUCCEEDED"},
        {"id": "task_b", "state": "SUCCEEDED"},
    ])

    cli.cmd_workflow_status(
        _WorkflowClient(envelope), argparse.Namespace(workflow_id="wf_x", result=True, json=False)
    )

    printed = capsys.readouterr().out
    assert printed.count("no result summary") == 1, printed
    assert "follow:" not in printed, printed


# ==========================================================================
# SC-F17: the help shows a spec, lists the choices, and a wf_ id is followable
# ==========================================================================


def _workflow_parser() -> argparse.ArgumentParser:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices["workflow"]


def test_the_help_keeps_the_exit_code_table_a_table():
    assert "    0  the thing asked for happened" in cli.build_parser().format_help()


def test_the_workflow_help_shows_a_minimal_spec():
    text = _workflow_parser().format_help()
    assert '"steps": [' in text, text
    assert '"step_id"' in text and '"prompt"' in text, text


def test_the_workflow_strategy_and_carrier_are_choices():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["workflow", "spec.json", "--strategy", "bogus"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["workflow", "spec.json", "--carrier", "bogus"])


def test_the_workflow_choices_are_the_apis():
    """The choices are a copy of the API's -- the bridge cannot import it -- so
    this is what keeps the copy honest."""
    from swarm_api.validation import DISPATCH_CARRIERS, DISPATCH_STRATEGIES

    actions = {a.dest: a for a in _workflow_parser()._actions}
    assert actions["strategy"].choices is not None
    assert tuple(actions["strategy"].choices) == DISPATCH_STRATEGIES
    assert actions["carrier"].choices is not None
    assert tuple(actions["carrier"].choices) == DISPATCH_CARRIERS


def test_status_takes_a_workflow_id(swarm, world, capsys):
    workflow_id, steps = world.workflow()

    cli.cmd_status(swarm, argparse.Namespace(task_ids=[workflow_id]))

    printed = capsys.readouterr().out
    assert steps["research"] in printed and steps["draft"] in printed, printed


def test_tail_takes_a_workflow_id(swarm, world, capsys):
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="SUCCEEDED")
    world.set(steps["draft"], state="CANCELLED", park_reason=None)

    cli.cmd_tail(swarm, _args(task_ids=[workflow_id]))

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("[research ") for line in lines), lines
    assert any(line.startswith("[draft ") for line in lines), lines


def test_follow_takes_a_workflow_id(swarm, world, capsys):
    workflow_id, steps = world.workflow()
    world.set(steps["research"], state="SUCCEEDED")
    world.set(steps["draft"], state="CANCELLED", park_reason=None)

    cli.cmd_follow(swarm, _args(task_ids=[workflow_id], once=True))

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("[research ") for line in lines), lines
    assert any(line.startswith("[draft ") for line in lines), lines


# ==========================================================================
# SC-F19: times on events, and one line that says why there is no output
# ==========================================================================


def test_follow_puts_a_time_on_every_event_line(swarm, world, capsys):
    task_id = world.task("task_0000000000000000time", state="SUCCEEDED")
    world.event(task_id, "ev_time", "succeeded", AT)

    cli.cmd_follow(swarm, _args(task_ids=[task_id], once=True))

    lines = capsys.readouterr().out.splitlines()
    assert f"[{task_id[-8:]}] 03:52:50Z · succeeded" in lines, lines


def test_follow_says_no_output_yet_once_and_never_started_at_the_end(swarm, world, capsys, monkeypatch):
    task_id = world.task("task_00000000000000queued", state="QUEUED")
    _sleeps(monkeypatch, lambda n: world.set(task_id, state="CANCELLED") if n == 3 else None)

    cli.cmd_follow(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    assert len([line for line in lines if "no output yet" in line]) == 1, lines
    assert any("never started" in line for line in lines), lines


def test_follow_tells_an_empty_log_from_a_missing_one(swarm, world, capsys):
    empty = world.task("task_00000000000000empty", state="SUCCEEDED")
    world.attempt(empty, "att_empty")
    world.final(empty, "att_empty", "")
    world.final(empty, "att_empty", "", stream="stderr")
    missing = world.task("task_0000000000000missing", state="SUCCEEDED")
    world.attempt(missing, "att_missing")

    cli.cmd_follow(swarm, _args(task_ids=[empty, missing], once=True))

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith(f"[{empty[-8:]}]") and "printed nothing" in line for line in lines), lines
    said = [line for line in lines if line.startswith(f"[{missing[-8:]}]") and "no log object" in line]
    assert said, lines
    # Neither object existing is what was OBSERVED. "Its output is MISSING" is
    # one reading of it, and the worker gives an ordinary one: an attempt that
    # fails during startup never creates the file either log is written from.
    assert "MISSING" not in said[0], said
    assert "att_missing" in said[0], said


# `tail` reads GCS itself, so its half of the same question is asked of a fake
# bucket: `objects` maps the end of an object's key to its bytes, and every
# other key is a 404 -- the answer GCS gives for an object never written.


def _bucket(monkeypatch, objects: dict[str, bytes]) -> None:
    monkeypatch.setenv("SWARM_ARTIFACT_BUCKET", "a-bucket")

    def _find(uri: str) -> bytes:
        for suffix, data in objects.items():
            if uri.endswith(suffix):
                return data
        raise SwarmError(f"could not read {uri}: 404 No such object", status=404)

    monkeypatch.setattr(cli, "download", lambda client, uri, timeout=120: _find(uri))
    # `raising=False`: on the commit these tests were written against, `cli`
    # has no `object_size`, and the test must fail on what `tail` PRINTS rather
    # than on the fake failing to install.
    monkeypatch.setattr(
        cli, "object_size", lambda client, uri, timeout=30: len(_find(uri)), raising=False
    )


def test_tail_does_not_call_a_short_runs_output_missing(swarm, world, capsys, monkeypatch):
    """The worker publishes the live log first one interval (5 s by default)
    after its agent starts, and never at exit, so a run shorter than that
    leaves no live log -- its output is in the completed log, `logs/stdout.log`.
    `tail` printed "its output is MISSING, not empty" for it."""
    task_id = world.task("task_000000000000000short", state="SUCCEEDED")
    world.attempt(task_id, "att_short")
    _bucket(monkeypatch, {"/logs/stdout.log": b"all done\n", "/logs/stderr.log": b""})

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    assert not any("MISSING" in line for line in lines), lines
    said = [line for line in lines if "no live log" in line]
    assert said, lines
    assert "9 bytes of stdout" in said[0], said
    assert "stderr" not in said[0], "an empty stream holds nothing worth naming"
    assert f"swarm follow {task_id}" in said[0], "say where the output can be read"


def test_tail_calls_an_empty_completed_log_printed_nothing(swarm, world, capsys, monkeypatch):
    """`_publish_live_logs` skips a stream with no bytes, so an agent that
    printed nothing NEVER has a live log. `tail` called that MISSING."""
    task_id = world.task("task_000000000000000quiet", state="SUCCEEDED")
    world.attempt(task_id, "att_quiet")
    _bucket(monkeypatch, {"/logs/stdout.log": b"", "/logs/stderr.log": b""})

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    assert not any("MISSING" in line for line in lines), lines
    assert any("printed nothing" in line and "completed log" in line for line in lines), lines


def test_tail_says_which_objects_it_found_absent_when_there_are_none(
    swarm, world, capsys, monkeypatch
):
    """No live log and no completed log: say exactly that, for which attempt."""
    task_id = world.task("task_00000000000000nologs", state="FAILED")
    world.attempt(task_id, "att_nologs")
    _bucket(monkeypatch, {})

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    assert not any("MISSING" in line for line in lines), lines
    said = [line for line in lines if "no log object" in line]
    assert said and "att_nologs" in said[0], lines


def test_tail_does_not_say_never_started_when_it_could_not_read_the_attempts(
    swarm, world, capsys, monkeypatch
):
    """The attempts read failing is not "no attempt". `tail` warned "logs
    unavailable" and then, for the same task, printed "never started, so it
    printed nothing" -- a verdict on the very read it had just said failed."""
    task_id = world.task("task_0000000000noattempts", state="SUCCEEDED")
    monkeypatch.setattr(
        cli,
        "_latest_attempt",
        lambda client, tid: (None, SwarmError(f"GET /v1/tasks/{tid}/attempts: 503 unavailable")),
    )

    cli.cmd_tail(swarm, _args(task_ids=[task_id]))

    lines = capsys.readouterr().out.splitlines()
    assert any("503" in line for line in lines), "the failed read is still warned about"
    assert not any("never started" in line for line in lines), lines
    assert any("unknown" in line for line in lines), lines


def test_the_state_is_spelled_once_across_sc_task_and_swarm_result(swarm, world, capsys):
    """`sc task` printed `succeeded` where `swarm result` printed `SUCCEEDED`."""
    task_id = world.task("task_0000000000000spelled", state="SUCCEEDED")

    cli.cmd_result(swarm, argparse.Namespace(task_id=task_id, json=False))
    out = io.StringIO()
    sc.cmd_task(swarm, sc.build_parser().parse_args(["task", task_id]), out)

    assert re.search(rf"^{task_id}\s+SUCCEEDED\b", capsys.readouterr().out, re.MULTILINE)
    assert re.search(rf"^{task_id}\s+SUCCEEDED\b", out.getvalue(), re.MULTILINE), out.getvalue()
