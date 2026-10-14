"""The planner's IN FLIGHT section and the `duplicate` NOT_READY kind (lane CTXQ-PL).

docs/design/context-quality.md §3 (class P), §4.2 (layers I and H), §4.5 (the
plan pipeline). Three issue-run PRs in §2.5's window were closed unmerged,
each a duplicate of a lane already in flight, because the planner could not
see the lane:

  * #1136 -- the sweep planned an issue a lane was already building: another
    live run on the same issue, and an open pull request naming it;
  * #1094 -- built its own PERM-1 beside the one already merged as #1090: a
    recent merge on the same file, which only the index's history (layer H)
    shows;
  * #1080 -- superseded by the MERGE-GAPS lane on `merge.py`: another issue's
    live run whose plan declares the file the issue names.

Each is replayed here through `POST /v1/runs` and the shipped shard writer:
the planner's prompt lists the colliding lane by the id a `duplicate`
verdict names, inside the 24 KiB allowance. The control: with no collision
and no history, the prompt is exactly the prompt this lane found.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import forge, forgewrite, issueruns, plancontext
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.schedtypes import issue_sweep
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import api_settings, auth_header
from .repo_fakes import repo_entry
from .repo_index_fakes import REPOSITORY, IndexGitHub, finish_index_task, fixture_index, sha

ONE = sha("one")
OWNER, REPO = REPOSITORY.split("/")
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# the replayed repository: three issues, their files, the open work
# --------------------------------------------------------------------------

SWEEP = "src/sweep/fire.py"
PERM = "src/perm/check.py"
MERGE = "src/merge/merge.py"
OTHER = "src/billing/invoice.py"

ISSUES = {
    1133: ("The sweep fires twice per cron tick",
           f"`fire_sweep` in {SWEEP} runs once per minute instead of once per tick."),
    1094: ("PERM-1: a permission check route",
           f"Add `check_permission` in {PERM} answering whether a member may act."),
    1080: ("Merge gaps: a merge that skips a red check",
           f"`merge_pull` in {MERGE} merges with a required check still pending."),
    1200: ("Invoices round down", f"`charge_order` in {OTHER} rounds the total down."),
}


class Forge:
    """One fake api.github.com: the issues, the open work, the index's reads."""

    def __init__(self, pulls: list[dict] | None = None,
                 files: dict[int, list[str]] | None = None) -> None:
        self.index = IndexGitHub(heads={"main": ONE},
                                 repos={REPOSITORY: repo_entry(REPOSITORY)})
        self.work = forge_fakes.GitHub(
            issues=[forge_fakes.issue(n, t) for n, (t, _b) in sorted(ISSUES.items())],
            pulls=pulls or [], files=files or {})

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        path = urlparse(url).path
        for number, (title, body) in ISSUES.items():
            if path == f"/repos/{REPOSITORY}/issues/{number}":
                return 200, json.dumps({
                    "number": number, "title": title, "body": body, "state": "open",
                    "labels": [], "comments": 0,
                    "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
                }).encode()
        if path.endswith(("/issues", "/pulls", "/files")):
            return self.work(url, headers, timeout)
        return self.index(url, headers, timeout)


@pytest.fixture
def github() -> Forge:
    return Forge()


@pytest.fixture
def api_context(db, tokens, group_map, objects, github):
    return build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(), forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


def _create_run(client, db, number: int, user: str = "alice") -> tuple[dict, str, dict]:
    created = client.post("/v1/runs", json={"issue": f"{REPOSITORY}#{number}"},
                          headers=auth_header(user))
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    prompt = db.docs[f"tasks/{run['planner_task_id']}"]["input"]["prompt"]
    stored = db.docs[f"{issueruns.RUNS_COLLECTION}/{run['id']}"]
    return run, prompt, stored


def _section(prompt: str, run_id: str) -> str:
    start, end = f"=== IN FLIGHT {run_id} ===", f"=== END IN FLIGHT {run_id} ==="
    assert prompt.count(start) == 1 and prompt.count(end) == 1, prompt[:2000]
    return prompt.split(start, 1)[1].split(end, 1)[0]


def _part(section: str, title: str) -> str:
    """The lines of one part of the section, from its title to the next blank line."""
    assert title in section, section
    return section.split(title, 1)[1].split("\n\n", 1)[0]


def _put_run(db, run_id: str, number: int, state: issueruns.RunState, *,
             files: list[str] | None = None, tenant: str | None = None,
             step_id: str = "impl-gaps") -> None:
    """Another live issue run, as a planner (and an approval) left it."""
    now = datetime(2026, 10, 12, 22, 20, tzinfo=timezone.utc)
    plan = None
    if files is not None:
        plan = {"summary": "fill the merge gaps", "steps": [
            {"step_id": step_id, "title": "merge gaps", "prompt": "do it", "files": files}]}
    tenant_id = tenant or next(
        d["tenant_id"] for k, d in db.docs.items() if k.startswith("tasks/"))
    run = issueruns.IssueRun(
        id=run_id, tenant_id=tenant_id, created_by="lane@example.com", created_at=now,
        updated_at=now, state=state, issue=IssueRef(owner=OWNER, repo=REPO, number=number),
        plan_approval="auto", auto_merge=False, fix_rounds=2, planner_task_id="task_lane",
        plan=plan)
    db.docs[f"{issueruns.RUNS_COLLECTION}/{run_id}"] = run.to_firestore()


# --------------------------------------------------------------------------
# #1136: the sweep planned an issue a lane was already building
# --------------------------------------------------------------------------

def test_1136_a_second_run_on_the_same_issue_lists_the_first_as_its_lane(client, db):
    first, _prompt, _stored = _create_run(client, db, 1133)
    run, prompt, stored = _create_run(client, db, 1133)
    same = _part(_section(prompt, run["id"]), "SAME ISSUE")
    assert f"lane `{first['id']}`" in same
    assert stored["index_context"]["in_flight"]["same_issue"] == [first["id"]]
    # The prompt tells the planner how to answer, naming the lane.
    assert '"kind": "duplicate"' in prompt and '"lane"' in prompt


def test_1136_an_open_pull_request_naming_the_issue_is_its_lane(client, db, api_context):
    api_context.forge = forge.GitHubIssues(send=Forge(
        pulls=[forge_fakes.pull(1134, "The sweep fires once per tick (part of #1133)")],
        files={1134: [SWEEP, "src/sweep/schedule.py", "tests/sweep/test_fire.py"]}))
    run, prompt, stored = _create_run(client, db, 1133)
    same = _part(_section(prompt, run["id"]), "SAME ISSUE")
    assert f"lane `{REPOSITORY}#1134`" in same
    assert f"{REPOSITORY}#1134" in stored["index_context"]["in_flight"]["same_issue"]


def test_1136_a_live_workflow_on_the_same_issue_is_its_lane(client, db, monkeypatch):
    def flight(_ctx, _tenant, _rows):
        return issue_sweep.InFlight(
            labels=[("wf_lane1133", "Sweep fires once (SWEEP-1; part of #1133)"),
                    ("wf_unrelated", "Billing rounding (BILL-2)")],
            links=[], read=2)
    monkeypatch.setattr(issue_sweep, "in_flight", flight)
    run, prompt, stored = _create_run(client, db, 1133)
    section = _section(prompt, run["id"])
    assert "lane `wf_lane1133`" in _part(section, "SAME ISSUE")
    assert "wf_unrelated" not in _part(section, "SAME ISSUE")
    assert stored["index_context"]["in_flight"]["same_issue"] == ["wf_lane1133"]


def test_live_workflows_that_cannot_be_read_are_unknown_never_nothing(client, db, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("store down")
    monkeypatch.setattr(issue_sweep, "in_flight", broken)
    run, prompt, stored = _create_run(client, db, 1200)
    section = _section(prompt, run["id"])
    assert "live workflows: UNKNOWN" in section
    assert stored["index_context"]["in_flight"]["workflows"] == "not read (RuntimeError)"


# --------------------------------------------------------------------------
# #1080: superseded by the MERGE-GAPS lane on merge.py
# --------------------------------------------------------------------------

def test_1080_another_issues_lane_declaring_the_named_file_is_listed(client, db):
    _create_run(client, db, 1200)  # gives the tenant a task to read its id from
    _put_run(db, "run_mergegaps", 1070, issueruns.RunState.RUNNING,
             files=[MERGE, "tests/merge/test_merge.py"])
    _put_run(db, "run_billing", 1201, issueruns.RunState.RUNNING, files=[OTHER])
    run, prompt, stored = _create_run(client, db, 1080)
    same_files = _part(_section(prompt, run["id"]), "SAME FILES")
    assert "lane `run_mergegaps`" in same_files and f"`{MERGE}`" in same_files
    assert "run_billing" not in same_files
    assert stored["index_context"]["in_flight"]["same_files"] == ["run_mergegaps"]


def test_another_tenants_lane_is_never_listed(client, db):
    _create_run(client, db, 1200)
    _put_run(db, "run_elsewhere", 1080, issueruns.RunState.RUNNING, files=[MERGE],
             tenant="tenant-other")
    run, prompt, _stored = _create_run(client, db, 1080)
    assert "run_elsewhere" not in prompt


# --------------------------------------------------------------------------
# #1094: PERM-1 was already merged as #1090 (layer H)
# --------------------------------------------------------------------------

def _sym(path: str, name: str) -> dict:
    return {"id": f"{path}#{name}", "kind": "function", "path": path, "start_line": 1,
            "end_line": 3, "language": "python", "exported": True}


def _graph(writer, extra_symbols: int = 0) -> dict:
    symbols = [_sym(PERM, "check_permission"), _sym(SWEEP, "fire_sweep"),
               _sym(MERGE, "merge_pull"), _sym(OTHER, "charge_order")]
    symbols += [_sym(f"src/bulk/m{i}.py", f"permission_helper_{i}") for i in range(extra_symbols)]
    postings: dict[str, dict[str, list]] = {}
    lengths = []
    for symbol in symbols:
        name = symbol["id"].split("#", 1)[1]
        terms = writer.tokenize(name)
        lengths.append(len(terms))
        for term in sorted(set(terms)):
            postings.setdefault(term, {}).setdefault(symbol["path"], []).append(
                [name, terms.count(term), len(terms)])
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": ONE, "branch": "main",
        "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "3"},
        "symbols": symbols, "call_edges": [], "symbol_test_map": [],
        "files": [{"path": s["path"]} for s in symbols],
        "communities": [], "signatures": [], "signature_changes": [], "flows": [],
        "terms": [{"term": t, "postings": p} for t, p in sorted(postings.items())],
        "term_stats": {"documents": len(symbols),
                       "average_length": sum(lengths) / max(len(lengths), 1),
                       "k1": writer.BM25_K1, "b": writer.BM25_B,
                       "tokenizer": writer.TOKENIZER_VERSION},
    }


def _history(changes: int = 1) -> dict:
    merged = {"pr": 1090, "sha": sha("perm1"), "date": "2026-10-11T09:00:00Z",
              "title": "PERM-1: the permission check answers {allowed, reason} (#1090)",
              "fix": False, "fix_round": False, "fix_subjects": [],
              "files": [PERM, "tests/perm/test_check.py"], "files_total": 2}
    older = [{**merged, "pr": 900 + i, "sha": sha(f"old{i}"), "date": "2026-09-20T09:00:00Z",
              "title": f"An older change to the permission module, number {i} " + "x" * 120}
             for i in range(changes - 1)]
    return {"layers": {"history": [{"path": PERM, "changes": [merged, *older]}],
                       "co_change": [], "test_fixes": [], "rules": [], "cannot_push": [],
                       "test_runners": []},
            "meta": {"available": True}}


def _promote(client, db, objects, writer, tmp_path, document: dict, context: dict | None
             ) -> str:
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    repo_id = created.json()["repository"]["repo_id"]
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header("alice"))
    assert started.status_code == 202, started.text
    task_id = started.json()["run"]["task_id"]
    tenant = db.docs[f"tasks/{task_id}"]["tenant_id"]
    root = tmp_path / f"store-{tenant}-{repo_id}"
    report = writer.write_graph(document, writer.LocalStore(root), tenant_id=tenant,
                                repo_id=repo_id, context=context)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    graph = {"manifest_digest": report["manifest_digest"], "symbols": 4, "edges": 0,
             "top_symbols": []}
    finish_index_task(db, objects, task_id, fixture_index(ONE, graph=graph))
    assert client.get(f"/v1/repositories/{repo_id}/index",
                      headers=auth_header("alice")).status_code == 200
    return tenant


def test_1094_a_recent_merge_on_the_named_file_is_listed(client, db, objects, writer, tmp_path):
    _promote(client, db, objects, writer, tmp_path, _graph(writer), _history())
    run, prompt, stored = _create_run(client, db, 1094)
    merges = _part(_section(prompt, run["id"]), "RECENT MERGES")
    assert f"lane `{REPOSITORY}#1090`" in merges and "PERM-1" in merges
    assert f"`{PERM}`" in merges
    assert stored["index_context"]["in_flight"]["recent_merges"] == [f"{REPOSITORY}#1090"]
    # The index sections are still there, before it.
    assert prompt.index(f"=== REPO GRAPH {run['id']}") < prompt.index(f"=== IN FLIGHT {run['id']}")


# --------------------------------------------------------------------------
# the 24 KiB allowance holds, and the collision is never the part cut
# --------------------------------------------------------------------------

def test_the_allowance_holds_with_every_layer_full(client, db, objects, writer, tmp_path):
    tenant = _promote(client, db, objects, writer, tmp_path, _graph(writer, extra_symbols=400),
                      _history(changes=40))
    for i in range(60):
        _put_run(db, f"run_lane{i:03d}", 2000 + i, issueruns.RunState.RUNNING,
                 files=[PERM] + [f"src/perm/deep/f{i}_{j}_{'y' * 80}.py" for j in range(20)],
                 tenant=tenant)
    _put_run(db, "run_samelane", 1094, issueruns.RunState.PLANNING, tenant=tenant)
    run, prompt, stored = _create_run(client, db, 1094)
    rid = run["id"]
    start = prompt.index(f"=== REPO INDEX {rid}")
    end_marker = f"=== END IN FLIGHT {rid} ==="
    end = prompt.index(end_marker) + len(end_marker)
    assert len(prompt[start:end].encode()) <= plancontext.MAX_CONTEXT_BYTES
    assert len(prompt.encode()) <= issueruns.MAX_PLANNER_PROMPT_BYTES
    section = _section(prompt, rid)
    assert len(section.encode()) <= plancontext.MAX_INFLIGHT_BYTES
    # The same-issue lane comes first and survives the cut; the cut says so.
    assert "lane `run_samelane`" in _part(section, "SAME ISSUE")
    assert "more not shown" in section
    assert stored["index_context"]["bytes"] <= plancontext.MAX_CONTEXT_BYTES


# --------------------------------------------------------------------------
# the control: no collision, no history -- the prompt this lane found
# --------------------------------------------------------------------------

def test_no_collision_and_no_index_gives_exactly_todays_prompt(client, db, api_context):
    api_context.forge = forge.GitHubIssues(send=Forge(
        pulls=[forge_fakes.pull(77, "Billing rounding")], files={77: [OTHER]}))
    _create_run(client, db, 1200)
    _put_run(db, "run_billing", 1201, issueruns.RunState.RUNNING, files=[OTHER])
    run, prompt, stored = _create_run(client, db, 1133)
    assert "=== IN FLIGHT" not in prompt
    ref = IssueRef(owner=OWNER, repo=REPO, number=1133)
    assert prompt == issueruns.planner_prompt(ref, run_id=run["id"], open_work=stored["open_work"])
    assert stored["index_context"]["in_flight"]["state"] == "none"


def test_no_collision_with_an_index_adds_no_section(client, db, objects, writer, tmp_path):
    _promote(client, db, objects, writer, tmp_path, _graph(writer), None)
    run, prompt, stored = _create_run(client, db, 1133)
    assert "=== IN FLIGHT" not in prompt
    assert f"=== REPO GRAPH {run['id']}" in prompt
    assert stored["index_context"]["in_flight"]["state"] == "none"


def test_a_plan_with_no_collision_parses_and_digests_as_before():
    plan = {"summary": "s", "steps": [{"step_id": "a", "title": "t", "prompt": "p",
                                       "files": [SWEEP]}]}
    kind, parsed = issueruns.parse_planner_output(dict(plan))
    assert kind == "plan"
    assert issueruns.plan_digest(parsed) == issueruns.plan_digest(issueruns.parse_plan(plan))


# --------------------------------------------------------------------------
# the `duplicate` NOT_READY kind
# --------------------------------------------------------------------------

def test_a_duplicate_verdict_names_its_lane():
    kind, verdict = issueruns.parse_planner_output({
        "ready": False, "kind": "duplicate", "lane": "run_abc123",
        "reason": "Another run is already building this issue.",
        "needs": ["the lane run_abc123 to finish or be cancelled"]})
    assert kind == "not_ready"
    assert verdict["kind"] == "duplicate" and verdict["lane"] == "run_abc123"
    # The issue comment quotes the reason, so the reason names the lane too.
    assert "run_abc123" in verdict["reason"]


def test_a_duplicate_verdict_without_a_lane_is_refused():
    with pytest.raises(issueruns.InvalidPlan, match="lane"):
        issueruns.parse_planner_output({"ready": False, "kind": "duplicate",
                                        "reason": "somebody else is on it"})


def test_other_kinds_keep_their_shape():
    kind, verdict = issueruns.parse_planner_output({"ready": False, "kind": "blocked",
                                                    "reason": "waits on #3", "needs": []})
    assert kind == "not_ready"
    assert verdict == {"kind": "blocked", "reason": "waits on #3", "needs": []}


def test_the_lane_round_trips_and_is_served():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    run = issueruns.IssueRun(
        id="run_x", tenant_id="t", created_by="a@example.com", created_at=now, updated_at=now,
        state=issueruns.RunState.NOT_READY, issue=IssueRef(owner=OWNER, repo=REPO, number=1),
        plan_approval="auto", auto_merge=False, fix_rounds=2, planner_task_id="task_p",
        not_ready={"kind": "duplicate", "lane": f"{REPOSITORY}#1134", "reason": "r",
                   "needs": []})
    back = issueruns.IssueRun.from_firestore(run.to_firestore())
    assert back.to_api()["not_ready"]["lane"] == f"{REPOSITORY}#1134"
