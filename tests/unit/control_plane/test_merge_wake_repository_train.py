"""The wake tick leaves the repository merge train's own check out, as the merge step does.

On 11 November 2026 six merge steps parked CI_PENDING on a base whose rules
require `merge-train`, the commit status scripts/merge-train.sh posts only
on a pull request it boarded. The train boards only `ready` pull requests,
which the merge step labels once it wakes (`agent_worker.merge._board`). The
step's gate already leaves `merge-train` out while the repository's train is
on (`vars.MERGE_TRAIN`, exactly `on`); the tick did not, so it read every
green head as pending and never wrote the wake marker.

What these tests hold:

  1. With the train on, a park whose only pending required check is
     `merge-train` wakes, green.
  2. With the train off, or a switch the tick cannot read, it stays parked:
     `merge-train` is then an ordinary required check (the woken step's own
     answer decides an unreadable switch, as it always has).
  3. Any other required check still pending keeps it parked either way.
  4. The switch is read at most once per repository per tick, only for a
     base that requires `merge-train`, and with the tenant's own token.
  5. The tick and the merge step name the same context, variable and value.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import pytest

from agent_worker import merge as worker_merge
from swarm_api import mergewake

from .test_merge_wake import (  # noqa: F401 - the fixtures are used by name
    CHECK, HEAD, _reads, _tick, _wait, clock, github, parked_merge, tenant_tokens,
    wake_client,
)

TRAIN = "merge-train"
SWITCH = "MERGE_TRAIN"


def _variable_reads(github) -> list[str]:
    return [url for url in _reads(github) if "/actions/variables/" in url]


def _train_required(github) -> None:
    """The base requires the ordinary check and the train's own status."""
    github.require(CHECK, TRAIN, app_id=None)


def test_the_tick_and_the_merge_step_name_the_same_train():
    assert mergewake.TRAIN_CONTEXT == worker_merge.REPOSITORY_TRAIN_CONTEXT == TRAIN
    assert mergewake.TRAIN_VARIABLE == worker_merge.REPOSITORY_TRAIN_VARIABLE == SWITCH
    assert mergewake.TRAIN_ON == worker_merge.REPOSITORY_TRAIN_ON == "on"


def test_only_the_trains_own_check_pending_wakes_with_the_train_on(db, wake_client, github):
    parked_merge(db, pending=[TRAIN])
    _train_required(github)
    github.variables[SWITCH] = "on"
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _wait(db)[mergewake.WAKE_MARKER]
    assert _wait(db)["wake_reason"] == "green"
    assert TRAIN not in _wait(db)[mergewake.READING_KEY]["pending"]
    assert len(_variable_reads(github)) == 1


@pytest.mark.parametrize("value", [None, "off", "On", "true"])
def test_with_the_train_off_its_check_is_an_ordinary_pending_one(db, wake_client, github, value):
    parked_merge(db, pending=[TRAIN])
    _train_required(github)
    if value is not None:
        github.variables[SWITCH] = value
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 0 and body["report"]["waiting"] == 1, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _wait(db)[mergewake.READING_KEY]["pending"] == [TRAIN]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_switch_the_tick_cannot_read_is_off(db, wake_client, github, status):
    """Unlike the step, the tick never reads an unreadable switch as on: the
    park waits, and the step's own read decides when it wakes otherwise."""
    parked_merge(db, pending=[TRAIN])
    _train_required(github)
    github.variables_answer = status
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["failures"] == [], body
    assert body["report"]["woken"] == 0 and body["report"]["waiting"] == 1, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert _wait(db)[mergewake.READING_KEY]["pending"] == [TRAIN]


@pytest.mark.parametrize("value", ["on", "off"])
def test_another_required_check_pending_stays_parked_either_way(db, wake_client, github, value):
    parked_merge(db, pending=[CHECK, TRAIN])
    _train_required(github)
    github.variables[SWITCH] = value
    github.check(HEAD, CHECK, None, status="in_progress")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 0 and body["report"]["waiting"] == 1, body
    assert mergewake.WAKE_MARKER not in _wait(db)
    assert CHECK in _wait(db)[mergewake.READING_KEY]["pending"]


def test_a_red_check_still_wakes_with_the_train_on(db, wake_client, github):
    parked_merge(db, pending=[TRAIN])
    _train_required(github)
    github.variables[SWITCH] = "on"
    github.check(HEAD, CHECK, "failure")

    body = _tick(wake_client).json()

    assert _wait(db)["wake_reason"] == "red", body


def test_the_switch_is_read_once_per_repository_per_tick(db, wake_client, github):
    parked_merge(db, "task_one", pending=[TRAIN])
    parked_merge(db, "task_two", pending=[TRAIN])
    _train_required(github)
    github.variables[SWITCH] = "on"
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 2, body
    assert len(_variable_reads(github)) == 1


def test_a_base_that_does_not_require_the_train_never_reads_the_switch(db, wake_client, github):
    parked_merge(db)
    github.variables[SWITCH] = "on"
    github.check(HEAD, CHECK, "success")

    body = _tick(wake_client).json()

    assert body["report"]["woken"] == 1, body
    assert _variable_reads(github) == []


def test_the_switch_is_read_with_the_tenants_own_token(db, wake_client, github, tenant_tokens):
    parked_merge(db, pending=[TRAIN])
    _train_required(github)
    github.variables[SWITCH] = "on"
    github.check(HEAD, CHECK, "success")

    _tick(wake_client)

    eng_token = tenant_tokens.issued["swarm-tenant-eng-git"]
    reads = [(url, headers) for method, url, headers, _ in github.calls
             if method == "GET" and "/actions/variables/" in url]
    assert reads and all(eng_token in headers["Authorization"] for _, headers in reads)
