"""`python -m agent_worker --self-test` exits 0 before anything a task needs (#363).

WHY IT EXISTS. On 2026-10-07 the owner measured that the first execution of
each Cloud Run job after a new image digest spends 30-59 s importing the image
(ContainerReady "Imported container image in Xs"), and every later start pays
1-3 s. The release now runs one warm execution per worker job after it promotes
new digests (scripts/warm-jobs.sh), so that import is paid by the release and
not by the first tenant task. That execution passes `--args=--self-test`, and
the worker must leave on it at once: no configuration, no lease, no Firestore
client, no task state.

The properties asserted here, against the REAL entrypoint in a child process
with no task identity in its environment:

  * `--self-test` exits 0 and says so in one JSON line on stdout;
  * it builds no Firestore client and never imports `google.cloud.firestore`;
  * the same process without the flag still exits 78 (CONFIG) on the missing
    task identity, so the flag is the only thing that changed the outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# The task identity the dispatcher supplies per execution. A warm execution has
# none of it, and neither do these children.
_TASK_ENV = ("TASK_ID", "ATTEMPT_ID", "LEASE_ID", "GENERATION", "RUNNER_PROFILE", "TENANT_ID")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _TASK_ENV}
    # Nothing in this test may reach a cloud API, whatever the developer's shell holds.
    env["DISABLE_CLOUD_MONITORING"] = "1"
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    return env


_PROBE = r"""
import json, sys
import agent_worker.__main__ as entrypoint

built = []
def _refuse(*args, **kwargs):
    built.append("firestore")
    raise AssertionError("the self-test built a Firestore client")
entrypoint._firestore_client = _refuse

rc = entrypoint.main(sys.argv[1:])
print(json.dumps({
    "rc": int(rc),
    "built": built,
    "firestore_imported": "google.cloud.firestore" in sys.modules,
}))
"""


def _run(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PROBE, *args],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_self_test_exits_0_without_building_or_importing_firestore() -> None:
    proc = _run("--self-test")
    assert proc.returncode == 0, proc.stderr
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert verdict == {"rc": 0, "built": [], "firestore_imported": False}


def test_self_test_line_says_what_it_did() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "agent_worker", "--self-test"],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, proc.stdout
    record = json.loads(lines[0])
    assert record["self_test"] is True
    assert "self-test" in record["message"]


@pytest.mark.parametrize("args", [(), ("--self-testing",)])
def test_without_the_exact_flag_the_worker_still_refuses_a_missing_task(args: tuple[str, ...]) -> None:
    proc = _run(*args)
    verdict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert verdict["rc"] == 78
    assert verdict["built"] == []
