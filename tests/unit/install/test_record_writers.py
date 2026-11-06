"""One install record, one writer per document (plan §5.4; DAYONE-ENGINE-1).

The engine writes `swarm-install/<env>` and no other document; it reads the
console's documents, which swarm-api writes. Every engine write is conditional
(a create, or an update on the update time it read). SETUP-API-1's
`test_install_record_writers.py` holds swarm-api's side of the same table.

`FakeDocuments` is imported by `test_engine_loop.py`.
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr

from swarm_install.engine import Engine
from swarm_install.events import Events
from swarm_install.lease import Lease
from swarm_install.record import (
    ENGINE,
    WRITERS,
    InstallRecord,
    RecordConflict,
    console_document,
    engine_document,
)
from swarm_install.state import SecretValueError, StateStore
from swarm_install.steps import Observation, Plan

from .test_state_store import FakeBucket, FakeClock


class FakeDocuments:
    """Firestore's create / update-with-precondition, recorded."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict, int]] = {}
        self.writes: list[tuple[str, str]] = []
        self._tick = 0

    def get(self, path: str):
        return self.docs.get(path)

    def create(self, path: str, data: dict) -> None:
        self.writes.append(("create", path))
        if path in self.docs:
            raise RecordConflict(path)
        self._tick += 1
        self.docs[path] = (dict(data), self._tick)

    def update(self, path: str, data: dict, *, update_time) -> None:
        self.writes.append(("update", path))
        if path not in self.docs or self.docs[path][1] != update_time:
            raise RecordConflict(path)
        self._tick += 1
        self.docs[path] = ({**self.docs[path][0], **data}, self._tick)


class _ReadsConsole:
    """A stage-5-and-later step: attaches the record, reads the console."""

    id = "record.attach"
    stage = 5
    satisfies = ("other.install-record-firestore",)

    def __init__(self, documents: FakeDocuments) -> None:
        self.documents = documents
        self.attached = False

    def check(self, ctx):
        return Observation("met" if self.attached else "unmet")

    def plan(self, ctx):
        return Plan("mirror the install state to Firestore")

    def apply(self, ctx, plan):
        ctx.attach_record(InstallRecord(env=ctx.environment, documents=self.documents))
        self.attached = True
        ctx.read_console("github_app")
        ctx.read_console("finished")

    def verify(self, ctx):
        return Observation("met")

    def explain(self):
        return "The console reads the install record."


def test_engine_writes_only_its_document(tmp_path: Path) -> None:
    documents = FakeDocuments()
    github_app = console_document("dev", "github_app")
    documents.docs[github_app] = ({"id": 7, "slug": "swarm-app"}, 1)
    documents._tick = 1
    bucket = FakeBucket()
    clock = FakeClock()
    engine = Engine(
        [_ReadsConsole(documents)],
        store=StateStore(project="p-test", environment="dev", local=tmp_path / "s.json", bucket=bucket),
        lease=Lease(holder="admin@example.com", store=bucket, clock=clock.now),
        events=Events(clock.now, json_lines=True, out=io.StringIO()),
        clock=clock,
        holder="admin@example.com",
    )
    result = engine.run()
    assert result.exit_code == 0, result

    written = {path for _, path in documents.writes}
    assert written == {engine_document("dev")}, f"the engine wrote {written}"
    engine_template = next(t for t, (writer, _) in WRITERS.items() if writer == ENGINE)
    assert engine_template == "swarm-install/{env}"
    assert [t for t, (w, _) in WRITERS.items() if w == ENGINE] == [engine_template], (
        "the engine is the writer of exactly one document"
    )
    fields = set(documents.docs[engine_document("dev")][0])
    assert fields <= WRITERS[engine_template][1], fields - WRITERS[engine_template][1]
    assert documents.docs[github_app] == ({"id": 7, "slug": "swarm-app"}, 1), "read, never written"
    assert bucket.state()["console"]["github_app"] == {"id": 7, "slug": "swarm-app"}, (
        "what the engine reads from the console is copied into its authoritative state"
    )
    stages = documents.docs[engine_document("dev")][0]["stages"]
    assert stages[5]["state"] == "done" and stages[5]["by"] == "admin@example.com"


def test_engine_write_is_conditional_and_retries() -> None:
    from swarm_install.state import InstallState

    documents = FakeDocuments()
    record = InstallRecord(env="dev", documents=documents)
    state = InstallState(project="p-test", environment="dev", region="europe-west1")
    record.write(state, {})
    assert documents.writes == [("create", engine_document("dev"))]

    real_get = documents.get
    raced = {"done": False}

    def racing_get(path):
        found = real_get(path)
        if not raced["done"]:
            raced["done"] = True
            documents._tick += 1  # another CLI wrote between this read and the write
            documents.docs[path] = (documents.docs[path][0], documents._tick)
        return found

    documents.get = racing_get  # type: ignore[method-assign]
    record.write(state, {})
    assert documents.writes[1:] == [
        ("update", engine_document("dev")),
        ("update", engine_document("dev")),
    ], "the stale update is refused and the writer re-reads and writes again"


def test_record_refuses_secret_values() -> None:
    from swarm_install.state import InstallState

    record = InstallRecord(env="dev", documents=FakeDocuments())
    with pytest.raises(SecretValueError):
        record.document_for(
            InstallState(project="p", environment="dev"), {},
            verify={"result": SecretStr(secrets.token_hex(8))},
        )
