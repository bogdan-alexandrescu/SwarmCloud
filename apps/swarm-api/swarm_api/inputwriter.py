"""The one place swarm-api WRITES to the artifact bucket: an await-input answer.

docs/design/human-steps.md §4.3 (lane HW-API-ANSWER). A person's answer to an
`await-input` step, its files and an expiry's declared default are objects
the next step stages like any artifact, so they have to be in the bucket, and
nothing but swarm-api sees the answer arrive. The grant that lets it write
(`google_storage_bucket_iam_member.api_input_writer`, terraform/infra/
await_input.tf) is `roles/storage.objectCreator`, bucket-scoped, and
IAM-conditioned to

    tenants/<tenant>/tasks/<task>/attempts/input-<nonce>/artifacts/...

WHY A MODULE OF ITS OWN. `objects.py` is read-only, and its docstring's
guarantee -- a method the class does not have is a method no refactor can
accidentally call -- is worth keeping for every reader. This class has
exactly one method that touches the bucket, `create`, and nothing that
reads, lists or deletes.

IT REFUSES WHAT THE CONDITION WOULD REFUSE, BEFORE IT CALLS GCS. The
condition cannot pin the nonce's twelve hex characters or the artifact name
(the comment above `await_input_object_condition` says why), so `key_refusal`
pins the whole shape here: one tenant segment that is not `tasks`, one task
segment, `input-` and twelve lower-case hex, `artifacts/`, then either the
artifact's name or one file under `<artifact stem>.files/`. A bug that builds
any other key fails in a unit test, not as a 403 in production.

CREATE, NEVER OVERWRITE. Every object is created with `if_generation_match=0`
and `customTime` set to the write time, so the retention rule's
`days_since_custom_time` clock runs for it as for a worker's upload
(terraform/modules/storage/main.tf). `objectCreator` alone could not
overwrite anyway (that needs delete); the precondition makes an existing
object a refusal rather than a 403 that reads like a permissions problem.
"""

from __future__ import annotations

import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Callable, Protocol

#: Stands in for a worker's attempt id in an answer's key. Worker attempt ids
#: are minted by `new_id` and never start with it, so the two namespaces
#: cannot collide; `agent_worker.inputs.artifact_key` accepts it unchanged.
INPUT_ATTEMPT_PREFIX = "input-"
#: Twelve hex characters: one nonce per attempt to answer (§4.3).
NONCE_HEX_CHARS = 12

#: The name of an artifact or of one attached file (§4.1, §4.3): no
#: separator, no leading dot, at most 128 characters.
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_NAME_RE = re.compile(rf"^{NAME_PATTERN}$")
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_KEY_RE = re.compile(
    rf"^tenants/(?P<tenant>{_SEGMENT})/tasks/(?P<task>{_SEGMENT})/attempts/"
    rf"{INPUT_ATTEMPT_PREFIX}(?P<nonce>[0-9a-f]{{{NONCE_HEX_CHARS}}})/artifacts/"
    rf"(?P<tail>{NAME_PATTERN}(?:/{NAME_PATTERN})?)$"
)

#: Where an artifact's attachments sit: `review.json` -> `review.files/`. The
#: worker's `agent_worker.inputs.ATTACHMENTS_DIR_SUFFIX`;
#: tests/unit/control_plane/test_await_input_files.py holds the two equal.
ATTACHMENTS_DIR_SUFFIX = ".files"


def new_input_attempt() -> str:
    """A fresh `input-<12 hex>`: a losing writer's objects are named by no manifest."""
    return INPUT_ATTEMPT_PREFIX + secrets.token_hex(NONCE_HEX_CHARS // 2)


def attachments_dir(artifact: str) -> str:
    """`review.json` -> `review.files`: the stem is everything before the LAST dot."""
    return f"{PurePosixPath(artifact).stem}{ATTACHMENTS_DIR_SUFFIX}"


def input_key(tenant_id: str, task_id: str, attempt: str, name: str) -> str:
    """`tenants/<t>/tasks/<k>/attempts/<attempt>/artifacts/<name>`, unchecked."""
    return f"tenants/{tenant_id}/tasks/{task_id}/attempts/{attempt}/artifacts/{name}"


def key_refusal(key: str) -> str | None:
    """Why `key` is outside the grant's shape, or None when it is inside it."""
    if not isinstance(key, str):
        return "the key is not a string"
    matched = _KEY_RE.match(key)
    if matched is None:
        return (
            "the key is not tenants/<tenant>/tasks/<task>/attempts/input-<12 hex>/"
            "artifacts/<name>"
        )
    if matched["tenant"] == "tasks":
        # The condition's clause 4 reads the "/tasks/" of tenants/tasks/ and
        # refuses it; refusing it here keeps the two in step.
        return "a tenant named 'tasks' is refused by the grant's condition"
    directory, separator, leaf = matched["tail"].partition("/")
    if separator and (not directory.endswith(ATTACHMENTS_DIR_SUFFIX) or not _NAME_RE.match(leaf)):
        return "an attached file must be one file under <artifact stem>.files/"
    return None


class InputKeyRefused(ValueError):
    """A key outside the grant's shape. A bug in the caller, never a person's input."""


class InputWriteFailed(Exception):
    """The bucket did not create the object. Nothing names it yet; a retry is safe."""


class InputWriter(Protocol):
    bucket: str

    def uri(self, key: str) -> str: ...

    def create(self, key: str, data: bytes, content_type: str) -> str: ...


def _checked(key: str) -> str:
    refusal = key_refusal(key)
    if refusal is not None:
        raise InputKeyRefused(refusal)
    return key


class GcsInputWriter:
    """Google Cloud Storage, create-only.

    Holds no client until the first `create`, as `objects.GcsObjectReader`
    holds none: `create_app()` must need no credentials.
    """

    def __init__(
        self,
        bucket: str,
        project_id: str | None = None,
        client: Any = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not bucket:
            raise ValueError("an artifact bucket is required to write an answer")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client
        self._now = now
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from google.cloud import storage  # lazy: create_app() needs no credentials

                self._client = storage.Client(project=self._project_id)
            return self._client

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket}/{key}"

    def create(self, key: str, data: bytes, content_type: str) -> str:
        """Create `key` holding `data`; the object's URI. Never overwrites."""
        _checked(key)
        try:
            blob = self._get_client().bucket(self.bucket).blob(key)
            blob.custom_time = self._now()
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except Exception as exc:  # the client's type only: its text can name the key
            raise InputWriteFailed(
                f"the artifact store did not create the object ({type(exc).__name__})"
            ) from None
        return self.uri(key)


class InMemoryInputWriter:
    """The same create-only semantics over a dict, for tests and `RUN_MODE=local`.

    Writes into an `objects.InMemoryObjectReader`'s dict when given one, so
    what an answer creates is what the artifact routes read back. `fail`
    makes every create raise, which is the only way to prove a failed write
    leaves Firestore untouched.
    """

    def __init__(self, reader: Any = None, *, bucket: str = "swarm-artifacts-test") -> None:
        self._reader = reader
        self.bucket = getattr(reader, "bucket", bucket)
        self.objects: dict[str, bytes] = reader.objects if reader is not None else {}
        self.content_types: dict[str, str] = {}
        self.created: list[str] = []
        self.fail = False
        self.before_create: Callable[[str], None] | None = None
        self._lock = threading.Lock()

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket}/{key}"

    def create(self, key: str, data: bytes, content_type: str) -> str:
        _checked(key)
        if self.before_create is not None:
            self.before_create(key)
        if self.fail:
            raise InputWriteFailed("the artifact store did not create the object (injected)")
        with self._lock:
            if key in self.objects:
                raise InputWriteFailed("the object already exists; answers never overwrite")
            if self._reader is not None:
                self._reader.put(key, bytes(data))
            else:
                self.objects[key] = bytes(data)
            self.content_types[key] = content_type
            self.created.append(key)
        return self.uri(key)


def build_input_writer(*, bucket: str, project_id: str | None = None) -> InputWriter | None:
    """The production writer, or None when no bucket is configured."""
    if not bucket:
        return None
    return GcsInputWriter(bucket=bucket, project_id=project_id)


__all__ = [
    "ATTACHMENTS_DIR_SUFFIX",
    "GcsInputWriter",
    "INPUT_ATTEMPT_PREFIX",
    "InMemoryInputWriter",
    "InputKeyRefused",
    "InputWriteFailed",
    "InputWriter",
    "NAME_PATTERN",
    "attachments_dir",
    "build_input_writer",
    "input_key",
    "key_refusal",
    "new_input_attempt",
]
