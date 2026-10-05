"""The daily git token re-verification in the poll pass (docs/git-tokens.md §5.3; lane GT2b).

What is held here:

  * every token x repository it covers is probed at most once a day: a pair
    whose last complete probe is younger than a day is skipped, whoever ran
    that probe (registration, "Verify now" or an earlier pass);
  * one pass probes at most §5.3's forty repositories x three tokens, and
    stops starting probes when the poll's time runs out; what it did not
    reach is left exactly as it was, so the next pass takes it first;
  * a slot that could not be read, or a forge that did not answer, is
    retried after an hour, not on every five-minute tick;
  * a worker's 403 for a capability makes the pair due on the next pass,
    and that probe turns the row `missing` with the step as its evidence,
    until the slot is rotated;
  * expiry is surfaced per §3.4: "expires in N days" from 14 days, red from
    3, "no expiry" as a warning, and a passed expiry makes the record
    `expired` on the next pass even without a probe;
  * `POST /v1/admin/repositories/poll` runs the pass, for the named tenant's
    tokens only, and no value reaches a log line or the response.

The forge is a fake transport and Secret Manager a map; nothing reaches a
network. Every token-shaped value is built at runtime.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.errors import NotFound, ValidationFailed
from swarm_api.gittokens import (
    CHECKS_COLLECTION,
    COLLECTION,
    MAX_GETS_PER_REPOSITORY,
    REVERIFY_EVERY,
    REVERIFY_MAX_PAIRS,
    REVERIFY_RETRY,
    GitTokens,
    GitTokenRecord,
    Scope,
    expiry_status,
    record_for_slot,
    repo_id_for,
    token_id_for,
)
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings, seed_tenant
from .repo_fakes import repo_entry
from .repo_index_fakes import REPOSITORY, HeadPolls, IndexGitHub, sha
from .test_git_token_probe import (
    ProbeForge,
    Slots,
    classic_value,
    fine_grained_value,
    repo_body,
)

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Ticks:
    """A monotonic clock that moves `step` seconds every time it is read."""

    def __init__(self, step: float = 0.0) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def repos(n: int) -> list[tuple[str, str]]:
    names = [f"saga-xyz/repo-{i:02d}" for i in range(n)]
    return [(repo_id_for("eng", name), name) for name in names]


def tenant(db, credentials=("git",)):
    return seed_tenant(db, "eng", credentials=credentials)


def add_repo_token(db, registry: GitTokens, repo_id: str, name: str) -> GitTokenRecord:
    record = record_for_slot("eng", Scope.REPOSITORY, repo_id=repo_id, repository=name,
                             registered_by="alice@saga.xyz", now=T0 - timedelta(days=2))
    registry.register(record)
    return record


def add_user_token(db, registry: GitTokens) -> GitTokenRecord:
    record = record_for_slot("eng", Scope.USER, user="alice@saga.xyz",
                             registered_by="alice@saga.xyz", now=T0 - timedelta(days=2))
    registry.register(record)
    return record


def pair_doc(db, token_id: str, repo_id: str) -> dict[str, Any] | None:
    return db.docs.get(f"{CHECKS_COLLECTION}/{token_id}_{repo_id}")


def record_doc(db, token_id: str) -> dict[str, Any]:
    return db.docs[f"{COLLECTION}/{token_id}"]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(db, clock) -> GitTokens:
    return GitTokens(db, now=clock)


@pytest.fixture
def slots() -> Slots:
    return Slots()


@pytest.fixture
def fake() -> ProbeForge:
    return ProbeForge()


def reverify(registry, ten, targets, slots, fake, **kwargs):
    return registry.reverify(ten, "eng", targets, tokens=slots, send=fake, **kwargs)


DEFAULT = token_id_for("eng", "tenant", "")


# -- sizing ------------------------------------------------------------------------------


def test_the_pass_is_sized_as_section_5_3_says() -> None:
    # Forty repositories x three tokens, each pair at most eight GETs: under a
    # thousand requests a day, against GitHub's 5,000 an hour per token.
    assert REVERIFY_MAX_PAIRS == 40 * 3
    assert REVERIFY_MAX_PAIRS * MAX_GETS_PER_REPOSITORY < 1000
    assert REVERIFY_EVERY == timedelta(days=1)
    assert REVERIFY_RETRY < REVERIFY_EVERY


# -- the daily bound ----------------------------------------------------------------------


def test_each_pair_is_probed_at_most_once_a_day(db, registry, slots, fake, clock) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    targets = repos(3)
    probed_at: dict[str, list[datetime]] = {}
    # Every five minutes for two days, as the scheduler calls the poll.
    for tick in range(0, 2 * 24 * 12 + 1):
        clock.now = T0 + timedelta(minutes=5 * tick)
        before = len(fake.calls)
        report = reverify(registry, ten, targets, slots, fake)
        if len(fake.calls) > before:
            probed_at.setdefault("default", []).append(clock.now)
            assert report.pairs == 3
    # Twice in two days and a tick: at T0, and the first tick a day later.
    assert probed_at["default"] == [T0, T0 + REVERIFY_EVERY, T0 + 2 * REVERIFY_EVERY]
    # The GETs a day cost: at most eight per pair (the account read included).
    assert len(fake.calls) <= 3 * len(targets) * MAX_GETS_PER_REPOSITORY
    for _, repository in targets:
        doc = pair_doc(db, DEFAULT, repo_id_for("eng", repository))
        assert doc is not None and doc["complete"] is True
        assert doc["verified_at"] == T0 + 2 * REVERIFY_EVERY
    assert record_doc(db, DEFAULT)["verified_at"] == T0 + 2 * REVERIFY_EVERY


def test_the_tenant_default_is_created_and_probed_on_the_first_pass(
    db, registry, slots, fake
) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    report = reverify(registry, ten, repos(1), slots, fake)
    assert report.tokens == 1 and report.probed == 1 and report.pairs == 1
    assert record_doc(db, DEFAULT)["state"] == "active"


def test_a_tenant_without_a_git_slot_reads_nothing(db, registry, slots, fake) -> None:
    ten = tenant(db, credentials=())
    report = reverify(registry, ten, repos(2), slots, fake)
    assert report.tokens == 0 and report.pairs == 0
    assert slots.asked == [] and fake.calls == []


# -- skip if fresh ------------------------------------------------------------------------


def test_a_pair_probed_within_the_day_is_skipped(db, registry, slots, fake, clock) -> None:
    ten = tenant(db)
    (repo_id, name), = repos(1)
    slots.put("swarm-tenant-eng-git", classic_value())
    registry.ensure_tenant_default(ten)
    # "Verify now" for the repository, an hour ago.
    clock.now = T0 - timedelta(hours=1)
    registry.probe(ten, "eng", DEFAULT, tokens=slots, send=fake, repository=name)
    fake.calls.clear()
    slots.asked.clear()

    clock.now = T0 + timedelta(hours=22, minutes=55)
    report = reverify(registry, ten, [(repo_id, name)], slots, fake)
    assert report.fresh == 1 and report.pairs == 0 and report.probed == 0
    # Not even the secret was read.
    assert fake.calls == [] and slots.asked == []

    clock.now = T0 + timedelta(hours=23)
    report = reverify(registry, ten, [(repo_id, name)], slots, fake)
    assert report.pairs == 1 and report.fresh == 0
    assert pair_doc(db, DEFAULT, repo_id)["verified_at"] == clock.now


def test_only_the_stale_pairs_of_a_token_are_probed(db, registry, slots, fake, clock) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    first, second = repos(2)
    reverify(registry, ten, [first], slots, fake)
    fake.calls.clear()
    clock.now = T0 + timedelta(hours=2)
    report = reverify(registry, ten, [first, second], slots, fake)
    assert report.pairs == 1 and report.fresh == 1
    repo_reads = [url for _, url, _ in fake.calls if "/repos/" in url]
    assert repo_reads and all(second[1] in url for url in repo_reads)


def test_a_failed_attempt_is_retried_after_an_hour_not_every_tick(
    db, registry, slots, fake, clock
) -> None:
    ten = tenant(db)
    targets = repos(1)
    # No value stored yet: the probe cannot run.
    report = reverify(registry, ten, targets, slots, fake)
    assert report.failed == 1 and slots.asked == ["swarm-tenant-eng-git"]
    clock.now = T0 + timedelta(minutes=5)
    report = reverify(registry, ten, targets, slots, fake)
    assert report.retry_later == 1 and report.probed == 0
    assert slots.asked == ["swarm-tenant-eng-git"]
    # The forge down for a repository: the same back-off.
    slots.put("swarm-tenant-eng-git", classic_value())
    fake.raises["repo"] = OSError("connection reset")
    clock.now = T0 + REVERIFY_RETRY
    report = reverify(registry, ten, targets, slots, fake)
    assert report.failed == 1 and report.pairs == 1
    calls = len(fake.calls)
    clock.now = T0 + REVERIFY_RETRY + timedelta(minutes=5)
    assert reverify(registry, ten, targets, slots, fake).probed == 0
    assert len(fake.calls) == calls
    fake.raises.clear()
    clock.now = T0 + 2 * REVERIFY_RETRY
    report = reverify(registry, ten, targets, slots, fake)
    assert report.pairs == 1 and report.failed == 0
    assert pair_doc(db, DEFAULT, targets[0][0])["complete"] is True


def test_a_revoked_token_is_not_reverified(db, registry, slots, fake) -> None:
    ten = tenant(db)
    (repo_id, name), = repos(1)
    record = add_repo_token(db, registry, repo_id, name)
    registry.revoke("eng", record.token_id, by="root@saga.xyz", allowed=lambda r: None)
    slots.put(record.secret_name, classic_value())
    report = reverify(registry, ten, [(repo_id, name)], slots, fake)
    assert record.secret_name not in slots.asked
    assert pair_doc(db, record.token_id, repo_id) is None
    assert report.tokens == 1  # the default; the revoked record is not counted


# -- the budget cut -------------------------------------------------------------------------


def test_the_pair_cap_leaves_the_rest_for_the_next_pass(db, registry, slots, fake, clock) -> None:
    ten = tenant(db)
    targets = repos(50)
    slots.put("swarm-tenant-eng-git", classic_value())
    user = add_user_token(db, registry)          # unnarrowed: covers all fifty
    slots.put(user.secret_name, classic_value())
    repo_tokens = [add_repo_token(db, registry, rid, name) for rid, name in targets[:25]]
    for record in repo_tokens:
        slots.put(record.secret_name, classic_value())
    total = 50 + 50 + 25

    first = reverify(registry, ten, targets, slots, fake)
    # Exactly the cap; nothing past it was touched.
    assert first.pairs == REVERIFY_MAX_PAIRS
    assert first.deferred == total - REVERIFY_MAX_PAIRS == 5
    written = [path for path in db.docs if path.startswith(CHECKS_COLLECTION + "/")]
    assert len(written) == REVERIFY_MAX_PAIRS
    # The GETs one pass may cost: eight per pair at most.
    assert len(fake.calls) <= REVERIFY_MAX_PAIRS * MAX_GETS_PER_REPOSITORY


def test_a_pass_over_the_cap_takes_the_rest_on_the_next_tick(
    db, registry, slots, fake, clock
) -> None:
    ten = tenant(db)
    targets = repos(50)
    slots.put("swarm-tenant-eng-git", classic_value())
    users = [add_user_token(db, registry)]
    slots.put(users[0].secret_name, classic_value())
    second_user = record_for_slot("eng", Scope.USER, user="bob@saga.xyz",
                                  registered_by="bob@saga.xyz", now=T0 - timedelta(days=2))
    registry.register(second_user)
    slots.put(second_user.secret_name, classic_value())
    total = 3 * 50  # 150 pairs: thirty past the cap

    first = reverify(registry, ten, targets, slots, fake)
    assert first.pairs == REVERIFY_MAX_PAIRS and first.deferred == total - REVERIFY_MAX_PAIRS
    written = [path for path in db.docs if path.startswith(CHECKS_COLLECTION + "/")]
    assert len(written) == REVERIFY_MAX_PAIRS
    # A token cut short is not "verified": its other pairs are still due.
    cut = [r for r in (DEFAULT, users[0].token_id, second_user.token_id)
           if sum(1 for p in written if p.split("/")[1].startswith(r)) < 50]
    assert len(cut) >= 1
    for token_id in cut:
        assert record_doc(db, token_id)["probe_complete"] is not False

    clock.now = T0 + timedelta(minutes=5)
    second = reverify(registry, ten, targets, slots, fake)
    assert second.pairs == total - REVERIFY_MAX_PAIRS
    assert second.fresh == REVERIFY_MAX_PAIRS and second.deferred == 0
    written = [path for path in db.docs if path.startswith(CHECKS_COLLECTION + "/")]
    assert len(written) == total

    clock.now = T0 + timedelta(minutes=10)
    third = reverify(registry, ten, targets, slots, fake)
    assert third.pairs == 0 and third.fresh == total


def test_the_time_budget_stops_the_pass_and_the_next_one_resumes(
    db, registry, slots, fake, clock
) -> None:
    ten = tenant(db)
    targets = repos(10)
    slots.put("swarm-tenant-eng-git", classic_value())
    # Every read of the clock is ten seconds: the probe's own budget check
    # between repositories runs out part-way through the token.
    first = reverify(registry, ten, targets, slots, fake, clock=Ticks(10.0),
                     budget_seconds=45.0)
    assert 0 < first.pairs < len(targets)
    assert first.deferred == len(targets) - first.pairs
    done = [rid for rid, _ in targets if pair_doc(db, DEFAULT, rid) is not None]
    assert len(done) == first.pairs
    # What was not reached is not written as an attempt, and the token is not
    # put into an hour's back-off for running out of time.
    assert record_doc(db, DEFAULT)["probe_complete"] is not False
    assert record_doc(db, DEFAULT)["verified_at"] is None

    clock.now = T0 + timedelta(minutes=5)
    second = reverify(registry, ten, targets, slots, fake, clock=Ticks(0.0))
    assert second.pairs == len(targets) - first.pairs and second.fresh == first.pairs
    assert all(pair_doc(db, DEFAULT, rid) is not None for rid, _ in targets)
    assert record_doc(db, DEFAULT)["verified_at"] == clock.now


def test_no_time_left_probes_nothing(db, registry, slots, fake) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    report = reverify(registry, ten, repos(3), slots, fake, budget_seconds=0.0)
    assert report.pairs == 0 and report.deferred == 3
    assert fake.calls == [] and slots.asked == []


# -- a step's 403 ---------------------------------------------------------------------------


def test_a_steps_403_turns_the_row_missing_on_the_next_pass(db, registry, slots, fake, clock
                                                            ) -> None:
    ten = tenant(db)
    (repo_id, name), = repos(1)
    fake.scopes = None
    fake.repo = repo_body()
    slots.put("swarm-tenant-eng-git", fine_grained_value(), version="1")
    reverify(registry, ten, [(repo_id, name)], slots, fake)
    row = pair_doc(db, DEFAULT, repo_id)["capabilities"]["open_pull_requests"]
    assert row["state"] == "unknown"

    clock.now = T0 + timedelta(hours=2)
    registry.report_refusal("eng", DEFAULT, repo_id=repo_id, capability="open_pull_requests",
                            status=403, step="task_0123456789abcdef/publish")
    calls = len(fake.calls)
    clock.now = T0 + timedelta(hours=2, minutes=5)
    report = reverify(registry, ten, [(repo_id, name)], slots, fake)
    # Fresh by the clock, and probed anyway: the report made it due.
    assert report.pairs == 1 and len(fake.calls) > calls
    row = pair_doc(db, DEFAULT, repo_id)["capabilities"]["open_pull_requests"]
    assert row["state"] == "missing"
    assert "403" in row["reason"]
    assert "task_0123456789abcdef/publish" in row["evidence"] and "403" in row["evidence"]
    # The other rows are the probe's own.
    assert pair_doc(db, DEFAULT, repo_id)["capabilities"]["clone"]["state"] == "ok"

    # It stands on the next day's probe of the same secret...
    clock.now = T0 + timedelta(days=1, hours=3)
    reverify(registry, ten, [(repo_id, name)], slots, fake)
    assert pair_doc(db, DEFAULT, repo_id)["capabilities"]["open_pull_requests"][
        "state"] == "missing"
    # ...and a rotation clears it: the new value is measured afresh.
    slots.put("swarm-tenant-eng-git", fine_grained_value(), version="2")
    clock.now = T0 + timedelta(days=2, hours=4)
    reverify(registry, ten, [(repo_id, name)], slots, fake)
    assert pair_doc(db, DEFAULT, repo_id)["capabilities"]["open_pull_requests"][
        "state"] == "unknown"


def test_a_401_report_makes_the_pair_due_without_overriding_a_row(
    db, registry, slots, fake, clock
) -> None:
    ten = tenant(db)
    (repo_id, name), = repos(1)
    slots.put("swarm-tenant-eng-git", classic_value())
    reverify(registry, ten, [(repo_id, name)], slots, fake)
    clock.now = T0 + timedelta(hours=1)
    registry.report_refusal("eng", DEFAULT, repo_id=repo_id, capability="clone",
                            status=401, step="task_0123456789abcdef/clone")
    clock.now = T0 + timedelta(hours=1, minutes=5)
    assert reverify(registry, ten, [(repo_id, name)], slots, fake).pairs == 1
    assert pair_doc(db, DEFAULT, repo_id)["capabilities"]["clone"]["state"] == "ok"


def test_a_refusal_report_is_checked(db, registry, slots, fake) -> None:
    ten = tenant(db)
    (repo_id, _), = repos(1)
    registry.ensure_tenant_default(ten)
    with pytest.raises(ValidationFailed):
        registry.report_refusal("eng", DEFAULT, repo_id=repo_id, capability="delete_repo",
                                status=403, step="s")
    with pytest.raises(ValidationFailed):
        registry.report_refusal("eng", DEFAULT, repo_id=repo_id, capability="push",
                                status=500, step="s")
    with pytest.raises(NotFound):
        registry.report_refusal("research", DEFAULT, repo_id=repo_id, capability="push",
                                status=403, step="s")


def test_a_reported_step_is_masked_before_it_is_stored(db, registry, slots, fake) -> None:
    ten = tenant(db)
    (repo_id, _), = repos(1)
    registry.ensure_tenant_default(ten)
    leaked = classic_value()
    registry.report_refusal("eng", DEFAULT, repo_id=repo_id, capability="push",
                            status=403, step=f"push with {leaked}")
    assert leaked not in json.dumps(pair_doc(db, DEFAULT, repo_id), default=str)


# -- expiry (§3.4) -------------------------------------------------------------------------


def _expiring(days: float) -> str:
    return (T0 + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")


@pytest.mark.parametrize("days,level", [(30, "ok"), (14, "warning"), (10, "warning"),
                                        (3, "danger"), (1, "danger")])
def test_expiry_levels(db, registry, slots, fake, days, level) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    fake.expiry = _expiring(days)
    report = reverify(registry, ten, repos(1), slots, fake)
    status = expiry_status(GitTokenRecord.from_firestore(record_doc(db, DEFAULT)), T0)
    assert status["level"] == level and status["days"] == days
    if level == "ok":
        assert report.warnings == []
    else:
        (warning,) = report.warnings
        assert warning["token_id"] == DEFAULT and warning["level"] == level
        assert warning["days"] == days and f"{days} day" in warning["message"]
        assert warning["secret_name"] == "swarm-tenant-eng-git"


def test_no_expiry_is_a_warning_and_an_app_token_says_by_design(db, registry, slots, fake
                                                                 ) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    fake.expiry = None
    report = reverify(registry, ten, repos(1), slots, fake)
    (warning,) = report.warnings
    assert warning["level"] == "no_expiry" and warning["days"] is None
    record = GitTokenRecord.from_firestore(record_doc(db, DEFAULT))
    record.kind = "app_installation"
    assert expiry_status(record, T0)["level"] == "by_design"
    # Never probed: nothing is known, and nothing is guessed.
    fresh = record_for_slot("eng", Scope.TENANT, registered_by="x", now=T0)
    assert expiry_status(fresh, T0)["level"] == "unknown"


def test_a_passed_expiry_makes_the_record_expired_without_a_probe(
    db, registry, slots, fake, clock
) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    fake.expiry = _expiring(0.5)
    reverify(registry, ten, repos(1), slots, fake)
    assert record_doc(db, DEFAULT)["state"] == "active"
    calls = len(fake.calls)
    clock.now = T0 + timedelta(hours=13)
    report = reverify(registry, ten, repos(1), slots, fake)
    assert len(fake.calls) == calls  # still fresh: no probe
    assert record_doc(db, DEFAULT)["state"] == "expired"
    (warning,) = report.warnings
    assert warning["level"] == "expired"


def test_the_record_serves_its_expiry(db, registry, slots, fake) -> None:
    ten = tenant(db)
    slots.put("swarm-tenant-eng-git", classic_value())
    fake.expiry = _expiring(5)
    reverify(registry, ten, repos(1), slots, fake)
    body = GitTokenRecord.from_firestore(record_doc(db, DEFAULT)).to_api([], now=T0)
    assert body["expiry"]["level"] == "warning" and body["expiry"]["days"] == 5


# -- through the poll route, and no secret in logs ------------------------------------------


SWEEPER = "swarm-rollup-sweeper@example-project.iam.gserviceaccount.com"


class Both:
    """The poll's head reads to HeadPolls, everything else to the probe's forge."""

    def __init__(self, heads: HeadPolls, probe: ProbeForge) -> None:
        self.heads = heads
        self.probe = probe

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        if "/commits/" in url and "/check-runs" not in url:
            return self.heads(url, headers, timeout)
        return self.probe(url, headers, timeout)


@pytest.fixture
def poll_client(db, tokens, group_map, objects, slots, fake, clock):
    seed_tenant(db, "eng", credentials=("git",))
    seed_tenant(db, "research", credentials=("git",))
    github = IndexGitHub(heads={"main": sha("one")},
                         repos={REPOSITORY: repo_entry(REPOSITORY, default_branch="main")})
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    ctx = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=slots,
        forge=forge.GitHubIssues(send=github, probe_send=Both(HeadPolls(github), fake)),
        now=clock,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_the_poll_pass_reverifies_the_named_tenant_only_and_logs_no_secret(
    poll_client, db, slots, fake, caplog
) -> None:
    from .conftest import auth_header

    caplog.set_level(logging.DEBUG)
    value = classic_value()
    other = classic_value()
    slots.put("swarm-tenant-eng-git", value)
    slots.put("swarm-tenant-research-git", other)
    created = poll_client.post("/v1/repositories", json={"repository": REPOSITORY},
                               headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    chatty = logging.getLogger("tests.chatty_transport")

    def careless(url: str, headers: dict[str, str]) -> None:
        chatty.debug("sending %s with %s", url, headers.get("Authorization"))
        chatty.debug("raw token in message: " + value)

    fake.on_call = careless
    fake.status["checks"] = 403
    fake.body["checks"] = {"message": f"Bad credentials for {value}"}
    fake.expiry = _expiring(2)
    slots.asked.clear()

    response = poll_client.post("/v1/admin/repositories/poll?tenant_id=eng",
                                headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    body = response.json()
    git_tokens = body["report"]["git_tokens"]
    assert git_tokens["pairs"] == 1 and git_tokens["probed"] == 1
    assert [w["level"] for w in git_tokens["warnings"]] == ["danger"]
    doc = pair_doc(db, DEFAULT, repo_id)
    assert doc is not None and doc["tenant_id"] == "eng"
    assert doc["capabilities"]["read_checks"]["state"] == "missing"
    # Only the named tenant's slot was read.
    assert set(slots.asked) == {"swarm-tenant-eng-git"}
    research_default = token_id_for("research", "tenant", "")
    assert f"{COLLECTION}/{research_default}" not in db.docs

    assert any(rec.name == "tests.chatty_transport" for rec in caplog.records)
    dumped = json.dumps(body)
    assert value not in dumped and value[4:] not in dumped
    for record in caplog.records:
        message = record.getMessage()
        assert value not in message and value[4:] not in message, message
    for path, stored in db.docs.items():
        if path.startswith((COLLECTION + "/", CHECKS_COLLECTION + "/")):
            assert value not in json.dumps(stored, default=str)


def test_a_failing_reverification_never_fails_the_poll(poll_client, db, slots, monkeypatch
                                                        ) -> None:
    from .conftest import auth_header

    slots.put("swarm-tenant-eng-git", classic_value())
    created = poll_client.post("/v1/repositories", json={"repository": REPOSITORY},
                               headers=auth_header("alice"))
    assert created.status_code == 201, created.text

    def broken(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(GitTokens, "reverify", broken)
    response = poll_client.post("/v1/admin/repositories/poll?tenant_id=eng",
                                headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"]["registrations"] == 1
    assert body["report"]["git_tokens"] == {"error": "RuntimeError"}


def test_an_unreadable_slot_is_a_failed_probe_not_a_failed_poll(poll_client, db, slots) -> None:
    from .conftest import auth_header

    slots.put("swarm-tenant-eng-git", classic_value())
    created = poll_client.post("/v1/repositories", json={"repository": REPOSITORY},
                               headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    slots.raises = RuntimeError("secret manager is down")
    response = poll_client.post("/v1/admin/repositories/poll?tenant_id=eng",
                                headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    assert response.json()["report"]["git_tokens"]["failed"] == 1
