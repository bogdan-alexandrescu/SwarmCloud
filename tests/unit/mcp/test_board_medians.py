"""Historical medians beside each workflow, shown and never used to order (BOARD-ORDER).

Owner decision, 2026-10-16 (#1437): each line shows the historical median of
its current step and of the steps still ahead "as long as that is not a heavy
load on the plugin or the terminal" -- at most a few reads per 15 minutes for
the whole bridge, never a read per workflow per refresh. Held here:

* `board.medians` reads `GET /v1/tasks?state=SUCCEEDED&view=summary&limit=100`,
  at most 3 pages, stops at the week's edge, and takes p50 of started_at to
  completed_at per (runner_profile, step_id);
* one read serves every listing for 15 minutes; a read that fails is said, is
  not retried on every listing, and never fails the listing;
* `board.annotate` puts `median_seconds` on each current step and
  `remaining_median_seconds` / `eta_seconds` on the progress, null where there
  is no history;
* the medians do not move the order.

THE MUTATIONS THESE CATCH: read medians per workflow or per listing (the
request-count tests go red); let a failed read raise (the failure test goes
red); drop the floor at 0 or count parallel steps twice (the eta tests go red).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm_mcp import board, sc

from test_board_order import NOW, FakeAPI, _iso, envelope


@pytest.fixture(autouse=True)
def _no_remembered_medians():
    board.forget_medians()
    yield
    board.forget_medians()


def _succeeded(step: str, minutes: float, *, days_ago: float = 1, profile: str = "claude-code") -> dict:
    end = NOW - timedelta(days=days_ago)
    return {"id": f"t_{step}_{minutes}_{days_ago}", "state": "SUCCEEDED", "runner_profile": profile,
            "step_id": step, "created_at": _iso(end - timedelta(minutes=minutes + 1)),
            "started_at": _iso(end - timedelta(minutes=minutes)), "completed_at": _iso(end)}


class _Pages:
    """`GET /v1/tasks` serving `pages` pages, each with a token, counting reads."""

    def __init__(self, pages: list[list[dict]], *, token_after_last: bool = True) -> None:
        self.pages = pages
        self.token_after_last = token_after_last
        self.paths: list[str] = []
        self.base_url = "https://pages.test"
        self.tenant = None

    def request(self, method, path, **_):
        self.paths.append(path)
        n = len(self.paths) - 1
        last = n >= len(self.pages) - 1
        return {"tasks": self.pages[min(n, len(self.pages) - 1)],
                "next_page_token": None if last and not self.token_after_last else f"p{n + 1}"}


def test_the_median_is_p50_of_started_to_completed_per_profile_and_step():
    pages = _Pages([[_succeeded("review", 10), _succeeded("review", 30), _succeeded("review", 20),
                     _succeeded("implement", 40), _succeeded("implement", 99, profile="codex")]],
                   token_after_last=False)
    found = board.medians(pages, now=NOW)
    assert found["by_step"][("claude-code", "review")] == 20 * 60
    assert found["by_step"][("claude-code", "implement")] == 40 * 60
    assert found["by_step"][("codex", "implement")] == 99 * 60
    (path,) = pages.paths
    query = path.split("?", 1)[1].split("&")
    assert {"state=SUCCEEDED", "view=summary", "limit=100"} <= set(query)


def test_at_most_three_pages_are_read():
    pages = _Pages([[_succeeded("review", 10)]] * 10)
    board.medians(pages, now=NOW)
    assert len(pages.paths) == board.MEDIAN_PAGES == 3
    assert "page_token=p1" in pages.paths[1]


def test_the_walk_stops_at_the_weeks_edge_and_leaves_older_successes_out():
    pages = _Pages([[_succeeded("review", 10), _succeeded("review", 500, days_ago=8)],
                    [_succeeded("review", 10)]])
    found = board.medians(pages, now=NOW)
    assert len(pages.paths) == 1, "a page that reached past 7 days ends the walk"
    assert found["by_step"][("claude-code", "review")] == 10 * 60


def test_one_read_serves_every_listing_for_fifteen_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(board.time, "monotonic", lambda: clock[0])
    pages = _Pages([[_succeeded("review", 10)]], token_after_last=False)
    for _ in range(50):
        board.medians(pages, now=NOW)
        clock[0] += 17  # 50 refreshes over 14 minutes
    assert len(pages.paths) == 1
    clock[0] += 60
    board.medians(pages, now=NOW)
    assert len(pages.paths) == 2, "the next read comes after 15 minutes"


def test_a_failed_read_leaves_the_medians_out_says_so_and_is_not_repeated_every_listing():
    api = FakeAPI([envelope("wf_a", ["RUNNING"])], fail_medians=True)
    listing = sc.running_workflows(api, now=NOW)
    assert listing["count"] == 1, "the listing never fails for want of a median"
    assert "429" in listing["medians"]["unavailable_because"]
    step = listing["workflows"][0]["progress"]["current"][0]
    assert step["median_seconds"] is None and listing["workflows"][0]["progress"]["eta_seconds"] is None
    sc.running_workflows(api, now=NOW)
    assert sum(p.startswith("/v1/tasks?") for p in api.paths) == 1


def test_the_listing_costs_one_median_read_however_many_workflows_run():
    envelopes = [envelope(f"wf_{n:02d}", ["SUCCEEDED", "RUNNING"]) for n in range(60)]
    api = FakeAPI(envelopes, succeeded=[_succeeded("s1", 30)])
    for _ in range(3):
        sc.running_workflows(api, now=NOW)
    assert sum(p.startswith("/v1/tasks?") for p in api.paths) == 1


def test_annotate_shows_each_current_steps_median_and_the_eta():
    env = envelope("wf_a", ["SUCCEEDED", "RUNNING", "QUEUED", "QUEUED"], started_minutes_ago=12)
    entry = {"workflow_id": "wf_a", "progress": board.progress(env, NOW)}
    found = {"by_step": {("claude-code", "s1"): 20 * 60, ("claude-code", "s2"): 5 * 60,
                         ("claude-code", "s3"): 7 * 60}}
    board.annotate(entry, found)
    p = entry["progress"]
    assert p["current"][0]["median_seconds"] == 20 * 60
    assert p["remaining_median_seconds"] == 12 * 60
    assert p["eta_seconds"] == (20 - 12) * 60 + 12 * 60, "on a chain: the remainder plus the steps after it"


def test_the_current_steps_remainder_is_floored_at_zero():
    env = envelope("wf_a", ["RUNNING"], started_minutes_ago=45)
    entry = {"workflow_id": "wf_a", "progress": board.progress(env, NOW)}
    board.annotate(entry, {"by_step": {("claude-code", "s0"): 20 * 60}})
    assert entry["progress"]["eta_seconds"] == 0


def test_steps_side_by_side_are_not_counted_twice():
    """Two lanes after one RUNNING step: the eta is the longer lane, not both."""
    env = envelope("wf_a", ["RUNNING", "QUEUED", "QUEUED"], started_minutes_ago=0)
    env["workflow"]["steps"][2]["depends_on"] = ["s0"]
    entry = {"workflow_id": "wf_a", "progress": board.progress(env, NOW)}
    board.annotate(entry, {"by_step": {("claude-code", "s0"): 600, ("claude-code", "s1"): 300,
                                       ("claude-code", "s2"): 900}})
    assert entry["progress"]["eta_seconds"] == 600 + 900
    assert entry["progress"]["remaining_median_seconds"] == 300 + 900


def test_no_history_is_null_never_zero():
    env = envelope("wf_a", ["RUNNING", "QUEUED"])
    entry = {"workflow_id": "wf_a", "progress": board.progress(env, NOW)}
    board.annotate(entry, {"by_step": {("claude-code", "s0"): 600}})
    assert entry["progress"]["remaining_median_seconds"] is None
    assert entry["progress"]["eta_seconds"] is None


def test_the_medians_do_not_move_the_order():
    """wf_slow's next step has a long history and wf_fast's a short one; both
    are 1 of 2 done and wf_slow's step has run longer, so it stays first."""
    envelopes = [envelope("wf_fast", ["SUCCEEDED", "RUNNING"], started_minutes_ago=5, profile="fast"),
                 envelope("wf_slow", ["SUCCEEDED", "RUNNING"], started_minutes_ago=6, profile="slow")]
    api = FakeAPI(envelopes, succeeded=[_succeeded("s1", 1, profile="fast"), _succeeded("s1", 500, profile="slow")])
    listing = sc.running_workflows(api, now=NOW)
    assert [e["workflow_id"] for e in listing["workflows"]] == ["wf_slow", "wf_fast"]
    assert listing["workflows"][1]["progress"]["eta_seconds"] == 0
