"""The install state store (docs/design/day-one-install.md §2.3; DAYONE-ENGINE-1).

On a fake bucket with GCS's generation semantics and no credentials:

* every write carries `ifGenerationMatch`;
* of two writers, one wins and the other re-reads and applies its change on top;
* a state holding a secret value refuses to serialise;
* the local state is uploaded once, when the bucket exists, and after that
  the bucket is authoritative.

`FakeBucket` and `FakeClock` are imported by the other engine tests.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr

from swarm_install.lease import Lease, LeaseHeld, LeaseRefused
from swarm_install.state import (
    STATE_OBJECT,
    InstallState,
    PreconditionFailed,
    SecretValueError,
    StateConflict,
    StateStore,
    StepRecord,
)


class FakeBucket:
    """GCS's generation rules: 0 means "must not exist", n means "must be n"."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.writes: list[tuple[str, int]] = []
        self._next = 1000

    def read(self, name: str) -> tuple[bytes, int] | None:
        return self.objects.get(name)

    def write(self, name: str, data: bytes, *, if_generation_match: int) -> int:
        self.writes.append((name, if_generation_match))
        current = self.objects.get(name)
        if (current[1] if current else 0) != if_generation_match:
            raise PreconditionFailed(name)
        self._next += 1
        self.objects[name] = (data, self._next)
        return self._next

    def delete(self, name: str, *, if_generation_match: int) -> None:
        current = self.objects.get(name)
        if current is None or current[1] != if_generation_match:
            raise PreconditionFailed(name)
        del self.objects[name]

    def state(self) -> dict:
        return json.loads(self.objects[STATE_OBJECT][0])


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _store(tmp_path: Path, bucket: FakeBucket | None, name: str = "a") -> StateStore:
    return StateStore(
        project="p-test", environment="dev", local=tmp_path / name / "p-test.json", bucket=bucket
    )


def _done(step: str) -> StepRecord:
    return StepRecord(state="done", at=1.0, by="admin@example.com")


def test_write_uses_if_generation_match(tmp_path: Path) -> None:
    bucket = FakeBucket()
    store = _store(tmp_path, bucket)
    store.load()
    store.update(lambda s: s.steps.__setitem__("one", _done("one")))
    store.update(lambda s: s.steps.__setitem__("two", _done("two")))

    assert [w for w in bucket.writes if w[0] == STATE_OBJECT] == [
        (STATE_OBJECT, 0),
        (STATE_OBJECT, 1001),
    ], "the first write must create only (0) and the second replace only the generation read"
    assert set(bucket.state()["steps"]) == {"one", "two"}


def test_conflicting_writer_loses(tmp_path: Path) -> None:
    bucket = FakeBucket()
    first = _store(tmp_path, bucket, "first")
    first.load()
    first.update(lambda s: s.steps.__setitem__("base", _done("base")))

    alice = _store(tmp_path, bucket, "alice")
    bob = _store(tmp_path, bucket, "bob")
    alice_state = alice.load()
    bob_state = bob.load()

    alice_state.steps["alice"] = _done("alice")
    alice.save(alice_state)

    bob_state.steps["bob"] = _done("bob")
    with pytest.raises(StateConflict):
        bob.save(bob_state)
    assert "bob" not in bucket.state()["steps"], "the losing write must not land"

    bob.update(lambda s: s.steps.__setitem__("bob", _done("bob")))
    assert set(bucket.state()["steps"]) == {"base", "alice", "bob"}, (
        "the loser re-reads and applies its change on top of the winner's"
    )


def test_no_secret_value_is_stored(tmp_path: Path) -> None:
    value = secrets.token_hex(16)
    state = InstallState(project="p-test", environment="dev")
    state.outputs["github_app_key"] = SecretStr(value)
    with pytest.raises(SecretValueError):
        state.to_bytes()

    bucket = FakeBucket()
    store = _store(tmp_path, bucket)
    store.load()
    with pytest.raises(SecretValueError):
        store.update(lambda s: s.steps.__setitem__(
            "secrets", StepRecord(state="done", at=1.0, by="x", outputs={"v": [SecretStr(value)]})
        ))
    assert STATE_OBJECT not in bucket.objects
    local = tmp_path / "a" / "p-test.json"
    assert not local.exists() or value not in local.read_text()


def test_local_cache_uploaded_once_bucket_exists(tmp_path: Path) -> None:
    store = _store(tmp_path, None)
    store.load()
    store.update(lambda s: s.steps.__setitem__("preflight", _done("preflight")))
    local = tmp_path / "a" / "p-test.json"
    assert local.is_file(), "before the bucket exists the state is local"

    bucket = FakeBucket()
    store.attach_bucket(bucket)
    assert bucket.writes == [(STATE_OBJECT, 0)], "uploaded once, create-only"
    assert set(bucket.state()["steps"]) == {"preflight"}

    # A second run (or a second attach) finds the object: it is authoritative,
    # nothing is uploaded over it, and the local file becomes its cache.
    again = _store(tmp_path, None)
    again.load()
    again.attach_bucket(bucket)
    assert bucket.writes == [(STATE_OBJECT, 0)]

    again.update(lambda s: s.steps.__setitem__("foundations", _done("foundations")))
    assert bucket.writes[-1] == (STATE_OBJECT, 1001)
    assert set(json.loads(local.read_text())["steps"]) == {"preflight", "foundations"}


def test_newer_schema_refused(tmp_path: Path) -> None:
    bucket = FakeBucket()
    bucket.write(STATE_OBJECT, json.dumps(
        {"schema_version": 99, "project": "p-test", "environment": "dev"}
    ).encode(), if_generation_match=0)
    from swarm_install.state import StateTooNew

    with pytest.raises(StateTooNew):
        _store(tmp_path, bucket).load()


def test_lease_one_holder_and_typed_takeover() -> None:
    bucket = FakeBucket()
    clock = FakeClock()
    alice = Lease(holder="alice@example.com", store=bucket, clock=clock.now)
    bob = Lease(holder="bob@example.com", store=bucket, clock=clock.now)
    alice.acquire()
    with pytest.raises(LeaseHeld):
        bob.acquire(lambda prompt: "alice@example.com")

    clock.t += 31 * 60  # alice's lease expired
    with pytest.raises(LeaseRefused):
        bob.acquire(lambda prompt: "yes")
    bob.acquire(lambda prompt: "alice@example.com")
    assert json.loads(bucket.objects["swarm-install/lock"][0])["holder"] == "bob@example.com"
