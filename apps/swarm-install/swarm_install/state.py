"""The install state and its store (docs/design/day-one-install.md §2.3).

* **Primary store:** `gs://swarm-tfstate-<project>/swarm-install/state.json`
  (manifest `other.install-state`). Every write carries `ifGenerationMatch`, so
  two admins cannot overwrite each other: the second writer's write fails, it
  re-reads, re-applies its change on top and writes again.
* **Before the bucket exists** (stages 0 and 1) the state is local only, in
  `~/.config/swarmcloud/install/<project>.json`. It is uploaded once, as soon
  as the bucket exists (`StateStore.attach_bucket`); from then on the bucket is
  authoritative and the local file is a cache of it.
* **Never stored: a secret value.** The state records that a secret version
  exists and its number, never the value: serialising a state that holds a
  `SecretStr`, `SecretBytes` or raw bytes anywhere raises `SecretValueError`.

The object store is a small protocol (`ObjectStore`) so the tests run on a
fake with no credentials; `GcsObjectStore` is the real one, and imports
`google.cloud.storage` only when it is built.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretBytes, SecretStr

#: The bump rule: a field added with a default keeps the version; a field
#: renamed, removed or re-typed bumps it, and the engine refuses a state whose
#: version is newer than it knows.
SCHEMA_VERSION = 1

STATE_OBJECT = "swarm-install/state.json"
LOCAL_DIR = Path("~/.config/swarmcloud/install")

#: A step's recorded state.
STEP_DONE = "done"
STEP_DRIFT = "drift"
STEP_FAILED = "failed"
STEP_PAUSED = "paused"


def state_bucket(project: str) -> str:
    """The bucket that holds Terraform state and the install state."""
    return f"swarm-tfstate-{project}"


def local_path(project: str, base: Path | None = None) -> Path:
    return (base if base is not None else LOCAL_DIR.expanduser()) / f"{project}.json"


class SecretValueError(TypeError):
    """A secret value reached the install state; it is never stored."""


class StateConflict(RuntimeError):
    """Another writer changed the state object since it was read."""


class PreconditionFailed(RuntimeError):
    """An object store write's generation precondition did not hold."""


class StateTooNew(RuntimeError):
    """The stored state's schema is newer than this engine understands."""


class ObjectStore(Protocol):
    """The three calls the engine makes on its bucket.

    `read` returns `(data, generation)` or `None` when the object is absent.
    `write` with `if_generation_match=0` creates only; with `n` replaces only
    generation `n`. Either raises `PreconditionFailed` when that does not hold,
    and returns the new generation.
    """

    def read(self, name: str) -> tuple[bytes, int] | None: ...

    def write(self, name: str, data: bytes, *, if_generation_match: int) -> int: ...

    def delete(self, name: str, *, if_generation_match: int) -> None: ...


class GcsObjectStore:
    """`ObjectStore` on a real GCS bucket."""

    def __init__(self, bucket: str, *, client: Any = None) -> None:
        from google.cloud import storage  # lazy: no client at import time

        self._bucket = (client or storage.Client()).bucket(bucket)

    def read(self, name: str) -> tuple[bytes, int] | None:
        blob = self._bucket.get_blob(name)
        if blob is None:
            return None
        data = blob.download_as_bytes(if_generation_match=blob.generation)
        return data, int(blob.generation)

    def write(self, name: str, data: bytes, *, if_generation_match: int) -> int:
        from google.api_core import exceptions

        blob = self._bucket.blob(name)
        try:
            blob.upload_from_string(
                data, content_type="application/json", if_generation_match=if_generation_match
            )
        except exceptions.PreconditionFailed as exc:
            raise PreconditionFailed(f"{name}: generation is not {if_generation_match}") from exc
        return int(blob.generation)

    def delete(self, name: str, *, if_generation_match: int) -> None:
        from google.api_core import exceptions

        try:
            self._bucket.blob(name).delete(if_generation_match=if_generation_match)
        except exceptions.PreconditionFailed as exc:
            raise PreconditionFailed(f"{name}: generation is not {if_generation_match}") from exc
        except exceptions.NotFound as exc:
            raise PreconditionFailed(f"{name}: already gone") from exc


class StepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    at: float
    by: str
    plan_digest: str | None = None
    outputs: dict[str, Any] = {}
    detail: str | None = None


class InstallState(BaseModel):
    """What the engine remembers between runs (design §2.3 "Contents")."""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    project: str
    environment: str
    release: str | None = None
    region: str | None = None
    hostname: str | None = None
    steps: dict[str, StepRecord] = {}
    #: The non-secret outputs later steps need (IAP audiences, App id and slug,
    #: Desktop client id); for a secret, only that a version exists and its
    #: number.
    outputs: dict[str, Any] = {}
    #: What the console wrote, copied in when the engine reads it (plan §5.4).
    console: dict[str, Any] = {}
    #: R4: the deny-list the engine honours; empty for every project but the
    #: shared one, and never read from an environment variable.
    deny_list: list[str] = Field(default_factory=list)
    hand_off_at: float | None = None
    ready_at: float | None = None

    def done(self, step_id: str) -> bool:
        record = self.steps.get(step_id)
        return record is not None and record.state == STEP_DONE

    def to_bytes(self) -> bytes:
        refuse_secrets("state", self)
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=1).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> InstallState:
        raw = json.loads(data)
        version = raw.get("schema_version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise StateTooNew(
                f"the install state is schema {version}; this engine knows {SCHEMA_VERSION}. "
                "Upgrade the installer before resuming."
            )
        return cls.model_validate(raw)


def refuse_secrets(path: str, value: Any) -> None:
    if isinstance(value, (SecretStr, SecretBytes, bytes, bytearray, memoryview)):
        raise SecretValueError(
            f"{path} holds a secret value; the install state records only that a "
            "secret version exists and its number"
        )
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            refuse_secrets(f"{path}.{name}", getattr(value, name))
    elif isinstance(value, dict):
        for key, item in value.items():
            refuse_secrets(f"{path}.{key}", item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            refuse_secrets(f"{path}[{index}]", item)


def _write_local(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class StateStore:
    """The install state: local before the bucket exists, the bucket after.

    `load` remembers the generation it read; `save` writes only over that
    generation. `update` is the safe way to change the state: it re-reads and
    re-applies the change when another writer got there first.
    """

    project: str
    environment: str
    local: Path
    bucket: ObjectStore | None = None
    _generation: int = 0
    _state: InstallState | None = None

    @property
    def generation(self) -> int:
        return self._generation

    def _fresh(self) -> InstallState:
        return InstallState(project=self.project, environment=self.environment)

    def _read_local(self) -> InstallState | None:
        if not self.local.is_file():
            return None
        return InstallState.from_bytes(self.local.read_bytes())

    def load(self) -> InstallState:
        if self.bucket is not None:
            found = self.bucket.read(STATE_OBJECT)
            if found is None:
                self._generation = 0
                state = self._read_local() or self._fresh()
            else:
                data, self._generation = found
                state = InstallState.from_bytes(data)
                _write_local(self.local, data)
        else:
            state = self._read_local() or self._fresh()
        self._state = state
        return state.model_copy(deep=True)

    def save(self, state: InstallState) -> None:
        """Write `state` over the generation last read; `StateConflict` if moved."""
        data = state.to_bytes()
        if self.bucket is not None:
            try:
                self._generation = self.bucket.write(
                    STATE_OBJECT, data, if_generation_match=self._generation
                )
            except PreconditionFailed as exc:
                raise StateConflict(
                    "another writer changed the install state since it was read"
                ) from exc
        _write_local(self.local, data)
        self._state = state.model_copy(deep=True)

    def update(self, change: Callable[[InstallState], None], *, attempts: int = 5) -> InstallState:
        """Apply `change` to the latest state and save it, re-reading on conflict."""
        state = self._state.model_copy(deep=True) if self._state is not None else self.load()
        for _ in range(attempts):
            change(state)
            try:
                self.save(state)
                return state
            except StateConflict:
                state = self.load()
        raise StateConflict(f"the install state kept moving; gave up after {attempts} attempts")

    def attach_bucket(self, bucket: ObjectStore) -> InstallState:
        """The bucket exists now: upload the local state once, then use the bucket.

        If the object is already there (another admin, or an earlier run), it
        is authoritative and the local file becomes its cache; nothing is
        uploaded over it.
        """
        self.bucket = bucket
        found = bucket.read(STATE_OBJECT)
        if found is None:
            local = self._read_local() or self._state or self._fresh()
            try:
                self._generation = bucket.write(
                    STATE_OBJECT, local.to_bytes(), if_generation_match=0
                )
                self._state = local.model_copy(deep=True)
                return local.model_copy(deep=True)
            except PreconditionFailed:
                pass
        return self.load()
