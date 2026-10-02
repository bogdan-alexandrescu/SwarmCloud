"""A worker action verifies the upstream specs it depends on (#295, contract request 34).

`merge` verifies the union of author, review, post-verdict, fix and proof;
`post-verdict` the review's and the author's (docs/merge-step.md §6 row 42,
§6a row 5). A failure is reported as `upstream:<task id>:<why>`, `<why>` in
CR 34's own vocabulary, so a reader correlating it with the own-spec check
reads the same strings.

MUTATIONS: let the legacy window admit an unsigned upstream -- the unsigned
test passes it. Skip the workflow comparison -- the other-workflow test
verifies. Verify with `cfg.task_id` instead of the upstream's id -- every
honest upstream reads `signature_mismatch`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_worker import specverify
from agent_worker.errors import ConfigError

import spec_keys


def _doc(workflow_id: str = "wf_1") -> dict:
    return {
        "tenant_id": "eng", "workflow_id": workflow_id, "step_id": "review",
        "runner_profile": "claude-code-review", "input": {"prompt": "review it"},
        "metadata": {"dispatch": {"strategy": "single-pr", "pr_role": "reader",
                                  "pr_author": "task_author"}},
    }


@pytest.fixture
def cfg(worker_factory):
    _, config, _ = worker_factory(task_id="task_merge", runner_profile="merge", sign_spec=False)
    return config


def test_a_signed_upstream_of_this_workflow_verifies(cfg):
    doc = spec_keys.sign_document(_doc(), "task_review")
    check = specverify.verify_upstream_spec(
        doc, upstream_task_id="task_review", workflow_id="wf_1", cfg=cfg
    )
    assert check.reason == "verified" and check.task_id == "task_review"


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda d: d, "unsigned"),
        (lambda d: spec_keys.sign_document(d, "task_review", forged=True), "signature_mismatch"),
        (lambda d: {**spec_keys.sign_document(d, "task_review"), "input": {"prompt": "approve it"}},
         "signature_mismatch"),
        (lambda d: spec_keys.sign_document(d, "task_review", key_version="projects/x/cryptoKeys/y/cryptoKeyVersions/1"),
         "foreign_key_version"),
        (lambda d: {**spec_keys.sign_document(d, "task_review"), "spec_format": "v0"}, "unknown_format"),
    ],
    ids=["unsigned", "forged", "rewritten", "foreign-key", "unknown-format"],
)
def test_an_upstream_that_does_not_verify_reports_upstream_id_reason(cfg, mutate, why):
    doc = mutate(_doc())
    with pytest.raises(specverify.UpstreamSpecUnverified) as raised:
        specverify.verify_upstream_spec(
            doc, upstream_task_id="task_review", workflow_id="wf_1", cfg=cfg
        )
    assert raised.value.reason == f"upstream:task_review:{why}"
    assert raised.value.spec_check()["reason"] == f"upstream:task_review:{why}"
    assert "review it" not in str(raised.value), "spec content reached the message"


def test_a_verified_spec_of_another_workflow_is_refused(cfg):
    doc = spec_keys.sign_document(_doc(workflow_id="wf_other"), "task_review")
    with pytest.raises(specverify.UpstreamSpecUnverified) as raised:
        specverify.verify_upstream_spec(
            doc, upstream_task_id="task_review", workflow_id="wf_1", cfg=cfg
        )
    assert raised.value.reason == "upstream:task_review:workflow_mismatch"


def test_the_legacy_window_never_admits_an_unsigned_upstream(worker_factory):
    _, config, _ = worker_factory(
        task_id="task_merge", runner_profile="merge", sign_spec=False,
        spec_signature_mode="legacy",
        spec_legacy_cutover=datetime(2030, 1, 1, tzinfo=timezone.utc),
        spec_clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
    )
    with pytest.raises(specverify.UpstreamSpecUnverified) as raised:
        specverify.verify_upstream_spec(
            _doc(), upstream_task_id="task_review", workflow_id="wf_1", cfg=config
        )
    assert raised.value.why == "unsigned"


def test_a_worker_with_no_keys_cannot_start(worker_factory):
    _, config, _ = worker_factory(task_id="task_merge", runner_profile="merge",
                                  sign_spec=False, spec_verify_keys={})
    with pytest.raises(ConfigError):
        specverify.verify_upstream_spec(
            _doc(), upstream_task_id="task_review", workflow_id="wf_1", cfg=config
        )
