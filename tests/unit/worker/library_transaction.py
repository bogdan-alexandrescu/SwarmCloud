"""Firestore's own transaction code, running over the in-memory document store.

`FakeTransactionRunner` calls a transaction body and nothing else. The runner
the worker uses in production, `control.FirestoreTransactionRunner`, hands the
body to `firestore.transactional`, which does three things the fake does not:

  * it begins the transaction with an RPC before the body runs;
  * it commits with another RPC after the body returns;
  * on ANY exception, BaseException included, it rolls back before it
    re-raises, and whatever the rollback raises REPLACES the exception that
    was unwinding. That is `ValueError("... cannot be rolled back.")` when the
    exception arrived before `begin_transaction` returned, and the rollback
    RPC's own error when Firestore cannot be reached.

`TransactionalFirestore` lets the production runner drive the library's real
`Transaction` and `_Transactional` (google-cloud-firestore, as uv.lock pins
it). Two things are stood in for, and only two:

  * the three RPCs a transaction makes for itself, `begin_transaction`,
    `commit` and `rollback`. `FirestoreRPCs` records each with the keyword
    arguments it was called with, which is where a `retry` and a `timeout`
    arrive, and a test can make any of them raise or deliver a signal;
  * the document reads and writes, which land on the in-memory store as
    `FakeTransaction`'s do. A transactional read returns a generator, as
    `Transaction.get` does.

Every non-transactional document call (`get`, `set`, `update`) is recorded as
well, with its keyword arguments, so a test can hold every Firestore call a
phase makes to a budget. Unlike `fakes.py`, this module records calls: what it
exists to show is how each call was made.
"""

from __future__ import annotations

import functools
from types import SimpleNamespace
from typing import Any, Callable

from fakes import FakeCollectionRef, FakeDocumentRef, FakeFirestore, FakeSnapshot

#: One recorded document call: (kind, path, keyword arguments). `kind` is
#: "get", "set" or "update" for a call made on its own, and "txn.get",
#: "txn.set" or "txn.update" for one made through a transaction.
Call = tuple[str, str, dict[str, Any]]


class _RecordingRef(FakeDocumentRef):
    _db: "TransactionalFirestore"

    def get(self, *args: Any, **kwargs: Any) -> FakeSnapshot:
        self._db.calls.append(("get", self.path, dict(kwargs)))
        if self._db.on_read is not None:
            self._db.on_read(self, False)
        return super().get(*args, **kwargs)

    def set(self, data: dict[str, Any], merge: bool = False, **kwargs: Any) -> None:
        self._db.calls.append(("set", self.path, dict(kwargs)))
        super().set(data, merge=merge)

    def update(self, data: dict[str, Any], **kwargs: Any) -> None:
        self._db.calls.append(("update", self.path, dict(kwargs)))
        super().update(data)

    def collection(self, name: str) -> FakeCollectionRef:
        # Subcollections record too: the task's `events` are written here.
        return _RecordingCollection(self._db, f"{self.path}/{name}")


class _RecordingCollection(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _RecordingRef(self._db, super().document(doc_id).path)


class FirestoreRPCs:
    """What `Transaction` calls on `client._firestore_api`: begin, commit, rollback.

    Each hook, when set, runs inside the call it names and may raise. A hook
    that calls the worker's SIGTERM handler raises `StartupInterrupted` from
    inside the RPC, exactly where a real signal would land in a grpc wait.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_begin: Callable[[], None] | None = None
        self.on_commit: Callable[[], None] | None = None
        self.on_rollback: Callable[[], None] | None = None
        self._begun = 0

    def begin_transaction(self, *, request: Any, metadata: Any, **kwargs: Any) -> Any:
        self.calls.append(("begin", dict(kwargs)))
        if self.on_begin is not None:
            self.on_begin()
        self._begun += 1
        return SimpleNamespace(transaction=f"txn-{self._begun}".encode())

    def commit(self, *, request: Any, metadata: Any, **kwargs: Any) -> Any:
        self.calls.append(("commit", dict(kwargs)))
        if self.on_commit is not None:
            self.on_commit()
        return SimpleNamespace(write_results=[], commit_time=None)

    def rollback(self, *, request: Any, metadata: Any, **kwargs: Any) -> None:
        self.calls.append(("rollback", dict(kwargs)))
        if self.on_rollback is not None:
            self.on_rollback()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@functools.lru_cache(maxsize=None)
def library_transaction_class() -> type:
    """The library's `Transaction`, with document IO on the in-memory store.

    Its `_begin`, `_commit` and `_rollback`, and the retry loop around them in
    `_Transactional`, are Firestore's own. Imported on use, like the worker
    imports it, so a test module that never builds one never imports grpc.
    One class for the whole run, as the library has one `Transaction`.
    """
    from google.cloud.firestore_v1.transaction import Transaction

    class OnTheFakeStore(Transaction):
        def get(self, ref: Any, *args: Any, **kwargs: Any) -> Any:
            db = self._client
            db.calls.append(("txn.get", ref.path, dict(kwargs)))
            if db.on_read is not None:
                db.on_read(ref, True)
            # A generator, as `Transaction.get` returns: it goes through
            # `get_all`, which streams.
            return iter([FakeSnapshot(ref.path, db.documents.get(ref.path))])

        def set(self, ref: Any, data: dict[str, Any], merge: bool = False) -> None:
            self._client.calls.append(("txn.set", ref.path, {}))
            # The store's own semantics, without recording a second, plain set.
            FakeDocumentRef.set(ref, data, merge=merge)

        def update(self, ref: Any, data: dict[str, Any]) -> None:
            self._client.calls.append(("txn.update", ref.path, {}))
            FakeDocumentRef.update(ref, data)

    return OnTheFakeStore


class TransactionalFirestore(FakeFirestore):
    """The in-memory store, and also the `client` a library `Transaction` needs.

    `db.transaction()` returns the library's `Transaction` (see
    `library_transaction_class`), which is what `FirestoreTransactionRunner`
    asks the client for.
    """

    #: What `Transaction` puts in each RPC request. Any string does.
    _database_string = "projects/swarm-test/databases/swarm"
    _rpc_metadata: tuple[Any, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self._firestore_api = FirestoreRPCs()
        self.calls: list[Call] = []
        #: Runs on every document read before it returns, with the reference
        #: and whether the read went through a transaction. A test raises
        #: from it to make Firestore unreachable in one phase.
        self.on_read: Callable[[Any, bool], None] | None = None

    @property
    def rpcs(self) -> FirestoreRPCs:
        return self._firestore_api

    def collection(self, name: str) -> FakeCollectionRef:
        return _RecordingCollection(self, name)

    def document(self, path: str) -> FakeDocumentRef:
        return _RecordingRef(self, path)

    def transaction(self) -> Any:
        return library_transaction_class()(self)
