"""A spec-check refusal copies no document value unbounded (#346).

`spec_format` and `spec_key_version` are fields of a task document the tenant
can write. A refusal copies them into `result_summary.spec_check` (the
error's `spec_check()`) and into its message, which the lifecycle logs. Before
`specverify._bounded`, a 1 MB `spec_format` became a 1 MB `detail` and a 1 MB
`key_version` became a 1 MB field of the task's own result summary.

MUTATION: replace `_bounded(version)` with `str(version)` or `version` at
either `SpecSignatureInvalid` that takes a document value in
`specverify._verify_signed`, or drop `_bounded` around the format's repr; the
matching test below fails on length.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import spec_keys
from agent_worker.config import WorkerConfig
from agent_worker.specverify import (
    DOCUMENT_VALUE_LIMIT,
    SpecSignatureInvalid,
    _bounded,
    verify_step_spec,
)
from swarm_common.profiles import RUNNER_PROFILES
from worker_seeds import TENANT

TASK = "task_1"
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
ONE_MB = 1024 * 1024
#: The longest a refusal's message may grow from one bounded value: the
#: reason, the task id and the fixed words around them are well under this.
MESSAGE_SLACK = 200


def _config() -> WorkerConfig:
    return WorkerConfig(
        task_id=TASK, attempt_id="att_1", lease_id="lease_1", tenant_id=TENANT,
        generation=3, runner_profile="browser", project_id="p", region="us-central1",
        firestore_database="swarm", artifact_bucket="b",
        spec_signing_key=spec_keys.SIGNING_KEY, spec_verify_keys=dict(spec_keys.VERIFY_KEYS),
    )


def _signed_doc() -> dict[str, Any]:
    profile = RUNNER_PROFILES["browser"]
    doc = {
        "id": TASK, "tenant_id": TENANT, "runner_profile": "browser",
        "resource_class": profile.resource_class, "timeout_seconds": profile.timeout_seconds,
        "max_attempts": 3, "provider": profile.provider, "input": {"prompt": "p"},
        "submitted_by": "alice@saga.xyz", "depends_on": [], "metadata": {},
    }
    return spec_keys.sign_document(doc, TASK)


def _refusal(doc: dict[str, Any]) -> SpecSignatureInvalid:
    with pytest.raises(SpecSignatureInvalid) as exc:
        verify_step_spec(doc, create_time=None, cfg=_config(), now=NOW)
    return exc.value


def _assert_bounded(err: SpecSignatureInvalid) -> None:
    for name, value in err.spec_check().items():
        assert value is None or len(value) <= DOCUMENT_VALUE_LIMIT, (name, len(value))
    assert len(err.detail) <= DOCUMENT_VALUE_LIMIT + len("format "), len(err.detail)
    assert len(str(err)) <= 2 * DOCUMENT_VALUE_LIMIT + MESSAGE_SLACK, len(str(err))


def test_a_megabyte_format_and_key_version_are_cut_in_an_unknown_format_refusal():
    doc = _signed_doc()
    doc["spec_format"] = "f" * ONE_MB
    doc["spec_key_version"] = "v" * ONE_MB

    err = _refusal(doc)

    assert err.reason == "unknown_format"
    _assert_bounded(err)
    assert err.key_version.endswith("[truncated]")
    assert err.detail.endswith("[truncated]")


def test_a_megabyte_key_version_is_cut_in_a_foreign_key_version_refusal():
    doc = _signed_doc()
    doc["spec_key_version"] = spec_keys.SIGNING_KEY + "/" + "v" * ONE_MB

    err = _refusal(doc)

    assert err.reason == "foreign_key_version"
    _assert_bounded(err)
    assert err.key_version.endswith("[truncated]")


def test_a_non_string_format_is_cut_by_its_repr():
    """Also the regression for an unhashable format: `in KNOWN_FORMATS` raised
    TypeError on a list, an uncaught crash rather than a refusal."""
    doc = _signed_doc()
    doc["spec_format"] = ["x" * 1000] * 1000

    err = _refusal(doc)

    assert err.reason == "unknown_format"
    _assert_bounded(err)


def test_a_legitimate_key_version_is_never_cut():
    """The longest real one -- a 30-character project, the longest region,
    `swarm-staging-specs` -- is about 150 characters; a refusal that cut its
    version number would hide which key was asked for."""
    longest = (
        "projects/" + "p" * 30 + "/locations/northamerica-northeast1"
        "/keyRings/swarm-staging-specs/cryptoKeys/step-spec/cryptoKeyVersions/12345"
    )
    assert _bounded(longest) == longest
    assert _bounded("x" * DOCUMENT_VALUE_LIMIT) == "x" * DOCUMENT_VALUE_LIMIT
    assert len(_bounded("x" * (DOCUMENT_VALUE_LIMIT + 1))) == DOCUMENT_VALUE_LIMIT
