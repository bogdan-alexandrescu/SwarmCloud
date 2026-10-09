"""`swarm_territory` and the dispatch-time territory warning (lane KG7).

docs/design/knowledge-graph.md §4.8 and §6 row KG7: before parallel lanes are
dispatched, each unit's declared `files` are expanded by swarm-api's territory
route (KG3) and intersected with the open pull requests, the live issue-run
steps, the other units of the same call and the tasks this session still has
in flight. The row's success measure is "parallel lanes dispatched with
overlapping territory, unannounced -> 0": every overlapping dispatch below is
counted, and the count of those whose reply does not name both sides is
asserted to be zero.

Every test drives `server._call` against a fake control plane that RECORDS
what it was sent. The fake borrows the real `SwarmClient.registration` and
`SwarmClient.territory`, so the client's half is exercised through the fake's
`request`, and nothing here needs a credential, a network or an emulator.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmClient, SwarmError

REPO = "https://github.com/acme/widgets"
REPO_ID = "repo_" + "a" * 16


def _digest() -> str:
    # Built at runtime: a 64-hex literal is the shape a credential scan reads.
    return "sha256:" + "0" * 64


class FakeApi:
    """`/v1/stats`, the registrations, the territory route, tasks and workflows.

    The territory route is a small model of KG3's: a named file's callers and
    tests come from `callers`/`tests`, and the overlap is every open pull
    request file and live issue-run step file equal to, or under a `dir/`
    prefix of, a path of that territory.
    """

    registration = SwarmClient.registration
    territory = SwarmClient.territory

    def __init__(self, *, format_version=3, graph=True, callers=None, tests=None, seams=(),
                 pulls=None, runs=None, registered=True, territory_error=None,
                 pulls_ok=True, max_batch_size=10):
        self.format_version = format_version
        self.graph = graph
        self.callers = callers or {}
        self.tests = tests or {}
        self.seams = set(seams)
        self.pulls = pulls or {}
        self.runs = runs or []
        self.registered = registered
        self.territory_error = territory_error
        self.pulls_ok = pulls_ok
        self.max_batch_size = max_batch_size
        self.requests: list[tuple[str, str]] = []
        self.territory_bodies: list[dict] = []
        self.dispatched: list[dict] = []
        self.batches: list[list[dict]] = []
        self.workflows: list[dict] = []
        self.states: dict[str, str] = {}

    # -- the transport the real client methods speak ----------------------
    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        self.requests.append((method, path))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": self.max_batch_size}}
        if method == "GET" and path.startswith("/v1/repositories?"):
            rows = [{"repo_id": "repo_other", "owner": "acme", "repo": "gadgets"}]
            if self.registered:
                rows.append({"repo_id": REPO_ID, "owner": "Acme", "repo": "Widgets"})
            return {"repositories": rows, "next_page_token": None}
        if method == "POST" and path == f"/v1/repositories/{REPO_ID}/territory":
            self.territory_bodies.append(payload)
            if self.territory_error is not None:
                raise self.territory_error
            return self._territory(payload)
        if method == "POST" and path == "/v1/workflows":
            self.workflows.append(payload)
            steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                      "runner_profile": s.get("runner_profile") or "claude-code"}
                     for s in payload["steps"]]
            return {"workflow": {"workflow_id": "wf_1", "steps": steps}, "dispatch": {}}
        raise SwarmError(f"{method} {path} -> 500: no such route", status=500)

    def _territory(self, body):
        named = sorted(set(body["files"]))
        callers = sorted({c for f in named for c in self.callers.get(f, [])}) if self.graph else []
        tests = sorted({t for f in named for t in self.tests.get(f, [])}) if self.graph else []
        reach = named + callers + tests

        def shared(files):
            return [{"path": p, "entry": e, "why": "named", "seam": p in self.seams}
                    for p in files for e in reach
                    if p == e or (e.endswith("/") and p.startswith(e))
                    or (p.endswith("/") and e.startswith(p))]

        pulls = []
        for number, files in sorted(self.pulls.items()):
            rows = shared(files)
            if rows:
                pulls.append({"number": number, "title": f"PR {number}", "shared": rows,
                              "shared_total": len(rows), "files_truncated": False})
        lanes = []
        for run in self.runs:
            rows = shared(run["files"])
            if rows:
                lanes.append({**{k: run[k] for k in ("run_id", "issue", "state", "step_id")},
                              "pull_request": None, "shared": rows, "shared_total": len(rows)})
        return {
            "repo_id": REPO_ID, "index_sha": "c" * 40, "head_sha": "c" * 40, "behind_by": 0,
            "stale": False,
            "graph_digest": _digest() if self.graph else None,
            "format_version": self.format_version if self.graph else None,
            "named": named, "unknown": [],
            "callers": [{"path": p, "depth": 1, "via": []} for p in callers],
            "tests": [{"path": p, "depth": 1, "via": []} for p in tests],
            "seams": [{"path": p} for p in sorted(self.seams) if p in reach],
            "communities": [], "cut": {"below_floor": 0, "judged": 0}, "truncated": [],
            "overlap": {
                "pull_requests_read": {"ok": self.pulls_ok, "code": None if self.pulls_ok
                                       else "forge_unreachable", "read_at": None},
                "pull_requests": pulls if self.pulls_ok else [],
                "pull_requests_unread": [], "pull_requests_truncated": False,
                "lanes": lanes, "lanes_undeclared": [], "lanes_truncated": False,
            },
        }

    # -- what the dispatch paths call directly ----------------------------
    def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        task_id = f"task_{len(self.dispatched)}"
        self.states[task_id] = "RUNNING"
        return {"id": task_id, "state": "QUEUED"}

    def dispatch_batch(self, payloads):
        self.batches.append(list(payloads))
        out = []
        for i, _ in enumerate(payloads, start=1):
            task_id = f"task_b{len(self.batches)}_{i}"
            self.states[task_id] = "RUNNING"
            out.append({"id": task_id, "state": "QUEUED"})
        return out

    def task(self, task_id):
        if task_id not in self.states:
            raise SwarmError(f"GET /v1/tasks/{task_id} -> 404", status=404)
        return {"id": task_id, "state": self.states[task_id]}

    def graph_reads(self) -> int:
        return sum(1 for _, path in self.requests if path.startswith("/v1/repositories"))


@pytest.fixture(autouse=True)
def _cloud_target(monkeypatch):
    """Every dispatch here goes to the cloud; where it goes is not under test."""
    monkeypatch.setenv(server.config.plugin_env(server.config.PLUGIN_TARGET), "cloud")


@pytest.fixture(autouse=True)
def _no_rows(monkeypatch):
    """The row launch writes a script; it is not under test here."""
    monkeypatch.setattr(server, "_dispatch_rows", lambda sent: {"skipped": True})


def _call(api, name, args):
    return json.loads(server._call(api, name, args))


def _says(reply) -> str:
    return json.dumps(reply.get("territory_warning") or {})


# ==========================================================================
# swarm_territory
# ==========================================================================


def test_the_tool_expands_each_lane_and_names_every_overlapping_pair():
    api = FakeApi(callers={"app/api.py": ["app/routes.py"]}, tests={"app/api.py": ["tests/test_api.py"]})
    reply = _call(api, "swarm_territory", {"repo": REPO, "lanes": [
        {"name": "KG-a", "files": ["app/api.py"]},
        {"name": "KG-b", "files": ["app/routes.py"]},
        {"name": "KG-c", "files": ["docs/"]},
    ]})
    assert reply["status"] == "checked"
    lanes = {lane["lane"]: lane for lane in reply["lanes"]}
    assert lanes["KG-a"]["callers"] == ["app/routes.py"]
    assert lanes["KG-a"]["tests"] == ["tests/test_api.py"]
    # KG-b edits a file KG-a's change reaches: one overlap, naming both.
    assert reply["overlap_count"] == 1
    (row,) = reply["overlaps"]
    assert row["lane"] == "KG-a" and row["with"] == {"kind": "this_call", "lane": "KG-b"}
    assert row["shared"][0]["path"] == "app/routes.py" and row["shared"][0]["why"] == "caller"
    assert "KG-a" in row["says"] and "KG-b" in row["says"]
    # The route was asked once per lane, with the lane's files and nothing else.
    assert [b["files"] for b in api.territory_bodies] == [["app/api.py"], ["app/routes.py"], ["docs/"]]


def test_the_tool_says_when_there_is_no_v3_index_rather_than_going_quiet():
    api = FakeApi(format_version=2, pulls={7: ["app/api.py"]})
    reply = _call(api, "swarm_territory", {"repo": "acme/widgets", "files": ["app/api.py"]})
    assert reply["status"] == "no_index"
    assert "index format 3" in reply["reason"]
    # The route still compared the named file; the tool shows that.
    assert [row["with"]["number"] for row in reply["overlaps"]] == [7]


def test_the_tool_says_an_unregistered_repository_is_one():
    reply = _call(FakeApi(registered=False), "swarm_territory",
                  {"repo": REPO, "files": ["app/api.py"]})
    assert reply["status"] == "unregistered" and "acme/widgets" in reply["reason"]


def test_the_tool_takes_files_or_lanes_and_needs_a_repository():
    api = FakeApi()
    with pytest.raises(SwarmError, match="exactly one"):
        server._call(api, "swarm_territory", {"repo": REPO})
    with pytest.raises(SwarmError, match="exactly one"):
        server._call(api, "swarm_territory", {"repo": REPO, "files": ["a"], "lanes": [{"files": ["b"]}]})
    with pytest.raises(SwarmError, match="needs `repo`"):
        server._call(api, "swarm_territory", {"files": ["a"]})
    assert api.requests == []


def test_the_tool_is_advertised_read_only_with_no_execution_parameter():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_territory")
    properties = set(tool["inputSchema"]["properties"])
    assert {"files", "lanes", "repo"} <= properties
    assert not {"image", "command", "backend", "prompt", "runner_profile"} & properties


# ==========================================================================
# the dispatch-time warning
# ==========================================================================


def test_a_single_dispatch_overlapping_an_open_pull_request_is_warned_and_still_sent():
    api = FakeApi(pulls={42: ["app/api.py", "README.md"]})
    reply = _call(api, "swarm_dispatch", {"prompt": "fix it", "repo": REPO, "label": "lane-x",
                                          "files": ["app/api.py"]})
    assert reply["task_id"] == "task_1" and len(api.dispatched) == 1
    warning = reply["territory_warning"]
    (row,) = warning["overlaps"]
    assert row["lane"] == "lane-x" and row["with"]["kind"] == "pull_request"
    assert row["with"]["number"] == 42
    assert "lane-x" in row["says"] and "#42" in row["says"]
    assert warning["warning"].startswith("DISPATCHED ANYWAY")
    # `files` is the bridge's; the platform never sees it.
    assert "files" not in api.dispatched[0]


def test_a_live_issue_run_step_is_named_by_run_issue_and_step():
    api = FakeApi(runs=[{"run_id": "run_9", "issue": 812, "state": "RUNNING", "step_id": "s2",
                         "files": ["app/"]}])
    reply = _call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO, "files": ["app/api.py"]})
    (row,) = reply["territory_warning"]["overlaps"]
    assert row["with"]["kind"] == "issue_run_step"
    assert "run_9" in row["says"] and "#812" in row["says"] and "'s2'" in row["says"]


def test_two_tasks_of_one_batch_that_overlap_are_named_together():
    api = FakeApi(callers={"app/api.py": ["app/main.py"]})
    reply = _call(api, "swarm_dispatch", {"tasks": [
        {"prompt": "a", "repo": REPO, "label": "left", "files": ["app/api.py"]},
        {"prompt": "b", "repo": REPO, "label": "right", "files": ["app/main.py"]},
        {"prompt": "c", "repo": REPO, "label": "apart", "files": ["docs/x.md"]},
    ]})
    assert reply["count"] == 3 and len(api.batches) == 1
    assert all("files" not in payload for payload in api.batches[0])
    (row,) = reply["territory_warning"]["overlaps"]
    assert row["lane"] == "tasks[0] (left)"
    assert row["with"] == {"kind": "this_call", "lane": "tasks[1] (right)"}


def test_parallel_workflow_steps_overlap_but_steps_on_one_line_do_not():
    api = FakeApi()
    steps = [
        {"step_id": "a", "prompt": "a", "files": ["app/api.py"]},
        {"step_id": "b", "prompt": "b", "files": ["app/api.py"]},
        {"step_id": "c", "prompt": "c", "depends_on": ["a"], "files": ["app/api.py"]},
    ]
    reply = _call(api, "swarm_workflow", {"repo": REPO, "steps": steps})
    pairs = {(r["lane"], r["with"]["lane"]) for r in reply["territory_warning"]["overlaps"]}
    # a-c is a dependency line: the plan, not a collision. b is on neither's line.
    assert pairs == {("a", "b"), ("b", "c")}
    sent = api.workflows[0]["steps"]
    assert all("files" not in step for step in sent)


def test_a_spec_carrying_files_keeps_its_digest_and_never_sends_them():
    from swarm_mcp import workflows

    spec = {"repository_url": REPO, "steps": [
        {"step_id": "a", "prompt": "a", "files": ["app/api.py"]},
        {"step_id": "b", "prompt": "b", "files": ["app/"]},
    ]}
    api = FakeApi()
    reply = _call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    assert reply["workflow_id"] == "wf_1"
    assert reply["territory_warning"]["overlaps"][0]["with"]["lane"] == "b"
    assert all("files" not in step for step in api.workflows[0]["steps"])


def test_a_spec_file_carrying_files_is_read_held_and_submitted_by_reference(tmp_path, monkeypatch):
    monkeypatch.setenv(server.checkout.CHECKOUT_DIR_ENV, str(tmp_path))
    (tmp_path / "plan.json").write_text(json.dumps({"repository_url": REPO, "steps": [
        {"step_id": "a", "prompt": "a", "files": ["app/api.py"]},
        {"step_id": "b", "prompt": "b", "files": ["app/api.py"]},
    ]}))
    api = FakeApi()
    held = _call(api, "swarm_workflow_spec", {"path": "plan.json"})
    reply = _call(api, "swarm_workflow", {"spec_ref": held["spec_ref"], "spec_digest": held["spec_digest"]})
    assert {(r["lane"], r["with"]["lane"]) for r in reply["territory_warning"]["overlaps"]} == {("a", "b")}
    assert all("files" not in step for step in api.workflows[0]["steps"])


def test_a_task_still_in_flight_from_this_session_is_named_until_it_finishes():
    api = FakeApi()
    first = _call(api, "swarm_dispatch", {"prompt": "one", "repo": REPO, "label": "first",
                                          "files": ["app/api.py"]})
    assert "territory_warning" not in first
    second = _call(api, "swarm_dispatch", {"prompt": "two", "repo": REPO, "label": "second",
                                           "files": ["app/api.py"]})
    (row,) = second["territory_warning"]["overlaps"]
    assert row["with"]["kind"] == "dispatched_task" and row["with"]["task_id"] == "task_1"
    assert "second" in row["says"] and "task_1" in row["says"] and "first" in row["says"]
    api.states["task_1"] = "SUCCEEDED"
    api.states["task_2"] = "CANCELLED"
    third = _call(api, "swarm_dispatch", {"prompt": "three", "repo": REPO, "files": ["app/api.py"]})
    assert "territory_warning" not in third


@pytest.mark.parametrize("fake", [
    pytest.param(dict(format_version=2), id="format-2-index"),
    pytest.param(dict(graph=False), id="no-promoted-graph"),
    pytest.param(dict(registered=False), id="unregistered"),
])
def test_without_a_v3_index_the_dispatch_says_nothing_and_still_goes(fake):
    api = FakeApi(pulls={42: ["app/api.py"]}, **fake)
    reply = _call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO, "files": ["app/api.py"]})
    assert reply["task_id"] == "task_1"
    assert not any(key.startswith("territory") for key in reply), reply


def test_a_unit_with_no_files_costs_no_graph_read():
    api = FakeApi(pulls={42: ["app/api.py"]})
    _call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO})
    _call(api, "swarm_workflow", {"repo": REPO, "steps": [{"step_id": "a", "prompt": "a"}]})
    assert api.graph_reads() == 0


def test_a_failed_territory_read_says_so_and_still_dispatches():
    api = FakeApi(territory_error=SwarmError("POST ... -> 503: unavailable", status=503))
    reply = _call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO, "files": ["app/api.py"]})
    assert reply["task_id"] == "task_1"
    assert "could not be read" in reply["territory_unchecked"]
    assert "territory_warning" not in reply


def test_pull_requests_that_could_not_be_read_are_never_reported_as_no_overlap():
    api = FakeApi(pulls_ok=False)
    reply = _call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO, "files": ["app/api.py"]})
    warning = reply["territory_warning"]
    assert warning["overlaps"] == []
    assert any("could not be read" in note for note in warning["not_read"])


def test_files_that_are_not_paths_are_refused_before_anything_is_sent():
    api = FakeApi()
    with pytest.raises(SwarmError, match="`files` is a list"):
        server._call(api, "swarm_dispatch", {"prompt": "x", "repo": REPO, "files": [3]})
    with pytest.raises(SwarmError, match="`files` is a list"):
        server._call(api, "swarm_workflow", {"repo": REPO, "steps": [
            {"step_id": "a", "prompt": "a", "files": {"app": 1}}]})
    assert not api.dispatched and not api.workflows and api.graph_reads() == 0


def test_every_dispatching_schema_offers_files():
    tools = {t["name"]: t["inputSchema"] for t in server.TOOLS}
    assert "files" in tools["swarm_dispatch"]["properties"]
    assert "files" in tools["swarm_dispatch"]["properties"]["tasks"]["items"]["properties"]
    assert "files" in tools["swarm_workflow"]["properties"]["steps"]["items"]["properties"]


# ==========================================================================
# the row's measure: overlapping dispatches, unannounced -> 0
# ==========================================================================


def _overlapping_dispatches():
    """Every way this bridge can send parallel lanes whose territories overlap,
    with the two names the reply must carry."""
    yield ("single vs open PR", FakeApi(pulls={5: ["app/api.py"]}), "swarm_dispatch",
           {"prompt": "x", "repo": REPO, "label": "L1", "files": ["app/api.py"]}, ("L1", "#5"))
    yield ("single vs issue-run step via a caller",
           FakeApi(callers={"app/api.py": ["app/main.py"]},
                   runs=[{"run_id": "run_3", "issue": 9, "state": "RUNNING", "step_id": "s1",
                          "files": ["app/main.py"]}]),
           "swarm_dispatch", {"prompt": "x", "repo": REPO, "label": "L2", "files": ["app/api.py"]},
           ("L2", "run_3"))
    yield ("batch pair by directory prefix", FakeApi(), "swarm_dispatch", {"tasks": [
        {"prompt": "a", "repo": REPO, "label": "A", "files": ["app/"]},
        {"prompt": "b", "repo": REPO, "label": "B", "files": ["app/x.py"]}]}, ("tasks[0] (A)", "tasks[1] (B)"))
    yield ("batch pair via a test", FakeApi(tests={"app/x.py": ["tests/test_x.py"]}), "swarm_dispatch",
           {"tasks": [{"prompt": "a", "repo": REPO, "label": "A", "files": ["app/x.py"]},
                      {"prompt": "b", "repo": REPO, "label": "B", "files": ["tests/test_x.py"]}]},
           ("tasks[0] (A)", "tasks[1] (B)"))
    yield ("workflow parallel steps", FakeApi(), "swarm_workflow", {"repo": REPO, "steps": [
        {"step_id": "s1", "prompt": "a", "files": ["app/x.py"]},
        {"step_id": "s2", "prompt": "b", "files": ["app/x.py"]}]}, ("s1", "s2"))
    yield ("workflow spec step vs open PR", FakeApi(pulls={11: ["app/y.py"]}), "swarm_workflow",
           {"spec": {"repository_url": REPO, "steps": [
               {"step_id": "s1", "prompt": "a", "files": ["app/y.py"]}]}}, ("s1", "#11"))


def test_overlapping_dispatches_unannounced_is_zero():
    cases = list(_overlapping_dispatches())
    unannounced = []
    for name, api, tool, args, (one, other) in cases:
        reply = _call(api, tool, args)
        says = _says(reply)
        if one not in says or other not in says:
            unannounced.append(name)
    # The in-flight case needs two calls on one client.
    api = FakeApi()
    _call(api, "swarm_dispatch", {"prompt": "p", "repo": REPO, "label": "early", "files": ["app/z.py"]})
    late = _call(api, "swarm_dispatch", {"prompt": "q", "repo": REPO, "label": "late", "files": ["app/z.py"]})
    if not ("late" in _says(late) and "task_1" in _says(late)):
        unannounced.append("single vs in-flight session task")
    print(f"overlapping dispatches checked: {len(cases) + 1}, unannounced: {len(unannounced)}")
    assert len(cases) + 1 == 7
    assert unannounced == []
