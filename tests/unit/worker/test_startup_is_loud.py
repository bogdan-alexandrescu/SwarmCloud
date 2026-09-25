"""A worker that cannot start says so within seconds. Tested in a real process.

WHAT WAS WRONG. Measured on 2026-09-24 (task_1f699ef4cdbb4cc79c16, attempt
att_78e34e7537de487ba71d): a GKE worker whose DNS was dropped by its egress
NetworkPolicy ran for 390 s and never wrote a byte. The entrypoint printed
nothing before its first Firestore call. google-auth's warnings went to a
NullHandler. The generation check's read retried UNAVAILABLE for up to 300 s at
DEBUG. The reconciler's SIGTERM only set a flag that nothing read before a
runner existed, and the SIGKILL 120 s later ended it with exit 137.

WHAT IS PINNED, each in the real entrypoint running in its own process
(`startup_harness.py` says why it has to be a process):

  * the first line is written before the configuration is read, and every
    startup phase gets a line, in order;
  * a warning from google-auth reaches stdout;
  * a DNS that drops every packet ends startup with exit 78 within the
    preflight budget, with one error line naming the hosts. A DNS that
    answers NXDOMAIN ends it at once;
  * a SIGTERM before the runner exists names the phase it arrived in and
    exits 143 within seconds, and it dumps every thread's stack to stderr.

Each test waits on the process with a bound far past what the fix needs, and
fails with the process's own output. Against the code before the fix, the
first two find no such lines, and the rest find a process that hangs where
the incident's did.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pytest

HARNESS = Path(__file__).resolve().parent / "startup_harness.py"

#: The attempt `seed_attempt` creates, as the dispatcher would name it.
IDENTITY = {
    "TASK_ID": "task_1",
    "ATTEMPT_ID": "att_1",
    "LEASE_ID": "lease_1",
    "TENANT_ID": "eng",
    "GENERATION": "1",
    "RUNNER_PROFILE": "mock",
}

#: Inherited variables that would change which hosts the preflight resolves,
#: or which credentials a library looks for. Removed so every test states its
#: own.
_NOT_INHERITED = (
    "FIRESTORE_EMULATOR_HOST",
    "GCE_METADATA_HOST",
    "GCE_METADATA_ROOT",
    "GCE_METADATA_IP",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "QUOTA_BROKER_URL",
    "REPOSITORY_URL",
)

#: The preflight's budget, restated here on purpose. The test asserts the
#: process honours the number the brief asked for, which a test that read it
#: back from the module could not do.
DNS_BUDGET_SECONDS = 10.0

#: An emulator address the preflight can resolve with no DNS at all. Nothing
#: listens on it: the harness stands `FakeFirestore` in for the client.
LOCAL_FIRESTORE = "127.0.0.1:8681"

#: How long a test waits for something the fix produces in about a second.
#: Generous because `-n auto` loads the runner. Against the old code these
#: waits are what the test fails on.
PATIENCE_SECONDS = 60.0


def _env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _NOT_INHERITED}
    env.update(IDENTITY)
    env.update(
        PROJECT_ID="swarm-harness",
        REGION="us-central1",
        FIRESTORE_DATABASE="swarm",
        LOCAL_ARTIFACT_ROOT=str(tmp_path / "gcs"),
        WORKSPACE_ROOT=str(tmp_path / "workspace"),
        DISABLE_CLOUD_MONITORING="1",
        PYTHONUNBUFFERED="1",
        LOG_LEVEL="INFO",
    )
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class Child:
    """One harness process, its stdout parsed line by line as it arrives."""

    def __init__(self, scenario: str, env: dict[str, str], cwd: Path) -> None:
        self.spawned_at = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, str(HARNESS), scenario],
            env=env,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.records: list[dict[str, Any]] = []
        self.stderr: list[str] = []
        self.exited_at: float | None = None
        self._cv = threading.Condition()
        self._pumps = [
            threading.Thread(target=self._pump_stdout, daemon=True),
            threading.Thread(target=self._pump_stderr, daemon=True),
        ]
        for pump in self._pumps:
            pump.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                record = json.loads(raw)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                record = {"raw": raw.rstrip("\n")}
            record["_seen_at"] = time.monotonic()
            with self._cv:
                self.records.append(record)
                self._cv.notify_all()

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            with self._cv:
                self.stderr.append(raw)

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = PATIENCE_SECONDS
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for record in self.records:
                    if predicate(record):
                        return record
                if self.proc.poll() is not None and not any(p.is_alive() for p in self._pumps[:1]):
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(min(remaining, 0.2))

    def finish(self, timeout: float = PATIENCE_SECONDS) -> int | None:
        """The exit code, or None if the process had to be killed."""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            code = None
        self.exited_at = time.monotonic()
        for pump in self._pumps:
            pump.join(timeout=10)
        return code

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def transcript(self) -> str:
        with self._cv:
            out = [r.get("raw") or json.dumps({k: v for k, v in r.items() if k != "_seen_at"})
                   for r in self.records]
            err = list(self.stderr)
        return (
            "---- stdout (last 60) ----\n" + "\n".join(out[-60:])
            + "\n---- stderr (last 60) ----\n" + "".join(err[-60:])
        )

    def messages(self) -> list[str]:
        with self._cv:
            return [str(r.get("message", "")) for r in self.records]

    def phases(self) -> list[str]:
        with self._cv:
            return [r["phase"] for r in self.records if r.get("message") == "startup phase"]


@pytest.fixture
def spawn(tmp_path: Path):
    children: list[Child] = []

    def _spawn(scenario: str, **env: str | None) -> Child:
        child = Child(scenario, _env(tmp_path, **env), tmp_path)
        children.append(child)
        return child

    yield _spawn
    for child in children:
        child.kill()


def _phase(name: str) -> Callable[[dict[str, Any]], bool]:
    return lambda r: r.get("message") == "startup phase" and r.get("phase") == name


def _seconds_between(earlier: dict[str, Any], later: dict[str, Any]) -> float:
    """Seconds between two of the child's lines, by the `time` each carries."""

    def _at(record: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(str(record["time"]).replace("Z", "+00:00"))

    return (_at(later) - _at(earlier)).total_seconds()


# ---------------------------------------------------------------------------
# the lines
# ---------------------------------------------------------------------------


def test_the_first_line_is_written_before_the_configuration_is_read(spawn):
    """A pod whose configuration is broken still says it started, and who it is.

    PROJECT_ID is missing, so `Settings.from_env` fails. That is the earliest
    failure the entrypoint can have. The old entrypoint printed its one line
    to stderr, and only on this path.
    """
    child = spawn("runs", FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE, PROJECT_ID=None)
    code = child.finish()
    assert code == 78, f"exit {code}\n{child.transcript()}"
    assert child.records, f"the worker wrote nothing to stdout\n{child.transcript()}"
    first = child.records[0]
    assert first.get("message") == "worker process started", child.transcript()
    assert first.get("labels", {}).get("task_id") == "task_1", first
    assert first.get("labels", {}).get("attempt_id") == "att_1", first
    failed = [r for r in child.records if str(r.get("message", "")).startswith(
        "worker configuration failed")]
    assert failed and failed[0].get("severity") == "ERROR", child.transcript()


def test_every_startup_phase_is_announced_in_order(spawn):
    """The last phase line of a pod that goes silent is the phase it went silent in."""
    child = spawn("runs", FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE)
    code = child.finish(timeout=120)
    assert code == 0, f"exit {code}\n{child.transcript()}"
    assert child.records[0].get("message") == "worker process started", child.transcript()

    expected = [
        "configuration",
        "dns_preflight",
        "build_worker",
        "validate_generation",
        "record_attempt_start",
        "advance_to_running",
        "workspace",
        "restore_checkpoint",
        "clone",
        "stage_inputs",
        "credentials",
        "revalidate_generation",
        "quota_preflight",
        "runner",
    ]
    assert child.phases() == expected, child.transcript()

    messages = child.messages()
    preflight = messages.index("DNS preflight passed")
    build = next(i for i, r in enumerate(child.records) if _phase("build_worker")(r))
    assert preflight < build, "the clients were built before DNS was checked"
    # The first line the OLD worker wrote was an event, after a Firestore
    # write. The attempt's own start is now announced before it.
    started = next(i for i, r in enumerate(child.records) if _phase("record_attempt_start")(r))
    first_event = messages.index("event")
    assert started < first_event, child.transcript()


def test_a_google_auth_warning_reaches_the_log(spawn):
    """google-auth attaches a NullHandler; without a root handler its warnings vanish.

    The harness's client factory logs the warning google-auth logs when the
    metadata server does not answer, through google-auth's own logger.
    """
    child = spawn("runs", FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE)
    code = child.finish(timeout=120)
    assert code == 0, f"exit {code}\n{child.transcript()}"
    warned = [
        r for r in child.records
        if r.get("logger") == "google.auth.compute_engine._metadata"
        and r.get("severity") == "WARNING"
    ]
    assert warned, f"google-auth's warning never reached stdout\n{child.transcript()}"
    assert "Metadata server unavailable" in warned[0]["message"]


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------


def test_dns_that_drops_every_packet_ends_startup_with_78_within_the_budget(spawn):
    """The incident's NetworkPolicy, as the process sees it: lookups that never answer."""
    child = spawn("dns-drops")
    announced = child.wait_for(_phase("dns_preflight"))
    assert announced is not None, f"no dns_preflight phase\n{child.transcript()}"
    code = child.finish()
    assert code == 78, f"exit {code}: the worker did not give up on DNS\n{child.transcript()}"

    errors = [r for r in child.records if str(r.get("message", "")).startswith("DNS unreachable")]
    assert len(errors) == 1, child.transcript()
    error = errors[0]
    # Timed on the CHILD's clock, from its own two lines: the parent reads
    # them through a pipe, late by however loaded the runner is.
    waited = _seconds_between(announced, error)
    assert DNS_BUDGET_SECONDS - 0.5 <= waited <= DNS_BUDGET_SECONDS + 3, (
        f"gave up {waited:.1f}s into a {DNS_BUDGET_SECONDS:g}s budget\n{child.transcript()}"
    )
    assert all(u["seconds"] >= DNS_BUDGET_SECONDS - 0.1 for u in error["unreachable"]), error
    assert child.exited_at - error["_seen_at"] < 10, "the error was logged but the process lingered"
    assert error["severity"] == "ERROR"
    assert error["phase"] == "dns_preflight"
    assert error["budget_seconds"] == DNS_BUDGET_SECONDS
    for host in ("metadata.google.internal", "firestore.googleapis.com"):
        assert host in error["message"], error
    assert {u["host"] for u in error["unreachable"]} == {
        "metadata.google.internal",
        "firestore.googleapis.com",
    }
    assert "build_worker" not in child.phases(), "a client was built after DNS failed"


def test_a_name_that_does_not_resolve_ends_startup_with_78_at_once(spawn):
    child = spawn("dns-nxdomain")
    announced = child.wait_for(_phase("dns_preflight"))
    assert announced is not None, f"no dns_preflight phase\n{child.transcript()}"
    code = child.finish()
    assert code == 78, f"exit {code}\n{child.transcript()}"
    error = next(
        r for r in child.records if str(r.get("message", "")).startswith("DNS unreachable")
    )
    waited = _seconds_between(announced, error)
    assert waited < 3, f"an immediate NXDOMAIN took {waited:.1f}s\n{child.transcript()}"
    assert all("gaierror" in u["error"] for u in error["unreachable"]), error


# ---------------------------------------------------------------------------
# SIGTERM before the runner exists
# ---------------------------------------------------------------------------


def _sigterm_after(child: Child, phase: str) -> float:
    announced = child.wait_for(_phase(phase))
    assert announced is not None, f"the worker never announced {phase}\n{child.transcript()}"
    # Long enough to be blocked inside the phase, not on its way into it.
    time.sleep(0.5)
    assert child.proc.poll() is None, f"exited during {phase}\n{child.transcript()}"
    sent = time.monotonic()
    child.proc.send_signal(signal.SIGTERM)
    return sent


@pytest.mark.parametrize(
    ("scenario", "phase", "env"),
    [
        # The incident: stuck in the generation check's Firestore read.
        ("stuck-in-generation-check", "validate_generation",
         {"FIRESTORE_EMULATOR_HOST": LOCAL_FIRESTORE}),
        # Before any client exists: the entrypoint's own handler.
        ("dns-drops", "dns_preflight", {}),
    ],
    ids=["generation-check", "dns-preflight"],
)
def test_a_sigterm_before_the_runner_names_its_phase_and_exits_in_seconds(
    spawn, scenario, phase, env
):
    child = spawn(scenario, **env)
    sent = _sigterm_after(child, phase)
    code = child.finish(timeout=30)
    took = child.exited_at - sent

    # 143 returned by the worker itself. A process killed by the signal would
    # report -15, and one that ignored it would still be running.
    assert code == 143, f"exit {code} {took:.1f}s after SIGTERM\n{child.transcript()}"
    assert took < 10, f"{took:.1f}s from SIGTERM to exit\n{child.transcript()}"

    said = [r for r in child.records if "SIGTERM" in str(r.get("message", ""))]
    assert said, f"nothing said about the SIGTERM\n{child.transcript()}"
    assert said[-1]["phase"] == phase, said[-1]
    assert said[-1]["exit_code"] == 143, said[-1]
    if phase == "dns_preflight":
        assert not any(
            str(r.get("message", "")).startswith("DNS unreachable") for r in child.records
        ), "the budget ran out first; the SIGTERM was not what ended it"

    # faulthandler, chained ahead of the handler: every thread's stack.
    stderr = "".join(child.stderr)
    assert "most recent call first" in stderr, f"no stack dump on SIGTERM\n{child.transcript()}"
    if phase == "validate_generation":
        # The dump names where the process was stuck: the read that never answers.
        assert "startup_harness.py" in stderr and "in get" in stderr, stderr[-4000:]
