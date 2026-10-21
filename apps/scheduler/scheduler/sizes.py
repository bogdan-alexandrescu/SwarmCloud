"""The runner-size version admission reads, and the size it pins on an agent.

docs/design/runner-sizes.md §4.3-§4.4 (SIZES-SCHED-2, part of #1689). An
admin's runner sizes are a versioned platform setting: `control/runner_sizes`
names `current_version`, and `runner_size_versions/<version>` holds the whole
table, written only by swarm-api's admin route. The frozen contract owns the
shape (`ResourceSize`, `LiveShare`), the bounds (`check_size`) and the rule
for which profiles follow (`effective_size`); this module reads the two
documents, judges them, and turns one agent's class into the `Sized` that
admission charges, the attempt records and the GKE pod is rendered from.

THE SIZE IS PINNED AT ADMISSION. `current` is read once per admission pass;
`for_task` derives the agent's size and units from that reading BEFORE the
lease, and the same object goes to the lease's units, to `create_attempt`'s
record and to the GKE renderer. A version saved between the lease and the
dispatch therefore cannot split them: the units the transaction charged and
the pod Autopilot bills are one object (invariant 2). A running agent keeps
the size it was admitted with, because nothing re-renders a running pod.

A MALFORMED VERSION READS AS THE CODE DEFAULTS, LOGGED AT ERROR. The route
validates every write with the same `check_size`, so a stored version that
fails it was written some other way -- by hand, or by a route with a bug --
and must not size a pod. Version 0 is the code defaults: no version written,
a malformed one, or a Cloud Run profile, which never follows (§4.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from swarm_common.models import Task
from swarm_common.profiles import (
    RESOURCE_CLASSES,
    Backend,
    LiveShare,
    ResourceSize,
    check_size,
    default_sizes,
    effective_size,
    units_for,
)

from .dispatch import live_split_for

log = logging.getLogger(__name__)

#: The fields every stored class carries (design §4.3). `units` may be stored
#: beside them, derived; `live_split` only on a class with a stored split.
SIZE_FIELDS = ("cpu_request", "cpu_limit", "memory_gib", "disk_gib")


@dataclass(frozen=True)
class SizeVersion:
    """One reading of the setting. `classes` None is the code defaults."""

    version: int
    classes: Mapping[str, ResourceSize] | None = None


#: Version 0: what runs with no version written, or with a malformed one.
DEFAULTS = SizeVersion(version=0)


@dataclass(frozen=True)
class Sized:
    """One agent's size, pinned at admission.

    `version` is the version APPLIED, not the one read: a Cloud Run profile,
    or a version refused as malformed, is version 0. `live_split` is the
    (worker, display) pair the GKE pod renders, set only on a live pod.
    """

    resource_class: str
    version: int
    size: ResourceSize
    live_split: tuple[LiveShare, LiveShare] | None = None

    @property
    def units(self) -> int:
        return units_for(self.size.cpu_request, self.size.memory_gib)

    def attempt_fields(self) -> dict[str, Any]:
        """The attempt document's record of this size (contract request 111)."""
        split = None
        if self.live_split is not None:
            split = {
                role: {
                    "cpu_request": share.cpu_request,
                    "cpu_limit": share.cpu_limit,
                    "memory_gib": share.memory_gib,
                }
                for role, share in zip(("worker", "display"), self.live_split)
            }
        return {
            "size_version": self.version,
            "sized_cpu_request": self.size.cpu_request,
            "sized_cpu_limit": self.size.cpu_limit,
            "sized_memory_gib": self.size.memory_gib,
            "sized_disk_gib": self.size.disk_gib,
            "sized_live_split": split,
        }


class MalformedVersion(ValueError):
    """A stored version that may not size a pod."""


def _share(raw: Any, where: str) -> LiveShare:
    if not isinstance(raw, dict) or set(raw) != {"cpu_request", "cpu_limit", "memory_gib"}:
        raise MalformedVersion(f"{where} holds exactly cpu_request, cpu_limit and memory_gib")
    return LiveShare(raw["cpu_request"], raw["cpu_limit"], raw["memory_gib"])


def parse_size(name: str, raw: Any) -> ResourceSize:
    """One stored class as the frozen shape. Only the SHAPE is checked here;
    the values are judged by `check_size`, over the whole table."""
    if not isinstance(raw, dict):
        raise MalformedVersion(f"class {name!r} is not a map")
    missing = [f for f in SIZE_FIELDS if f not in raw]
    if missing:
        raise MalformedVersion(f"class {name!r} is missing {missing}")
    split = raw.get("live_split")
    if split is not None:
        if not isinstance(split, dict) or set(split) != {"worker", "display"}:
            raise MalformedVersion(f"class {name!r}: live_split is a map of worker and display")
        split = (_share(split["worker"], f"{name}.live_split.worker"),
                 _share(split["display"], f"{name}.live_split.display"))
    return ResourceSize(
        cpu_request=raw["cpu_request"],
        cpu_limit=raw["cpu_limit"],
        memory_gib=raw["memory_gib"],
        disk_gib=raw["disk_gib"],
        live_split=split,
    )


def parse_version(pointer: dict[str, Any] | None,
                  document: dict[str, Any] | None) -> SizeVersion:
    """The pointer and its version document as a `SizeVersion`.

    Raises MalformedVersion for anything that may not size a pod: a pointer
    whose version is not a whole number, a version document that is missing
    or names another version, a class that is not the frozen shape, or a
    table `check_size` refuses (every refusal named).
    """
    if pointer is None:
        return DEFAULTS
    version = pointer.get("current_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise MalformedVersion(f"current_version {version!r} is not a version number")
    if version == 0:
        return DEFAULTS
    if document is None:
        raise MalformedVersion(f"version {version} has no document")
    if document.get("version") != version:
        raise MalformedVersion(
            f"version {version}'s document says version {document.get('version')!r}")
    classes = document.get("classes")
    if not isinstance(classes, dict):
        raise MalformedVersion(f"version {version}: classes is not a map")
    try:
        table = {name: parse_size(name, raw) for name, raw in classes.items()}
    except MalformedVersion as exc:
        raise MalformedVersion(f"version {version}: {exc}") from None
    refusals = check_size(table)
    if refusals:
        raise MalformedVersion(f"version {version}: " + "; ".join(
            f"{r.resource_class}.{r.field}={r.value!r} breaks {r.bound}" for r in refusals))
    for name, raw in classes.items():
        stored = raw.get("units")
        if stored is not None and stored != table[name].units:
            raise MalformedVersion(
                f"version {version}: {name}.units is {stored!r}, derived {table[name].units}")
    return SizeVersion(version=version, classes=MappingProxyType(table))


def current(store: Any) -> SizeVersion:
    """The version admission applies this pass: the stored one when it may
    size a pod, else the code defaults with an ERROR naming why. A Firestore
    failure is not caught: it is not a malformed setting, and admission's
    own transaction would meet it next."""
    pointer, document = store.runner_size_documents()
    try:
        return parse_version(pointer, document)
    except MalformedVersion as exc:
        log.error("runner sizes: the stored setting is malformed, so admission uses the "
                  "code defaults (version 0) until it is fixed: %s", exc)
        return DEFAULTS


def _default(name: str, live: bool) -> Sized:
    size = default_sizes()[name]
    split = live_split_for(RESOURCE_CLASSES[name], size) if live else None
    return Sized(resource_class=name, version=0, size=size, live_split=split)


def for_task(version: SizeVersion, task: Task, *, backend: Backend, live: bool) -> Sized:
    """The size `task` is charged, recorded and rendered at.

    Its OWN class, `task.resource_class`, as admission's pools are
    (`pool_names_for`): a step that named a smaller class is sized from it.
    A GKE profile follows `version`; a Cloud Run profile keeps the code
    default and is version 0, because Cloud Run fixes resources on the Job
    (`effective_size`, §4.7). `live` is whether the GKE pod carries the
    display sidecar (`dispatch.live_display_on`); only then is a split
    recorded. A resized class whose live split would leave the worker
    nothing is sized at its default, logged at ERROR, rather than refused:
    `check_size` cannot see the renderer's table for a class with no stored
    split.
    """
    name = task.resource_class
    follows = backend is Backend.GKE_AUTOPILOT and version.classes is not None
    if not follows:
        return _default(name, live and backend is Backend.GKE_AUTOPILOT)
    size = effective_size(name, version.classes, backend)
    split = None
    if live:
        try:
            split = live_split_for(RESOURCE_CLASSES[name], size)
        except ValueError as exc:
            log.error("runner sizes: version %s's %s leaves a live pod no room (%s); "
                      "task %s is sized at the code default", version.version, name, exc, task.id)
            return _default(name, live)
    return Sized(resource_class=name, version=version.version, size=size, live_split=split)
