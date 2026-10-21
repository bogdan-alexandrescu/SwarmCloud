"""The per-project build board (BOARD-CORE, part of #1773).

Owner requirements and decisions, 2026-10-20 (#1773):

* SCOPE: every workflow and single task THE CALLER submitted against THIS
  project's repository, from any session; in flight always, finished within
  the last 30 days. Not teammates' work.
* GROUPING: by the issue each lane names (`input.issue`, else `(#N)` in its
  title), labelled with the issue's title where SwarmCloud has it; "No issue"
  for the rest.
* WAITING ON SOMEONE: owner questions (recommended option marked); steps in
  AWAITING_INPUT and HUMAN_REQUIRED parks, each with `/sc answer WF STEP`;
  merges refused (head_moved, merge_conflict, checks_failed) and CI parks over
  90 minutes. Failed steps in general are NOT listed. Nothing is read from
  GitHub.
* STORED at `<project>/.claude/build-board/`, git-ignored by a `.gitignore` of
  `*` beside it, never by editing the repository's own.

Every assertion carries the MUTATION it catches.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest

import build_board_fixture as fx
from swarm_mcp import buildboard, sc, server


def _model(client=None, cache=None):
    client = client or fx.FakeClient()
    return buildboard.collect(client, repository=fx.ORIGIN, now=fx.NOW,
                              cache={} if cache is None else cache), client


def _lanes(model):
    return {lane["id"]: lane for lane in model["lanes"]}


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, check=True)
    return done.stdout


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "acme-app"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", fx.ORIGIN)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    monkeypatch.setattr(buildboard, "_now", lambda: fx.NOW)
    return repo


# --------------------------------------------------------------------------
# Collect
# --------------------------------------------------------------------------


def test_the_board_holds_the_callers_lanes_in_this_repository_from_every_page():
    model, client = _model()
    assert set(_lanes(model)) == {
        "wf_a1", "wf_a2", "wf_b1", "wf_b2", "wf_b3", "wf_c1", "wf_c2", "wf_q", "wf_f", "wf_x",
        "t_s1", "t_s2",
    }  # MUTATION: keep a teammate's workflow (wf_theirs), one finished past 30 days (wf_old, t_s4), another repository's task (t_s3), a workflow's step as a single lane (t_a2_impl), or stop after the first page -- the set differs.
    pages = [r for r in client.reads if r.startswith("workflows active=False")]
    assert len(pages) >= 3  # MUTATION: read only the first page of the repository's workflows -- one page read.
    assert all("repository=github.com/acme/app" in r for r in client.reads if r.startswith("workflows"))  # MUTATION: list without the repository filter -- the filter is missing from the read.


def test_a_route_that_ignored_the_repository_filter_still_yields_only_this_repository():
    data = fx.build()
    row, tasks = next((r, t) for r, t in data["workflows"] if r["workflow_id"] == "wf_x")
    data["workflows"].append(({**row, "workflow_id": "wf_elsewhere"},
                              [{**t, "repository_url": fx.OTHER_REPO, "workflow_id": "wf_elsewhere"} for t in tasks]))
    model, _ = _model(fx.FakeClient(data, echo_repository=False))
    assert "wf_elsewhere" not in _lanes(model)  # MUTATION: trust the listing without checking each workflow's step repositories -- wf_elsewhere is on the board.
    assert "wf_x" in _lanes(model)  # MUTATION: drop every workflow when the route did not echo the filter -- wf_x is missing.


def test_each_lane_has_its_state():
    states = {k: v["state"] for k, v in _lanes(_model()[0]).items()}
    assert states == {
        "wf_a1": "Merged", "wf_a2": "Running", "wf_b1": "In CI", "wf_b2": "Red CI or refused",
        "wf_b3": "In CI", "wf_c1": "Running", "wf_c2": "Running", "wf_q": "Queued", "wf_f": "Failed",
        "wf_x": "Cancelled", "t_s1": "Succeeded", "t_s2": "Running",
    }  # MUTATION: read SUCCEEDED without the merge step (Merged becomes Succeeded), ignore the CI_PENDING park (In CI becomes Running), or a refused merge as plain Failed -- the map differs.


def test_each_lane_has_its_pull_request_console_times_and_issue():
    lanes = _lanes(_model()[0])
    assert lanes["wf_a1"]["pull_request"] == 501  # MUTATION: read the PR from the workflow row (which has none) -- None.
    assert lanes["wf_b2"]["pull_request"] == 613  # MUTATION: skip the git publish block -- None.
    assert lanes["t_s1"]["pull_request"] == 777  # MUTATION: no PR for a single task -- None.
    assert lanes["wf_a1"]["console"] == f"{fx.CONSOLE}/workflows/wf_a1"  # MUTATION: build the console link instead of taking it as served -- another string.
    assert lanes["t_s2"]["console"] == f"{fx.CONSOLE}/agents/t_s2"  # MUTATION: no console for a single task -- None.
    assert lanes["wf_a1"]["started"] == fx.ago(days=3)  # MUTATION: start from the newest step -- a later time.
    assert lanes["wf_a1"]["ended"] == fx.ago(days=2, hours=18)  # MUTATION: end at the first step's completion -- an earlier time.
    assert lanes["wf_a2"]["ended"] is None  # MUTATION: give a running lane an end -- a time.
    assert lanes["wf_a1"]["issue"] == 101  # MUTATION: ignore the step's `issue` input -- the title's `#101:` is not `(#N)`, so None.
    assert lanes["wf_a2"]["issue"] == 101  # MUTATION: read only the `issue` input -- the "(#101)" title gives None.
    assert lanes["wf_c2"]["issue"] is None  # MUTATION: invent an issue -- a number.
    assert lanes["wf_a2"]["label"] == "Cache the session lookup (#101)"  # MUTATION: label by the id -- wf_a2.
    assert lanes["t_s2"]["label"] == "flaky-hunt"  # MUTATION: no label fallback to metadata.unit -- the id.


def test_lanes_are_grouped_by_issue_with_the_issues_title_where_swarmcloud_has_it():
    groups = _model()[0]["groups"]
    assert [g["issue"] for g in groups] == [202, 101, None]  # MUTATION: one group per lane, or "No issue" first -- the order differs.
    by = {g["issue"]: g for g in groups}
    assert by[101]["title"] == "Speed up the login page"  # MUTATION: ignore the issue run's workflow title `acme/app#101: ...` -- None (or another repository's run title).
    assert by[202]["title"] == "Ship the merge gate"  # MUTATION: skip the issue runs' `issue_read.title` -- None.
    assert by[None]["title"] == "No issue"  # MUTATION: label the issueless group otherwise -- the text differs.
    assert sorted(by[101]["lanes"]) == ["t_s1", "wf_a1", "wf_a2", "wf_q"]  # MUTATION: leave single tasks out of the grouping -- t_s1 missing.
    assert (by[101]["done"], by[101]["total"], by[101]["in_flight"], by[101]["queued"]) == (2, 4, 1, 1)  # MUTATION: count Queued as in flight, or Merged as not done -- the counts differ.


def test_a_failed_runs_read_leaves_the_issue_numbered_not_the_board_unwritten():
    model, _ = _model(fx.FakeClient(fail="runs"))
    by = {g["issue"]: g for g in model["groups"]}
    assert by[202]["title"] is None  # MUTATION: keep a stale or invented title -- a string.
    assert by[101]["title"] == "Speed up the login page"  # MUTATION: drop the workflow-title source along with the runs -- None.


def test_waiting_on_someone_lists_exactly_the_three_kinds():
    waiting = _model()[0]["waiting"]
    kinds = sorted((w["kind"], w["lane"]) for w in waiting)
    assert kinds == [
        ("awaiting_input", "wf_c1"),
        ("ci_pending", "wf_b1"),
        ("human_required", "wf_c2"),
        ("merge_refused", "wf_b2"),
        ("question", "t_s1"),
        ("question", "wf_a1"), ("question", "wf_a1"),
    ]  # MUTATION: list wf_f's failed step, the 30-minute CI park of wf_b3, or drop a kind -- the list differs.
    awaiting = next(w for w in waiting if w["kind"] == "awaiting_input")
    assert awaiting["next"] == "/sc answer wf_c1 review"  # MUTATION: no answer command -- None.
    assert awaiting["since"] == fx.ago(hours=3)  # MUTATION: date it from the task's creation -- 4 hours ago.
    assert "ship" in awaiting["unlocks"]  # MUTATION: say nothing of the dependent step -- "ship" absent.
    human = next(w for w in waiting if w["kind"] == "human_required")
    assert human["next"] == "/sc answer wf_c2 implement"  # MUTATION: no command for a HUMAN_REQUIRED park -- None.
    ci = next(w for w in waiting if w["kind"] == "ci_pending")
    assert ci["since"] == fx.ago(hours=2)  # MUTATION: age a CI park from its creation -- 5 hours ago.
    refused = next(w for w in waiting if w["kind"] == "merge_refused")
    assert "checks_failed" in refused["detail"] and "#613" in refused["item"]  # MUTATION: drop the refusal code or the PR -- absent.
    question = next(w for w in waiting if w["kind"] == "question" and w["lane"] == "wf_a1")
    marked = [o["label"] for o in question["options"] if o["recommended"]]
    assert marked == ["Drop it"]  # MUTATION: mark no option, or the first one -- [] or ["Keep it"].


def test_a_finished_tasks_questions_and_pr_are_cached_and_not_read_again():
    cache: dict = {}
    first, client = _model(cache=cache)
    assert any(r.startswith("artifact t_a1_impl") for r in client.reads)  # MUTATION: never read the questions -- no artifact read.
    again = fx.FakeClient()
    second, _ = _model(again, cache=json.loads(json.dumps(cache)))
    assert not [r for r in again.reads if r.startswith("artifact")]  # MUTATION: no cache -- the questions are read again.
    assert "/v1/workflows/wf_a1" not in again.reads  # MUTATION: re-read a finished workflow's steps -- its envelope is read.
    assert "/v1/workflows/wf_a2" in again.reads  # MUTATION: cache a running lane -- it would never move.
    assert _lanes(second)["wf_a1"] == _lanes(first)["wf_a1"]  # MUTATION: cache a lane without its PR or questions -- the lane differs.
    assert sorted(w["kind"] for w in second["waiting"]) == sorted(w["kind"] for w in first["waiting"])  # MUTATION: drop a cached lane's questions from the waiting list -- fewer items.


def test_a_lane_whose_questions_could_not_be_read_is_read_again_on_the_next_refresh():
    cache: dict = {}
    first, _ = _model(fx.FakeClient(fail="artifacts"), cache=cache)
    unread = [w for w in first["waiting"] if w["lane"] == "wf_a1" and w["kind"] == "question"]
    assert [w["item"] for w in unread] == ["Questions that could not be read"]  # MUTATION: hide the failed read -- no item.
    assert "wf_a1" not in cache["lanes"]  # MUTATION: cache the lane anyway -- it is kept with only the 'could not be read' item.
    again = fx.FakeClient()
    second, _ = _model(again, cache=json.loads(json.dumps(cache)))
    assert any(r.startswith("artifact t_a1_impl") for r in again.reads)  # MUTATION: take the lane from the cache -- no artifact read.
    asked = [w["item"] for w in second["waiting"] if w["lane"] == "wf_a1" and w["kind"] == "question"]
    assert len(asked) == 2 and "Questions that could not be read" not in asked  # MUTATION: cache the lane anyway -- only the 'could not be read' item.


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def _page(model=None, banner=None):
    model = model or _model()[0]
    return buildboard.render(model, project_name="acme-app", origin=fx.ORIGIN,
                             written_at=fx.NOW, banner=banner)


def test_the_page_is_self_contained_and_themed():
    page = _page()
    assert page.startswith("<!doctype html>")  # MUTATION: render a fragment -- no doctype.
    assert "<title>SwarmCloud build board · acme-app</title>" in page  # MUTATION: another title -- absent.
    assert not re.search(r"<(link|script)[^>]+(src|href)=", page)  # MUTATION: load a stylesheet or script from a URL -- a match.
    assert "@import" not in page and "url(" not in page  # MUTATION: pull a font from the network -- present.
    assert "prefers-color-scheme: dark" in page and ":root" in page  # MUTATION: one theme only -- absent.
    assert 'name="viewport"' in page  # MUTATION: no viewport -- a phone renders it at desktop width.
    assert ".card { overflow-x: auto" in page  # MUTATION: let a table widen the page -- sideways scroll at 390 px.


def test_the_page_has_its_sections_in_order():
    page = _page()
    order = [page.index(s) for s in (
        "SwarmCloud build board · acme-app", 'id="written"', 'class="tiles"',
        "Waiting on someone", "By feature", "Every lane")]
    assert order == sorted(order)  # MUTATION: move a section -- out of order.
    assert 'datetime="2026-10-21T12:00:00Z"' in page and "12:00 UTC" in page  # MUTATION: no ISO stamp for the local time, or no UTC beside it -- absent.
    for label in ("lanes", "merged/done", "in flight", "queued", "needs you"):
        assert f'<span class="tile-label">{label}</span>' in page  # MUTATION: drop a tile -- absent.
    assert '<b class="tile-n">12</b><span class="tile-label">lanes</span>' in page  # MUTATION: count something else -- another number.
    assert '<b class="tile-n">7</b><span class="tile-label">needs you</span>' in page  # MUTATION: count waiting lanes rather than items -- another number.
    assert 'id="hide-finished" checked' in page  # MUTATION: hide finished off by default -- not checked.
    assert 'id="lane-filter"' in page  # MUTATION: no text filter -- absent.
    assert 'href="https://github.com/acme/app/pull/501"' in page  # MUTATION: no PR link, or one built from something but the origin -- absent.
    assert f'href="{fx.CONSOLE}/workflows/wf_a1"' in page  # MUTATION: the lane without its console link -- absent.
    assert "Drop it ★" in page  # MUTATION: no mark on the recommended option -- absent.
    assert "/sc answer wf_c1 review" in page  # MUTATION: no next command in the waiting table -- absent.


def test_every_string_is_escaped_and_no_secret_or_environment_value_reaches_the_page(monkeypatch):
    token = "ghp_" + secrets.token_hex(18)
    monkeypatch.setenv("SWARM_BOARD_TEST_TOKEN", token)
    page = _page()
    assert "<script>alert(1)</script>" not in page  # MUTATION: render a title unescaped -- the markup is live.
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; co" in page  # MUTATION: drop the title -- absent.
    assert "日本語のテスト" in page and "Größen" in page  # MUTATION: mangle Unicode -- absent.
    assert fx.SECRET_IN_INPUT not in page  # MUTATION: render a step's input or prompt -- the secret is on the page.
    assert token not in page  # MUTATION: render an environment value -- the token is on the page.


def test_the_banner_says_why_the_previous_refresh_failed():
    page = _page(banner="the last refresh failed at 11:00 UTC: GET /v1/workflows answered HTTP 503")
    assert 'class="banner"' in page and "HTTP 503" in page  # MUTATION: drop the banner -- absent.
    assert 'class="banner"' not in _page()  # MUTATION: always show a banner -- present.


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------


def test_the_board_is_written_atomically_and_git_ignores_it(project):
    path = buildboard.write(project, "<!doctype html><p>one</p>")
    assert path == project / ".claude" / "build-board" / "index.html"  # MUTATION: another place -- another path.
    assert (path.parent / ".gitignore").read_text() == "*\n"  # MUTATION: no .gitignore beside it -- missing.
    assert not (project / ".gitignore").exists()  # MUTATION: edit the repository's own .gitignore -- it exists.
    assert _git(project, "status", "--porcelain") == ""  # MUTATION: no `*` ignore -- `?? .claude/` is listed.
    buildboard.write(project, "<!doctype html><p>two</p>")
    assert path.read_text() == "<!doctype html><p>two</p>"  # MUTATION: never replace the page -- "one".
    assert sorted(p.name for p in path.parent.iterdir()) == [".gitignore", "index.html"]  # MUTATION: leave a temp file behind -- an extra file.


def test_a_write_renames_a_complete_temp_file_into_place(project, monkeypatch):
    seen = []
    real = os.replace
    monkeypatch.setattr(buildboard.os, "replace", lambda a, b: (seen.append((Path(a), Path(b))), real(a, b)))
    path = buildboard.write(project, "<!doctype html>")
    into_page = [source for source, target in seen if target == path]
    assert len(into_page) == 1 and into_page[0].parent == path.parent and into_page[0] != path  # MUTATION: write index.html in place -- no rename into it.


def test_a_refresh_writes_the_page_and_reports_it(project):
    got = buildboard.refresh(fx.FakeClient())
    path = project / ".claude" / "build-board" / "index.html"
    assert got["path"] == str(path) and path.is_file()  # MUTATION: report a path not written -- missing.
    assert got["written_at"] == "2026-10-21T12:00:00Z"  # MUTATION: another stamp -- differs.
    assert got["counts"] == {"lanes": 12, "done": 2, "in_flight": 6, "queued": 1, "needs_you": 7}  # MUTATION: count otherwise -- differs.
    assert got["waiting"] == 7  # MUTATION: report the waiting list itself or its lanes -- differs.
    cache = json.loads((path.parent / "cache.json").read_text())
    assert "wf_a1" in cache["lanes"] and "wf_a2" not in cache["lanes"]  # MUTATION: no cache, or cache a running lane -- differs.
    assert _git(project, "status", "--porcelain") == ""  # MUTATION: a cache file git sees -- listed.


def test_a_failed_read_keeps_the_previous_page_and_the_next_write_says_why(project):
    buildboard.refresh(fx.FakeClient())
    path = project / ".claude" / "build-board" / "index.html"
    before = path.read_text()
    failed = buildboard.refresh(fx.FakeClient(fail="workflows"))
    assert "HTTP 503" in failed["error"]  # MUTATION: report success -- no error.
    assert path.read_text() == before  # MUTATION: write an empty or error page -- the page changed.
    buildboard.refresh(fx.FakeClient())
    page = path.read_text()
    assert 'class="banner"' in page and "HTTP 503" in page  # MUTATION: forget the failure -- no banner.
    buildboard.refresh(fx.FakeClient())
    assert 'class="banner"' not in path.read_text()  # MUTATION: never clear the banner -- still there.


def test_a_malformed_listing_or_a_failed_write_answers_an_error_and_keeps_the_previous_page(project, monkeypatch):
    buildboard.refresh(fx.FakeClient())
    path = project / ".claude" / "build-board" / "index.html"
    before = path.read_text()
    broken = fx.FakeClient()
    monkeypatch.setattr(broken, "workflows", lambda **_: {"next_page_token": None})
    got = buildboard.refresh(broken)
    assert "not refreshed" in got["error"] and got["path"] == str(path)  # MUTATION: catch only SwarmError -- KeyError propagates.
    assert path.read_text() == before  # MUTATION: write a page from the malformed listing -- the page changed.

    def full_disk(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(buildboard, "write", full_disk)
    got = buildboard.refresh(fx.FakeClient())
    assert "No space left" in got["error"]  # MUTATION: let the OSError propagate -- the call raises.
    monkeypatch.undo()
    monkeypatch.setattr(buildboard, "_now", lambda: fx.NOW)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    buildboard.refresh(fx.FakeClient())
    assert "No space left" in path.read_text()  # MUTATION: forget a failed write -- no banner on the next page.


def test_a_refresh_outside_a_project_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(buildboard, "_project", lambda: None)
    got = buildboard.refresh(fx.FakeClient())
    assert "project" in got["error"] and got.get("path") is None  # MUTATION: write somewhere anyway -- a path.


def test_a_project_with_no_origin_says_why(project):
    _git(project, "remote", "remove", "origin")
    got = buildboard.refresh(fx.FakeClient())
    assert "origin" in got["error"]  # MUTATION: board every repository's work -- no error.


# --------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------


def test_the_tool_is_registered_read_only_and_answers_path_counts_and_waiting(project):
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_build_board")  # MUTATION: no tool -- StopIteration.
    assert tool["inputSchema"]["type"] == "object"  # MUTATION: no schema -- KeyError.
    client = fx.FakeClient()
    got = json.loads(server._call(client, "swarm_build_board", {}))
    assert set(got) == {"path", "written_at", "counts", "waiting"}  # MUTATION: answer more or less -- the keys differ.
    assert got["waiting"] == 7  # MUTATION: another count -- differs.


def test_the_tool_says_why_it_could_not(project):
    got = json.loads(server._call(fx.FakeClient(fail="workflows"), "swarm_build_board", {}))
    assert "HTTP 503" in got["error"]  # MUTATION: swallow the reason -- absent.


def test_sc_build_board_prints_the_path_and_opens_it_unless_told_not_to(project, monkeypatch):
    opened = []
    monkeypatch.setattr(buildboard, "open_page", lambda path: opened.append(path) or None)
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: fx.FakeClient())
    monkeypatch.setattr(sc, "tenant_choice", lambda: (None, None))
    out = io.StringIO()
    assert sc.main(["build-board", "--no-open"], out=out) == 0  # MUTATION: fail a good refresh -- nonzero.
    path = str(project / ".claude" / "build-board" / "index.html")
    assert path in out.getvalue()  # MUTATION: do not print the path -- absent.
    assert opened == []  # MUTATION: open despite --no-open -- opened.
    assert sc.main(["build-board"], out=io.StringIO()) == 0
    assert opened == [path]  # MUTATION: never open it -- [].


def test_sc_build_board_fails_with_the_reason(project, monkeypatch):
    monkeypatch.setattr(buildboard, "open_page", lambda path: pytest.fail("a failed refresh opens nothing"))
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: fx.FakeClient(fail="workflows"))
    monkeypatch.setattr(sc, "tenant_choice", lambda: (None, None))
    err = io.StringIO()
    monkeypatch.setattr("sys.stderr", err)
    assert sc.main(["build-board", "--no-open"], out=io.StringIO()) == sc.EXIT_FAIL  # MUTATION: exit 0 on a failed read -- 0.
    assert "HTTP 503" in err.getvalue()  # MUTATION: no reason -- absent.


@pytest.mark.parametrize("platform,command", [("darwin", "open"), ("linux", "xdg-open")])
def test_the_page_is_opened_with_the_platforms_opener(monkeypatch, platform, command):
    ran = []
    monkeypatch.setattr(buildboard.sys, "platform", platform)
    monkeypatch.setattr(buildboard.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert buildboard.open_page("/p/index.html") is None  # MUTATION: report a successful open as a failure -- a reason.
    assert ran == [[command, "/p/index.html"]]  # MUTATION: one opener everywhere -- the other platform's command.
