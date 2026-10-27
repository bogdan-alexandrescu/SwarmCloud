"""A CI fix's red head is copied INTO the signed dispatch block (#2276).

A CI-fix task can wait hours before it starts, and by then the pull request
may have moved past the red commit or been merged. The worker is to check
that at attempt start, but it acts only on metadata the step-spec signature
covers (`swarm_common.specsign.SIGNED_METADATA_KEYS`, the CONFLICT_ROUND_FIELD
comment in `agent_worker.lifecycle`), and `metadata.ci_fix` is not covered.
So swarm-api copies the record's `pull_request` and `head_sha` -- and nothing
else of it -- into `metadata.dispatch.ci_fix_head` at submission, on a
continuation only, as it does the conflict round mark (#1321).

No credentials, no network, no emulator.
"""

from __future__ import annotations

from swarm_common.specsign import SIGNED_METADATA_KEYS

from swarm_api.validation import CI_FIX_HEAD_FIELD, DISPATCH_METADATA_KEY, DispatchOptions

from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    PR,
    REPO_URL,
    _fix_task,
    _members_hold_grants,
    _original,
    _red_run_payload,
    _submit,
    clock,
    fix_client,
    github,
    tenant_tokens,
)


def _dispatch(db, response) -> dict:
    assert response.status_code == 201, response.text
    return _fix_task(db, response)["metadata"][DISPATCH_METADATA_KEY]


def test_the_dispatch_block_is_signed():
    assert DISPATCH_METADATA_KEY in SIGNED_METADATA_KEYS


def test_a_ci_fix_continuation_carries_the_red_head_in_its_signed_block(fix_client, db):
    original = _original(fix_client)
    dispatch = _dispatch(db, _submit(fix_client, _red_run_payload(original)))
    assert dispatch[CI_FIX_HEAD_FIELD] == {"pull_request": PR, "head_sha": HEAD}
    # Only the head: no claim, no post-back, no run id or attempt count.
    assert dispatch == {
        "strategy": "direct-pr",
        "carrier": "checkpoints",
        "continues": original,
        CI_FIX_HEAD_FIELD: {"pull_request": PR, "head_sha": HEAD},
    }
    assert "ci_fix" not in dispatch


def test_a_ci_fix_record_without_a_head_marks_nothing(fix_client, db):
    original = _original(fix_client)
    payload = _red_run_payload(original)
    del payload["metadata"]["ci_fix"]["head_sha"]
    dispatch = _dispatch(db, _submit(fix_client, payload))
    assert CI_FIX_HEAD_FIELD not in dispatch, dispatch


def test_a_continuation_that_is_not_a_ci_fix_stores_no_head(fix_client, db):
    original = _original(fix_client)
    payload = _red_run_payload(original)
    del payload["metadata"]["ci_fix"]
    dispatch = _dispatch(db, _submit(fix_client, payload))
    assert CI_FIX_HEAD_FIELD not in dispatch, dispatch


def test_a_workflow_that_continues_nothing_stores_no_head(fix_client, db):
    response = _submit(fix_client, {
        "steps": [{"step_id": "work", "runner_profile": "mock", "input": {"prompt": "do it"}}],
        "strategy": "direct-pr",
        "repository_url": REPO_URL,
    })
    dispatch = _dispatch(db, response)
    assert CI_FIX_HEAD_FIELD not in dispatch, dispatch


def test_the_field_is_absent_unless_set():
    assert CI_FIX_HEAD_FIELD not in DispatchOptions().to_metadata()
    block = DispatchOptions(
        strategy="direct-pr", ci_fix_head={"pull_request": PR, "head_sha": HEAD},
    ).to_metadata()
    assert block[CI_FIX_HEAD_FIELD] == {"pull_request": PR, "head_sha": HEAD}
