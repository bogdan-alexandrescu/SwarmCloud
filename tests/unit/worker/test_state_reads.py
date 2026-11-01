"""Every other worker module reaches its documents through the state routes when the switch is on.

Lane ISO-WORKER-READS (docs/design/tenant-settings-integrity.md §4.2, §6;
part of #1457). A tenant switched to the state routes builds no Firestore
client (`__main__.build_worker` hands its modules the `StateCall` as `db`,
lane ISO-WIRE); the modules that still read or wrote Firestore directly -- `secrets`, `jevkey`, `inputs`, `indexrun`,
`graphstage`, `judge.attempt`, the merge slot and train, the human park --
take the attempt's `statecall.StateCall` in its place and then make no
Firestore call. The red-first rules this file holds:

  * with the state client, each module answers what it answered from
    Firestore, from documents swarm-api's REAL routes served
    (`state_routes_harness`: the worker's own `ChildApi`, answered in
    process by swarm-api's app over the control-plane suite's in-memory
    Firestore);
  * while it does, `google.cloud.firestore.Client` raises, and so does any
    Firestore client a test could have handed it (`RaisingFirestore`): no
    Firestore call is made;
  * `RemoteMergeSlots` passes the `MergeSlots` tests -- the same `SlotUse`
    scenario, run against `FirestoreMergeSlots` and against the routes, ends
    in the same slot and the same record;
  * a superseded attempt (409) is `FencedWriteRefused` and nothing is written;
  * with the switch off -- a Firestore client in `db` -- every module reads
    Firestore exactly as before (the `RaisingFirestore` control proves the
    direct path still makes its call).

Offline: no credentials, no emulator.
"""

from __future__ import annotations

import hashlib
import secrets as stdlib_secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import graphstage, handoff, indexrun, inputs, jevkey, mergeslot, mergetrain
from agent_worker import secrets as worker_secrets
from agent_worker.children import PROOF_HEADER
from agent_worker.errors import FencedError, FencedWriteRefused, InputUnavailable
from agent_worker.judge import attempt as judge_attempt
from agent_worker.statecall import StateCall
from swarm_common.states import ParkReason, TaskState

from control_plane.fakes import FakeFirestore as ApiFirestore
from fakes import FakeFirestore, FakeTransactionRunner
from state_routes_harness import (
    ATTEMPT,
    LEASE,
    TASK,
    TENANT,
    RoutesOpener,
    api_client,
    remote_control,
    seed_attempt,
)

OWNER, REPO = "example-org", "widgets"
REPOSITORY = f"{OWNER}/{REPO}"
URL = f"https://github.com/{REPOSITORY}"
SUBMITTER = "alice@example.com"
BASE = "main"
REPO_ID = indexrun.repo_id_for(TENANT, OWNER, REPO)


def _sha(n: int) -> str:
    return hashlib.sha1(str(n).encode()).hexdigest()


def _digest(n: int) -> str:
    return "sha256:" + hashlib.sha256(str(n).encode()).hexdigest()


class RaisingFirestore:
    """A Firestore client that fails the test on any call at all."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"a Firestore call was made (db.{name})")


@pytest.fixture(autouse=True)
def no_firestore_client(monkeypatch):
    """No module may build a Firestore client of its own on the route path."""
    from google.cloud import firestore

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a Firestore client was built")

    monkeypatch.setattr(firestore, "Client", refuse)


@pytest.fixture
def api_db() -> ApiFirestore:
    return ApiFirestore()


@pytest.fixture
def opener(api_db) -> RoutesOpener:
    return RoutesOpener(api_client(api_db))


def _opened(api_db: ApiFirestore, opener: RoutesOpener, **extra: Any) -> StateCall:
    """This attempt's state client, after `state/open` registered its key."""
    seed_attempt(api_db, **{"repository_url": URL, "submitted_by": SUBMITTER, **extra})
    control = remote_control(opener, **{k: extra[k] for k in ("task_id", "attempt_id",
                                                              "lease_id", "generation")
                                        if k in extra})
    control.validate_generation()
    state = control._calls
    assert isinstance(state, StateCall) and state.opened
    return state


def _register_repository(db: Any, *, tenant: str = TENANT, versions: int = 2,
                         current: int | None = 1, **fields: Any) -> list[str]:
    """`repositories/<repo_id>` and `versions` promoted versions; their shas."""
    docs = db.docs if hasattr(db, "docs") else db.documents
    docs[f"repositories/{REPO_ID}"] = {
        "tenant_id": tenant, "repo_id": REPO_ID,
        "index": {"current_sha": _sha(current)} if current else {}, **fields,
    }
    shas = []
    for n in range(1, versions + 1):
        docs[f"repositories/{REPO_ID}/index_versions/{_sha(n)}"] = {
            "commit_sha": _sha(n), "task_id": f"task_index_{n}", "digest": _digest(n),
            "graph_digest": _digest(100 + n), "built_at": f"2026-10-0{n}T00:00:00+00:00",
        }
        shas.append(_sha(n))
    return shas


# ---------------------------------------------------------------------------
# indexrun: the switch, the registration, its versions
# ---------------------------------------------------------------------------


def test_only_the_attempts_state_client_selects_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    assert indexrun.state_routes(state) is state
    assert indexrun.state_routes(RaisingFirestore()) is None
    assert indexrun.state_routes(FakeFirestore()) is None
    assert indexrun.state_routes(None) is None


def test_the_route_version_bound_is_swarm_apis_kept_count_plus_one():
    from swarm_api.repoindex import KEPT_VERSIONS

    assert indexrun.ROUTE_VERSIONS_SERVED == KEPT_VERSIONS + 1
    assert graphstage.MAX_VERSIONS_READ <= indexrun.ROUTE_VERSIONS_SERVED


def test_the_promoted_base_is_served_past_the_routes_bound(api_db, opener):
    """A repository may keep up to 100 versions (`index.keep_versions`, lane
    INDEX-RETENTION); the route serves ROUTE_VERSIONS_SERVED of them, and the
    promoted one -- the base an incremental run builds on -- is always among
    them, wherever its id sorts."""
    state = _opened(api_db, opener)
    count = indexrun.ROUTE_VERSIONS_SERVED + 9
    shas = _register_repository(api_db, versions=count, current=None)
    # Past the bound whether the store answers in id order (Firestore) or in
    # insertion order (the fake).
    bound = indexrun.ROUTE_VERSIONS_SERVED
    late = [commit for commit in shas[bound:] if commit in sorted(shas)[bound:]]
    assert late, "the fixture must give a version past the bound in both orders"
    promoted = late[0]
    api_db.docs[f"repositories/{REPO_ID}"]["index"] = {"current_sha": promoted}
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    # MUTATION: `AttemptState.repository` serving only the first versions by id.
    base = indexrun.resolve_base(state, tenant_id=TENANT, where=where, base_sha=promoted)
    assert isinstance(base, indexrun.BaseVersion) and base.sha == promoted


def test_the_worker_reads_no_version_for_retention():
    """Lane INDEX-RETENTION (#1693): the kept commits come from the SIGNED
    prompt, so retention reads nothing, through the routes or Firestore."""
    own = _sha(99)
    task_input = {"prompt": f"x\n{indexrun.KEEP_LINE}{_sha(1)} {_sha(2)}\n"}
    # MUTATION: a `kept_commits` that takes a database again.
    assert indexrun.kept_commits(task_input, own=own) == sorted({_sha(1), _sha(2), own})


def test_another_tenants_registration_is_not_read_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    _register_repository(api_db, tenant="research")
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    assert isinstance(indexrun.resolve_base(state, tenant_id=TENANT, where=where,
                                            base_sha=_sha(1)), str)


def test_resolve_base_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    _register_repository(api_db, versions=2)
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    base = indexrun.resolve_base(state, tenant_id=TENANT, where=where, base_sha=_sha(2))
    assert base == indexrun.BaseVersion(sha=_sha(2), task_id="task_index_2",
                                        digest=_digest(2), graph_digest=_digest(102))
    missing = indexrun.resolve_base(state, tenant_id=TENANT, where=where, base_sha=_sha(7))
    assert isinstance(missing, str) and "no promoted index" in missing


def test_a_registration_other_than_the_signed_one_is_refused(api_db, opener):
    """swarm-api serves the SIGNED repository's registration; `where` built
    from any other URL is answered with a reason, never with that one."""
    state = _opened(api_db, opener)
    _register_repository(api_db)
    elsewhere = indexrun.target(TENANT, "https://github.com/example-org/gadgets")
    assert not isinstance(elsewhere, str)
    reason = indexrun.resolve_base(state, tenant_id=TENANT, where=elsewhere, base_sha=_sha(1))
    assert isinstance(reason, str) and elsewhere.repo_id in reason


def test_switch_off_indexrun_still_reads_firestore():
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    with pytest.raises(AssertionError, match="Firestore call"):
        indexrun.resolve_base(RaisingFirestore(), tenant_id=TENANT, where=where,
                              base_sha=_sha(1))
    db = FakeFirestore()
    _register_repository(db, versions=2)
    base = indexrun.resolve_base(db, tenant_id=TENANT, where=where, base_sha=_sha(2))
    assert isinstance(base, indexrun.BaseVersion) and base.task_id == "task_index_2"


# ---------------------------------------------------------------------------
# secrets: the tenant document and the forge grant
# ---------------------------------------------------------------------------


def test_load_tenant_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    api_db.docs[f"tenants/{TENANT}"]["max_active"] = 7
    tenant = worker_secrets.load_tenant(state, TENANT)
    assert tenant.tenant_id == TENANT and tenant.max_active == 7
    assert ("GET", "context") in opener.calls


def test_load_tenant_refuses_a_tenant_document_naming_another_tenant(api_db, opener):
    state = _opened(api_db, opener)
    api_db.docs[f"tenants/{TENANT}"]["tenant_id"] = "research"
    with pytest.raises(worker_secrets.SecretError):
        worker_secrets.load_tenant(state, TENANT)


def test_switch_off_load_tenant_still_reads_firestore():
    with pytest.raises(AssertionError, match="Firestore call"):
        worker_secrets.load_tenant(RaisingFirestore(), TENANT)


def _user_hex(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def _grant(db: ApiFirestore, *, mode: str = "write") -> str:
    hashed = _user_hex(SUBMITTER)
    db.docs[f"forge_grants/{TENANT}__{hashed}__{REPO_ID}"] = {
        "tenant_id": TENANT, "user_hash": hashed, "repo_id": REPO_ID, "mode": mode}
    return f"git-u-{hashed}"


def test_the_forge_grant_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    suffix = _grant(api_db, mode="write")
    assert worker_secrets.grant_refusal(state, tenant_id=TENANT, suffix=suffix,
                                        repository_url=URL, write=True,
                                        submitted_by=SUBMITTER) is None
    api_db.docs[f"forge_grants/{TENANT}__{_user_hex(SUBMITTER)}__{REPO_ID}"]["mode"] = "read"
    refusal = worker_secrets.grant_refusal(state, tenant_id=TENANT, suffix=suffix,
                                           repository_url=URL, write=True,
                                           submitted_by=SUBMITTER)
    assert refusal is not None and "'read'" in refusal


def test_a_slot_that_is_not_the_submitters_reads_as_no_grant(api_db, opener):
    """The route serves only the grant derived from the SIGNED submitter."""
    state = _opened(api_db, opener)
    _grant(api_db)
    other = f"git-u-{_user_hex('mallory@example.com')}"
    refusal = worker_secrets.grant_refusal(state, tenant_id=TENANT, suffix=other,
                                           repository_url=URL, write=False,
                                           submitted_by=SUBMITTER)
    assert refusal is not None and "removed" in refusal


# ---------------------------------------------------------------------------
# jevkey: which TypeSafe keys are set
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.secrets: list[str] = []
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def register_secret(self, value: str) -> None:
        self.secrets.append(value)

    def warning(self, msg: str, **kw: Any) -> None:
        self.lines.append((msg, kw))

    info = warning


class _Keys:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.asked: list[str] = []

    def access(self, secret: str) -> str:
        self.asked.append(secret)
        return self.values[secret]


def test_jev_key_presence_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    personal = "u-alice"
    api_db.docs[f"tenants/{personal}"] = {"tenant_id": personal, "kind": "user",
                                          "principal": SUBMITTER}
    tenant_key = jevkey.secret_id(TENANT)
    user_key = jevkey.secret_id(personal)
    api_db.docs[f"provider_keys/{tenant_key}"] = {"set": True}
    api_db.docs[f"provider_keys/{user_key}"] = {"set": True}
    value = "k-" + stdlib_secrets.token_hex(8)
    keys = _Keys({user_key: value, tenant_key: "k-" + stdlib_secrets.token_hex(8)})
    log = _Log()
    found = jevkey.resolve(db=state, client=keys, tenant_id=TENANT, submitted_by=SUBMITTER,
                           logger=log)
    assert found == (value, jevkey.USER, user_key)
    assert log.secrets == [value]


def test_an_unset_jev_key_is_absent_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    api_db.docs[f"provider_keys/{jevkey.secret_id(TENANT)}"] = {"set": False}
    keys = _Keys({})
    assert jevkey.resolve(db=state, client=keys, tenant_id=TENANT, submitted_by=SUBMITTER,
                          logger=_Log()) is None
    assert keys.asked == []


def test_switch_off_jevkey_still_reads_firestore():
    with pytest.raises(AssertionError, match="Firestore call"):
        jevkey.resolve(db=RaisingFirestore(), client=_Keys({}), tenant_id=TENANT,
                       submitted_by=SUBMITTER, logger=_Log())


# ---------------------------------------------------------------------------
# inputs: the upstream task and the CI-fix query
# ---------------------------------------------------------------------------


def test_a_declared_upstream_through_the_routes(api_db, opener):
    state = _opened(api_db, opener, depends_on=["task_up"])
    api_db.docs["tasks/task_up"] = {"id": "task_up", "tenant_id": TENANT,
                                    "state": TaskState.SUCCEEDED.value}
    document = inputs.fetch_upstream_task(state, upstream_task_id="task_up", tenant_id=TENANT)
    assert document["state"] == TaskState.SUCCEEDED.value


def test_an_undeclared_or_foreign_upstream_is_input_unavailable(api_db, opener):
    state = _opened(api_db, opener, depends_on=["task_theirs"])
    api_db.docs["tasks/task_theirs"] = {"id": "task_theirs", "tenant_id": "research"}
    api_db.docs["tasks/task_other"] = {"id": "task_other", "tenant_id": TENANT}
    for upstream in ("task_theirs", "task_other"):
        with pytest.raises(InputUnavailable):
            inputs.fetch_upstream_task(state, upstream_task_id=upstream, tenant_id=TENANT)


def test_a_declared_upstream_with_no_document_is_input_unavailable(api_db, opener):
    state = _opened(api_db, opener, depends_on=["task_gone"])
    with pytest.raises(InputUnavailable, match="no task document"):
        inputs.fetch_upstream_task(state, upstream_task_id="task_gone", tenant_id=TENANT)


def test_ci_fix_tasks_through_the_routes(api_db, opener):
    state = _opened(api_db, opener)
    for task_id, tenant in (("task_fix_1", TENANT), ("task_fix_2", TENANT),
                            ("task_fix_r", "research")):
        api_db.docs[f"tasks/{task_id}"] = {
            "id": task_id, "tenant_id": tenant,
            "metadata": {"ci_fix": {"pull_request": 12}, "dispatch": {"continues": "task_root"}},
        }
    found = inputs.find_ci_fix_tasks(state, tenant_id=TENANT, pull_request=12,
                                     root_task_id="task_root", limit=10)
    assert sorted(task_id for task_id, _doc in found) == ["task_fix_1", "task_fix_2"]
    assert all(doc["tenant_id"] == TENANT for _id, doc in found)


def test_switch_off_inputs_still_read_firestore():
    with pytest.raises(AssertionError, match="Firestore call"):
        inputs.fetch_upstream_task(RaisingFirestore(), upstream_task_id="t", tenant_id=TENANT)


# ---------------------------------------------------------------------------
# graphstage and the judge: the settings and the registration
# ---------------------------------------------------------------------------


def test_graph_staging_reads_its_settings_through_the_routes(api_db, opener, tmp_path):
    state = _opened(api_db, opener)
    _register_repository(api_db)
    api_db.docs[f"workspace_settings/{TENANT}"] = {"code_intelligence": {"context": "off"}}
    result = graphstage.stage(db=state, store=None, tenant_id=TENANT, repository_url=URL,
                              task_id=TASK, checkout=None, base_sha=_sha(1),
                              context_dir=tmp_path)
    assert not result.staged
    assert result.reason == "graph context is off (workspace setting)"


def test_graph_staging_reaches_its_candidates_through_the_routes(api_db, opener, tmp_path):
    """Context on, no checkout to prove ancestry with: the refusal names the
    promoted version the route served, so the candidates were read."""
    state = _opened(api_db, opener)
    _register_repository(api_db, versions=2, current=2)
    api_db.docs[f"workspace_settings/{TENANT}"] = {
        "code_intelligence": {"context": "on", "context_ab": "on"}}
    result = graphstage.stage(db=state, store=None, tenant_id=TENANT, repository_url=URL,
                              task_id=TASK, checkout=None, base_sha=_sha(50),
                              context_dir=tmp_path)
    assert not result.staged
    assert result.decision is not None and result.decision.stage
    assert "staging failed" not in result.reason, result.reason


def test_served_candidates_put_the_current_version_first():
    rows = [{"id": _sha(n), "commit_sha": _sha(n), "graph_digest": _digest(n),
             "built_at": f"2026-10-0{n}T00:00:00+00:00"} for n in (1, 2, 3)]
    found = graphstage._served_candidates(rows, {"current_sha": _sha(1)})
    assert [v.sha for v in found] == [_sha(1), _sha(3), _sha(2)]


def test_switch_off_graph_staging_still_reads_firestore(tmp_path):
    result = graphstage.stage(db=RaisingFirestore(), store=None, tenant_id=TENANT,
                              repository_url=URL, task_id=TASK, checkout=None,
                              base_sha=_sha(1), context_dir=tmp_path)
    assert result.reason == "staging failed (AssertionError)"


def test_the_judge_reads_its_settings_through_the_routes(api_db, opener, tmp_path,
                                                         monkeypatch):
    state = _opened(api_db, opener)
    _register_repository(api_db, hard_stops=["infra/"])
    api_db.docs[f"workspace_settings/{TENANT}"] = {"code_intelligence": {"jev": "on"}}
    seen: list[tuple[Any, Any]] = []

    def judge_settings(workspace: Any, registration: Any) -> Any:
        seen.append((workspace, registration))
        raise RuntimeError("stop here")

    monkeypatch.setattr(judge_attempt, "judge_settings", judge_settings)
    work = tmp_path / "work"
    work.mkdir()
    assert judge_attempt.setup(work=work, db=state, tenant_id=TENANT, repository_url=URL) == {}
    [(workspace, registration)] = seen
    assert workspace == {"code_intelligence": {"jev": "on"}}
    assert registration is not None and registration["repo_id"] == REPO_ID


def test_switch_off_the_judge_still_reads_firestore(tmp_path, monkeypatch):
    seen: list[str] = []

    class Log:
        def warning(self, msg: str, **kw: Any) -> None:
            seen.append(kw.get("error", ""))

    work = tmp_path / "work"
    work.mkdir()
    assert judge_attempt.setup(work=work, db=RaisingFirestore(), tenant_id=TENANT,
                               repository_url=URL, log=Log()) == {}
    assert seen == ["AssertionError"]


# ---------------------------------------------------------------------------
# the merge train entry
# ---------------------------------------------------------------------------


def _fields(task_id: str = TASK) -> dict[str, Any]:
    return {"task_id": task_id, "repository": REPOSITORY, "repository_url": URL,
            "base": BASE, "number": 12, "head": _sha(5), "title": "Widgets",
            "message": "provenance"}


def test_the_train_entry_through_the_routes_is_the_one_firestore_would_hold(api_db, opener):
    state = _opened(api_db, opener)
    trains = mergetrain.RemoteTrainEntries(state, tenant_id=TENANT, task_id=TASK)
    assert trains.peek(TASK) is None
    entry = trains.register(_fields())
    entry_id = mergetrain.entry_id_for(TENANT, TASK)
    stored = api_db.docs[f"{mergetrain.ENTRIES_COLLECTION}/{entry_id}"]

    db = FakeFirestore()
    direct = mergetrain.FirestoreTrainEntries(db, tenant_id=TENANT,
                                              run_transaction=FakeTransactionRunner(db).run,
                                              fence=lambda _t: None)
    expected = direct.register(_fields())
    for key in (*mergetrain.REGISTERED_FIELDS, "state", "returns", "entry_id"):
        assert stored[key] == expected[key], key
        assert entry[key] == expected[key], key
    assert (trains.peek(TASK) or {}).get("state") == mergetrain.QUEUED


def test_another_steps_entry_is_refused_before_any_call(api_db, opener):
    state = _opened(api_db, opener)
    trains = mergetrain.RemoteTrainEntries(state, tenant_id=TENANT, task_id=TASK)
    sent = len(opener.calls)
    with pytest.raises(mergetrain.EntryTenantMismatch):
        trains.peek("task_someone_else")
    with pytest.raises(mergetrain.EntryTenantMismatch):
        trains.register(_fields("task_someone_else"))
    assert len(opener.calls) == sent


def test_a_superseded_attempt_registers_no_train_entry(api_db, opener):
    state = _opened(api_db, opener)
    api_db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    trains = mergetrain.RemoteTrainEntries(state, tenant_id=TENANT, task_id=TASK)
    with pytest.raises(FencedWriteRefused):
        trains.register(_fields())
    assert not [p for p in api_db.docs if p.startswith(mergetrain.ENTRIES_COLLECTION)]


# ---------------------------------------------------------------------------
# the merge slot: the MergeSlots tests, against both stores
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
A, B = "task_slot_a", "task_slot_b"


class FirestoreWorld:
    """`FirestoreMergeSlots` over the worker suite's in-memory Firestore."""

    def __init__(self) -> None:
        self.db = FakeFirestore()
        for minute, task_id in enumerate((A, B)):
            self.db.seed(f"tasks/{task_id}", {"id": task_id, "tenant_id": TENANT,
                                              "state": TaskState.RUNNING.value,
                                              "created_at": T0 + timedelta(minutes=minute),
                                              "metadata": {}})

    def use(self, task_id: str, attempt: str) -> mergeslot.SlotUse:
        slots = mergeslot.FirestoreMergeSlots(
            self.db, tenant_id=TENANT, run_transaction=FakeTransactionRunner(self.db).run,
            fence=lambda _t: None)
        submitted = self.task(task_id)["created_at"]
        return mergeslot.SlotUse(
            slots, mergeslot.Claimant(task_id, attempt, submitted), REPOSITORY,
            task_state=lambda t: TaskState(self.task(t)["state"]), base=BASE)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.db.doc(f"tasks/{task_id}")

    def slot(self) -> dict[str, Any]:
        return self.db.doc(f"{mergeslot.MERGE_SLOTS_COLLECTION}/"
                           f"{mergeslot.slot_id(TENANT, REPOSITORY, BASE)}")

    def next_attempt(self, task_id: str) -> None:
        self.task(task_id)["state"] = TaskState.RUNNING.value


class RoutesWorld:
    """`RemoteMergeSlots`: each claimant its own attempt, over the real routes."""

    def __init__(self, api_db: ApiFirestore, opener: RoutesOpener) -> None:
        self.db = api_db
        self.opener = opener
        self.generations = {A: 1, B: 1}
        for minute, task_id in enumerate((A, B)):
            self._seed(task_id, created_at=T0 + timedelta(minutes=minute))

    def _seed(self, task_id: str, **extra: Any) -> None:
        g = self.generations[task_id]
        seed_attempt(self.db, task_id=task_id, attempt_id=f"att_{task_id}_{g}",
                     lease_id=f"lease_{task_id}_{g}", generation=g, repository_url=URL,
                     **extra)

    def use(self, task_id: str, attempt: str) -> mergeslot.SlotUse:
        g = self.generations[task_id]
        control = remote_control(self.opener, task_id=task_id, attempt_id=f"att_{task_id}_{g}",
                                 lease_id=f"lease_{task_id}_{g}", generation=g)
        control.validate_generation()
        slots = mergeslot.RemoteMergeSlots(control._calls, tenant_id=TENANT)
        submitted = self.task(task_id)["created_at"]

        def never(_t: str) -> TaskState:
            raise AssertionError("the route path reads no task state itself")

        return mergeslot.SlotUse(slots, mergeslot.Claimant(task_id, attempt, submitted),
                                 REPOSITORY, task_state=never, base=BASE)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.db.docs[f"tasks/{task_id}"]

    def slot(self) -> dict[str, Any]:
        return self.db.docs[f"{mergeslot.MERGE_SLOTS_COLLECTION}/"
                            f"{mergeslot.slot_id(TENANT, REPOSITORY, BASE)}"]

    def next_attempt(self, task_id: str) -> None:
        self.generations[task_id] += 1
        created = self.task(task_id)["created_at"]
        metadata = self.task(task_id).get("metadata")
        self._seed(task_id, created_at=created, metadata=metadata)


@pytest.fixture(params=["firestore", "routes"])
def world(request, api_db, opener):
    if request.param == "firestore":
        return FirestoreWorld()
    return RoutesWorld(api_db, opener)


def _park_for_slot(world: Any, task_id: str) -> None:
    """What the lifecycle's slot-wait park leaves on the task."""
    world.task(task_id).update(
        state=TaskState.PARKED.value, park_reason=ParkReason.CI_PENDING.value,
        metadata={"merge_wait": {"code": mergeslot.MERGE_SLOT_WAIT}})


def test_merge_slots_hold_queue_hand_on_and_wake(world):
    first = world.use(A, "att_a")
    taken = first.acquire(updates=1)
    assert taken == mergeslot.Held(generation=1, updates_at_acquire=1)
    first.confirm()

    second = world.use(B, "att_b")
    waiting = second.acquire(updates=0)
    assert isinstance(waiting, mergeslot.Waiting)
    assert (waiting.holder, waiting.position, waiting.waiters) == (A, 1, 1)
    _park_for_slot(world, B)

    assert first.release("merged") == B
    assert first.record["handed_to"] == B
    assert first.record["woke"] == [{"task_id": B, "marked": True}]
    wait = world.task(B)["metadata"]["merge_wait"]
    assert wait["wake_reason"] == mergeslot.SLOT_WAKE_REASON and wait["wake_requested_at"]
    holder = world.slot()["holder"]
    assert holder["task_id"] == B and holder["generation"] == 2
    with pytest.raises(mergeslot.SlotLost):
        first.confirm()

    world.next_attempt(B)
    again = world.use(B, "att_b2")
    assert again.renew(updates=3) is not None
    assert again.held is not None and again.held.generation == 2
    again.confirm()
    assert again.release("merged") is None
    assert world.slot()["holder"] is None


def test_a_step_with_nothing_in_the_slot_releases_without_a_write(world):
    world.use(A, "att_a").acquire(updates=0)
    before = dict(world.slot())
    assert world.use(B, "att_b").release("cancelled") is None
    assert world.slot() == before


def test_a_superseded_attempt_takes_no_slot_through_the_routes(api_db, opener):
    """Invariant 5: the route's fence is a 409, `FencedWriteRefused`."""
    routes = RoutesWorld(api_db, opener)
    use = routes.use(A, "att_a")
    api_db.docs[f"tasks/{A}"]["current_generation"] = 5
    with pytest.raises(FencedError):
        use.acquire(updates=0)
    assert not [p for p in api_db.docs if p.startswith(mergeslot.MERGE_SLOTS_COLLECTION)]


def test_another_tenants_slot_is_never_read_through_the_routes(api_db, opener):
    routes = RoutesWorld(api_db, opener)
    use = routes.use(A, "att_a")
    slot = mergeslot.slot_id(TENANT, REPOSITORY, BASE)
    api_db.docs[f"{mergeslot.MERGE_SLOTS_COLLECTION}/{slot}"] = {"tenant_id": "research"}
    with pytest.raises(mergeslot.SlotTenantMismatch):
        use.acquire(updates=0)
    assert api_db.docs[f"{mergeslot.MERGE_SLOTS_COLLECTION}/{slot}"] == {"tenant_id": "research"}


# ---------------------------------------------------------------------------
# the human park
# ---------------------------------------------------------------------------


def _running_control(api_db: ApiFirestore, opener: RoutesOpener, **extra: Any):
    seed_attempt(api_db, **extra)
    control = remote_control(opener)
    control.validate_generation()
    control.advance_to_running()
    return control


def test_the_human_park_is_one_state_park(api_db, opener):
    control = _running_control(api_db, opener)
    park = handoff.park_human_required(control, max_resumes=handoff.MAX_HUMAN_RESUMES,
                                       detail={"park_phase": "human_wait"})
    assert park == handoff.HumanPark(True, None)
    task = api_db.docs[f"tasks/{TASK}"]
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert api_db.docs[f"leases/{LEASE}"]["released_at"] is not None
    assert [r for _m, r in opener.calls].count("park") == 1


def test_a_persons_return_requeues_through_the_routes(api_db, opener):
    control = _running_control(api_db, opener)
    api_db.docs[f"tasks/{TASK}"]["metadata"] = {
        handoff.HUMAN_RETURNED_METADATA_KEY: {"attempt_id": ATTEMPT, "generation": 1,
                                             "by": SUBMITTER}}
    park = handoff.park_human_required(control, max_resumes=handoff.MAX_HUMAN_RESUMES)
    assert park.returned_by is not None
    assert api_db.docs[f"tasks/{TASK}"]["state"] == TaskState.READY.value


def test_the_worker_and_the_route_bound_human_resumes_alike():
    from swarm_api import attemptstate

    assert handoff.MAX_HUMAN_RESUMES == attemptstate.HUMAN_MAX_RESUMES


def test_a_superseded_attempt_writes_no_human_park(api_db, opener):
    control = _running_control(api_db, opener)
    api_db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    with pytest.raises(FencedError):
        handoff.park_human_required(control, max_resumes=handoff.MAX_HUMAN_RESUMES)
    assert api_db.docs[f"tasks/{TASK}"]["state"] == TaskState.RUNNING.value
    assert api_db.docs[f"leases/{LEASE}"]["released_at"] is None


# ---------------------------------------------------------------------------
# nothing above reached Firestore from the worker
# ---------------------------------------------------------------------------


def test_the_route_paths_never_touch_the_firestore_they_were_not_handed(api_db, opener,
                                                                       tmp_path):
    """The state client is all each module is handed: a RaisingFirestore
    anywhere it could have reached would have failed. Every read above went
    out as a signed state call, and every one carried the attempt proof."""
    state = _opened(api_db, opener, depends_on=["task_up"])
    _register_repository(api_db)
    api_db.docs["tasks/task_up"] = {"id": "task_up", "tenant_id": TENANT}
    where = indexrun.target(TENANT, URL)
    assert not isinstance(where, str)
    worker_secrets.load_tenant(state, TENANT)
    inputs.fetch_upstream_task(state, upstream_task_id="task_up", tenant_id=TENANT)
    indexrun.resolve_base(state, tenant_id=TENANT, where=where, base_sha=_sha(1))
    graphstage.stage(db=state, store=None, tenant_id=TENANT, repository_url=URL, task_id=TASK,
                     checkout=None, base_sha=_sha(1), context_dir=Path(tmp_path))
    routes = [r for _m, r in opener.calls]
    assert routes[0] == "open"
    assert {"context", "upstream", "repository"} <= set(routes[1:])
    signed = opener.headers[1:]
    assert all(any(k.lower() == PROOF_HEADER.lower() for k in h) for h in signed)
