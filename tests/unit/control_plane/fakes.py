"""An in-memory Firestore good enough to run the real control plane against.

This is not a mock. The services under test build their real queries, real
transactions and real batches; this object executes them. Two consequences that
matter for what the tests can prove:

  * `firestore.transactional` is the REAL decorator from google-cloud-firestore.
    `FakeTransaction` implements the private protocol it drives (`_clean_up`,
    `_begin`, `_commit`, `_rollback`), so the admission path under test is the
    same control flow that runs in production, not a re-implementation.
  * Reads inside a transaction see committed state and writes are buffered until
    commit, exactly as Firestore behaves. A test that passes because writes were
    visible mid-transaction would be lying.

Unsupported query shapes raise rather than returning wrong answers, so a query
this fake cannot execute fails loudly in a test instead of silently passing.
"""

from __future__ import annotations

import copy
import itertools
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Iterator

from google.api_core import exceptions as gexc

ASCENDING = "ASCENDING"
DESCENDING = "DESCENDING"

_MISSING = object()


class _Bottom:
    """Sorts before everything, so a missing field never raises on compare."""

    def __lt__(self, other: Any) -> bool:
        return not isinstance(other, _Bottom)

    def __gt__(self, other: Any) -> bool:
        return False

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _Bottom)

    def __hash__(self) -> int:
        return hash("_Bottom")


BOTTOM = _Bottom()


def _field(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _sortable(value: Any) -> Any:
    if value is _MISSING or value is None:
        return BOTTOM
    if isinstance(value, bool):
        return int(value)
    return value


_RANGE_OPS = frozenset({"<", "<=", ">", ">="})


def _compare(actual: Any, op: Any, expected: Any) -> bool:
    # `FieldFilter(field, "==", None)` does not arrive as "==". The installed
    # google-cloud-firestore turns it into the unary operator IS_NULL (and
    # `!= None` into IS_NOT_NULL) before the query ever sees it. Firestore
    # matches IS_NULL only where the field is PRESENT and null -- a document
    # without the field is not returned -- which is why admission writes
    # `released_at: None` explicitly and why this does not treat _MISSING as
    # null.
    unary = getattr(op, "name", None)
    if unary == "IS_NULL":
        return actual is None
    if unary == "IS_NOT_NULL":
        return actual is not _MISSING and actual is not None
    if op in {"==", "eq"}:
        return actual == expected
    if op in {"!=", "ne"}:
        return actual != expected
    if op == "in":
        return actual in expected
    if op == "not-in":
        return actual not in expected
    if op == "array_contains":
        return isinstance(actual, list) and expected in actual
    if op == "array_contains_any":
        return isinstance(actual, list) and any(item in actual for item in expected)
    # Refuse an unknown operator BEFORE the missing-field shortcut below. It
    # used to come after it, so an operator this fake did not know returned
    # False for every document whose field was null -- an empty result, not
    # the loud failure the module docstring promises. IS_NULL was the operator
    # that found it: every live lease matched nothing and only a released one
    # reached the raise.
    if op not in _RANGE_OPS:
        raise NotImplementedError(f"fake firestore does not implement operator {op!r}")
    if actual is _MISSING or actual is None:
        # Firestore excludes documents missing the field from inequality results.
        return False
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    return actual >= expected


@dataclass(frozen=True)
class FakeSnapshot:
    id: str
    path: str
    _data: dict[str, Any] | None
    reference: "FakeDocumentRef"

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field_path: str) -> Any:
        value = _field(self._data or {}, field_path)
        return None if value is _MISSING else value


@dataclass(frozen=True)
class AggregationResult:
    value: int
    alias: str = "count"


class FakeAggregationQuery:
    def __init__(self, rows: list[FakeSnapshot]) -> None:
        self._rows = rows

    def get(self) -> list[list[AggregationResult]]:
        return [[AggregationResult(value=len(self._rows))]]


class FakeQuery:
    def __init__(
        self,
        db: "FakeFirestore",
        collection_path: str,
        *,
        filters: tuple[tuple[str, str, Any], ...] = (),
        orders: tuple[tuple[str, str], ...] = (),
        limit_n: int | None = None,
    ) -> None:
        self._db = db
        self._path = collection_path
        self._filters = filters
        self._orders = orders
        self._limit = limit_n

    # -- builders ---------------------------------------------------------

    def _with(self, **kwargs: Any) -> "FakeQuery":
        return FakeQuery(
            self._db,
            self._path,
            filters=kwargs.get("filters", self._filters),
            orders=kwargs.get("orders", self._orders),
            limit_n=kwargs.get("limit_n", self._limit),
        )

    def where(self, *args: Any, filter: Any = None, **kwargs: Any) -> "FakeQuery":
        if filter is None:
            raise NotImplementedError(
                "fake firestore only accepts the keyword form where(filter=FieldFilter(...)); "
                "the positional form is deprecated in google-cloud-firestore"
            )
        clause = (filter.field_path, filter.op_string, filter.value)
        return self._with(filters=self._filters + (clause,))

    def order_by(self, field_path: str, direction: str = ASCENDING, **kwargs: Any) -> "FakeQuery":
        return self._with(orders=self._orders + ((field_path, direction),))

    def limit(self, count: int) -> "FakeQuery":
        return self._with(limit_n=count)

    def count(self, alias: str | None = None) -> FakeAggregationQuery:
        return FakeAggregationQuery(list(self._rows()))

    # -- execution --------------------------------------------------------

    def _rows(self) -> Iterator[FakeSnapshot]:
        depth = self._path.count("/") + 1
        matches: list[tuple[str, dict[str, Any]]] = []
        for path, data in self._db.docs.items():
            if not path.startswith(self._path + "/"):
                continue
            if path.count("/") != depth:
                continue
            if all(
                _compare(_field(data, field), op, value)
                for field, op, value in self._filters
            ):
                matches.append((path, data))

        for field, direction in reversed(self._orders):
            matches.sort(
                key=lambda item, f=field: _sortable(_field(item[1], f)),
                reverse=(direction == DESCENDING),
            )

        rows = (
            FakeSnapshot(
                id=path.rsplit("/", 1)[1],
                path=path,
                _data=copy.deepcopy(data),
                reference=FakeDocumentRef(self._db, path),
            )
            for path, data in matches
        )
        if self._limit is not None:
            rows = itertools.islice(rows, self._limit)
        return iter(rows)

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[FakeSnapshot]:
        return self._rows()

    def get(self, *args: Any, **kwargs: Any) -> list[FakeSnapshot]:
        return list(self._rows())


class FakeCollectionRef(FakeQuery):
    def __init__(self, db: "FakeFirestore", path: str) -> None:
        super().__init__(db, path)

    @property
    def id(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def document(self, doc_id: str | None = None) -> "FakeDocumentRef":
        return FakeDocumentRef(self._db, f"{self._path}/{doc_id or uuid.uuid4().hex}")

    def add(self, data: dict[str, Any]) -> tuple[datetime | None, "FakeDocumentRef"]:
        ref = self.document()
        ref.set(data)
        return None, ref


class FakeDocumentRef:
    def __init__(self, db: "FakeFirestore", path: str) -> None:
        self._db = db
        self.path = path

    @property
    def id(self) -> str:
        return self.path.rsplit("/", 1)[1]

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self._db, f"{self.path}/{name}")

    def get(self, *args: Any, **kwargs: Any) -> FakeSnapshot:
        data = self._db.docs.get(self.path)
        return FakeSnapshot(
            id=self.id,
            path=self.path,
            _data=copy.deepcopy(data) if data is not None else None,
            reference=self,
        )

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self.path in self._db.docs:
            merged = copy.deepcopy(self._db.docs[self.path])
            merged.update(copy.deepcopy(data))
            self._db.docs[self.path] = merged
        else:
            self._db.docs[self.path] = copy.deepcopy(data)

    def update(self, data: dict[str, Any]) -> None:
        if self.path not in self._db.docs:
            # Real Firestore refuses to update a document that does not exist;
            # silently creating one here would hide a genuine bug.
            raise gexc.NotFound(f"no document to update at {self.path}")
        current = self._db.docs[self.path]
        for key, value in copy.deepcopy(data).items():
            current[key] = value

    def delete(self) -> None:
        self._db.docs.pop(self.path, None)


class FakeWriteBatch:
    def __init__(self, db: "FakeFirestore") -> None:
        self._db = db
        self._ops: list[tuple[str, FakeDocumentRef, dict[str, Any]]] = []

    def set(self, ref: FakeDocumentRef, data: dict[str, Any], merge: bool = False) -> None:
        self._ops.append(("set_merge" if merge else "set", ref, data))

    def update(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        self._ops.append(("update", ref, data))

    def delete(self, ref: FakeDocumentRef) -> None:
        self._ops.append(("delete", ref, {}))

    def commit(self) -> list[Any]:
        if len(self._ops) > 500:
            raise gexc.InvalidArgument("a Firestore write batch is capped at 500 operations")
        for op, ref, data in self._ops:
            if op == "set":
                ref.set(data)
            elif op == "set_merge":
                ref.set(data, merge=True)
            elif op == "update":
                ref.update(data)
            else:
                ref.delete()
        self._ops.clear()
        return []


class FakeTransaction:
    """Implements the protocol `firestore.transactional` drives.

    Reads see committed state; writes are buffered until `_commit`, so a test
    cannot accidentally observe a half-applied transaction and conclude the
    all-or-nothing reservation works when it does not.
    """

    def __init__(self, db: "FakeFirestore", max_attempts: int = 5) -> None:
        self._db = db
        self._max_attempts = max_attempts
        self._read_only = False
        self._id: bytes | None = None
        self._buffer: list[tuple[str, FakeDocumentRef, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    # -- protocol used by google.cloud.firestore.transactional ------------

    def _clean_up(self) -> None:
        self._buffer = []
        self._id = None

    def _begin(self, retry_id: bytes | None = None) -> None:
        self._id = retry_id or uuid.uuid4().bytes
        self._db.transactions_begun += 1

    def _commit(self) -> list[Any]:
        for op, ref, data in self._buffer:
            if op == "set":
                ref.set(data)
            elif op == "update":
                ref.update(data)
            else:
                ref.delete()
        self._buffer = []
        self._id = None
        self.commits += 1
        self._db.transactions_committed += 1
        return []

    def _rollback(self) -> None:
        self._buffer = []
        self._id = None
        self.rollbacks += 1

    # -- the API the admission code uses ----------------------------------

    def get(self, ref: Any, **kwargs: Any) -> Any:
        if isinstance(ref, FakeDocumentRef):
            self._db.transaction_reads += 1
            return ref.get()
        return ref.stream()

    def set(self, ref: FakeDocumentRef, data: dict[str, Any], merge: bool = False) -> None:
        self._buffer.append(("set", ref, copy.deepcopy(data)))

    def update(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        self._buffer.append(("update", ref, copy.deepcopy(data)))

    def delete(self, ref: FakeDocumentRef) -> None:
        self._buffer.append(("delete", ref, {}))


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.transactions_begun = 0
        self.transactions_committed = 0
        self.transaction_reads = 0

    def collection(self, path: str) -> FakeCollectionRef:
        return FakeCollectionRef(self, path)

    def document(self, path: str) -> FakeDocumentRef:
        return FakeDocumentRef(self, path)

    def batch(self) -> FakeWriteBatch:
        return FakeWriteBatch(self)

    def transaction(self, **kwargs: Any) -> FakeTransaction:
        return FakeTransaction(self)

    # -- test conveniences -------------------------------------------------

    def dump(self, prefix: str = "") -> dict[str, dict[str, Any]]:
        return {k: copy.deepcopy(v) for k, v in self.docs.items() if k.startswith(prefix)}

    def paths(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.docs if k.startswith(prefix))

    def collection_docs(self, path: str) -> Iterable[dict[str, Any]]:
        return [snap.to_dict() for snap in FakeCollectionRef(self, path).stream()]
