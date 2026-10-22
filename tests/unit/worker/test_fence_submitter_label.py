"""The `generation_fenced` log line names a SERVICE submitter, never a person (FENCE-SUBMITTER).

Part of #1822. Owner decision 2026-10-22: the `generation_fenced` alert
excluded tenants to silence verify-remote's deliberate fences, but
verify-remote submits as `eng`, so over 7 days (measured 2026-10-22 01:11Z)
eng carried 347 fences and smoke 57, and no tenant filter could drop
swarm-verify's without hiding eng's real ones. The worker now writes
`submitter_service` on the fence's event line: the short name of a
user-managed service account (`swarm-verify`), or `person` for anyone else.
A person's address never reaches the line: logs and metric labels are read
widely, and an email there is personal data.

The submitter comes from the task document the control plane already read
(`fetch_task` on the direct path, `state/open` on the routes). No read is
added for it. Offline: no credentials, no emulator.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from fakes import ExplodingChildProcess

from agent_worker import lifecycle
from agent_worker.errors import ExitCode
from agent_worker.logs import SUBMITTER_NOT_SERVICE, submitter_service
from swarm_common.states import EventType, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from state_routes_harness import RoutesOpener, api_client, remote_control
from state_routes_harness import seed_attempt as seed_routes_attempt
from worker_seeds import seed_attempt

VERIFY_SA = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
PERSON = "alice.person@example.com"


def _fence_lines(stream) -> list[dict]:
    """Every `event` line the worker wrote for `generation_fenced`: the lines
    the log-based metric counts (terraform/modules/monitoring/metrics.tf)."""
    lines = [json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip()]
    return [
        line for line in lines
        if line.get("message") == "event"
        and line.get("event_type") == EventType.GENERATION_FENCED.value
    ]


def _raw_fence_lines(stream) -> list[str]:
    return [
        raw for raw in stream.getvalue().splitlines()
        if '"message": "event"' in raw and EventType.GENERATION_FENCED.value in raw
    ]


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (VERIFY_SA, "swarm-verify"),
        ("Swarm-Verify@saga-agents-staging.iam.gserviceaccount.com ", "swarm-verify"),
        ("swarm-ci-fix@other-project-1.iam.gserviceaccount.com", "swarm-ci-fix"),
        (PERSON, SUBMITTER_NOT_SERVICE),
        # Google-managed accounts name no workload of ours: every workload in a
        # project can run as them (swarm_common.identity.SERVICE_ACCOUNT_EMAIL).
        ("123456-compute@developer.gserviceaccount.com", SUBMITTER_NOT_SERVICE),
        # A Workspace domain that merely contains the suffix is still a person.
        ("bob@iam.gserviceaccount.com.example.com", SUBMITTER_NOT_SERVICE),
        (None, None),
        ("", None),
        (17, None),
    ],
)
def test_submitter_service_is_a_short_service_name_or_person(principal, expected):
    # MUTATION: return the address unchanged and the person row carries an
    # email; return the local part for any address and the person row reads
    # `alice.person`; drop the regex anchor and the look-alike domain reads as
    # a service.
    assert submitter_service(principal) == expected


def test_a_startup_fence_for_swarm_verify_names_it(db, worker_factory, log_stream, monkeypatch):
    seed_attempt(db, generation=6, task_generation=7, state=TaskState.RUNNING)
    db.doc("tasks/task_1")["submitted_by"] = VERIFY_SA
    worker, _, _ = worker_factory(generation=6)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == ExitCode.GENERATION_FENCED

    (line,) = _fence_lines(log_stream)
    # MUTATION: leave `submitter_service` off the event line, or read it from
    # a document validate_generation did not fetch, and this is a KeyError or
    # None.
    assert line["submitter_service"] == "swarm-verify"
    assert line["detail"]["phase"] == "startup"


def test_a_startup_fence_for_a_person_carries_no_email(db, worker_factory, log_stream, monkeypatch):
    seed_attempt(db, generation=6, task_generation=7, state=TaskState.RUNNING)
    db.doc("tasks/task_1")["submitted_by"] = PERSON
    worker, _, _ = worker_factory(generation=6)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)

    assert worker.run() == ExitCode.GENERATION_FENCED

    (raw,) = _raw_fence_lines(log_stream)
    # MUTATION: log `submitted_by` itself, or its local part, and the person's
    # address (or name) is in the line.
    assert PERSON not in raw and "alice" not in raw and "@" not in raw
    assert json.loads(raw)["submitter_service"] == SUBMITTER_NOT_SERVICE


def test_a_mid_run_fence_for_swarm_verify_names_it(db, worker_factory, log_stream):
    seed_attempt(
        db,
        generation=1,
        state=TaskState.LEASED,
        task_input={"prompt": "long", "steps": 40, "sleep_seconds": 8.0},
    )
    db.doc("tasks/task_1")["submitted_by"] = VERIFY_SA
    worker, _, _ = worker_factory(generation=1, control_poll_seconds=1, timeout_seconds=30)

    def bump() -> None:
        time.sleep(1.5)
        db.doc("tasks/task_1")["current_generation"] = 99

    thread = threading.Thread(target=bump)
    thread.start()
    exit_code = worker.run()
    thread.join()

    assert exit_code == ExitCode.GENERATION_FENCED
    lines = _fence_lines(log_stream)
    # MUTATION: label only the startup fence's line and the running phase's
    # line carries nothing.
    assert lines and lines[-1]["detail"]["phase"] == "running"
    assert lines[-1]["submitter_service"] == "swarm-verify"


def test_other_event_lines_carry_no_submitter(db, worker_factory, log_stream, monkeypatch):
    """The label is the fence's: the metric that reads it is the fence's."""
    seed_attempt(db, generation=6, task_generation=7, state=TaskState.RUNNING)
    db.doc("tasks/task_1")["submitted_by"] = VERIFY_SA
    worker, _, _ = worker_factory(generation=6)
    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    worker.run()
    worker.control.emit(EventType.HEARTBEAT, {})

    lines = [json.loads(raw) for raw in log_stream.getvalue().splitlines() if raw.strip()]
    heartbeat = [x for x in lines if x.get("event_type") == EventType.HEARTBEAT.value]
    # MUTATION: add the label to every event line and this carries it.
    assert heartbeat and "submitter_service" not in heartbeat[-1]


def test_the_routes_path_names_the_submitter_state_open_served(log_stream):
    """`RemoteControlPlane` reads the submitter from the task `state/open`
    served, the one task read it makes."""
    api_db = ApiFirestore()
    seed_routes_attempt(api_db, submitted_by=VERIFY_SA)
    control = remote_control(RoutesOpener(api_client(api_db)), stream=log_stream)
    control.validate_generation()

    control.emit(EventType.GENERATION_FENCED, {"phase": "running"})

    (line,) = _fence_lines(log_stream)
    # MUTATION: record the submitter on the direct path's fetch_task only and
    # the routes path's line carries nothing.
    assert line["submitter_service"] == "swarm-verify"


def test_the_routes_path_carries_no_email_for_a_person(log_stream):
    api_db = ApiFirestore()
    seed_routes_attempt(api_db, submitted_by=PERSON)
    control = remote_control(RoutesOpener(api_client(api_db)), stream=log_stream)
    control.validate_generation()

    control.emit(EventType.GENERATION_FENCED, {"phase": "running"})

    (raw,) = _raw_fence_lines(log_stream)
    # MUTATION: log the served `submitted_by` and the address is in the line.
    assert PERSON not in raw and "@" not in raw
    assert json.loads(raw)["submitter_service"] == SUBMITTER_NOT_SERVICE
