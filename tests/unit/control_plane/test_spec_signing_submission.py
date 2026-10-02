"""swarm-api signs every step spec it creates (contract request 34, section 4).

WHAT IS ASSERTED, AS PROPERTIES OF THE STORED DOCUMENT. Every task from
`submit_tasks` and `submit_workflow` carries the three fields, and its
signature verifies over its STORED form -- the document the worker will
fetch, not the object swarm-api held. That includes the task-id-keyed
`metadata.input_from` and the upstream step's `metadata.expected_outputs`,
both written after `_build_task`, so a signature made inside `_build_task`
fails here. A signing failure is 503 and nothing is stored; a value with no
canonical form is 422 `invalid_input` and the signer is never called.

The signer is a local P-256 key behind swarm-api's own interface
(`spec_signer.LocalSpecSigner`); nothing here reaches KMS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import UpstreamUnavailable, ValidationFailed
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.specsigning import KmsSpecSigner, SpecSignature, sign_task_specs
from swarm_api.waker import NullWaker
from swarm_common.models import Task
from swarm_common.states import TaskState

from .conftest import PROJECT, api_settings, auth_header
from .spec_signer import KEY_VERSION, LocalSpecSigner


@pytest.fixture
def signer() -> LocalSpecSigner:
    return LocalSpecSigner()


@pytest.fixture
def signed_context(db, tokens, group_map, signer):
    return build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=signer,
    )


@pytest.fixture
def signed_client(signed_context) -> TestClient:
    return TestClient(create_app(signed_context), raise_server_exceptions=False)


def _tasks(db) -> dict[str, dict[str, Any]]:
    return {
        key.split("/", 1)[1]: doc
        for key, doc in db.docs.items()
        if key.startswith("tasks/") and key.count("/") == 1
    }


def _workflows(db) -> list[str]:
    return [key for key in db.docs if key.startswith("workflows/")]


# ---------------------------------------------------------------------------
# Every created task is signed, over what was stored
# ---------------------------------------------------------------------------


def test_a_submitted_task_is_signed_over_its_stored_form(signed_client, db, signer):
    body = {"runner_profile": "mock", "input": {"prompt": "hello"}, "metadata": {"label": "x"}}
    response = signed_client.post("/v1/tasks", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 1
    (task_id, doc), = tasks.items()
    assert doc["spec_key_version"] == KEY_VERSION
    # Contract request 42: a task that names no parent is signed at format 1,
    # so a worker built before format 2 keeps running it; only a child is
    # signed at format 2 (tests/unit/control_plane/test_child_tasks_api.py).
    assert doc["spec_format"] == 1
    assert isinstance(doc["spec_signature"], str) and doc["spec_signature"]
    assert signer.verifies(doc, task_id), "the stored document does not verify"
    assert len(signer.signed) == 1


def test_a_batch_signs_each_task_by_its_own_id(signed_client, db, signer):
    body = [{"runner_profile": "mock", "input": {"prompt": f"n{i}"}} for i in range(3)]
    response = signed_client.post("/v1/tasks/batch", headers=auth_header("alice"), json={"tasks": body})
    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 3
    for task_id, doc in tasks.items():
        assert signer.verifies(doc, task_id), task_id
    ids = list(tasks)
    # A signature is bound to its own task id: none verifies as another's.
    assert not signer.verifies(tasks[ids[0]], ids[1])


def _workflow_body() -> dict[str, Any]:
    return {
        "on_step_failure": "continue",
        "strategy": "direct-pr",
        "repository_url": "https://github.com/acme/widgets.git",
        "steps": [
            {"step_id": "impl", "runner_profile": "mock", "input": {"prompt": "implement"}},
            {
                "step_id": "review",
                "runner_profile": "mock",
                "input": {"prompt": "review"},
                "depends_on": ["impl"],
                "input_from": {"impl": "summary.md"},
            },
        ],
    }


def test_every_workflow_step_is_signed_after_input_from_and_expected_outputs(
    signed_client, db, signer
):
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow_body())
    assert response.status_code == 201, response.text
    tasks = _tasks(db)
    assert len(tasks) == 2
    by_step = {doc["step_id"]: (task_id, doc) for task_id, doc in tasks.items()}
    impl_id, impl = by_step["impl"]
    review_id, review = by_step["review"]
    # The fields written AFTER `_build_task` are in the stored documents...
    assert review["metadata"]["input_from"] == {impl_id: "summary.md"}
    assert impl["metadata"]["expected_outputs"] == ["summary.md"]
    # ...and inside what the signatures cover.
    for task_id, doc in tasks.items():
        assert signer.verifies(doc, task_id), doc["step_id"]
    # A signature made before either was written would not verify.
    stale = dict(review)
    stale["metadata"] = {k: v for k, v in review["metadata"].items() if k != "input_from"}
    assert not signer.verifies(stale, review_id)
    stale = dict(impl)
    stale["metadata"] = {k: v for k, v in impl["metadata"].items() if k != "expected_outputs"}
    assert not signer.verifies(stale, impl_id)
    assert len(signer.signed) == 2, "one sign call per step"


def test_a_continuation_shaped_submission_is_signed_by_the_same_path(signed_client, db, signer):
    """Section 7: a continuation is a new `direct-pr` workflow submitted through
    `submit_workflow`, so its task is signed at its own submission, its
    dispatch block inside the signature. No special case exists or is needed:
    this is the same path as any workflow, asserted on its dispatch block."""
    body = _workflow_body()
    body["steps"] = body["steps"][:1]
    response = signed_client.post("/v1/workflows", headers=auth_header("alice"), json=body)
    assert response.status_code == 201, response.text
    (task_id, doc), = _tasks(db).items()
    assert doc["metadata"]["dispatch"]["strategy"] == "direct-pr"
    assert signer.verifies(doc, task_id)
    rewritten = {**doc, "metadata": {**doc["metadata"], "dispatch": {**doc["metadata"]["dispatch"], "strategy": "none"}}}
    assert not signer.verifies(rewritten, task_id)


# ---------------------------------------------------------------------------
# Refusals: nothing signed, nothing stored
# ---------------------------------------------------------------------------


def test_a_signing_failure_is_503_and_nothing_is_stored(db, tokens, group_map):
    failing = LocalSpecSigner(fail_with=RuntimeError("KMS UNAVAILABLE"))
    context = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"), signer=failing,
    )
    client = TestClient(create_app(context), raise_server_exceptions=False)
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"), json={"runner_profile": "mock", "input": {"prompt": "x"}}
    )
    assert response.status_code == 503, response.text
    assert _tasks(db) == {}
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_workflow_body())
    assert response.status_code == 503, response.text
    assert _tasks(db) == {} and _workflows(db) == []


@pytest.mark.parametrize(
    "value", [2**53, -(2**53), "\ud800"], ids=["past-2^53", "below--2^53", "lone-surrogate"]
)
def test_a_value_with_no_canonical_form_is_422_and_never_signed(signed_client, db, signer, value):
    import json

    body = {"runner_profile": "generic", "input": {"prompt": "x", "planted": value}}
    # Sent as JSON text with the surrogate ESCAPED (json.dumps' default
    # ensure_ascii), which is valid JSON a client can send; httpx's own `json=`
    # encodes to UTF-8 and cannot carry a lone surrogate at all.
    response = signed_client.post(
        "/v1/tasks",
        headers={**auth_header("alice"), "content-type": "application/json"},
        content=json.dumps(body),
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_input", response.json()
    assert signer.signed == [], "KMS was called for a spec with no canonical form"
    assert _tasks(db) == {}


def _task(**extra: Any) -> Task:
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    return Task(
        id="task_1", tenant_id="eng", created_at=now, updated_at=now,
        state=TaskState.READY, runner_profile="generic", resource_class="standard",
        input={"prompt": "x", **extra}, submitted_by="alice@saga.xyz",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 2**63 - 1])
def test_every_non_canonical_value_is_refused_before_any_sign_call(value):
    signer = LocalSpecSigner()
    good, bad = _task(), _task(planted=value)
    bad.id = "task_2"
    with pytest.raises(ValidationFailed) as exc:
        sign_task_specs([good, bad], signer)
    assert exc.value.code == "invalid_input"
    assert signer.signed == [], "the good task was signed before the bad one was refused"
    assert good.spec_signature is None


def test_an_unconfigured_signer_leaves_tasks_unsigned_only_outside_hardened():
    """No signer is local development only; `build_context` refuses a hardened
    environment without SPEC_SIGNING_KEY_VERSION (see its own test below)."""
    task = _task()
    sign_task_specs([task], None)
    assert task.spec_signature is None and task.spec_key_version is None


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_a_declared_deployed_environment_without_a_signing_key_refuses_to_start(environment):
    """#353 security review, M2: `hardened` is False for dev, and dev is the
    environment that is deployed. A declared dev swarm-api with no key must
    refuse to start, not write unsigned tasks every enforcing worker refuses."""
    import dataclasses

    from swarm_api.specsigning import signer_from_settings

    from .conftest import core_settings

    settings = dataclasses.replace(
        api_settings(core=core_settings(environment=environment), spec_signing_key_version=""),
        environment_declared=True,
    )
    with pytest.raises(ValueError, match="SPEC_SIGNING_KEY_VERSION"):
        signer_from_settings(settings)


@pytest.mark.parametrize(
    "environment,declared",
    [("test", True), ("local", True), ("dev", False), ("test", False)],
)
def test_local_development_and_tests_may_run_without_a_signing_key(environment, declared):
    """Undeclared reads as the frozen default "dev": that is local development."""
    import dataclasses

    from swarm_api.specsigning import signer_from_settings

    from .conftest import core_settings

    settings = dataclasses.replace(
        api_settings(core=core_settings(environment=environment), spec_signing_key_version=""),
        environment_declared=declared,
    )
    assert signer_from_settings(settings) is None


def test_a_declared_dev_with_a_signing_key_gets_the_kms_signer():
    import dataclasses

    from swarm_api.specsigning import KmsSpecSigner, signer_from_settings

    from .conftest import core_settings

    version = (
        "projects/p/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec/"
        "cryptoKeyVersions/1"
    )
    settings = dataclasses.replace(
        api_settings(core=core_settings(environment="dev"), spec_signing_key_version=version),
        environment_declared=True,
    )
    assert isinstance(signer_from_settings(settings), KmsSpecSigner)


def test_a_hardened_environment_without_a_signing_key_refuses_to_start(db, tokens, group_map):
    from .conftest import core_settings

    settings = api_settings(core=core_settings(environment="prod"), spec_signing_key_version="")
    with pytest.raises(ValueError, match="SPEC_SIGNING_KEY_VERSION"):
        build_context(
            settings=settings, db=db, verifier=StaticTokenVerifier(tokens),
            groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
            waker=NullWaker(), metrics=ApiMetrics(),
            objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        )


# ---------------------------------------------------------------------------
# The KMS signer checks what KMS answered
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, *, signature: bytes, name: str, verified: bool, crc: int | None = None):
        import google_crc32c  # noqa: PLC0415

        self.signature = signature
        self.name = name
        self.verified_digest_crc32c = verified
        self.signature_crc32c = crc if crc is not None else int(google_crc32c.value(signature))


class _Kms:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def asymmetric_sign(self, request: dict[str, Any], **_: Any) -> _Response:
        self.requests.append(request)
        return self.response


def test_the_kms_signer_sends_the_digest_and_its_crc_and_checks_the_answer():
    import google_crc32c  # noqa: PLC0415

    digest = b"\x01" * 32
    kms = _Kms(_Response(signature=b"sig", name=KEY_VERSION, verified=True))
    got = KmsSpecSigner(KEY_VERSION, client=kms).sign(digest)
    assert got == SpecSignature(signature=b"sig", key_version=KEY_VERSION)
    request = kms.requests[0]
    assert request["name"] == KEY_VERSION
    assert request["digest"] == {"sha256": digest}
    assert int(request["digest_crc32c"]) == int(google_crc32c.value(digest))


def test_the_kms_signer_refuses_an_unverified_digest():
    kms = _Kms(_Response(signature=b"sig", name=KEY_VERSION, verified=False))
    with pytest.raises(UpstreamUnavailable):
        KmsSpecSigner(KEY_VERSION, client=kms).sign(b"\x01" * 32)


def test_the_kms_signer_refuses_another_versions_answer():
    kms = _Kms(_Response(signature=b"sig", name=KEY_VERSION.replace("/1", "/2"), verified=True))
    with pytest.raises(UpstreamUnavailable):
        KmsSpecSigner(KEY_VERSION, client=kms).sign(b"\x01" * 32)


def test_the_kms_signer_refuses_a_corrupted_signature():
    kms = _Kms(_Response(signature=b"sig", name=KEY_VERSION, verified=True, crc=12345))
    with pytest.raises(UpstreamUnavailable):
        KmsSpecSigner(KEY_VERSION, client=kms).sign(b"\x01" * 32)
