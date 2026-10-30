"""The visual-QA route list covers the nav (QA-CAPTURE; docs/design/visual-qa.md §4.3).

`apps/swarm-ui/qa/routes.json` is what `swarm_qa capture` sweeps, one group
per capture step. A tab missing from it is a page no QA run ever looks at, and
nothing in the run would say so: the sweep finds no defects on a screen it
never opened, which is CLAUDE.md's "empty output is not success". So this
holds the list to the console's own declarations, the way test_issue_forms.py
holds the issue forms:

* every `SECTIONS` tab in App.tsx is a route, and every route names a tab,
  pane or page that exists;
* every route's `path` is the router's path for its address -- FIXED in
  paths.ts for a page, `/agents/live/<task>/<pane>` for a task pane -- since
  the console moved from hashes to History-API paths (2026-10-01), which the
  design's illustrative `#overview/now` predates;
* every task pane (`TASK_PANES`, plus Details, the bare task) is captured, on
  the fixture task the route list names, which must exist in the fixture
  build's task list -- except Browser, captured on the one fixture task that
  runs `claude-code-browser` (QAFIX2-QABROWSER, owner decision 2026-10-21):
  the ordinary fixture agent has no live browser, so its Browser pane is a
  page that build can never fill;
* every `settings/*` page in FIXED is captured;
* the list parses as `swarm_qa` reads it, and a mockup it names exists.

The SECTIONS parser is IMPORTED from test_nav_headings_agree.py, not
restated: a second regex over the same array is the mirrored copy
docs/mirrored-values.md warns about.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

from control_plane.test_nav_headings_agree import APP_TSX, SRC, _balanced, _read, _sections

REPO = Path(__file__).resolve().parents[3]
ROUTES = REPO / "apps" / "swarm-ui" / "qa" / "routes.json"
PATHS_TS = SRC / "paths.ts"
API_TS = SRC / "api.ts"
SWARM_QA = REPO / "images" / "agent-runtime-browser" / "swarm_qa.py"
#: The task's own page, `/agents/<tab>/<id>`, is its Details pane.
DETAIL = "detail"
#: The pane captured on the live-browser fixture task rather than the fixture task.
BROWSER_PANE = "browser"
#: The catalogue profile whose session reads `live_browser: true` in the fixture build.
BROWSER_PROFILE = "claude-code-browser"
#: The Budget card's agents (#2459, BUDGET-FIXTURES), route id -> task id: each
#: captured on its Details only, and served by id and off the lists in api.ts.
BUDGET_ROUTES = {
    "budget-warn": "task_budget_warn",
    "budget-exceeded": "task_budget_exceeded",
    "budget-none": "task_budget_none",
}


def _routes() -> dict:
    return json.loads(_read(ROUTES))


def _all_routes() -> list[tuple[str, dict]]:
    out = [(group, route) for group, routes in _routes()["groups"].items() for route in routes]
    assert out, "routes.json lists no route; a sweep of it would capture nothing"
    return out


def _fixed() -> dict[str, str]:
    text = _read(PATHS_TS)
    decl = "export const FIXED: Readonly<Record<string, string>> = "
    block = _balanced(text, text.index("{", text.index(decl) + len(decl) - 1), "{", "}")
    pairs = dict(re.findall(r"^\s*'([a-z0-9/-]+)':\s*'(/[^']*)'", block, re.M))
    assert "overview/now" in pairs and "settings/profile" in pairs, "FIXED parsed to too little"
    return pairs


def _task_panes() -> list[str]:
    m = re.search(r"export const TASK_PANES = \[([^\]]*)\]", _read(PATHS_TS))
    assert m, "TASK_PANES is not in paths.ts"
    panes = re.findall(r"'([a-z-]+)'", m.group(1))
    assert panes, "TASK_PANES parsed to nothing"
    return panes


def _fixture_tasks() -> dict[str, tuple[str, str]]:
    """`{task id: (state, profile)}` for every literal `mk(id, state, profile, ...)` in fixtureTasks."""
    rows = re.findall(r"mk\('(task_[A-Za-z0-9_]+)',\s*'([A-Z_]+)',\s*'([a-z0-9-]+)'", _read(API_TS))
    assert len(rows) >= 10, "fixtureTasks parsed to too few rows"
    return {tid: (state, profile) for tid, state, profile in rows}


def _task_of(route: dict) -> str:
    seg = route["address"].split("/")
    assert seg[:2] == ["work", "task"] and len(seg) > 2, f"{route['id']}: not a task route"
    return seg[2]


def _pane_task(pane: str | None) -> str:
    """The task a pane is captured on: Browser on the live-browser fixture task, the rest on the fixture task."""
    if pane == BROWSER_PANE:
        return _task_of(next(r for _g, r in _all_routes() if r["id"] == "task-browser"))
    return _routes()["fixture_task"]


def test_every_sections_tab_is_in_a_route_group():
    tabs = {f"{sid}/{tid}" for sid, _label, section_tabs in _sections(_read(APP_TSX)) for tid, _ in section_tabs}
    sections = {sid for sid, _label, _tabs in _sections(_read(APP_TSX))}
    addresses = {route["address"] for _group, route in _all_routes()}
    missing = sorted(tabs - addresses)
    assert not missing, f"tabs in SECTIONS that no QA route group captures: {missing}; add each to apps/swarm-ui/qa/routes.json"
    stale = sorted(a for a in addresses if a.split("/")[0] in sections and a.split("/")[1] != "task" and a not in tabs)
    assert not stale, f"routes naming a tab SECTIONS no longer has: {stale}"


def test_every_route_path_is_the_routers_path():
    fixed = _fixed()
    for group, route in _all_routes():
        address, path = route["address"], route["path"]
        seg = address.split("/")
        if seg[:2] == ["work", "task"]:
            pane = seg[3] if len(seg) > 3 else None
            task = BUDGET_ROUTES.get(route["id"]) or _pane_task(pane)
            assert seg[2] == task, f"{group}/{route['id']}: a task pane on {seg[2]}, not {task}"
            want = f"/agents/live/{task}" + (f"/{pane}" if pane else "")
        else:
            assert address in fixed, f"{group}/{route['id']}: {address!r} is not an address in FIXED (paths.ts)"
            want = fixed[address]
        assert path == want, f"{group}/{route['id']}: path {path!r}, but the router serves {address!r} at {want!r}"


def test_every_task_pane_and_settings_page_is_captured():
    task = _routes()["fixture_task"]
    addresses = {route["address"] for _group, route in _all_routes()}
    panes = {f"work/task/{task}"} | {f"work/task/{_pane_task(p)}/{p}" for p in _task_panes()}
    assert len(panes) == 1 + len(_task_panes())
    assert not sorted(panes - addresses), f"task panes no route captures: {sorted(panes - addresses)}"
    settings = {a for a in _fixed() if a.startswith("settings/")}
    assert settings and not sorted(settings - addresses), f"settings pages no route captures: {sorted(settings - addresses)}"
    # The fixture task is a task the fixture build lists, so its panes render.
    assert re.search(rf"mk\('{re.escape(task)}',", _read(API_TS)), f"{task} is not in api.ts fixtureTasks"


def test_the_browser_route_is_on_the_fixture_build_live_browser_agent():
    """QAFIX2-QAROUTE found `task-browser` on task_a073aff5, an ordinary
    `claude-code` agent: the fixture build answers `live_browser: false` for
    it, so the route captured a pane that build can never fill. The route is
    on the one RUNNING fixture task on `claude-code-browser`, whose fixture
    session reads live (owner decision 2026-10-21, QAFIX2-QABROWSER)."""
    route = next((r for _g, r in _all_routes() if r["id"] == "task-browser"), None)
    assert route is not None, "routes.json has no task-browser route"
    task = _task_of(route)
    assert route["address"] == f"work/task/{task}/{BROWSER_PANE}", route["address"]
    tasks = _fixture_tasks()
    # MUTATION: point task-browser back at task_a073aff5 (profile claude-code) -> fails.
    assert task in tasks, f"task-browser is on {task}, which is not an mk(...) row of fixtureTasks in api.ts"
    state, profile = tasks[task]
    # MUTATION: give the new fixture row profile 'claude-code' -> fails.
    assert profile == BROWSER_PROFILE, f"task-browser is on {task}, a {profile!r} task, not {BROWSER_PROFILE!r}"
    # MUTATION: make the new fixture row SUCCEEDED -> fails (a finished agent's browser is closed).
    assert state == "RUNNING", f"task-browser is on {task}, which is {state}, not RUNNING"
    # MUTATION: point the route at the browser task but leave it the fixture_task too -> fails.
    assert task != _routes()["fixture_task"], "the live-browser task is not the ordinary fixture task"
    # MUTATION: leave the fixture branch of loadLiveSession answering false for every id -> fails.
    assert re.search(rf"'{re.escape(task)}'", _live_session_fixture()), (
        f"the fixture branch of loadLiveSession never names {task}, so its Browser pane reads no live browser"
    )


def test_the_budget_card_is_captured_on_its_three_by_id_agents():
    """BUDGET-FIXTURES (part of #2459): the agent's Budget card is drawn only
    for a budgeted agent, and no listed fixture task has a budget, so each of
    its three states is captured on an agent the fixture build answers BY ID
    and keeps OFF THE LISTS (BGD, 2026-10-27): other tests pin every count."""
    routes = {r["id"]: r for _g, r in _all_routes()}
    tasks = _fixture_tasks()
    budget_tasks = _budget_fixture()
    for rid, task in BUDGET_ROUTES.items():
        # MUTATION: delete a budget route from routes.json -> fails.
        route = routes.get(rid)
        assert route is not None, f"routes.json has no {rid} route"
        # MUTATION: point a budget route at a pane -> fails; the card is on Details.
        assert route["address"] == f"work/task/{task}", f"{rid}: {route['address']}"
        assert route["path"] == f"/agents/live/{task}", f"{rid}: {route['path']}"
        # MUTATION: add the task as an mk(...) row of fixtureTasks -> fails.
        assert task not in tasks, f"{task} is on the fixture build's task list; budget agents stay off it"
        # MUTATION: rename the id in fixtureBudgetTasks -> fails.
        assert re.search(rf"'{re.escape(task)}'", budget_tasks), f"fixtureBudgetTasks in api.ts never names {task}"


def _budget_fixture() -> str:
    """The budget agents' ids and `fixtureBudgetTasks`, in api.ts: the fixtures a per-task read answers."""
    text = _read(API_TS)
    start = text.index("const FIXTURE_BUDGET_WARN = ")
    end = text.index("function fixtureOffList(", start)
    return text[start:end]


def _live_session_fixture() -> str:
    """The body of `loadLiveSession` in api.ts: its fixture branch names the one live task."""
    text = _read(API_TS)
    start = text.index("export async function loadLiveSession(")
    return _balanced(text, text.index("{", text.index(")", start)), "{", "}")


def test_route_ids_are_unique_file_name_safe_and_ready_selectors_are_set():
    seen: set[str] = set()
    groups = _routes()["groups"]
    assert len(groups) >= 6, "six capture groups (visual-qa.md §4.3), so each capture step is about as long"
    for group, route in _all_routes():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", group), group
        rid = route["id"]
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", rid) and "--" not in rid, f"{group}/{rid}: not a file-name-safe id"
        assert rid not in seen, f"route id {rid!r} is in two groups; a report could not tell them apart"
        seen.add(rid)
        assert isinstance(route.get("ready"), str) and route["ready"].strip(), f"{group}/{rid}: no ready selector"


def test_the_list_reads_as_swarm_qa_reads_it_and_every_mockup_exists():
    spec = importlib.util.spec_from_file_location("swarm_qa_routes_check", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    document = module.load_routes(ROUTES)
    assert document["format"] == 1
    ids = {route["id"] for _group, route in _all_routes()}
    for mockup in document["mockups"]:
        assert (REPO / mockup["path"]).is_file(), f"mockup {mockup['id']}: {mockup['path']} does not exist"
        assert mockup["compare_with"] in ids, f"mockup {mockup['id']} compares with no route"


#: Run 4's phone captures that stopped at their budget before the page ended
#: (box comment 6283704756): admin-006, capacity-001, capacity-003,
#: overview-automate-008 and work-a-010. (capacity-002, "capacity/runtimes",
#: is an address the list no longer has.)
RUN4_CUT = ("limits", "pools", "holders", "now", "timeline")


def test_a_phone_capture_runs_to_the_end_and_segments_governs_the_desktop():
    """The owner's decision c49 (2026-10-22): at phone-390 every route is
    captured until its scroller ends, up to MAX_SEGMENTS; a route's
    `segments` governs the desktop preset only."""
    spec = importlib.util.spec_from_file_location("swarm_qa_budget_check", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    routes = dict((route["id"], route) for _group, route in _all_routes())
    assert set(RUN4_CUT) <= set(routes), sorted(set(RUN4_CUT) - set(routes))
    for rid, route in routes.items():
        # MUTATION: keep the route's `segments` as the phone budget and this
        # fails on `now` (3) and on every route at the default (4).
        assert module.segment_budget(route, "phone-390") == module.MAX_SEGMENTS, rid
        # MUTATION: give the desktop MAX_SEGMENTS too and this fails.
        assert module.segment_budget(route, "desktop") == route.get("segments", module.DEFAULT_SEGMENTS), rid


#: The admin group's routes, captured as a platform admin who also administers
#: one group workspace (QAFIX4-FIXTURE-A, run-4 admin-004 and admin-005).
ADMIN_ROUTES = ("limits", "runner-sizes", "tenants", "counts", "platform-secrets", "setup", "health", "people", "workspaces")


def test_the_admin_group_is_captured_as_a_platform_admin_and_pool_limits_once_as_the_member():
    groups = _routes()["groups"]
    admin = {route["id"]: route for route in groups["admin"]}
    for rid in ADMIN_ROUTES:
        assert rid in admin, f"the admin group has no {rid!r} route"
        # MUTATION: drop `"persona": "platform-admin"` from any admin route -> fails.
        assert admin[rid].get("persona") == "platform-admin", f"admin/{rid} is not captured as a platform admin"
    # Pool limits' locked view (owner decision c45) stays captured, as the member.
    locked = admin.get("limits-locked")
    # MUTATION: delete the limits-locked route -> fails.
    assert locked is not None, "the admin group has no limits-locked route"
    assert (locked["address"], locked["path"]) == (admin["limits"]["address"], admin["limits"]["path"])
    # MUTATION: give limits-locked a persona -> fails; absent is the member.
    assert "persona" not in locked, "limits-locked must be the member: no persona key"
    # Every other route is today's identity: absent means member.
    others = [f"{g}/{r['id']}" for g, routes in groups.items() for r in routes
              if r.get("persona") is not None and not (g == "admin" and r["id"] in ADMIN_ROUTES)]
    # MUTATION: give a Settings route the admin persona -> fails.
    assert not others, f"routes outside the admin group carry a persona: {others}"


def test_the_console_reads_the_cookie_the_capture_sets():
    spec = importlib.util.spec_from_file_location("swarm_qa_persona_check", SWARM_QA)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    text = _read(API_TS)
    # MUTATION: rename the cookie on either side -> fails.
    assert f"'{module.PERSONA_COOKIE}'" in text, f"api.ts never reads the {module.PERSONA_COOKIE} cookie"
    for persona in module.PERSONAS:
        # MUTATION: rename a persona in api.ts -> fails.
        assert f"'{persona}'" in text, f"api.ts has no persona {persona!r}"
