"""A `ready` pull request boards the SwarmCloud merge train (lane 7, docs/design/merging-without-a-laptop.md §6.2 B).

In a registered repository whose effective mode is `train`, a pull request
that carries `ready` and that no merge step registered boards when it is
open, not a draft, not a fork, on the lane's base, green at its head with
the repository's own required checks, mergeable, and the LAST `labeled
ready` event on it was made by an account with write or higher (read with
the collaborator-permission call, so a triage account cannot board one).
Merge-step entries board first; labelled pull requests in label order. One
that cannot board is told why once per head and reason; an ejected one is
told why and loses `ready`.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

from swarm_api import mergetrain

from .test_merge_train_coordinator import API, OWNER, _entry, _ride, clock, ctx  # noqa: F401
from .test_merge_train_gather_platform import (  # noqa: F401
    at, github, register, settings, tick,
)

WRITER = "writer-account"
TRIAGE = "triage-account"


def _ready(github, number: int, by: str = WRITER, *, minute: int = 0) -> str:
    head = github.open_pull(number)
    github.pulls[number]["labels"] = []
    github.label(number, by, minute=minute)
    return head


def _label_entry(db, number: int) -> dict | None:
    entry_id = mergetrain.label_entry_id_for("eng", f"{OWNER}/widgets", number)
    return db.docs.get(f"{mergetrain.ENTRIES_COLLECTION}/{entry_id}")


def _setup(db, github, **values):
    settings(db, **{"gather_min": 1, **values})
    register(db)
    github.permissions = {WRITER: "write", TRIAGE: "read", "maintainer": "admin"}


def test_a_ready_pull_request_labelled_by_a_write_collaborator_boards(ctx, db, github, clock):
    _setup(db, github)
    head = _ready(github, 31)
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.boarded == 1 and report.composed == 1
    entry = _label_entry(db, 31)
    assert entry["source"] == mergetrain.SOURCE_LABEL
    assert entry["head"] == head and entry["state"] == mergetrain.RIDING
    assert entry["tenant_id"] == "eng"
    at(clock, 2)
    tick(ctx, clock)
    assert github.main_landed == [31]
    assert _label_entry(db, 31)["state"] == mergetrain.LANDED


def test_a_pull_request_labelled_by_a_triage_account_does_not_board(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 32, by=TRIAGE)
    for minute in (0, 2, 4):
        at(clock, minute)
        report = tick(ctx, clock)
        assert report.boarded == 0 and report.composed == 0
    assert _label_entry(db, 32) is None
    # Told once for this head and reason, not once a tick.
    told = [body for number, body in github.comments if number == 32]
    assert len(told) == 1 and "labeller_not_writer" in told[0]
    assert "write access" in told[0]


def test_the_last_label_decides_who_boarded_it(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 33, by=WRITER, minute=0)
    github.label(33, TRIAGE, minute=1, event="unlabeled")
    github.label(33, TRIAGE, minute=2)
    at(clock, 3)
    assert tick(ctx, clock).boarded == 0
    github.label(33, "maintainer", minute=4)
    at(clock, 5)
    assert tick(ctx, clock).boarded == 1


def test_a_draft_does_not_board(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 34)
    github.pulls[34]["draft"] = True
    at(clock, 0)
    assert tick(ctx, clock).boarded == 0
    assert _label_entry(db, 34) is None
    assert any(n == 34 and "draft" in body for n, body in github.comments)


def test_a_fork_does_not_board(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 35)
    github.pulls[35]["head"]["repo"] = {"full_name": "someone-else/widgets"}
    at(clock, 0)
    assert tick(ctx, clock).boarded == 0
    assert _label_entry(db, 35) is None


def test_red_or_pending_checks_at_its_head_do_not_board(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 36)
    github.verdict = lambda numbers: None if numbers == frozenset({36}) else "success"
    at(clock, 0)
    assert tick(ctx, clock).boarded == 0
    github.verdict = lambda numbers: "failure" if numbers == frozenset({36}) else "success"
    at(clock, 2)
    assert tick(ctx, clock).boarded == 0
    assert any(n == 36 and "checks_red" in body for n, body in github.comments)


def test_a_conflicting_pull_request_does_not_board(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 37)
    github.pulls[37]["mergeable"] = False
    at(clock, 0)
    assert tick(ctx, clock).boarded == 0


def test_a_repository_in_direct_mode_boards_nothing(ctx, db, github, clock):
    settings(db, gather_min=1, mode="direct")
    register(db)
    github.permissions = {WRITER: "write"}
    _ready(github, 38)
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.boarded == 0 and report.lanes == 0
    assert not github.path_calls("GET", f"{API}/pulls")


def test_another_tenants_registration_boards_nothing_here(ctx, db, github, clock):
    settings(db, gather_min=1)
    register(db, tenant_id="research")
    github.permissions = {WRITER: "write"}
    _ready(github, 39)
    at(clock, 0)
    assert tick(ctx, clock).boarded == 0


def test_merge_step_entries_board_before_labelled_pull_requests(ctx, db, github, clock):
    _setup(db, github, train_size=1)
    _ready(github, 40, minute=-30)
    _ride(db, github, 41, minute=0)
    at(clock, 0)
    tick(ctx, clock)
    first = [c[2]["head"] for c in github.path_calls("POST", f"{API}/merges")]
    assert first == [github.pulls[41]["head"]["sha"]]
    for minute in (2, 4, 6):
        at(clock, minute)
        tick(ctx, clock)
    assert github.main_landed == [41, 40]


def test_a_pull_request_that_loses_ready_is_returned(ctx, db, github, clock):
    _setup(db, github, gather_min=2, gather_wait_minutes=60)
    _ready(github, 42)
    at(clock, 0)
    assert tick(ctx, clock).boarded == 1
    github.label(42, WRITER, minute=1, event="unlabeled")
    at(clock, 2)
    tick(ctx, clock)
    entry = _label_entry(db, 42)
    assert entry["state"] == mergetrain.RETURNED and entry["reason"] == "not_ready"


def test_an_ejected_labelled_pull_request_is_told_and_loses_ready(ctx, db, github, clock):
    _setup(db, github)
    _ready(github, 43)
    at(clock, 0)
    assert tick(ctx, clock).composed == 1
    # Green when it boarded; red in the train.
    github.verdict = lambda numbers: "failure" if numbers else "success"
    for minute in (2, 4):
        at(clock, minute)
        tick(ctx, clock)
    entry = _label_entry(db, 43)
    assert entry["state"] == mergetrain.EJECTED
    told = [body for number, body in github.comments if number == 43]
    assert len(told) == 1 and "Ejected" in told[0]
    assert "ready" not in {label["name"] for label in github.pulls[43]["labels"]}
    # Not boarded again at the same head.
    at(clock, 6)
    assert tick(ctx, clock).boarded == 0


def test_an_idle_lane_of_a_registered_repository_costs_one_list_call(ctx, db, github, clock):
    _setup(db, github)
    github.open_pull(44)
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.lanes == 1 and report.boarded == 0
    assert [c[1] for c in github.calls] == [f"{API}/pulls"]
