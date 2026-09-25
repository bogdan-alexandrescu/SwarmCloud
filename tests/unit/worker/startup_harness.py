"""Runs the REAL worker entrypoint, `agent_worker.__main__.main`, in its own process.

`test_startup_is_loud.py` starts this file as a child process and reads what it
writes. It is a separate process, not a function call, for three reasons:

  * **Signals.** The defect under test is what a process does when a real
    SIGTERM arrives while it is stuck. A signal sent to the test process would
    land in pytest.
  * **The root logger.** `configure_logging` installs a process-wide handler on
    stdout. Installed inside pytest, it would outlive the test that installed
    it and write into every later test's captured output.
  * **The exit.** A worker with nothing written leaves through `os._exit`, and
    only a child process can be allowed to do that.

Only what a test cannot provide for real is substituted:

  * Firestore becomes `FakeFirestore` (through `_firestore_client`), with the
    in-memory transaction runner in place of `firestore.transactional`;
  * DNS becomes a stand-in for `socket.getaddrinfo`, in the scenarios that are
    about DNS.

Everything else is production code: the configuration, the preflight,
`build_worker`, the lifecycle, and the mock runner as a genuine subprocess.

    python startup_harness.py <scenario>

    runs                      a healthy attempt; the mock runner runs to the end
    runs-long                 a healthy attempt whose mock runner takes a minute,
                              so a test can SIGTERM it while the agent runs
    stuck-in-generation-check the task read neither returns nor fails
    dns-drops                 every lookup hangs, as under the incident's policy
    dns-nxdomain              every lookup of a name fails at once
    dns-flaky-once            each name's FIRST lookup fails (EAI_AGAIN), and
                              every later one answers: one transient failure

In the dns-drops and dns-nxdomain scenarios, Firestore is ALSO the stuck kind.
So a worker that skips the preflight hangs exactly where the incident's worker
hung. It does not fail some other way that a test could mistake for the
preflight working. dns-flaky-once has a healthy Firestore, because the worker
it describes is expected to start and run.

HARNESS_TERMINATION_LOG, when set, names the file the worker is to treat as its
Kubernetes termination-message file (`startup.TERMINATION_MESSAGE_PATH`). The
test creates it empty, as the kubelet does, and reads what the worker left in it.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_worker.__main__ as entrypoint  # noqa: E402
import agent_worker.control as control_mod  # noqa: E402
import agent_worker.startup as startup_mod  # noqa: E402
from conftest import seed_attempt  # noqa: E402
from fakes import (  # noqa: E402
    FakeCollectionRef,
    FakeDocumentRef,
    FakeFirestore,
    FakeTransactionRunner,
)

SCENARIOS = (
    "runs",
    "runs-long",
    "stuck-in-generation-check",
    "dns-drops",
    "dns-nxdomain",
    "dns-flaky-once",
)

#: What the mock runner is asked to do: one step, at once.
QUICK_RUN = {"prompt": "startup harness", "steps": 1, "sleep_seconds": 0.05}

#: A runner that is still running a minute later, so that a SIGTERM sent a
#: second after the runner phase begins lands while the agent runs.
LONG_RUN = {"prompt": "startup harness, long", "steps": 60, "sleep_seconds": 60}

#: What a resolver that answers says, for a name the harness stands in for.
_ANSWER = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443))]


class _NeverAnswers(FakeDocumentRef):
    """A read that neither returns nor fails: the incident's 300 s retry, unbounded.

    It sleeps in short slices, so a Python signal handler runs between them, as
    it runs between grpc's own 0.1 s condition waits.
    """

    def get(self, *_args: Any, **_kwargs: Any) -> Any:
        while True:
            time.sleep(0.05)


class _TasksNeverAnswer(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _NeverAnswers(self._db, super().document(doc_id).path)


class StuckFirestore(FakeFirestore):
    """Every document read of `tasks/...` hangs. The first one is the generation check."""

    def collection(self, name: str) -> FakeCollectionRef:
        if name == "tasks":
            return _TasksNeverAnswer(self, name)
        return super().collection(name)


_real_getaddrinfo = socket.getaddrinfo


def _is_address(host: Any) -> bool:
    try:
        ipaddress.ip_address(str(host))
    except ValueError:
        return False
    return True


def _lookups_hang(host: Any, *args: Any, **kwargs: Any) -> Any:
    """A resolver whose packets are dropped: nobody ever answers.

    Addresses still resolve, because resolving an address asks nobody.
    """
    if _is_address(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    threading.Event().wait()
    raise AssertionError("unreachable")


def _lookups_fail(host: Any, *args: Any, **kwargs: Any) -> Any:
    if _is_address(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


_asked: set[str] = set()
_asked_lock = threading.Lock()


def _lookups_fail_once(host: Any, *args: Any, **kwargs: Any) -> Any:
    """One transient failure per name, then answers: a resolver that blinked.

    EAI_AGAIN is what glibc reports when the nameserver did not answer in time
    or answered SERVFAIL: the resolver's own word for "try again".
    """
    if _is_address(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    with _asked_lock:
        first = str(host) not in _asked
        _asked.add(str(host))
    if first:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
    return list(_ANSWER)


def _client_for(db: FakeFirestore):
    def _client(_settings: Any) -> FakeFirestore:
        # What google-auth does inside `firestore.Client()` when the metadata
        # server is slow: warn through a logger whose package attached a
        # NullHandler at import. Whether that warning is ever seen depends
        # only on whether the entrypoint configured the root logger.
        import google.auth  # noqa: F401  -- attaches the NullHandler, as in production

        logging.getLogger("google.auth.compute_engine._metadata").warning(
            "Compute Engine Metadata server unavailable on attempt 1 of 5. "
            "Reason: startup harness"
        )
        return db

    return _client


def main(scenario: str) -> int:
    if scenario not in SCENARIOS:
        print(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2
    healthy = scenario in ("runs", "runs-long", "dns-flaky-once")
    db: FakeFirestore = FakeFirestore() if healthy else StuckFirestore()
    seed_attempt(db, task_input=dict(LONG_RUN if scenario == "runs-long" else QUICK_RUN))
    control_mod.FirestoreTransactionRunner = FakeTransactionRunner  # type: ignore[misc]
    entrypoint._firestore_client = _client_for(db)  # type: ignore[assignment]
    termination_log = os.environ.get("HARNESS_TERMINATION_LOG", "").strip()
    if termination_log:
        # Set whether or not the module defines it, so that a worker which
        # never writes a termination message is caught by what the file
        # holds, not by an AttributeError here.
        setattr(startup_mod, "TERMINATION_MESSAGE_PATH", termination_log)
    if scenario == "dns-drops":
        socket.getaddrinfo = _lookups_hang  # type: ignore[assignment]
    elif scenario == "dns-nxdomain":
        socket.getaddrinfo = _lookups_fail  # type: ignore[assignment]
    elif scenario == "dns-flaky-once":
        socket.getaddrinfo = _lookups_fail_once  # type: ignore[assignment]
    return entrypoint.main()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
