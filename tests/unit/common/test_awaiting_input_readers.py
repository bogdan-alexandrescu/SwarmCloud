"""Contract request 92: every reader of HUMAN_REQUIRED decides about AWAITING_INPUT (HW-CONTRACT, part of #1436).

docs/design/human-steps.md §3.4. `HUMAN_REQUIRED` already has readers that act
on it -- a 24-hour dead-letter sweep, `human-ready`'s promotion to READY, the
browser bell, the plugin's wording -- and each would do the wrong thing to an
`await-input` step. The parity script and the UI's exhaustive tables hold
every restatement of the enum; what they cannot catch is a reader that names
`HUMAN_REQUIRED` in a branch and silently skips the new member. So:

  * every non-test file under `apps/` and `scripts/` that names
    `HUMAN_REQUIRED` must name `AWAITING_INPUT`, or be on `MUST_IGNORE` below
    with its reason. A new file that names one and not the other fails here
    until someone decides which it is;
  * the three readers that must ignore it are held behaviourally: the
    scheduler's human-wait sweep leaves a 25-hour-old AWAITING_INPUT park
    alone, `human-ready` on one is 409, and the `handoffs` bell derives
    nothing for one.

MUTATION: delete `apps/swarm-mcp/swarm_mcp/progress.py`'s AWAITING_INPUT row
and `test_every_reader_of_human_required_decides_about_awaiting_input` fails
on that file; `test_the_scan_refuses_a_reader_that_has_not_decided` holds the
scan itself against a synthetic tree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.conftest import (  # noqa: F401  (fixtures, by name)
    _fresh_quota_ledger,
    api_context,
    auth_header,
    client,
    db,
    dispatcher,
    group_map,
    make_scheduler,
    objects,
    seed_task,
    seed_tenant,
    tokens,
)
from scheduler.loop import DrainReport
from swarm_api import notifications as notif
from swarm_api.store import Store
from swarm_common.states import ParkReason

REPO = Path(__file__).resolve().parents[3]
ROOTS = ("apps", "scripts")

OLD = ParkReason.HUMAN_REQUIRED.value
NEW = ParkReason.AWAITING_INPUT.value

#: Source files only: a test that names HUMAN_REQUIRED tests the hand-off.
SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".jq")
SKIP_DIRS = {"node_modules", "dist", "build", ".venv", "__pycache__", "__tests__", "tests"}

#: The files that name HUMAN_REQUIRED and must IGNORE AWAITING_INPUT, each
#: with why. Every one is about the live-browser hand-off (contract request
#: 68), which an await-input step never has: no container runs it (request
#: 93), so it has no worker, no browser and no attempt.
MUST_IGNORE: dict[str, str] = {
    "apps/agent-worker/agent_worker/control.py":
        "the worker's own human park; no worker runs a control_plane profile, "
        "so no worker ever parks AWAITING_INPUT",
    "apps/agent-worker/agent_worker/handoff.py":
        "the worker half of the live-browser hand-off; an await-input step has "
        "no worker and no browser",
    "apps/agent-worker/agent_worker/lifecycle.py":
        "names the hand-off's park in a docstring; no lifecycle runs an await-input step",
    "apps/scheduler/scheduler/loop.py":
        "`_expire_human_waits` must leave AWAITING_INPUT alone (held below); the "
        "step's own expiry, which honours on_expire, is lane HW-SCHED's awaitinput.py",
    "apps/swarm-api/swarm_api/attemptstate.py":
        "the worker's park route; its refund rule is the hand-off's. No worker "
        "code parks AWAITING_INPUT, but the route accepts one from a worker "
        "token today (with next_eligible_at); its refusal is outside "
        "HW-CONTRACT's territory and is the PR's question 2, with a tested patch",
    "apps/swarm-api/swarm_api/codec.py":
        "a comment on a PARKED(HUMAN_REQUIRED) task's human_wait; the park "
        "reason itself is decoded through the enum, which carries the new member",
    "apps/swarm-api/swarm_api/handoff.py":
        "`human-ready` promotes only a hand-off park; an await-input step is "
        "ended by its answer, never promoted (held below: 409)",
    "apps/swarm-api/swarm_api/live.py":
        "the live-browser session; an await-input step has no browser",
    "apps/swarm-api/swarm_api/notifications.py":
        "the `handoffs` bell links to the Browser tab (held below: nothing for "
        "an await-input step); its own source, `awaiting_input`, is lane HW-NOTIFY's",
    "apps/swarm-api/swarm_api/routes/tasks.py":
        "the `human-ready` route's docstring",
    "apps/swarm-api/swarm_api/validation.py":
        "the hand-off's reserved resume counter key",
    "apps/swarm-mcp/swarm_mcp/check.py":
        "`/sc check`'s STUCK rule for a hand-off park; the `awaiting_input` "
        "item kind, listed as information, is lane HW-MCP's",
    "apps/swarm-mcp/swarm_mcp/server.py":
        "`/sc check`'s help text for the hand-off row; lane HW-MCP's",
    "apps/swarm-ui/src/AgentSplit.tsx":
        "a merge held by the owner's questions has no Browser tab (#2313), read "
        "through `ownerQuestionsHold`; an await-input step is no browser either",
    "apps/swarm-ui/src/QuestionsCard.tsx":
        "a merge held by the owner's questions (#2313) and an agent's blocking "
        "questions; an await-input step is answered on its own card (lane HW-UI's)",
    "apps/swarm-ui/src/AgentDetail.tsx":
        "the live-browser hand-off banner; the await-input card is lane HW-UI's",
    "apps/swarm-ui/src/Handoff.tsx":
        "the live-browser hand-off view; the await-input card is lane HW-UI's",
    "apps/swarm-ui/src/LiveBrowser.tsx":
        "the live browser; an await-input step has none",
    "scripts/acceptance/groups/live-browser.sh":
        "the live-browser acceptance group's stuck-state comment: a hand-off whose "
        "hold ran out parks on HUMAN_REQUIRED; the group submits a "
        "claude-code-browser task, never an await-input step",
}


def _is_source(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if path.suffix not in SUFFIXES or not path.is_file():
        return False
    if any(part in SKIP_DIRS for part in rel.parts[:-1]):
        return False
    name = path.name
    return not (name.startswith("test_") or ".test." in name or ".spec." in name)


def undecided(root: Path, ignore: dict[str, str]) -> list[str]:
    """Files under `root`'s ROOTS that name OLD, not NEW, and are not in `ignore`."""
    found: list[str] = []
    for top in ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not _is_source(path, root):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if OLD not in text or NEW in text:
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in ignore:
                found.append(rel)
    return found


def readers(root: Path) -> list[str]:
    """Every source file that names OLD, so the scan can be shown to have run."""
    out: list[str] = []
    for top in ROOTS:
        for path in sorted((root / top).rglob("*")):
            if _is_source(path, root) and OLD in path.read_text(encoding="utf-8", errors="replace"):
                out.append(path.relative_to(root).as_posix())
    return out


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------


def test_every_reader_of_human_required_decides_about_awaiting_input():
    seen = readers(REPO)
    # On 2026-10-17 the design counted the readers; fewer than ten found means
    # the scan is reading the wrong tree, not that the readers went away.
    assert len(seen) >= 10, seen
    assert undecided(REPO, MUST_IGNORE) == [], (
        "these files name HUMAN_REQUIRED and not AWAITING_INPUT, and are not on "
        "MUST_IGNORE: decide whether each must handle the await-input park "
        "(docs/design/human-steps.md §3.4)"
    )


def test_the_restatements_name_both():
    """The files this lane edited are deciders, not ignorers."""
    for rel in (
        "apps/common/swarm_common/states.py",
        "apps/swarm-ui/src/types.ts",
        "apps/swarm-ui/src/components/StatePill.tsx",
        "apps/swarm-mcp/swarm_mcp/progress.py",
        "apps/swarm-mcp/swarm_mcp/compact.py",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert OLD in text and NEW in text, rel
        assert rel not in MUST_IGNORE, rel


def test_every_ignored_file_exists():
    """A renamed reader leaves its exemption behind; the new path must decide."""
    missing = [rel for rel in MUST_IGNORE if not (REPO / rel).is_file()]
    assert missing == []
    assert all(reason.strip() for reason in MUST_IGNORE.values())


def test_the_scan_refuses_a_reader_that_has_not_decided(tmp_path):
    """The control: a synthetic tree, so the scan is shown to fail."""
    (tmp_path / "apps" / "svc").mkdir(parents=True)
    (tmp_path / "apps" / "svc" / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    reader = tmp_path / "apps" / "svc" / "reader.py"
    reader.write_text(f'if task.park_reason == "{OLD}":\n    promote(task)\n', encoding="utf-8")
    decided = tmp_path / "apps" / "svc" / "decided.ts"
    decided.write_text(f"const a = '{OLD}'\nconst b = '{NEW}'\n", encoding="utf-8")
    listed = tmp_path / "scripts" / "listed.sh"
    listed.write_text(f"echo {OLD}\n", encoding="utf-8")
    a_test = tmp_path / "apps" / "svc" / "tests" / "test_reader.py"
    a_test.write_text(f"assert '{OLD}'\n", encoding="utf-8")

    assert undecided(tmp_path, {"scripts/listed.sh": "a reason"}) == ["apps/svc/reader.py"]
    assert undecided(tmp_path, {}) == ["apps/svc/reader.py", "scripts/listed.sh"]
    reader.write_text(f"# {OLD} and {NEW}\n", encoding="utf-8")
    assert undecided(tmp_path, {"scripts/listed.sh": "a reason"}) == []


# --------------------------------------------------------------------------
# The three readers that must leave it alone
# --------------------------------------------------------------------------


TASK = "task_await"
ALICE = "alice@example.com"


def _awaiting(db, *, hours: float = 25) -> dict:
    """A PARKED(AWAITING_INPUT) await-input step, parked `hours` ago."""
    then = datetime.now(timezone.utc) - timedelta(hours=hours)
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="PARKED",
                    runner_profile="await-input", park_reason=NEW,
                    next_eligible_at=then, created_at=then, workflow_id="wf_await")
    doc.update({"submitted_by": ALICE, "attempt_count": 0})
    return doc


def test_the_human_wait_sweep_leaves_a_25_hour_awaiting_input_park_alone(db, make_scheduler):
    seed_tenant(db, "eng")
    doc = _awaiting(db, hours=25)
    before = dict(doc)
    scheduler = make_scheduler()
    report = DrainReport()
    assert scheduler._expire_human_waits(report) == 0
    assert doc == before
    assert doc["state"] == "PARKED" and doc["park_reason"] == NEW
    assert not any(path.startswith(f"tasks/{TASK}/events/") for path in db.docs)


def test_the_human_wait_sweep_still_ends_a_25_hour_human_park(db, make_scheduler):
    """The control: the same task under HUMAN_REQUIRED is dead-lettered."""
    seed_tenant(db, "eng")
    doc = _awaiting(db, hours=25)
    doc["park_reason"] = OLD
    doc["runner_profile"] = "mock"
    assert make_scheduler()._expire_human_waits(DrainReport()) == 1
    assert doc["state"] == "DEAD_LETTERED"


def test_human_ready_on_an_awaiting_input_step_is_409(client, db):
    seed_tenant(db, "eng")
    doc = _awaiting(db, hours=1)
    response = client.post(f"/v1/tasks/{TASK}/human-ready", headers=auth_header("alice"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "not_waiting_for_a_person"
    assert doc["state"] == "PARKED" and doc["park_reason"] == NEW


def _reading(db) -> notif.Reading:
    viewer = notif.Viewer(email=ALICE, tenant_id="eng", is_admin=False, is_owner=False)
    return notif.Reading(db=db, store=Store(db), viewer=viewer, now=datetime.now(timezone.utc))


def test_the_handoffs_bell_derives_nothing_for_an_awaiting_input_step(db):
    seed_tenant(db, "eng")
    doc = _awaiting(db, hours=1)
    assert notif.handoffs(_reading(db)) == []
    # The control: the same task under HUMAN_REQUIRED rings the bell.
    doc["park_reason"] = OLD
    assert [i["id"] for i in notif.handoffs(_reading(db))] == [f"task:handoff:{TASK}:0"]
