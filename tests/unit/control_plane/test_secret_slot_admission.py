"""A required workspace-secret slot with no enabled grant parks before the lease (SEC-ADMIT-PARK).

docs/design/workspace-secrets.md §4.2: "A required slot with no enabled grant
parks the task CREDENTIAL_MISSING **before** it leases, at no cost, and it
starts by itself once the grant is ticked." SEC-RELEASE built only the
worker's backstop, which parks after the lease and after a pod has started.
These tests hold the scheduler's half: `credentials.credential_for` asks
`secret_slots.WorkspaceGrants`, admission parks on a no with nothing reserved
(invariants 1, 2 and 3), and the credential sweep promotes once the grant is
enabled.

No catalogue slot is `required` yet (contract request 80), so the required
slots here are built in the fixture -- a copy of `mock` with one slot, and
jev-browser's slot made required -- and never in the catalogue. The last
tests hold the scheduler's derivation of the consumers, and its restated
collection names, to swarm-api's own release.
"""

from __future__ import annotations

from dataclasses import replace
from secrets import token_hex
from types import MappingProxyType

import pytest

from swarm_api import repositories as api_repositories
from swarm_api import workspace_secrets as api_secrets
from swarm_api.grant_attest import REPO_ID_FIELD, GrantKeys, GrantPolicy
from swarm_common import profiles as profiles_mod
from swarm_common.identity import tenant_id_for_user
from swarm_common.models import Tenant
from swarm_common.profiles import RUNNER_PROFILES, RUNTIME_TOOLS, SecretSlot

from scheduler import credentials
from scheduler import dispatch as dispatch_mod
from scheduler import loop as loop_mod
from scheduler import secret_slots
from scheduler.codec import task_from_dict
from scheduler.secret_slots import MissingSlot, missing_required_slots, required_slots

from .conftest import seed_pool, seed_task, seed_tenant

TENANT = "eng"
OTHER = "research"
PERSON = "ada@example.com"
PERSONAL = tenant_id_for_user(PERSON)

#: A test profile: `mock` (no provider key, Cloud Run, standard) plus one
#: slot that is required, and one that is optional.
REQUIRED = "secret-required"
OPTIONAL = "secret-optional"
#: `claude-code` with a required slot: needs a provider key as well.
KEYED = "secret-keyed"


def _slot(required: bool) -> SecretSlot:
    return SecretSlot(kinds=("api_key",), means="a test slot", required=required)


def _profiles() -> dict:
    out = dict(RUNNER_PROFILES)
    out[REQUIRED] = replace(RUNNER_PROFILES["mock"], name=REQUIRED,
                            secret_slots={"files": _slot(True)})
    out[OPTIONAL] = replace(RUNNER_PROFILES["mock"], name=OPTIONAL,
                            secret_slots={"files": _slot(False)})
    out[KEYED] = replace(RUNNER_PROFILES["claude-code"], name=KEYED,
                         secret_slots={"files": _slot(True)})
    return out


@pytest.fixture(autouse=True)
def catalogue(monkeypatch):
    """The fixture profiles, visible to admission and dispatch; and
    jev-browser's `typesafe` slot made required. Nothing in the catalogue
    itself changes."""
    patched = MappingProxyType(_profiles())
    monkeypatch.setattr(loop_mod, "RUNNER_PROFILES", patched)
    monkeypatch.setattr(dispatch_mod, "RUNNER_PROFILES", patched)
    jev = RUNTIME_TOOLS["jev-browser"]
    tools = dict(RUNTIME_TOOLS)
    tools["jev-browser"] = replace(jev, secret_slots={"typesafe": replace(
        jev.secret_slots["typesafe"], required=True)})
    tools = MappingProxyType(tools)
    monkeypatch.setattr(profiles_mod, "RUNTIME_TOOLS", tools)
    monkeypatch.setattr(secret_slots, "RUNTIME_TOOLS", tools)
    return patched


def world(db) -> None:
    seed_pool(db, "global", hard_limit=10)
    seed_tenant(db, TENANT)
    seed_tenant(db, OTHER)


def task(db, task_id: str, *, profile: str = REQUIRED, tenant_id: str = TENANT,
         submitted_by: str | None = None, inputs: dict | None = None, **fields) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, runner_profile=profile, **fields)
    if submitted_by is not None:
        doc["submitted_by"] = submitted_by
    if inputs is not None:
        doc["input"] = inputs
    return doc


def secret_id(tenant_id: str, n: int = 1) -> str:
    return f"{api_secrets.wsec_prefix(tenant_id)}{n:016x}"


def grant(db, consumer: str = f"profile:{REQUIRED}", *, tenant_id: str = TENANT,
          slot: str | None = None, enabled: bool = True, n: int = 1) -> str:
    """A grant document as `SecretStore.set_grant` writes it."""
    sid = secret_id(tenant_id, n)
    db.docs[f"workspace_secret_grants/{sid}~{consumer}"] = {
        "secret_id": sid, "tenant_id": tenant_id, "consumer": consumer,
        "enabled": enabled, "slot": slot, "changed_by": "admin@example.com",
    }
    return sid


def personal_world(db, *, opted_in: bool = True, creator: str = PERSON,
                   consumer: str = f"profile:{REQUIRED}") -> str:
    """Ada's personal tenant, one personal secret in it granted to
    `consumer`, opted in (or not) to `eng`."""
    from dataclasses import asdict
    from datetime import datetime, timezone

    db.docs[f"tenants/{PERSONAL}"] = asdict(Tenant(
        tenant_id=PERSONAL, kind="user", principal=PERSON,
        created_at=datetime.now(timezone.utc),
    ))
    sid = grant(db, consumer, tenant_id=PERSONAL, n=7)
    db.docs[f"workspace_secrets/{sid}"] = {
        "secret_id": sid, "tenant_id": PERSONAL, "name": "my-key", "provider": "other",
        "kind": "api_key", "scope": "personal", "state": "active",
        "current_version": f"projects/123/secrets/{sid}/versions/1",
        "use_in_workspaces": [TENANT] if opted_in else [], "created_by": creator,
    }
    return sid


def parked_detail(db, task_id: str) -> dict:
    prefix = f"tasks/{task_id}/events/"
    events = [doc for path, doc in sorted(db.docs.items())
              if path.startswith(prefix) and doc.get("type") == "parked"]
    assert events, f"{task_id} recorded no parked event"
    return events[-1]["detail"]


def leases(db) -> list[str]:
    return [path for path in db.docs if path.startswith("leases/")]


def assert_reserved_nothing(db, dispatcher) -> None:
    # Invariant 1: no lease, no worker. Invariant 2: no pool moved, so no
    # half-made reservation. Invariant 3: nothing LEASED to count.
    assert leases(db) == []
    assert dispatcher.dispatched == []
    pools = {path: doc["active"] for path, doc in db.docs.items() if path.startswith("pools/")}
    assert pools and all(active == 0 for active in pools.values()), pools


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


def test_a_required_slot_with_no_grant_parks_before_any_lease(db, make_scheduler, dispatcher):
    world(db)
    task(db, "task_no_grant")

    report = make_scheduler().drain()

    doc = db.docs["tasks/task_no_grant"]
    assert doc["state"] == "PARKED", doc
    assert doc["park_reason"] == "CREDENTIAL_MISSING"
    assert report.parked == 1
    detail = parked_detail(db, "task_no_grant")
    # credentials.py's shape, plus the consumer and slot by name.
    assert detail["provider"] is None and "account_pool" in detail
    assert detail["workspace_secrets"] == [{"consumer": f"profile:{REQUIRED}", "slot": "files"}]
    assert "wsec" not in repr(detail)
    assert_reserved_nothing(db, dispatcher)


def test_an_enabled_grant_admits(db, make_scheduler, dispatcher):
    world(db)
    grant(db)
    task(db, "task_granted")

    make_scheduler().drain()

    assert db.docs["tasks/task_granted"]["state"] == "DISPATCHED"
    assert [d["task_id"] for d in dispatcher.dispatched] == ["task_granted"]


def test_a_grant_naming_the_slot_admits_too(db, make_scheduler, dispatcher):
    world(db)
    grant(db, slot="files")
    task(db, "task_named_slot")

    make_scheduler().drain()

    assert db.docs["tasks/task_named_slot"]["state"] == "DISPATCHED"


@pytest.mark.parametrize("why, kwargs", [
    ("a disabled grant is none", {"enabled": False}),
    ("another workspace's grant is not this one's (invariant 9)", {"tenant_id": OTHER}),
    ("a grant to another consumer", {"consumer": f"profile:{OPTIONAL}"}),
    ("a grant to another slot", {"slot": "other"}),
])
def test_a_grant_that_does_not_fill_the_slot_parks(db, make_scheduler, dispatcher, why, kwargs):
    world(db)
    grant(db, **kwargs)
    task(db, "task_not_filled")

    make_scheduler().drain()

    assert db.docs["tasks/task_not_filled"]["state"] == "PARKED", why
    assert db.docs["tasks/task_not_filled"]["park_reason"] == "CREDENTIAL_MISSING"
    assert_reserved_nothing(db, dispatcher)


def test_an_optional_only_slot_admits_with_no_grant(db, make_scheduler, dispatcher):
    world(db)
    task(db, "task_optional", profile=OPTIONAL)

    make_scheduler().drain()

    assert db.docs["tasks/task_optional"]["state"] == "DISPATCHED"


def test_a_launched_tools_required_slot_parks(db, make_scheduler, dispatcher):
    """`browser_tool: jev` launches jev-browser, whose slot the fixture makes
    required. The profile's own `files` slot stays optional."""
    world(db)
    seed_tenant(db, TENANT, credentials=("anthropic",))
    task(db, "task_jev", profile="claude-code-browser", inputs={"browser_tool": "jev"})

    make_scheduler().drain()

    assert db.docs["tasks/task_jev"]["state"] == "PARKED"
    assert parked_detail(db, "task_jev")["workspace_secrets"] == [
        {"consumer": "tool:jev-browser", "slot": "typesafe"}]
    assert_reserved_nothing(db, dispatcher)


def test_which_tools_are_launched_decides_the_slots():
    browser = RUNNER_PROFILES["claude-code-browser"]
    jev = MissingSlot("tool:jev-browser", "typesafe")
    assert missing_required_slots(browser, {"browser_tool": "jev"}, []) == (jev,)
    assert missing_required_slots(browser, {}, []) == (jev,), "absent is auto"
    assert missing_required_slots(browser, {"browser_tool": "playwright"}, []) == ()
    assert missing_required_slots(
        browser, {"browser_tool": "auto"}, [("tool:jev-browser", None)]) == ()
    assert required_slots(RUNNER_PROFILES["claude-code"], {}) == ()


# --------------------------------------------------------------------------
# Q3: a person's own secret on a group task
# --------------------------------------------------------------------------


def test_a_personal_secret_admits_a_group_task_only_under_the_opt_in(
    db, make_scheduler, dispatcher
):
    world(db)
    personal_world(db, opted_in=False)
    task(db, "task_not_opted", submitted_by=PERSON)
    make_scheduler().drain()
    assert db.docs["tasks/task_not_opted"]["state"] == "PARKED"
    assert_reserved_nothing(db, dispatcher)

    personal_world(db, opted_in=True)
    task(db, "task_opted", submitted_by=PERSON)
    make_scheduler().drain()
    assert db.docs["tasks/task_opted"]["state"] == "DISPATCHED"


@pytest.mark.parametrize("why, submitted_by, creator", [
    ("only on the owner's own submissions", "bob@example.com", PERSON),
    ("the record's creator must be the submitter", PERSON, "bob@example.com"),
])
def test_a_personal_secret_is_not_anyone_elses(
    db, make_scheduler, dispatcher, why, submitted_by, creator
):
    world(db)
    personal_world(db, creator=creator)
    task(db, "task_someone_else", submitted_by=submitted_by)

    make_scheduler().drain()

    assert db.docs["tasks/task_someone_else"]["state"] == "PARKED", why


@pytest.mark.parametrize("opted_in", [True, False])
def test_the_q3_rule_matches_swarm_apis_release(db, opted_in):
    """The same personal seed, granted to a real catalogue consumer, is
    released by swarm-api's own `release_plan` (the submitter a member)
    exactly when admission counts it.

    The seed's grants are unattested, as the scheduler's own tests write
    them, so the release runs under the migration-window policy
    (`grant_attestation_required` off, no key): attestation is swarm-api's
    check alone, and this test holds only the Q3 rule both sides share."""
    consumer = "profile:claude-code"
    personal_world(db, opted_in=opted_in, consumer=consumer)
    seed_tenant(db, TENANT)
    plan = api_secrets.release_plan(
        db, tenant_id=TENANT, consumers=(consumer,), project_id="p",
        grants=GrantPolicy(keys=GrantKeys(), required=False),
        personal=(PERSONAL, PERSON), personal_refusal=lambda: None,
    )
    released = [d.name for d in plan.due]

    grants = secret_slots.WorkspaceGrants(db)
    tenants = {t: Tenant(**{k: v for k, v in db.docs[f"tenants/{t}"].items()})
               for t in (TENANT, PERSONAL)}
    task_doc = task(db, "task_q3", profile="claude-code", submitted_by=PERSON)
    counted = list(grants._personal(task_from_dict(task_doc), tenants[TENANT], tenants.get))

    assert released == (["my-key"] if opted_in else [])
    assert counted == ([(consumer, None)] if opted_in else [])


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------


def test_the_sweep_returns_the_park_to_ready_once_the_grant_is_enabled(
    db, make_scheduler, dispatcher
):
    world(db)
    grant(db, enabled=False)
    task(db, "task_waits")
    make_scheduler().drain()
    assert db.docs["tasks/task_waits"]["state"] == "PARKED"

    make_scheduler().drain()
    assert db.docs["tasks/task_waits"]["state"] == "PARKED", "nothing changed"

    grant(db, enabled=True)
    report = make_scheduler().drain()
    assert report.promoted_credentials == 1
    assert db.docs["tasks/task_waits"]["state"] == "DISPATCHED"


def test_the_worker_backstop_park_waits_for_the_grant_too(db, make_scheduler, dispatcher):
    """The worker's own park (`workspace-secret:<slots>`) was promoted on the
    provider answer alone; now it waits for the grant like admission's."""
    world(db)
    task(db, "task_backstop", state="PARKED", park_reason="CREDENTIAL_MISSING")

    report = make_scheduler().drain()
    assert report.promoted_credentials == 0
    assert db.docs["tasks/task_backstop"]["state"] == "PARKED"

    grant(db)
    report = make_scheduler().drain()
    assert report.promoted_credentials == 1


def test_a_provider_key_park_stays_parked_whatever_the_grants_say(
    db, make_scheduler, dispatcher
):
    world(db)
    grant(db, f"profile:{KEYED}")
    task(db, "task_no_key", profile=KEYED, state="PARKED", park_reason="CREDENTIAL_MISSING")

    report = make_scheduler().drain()

    assert report.promoted_credentials == 0
    assert db.docs["tasks/task_no_key"]["state"] == "PARKED"


# --------------------------------------------------------------------------
# One read per pass
# --------------------------------------------------------------------------


class Counting:
    """Wraps the fake and counts each query streamed from the grants collection."""

    def __init__(self, db) -> None:
        self._db = db
        self.grant_queries = 0

    def __getattr__(self, name):
        return getattr(self._db, name)

    def collection(self, path):
        ref = self._db.collection(path)
        if path != secret_slots.GRANTS_COLLECTION:
            return ref
        outer = self

        class Ref:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def where(self, *args, **kwargs):
                return Ref(self._inner.where(*args, **kwargs))

            def stream(self, *args, **kwargs):
                outer.grant_queries += 1
                return self._inner.stream(*args, **kwargs)

        return Ref(ref)


def test_the_grants_are_read_once_per_pass(db, dispatcher):
    from scheduler.dispatch import BackendRouter
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    from .conftest import scheduler_settings

    world(db)
    grant(db)
    for i in range(3):
        task(db, f"task_eng_{i}")
        task(db, f"task_research_{i}", tenant_id=OTHER)
    counting = Counting(db)
    settings = scheduler_settings()
    scheduler = Scheduler(
        settings=settings, store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(), secrets=secret_slots.WorkspaceGrants(counting),
    )

    report = scheduler.drain()

    assert report.passes == 1 and report.scanned == 6
    assert counting.grant_queries == 1
    assert sorted(d["task_id"] for d in dispatcher.dispatched) == [
        f"task_eng_{i}" for i in range(3)]


def test_a_pass_with_no_required_slot_reads_no_grant(db, dispatcher):
    from scheduler.dispatch import BackendRouter
    from scheduler.loop import Scheduler
    from scheduler.metrics import SchedulerMetrics
    from scheduler.store import SchedulerStore

    from .conftest import scheduler_settings

    world(db)
    task(db, "task_plain", profile="mock")
    task(db, "task_optional", profile=OPTIONAL)
    counting = Counting(db)
    settings = scheduler_settings()
    Scheduler(
        settings=settings, store=SchedulerStore(db),
        router=BackendRouter(cloud_run=dispatcher, gke=dispatcher, settings=settings),
        metrics=SchedulerMetrics(), secrets=secret_slots.WorkspaceGrants(counting),
    ).drain()

    assert counting.grant_queries == 0
    assert len(dispatcher.dispatched) == 2


# --------------------------------------------------------------------------
# Held to swarm-api's release
# --------------------------------------------------------------------------


def test_the_restated_names_are_swarm_apis():
    assert secret_slots.GRANTS_COLLECTION == api_secrets.GRANTS_COLLECTION
    assert secret_slots.SECRETS_COLLECTION == api_secrets.SECRETS_COLLECTION
    assert secret_slots.SCOPE_PERSONAL == api_secrets.SCOPE_PERSONAL
    assert secret_slots.ACTIVE == api_secrets.ACTIVE


@pytest.mark.parametrize("profile", sorted(RUNNER_PROFILES))
def test_the_consumers_are_the_releases_slot_declaring_ones(profile):
    """For every catalogue profile and every `browser_tool` value (and none),
    the consumers admission asks about are exactly those the release derives
    (`attempt_consumers`) that declare a slot."""
    declared = {c.consumer for c in api_secrets.CONSUMERS if c.slots}
    choices = [None]
    if "browser_tool" in RUNNER_PROFILES[profile].inputs:
        choices += list(RUNNER_PROFILES[profile].inputs["browser_tool"].choices)
    for choice in choices:
        inputs = {} if choice is None else {"browser_tool": choice}
        released = api_secrets.attempt_consumers({"runner_profile": profile, "input": inputs})
        ours = [c for c, _ in secret_slots.slot_consumers(RUNNER_PROFILES[profile], inputs)]
        assert ours == [c for c in released if c in declared], (profile, choice)


# --------------------------------------------------------------------------
# A repository's secret (SSC-ADMIT-REPO, secrets-scopes.md §2.2, §2.3)
# --------------------------------------------------------------------------

WIDGETS = "example-org/widgets"
GADGETS = "example-org/gadgets"
GIZMOS = "example-org/gizmos"


def repo_id(repository: str, tenant_id: str = TENANT) -> str:
    return api_repositories.repo_id_for(tenant_id, *repository.split("/"))


def register(db, repository: str, *, tenant_id: str = TENANT) -> str:
    owner, repo = repository.split("/")
    rid = repo_id(repository, tenant_id)
    db.docs[f"repositories/{rid}"] = {"repo_id": rid, "tenant_id": tenant_id,
                                      "owner": owner, "repo": repo}
    return rid


def repository_secret(db, repository: str, consumer: str = f"profile:{REQUIRED}", *,
                      name: str = "db-password", key: str | None = None,
                      grant_repo_id: str | None = "record", marked: bool = True) -> str:
    """A repository secret as SSC-REPO-SCOPE writes it: its registration, a
    marked `-wsec-` id, a record of `scope: repository` bound to the
    registration's `repo_id`, and a grant carrying the same `repo_id`, signed
    "v2" when `key` is given. The keyword arguments rewrite one field."""
    rid = register(db, repository)
    tail = api_secrets.repository_hex() if marked else api_secrets.workspace_hex()
    sid = api_secrets.wsec_prefix(TENANT) + tail
    db.docs[f"workspace_secrets/{sid}"] = {
        "secret_id": sid, "tenant_id": TENANT, "name": name, "provider": "other",
        "kind": "api_key", "scope": "repository", "state": "active",
        "repo_id": rid, "repository": repository,
        "current_version": f"projects/123/secrets/{sid}/versions/1",
    }
    doc = {"secret_id": sid, "tenant_id": TENANT, "consumer": consumer, "enabled": True,
           "slot": None, "changed_by": "admin@example.com"}
    if grant_repo_id is not None:
        doc["repo_id"] = rid if grant_repo_id == "record" else grant_repo_id
    if key is not None:
        doc["attestation"] = GrantKeys(key).sign(doc)
    db.docs[f"workspace_secret_grants/{sid}~{consumer}"] = doc
    return sid


def extra(repository: str, *, rid: str | None = None) -> dict:
    """One `additional_repositories` entry, as swarm-api writes it."""
    return {
        "name": repository.split("/")[1], "repo_id": rid or repo_id(repository),
        "repository": repository, "repository_url": f"https://github.com/{repository}",
        "ref": "main", "commit": token_hex(20), "forge_credential": "git",
        "forge_credential_source": "tenant", "size_kib": 0,
    }


def on(db, task_id: str, repository: str | None, *extras: dict, **kwargs) -> dict:
    doc = task(db, task_id, **kwargs)
    doc["repository_url"] = None if repository is None else f"https://github.com/{repository}"
    doc["additional_repositories"] = list(extras) if extras else None
    return doc


def test_a_slot_filled_only_by_another_repositorys_secret_parks(db, make_scheduler, dispatcher):
    world(db)
    repository_secret(db, GADGETS)
    on(db, "task_other_repo", WIDGETS)

    make_scheduler().drain()

    doc = db.docs["tasks/task_other_repo"]
    assert doc["state"] == "PARKED" and doc["park_reason"] == "CREDENTIAL_MISSING", doc
    assert parked_detail(db, "task_other_repo")["workspace_secrets"] == [
        {"consumer": f"profile:{REQUIRED}", "slot": "files"}]
    # Invariant 1: CREDENTIAL_MISSING holds no lease and no capacity.
    assert_reserved_nothing(db, dispatcher)


def test_a_slot_filled_by_this_repositorys_secret_admits(db, make_scheduler, dispatcher):
    world(db)
    repository_secret(db, WIDGETS)
    on(db, "task_this_repo", WIDGETS)

    make_scheduler().drain()

    assert db.docs["tasks/task_this_repo"]["state"] == "DISPATCHED"


def test_a_slot_filled_by_an_additional_repositorys_secret_admits(
    db, make_scheduler, dispatcher
):
    world(db)
    repository_secret(db, GADGETS)
    on(db, "task_extra_repo", WIDGETS, extra(GADGETS))

    make_scheduler().drain()

    assert db.docs["tasks/task_extra_repo"]["state"] == "DISPATCHED"


@pytest.mark.parametrize("why, secret, clones", [
    ("an attempt that clones nothing is released no repository secret",
     {}, (None,)),
    ("an additional entry whose repo_id is not its repository's withholds them all",
     {}, (WIDGETS, "mismatch")),
    ("a grant bound to another repository than its record",
     {"grant_repo_id": "repo_" + "0" * 16}, (WIDGETS,)),
    ("a record rewritten to this repository, its grant still the other's",
     {"rewrite": True}, (WIDGETS,)),
    ("a grant on a repository secret that carries no repo_id",
     {"grant_repo_id": None}, (WIDGETS,)),
    ("a repository-bound grant on a workspace secret's id",
     {"marked": False}, (WIDGETS,)),
])
def test_a_repository_grant_the_release_would_drop_parks(
    db, make_scheduler, dispatcher, why, secret, clones
):
    world(db)
    if secret.get("rewrite"):
        sid = repository_secret(db, GADGETS)
        db.docs[f"workspace_secrets/{sid}"]["repo_id"] = repo_id(WIDGETS)
        register(db, WIDGETS)
    else:
        repository_secret(db, WIDGETS, **secret)
    primary, *rest = clones
    extras = [extra(GIZMOS, rid=repo_id(GADGETS))] if rest == ["mismatch"] else []
    on(db, "task_dropped", primary, *extras)

    make_scheduler().drain()

    assert db.docs["tasks/task_dropped"]["state"] == "PARKED", why
    assert_reserved_nothing(db, dispatcher)


def test_a_removed_registration_no_longer_fills_the_slot(db, make_scheduler, dispatcher):
    world(db)
    repository_secret(db, WIDGETS)
    del db.docs[f"repositories/{repo_id(WIDGETS)}"]
    on(db, "task_unregistered", WIDGETS)

    make_scheduler().drain()

    assert db.docs["tasks/task_unregistered"]["state"] == "PARKED"
    assert_reserved_nothing(db, dispatcher)


def test_q1_one_slot_offered_by_every_scope_admits(db, make_scheduler, dispatcher):
    """The person's, the repository's and the workspace's item of one name:
    the release keeps the person's and withholds the rest as shadowed, and
    the slot is filled once. So admission admits, whichever scope wins."""
    world(db)
    personal_world(db)
    repository_secret(db, WIDGETS, name="my-key")
    grant(db)
    on(db, "task_every_scope", WIDGETS, submitted_by=PERSON)

    make_scheduler().drain()

    assert db.docs["tasks/task_every_scope"]["state"] == "DISPATCHED"


def test_q1_asks_the_repository_before_the_workspace(db):
    """The order of §11 Q1: once the repository's grant fills the slot, the
    workspace's own grants are not asked for it."""
    world(db)
    repository_secret(db, WIDGETS)
    grant(db)
    doc = on(db, "task_order", WIDGETS)
    grants = secret_slots.WorkspaceGrants(db)
    asked: list[str] = []
    real_repository, real_own = grants._repository, grants._own
    grants._repository = lambda t: asked.append("repository") or real_repository(t)
    grants._own = lambda t: asked.append("workspace") or real_own(t)

    missing = grants.missing(task_from_dict(doc), _profiles()[REQUIRED],
                             Tenant(**db.docs[f"tenants/{TENANT}"]), lambda _t: None)

    assert missing == ()
    assert asked == ["repository"]


@pytest.mark.parametrize("why, primary, extras", [
    ("its own repository", WIDGETS, ()),
    ("another repository", GADGETS, ()),
    ("an additional repository", GADGETS, (WIDGETS,)),
    ("an additional entry that does not match", WIDGETS, ("mismatch",)),
    ("no repository", None, ()),
])
def test_the_repository_rule_matches_swarm_apis_release(db, why, primary, extras):
    """The same repository seed, signed "v2", is released by swarm-api's own
    `release_plan` exactly when admission counts it."""
    key = token_hex(32)
    consumer = "profile:claude-code"
    seed_tenant(db, TENANT)
    repository_secret(db, WIDGETS, consumer, key=key)
    entries = [extra(GIZMOS, rid=repo_id(GADGETS)) if e == "mismatch" else extra(e)
               for e in extras]
    doc = on(db, "task_parity", primary, *entries, profile="claude-code")

    plan = api_secrets.release_plan(
        db, tenant_id=TENANT, consumers=(consumer,), project_id="p",
        grants=GrantPolicy(keys=GrantKeys(key), required=False),
        repositories=api_secrets.attempt_repositories(doc),
    )
    counted = secret_slots.WorkspaceGrants(db)._repository(task_from_dict(doc))

    assert bool([d.name for d in plan.due]) == bool(counted), why
    assert counted in ([], [(consumer, None)])


@pytest.mark.parametrize("doc", [
    {"tenant_id": TENANT, "repository_url": "https://github.com/Example-Org/Widgets"},
    {"tenant_id": TENANT, "repository_url": "https://github.com/example-org/widgets.git/"},
    {"tenant_id": TENANT, "repository_url": " https://github.com/example-org/widgets "},
    {"tenant_id": TENANT, "repository_url": "git@github.com:example-org/widgets.git"},
    {"tenant_id": TENANT, "repository_url": "https://www.github.com/example-org/widgets"},
    {"tenant_id": TENANT, "repository_url": "https://github.com/example-org/.hidden"},
    {"tenant_id": TENANT, "repository_url": "https://github.com/-bad/widgets"},
    {"tenant_id": TENANT, "repository_url": "https://github.com/a/b/c"},
    {"tenant_id": TENANT, "repository_url": None},
    {"tenant_id": "", "repository_url": "https://github.com/example-org/widgets"},
    {"tenant_id": TENANT, "additional_repositories": [extra(GADGETS)]},
    {"tenant_id": TENANT, "repository_url": "https://github.com/example-org/gadgets",
     "additional_repositories": [extra(GADGETS), extra(WIDGETS)]},
    {"tenant_id": TENANT, "additional_repositories": [extra(GIZMOS, rid=repo_id(GADGETS))]},
    {"tenant_id": TENANT, "additional_repositories": [{**extra(GADGETS), "repository": "x"}]},
    {"tenant_id": TENANT, "additional_repositories": [extra(GADGETS, rid=repo_id(GADGETS, OTHER))]},
    {"tenant_id": TENANT, "additional_repositories": ["example-org/gadgets"]},
    {"tenant_id": TENANT, "additional_repositories": {"a": 1}},
    {"tenant_id": TENANT, "additional_repositories": []},
])
def test_the_attempts_repositories_are_swarm_apis(doc):
    assert credentials.attempt_repositories(doc) == api_secrets.attempt_repositories(doc)


def test_the_repository_mark_and_names_are_swarm_apis():
    assert secret_slots.SCOPE_REPOSITORY == api_secrets.SCOPE_REPOSITORY
    assert secret_slots.REPO_ID_FIELD == REPO_ID_FIELD
    assert secret_slots.REPOSITORIES_COLLECTION == api_repositories.COLLECTION
    prefix = api_secrets.wsec_prefix(TENANT)
    ids = [prefix + api_secrets.repository_hex() for _ in range(50)] + \
          [prefix + api_secrets.workspace_hex() for _ in range(50)] + \
          [secret_id(TENANT), prefix + "abc", "no-mark-here", None, 7]
    for sid in ids:
        assert secret_slots.is_repository_secret_id(sid) == \
            api_secrets.is_repository_secret_id(sid), sid
