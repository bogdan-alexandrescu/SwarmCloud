"""Garbage collection of pinned plugin copies (docs/design/managed-plugins.md section 9.3; lane 11).

Every managed plugin version is one tarball in the plugins bucket, named by its
sha256: `platform/<sha256>.tar` for a platform install and
`tenants/<tenant>/<sha256>.tar` for a workspace's own (section 3). Nothing but
this module ever deletes one. A copy is deleted when it is older than the
retention window (7 days) and NOTHING still names its digest:

  * an install that is not removed, through its current version or a
    CANDIDATE (a version an update check pinned after the current one, which
    an admin may still apply);
  * any version written inside the window, whatever its install's state: an
    intake that pins bytes already in the bucket reuses the object, so a
    version that new may name a copy this sweep is looking at;
  * a workspace's "on for every step" default (`workspace_settings/{t}.plugins.
    defaults`), through that install's current version, even when the install
    is removed: removal clears the defaults, and until it has, a submission
    could still resolve one;
  * a step that is NOT FINISHED, through its signed plugin set
    (`metadata.dispatch.plugins`): a queued, parked or running step keeps the
    version it was signed with through every retry and resume (section 9.1),
    and its next attempt downloads the copy again (section 4.2) however long
    it waited. Age never releases these;
  * an attempt that started inside the window, through its step's signed set
    and the worker's load record (`result_summary.plugins`, section 4.5), which
    says which digests that step's attempts actually loaded.

WHY NO MAC IS CHECKED HERE. swarm-api acts on a catalogue record only when its
MAC verifies, because a forged record could swap a digest a step is signed
with. This reader only ever KEEPS on what it reads: a forged or unsigned
document can make a copy live longer, never make one go. So every document is
honoured as written, and the reconciler needs no settings key.

WHAT STOPS A DELETE. Any read that fails -- the bucket listing, or any one of
the Firestore reads above -- ends the sweep before anything is deleted: "I
could not read who names it" must never look like "nobody names it". The
references are read a second time just before the first delete, so a version
pinned while the sweep ran keeps its bytes. Each delete is conditioned on the
generation the listing saw (`ifGenerationMatch`), so an object rewritten since
is left alone. The bucket keeps a deleted copy as a noncurrent version for 7
days (terraform/infra/plugins.tf), so a wrong delete is recoverable.

THE AUDIT (section 10). Each deletion writes one `admin_audit` row,
`plugin_copy_deleted`, by `swarm-reconciler`, with the object as its target
and ids, digests and counts as its detail: never an upstream string (a
plugin's name or version is the archive's text). An object delete cannot share
a Firestore transaction, so the row is written right after the delete
succeeds; a row that cannot be written stops the sweep, so at most one
deletion is ever unaudited, and it is still on the pass document.

THE VIEW. The sweep's counters and one row per deletion ride on the pass
report (`ReconcileReport.plugin_gc` and its outcomes), which the reconciler
already persists to `reconciler_passes` and serves on `/readyz`: the
reconciler's existing read path, so no new route and no new reader.

THE CLOCK. Daily, claimed across instances in `reconciler_state/plugin_gc`
before the sweep runs (as the checkpoint sweep claims its hour), so a failing
sweep lists the bucket once a day, not on every one-minute tick. A dry run
claims nothing and deletes nothing, and still reports what it would delete.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from swarm_common.admission import _snapshot
from swarm_common.states import TERMINAL_STATES, TaskState

from .store import FirestoreTransactionRunner, _field_filter

#: The bucket's name when the deployment names one (terraform/infra
#: plugins.tf `plugins_bucket_name`); the worker and swarm-api read the same.
BUCKET_ENV = "SWARM_PLUGINS_BUCKET"
#: terraform/infra/plugins.tf's default name, `<project_id>-swarm-plugins`.
BUCKET_SUFFIX = "-swarm-plugins"
ENABLE_ENV = "RECONCILER_ENABLE_PLUGIN_GC"
RETENTION_ENV = "PLUGIN_GC_RETENTION_DAYS"
INTERVAL_ENV = "PLUGIN_GC_INTERVAL_SECONDS"
#: Section 9.3: "older than 7 days".
RETENTION_DAYS = 7
#: Section 9.3: "runs this daily".
INTERVAL_SECONDS = 24 * 3600

PREFIXES = ("platform/", "tenants/")
#: The pinned layout and nothing else; any other object is not this module's.
_PLATFORM_OBJECT = re.compile(r"^platform/([0-9a-f]{64})\.tar$")
_TENANT_OBJECT = re.compile(r"^tenants/([^/]+)/([0-9a-f]{64})\.tar$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

INSTALLS = "plugin_installs"
VERSIONS = "versions"
STATE_REMOVED = "removed"
#: swarm_api.plugins.COLLECTION / FIELD / pluginconfig.DEFAULTS.
SETTINGS = "workspace_settings"
SETTINGS_FIELD = "plugins"
DEFAULTS = "defaults"
#: `metadata.dispatch.plugins` (agent_worker.pluginload) and the attempt
#: summary's `plugins` (agent_worker.plugins.SUMMARY_KEY).
DISPATCH_KEY = "dispatch"
PLUGINS_KEY = "plugins"
#: swarm_api.admins.AUDIT_COLLECTION, where every catalogue change is audited.
AUDIT_COLLECTION = "admin_audit"
AUDIT_ACTION = "plugin_copy_deleted"
AUDIT_BY = "swarm-reconciler"
REASON = "unreferenced_past_retention"
SWEEP_STATE_PATH = ("reconciler_state", "plugin_gc")

DELETE_DONE = "deleted"
#: The object's generation is no longer the one listed (412).
DELETE_CHANGED = "changed"
#: Someone else deleted it first (404).
DELETE_GONE = "gone"

#: Every state a step can still start an attempt from (invariant 1's demand
#: states and the three that cost nothing alike).
UNFINISHED_STATES = sorted(s.value for s in TaskState if s not in TERMINAL_STATES)


@dataclass(frozen=True)
class PluginGcConfig:
    bucket: str
    enabled: bool = True
    retention_days: int = RETENTION_DAYS
    interval_seconds: int = INTERVAL_SECONDS

    @classmethod
    def from_env(cls, project_id: str, environ: Mapping[str, str] | None = None) -> "PluginGcConfig":
        env = os.environ if environ is None else environ
        named = (env.get(BUCKET_ENV) or "").strip()
        enabled = (env.get(ENABLE_ENV) or "true").strip().lower() not in ("0", "false", "no", "off")
        return cls(
            bucket=named or (f"{project_id}{BUCKET_SUFFIX}" if project_id else ""),
            enabled=enabled,
            retention_days=max(1, int(env.get(RETENTION_ENV) or RETENTION_DAYS)),
            interval_seconds=max(60, int(env.get(INTERVAL_ENV) or INTERVAL_SECONDS)),
        )


# --- the bucket ------------------------------------------------------------------


@dataclass(frozen=True)
class PluginObject:
    name: str
    generation: int
    created_at: datetime | None
    size: int | None = None


class PluginObjects(Protocol):
    def list_objects(self, prefix: str) -> list[PluginObject]: ...
    def delete(self, name: str, *, generation: int) -> str: ...


class GcsPluginObjects:
    """The plugins bucket. The client is imported lazily, so the unit tests
    never pull in grpc or authenticate to anything."""

    def __init__(self, bucket: str, project_id: str | None = None, client: Any = None) -> None:
        if not bucket:
            raise ValueError("a bucket is required to collect plugin copies")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client(project=self._project_id)
        return self._client

    def list_objects(self, prefix: str) -> list[PluginObject]:
        return [
            PluginObject(name=b.name, generation=int(b.generation), created_at=b.time_created, size=b.size)
            for b in self._get_client().list_blobs(self.bucket, prefix=prefix)
        ]

    def delete(self, name: str, *, generation: int) -> str:
        from google.api_core import exceptions

        try:
            self._get_client().bucket(self.bucket).delete_blob(name, if_generation_match=generation)
        except exceptions.PreconditionFailed:
            return DELETE_CHANGED
        except exceptions.NotFound:
            return DELETE_GONE
        return DELETE_DONE


@dataclass(frozen=True)
class PinnedCopy:
    object: PluginObject
    digest: str
    scope: str
    tenant_id: str | None


def parse_object(obj: PluginObject) -> PinnedCopy | None:
    """The copy a name holds, or None for anything outside the pinned layout."""
    found = _PLATFORM_OBJECT.match(obj.name)
    if found:
        return PinnedCopy(obj, found.group(1), "platform", None)
    found = _TENANT_OBJECT.match(obj.name)
    if found:
        return PinnedCopy(obj, found.group(2), "workspace", found.group(1))
    return None


# --- who names a digest ----------------------------------------------------------


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _digests_in(block: Any) -> set[str]:
    """Every digest a signed set or a load record names: per install, its
    `runnable_sha256` and the digest in its `object`'s name."""
    found: set[str] = set()
    if not isinstance(block, Mapping):
        return found
    for entry in block.values():
        if not isinstance(entry, Mapping):
            continue
        sha = entry.get("runnable_sha256")
        if isinstance(sha, str) and _SHA256.match(sha):
            found.add(sha)
        name = entry.get("object")
        if isinstance(name, str):
            pinned = parse_object(PluginObject(name=name, generation=0, created_at=None))
            if pinned is not None:
                found.add(pinned.digest)
    return found


def task_digests(task: Mapping[str, Any]) -> set[str]:
    """What a step's attempts may fetch (its signed set) and did load (the record)."""
    metadata = task.get("metadata")
    dispatch = metadata.get(DISPATCH_KEY) if isinstance(metadata, Mapping) else None
    signed = dispatch.get(PLUGINS_KEY) if isinstance(dispatch, Mapping) else None
    summary = task.get("result_summary")
    record = summary.get(PLUGINS_KEY) if isinstance(summary, Mapping) else None
    return _digests_in(signed) | _digests_in(record)


@dataclass
class References:
    #: digest -> the first reason found for keeping it.
    digests: dict[str, str] = field(default_factory=dict)

    def add(self, digest: Any, reason: str) -> None:
        if isinstance(digest, str) and _SHA256.match(digest):
            self.digests.setdefault(digest, reason)

    def add_all(self, digests: Iterable[str], reason: str) -> None:
        for digest in digests:
            self.add(digest, reason)


class PluginReferences:
    """Reads every document that can name a pinned copy. Any read that fails
    raises, and the caller deletes nothing."""

    def __init__(self, db: Any, *, filter_factory: Callable[[str, str, Any], Any] | None = None) -> None:
        self._db = db
        self._filter = filter_factory or _field_filter

    def read(self, *, now: datetime, retention: timedelta) -> References:
        cutoff = now - retention
        refs = References()
        current_digest = self._installs(refs, cutoff)
        self._defaults(refs, current_digest)
        self._unfinished(refs)
        self._recent_attempts(refs, cutoff)
        return refs

    def _installs(self, refs: References, cutoff: datetime) -> dict[str, str | None]:
        """Each install's current, candidate and recent versions; returns
        install id -> its current version's digest, for the defaults."""
        current_digest: dict[str, str | None] = {}
        for snap in self._db.collection(INSTALLS).stream():
            install = snap.to_dict() or {}
            install_id = snap.id
            versions = {v.id: (v.to_dict() or {}) for v in
                        self._db.collection(INSTALLS).document(install_id).collection(VERSIONS).stream()}
            current_id = install.get("current_version")
            current = versions.get(current_id) if isinstance(current_id, str) else None
            current_digest[install_id] = (current or {}).get("runnable_sha256")
            active = install.get("state") != STATE_REMOVED
            current_at = _as_datetime((current or {}).get("created_at"))
            for version_id, version in versions.items():
                created = _as_datetime(version.get("created_at"))
                digests = _digests_in({version_id: version})
                if created is None or created >= cutoff:
                    refs.add_all(digests, "version_recent")
                if not active:
                    continue
                if version_id == current_id:
                    refs.add_all(digests, "install_current")
                elif current is None or current_at is None or created is None or created > current_at:
                    # A candidate, or a version whose place against the
                    # current one cannot be told: kept either way.
                    refs.add_all(digests, "install_candidate")
        return current_digest

    def _defaults(self, refs: References, current_digest: Mapping[str, str | None]) -> None:
        for snap in self._db.collection(SETTINGS).stream():
            block = (snap.to_dict() or {}).get(SETTINGS_FIELD)
            names = block.get(DEFAULTS) if isinstance(block, Mapping) else None
            for install_id in names if isinstance(names, list) else ():
                if isinstance(install_id, str):
                    refs.add(current_digest.get(install_id), "workspace_default")

    def _unfinished(self, refs: References) -> None:
        query = self._db.collection("tasks").where(filter=self._filter("state", "in", UNFINISHED_STATES))
        for snap in query.stream():
            refs.add_all(task_digests(snap.to_dict() or {}), "step_unfinished")

    def _recent_attempts(self, refs: References, cutoff: datetime) -> None:
        task_ids: set[str] = set()
        for stamp in ("created_at", "started_at"):
            query = self._db.collection("attempts").where(filter=self._filter(stamp, ">=", cutoff))
            for snap in query.stream():
                task_id = (snap.to_dict() or {}).get("task_id")
                if isinstance(task_id, str) and task_id:
                    task_ids.add(task_id)
        for task_id in sorted(task_ids):
            snap = self._db.collection("tasks").document(task_id).get()
            if snap.exists:
                refs.add_all(task_digests(snap.to_dict() or {}), "attempt_recent")


# --- the sweep -------------------------------------------------------------------


@dataclass
class Deletion:
    object: str
    runnable_sha256: str
    scope: str
    tenant_id: str | None
    generation: int
    audited: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"object": self.object, "runnable_sha256": self.runnable_sha256, "scope": self.scope,
                "tenant_id": self.tenant_id, "generation": self.generation, "audited": self.audited}


@dataclass
class PluginSweepReport:
    ran: bool = False
    dry_run: bool = False
    listed: int = 0
    #: Pinned copies examined (objects in the layout).
    examined: int = 0
    kept: int = 0
    #: Decided deletable; equal to `deleted` on a real sweep that met no race.
    deletable: int = 0
    deleted: int = 0
    #: Rewritten since the listing, so left alone.
    changed: int = 0
    deletions: list[Deletion] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran, "dry_run": self.dry_run, "listed": self.listed, "examined": self.examined,
            "kept": self.kept, "deletable": self.deletable, "deleted": self.deleted, "changed": self.changed,
            "deletions": [d.as_dict() for d in self.deletions],
        }


class PluginCollector:
    """Lists the pinned copies, reads who names them, deletes what nobody does."""

    def __init__(
        self,
        *,
        objects: PluginObjects,
        references: PluginReferences,
        db: Any,
        config: PluginGcConfig,
        logger: Any,
        dry_run: bool = False,
        txn_runner: Any | None = None,
    ) -> None:
        self._objects = objects
        self._references = references
        self._db = db
        self._config = config
        self._log = logger
        self._dry_run = dry_run
        self._txn = txn_runner or FirestoreTransactionRunner(db)

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def claim(self, *, now: datetime) -> bool:
        """True at most once per `interval_seconds`, across all instances.
        Written before the sweep, so a failing sweep waits a full interval."""
        collection, document = SWEEP_STATE_PATH
        ref = self._db.collection(collection).document(document)
        interval = self._config.interval_seconds

        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            if snap.exists:
                last = _as_datetime((snap.to_dict() or {}).get("last_started_at"))
                if last is not None and (now - last).total_seconds() < interval:
                    return False
                txn.update(ref, {"last_started_at": now})
            else:
                txn.set(ref, {"last_started_at": now})
            return True

        return bool(self._txn.run(_apply))

    def sweep(self, *, now: datetime) -> PluginSweepReport:
        report = PluginSweepReport(dry_run=self._dry_run)
        retention = timedelta(days=self._config.retention_days)
        listed: list[PluginObject] = []
        try:
            for prefix in PREFIXES:
                listed.extend(self._objects.list_objects(prefix))
        except Exception as exc:
            report.errors.append(f"plugin_gc listing {self._config.bucket}: {type(exc).__name__}: {exc}")
            self._log.error("plugin sweep skipped: the plugins bucket could not be listed",
                            error=type(exc).__name__)
            return report
        try:
            refs = self._references.read(now=now, retention=retention)
        except Exception as exc:
            report.errors.append(f"plugin_gc references: {type(exc).__name__}: {exc}")
            self._log.error("plugin sweep skipped: who names a plugin copy could not be read",
                            error=type(exc).__name__)
            return report

        report.ran = True
        report.listed = len(listed)
        candidates: list[PinnedCopy] = []
        for obj in sorted(listed, key=lambda o: o.name):
            pinned = parse_object(obj)
            if pinned is None:
                continue
            report.examined += 1
            created = _as_datetime(obj.created_at)
            if created is None or now - created < retention or pinned.digest in refs.digests:
                report.kept += 1
                continue
            candidates.append(pinned)

        if candidates and not self._dry_run:
            # Again, just before deleting: a version pinned while this sweep
            # listed and read keeps its bytes.
            try:
                refs = self._references.read(now=now, retention=retention)
            except Exception as exc:
                report.errors.append(f"plugin_gc references: {type(exc).__name__}: {exc}")
                self._log.error("plugin sweep stopped: the second reference read failed",
                                error=type(exc).__name__)
                report.kept += len(candidates)
                return report
            still = [c for c in candidates if c.digest not in refs.digests]
            report.kept += len(candidates) - len(still)
            candidates = still

        report.deletable = len(candidates)
        for index, pinned in enumerate(candidates):
            deletion = Deletion(object=pinned.object.name, runnable_sha256=pinned.digest, scope=pinned.scope,
                                tenant_id=pinned.tenant_id, generation=pinned.object.generation)
            if self._dry_run:
                report.deletions.append(deletion)
                continue
            try:
                outcome = self._objects.delete(pinned.object.name, generation=pinned.object.generation)
            except Exception as exc:
                report.errors.append(f"plugin_gc delete {pinned.object.name}: {type(exc).__name__}: {exc}")
                self._log.error("plugin copy could not be deleted", object=pinned.object.name,
                                error=type(exc).__name__)
                continue
            if outcome == DELETE_CHANGED:
                report.changed += 1
                continue
            if outcome != DELETE_DONE:
                continue
            report.deleted += 1
            report.deletions.append(deletion)
            try:
                self._audit(deletion, pinned, now=now, retention=retention)
                deletion.audited = True
            except Exception as exc:
                report.errors.append(f"plugin_gc audit {pinned.object.name}: {type(exc).__name__}: {exc}")
                self._log.error("plugin sweep stopped: a deletion could not be audited",
                                object=pinned.object.name, error=type(exc).__name__)
                report.kept += len(candidates) - index - 1
                break
            self._log.info("plugin copy deleted", object=pinned.object.name, runnable_sha256=pinned.digest,
                           generation=pinned.object.generation)

        self._log.info("plugin sweep complete", **{k: v for k, v in report.as_dict().items() if k != "deletions"})
        return report

    def _audit(self, deletion: Deletion, pinned: PinnedCopy, *, now: datetime, retention: timedelta) -> None:
        created = _as_datetime(pinned.object.created_at)
        self._db.collection(AUDIT_COLLECTION).document().set({
            "action": AUDIT_ACTION,
            "by": AUDIT_BY,
            "at": now,
            "target": deletion.object,
            "target_tenant_id": deletion.tenant_id,
            "detail": {
                "scope": deletion.scope,
                "object": deletion.object,
                "runnable_sha256": deletion.runnable_sha256,
                "generation": deletion.generation,
                "bytes": pinned.object.size,
                "created_at": created.isoformat() if created else None,
                "retention_days": retention.days,
                "reason": REASON,
            },
        })


def build_plugin_collector(config: Any, db: Any, logger: Any, environ: Mapping[str, str] | None = None
                           ) -> PluginCollector | None:
    """The collector for this deployment, or None when it is switched off or
    no bucket can be named: a sweep that lists nothing and reports a clean
    pass over a bucket it never opened is worse than none."""
    gc = PluginGcConfig.from_env(getattr(config, "project_id", ""), environ)
    if not gc.enabled or not gc.bucket:
        logger.info("plugin copy collection disabled", enabled=gc.enabled, bucket=gc.bucket or "(unset)")
        return None
    return PluginCollector(
        objects=GcsPluginObjects(gc.bucket, project_id=getattr(config, "project_id", None) or None),
        references=PluginReferences(db),
        db=db,
        config=gc,
        logger=logger,
        dry_run=bool(getattr(config, "dry_run", False)),
    )


__all__ = [
    "AUDIT_ACTION",
    "GcsPluginObjects",
    "PluginCollector",
    "PluginGcConfig",
    "PluginObject",
    "PluginReferences",
    "PluginSweepReport",
    "build_plugin_collector",
    "parse_object",
    "task_digests",
]
