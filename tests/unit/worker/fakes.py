"""Test doubles for the execution plane.

`FakeFirestore` is not a mock -- nothing here records calls or asserts on them.
It is a small, real document store with Firestore's semantics (documents are
dicts, `set` replaces, `update` merges and fails on a missing document,
subcollections hang off documents), so the code under test is the production
`ControlPlane` running against the production `release_lease_in_transaction`
from the frozen admission module. A mock would prove only that the worker calls
the functions the test expects it to call; this proves the pools actually come
back down.
"""

from __future__ import annotations

from typing import Any, Iterator


class DocumentMissing(KeyError):
    """Firestore's `update` on a missing document is an error, not an insert."""


class FakeSnapshot:
    def __init__(self, path: str, data: dict[str, Any] | None) -> None:
        self.reference_path = path
        self.id = path.rsplit("/", 1)[-1]
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None

    def get(self, field: str) -> Any:
        return (self._data or {}).get(field)


class FakeDocumentRef:
    def __init__(self, db: "FakeFirestore", path: str) -> None:
        self._db = db
        self.path = path
        self.id = path.rsplit("/", 1)[-1]

    def get(self, *_args: Any, **_kwargs: Any) -> FakeSnapshot:
        return FakeSnapshot(self.path, self._db.documents.get(self.path))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self.path in self._db.documents:
            self._db.documents[self.path].update(dict(data))
        else:
            self._db.documents[self.path] = dict(data)
        self._db.writes.append(("set", self.path, dict(data)))

    def update(self, data: dict[str, Any]) -> None:
        if self.path not in self._db.documents:
            raise DocumentMissing(self.path)
        self._db.documents[self.path].update(dict(data))
        self._db.writes.append(("update", self.path, dict(data)))

    def delete(self) -> None:
        self._db.documents.pop(self.path, None)
        self._db.writes.append(("delete", self.path, {}))

    def collection(self, name: str) -> "FakeCollectionRef":
        return FakeCollectionRef(self._db, f"{self.path}/{name}")


class _Filter:
    def __init__(self, field: str, op: str, value: Any) -> None:
        self.field_path = field
        self.op_string = op
        self.value = value


#: Firestore normalises some comparisons into operator enums -- notably
#: `== None`, which becomes IS_NULL -- so the fake maps them back.
_ENUM_OPS = {
    "EQUAL": "==",
    "NOT_EQUAL": "!=",
    "LESS_THAN": "<",
    "LESS_THAN_OR_EQUAL": "<=",
    "GREATER_THAN": ">",
    "GREATER_THAN_OR_EQUAL": ">=",
    "IN": "in",
    "NOT_IN": "not-in",
    "ARRAY_CONTAINS": "array-contains",
}


def _matches(doc: dict[str, Any], flt: Any) -> bool:
    actual = doc.get(flt.field_path)
    op = flt.op_string
    value = flt.value
    enum_name = getattr(op, "name", None)
    if enum_name == "IS_NULL":
        return actual is None
    if enum_name == "IS_NOT_NULL":
        return actual is not None
    if enum_name in _ENUM_OPS:
        op = _ENUM_OPS[enum_name]
    if op in ("==", "eq"):
        return actual == value
    if op in ("!=", "ne"):
        return actual != value
    if op == "in":
        return actual in value
    if op == "not-in":
        return actual not in value
    if op == "<":
        return actual is not None and actual < value
    if op == "<=":
        return actual is not None and actual <= value
    if op == ">":
        return actual is not None and actual > value
    if op == ">=":
        return actual is not None and actual >= value
    if op == "array-contains":
        return isinstance(actual, list) and value in actual
    raise NotImplementedError(f"operator {op!r} is not implemented in the fake")


class FakeQuery:
    def __init__(self, db: "FakeFirestore", prefix: str, filters: list[Any], limit: int | None = None):
        self._db = db
        self._prefix = prefix
        self._filters = filters
        self._limit = limit

    def where(self, *args: Any, filter: Any = None, **_kwargs: Any) -> "FakeQuery":
        flt = filter if filter is not None else _Filter(*args)
        return FakeQuery(self._db, self._prefix, [*self._filters, flt], self._limit)

    def limit(self, count: int) -> "FakeQuery":
        return FakeQuery(self._db, self._prefix, self._filters, count)

    def stream(self) -> Iterator[FakeSnapshot]:
        seen = 0
        for path, doc in sorted(self._db.documents.items()):
            if not path.startswith(self._prefix + "/"):
                continue
            if path.count("/") != self._prefix.count("/") + 1:
                continue  # documents directly in this collection only
            if all(_matches(doc, f) for f in self._filters):
                yield FakeSnapshot(path, doc)
                seen += 1
                if self._limit is not None and seen >= self._limit:
                    return


class FakeCollectionRef(FakeQuery):
    def __init__(self, db: "FakeFirestore", path: str) -> None:
        super().__init__(db, path, [])
        self.path = path

    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        if doc_id is None:
            self._db._auto += 1
            doc_id = f"auto-{self._db._auto}"
        return FakeDocumentRef(self._db, f"{self.path}/{doc_id}")


class FakeTransaction:
    """Applies writes immediately. Unit tests are single-threaded, so the only
    property that matters here is that the SAME code path runs -- the retry and
    isolation behaviour belongs to Firestore itself."""

    def __init__(self, db: "FakeFirestore") -> None:
        self._db = db

    def get(self, ref: FakeDocumentRef) -> FakeSnapshot:
        return ref.get()

    def set(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        ref.set(data)

    def update(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        ref.update(data)


class FakeTransactionRunner:
    def __init__(self, db: "FakeFirestore") -> None:
        self._db = db

    def run(self, fn: Any) -> Any:
        return fn(FakeTransaction(self._db))


class FakeFirestore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self._auto = 0

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self, name)

    def document(self, path: str) -> FakeDocumentRef:
        return FakeDocumentRef(self, path)

    # -- helpers used by the tests -------------------------------------
    def seed(self, path: str, data: dict[str, Any]) -> None:
        self.documents[path] = dict(data)

    def doc(self, path: str) -> dict[str, Any]:
        return self.documents[path]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        """Events in the order they happened.

        Event ids are random, so path order is meaningless; the control plane
        reads this collection ordered by `at`, and so does this helper (ties
        broken by write order, which is what a same-millisecond pair means).
        """
        prefix = f"tasks/{task_id}/events/"
        order = {path: i for i, (_, path, _) in enumerate(self.writes)}
        docs = [(p, d) for p, d in self.documents.items() if p.startswith(prefix)]
        docs.sort(key=lambda item: (item[1].get("at"), order.get(item[0], 0)))
        return [d for _, d in docs]

    def event_types(self, task_id: str) -> list[str]:
        return [e["type"] for e in self.events(task_id)]


class RecordingExporter:
    def __init__(self) -> None:
        self.exports: list[tuple[Any, dict[str, str]]] = []

    def export(self, usage: Any, labels: dict[str, str]) -> None:
        self.exports.append((usage, labels))


class ExplodingChildProcess:
    """Substituted for `ChildProcess` in tests that must prove nothing ran."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "the runner child was started; a fenced worker must never execute the agent"
        )


class FakeSecretClient:
    """Stands in for Secret Manager. Returns a per-secret deterministic value.

    Real enough to prove the worker reads the tenant's OWN secret name: the
    value it hands back is derived from the name it was asked for, so a test can
    assert which secret was accessed rather than that `access` was called.
    """

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.accessed: list[str] = []

    def access(self, secret_name: str, version: str = "latest") -> str:
        self.accessed.append(secret_name)
        return self.values.get(secret_name, f"test-key-for-{secret_name}")


class FakeBackend:
    """A backend whose executions and failures are scripted.

    Every call is appended to a shared journal -- the same list the document
    store writes to -- so a test can assert the ORDER of "invalidated the
    generation", "terminated the execution" and "released the slot" across both,
    which is the property that keeps two agents off one task.
    """

    def __init__(
        self,
        name: str = "CLOUD_RUN_JOB",
        executions: list[Any] | None = None,
        resources: list[Any] | None = None,
        journal: list[Any] | None = None,
        *,
        terminate_raises: Exception | None = None,
        terminate_returns: bool = True,
        list_raises: Exception | None = None,
    ) -> None:
        self.name = name
        self._executions = list(executions or [])
        self._resources = list(resources or [])
        self.journal = journal if journal is not None else []
        self.terminate_raises = terminate_raises
        self.terminate_returns = terminate_returns
        self.list_raises = list_raises
        self.terminated: list[str] = []
        self.deleted: list[str] = []

    def list_executions(self) -> list[Any]:
        if self.list_raises is not None:
            raise self.list_raises
        return list(self._executions)

    def terminate(self, execution: Any) -> bool:
        self.journal.append(("terminate", execution.name, {}))
        if self.terminate_raises is not None:
            raise self.terminate_raises
        if self.terminate_returns:
            self.terminated.append(execution.name)
        return self.terminate_returns

    def list_job_resources(self) -> list[Any]:
        return list(self._resources)

    def delete_job_resource(self, resource: Any) -> bool:
        if not resource.managed:
            raise PermissionError(f"refusing to delete unmanaged resource {resource.name}")
        self.journal.append(("delete_resource", resource.name, {}))
        self.deleted.append(resource.name)
        return True
