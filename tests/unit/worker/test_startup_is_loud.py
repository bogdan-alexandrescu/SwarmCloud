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
  * a DNS that drops every packet ends startup with exit 78 after the
    preflight's bounded retries, with one warning per failed attempt and one
    error line naming the hosts. A DNS that answers NXDOMAIN is retried the
    same number of times, with the backoff between attempts;
  * the retries SPAN 30-45 s on the worker's own clock however the lookups
    fail. Lookups that fail at once must not use every attempt up in the few
    seconds the backoffs add to (review of PR #59: that was about 7 s);
  * ONE transient DNS failure does not fail the attempt: the next lookup
    answers and the worker starts and runs (owner, 2026-09-25: "have some
    retry logic before failing, to prevent intermittent dns failures"). Nor
    does a resolver that fails every lookup at once for 15 s and then comes
    back;
  * a worker that exits 78 leaves its cause, as one JSON line, in the file
    Kubernetes reads as the container's termination message, which is how
    the reconciler learns it without Firestore;
  * a SIGTERM before the runner exists names the phase it arrived in and
    exits 143 within seconds, and it dumps every thread's stack to stderr;
  * a SIGTERM while the agent RUNS dumps nothing, and says so in one INFO
    line. GKE files every stderr line as ERROR, and a mid-run SIGTERM is the
    ordinary way an attempt is stopped, not a fault (owner, 2026-09-25).

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

#: The preflight's budget PER ATTEMPT, restated here on purpose. The test
#: asserts the process honours the number the brief asked for, which a test
#: that read it back from the module could not do.
DNS_BUDGET_SECONDS = 10.0

#: The owner's bounds on the retries, as he stated them on 2026-09-25: "e.g.
#: 3-4 attempts over ~30-45 s total". The exact choice is the worker's. These
#: hold what the worker DID inside what was asked: the attempts it made, and
#: the seconds its own log lines say the retries took. Not the window it
#: reports, which is a constant and says nothing about how long it waited.
DNS_ATTEMPTS_ALLOWED = (3, 4)
DNS_WINDOW_ALLOWED_SECONDS = (30.0, 45.0)

#: Slack on the child's own clock, for its log timestamps and a loaded runner.
#: The worker sleeps to a schedule, so what it overshoots by is scheduling
#: noise, not a lookup.
CLOCK_SLACK_SECONDS = 0.5

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


def _dns_error(child: Child) -> dict[str, Any]:
    errors = [r for r in child.records if str(r.get("message", "")).startswith("DNS unreachable")]
    assert len(errors) == 1, f"{len(errors)} 'DNS unreachable' lines\n{child.transcript()}"
    return errors[0]


def _retry_warnings(child: Child) -> list[dict[str, Any]]:
    return [
        r for r in child.records
        if str(r.get("message", "")).startswith("DNS preflight attempt")
    ]


def _assert_bounded_retries(
    child: Child, announced: dict[str, Any], error: dict[str, Any]
) -> list[dict[str, Any]]:
    """The retries it made, and the time they TOOK, inside the owner's bounds; one line each.

    The time is the child's own: from its dns_preflight phase line to its
    error line, by the `time` each carries. The parent reads both through a
    pipe, late by however loaded the runner is, so the parent's clock would
    measure the runner, not the worker.
    """
    attempts = error.get("attempts")
    low, high = DNS_ATTEMPTS_ALLOWED
    assert isinstance(attempts, int) and low <= attempts <= high, (
        f"the preflight made {attempts!r} attempts; the owner asked for {low}-{high}\n"
        f"{child.transcript()}"
    )
    w_low, w_high = DNS_WINDOW_ALLOWED_SECONDS
    waited = _seconds_between(announced, error)
    assert w_low - CLOCK_SLACK_SECONDS <= waited <= w_high + CLOCK_SLACK_SECONDS, (
        f"the retries gave up {waited:.1f}s after the preflight began; the owner asked "
        f"for about {w_low:g}-{w_high:g}s, so that a resolver outage shorter than that "
        f"does not fail the task\n{child.transcript()}"
    )
    # What the worker says it took agrees with what its lines show.
    assert abs(error.get("seconds", -1) - waited) <= CLOCK_SLACK_SECONDS, (error, waited)
    warnings = _retry_warnings(child)
    # Every failed attempt is logged: the ones that were retried as a warning
    # each, the last one as the error.
    assert len(warnings) == attempts - 1, child.transcript()
    for number, warning in enumerate(warnings, start=1):
        assert warning["severity"] == "WARNING", warning
        assert warning["phase"] == "dns_preflight", warning
        assert warning["attempt"] == number, warning
        assert warning["retry_in_seconds"] > 0, warning
        assert warning["unreachable"], warning
    return warnings


def test_dns_that_drops_every_packet_ends_startup_with_78_after_bounded_retries(spawn):
    """The incident's NetworkPolicy, as the process sees it: lookups that never answer.

    A policy that drops DNS does not heal by waiting, so the retries end in 78
    like the single attempt did. What changed is how long it takes: every
    attempt waits out its own budget, and the backoff comes between them.
    """
    child = spawn("dns-drops")
    announced = child.wait_for(_phase("dns_preflight"))
    assert announced is not None, f"no dns_preflight phase\n{child.transcript()}"
    code = child.finish(timeout=PATIENCE_SECONDS + 30)
    assert code == 78, f"exit {code}: the worker did not give up on DNS\n{child.transcript()}"

    error = _dns_error(child)
    _assert_bounded_retries(child, announced, error)
    # Timed on the CHILD's clock, from its own two lines: the parent reads
    # them through a pipe, late by however loaded the runner is. Lookups that
    # never answer spend the whole window, and no more.
    waited = _seconds_between(announced, error)
    window = error["window_seconds"]
    assert window - 1.0 <= waited <= window + 5, (
        f"gave up {waited:.1f}s into a {window:g}s retry window\n{child.transcript()}"
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


def test_a_name_that_does_not_resolve_is_retried_with_backoff_then_ends_startup_with_78(spawn):
    """NXDOMAIN fails each attempt at once, so what is left to see is the backoff.

    Retried like any other failure. A NodeLocal DNSCache or kube-dns pod that
    is restarting can answer SERVFAIL or NXDOMAIN for a moment, and the
    preflight cannot tell that moment from a name that will never exist.

    And retried for as LONG as a lookup that hangs is. The first version gave
    up about 7 s in here, because only its backoffs took any time, while a
    restarting resolver pod takes longer than that to come back (review of
    PR #59). `_assert_bounded_retries` holds the time to the owner's 30-45 s.
    """
    child = spawn("dns-nxdomain")
    announced = child.wait_for(_phase("dns_preflight"))
    assert announced is not None, f"no dns_preflight phase\n{child.transcript()}"
    code = child.finish()
    assert code == 78, f"exit {code}\n{child.transcript()}"
    error = _dns_error(child)
    warnings = _assert_bounded_retries(child, announced, error)
    assert all("gaierror" in u["error"] for u in error["unreachable"]), error

    # Each retry waited the backoff it announced before it asked again. The
    # lookups themselves take no time, so the gap between two lines is it.
    lines = [*warnings, error]
    for before, after in zip(lines, lines[1:]):
        gap = _seconds_between(before, after)
        announced_wait = before["retry_in_seconds"]
        assert announced_wait - 0.3 <= gap <= announced_wait + 3, (
            f"announced a {announced_wait:g}s wait and asked again after {gap:.1f}s\n"
            f"{child.transcript()}"
        )
    waited = _seconds_between(announced, error)
    assert waited < error["window_seconds"], (waited, error["window_seconds"])


def test_one_transient_dns_failure_does_not_fail_the_attempt(spawn):
    """Each name fails its first lookup and answers the next: the worker starts and runs.

    Before the retries, this was exit 78: one blink of the resolver ended the
    attempt, and with 78 made non-retryable it would have ended the TASK.
    """
    child = spawn("dns-flaky-once")
    code = child.finish(timeout=120)
    assert code == 0, f"exit {code}: one transient DNS failure ended the attempt\n{child.transcript()}"

    warnings = _retry_warnings(child)
    assert len(warnings) == 1, f"expected one retried attempt\n{child.transcript()}"
    [warning] = warnings
    assert warning["severity"] == "WARNING" and warning["attempt"] == 1, warning
    assert {u["host"] for u in warning["unreachable"]} == {
        "metadata.google.internal",
        "firestore.googleapis.com",
    }, warning
    assert all("Temporary failure in name resolution" in u["error"] for u in warning["unreachable"])
    messages = child.messages()
    assert "DNS preflight passed" in messages, child.transcript()
    assert not any(m.startswith("DNS unreachable") for m in messages), child.transcript()
    assert child.phases()[-1] == "runner", child.phases()


def test_a_resolver_that_fails_at_once_for_fifteen_seconds_does_not_fail_the_attempt(spawn):
    """The outage the retries exist for, in the shape that fails fastest.

    Every lookup fails at once with EAI_AGAIN for 15 s from the first, then
    every lookup answers (`startup_harness.DNS_OUTAGE_SECONDS`). That is a
    kube-dns or NodeLocal DNSCache pod restarting during a node upgrade, or an
    upstream resolver answering SERVFAIL for a while. With 78 failing the task
    for good, a worker that gave up inside the outage would end a task that
    its next lookup would have started (review of PR #59).
    """
    outage = 15.0  # startup_harness.DNS_OUTAGE_SECONDS, restated: see DNS_BUDGET_SECONDS
    child = spawn("dns-down-15s")
    announced = child.wait_for(_phase("dns_preflight"))
    assert announced is not None, f"no dns_preflight phase\n{child.transcript()}"
    code = child.finish(timeout=120)
    assert code == 0, (
        f"exit {code}: a {outage:g}s resolver outage ended the attempt\n{child.transcript()}"
    )

    messages = child.messages()
    assert not any(m.startswith("DNS unreachable") for m in messages), child.transcript()
    passed = [r for r in child.records if r.get("message") == "DNS preflight passed"]
    assert len(passed) == 1, child.transcript()
    # It passed because it was still asking once the outage was over, on the
    # child's own clock, and not because the outage never happened.
    assert _seconds_between(announced, passed[0]) >= outage - CLOCK_SLACK_SECONDS, (
        f"passed {_seconds_between(announced, passed[0]):.1f}s in, inside a {outage:g}s "
        f"outage\n{child.transcript()}"
    )
    warnings = _retry_warnings(child)
    assert warnings and len(warnings) == passed[0]["attempts"] - 1, child.transcript()
    for number, warning in enumerate(warnings, start=1):
        assert warning["severity"] == "WARNING" and warning["attempt"] == number, warning
        assert warning["retry_in_seconds"] > 0, warning
    assert child.phases()[-1] == "runner", child.phases()


# ---------------------------------------------------------------------------
# the cause, where the reconciler can read it without Firestore
# ---------------------------------------------------------------------------


def _termination_message(path: Path) -> dict[str, Any]:
    text = path.read_text()
    assert text.strip(), "the worker left no termination message"
    assert len(text.encode("utf-8")) <= 4096, "the kubelet keeps 4096 bytes; this would be cut"
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1, f"one JSON line expected, got {len(lines)}: {text!r}"
    record = json.loads(lines[0])
    assert isinstance(record, dict), record
    return record


def test_a_dns_failure_leaves_its_cause_as_the_termination_message(spawn, tmp_path):
    """What `state.terminated.message` reads on the worker's pod after a 78.

    Kubernetes creates the file empty (`terminationMessagePath`, default
    /dev/termination-log) and copies what the container wrote into the pod
    status. The reconciler reads pods already (the `swarm-reaper` Role), and
    it cannot read Firestore's view of a worker that never reached Firestore.
    """
    termination_log = tmp_path / "termination-log"
    termination_log.write_text("")
    child = spawn("dns-nxdomain", HARNESS_TERMINATION_LOG=str(termination_log))
    code = child.finish()
    assert code == 78, f"exit {code}\n{child.transcript()}"

    record = _termination_message(termination_log)
    assert record["exit_code"] == 78, record
    assert record["phase"] == "dns_preflight", record
    assert record["cause"] == (
        "DNS unreachable (metadata.google.internal, firestore.googleapis.com)"
    ), record
    assert str(record["message"]).startswith("DNS unreachable"), record


def test_a_configuration_failure_leaves_its_cause_as_the_termination_message(spawn, tmp_path):
    termination_log = tmp_path / "termination-log"
    termination_log.write_text("")
    child = spawn(
        "runs",
        FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE,
        PROJECT_ID=None,
        HARNESS_TERMINATION_LOG=str(termination_log),
    )
    code = child.finish()
    assert code == 78, f"exit {code}\n{child.transcript()}"
    record = _termination_message(termination_log)
    assert record["exit_code"] == 78 and record["phase"] == "configuration", record
    assert "PROJECT_ID" in record["cause"], record


def test_a_worker_that_starts_leaves_no_termination_message(spawn, tmp_path):
    """Only a 78 writes one. A run that succeeds leaves the kubelet's empty file alone."""
    termination_log = tmp_path / "termination-log"
    termination_log.write_text("")
    child = spawn(
        "runs",
        FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE,
        HARNESS_TERMINATION_LOG=str(termination_log),
    )
    code = child.finish(timeout=120)
    assert code == 0, f"exit {code}\n{child.transcript()}"
    assert termination_log.read_text() == ""


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


# ---------------------------------------------------------------------------
# SIGTERM while the agent runs
# ---------------------------------------------------------------------------


def test_a_sigterm_while_the_agent_runs_logs_one_info_line_and_dumps_no_stacks(spawn):
    """The stack dump is for a worker stuck STARTING. A running one is simply being stopped.

    faulthandler writes to stderr, and GKE files every stderr line as ERROR.
    Once the runner child exists, a SIGTERM is the ordinary way an attempt
    ends (the reconciler deleting its Job, a node drain, a cancellation), so a
    dump there is a page of false errors per stop. What is asked instead
    (owner, 2026-09-25): one INFO line, "stopping: SIGTERM in phase <phase>".
    The stop itself is unchanged: the runner is stopped, the attempt
    checkpoints and parks, exit 75.
    """
    child = spawn("runs-long", FIRESTORE_EMULATOR_HOST=LOCAL_FIRESTORE)
    sent = _sigterm_after(child, "runner")
    code = child.finish(timeout=PATIENCE_SECONDS)
    took = child.exited_at - sent
    assert code == 75, f"exit {code} {took:.1f}s after SIGTERM\n{child.transcript()}"

    stderr = "".join(child.stderr)
    assert "most recent call first" not in stderr, (
        f"a mid-run SIGTERM dumped the thread stacks\n{child.transcript()}"
    )
    said = [r for r in child.records if "SIGTERM" in str(r.get("message", ""))]
    assert len(said) == 1, f"{len(said)} lines about the SIGTERM\n{child.transcript()}"
    [line] = said
    assert line["message"] == "stopping: SIGTERM in phase runner", line
    assert line["severity"] == "INFO", line
    assert line.get("phase") == "runner", line
