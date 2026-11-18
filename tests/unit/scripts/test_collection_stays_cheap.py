"""Importing a test module here is collection, and collection must stay cheap.

`pytest tests/unit/scripts -k <expr>` imports every module in this directory
before it selects a single test, so whatever a module does at import time is
paid by every run, lanes and CI alike, however few tests it selects. Measured
2026-10-06 with a per-module import in a fresh interpreter: one module,
test_bench_contention.py, took 1.50 s of the directory's ~1.7 s of module
imports. It imported a control_plane TEST module at the top, which dragged in
control_plane/conftest.py and with it swarm_api, fastapi, scheduler,
quota_broker, google.cloud and grpc. The next two were swarm_api.schemas
(pydantic) and agent_worker, also imported at module top for one test each.

So this holds two things over every module in the directory, in one fresh
interpreter that imports them all the way collection does:

  * none of them loads the service stack at import time. A test that needs it
    imports it inside the test or a fixture, where only a selected test pays;
  * none of them starts a process at import time. A subprocess at module top
    runs on every collection, including the ones that select nothing from it.

It asserts which modules were loaded, not how long it took, so it does not
flake on a slow runner.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
UNIT = HERE.parent

# Top-level packages a scripts test may use but must not import at module top.
# `control_plane.conftest` is listed by full name: the `control_plane` package
# itself is cheap and test_issue_forms reads a helper from it legitimately.
HEAVY = (
    "agent_worker",
    "control_plane.conftest",
    "fastapi",
    "google",
    "grpc",
    "pydantic",
    "quota_broker",
    "scheduler",
    "starlette",
    "swarm_api",
)

_PROBE = r"""
import importlib, json, subprocess, sys

heavy = tuple(json.loads(sys.argv[1]))
modules = json.loads(sys.argv[2])
sys.path.insert(0, sys.argv[3])

spawned = []
current = [None]
_init = subprocess.Popen.__init__

def _record(self, args, *a, **kw):
    spawned.append([current[0], str(args)[:200]])
    return _init(self, args, *a, **kw)

subprocess.Popen.__init__ = _record

def loaded():
    return {h for h in heavy if any(m == h or m.startswith(h + ".") for m in sys.modules)}

import pytest  # noqa: F401  -- pytest is loaded before collection anyway

pulled = {}
failed = {}
before = loaded()
for name in modules:
    current[0] = name
    try:
        importlib.import_module("scripts." + name)
    except BaseException as exc:  # a module-level skip is not an import cost
        failed[name] = type(exc).__name__ + ": " + str(exc)[:200]
    now = loaded()
    if now - before:
        pulled[name] = sorted(now - before)
    before = now

print(json.dumps({"visited": len(modules), "pulled": pulled, "spawned": spawned, "failed": failed}))
"""


def _test_modules() -> list[str]:
    return sorted(p.stem for p in HERE.glob("test_*.py"))


@pytest.fixture(scope="module")
def probe() -> dict:
    """One fresh interpreter importing the whole directory, shared by the tests below."""
    modules = _test_modules()
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(HEAVY), json.dumps(modules), str(UNIT)],
        capture_output=True,
        text=True,
        cwd=UNIT.parents[1],
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # An empty answer must come from a probe that visited every module.
    assert result["visited"] == len(modules) > 50, result
    return result


def test_every_module_in_the_directory_imports(probe: dict) -> None:
    result = probe
    assert result["failed"] == {}, result["failed"]


def test_no_module_loads_the_service_stack_at_import_time(probe: dict) -> None:
    pulled = probe["pulled"]
    assert pulled == {}, (
        "these test modules import heavy packages at module top, so every "
        "collection of tests/unit/scripts pays for them; import inside the test "
        f"or a fixture instead: {pulled}"
    )


def test_no_module_starts_a_process_at_import_time(probe: dict) -> None:
    spawned = probe["spawned"]
    assert spawned == [], f"processes started while importing test modules: {spawned}"
