"""The reconciler DERIVES each tenant's namespace; it does not trust the stored one.

`tenants/<id>.namespace` is a document a tenant's own worker can write today
(docs/design/tenant-settings-integrity.md §1.3 row 2). `ControlStore.
tenant_namespaces` used to hand the raw value on, and `GkeBackend.namespace_for`
kept any value inside `swarm-tenant-`, so tenant A recording
`swarm-tenant-B` moved what the reconciler reads, protects and attributes for A
into B's namespace. Phase 0 of that design (ISO-RECON; part of #1457): the
store returns `sanitize_name(<prefix><tenant id>)`, the dispatcher's own
naming, whatever is stored, and says so once per tenant as
`tenant_namespace_mismatch` -- ERROR, which the existing alert pages on, when
the stored value names another registered tenant's namespace or lies outside
the prefix; WARNING for any other rename inside it.

Offline: the worker suite's in-memory Firestore, no backend.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from reconciler import backends as reconciler_backends
from reconciler.backends import GkeBackend, sanitize_name
from reconciler.logs import build_logger
from reconciler.store import ControlStore

PREFIX = "swarm-tenant-"


@pytest.fixture(autouse=True)
def fresh_mismatch_log():
    """The once-per-tenant dedupe is process-wide; start every test with none used."""
    reconciler_backends._namespace_mismatch_due.clear()
    yield
    reconciler_backends._namespace_mismatch_due.clear()


def seed(db: Any, tenant: str, namespace: str | None = None) -> None:
    doc: dict[str, Any] = {"tenant_id": tenant, "kind": "group", "enabled": True}
    if namespace is not None:
        doc["namespace"] = namespace
    db.seed(f"tenants/{tenant}", doc)


def store_with_log(db: Any) -> tuple[ControlStore, io.StringIO]:
    stream = io.StringIO()
    return ControlStore(db, logger=build_logger(stream=stream)), stream


def mismatches(stream: io.StringIO) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    return [line for line in lines if line.get("event") == "tenant_namespace_mismatch"]


def test_a_stored_namespace_naming_another_tenant_yields_the_derived_one(db):
    """The row-2 forgery: tenant `eng` records tenant `finance`'s namespace."""
    seed(db, "finance", f"{PREFIX}finance")
    seed(db, "eng", f"{PREFIX}finance")
    store, stream = store_with_log(db)

    assert store.tenant_namespaces() == {
        "finance": f"{PREFIX}finance",
        "eng": f"{PREFIX}eng",
    }
    [entry] = mismatches(stream)
    assert entry["severity"] == "ERROR", entry
    assert (entry["tenant_id"], entry["stored"], entry["derived"], entry["used"]) == (
        "eng", f"{PREFIX}finance", f"{PREFIX}eng", f"{PREFIX}eng"
    ), entry
    assert entry["names_tenant"] == "finance", entry


def test_the_backend_then_reads_the_derived_namespace_for_that_tenant(db):
    """What `Reconciler._look_namespaced` does with the pair: one namespace each."""
    seed(db, "finance", f"{PREFIX}finance")
    seed(db, "eng", f"{PREFIX}finance")
    store, _ = store_with_log(db)
    backend = GkeBackend(namespace_prefix=PREFIX, batch_api=object(), core_api=object())

    wanted = {
        tenant: backend.namespace_for(tenant, recorded)
        for tenant, recorded in store.tenant_namespaces().items()
    }
    assert wanted == {"finance": f"{PREFIX}finance", "eng": f"{PREFIX}eng"}


@pytest.mark.parametrize(
    "stored, severity",
    [
        # Outside the prefix: the pre-2026-09-23 spelling tenants/u-bogdan carried.
        ("swarm-eng", "ERROR"),  # namespace-prefix-exempt: stale, on purpose
        # The bare prefix names no tenant at all.
        (PREFIX, "ERROR"),
        # Inside the prefix and no registered tenant's: a `-canary` rename. Still
        # not trusted, but a permitted configuration until the dispatcher derives
        # too, so it is counted rather than paged on.
        (f"{PREFIX}eng-canary", "WARNING"),
    ],
)
def test_any_other_stored_namespace_yields_the_derived_one(db, stored, severity):
    seed(db, "eng", stored)
    store, stream = store_with_log(db)

    assert store.tenant_namespaces() == {"eng": f"{PREFIX}eng"}
    [entry] = mismatches(stream)
    assert entry["severity"] == severity, entry
    assert (entry["stored"], entry["used"]) == (stored, f"{PREFIX}eng"), entry


def test_a_tenant_recording_nothing_or_its_own_namespace_is_derived_and_logs_nothing(db):
    seed(db, "eng", f"{PREFIX}eng")
    seed(db, "finance")
    store, stream = store_with_log(db)

    assert store.tenant_namespaces() == {
        "eng": f"{PREFIX}eng",
        "finance": f"{PREFIX}finance",
    }
    assert mismatches(stream) == []


def test_a_long_tenant_id_is_derived_through_the_dispatchers_sanitiser(db):
    """Past 63 characters the dispatcher truncates and hashes; the store must agree."""
    tenant = "u-" + "x" * 70
    seed(db, tenant, f"{PREFIX}finance")
    store, _ = store_with_log(db)

    assert store.tenant_namespaces() == {tenant: sanitize_name(f"{PREFIX}{tenant}")}


def test_the_mismatch_is_logged_once_per_tenant_however_many_passes_ask(db):
    """Every pass reads the tenants; the alert needs one entry, not one a minute."""
    seed(db, "eng", f"{PREFIX}finance")
    seed(db, "ops", "swarm-ops")  # namespace-prefix-exempt: stale, on purpose
    store, stream = store_with_log(db)

    for _ in range(5):
        store.tenant_namespaces()
    assert sorted(entry["tenant_id"] for entry in mismatches(stream)) == ["eng", "ops"]


def test_registered_tenants_still_lists_every_tenant(db):
    """The namespace GC's protection keys on registration, not on the stored value."""
    seed(db, "eng", f"{PREFIX}finance")
    seed(db, "finance")
    store, _ = store_with_log(db)

    assert store.registered_tenants() == {"eng", "finance"}
