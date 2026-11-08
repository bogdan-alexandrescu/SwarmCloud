"""The reconciler's garbage collection of pinned plugin copies (managed-plugins.md section 9.3).

A pinned copy, `platform/<sha256>.tar` or `tenants/<t>/<sha256>.tar` in the
plugins bucket, is deleted only when it is past the retention window and
nothing still names its digest: no install's current or candidate version, no
workspace default, no step that is not finished, and no attempt started inside
the window (its step's signed set and the worker's load record). Held here:

    an unreferenced copy past retention is deleted, by generation, and audited
    a copy named by an install, a candidate, a default or a recent attempt is kept
    a copy an unfinished step may still fetch is kept, however old
    a copy younger than the window is kept, referenced or not
    a bucket listing or a Firestore read that fails deletes nothing
    a copy rewritten since it was listed is not deleted and not audited
    a dry run deletes and audits nothing
    the pass runs the sweep once a day and reports it on the pass document
"""

from __future__ import annotations

import importlib.util
import io
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fakes import FakeFirestore, FakeTransactionRunner

from reconciler import plugingc
from reconciler.config import ReconcilerConfig
from reconciler.logs import build_logger
from reconciler.plugingc import (
    AUDIT_ACTION,
    PluginCollector,
    PluginGcConfig,
    PluginObject,
    PluginReferences,
)
from reconciler.repair import Reconciler
from reconciler.store import ControlStore

_SPEC = importlib.util.spec_from_file_location(
    "placeholders", Path(__file__).resolve().parents[2] / "placeholders.py")
placeholders = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(placeholders)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
TENANT = placeholders.TENANT
PROJECT = placeholders.PROJECT
REGION = placeholders.REGION


def digest() -> str:
    return secrets.token_hex(32)


class FakeObjects:
    """The plugins bucket: a listing with generations, and deletes by generation."""

    def __init__(self, *, fail_list: Exception | None = None) -> None:
        self.objects: dict[str, PluginObject] = {}
        self.deleted: list[tuple[str, int]] = []
        self.fail_list = fail_list
        #: name -> the generation it has by the time a delete arrives.
        self.rewritten: dict[str, int] = {}

    def put(self, name: str, *, created: datetime = OLD, generation: int = 1) -> str:
        self.objects[name] = PluginObject(name=name, generation=generation, created_at=created, size=1024)
        return name

    def list_objects(self, prefix: str) -> list[PluginObject]:
        if self.fail_list is not None:
            raise self.fail_list
        return [o for n, o in sorted(self.objects.items()) if n.startswith(prefix)]

    def delete(self, name: str, *, generation: int) -> str:
        if name not in self.objects:
            return plugingc.DELETE_GONE
        if self.rewritten.get(name, generation) != generation:
            return plugingc.DELETE_CHANGED
        del self.objects[name]
        self.deleted.append((name, generation))
        return plugingc.DELETE_DONE


def platform_object(sha: str) -> str:
    return f"platform/{sha}.tar"


def tenant_object(sha: str, tenant: str = TENANT) -> str:
    return f"tenants/{tenant}/{sha}.tar"


def seed_install(db: FakeFirestore, install_id: str, versions: dict[str, tuple[str, datetime]], *,
                 current: str, state: str = "active", scope: str = "platform") -> None:
    db.seed(f"plugin_installs/{install_id}", {
        "install_id": install_id, "scope": scope, "tenant_id": None if scope == "platform" else TENANT,
        "current_version": current, "state": state,
    })
    for version_id, (sha, created) in versions.items():
        db.seed(f"plugin_installs/{install_id}/versions/{version_id}", {
            "install_id": install_id, "version_id": version_id, "runnable_sha256": sha,
            "object": platform_object(sha) if scope == "platform" else tenant_object(sha),
            "created_at": created,
        })


def seed_task(db: FakeFirestore, task_id: str, *, state: str, signed: dict | None = None,
              record: dict | None = None) -> None:
    db.seed(f"tasks/{task_id}", {
        "id": task_id, "tenant_id": TENANT, "state": state,
        "metadata": {"dispatch": {"plugins": signed or {}}},
        "result_summary": {"plugins": record} if record is not None else None,
    })


def seed_attempt(db: FakeFirestore, attempt_id: str, task_id: str, *, created: datetime,
                 completed: datetime | None = None) -> None:
    db.seed(f"attempts/{attempt_id}", {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": TENANT,
        "created_at": created, "started_at": created, "completed_at": completed,
    })


def entry(sha: str, *, scope: str = "platform") -> dict:
    return {"name": "a-plugin", "scope": scope, "version_id": "v-" + secrets.token_hex(6),
            "runnable_sha256": sha,
            "object": platform_object(sha) if scope == "platform" else tenant_object(sha)}


def collector(db: FakeFirestore, objects: FakeObjects, *, dry_run: bool = False,
              stream: io.StringIO | None = None) -> PluginCollector:
    return PluginCollector(
        objects=objects,
        references=PluginReferences(db),
        db=db,
        config=PluginGcConfig(bucket="swarm-plugins-test", retention_days=7),
        logger=build_logger(stream=stream or io.StringIO()),
        dry_run=dry_run,
    )


def audit_rows(db: FakeFirestore) -> list[dict]:
    return [doc for path, doc in sorted(db.documents.items()) if path.startswith("admin_audit/")]


@pytest.fixture
def objects() -> FakeObjects:
    return FakeObjects()


# -- deleted and audited --------------------------------------------------------


def test_an_unreferenced_copy_past_retention_is_deleted_by_generation_and_audited(db, objects):
    sha = digest()
    name = objects.put(tenant_object(sha), generation=7)

    report = collector(db, objects).sweep(now=NOW)

    assert objects.deleted == [(name, 7)]
    assert report.ran and report.deleted == 1 and report.kept == 0
    (row,) = audit_rows(db)
    assert row["action"] == AUDIT_ACTION == "plugin_copy_deleted"
    assert row["by"] == "swarm-reconciler"
    assert row["at"] == NOW
    assert row["target"] == name
    assert row["target_tenant_id"] == TENANT
    assert row["detail"]["scope"] == "workspace"
    assert row["detail"]["runnable_sha256"] == sha
    assert row["detail"]["generation"] == 7
    assert row["detail"]["reason"] == "unreferenced_past_retention"
    assert report.as_dict()["deletions"] == [
        {"object": name, "runnable_sha256": sha, "scope": "workspace", "tenant_id": TENANT,
         "generation": 7, "audited": True}]


def test_a_superseded_version_and_a_removed_install_release_their_copies(db, objects):
    old, current, removed = digest(), digest(), digest()
    seed_install(db, "p-" + "a" * 12, {"v-" + "1" * 12: (old, OLD), "v-" + "2" * 12: (current, OLD + timedelta(days=1))},
                 current="v-" + "2" * 12)
    seed_install(db, "p-" + "b" * 12, {"v-" + "3" * 12: (removed, OLD)}, current="v-" + "3" * 12, state="removed")
    for sha in (old, current, removed):
        objects.put(platform_object(sha))

    collector(db, objects).sweep(now=NOW)

    assert sorted(n for n, _ in objects.deleted) == sorted([platform_object(old), platform_object(removed)])
    assert platform_object(current) in objects.objects


# -- kept -----------------------------------------------------------------------


def test_a_copy_named_by_an_install_a_candidate_a_default_or_a_recent_attempt_is_kept(db, objects):
    current, candidate, defaulted, loaded, signed_recent = (digest() for _ in range(5))
    seed_install(db, "p-" + "a" * 12, {"v-" + "1" * 12: (current, OLD),
                                       "v-" + "2" * 12: (candidate, OLD + timedelta(days=2))},
                 current="v-" + "1" * 12)
    # A removed install a workspace still names as a default keeps its current copy.
    seed_install(db, "w-" + "c" * 12, {"v-" + "4" * 12: (defaulted, OLD)}, current="v-" + "4" * 12,
                 state="removed", scope="workspace")
    db.seed(f"workspace_settings/{TENANT}", {"plugins": {"allowed": [], "defaults": ["w-" + "c" * 12]}})
    # A finished step whose attempt started inside the window: its load record
    # and its signed set each name a copy.
    seed_task(db, "task_recent", state="SUCCEEDED", signed={"p-" + "d" * 12: entry(signed_recent)},
              record={"p-" + "e" * 12: {"managed": True, "runnable_sha256": loaded}})
    seed_attempt(db, "att_recent", "task_recent", created=NOW - timedelta(days=2), completed=NOW - timedelta(days=2))
    objects.put(platform_object(current))
    objects.put(platform_object(candidate))
    objects.put(tenant_object(defaulted))
    objects.put(platform_object(loaded))
    objects.put(platform_object(signed_recent))

    report = collector(db, objects).sweep(now=NOW)

    assert objects.deleted == []
    assert report.kept == 5 and report.deleted == 0
    assert audit_rows(db) == []


def test_a_copy_an_unfinished_step_may_still_fetch_is_kept_however_old(db, objects):
    queued, parked, running = digest(), digest(), digest()
    seed_task(db, "task_queued", state="QUEUED", signed={"p-" + "a" * 12: entry(queued)})
    seed_task(db, "task_parked", state="PARKED", signed={"w-" + "b" * 12: entry(parked, scope="workspace")})
    seed_task(db, "task_running", state="RUNNING", signed={"p-" + "c" * 12: entry(running)})
    # Its attempt began long before the window: only the unfinished state keeps it.
    seed_attempt(db, "att_running", "task_running", created=OLD)
    objects.put(platform_object(queued))
    objects.put(tenant_object(parked))
    objects.put(platform_object(running))

    collector(db, objects).sweep(now=NOW)

    assert objects.deleted == []


def test_an_old_finished_step_does_not_keep_a_copy(db, objects):
    sha = digest()
    seed_task(db, "task_old", state="SUCCEEDED", signed={"p-" + "a" * 12: entry(sha)},
              record={"p-" + "a" * 12: {"managed": True, "runnable_sha256": sha}})
    seed_attempt(db, "att_old", "task_old", created=OLD, completed=OLD)
    objects.put(platform_object(sha))

    collector(db, objects).sweep(now=NOW)

    assert objects.deleted == [(platform_object(sha), 1)]


def test_a_copy_younger_than_the_window_is_kept(db, objects):
    objects.put(platform_object(digest()), created=NOW - timedelta(days=6))

    report = collector(db, objects).sweep(now=NOW)

    assert objects.deleted == [] and report.kept == 1


def test_objects_outside_the_pinned_layout_are_never_touched(db, objects):
    objects.put("platform/readme.txt")
    objects.put(f"tenants/{TENANT}/nested/{digest()}.tar")
    objects.put(f"tenants/{digest()}.tar")

    report = collector(db, objects).sweep(now=NOW)

    assert objects.deleted == [] and report.examined == 0


# -- a read that fails deletes nothing -------------------------------------------


def test_a_bucket_read_that_fails_deletes_nothing(db):
    objects = FakeObjects(fail_list=RuntimeError("listing refused"))

    report = collector(db, objects).sweep(now=NOW)

    assert not report.ran
    assert objects.deleted == [] and audit_rows(db) == []
    assert report.errors and "listing" in report.errors[0]


class _FailingQuery:
    def where(self, *args, **kwargs):
        return self

    def stream(self, **kwargs):
        raise RuntimeError("firestore unavailable")


class _ReadFails(FakeFirestore):
    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    def collection(self, name):
        return _FailingQuery() if name == self.failing else super().collection(name)


@pytest.mark.parametrize("failing", ["plugin_installs", "workspace_settings", "tasks", "attempts"])
def test_a_reference_read_that_fails_deletes_nothing(objects, failing):
    db = _ReadFails(failing)
    objects.put(platform_object(digest()))

    report = collector(db, objects).sweep(now=NOW)

    assert not report.ran
    assert objects.deleted == [] and audit_rows(db) == []
    assert report.errors


def test_a_copy_rewritten_since_the_listing_is_not_deleted_or_audited(db, objects):
    name = objects.put(platform_object(digest()), generation=3)
    objects.rewritten[name] = 4

    report = collector(db, objects).sweep(now=NOW)

    assert objects.deleted == [] and audit_rows(db) == []
    assert report.changed == 1 and report.deleted == 0


def test_a_reference_that_appears_before_the_delete_keeps_the_copy(db, objects):
    """The references are read again just before deleting, so an intake that
    pinned the same bytes while the sweep ran keeps them."""
    sha = digest()
    objects.put(platform_object(sha))
    references = PluginReferences(db)
    first = references.read(now=NOW, retention=timedelta(days=7))
    seed_install(db, "p-" + "f" * 12, {"v-" + "9" * 12: (sha, NOW)}, current="v-" + "9" * 12)

    class Once(PluginReferences):
        calls = 0

        def read(self, *, now, retention):
            Once.calls += 1
            return first if Once.calls == 1 else super().read(now=now, retention=retention)

    sweep = PluginCollector(objects=objects, references=Once(db), db=db,
                            config=PluginGcConfig(bucket="swarm-plugins-test", retention_days=7),
                            logger=build_logger(stream=io.StringIO()), dry_run=False)
    sweep.sweep(now=NOW)

    assert objects.deleted == []


def test_a_dry_run_deletes_and_audits_nothing(db, objects):
    objects.put(platform_object(digest()))

    report = collector(db, objects, dry_run=True).sweep(now=NOW)

    assert objects.deleted == [] and audit_rows(db) == []
    assert report.deletable == 1 and report.deleted == 0


# -- the configuration ------------------------------------------------------------


def test_the_bucket_defaults_to_the_name_terraform_gives_it():
    project = PROJECT
    assert PluginGcConfig.from_env(project, environ={}).bucket == f"{project}-swarm-plugins"
    named = PluginGcConfig.from_env(project, environ={"SWARM_PLUGINS_BUCKET": "swarm-plugins-named"})
    assert named.bucket == "swarm-plugins-named"
    off = PluginGcConfig.from_env(project, environ={"RECONCILER_ENABLE_PLUGIN_GC": "false"})
    assert off.enabled is False
    assert PluginGcConfig.from_env(project, environ={}).retention_days == 7


# -- registration in the pass -----------------------------------------------------


def _reconciler(db: FakeFirestore, objects: FakeObjects, *, dry_run: bool = False) -> Reconciler:
    logger = build_logger(stream=io.StringIO())
    config = ReconcilerConfig(project_id=PROJECT, region=REGION,
                              firestore_database="swarm", enable_gke=False, dry_run=dry_run)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    plugins = PluginCollector(
        objects=objects, references=PluginReferences(db), db=db,
        config=PluginGcConfig(bucket="swarm-plugins-test", retention_days=7, interval_seconds=86400),
        logger=logger, dry_run=dry_run, txn_runner=FakeTransactionRunner(db))
    return Reconciler(store=store, backends=[], config=config, logger=logger,
                      hold_releaser=None, finish_announcer=None, plugin_gc=plugins)


def test_the_pass_runs_the_sweep_once_a_day_and_records_it_on_the_pass(db, objects):
    sha = digest()
    objects.put(platform_object(sha), created=datetime(2020, 1, 1, tzinfo=timezone.utc))
    reconciler = _reconciler(db, objects)

    first = reconciler.run_once()
    objects.put(platform_object(digest()), created=datetime(2020, 1, 1, tzinfo=timezone.utc))
    second = reconciler.run_once()

    assert first.plugin_gc is not None and first.plugin_gc["deleted"] == 1
    assert second.plugin_gc is None            # claimed for the day already
    assert len(objects.deleted) == 1
    (row,) = [o for o in first.outcomes if o.kind == "plugin_copy_deleted"]
    assert row.deleted == platform_object(sha)
    passes = [doc for path, doc in db.documents.items() if path.startswith("reconciler_passes/")]
    assert any((p.get("plugin_gc") or {}).get("deleted") == 1 for p in passes)
    assert db.doc("reconciler_state/plugin_gc")["last_started_at"] is not None


def test_a_reconciler_without_a_plugin_collector_reports_no_sweep(db):
    logger = build_logger(stream=io.StringIO())
    config = ReconcilerConfig(project_id=PROJECT, region=REGION,
                              firestore_database="swarm", enable_gke=False)
    store = ControlStore(db, logger=logger, txn_runner=FakeTransactionRunner(db))
    report = Reconciler(store=store, backends=[], config=config, logger=logger,
                        hold_releaser=None, finish_announcer=None).run_once()
    assert report.plugin_gc is None
    assert report.as_dict()["plugin_gc"] is None
