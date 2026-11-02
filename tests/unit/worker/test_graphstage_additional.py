"""The code graph of a step's additional repositories (docs/design/multi-
repository-steps.md §4.9; lane MR-GRAPH, part of #1434).

For each entry of the task's SIGNED `additional_repositories` the worker makes
the stored-setting decision against that entry's registration, proves an index
version an ancestor of the entry's `commit` in its checkout under
`work/repos/<name>`, pins it at `work/.swarm-context/repos/<name>/` and mints
its own ticket into `private/graph-ticket-<name>`, the body naming the entry's
`repo_id`. The one shim finds them there and takes a `repo` argument that
picks that repository's ticket file. Held here:

  * a version that is not an ancestor of the entry's commit is never pinned;
  * an additional graph is served only beside a served graph of the step's
    own repository; otherwise, and by default, it is staged at
    `repos/<name>/snapshot/`, and an upkeep not served is discarded;
  * the shim's `repo` sends THAT repository's ticket and never forwards `repo`;
  * the primary's request, file, tool list and variables are today's.

Offline: a LocalObjectStore, the worker's FakeFirestore, local git repos and a
fake of swarm-api's route that verifies every proof with swarm-api's code.
Every ticket is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_graph_ticket_worker import Wall, _upkeep, routes  # noqa: F401 - a fixture
from test_graphmcp_shim import fake_ticket, make_shim
from test_graphstage import (
    CLAUDE, GKE, SERVICE_URL, _code_intelligence, _commit, _git, _no_ab, _promote,
)

from agent_worker import children as children_mod
from agent_worker import graph_ticket, graphstage, indexrun
from agent_worker.graphmcp import shim, tools
from swarm_api import graph_ticket as api_graph_ticket

from worker_seeds import TENANT

URL = "https://github.com/acme/widgets"
WEB_URL = "https://github.com/acme/web"
WEB_ID = indexrun.repo_id_for(TENANT, "acme", "web")
SDK_ID = indexrun.repo_id_for(TENANT, "acme", "sdk")


# --------------------------------------------------------------------------
# fixtures: the step's own checkout and graph, and an additional repository
# --------------------------------------------------------------------------


def _init(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    """work/repo (two commits) and work/repos/web (two commits on main, and
    one on a side branch that main never merged)."""
    work = tmp_path / "work"
    repo = work / "repo"
    _init(repo)
    first = _commit(repo, "a.py", "def f():\n    return 1\n")
    second = _commit(repo, "b.py", "def g():\n    return 2\n")
    web = work / "repos" / "web"
    _init(web)
    web_first = _commit(web, "api.py", "def route():\n    return 1\n")
    _git(web, "checkout", "-q", "-b", "side")
    web_side = _commit(web, "side.py", "def side():\n    return 3\n")
    _git(web, "checkout", "-q", "main")
    web_second = _commit(web, "client.py", "def call():\n    return 2\n")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    return SimpleNamespace(work=work, repo=repo, first=first, second=second, web=web,
                           web_first=web_first, web_second=web_second, web_side=web_side,
                           private=private, context=work / graphstage.CONTEXT_DIR_NAME)


def _promote_extra(db: Any, store: Any, repo_id: str, sha: str, *,
                   registration: dict | None = None) -> str:
    """A promoted format-3 graph of `sha` for the registration `repo_id`; its digest."""
    root = f"tenants/{TENANT}/repos/{repo_id}/graph"
    row = {"id": "api.py#route", "path": "api.py", "name": "route", "kind": "function",
           "line": 1}
    blob = gzip.compress(json.dumps(row, sort_keys=True).encode() + b"\n", mtime=0)
    hexdigest = hashlib.sha256(blob).hexdigest()
    manifest = {
        "schema": "swarm.repo-graph-manifest/v1", "tenant_id": TENANT, "repo_id": repo_id,
        "commit_sha": sha, "format_version": 3, "compression": "gzip",
        "blob_suffix": ".jsonl.gz", "term_stats": {},
        "shards": {"symbols": {".": {"blob": f"sha256:{hexdigest}"}}}, "index_shards": {},
    }
    data = json.dumps(manifest, sort_keys=True).encode() + b"\n"
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    store.upload_bytes(f"{root}/{sha}/manifest.json", data)
    store.upload_bytes(f"{root}/blobs/{hexdigest}.jsonl.gz", blob)
    db.seed(f"repositories/{repo_id}", {"tenant_id": TENANT, "repo_id": repo_id,
                                        "index": {"current_sha": sha}, **(registration or {})})
    db.seed(f"repositories/{repo_id}/index_versions/{sha}", {
        "commit_sha": sha, "graph_digest": digest, "built_at": "2026-10-17T00:00:00Z"})
    return digest


def _entry(name: str, repo: str, commit: str, **over: Any) -> dict[str, Any]:
    return {"name": name, "repo_id": indexrun.repo_id_for(TENANT, "acme", repo),
            "repository": f"acme/{repo}", "repository_url": f"https://github.com/acme/{repo}",
            "ref": "main", "commit": commit, "forge_credential": "git",
            "forge_credential_source": "tenant", "size_kib": 64, **over}


class _Upkeep:
    """`graph_ticket.GraphTicket`'s calls graphstage makes, recorded: the
    step's own, and each additional repository's from `for_repository`."""

    def __init__(self, name: str | None = None, outcomes: dict | None = None) -> None:
        self.name = name
        self.outcomes = outcomes if outcomes is not None else {}
        self.started: list[str] = []
        self.discarded = 0
        self.path: Path | None = None
        self.additional: dict[str, _Upkeep] = {}
        self.asked: list[tuple[str, str]] = []

    def for_repository(self, name: str, repo_id: str) -> "_Upkeep":
        self.asked.append((name, repo_id))
        return self.additional.setdefault(name, _Upkeep(name, self.outcomes))

    def start(self, private_dir: Path, index_sha: str) -> str | None:
        self.started.append(index_sha)
        outcome = self.outcomes.get(self.name)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        self.path = Path(private_dir) / graph_ticket.ticket_file_name(self.name)
        self.path.write_text(fake_ticket())
        return None

    def discard(self) -> None:
        self.discarded += 1
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def _serve_all(db: Any) -> None:
    _no_ab(db)
    # Merged: `context_ab` and `graph_delivery` are one document (2026-11-01).
    _code_intelligence(db, graph_delivery="service")


def _prepare(db: Any, store: Any, lay: SimpleNamespace, entries: Any, *,
             ticket: Any) -> dict[str, str]:
    return graphstage.prepare(
        lay.work, profile_name="claude-code", runner_argv=CLAUDE, db=db, store=store,
        tenant_id=TENANT, repository_url=URL, task_id="task_b", checkout=lay.repo,
        base_sha=lay.second, graph_ticket=ticket, private_dir=lay.private, backend=GKE,
        service_url=SERVICE_URL, probe=lambda url, path: None,
        additional_repositories=entries)


def _extra_record(lay: SimpleNamespace, name: str) -> dict[str, Any]:
    path = lay.context / graphstage.REPOS_CONTEXT_DIR_NAME / name / graphstage.STAGE_RECORD_NAME
    return json.loads(path.read_text())


def _extra_pin(lay: SimpleNamespace, name: str) -> Path:
    return lay.context / graphstage.REPOS_CONTEXT_DIR_NAME / name / graphstage.PIN_NAME


# --------------------------------------------------------------------------
# the names the stager, the shim and swarm-api share
# --------------------------------------------------------------------------


def test_the_shims_restated_names_are_the_stagers_and_the_tickets():
    assert shim.REPOS_CONTEXT_DIR_NAME == graphstage.REPOS_CONTEXT_DIR_NAME
    assert shim.PIN_NAME == graphstage.PIN_NAME
    assert shim.REPOS_DIR_NAME == graphstage.REPOS_DIR_NAME
    assert graph_ticket.ticket_file_name("web") == shim.TICKET_FILE_PREFIX + "web"
    assert graph_ticket.ticket_file_name(None) == graph_ticket.TICKET_FILE_NAME
    assert graph_ticket.ADDITIONAL_REQUEST_FIELDS == \
        set(api_graph_ticket.AdditionalGraphTicketRequest.model_fields)


@pytest.mark.parametrize("bad", ["..", ".web", "../web", "Web", "a/b", "", "x" * 64])
def test_a_ticket_file_name_never_leaves_private(bad):
    with pytest.raises(ValueError):
        graph_ticket.ticket_file_name(bad)


# --------------------------------------------------------------------------
# the ticket: one upkeep per additional repository, renewed by the primary's
# --------------------------------------------------------------------------


def test_the_primarys_body_is_todays_and_an_additional_ones_adds_only_its_repo_id(routes):
    upkeep = _upkeep(routes)
    graph_ticket.mint(upkeep.cfg, upkeep.children._key, routes, index_sha="a" * 40,
                      now=1_000.0)
    graph_ticket.mint(upkeep.cfg, upkeep.children._key, routes, index_sha="c" * 40,
                      now=1_000.0, repo_id=WEB_ID)
    own, extra = (json.loads(call["body"]) for call in routes.calls)
    assert set(own) == graph_ticket.REQUEST_FIELDS
    assert set(extra) == graph_ticket.REQUEST_FIELDS | {"repo_id"}
    assert extra["repo_id"] == WEB_ID and extra["index_sha"] == "c" * 40
    request = api_graph_ticket.AdditionalGraphTicketRequest.model_validate(extra)
    assert request.repo_id == WEB_ID
    assert api_graph_ticket.AdditionalGraphTicketRequest.model_validate(own).repo_id is None


def test_an_additional_upkeep_writes_its_own_file_and_the_primarys_poll_renews_it(
        routes, tmp_path):
    wall = Wall()
    primary = _upkeep(routes, wall=wall)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    web = primary.for_repository("web", WEB_ID)
    assert primary.for_repository("web", WEB_ID) is web
    assert primary.start(private, "a" * 40) is None
    assert web.start(private, "c" * 40) is None
    assert web.path == private / "graph-ticket-web"
    assert stat.S_IMODE(web.path.stat().st_mode) == 0o600
    assert web.path.read_text() != primary.path.read_text()
    assert sorted(p.name for p in private.iterdir()) == ["graph-ticket", "graph-ticket-web"]
    assert json.loads(routes.calls[1]["body"])["repo_id"] == WEB_ID

    wall.now += 600
    assert primary.due()
    primary.keep()
    renewed = [json.loads(call["body"]).get("repo_id") for call in routes.calls[2:]]
    assert renewed == [None, WEB_ID]
    assert web.renewals == 1 and primary.renewals == 1


def test_a_409_at_an_additional_renewal_is_the_fence_for_the_attempt(routes, tmp_path):
    wall = Wall()
    primary = _upkeep(routes, wall=wall)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    web = primary.for_repository("web", WEB_ID)
    assert web.start(private, "c" * 40) is None
    wall.now += 600
    routes.fail = children_mod.ChildApiError(409, "conflict", "fenced", retryable=False)
    with pytest.raises(graph_ticket.GraphTicketFenced):
        primary.keep()


def test_an_upkeep_refuses_a_name_or_repo_id_off_the_contracts_shape(routes):
    primary = _upkeep(routes)
    for name, repo_id in (("..", WEB_ID), ("web", "acme/web"), ("web", None)):
        with pytest.raises(ValueError):
            primary.for_repository(name, repo_id)  # type: ignore[arg-type]
    web = primary.for_repository("web", WEB_ID)
    with pytest.raises(ValueError):
        primary.for_repository("web", SDK_ID)
    with pytest.raises(ValueError):
        web.for_repository("sdk", SDK_ID)


# --------------------------------------------------------------------------
# staging: pinned beside the step's own, proven against the entry's commit
# --------------------------------------------------------------------------


def test_an_indexed_additional_repository_is_pinned_and_ticketed_beside_the_primary(
        db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    digest = _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    # The runner's variables are the step's own, exactly.
    assert env == {graphstage.CONTEXT_ENV: str(lay.context),
                   graphstage.PIN_ENV: str(lay.context / graphstage.PIN_NAME),
                   graphstage.TICKET_FILE_ENV: str(upkeep.path),
                   graphstage.URL_ENV: SERVICE_URL}
    assert upkeep.asked == [("web", WEB_ID)]
    web = upkeep.additional["web"]
    assert web.started == [lay.web_first]
    assert web.path == lay.private / "graph-ticket-web"
    pin = json.loads(_extra_pin(lay, "web").read_text())
    # Proven in work/repos/web against the ENTRY's commit, not the primary's base.
    assert pin == {"index_sha": lay.web_first, "base_sha": lay.web_second, "behind_by": 1,
                   "manifest_digest": digest, "basis": "git", "changed": ["client.py"]}
    record = _extra_record(lay, "web")
    assert record["delivery"] == "service" and record["name"] == "web"
    assert record["repository"] == "acme/web" and record["commit"] == lay.web_second
    # Pinned, never copied: no snapshot for an additional repository.
    assert not (_extra_pin(lay, "web").parent / graphstage.SNAPSHOT_DIR_NAME).exists()


def test_an_index_version_that_is_not_an_ancestor_of_the_entrys_commit_is_not_pinned(
        db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_side)
    upkeep = _Upkeep()

    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    assert env[graphstage.PIN_ENV] == str(lay.context / graphstage.PIN_NAME)
    web = upkeep.additional["web"]
    assert web.started == []
    assert not _extra_pin(lay, "web").exists()
    assert not (lay.private / "graph-ticket-web").exists()
    record = _extra_record(lay, "web")
    assert record["delivery"] == "none"
    assert "is not an ancestor" in record["reason"]


def test_the_default_delivery_stages_an_additional_graph_under_its_name(
        db, store, layout):
    """The default delivery stages the step's own graph, and each indexed
    additional repository's at `repos/<name>/snapshot/`, proven against the
    entry's commit; no ticket is asked for."""
    lay = layout
    _no_ab(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    assert env[graphstage.SNAPSHOT_ENV] == str(lay.context / graphstage.SNAPSHOT_DIR_NAME)
    assert upkeep.asked == [] and upkeep.started == []
    assert not (lay.private / "graph-ticket-web").exists()
    web_dir = _extra_pin(lay, "web").parent
    assert sorted(p.name for p in web_dir.iterdir()) == [
        graphstage.SNAPSHOT_DIR_NAME, graphstage.STAGE_RECORD_NAME]
    snapshot = web_dir / graphstage.SNAPSHOT_DIR_NAME
    freshness = json.loads((snapshot / "freshness.json").read_text())
    assert freshness["index_sha"] == lay.web_first
    assert freshness["base_sha"] == lay.web_second
    manifest = json.loads((snapshot / "manifest.json").read_text())
    assert manifest["repo_id"] == WEB_ID
    record = _extra_record(lay, "web")
    assert record["delivery"] == "staged" and record["graph"] is True
    assert record["snapshot"] == str(snapshot)


def test_an_additional_graph_is_staged_not_served_when_the_primary_was_not_served(
        db, store, layout):
    """`service` asked for, but the step's own graph had no index: only its
    shim reaches an additional ticket, so none is minted and the entry stages."""
    lay = layout
    _serve_all(db)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    assert graphstage.PIN_ENV not in env
    assert upkeep.asked == []
    assert not _extra_pin(lay, "web").exists()
    record = _extra_record(lay, "web")
    assert record["delivery"] == "staged" and record["graph"] is True
    assert "served only beside the step's own" in record["fallback"]
    assert (_extra_pin(lay, "web").parent / graphstage.SNAPSHOT_DIR_NAME).is_dir()


def test_an_additional_graph_whose_probe_fails_is_staged_and_its_upkeep_discarded(
        db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    env = graphstage.prepare(
        lay.work, profile_name="claude-code", runner_argv=CLAUDE, db=db, store=store,
        tenant_id=TENANT, repository_url=URL, task_id="task_b", checkout=lay.repo,
        base_sha=lay.second, graph_ticket=upkeep, private_dir=lay.private, backend=GKE,
        service_url=SERVICE_URL,
        probe=lambda url, path: "refused" if path.name == "graph-ticket-web" else None,
        additional_repositories=[_entry("web", "web", lay.web_second)])

    assert env[graphstage.PIN_ENV] == str(lay.context / graphstage.PIN_NAME)
    web = upkeep.additional["web"]
    assert web.started == [lay.web_first]
    # Discarded, so the primary's control poll never renews a ticket nothing reads.
    assert web.discarded >= 1 and not (lay.private / "graph-ticket-web").exists()
    assert upkeep.discarded == 0
    assert not _extra_pin(lay, "web").exists()
    record = _extra_record(lay, "web")
    assert record["delivery"] == "staged" and record["graph"] is True


def test_an_unindexed_or_unregistered_entry_says_why_and_the_others_still_pin(
        db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    _prepare(db, store, lay, [_entry("sdk", "sdk", lay.web_second),
                              _entry("web", "web", lay.web_second)], ticket=upkeep)

    assert _extra_record(lay, "sdk")["delivery"] == "none"
    assert _extra_record(lay, "sdk")["graph"] is False
    assert "not registered" in _extra_record(lay, "sdk")["reason"]
    assert _extra_record(lay, "web")["delivery"] == "service"
    assert _extra_record(lay, "web")["graph"] is True


def test_an_entry_whose_repo_id_is_not_its_repositorys_is_never_asked_for(
        db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    _prepare(db, store, lay, [_entry("web", "sdk", lay.web_second, repo_id=WEB_ID)],
             ticket=upkeep)

    assert upkeep.asked == []
    assert "is not this tenant's registration" in _extra_record(lay, "web")["reason"]


def test_a_malformed_list_pins_nothing_and_leaves_the_primary_served(db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()

    env = _prepare(db, store, lay,
                   [_entry("web", "web", lay.web_second, forge_credential="git-r-x")],
                   ticket=upkeep)

    assert env[graphstage.PIN_ENV] == str(lay.context / graphstage.PIN_NAME)
    assert upkeep.asked == []
    assert not (lay.context / graphstage.REPOS_CONTEXT_DIR_NAME).exists()


def test_a_restored_checkpoints_additional_pin_is_removed(db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    stale = lay.context / graphstage.REPOS_CONTEXT_DIR_NAME / "old"
    stale.mkdir(parents=True)
    (stale / graphstage.PIN_NAME).write_text("{}")

    _prepare(db, store, lay, None, ticket=_Upkeep())

    assert not (lay.context / graphstage.REPOS_CONTEXT_DIR_NAME).exists()


def test_the_setting_refusing_an_additional_graph_pins_nothing_for_it(db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep(outcomes={"web": graph_ticket.GraphTicketForbidden("refused (forbidden)")})

    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    assert env[graphstage.PIN_ENV] == str(lay.context / graphstage.PIN_NAME)
    assert not _extra_pin(lay, "web").exists()
    assert _extra_record(lay, "web")["delivery"] == "none"


def test_a_409_at_an_additional_mint_is_the_fence(db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep(outcomes={"web": graph_ticket.GraphTicketFenced("graph ticket: 409")})

    with pytest.raises(graph_ticket.GraphTicketFenced):
        _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)


# --------------------------------------------------------------------------
# the shim: `repo` picks that repository's ticket file
# --------------------------------------------------------------------------


class _Recording:
    """The service: records each request's ticket and arguments, answers a page."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        message = json.loads(body)
        self.seen.append((headers["Authorization"].removeprefix("Bearer "),
                          message["params"].get("arguments")))
        text = tools.dumps({"freshness": {"index_sha": "0" * 40}, "matches": 0, "total": 0,
                            "rows": [], "next_cursor": None})
        return 200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": text}], "isError": False}}).encode()


def _with_web(tmp_path: Path, transport: Any, web_sha: str = "c" * 40):
    own = fake_ticket()
    server, ticket_file = make_shim(tmp_path, transport, ticket=own)
    web_ticket = fake_ticket()
    (ticket_file.parent / "graph-ticket-web").write_text(web_ticket + "\n")
    pin = tmp_path / "web-pin.json"
    pin.write_text(json.dumps({"index_sha": web_sha, "base_sha": "d" * 40, "behind_by": 1,
                               "changed": [], "basis": "git"}))
    server.repositories = {"web": shim.Repository(
        "web", pin=pin, ticket=shim.TicketFile(ticket_file.parent / "graph-ticket-web"),
        workdir=None)}
    return server, own, web_ticket


def _call(server: shim.ShimServer, arguments: Any) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "search", "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def test_the_shims_repo_argument_picks_its_own_ticket_and_is_never_forwarded(tmp_path):
    service = _Recording()
    server, own, web_ticket = _with_web(tmp_path, service)

    on_web = _call(server, {"query": "route", "repo": "web"})
    on_own = _call(server, {"query": "route"})

    assert service.seen == [(web_ticket, {"query": "route"}), (own, {"query": "route"})]
    # Each answer is marked by its own repository's observation.
    assert on_web["freshness"]["index_sha"] == "c" * 40
    assert on_own["freshness"]["index_sha"] == "a" * 40


def test_a_repo_that_names_nothing_pinned_is_told_so_and_nothing_is_sent(tmp_path):
    service = _Recording()
    server, _, _ = _with_web(tmp_path, service)
    for bad in ("sdk", "../web", 7, ""):
        answer = _call(server, {"query": "route", "repo": bad})
        assert "error" in answer and answer["freshness"]["index_sha"] == "a" * 40
    assert "web" in _call(server, {"query": "x", "repo": "sdk"})["error"]
    assert service.seen == []


def test_a_one_repository_step_lists_and_forwards_exactly_todays(tmp_path):
    service = _Recording()
    server, _ = make_shim(tmp_path, service, ticket=fake_ticket())
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed["result"]["tools"] == tools.TOOLS
    answer = _call(server, {"query": "route", "repo": "web"})
    assert answer["error"] == shim.NO_REPOSITORIES
    assert service.seen == []


def test_a_multi_repository_step_lists_repo_as_an_optional_enum_of_the_pinned(tmp_path):
    server, _, _ = _with_web(tmp_path, _Recording())
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    for tool, today in zip(listed["result"]["tools"], tools.TOOLS):
        schema = tool["inputSchema"]
        assert schema["properties"][tools.REPO_ARGUMENT]["enum"] == ["web"]
        assert tools.REPO_ARGUMENT not in schema.get("required", [])
        assert {k: v for k, v in schema["properties"].items()
                if k != tools.REPO_ARGUMENT} == today["inputSchema"]["properties"]
    # TOOLS, what the service lists and validates against, is unchanged.
    assert all(tools.REPO_ARGUMENT not in t["inputSchema"]["properties"] for t in tools.TOOLS)


def test_the_shim_discovers_what_the_stager_pinned(db, store, layout):
    lay = layout
    _serve_all(db)
    _promote(db, store, lay.first)
    _promote_extra(db, store, WEB_ID, lay.web_first)
    upkeep = _Upkeep()
    env = _prepare(db, store, lay, [_entry("web", "web", lay.web_second)], ticket=upkeep)

    found = shim.discover(env[graphstage.PIN_ENV], env[graphstage.TICKET_FILE_ENV])

    assert sorted(found) == ["web"]
    web = found["web"]
    assert web.ticket.path == upkeep.additional["web"].path
    assert web.workdir == lay.work / graphstage.REPOS_DIR_NAME / "web"
    index_sha, freshness = web.load()
    assert index_sha == lay.web_first and freshness.behind_by == 1


def test_discovery_follows_no_link_and_takes_no_name_off_the_contracts_shape(tmp_path):
    context = tmp_path / "work" / graphstage.CONTEXT_DIR_NAME
    root = context / graphstage.REPOS_CONTEXT_DIR_NAME
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / graphstage.PIN_NAME).write_text("{}")
    root.mkdir(parents=True)
    (root / "linked").symlink_to(elsewhere)
    (root / "Bad.Name").mkdir()
    (root / "Bad.Name" / graphstage.PIN_NAME).write_text("{}")
    (root / "nopin").mkdir()
    assert shim.discover(context / graphstage.PIN_NAME, tmp_path / "private" / "t") == {}

