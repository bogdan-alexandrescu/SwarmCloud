"""`Task.forge_credential` and `Task.forge_access` (contract request 54, request E).

Two optional fields, written by swarm-api only, that name the forge secret a
task's worker reads and whether it may write with it. Held here to: the shape
the field promises (a suffix `Tenant.secret_name` places under the task's own
tenant, never anything else), None as today's behaviour, and the signer
choosing format 3 exactly when either is set.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_common import specsign
from swarm_common.models import FORGE_ACCESS, FORGE_CREDENTIAL, Task, Tenant
from swarm_common.states import TaskState

HEX = "0123456789abcdef"


def _task(**overrides) -> Task:
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    fields = dict(
        id="task_0123456789abcdef",
        tenant_id="eng",
        created_at=now,
        updated_at=now,
        state=TaskState.QUEUED,
        runner_profile="claude-code",
        resource_class="standard",
        input={"prompt": "x"},
        submitted_by="alice@saga.xyz",
    )
    fields.update(overrides)
    return Task(**fields)


def test_both_default_to_none_and_a_task_without_them_signs_at_its_old_format():
    task = _task()
    assert task.forge_credential is None and task.forge_access is None
    stored = task.to_firestore()
    assert stored["forge_credential"] is None and stored["forge_access"] is None
    assert specsign.signing_format(stored) == 1


@pytest.mark.parametrize("suffix", ["git", "git-u-" + HEX, "git-r-" + HEX])
def test_a_provider_suffix_is_accepted_and_names_the_tasks_own_tenants_secret(suffix):
    task = _task(forge_credential=suffix, forge_access="read")
    tenant = Tenant(
        tenant_id=task.tenant_id, kind="group", principal="eng@saga.xyz",
        created_at=task.created_at,
    )
    assert tenant.secret_name(task.forge_credential) == f"swarm-tenant-eng-{suffix}"
    assert specsign.signing_format(task.to_firestore()) == 3


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "git-u-",
        "git-u-" + HEX[:-1],
        "git-u-" + HEX + "0",
        "git-u-" + HEX.upper(),
        "git-x-" + HEX,
        "git-u-" + HEX + "-refresh",
        "anthropic",
        "swarm-tenant-other-git",
        "git\n",
        3,
    ],
)
def test_any_other_credential_is_refused_at_construction(suffix):
    with pytest.raises(ValueError):
        _task(forge_credential=suffix)


@pytest.mark.parametrize("mode", ["", "Write", "admin", "push", 1])
def test_any_other_access_is_refused_at_construction(mode):
    with pytest.raises(ValueError):
        _task(forge_access=mode)


def test_the_vocabulary():
    assert FORGE_ACCESS == ("write", "read")
    assert FORGE_CREDENTIAL.fullmatch("git")
    for mode in FORGE_ACCESS:
        assert _task(forge_access=mode).forge_access == mode


@pytest.mark.parametrize(
    "stored,expected",
    [
        ({}, (None, None)),
        ({"forge_credential": "git-u-" + HEX, "forge_access": "read"}, ("git-u-" + HEX, "read")),
        ({"forge_credential": "git-u-" + HEX + "-refresh", "forge_access": "admin"}, (None, None)),
        ({"forge_credential": 7, "forge_access": True}, (None, None)),
    ],
    ids=["absent", "valid", "malformed", "wrong-type"],
)
def test_swarm_apis_decoder_reads_them_back_and_never_fails_on_a_malformed_one(stored, expected):
    """A tenant's agent can write its task documents; one bad field must not
    make the task unreadable to swarm-api, which acts on neither field."""
    from swarm_api.codec import task_from_dict

    doc = {**_task().to_firestore(), **stored}
    for key in ("forge_credential", "forge_access"):
        doc.setdefault(key, None)
    if not stored:
        del doc["forge_credential"], doc["forge_access"]
    task = task_from_dict(doc)
    assert (task.forge_credential, task.forge_access) == expected
