"""The worker records the dispatcher's `<namespace>/<job>`, never its pod's hostname (#1804).

A GKE Job sets none of CLOUD_RUN_EXECUTION, K_REVISION or JOB_NAME, so
`_execution_name()` fell through to HOSTNAME -- the pod name, e.g.
`swarm-918a848f969e406389fd-1-2cq5s` -- and `record_attempt_start` wrote it
over the dispatcher's record. The GKE dispatcher now renders
SWARM_EXECUTION_NAME into the Job (`scheduler.dispatch.GKE_EXECUTION_NAME_ENV`),
and the worker prefers it over every other source.
"""

from __future__ import annotations

import pytest

from agent_worker import lifecycle
from agent_worker.lifecycle import _execution_name

GKE_NAME = "swarm-tenant-eng/swarm-918a848f969e406389fd-1"
POD = "swarm-918a848f969e406389fd-1-2cq5s"
SOURCES = ("CLOUD_RUN_EXECUTION", "K_REVISION", "JOB_NAME", "HOSTNAME")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (*SOURCES, lifecycle.EXECUTION_NAME_ENV):
        monkeypatch.delenv(name, raising=False)


def test_the_env_name_is_the_one_the_gke_dispatcher_renders():
    from scheduler.dispatch import GKE_EXECUTION_NAME_ENV

    # MUTATION: rename either side and the worker never sees the dispatcher's name.
    assert lifecycle.EXECUTION_NAME_ENV == GKE_EXECUTION_NAME_ENV == "SWARM_EXECUTION_NAME"


def test_a_gke_worker_records_the_dispatchers_name_over_its_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", POD)
    monkeypatch.setenv(lifecycle.EXECUTION_NAME_ENV, GKE_NAME)
    # MUTATION: drop SWARM_EXECUTION_NAME from `_execution_name`'s sources and
    # this is the pod name the gate and cancel cannot find.
    assert _execution_name() == GKE_NAME


@pytest.mark.parametrize("other", SOURCES)
def test_the_dispatchers_name_wins_over_every_other_source(monkeypatch, other):
    monkeypatch.setenv(other, "something-else")
    monkeypatch.setenv(lifecycle.EXECUTION_NAME_ENV, GKE_NAME)
    # MUTATION: put SWARM_EXECUTION_NAME after any of the four and that one wins.
    assert _execution_name() == GKE_NAME


def test_a_blank_dispatcher_name_falls_back_as_before(monkeypatch):
    """Cloud Run is unchanged: no SWARM_EXECUTION_NAME, CLOUD_RUN_EXECUTION wins."""
    monkeypatch.setenv(lifecycle.EXECUTION_NAME_ENV, "  ")
    monkeypatch.setenv("CLOUD_RUN_EXECUTION", "swarm-job-eng-mock-x1")
    monkeypatch.setenv("HOSTNAME", "localhost")
    # MUTATION: return a blank SWARM_EXECUTION_NAME and an attempt records "".
    assert _execution_name() == "swarm-job-eng-mock-x1"
