"""Run one function of a verification-gate suite, offline, with its reads stubbed.

The gate's suites (scripts/concurrency-test.sh, race-test.sh, e2e-test.sh) run
top to bottom against a deployed platform as soon as they are executed, so a
unit test cannot execute them. What it can do is what
test_smoke_own_capacity.py does: source common.sh and testlib.sh exactly as the
suites do, lift the function under test out of the SHIPPED script (so the test
cannot pass against a copy of it), define stand-ins for the reads after the
libraries, and run a snippet. No credential, no network, no emulator.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


def function_source(script: Path, name: str) -> str:
    """The text of `name() { ... }` in `script`, up to its closing `}` line."""
    text = script.read_text()
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    assert match, f"{script.relative_to(ROOT)} defines no function {name}()"
    return match.group(0)


def functions(script: Path, *names: str) -> str:
    return "\n".join(function_source(script, n) for n in names)


def run_bash(body: str, *, env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """`body` after common.sh and testlib.sh, in a fresh bash, colour off."""
    full_env = dict(os.environ)
    full_env["NO_COLOR"] = "1"
    full_env.pop("SWARM_ENV_FILE", None)
    full_env.pop("SWARM_API_TENANT", None)
    full_env["SWARM_POLL_INTERVAL_SECONDS"] = "0"
    if env:
        full_env.update(env)
    script = (
        f'source "{SCRIPTS}/lib/common.sh"\n'
        f'source "{SCRIPTS}/lib/testlib.sh"\n'
        + body
    )
    return subprocess.run(
        ["bash", "-c", script, "gate-fix-test"],
        cwd=ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def firestore_value(value):
    """One Python value, spelled the way the Firestore REST API returns it."""
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: firestore_value(v) for k, v in value.items()}}}
    raise TypeError(value)


def firestore_document(collection: str, doc_id: str, data: dict) -> dict:
    return {
        "name": f"projects/p/databases/d/documents/{collection}/{doc_id}",
        "fields": {k: firestore_value(v) for k, v in data.items()},
    }


def lines(result: subprocess.CompletedProcess, marker: str) -> list[str]:
    """The PASS / FAIL / SKIP lines a run printed, stripped."""
    return [
        line.strip()
        for line in (result.stdout + result.stderr).splitlines()
        if line.strip().startswith(marker)
    ]
