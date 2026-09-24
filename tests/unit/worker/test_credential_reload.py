"""A credential revoked under a running agent is not a failed attempt.

WHY THIS PATH EXISTS. Refreshing an OAuth credential REVOKES the previously
issued access token -- measured, and the single most consequential fact about
running Claude subscriptions from a pool. The platform refreshes every
registered account on a timer, deliberately without an "in use" filter, because
an account nobody refreshes rots until the day it is wanted. Those two correct
rules collide: a long attempt can have the token in its environment revoked
underneath it, through no fault of its own.

The worker's response is to re-read the secret -- which already holds the
replacement, because the refresher writes it back immediately -- and restart in
place. Not to park (the token is gone permanently, so there is no reset to wait
for) and not to fail (that would spend one of the task's three attempts on
something a single API call fixes).
"""

from __future__ import annotations

import pytest

from agent_worker.errors import ExitCode
from agent_worker.lifecycle import MAX_CREDENTIAL_RELOADS
from agent_worker.runners.base import EXIT_CREDENTIAL_REVOKED, EXIT_QUOTA_EXHAUSTED
from agent_worker.runners.cliagent import detect_credential_failure, detect_rate_limit
from swarm_common.states import TaskState

from conftest import seed_attempt


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"type":"authentication_error","message":"OAuth access token has been revoked."}',
        '{"is_error":true,"api_error_status":401,"result":"Invalid API key"}',
        "Invalid API key · Fix external API key",
        "401 Unauthorized",
        "Please run /login to authenticate",
    ],
)
def test_a_refused_credential_is_recognised(text):
    hit, marker = detect_credential_failure(text)
    assert hit is True
    assert marker


@pytest.mark.parametrize(
    "text",
    [
        "the test asserted a 401 response and passed",
        "HTTP status codes: 200, 301, 404, 500",
        "wrote 401 lines to the transcript",
        "everything completed successfully",
    ],
)
def test_ordinary_agent_output_mentioning_401_is_not_a_refusal(text):
    """A bare "401" occurs in real agent output -- a transcript discussing HTTP,
    a test fixture, a line count. Treating it as a dead credential would
    silently restart a healthy run and reload a secret that was never stale."""
    hit, _ = detect_credential_failure(text)
    assert hit is False


def test_a_rate_limit_is_not_mistaken_for_a_refused_credential():
    """They arrive from the same provider over the same connection and mean
    opposite things. Reading a 429 as a dead credential would reload a
    perfectly good secret and restart straight back into the same 429 --
    turning a wait into a hot loop."""
    body = '{"type":"rate_limit_error","message":"429 too many requests"}'
    assert detect_rate_limit(body)[0] is True
    # cliagent checks the rate limit FIRST; this asserts the ordering matters
    # by showing the two are not mutually exclusive on text alone.
    assert detect_credential_failure(body)[0] is False


def test_the_two_exit_codes_are_distinct():
    """The worker branches on them and they must never collide."""
    assert EXIT_CREDENTIAL_REVOKED != EXIT_QUOTA_EXHAUSTED


# -- the worker's response -------------------------------------------------


def test_a_revoked_credential_is_reloaded_and_the_attempt_survives(db, worker_factory):
    """The property the whole path exists for: the attempt completes."""
    seed_attempt(
        db,
        task_input={
            "prompt": "work through a rotation",
            "steps": 1,
            "sleep_seconds": 0.01,
            "credential_revoked_times": 1,
        },
    )
    worker, _, _ = worker_factory()

    assert worker.run() == ExitCode.OK
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_reloading_is_bounded_so_a_broken_account_cannot_run_forever(db, worker_factory):
    """A credential that was ROTATED is fixed by one reload. One that is
    BROKEN is not fixed by any number, and retrying it forever would turn a
    single bad account into an attempt that never fails, never finishes, and
    holds its lease the whole time."""
    seed_attempt(
        db,
        task_input={
            "prompt": "never gets a working credential",
            "steps": 1,
            "sleep_seconds": 0.01,
            "credential_revoked_times": MAX_CREDENTIAL_RELOADS + 5,
        },
    )
    worker, _, _ = worker_factory()

    assert worker.run() != ExitCode.OK
    assert db.doc("tasks/task_1")["state"] != TaskState.SUCCEEDED.value


def test_a_reload_does_not_spend_one_of_the_tasks_attempts(db, worker_factory):
    """The whole point of handling this in-worker. A rotation is not the
    task's fault and must not count against its retries."""
    seed_attempt(
        db,
        task_input={
            "prompt": "rotated once",
            "steps": 1,
            "sleep_seconds": 0.01,
            "credential_revoked_times": 1,
        },
    )
    worker, _, _ = worker_factory()
    worker.run()

    task = db.doc("tasks/task_1")
    # `attempt_count` is incremented by the CONTROL PLANE when it hands out an
    # attempt, so an in-worker reload must leave it alone.
    assert int(task.get("attempt_count", 1)) == 1
