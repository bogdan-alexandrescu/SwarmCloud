"""`submitted_by` is a REQUIRED keyword on the four task/workflow reads.

Contract request 30, round 3 of re-review. A continuation-scoped caller (a
listed service account) may read only what IT submitted, and the filter that
enforces that lives once, in `Store`. With a default of `None` a layer that
never learned about the filter -- the artifact service, the inspection
service, a helper written next year -- would keep compiling and keep returning
the task, silently, to the caller the filter exists to refuse. Without a
default that shape is a `TypeError` at the call.

This file proves the SIGNATURE, independent of any route, so it stays red even
if every route-level test were satisfied by an accident of fixture setup.
"""

from __future__ import annotations

import pytest

from swarm_api.store import Store

from .conftest import seed_task


@pytest.fixture
def store(db) -> Store:
    seed_task(db, task_id="task_1", tenant_id="eng")
    return Store(db)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda s: s.get_task("eng", "task_1"), id="get_task"),
        pytest.param(lambda s: s.list_tasks("eng"), id="list_tasks"),
        pytest.param(lambda s: s.get_workflow("eng", "wf_1"), id="get_workflow"),
        pytest.param(lambda s: s.list_workflows("eng"), id="list_workflows"),
    ],
)
def test_omitting_submitted_by_is_a_type_error(store, call):
    with pytest.raises(TypeError, match="submitted_by"):
        call(store)


def test_submitted_by_none_is_the_unfiltered_read(store, db):
    db.docs["tasks/task_1"]["submitted_by"] = "alice@saga.xyz"
    assert store.get_task("eng", "task_1", submitted_by=None).id == "task_1"
    assert [t.id for t in store.list_tasks("eng", submitted_by=None).items] == ["task_1"]


def test_a_named_submitter_reads_only_its_own_task(store, db):
    db.docs["tasks/task_1"]["submitted_by"] = "alice@saga.xyz"
    assert store.get_task("eng", "task_1", submitted_by="alice@saga.xyz").id == "task_1"
    assert [t.id for t in store.list_tasks("eng", submitted_by="alice@saga.xyz").items] == [
        "task_1"
    ]
    assert store.list_tasks("eng", submitted_by="bot@p.iam.gserviceaccount.com").items == []


def test_not_yours_reads_exactly_like_does_not_exist(store, db):
    """The same words as the cross-tenant refusal, so a filtered caller cannot
    tell "not yours" from "not in your tenant" from "no such task"."""
    from swarm_api.errors import NotFound

    db.docs["tasks/task_1"]["submitted_by"] = "alice@saga.xyz"
    with pytest.raises(NotFound) as not_yours:
        store.get_task("eng", "task_1", submitted_by="bot@p.iam.gserviceaccount.com")
    with pytest.raises(NotFound) as other_tenant:
        store.get_task("research", "task_1", submitted_by=None)
    assert str(not_yours.value) == str(other_tenant.value)
