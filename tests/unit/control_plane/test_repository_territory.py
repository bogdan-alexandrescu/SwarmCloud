"""Search, communities and territory over a promoted graph (docs/design/knowledge-graph.md §6, lane KG3).

What is held here:

  * the API's tokenizer and BM25 scorer are the writer's, input for input
    (`territory.py` restates them because the writer runs in another image);
  * `POST .../search` ranks by BM25 and reads only the term buckets of the
    query's own terms; a graph without a term index is searched by
    substring and says so;
  * `GET .../communities` serves KG2's partition, whole or one community;
  * `POST .../territory` widens files to their callers and tests from fact
    edges only -- a judged or ambiguous edge is counted in `cut`, never
    followed -- names the seams, and reports the overlap with the open pull
    requests and the tenant's live issue runs, saying what it could not read;
  * TENANT ISOLATION (invariant 9): another tenant's repository answers all
    three routes as missing, before any shard or forge read; a same-named
    registration of another tenant reads none of this graph, and one
    tenant's issue runs never appear in another's overlap;
  * every answer carries an ETag and a matching `If-None-Match` is a 304,
    counted under the route's template as `3xx` -- the hit rate KG3's
    success measure reads; a warm territory read is the manifest plus the
    named modules' shards, which is what bounds its p95.

Fixture graphs are written by the shipped shard writer and promoted through
the API, so the routes read exactly the shards an index run stores. Every
token is built at runtime; every sha is made from a word.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from swarm_api import territory
from swarm_api.issueruns import IssueRun, IssueRuns, RunState
from swarm_api.validation import IssueRef

from .conftest import auth_header
from .repo_fakes import make_client
from .repo_index_fakes import REPOSITORY, finish_index_task, fixture_index, sha
from .test_repository_impact import (
    NOW,
    ImpactGitHub,
    SlotTokens,
    _recording,
    edge,
    graph_document,
    sym,
)

BASE, HEAD = sha("territory-base"), sha("territory-head")
WRITER = (Path(__file__).resolve().parents[3] / "images" / "agent-runtime-indexer" / "repo-index"
          / "repo_graph_shards.py")
OWNER, NAME = REPOSITORY.split("/")


@pytest.fixture(scope="module")
def writer() -> Any:
    spec = importlib.util.spec_from_file_location("repo_graph_shards", WRITER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_graph_shards"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# the fixture repository
# --------------------------------------------------------------------------
#
#   app/main.py       create_app imports and calls six route handlers: fan-OUT 6
#   app/schemas.py    Shape, used by all six handlers:                  fan-IN 6
#   app/routes/rN.py  handler_N
#   app/orders.py     OrderService.total, called by handler_1 (lsp), by
#                     app/legacy.py through a `naming` guess (judged) and by
#                     app/maybe.py through an ambiguous 0.3 `ast` edge, and by
#                     app/cochange.py through a `co-change` edge at 0.5 (the
#                     extractor's CO_CHANGE_CAP): judged, though above the floor
#   tests/...         test_total -> OrderService.total, test_handler_1 -> handler_1,
#                     and six tests of app/legacy.py: dependants a seam never counts

MAIN, SCHEMAS, ORDERS = "app/main.py", "app/schemas.py", "app/orders.py"
LEGACY, MAYBE, COCHANGE = "app/legacy.py", "app/maybe.py", "app/cochange.py"
T_ORDERS, T_R1 = "tests/test_orders.py", "tests/test_r1.py"
ROUTES = [f"app/routes/r{i}.py" for i in range(1, 7)]
TOTAL = f"{ORDERS}#OrderService.total"
CREATE_APP = f"{MAIN}#create_app"
SHAPE = f"{SCHEMAS}#Shape"
HANDLER = {i: f"app/routes/r{i}.py#handler_{i}" for i in range(1, 7)}


def _postings(writer, symbols: list[dict]) -> tuple[list[dict], dict]:
    """A term index built the way KG2's extractor builds one (name weighted x3)."""
    postings: dict[str, dict[str, list[list]]] = {}
    lengths = documents = 0
    for s in symbols:
        if s["kind"] == "test":
            continue
        qual = s["id"].split("#", 1)[1]
        owner, _dot, name = qual.rpartition(".")
        stem = s["path"].rsplit("/", 1)[-1].split(".")[0]
        terms = writer.tokenize(name) * 3 + writer.tokenize(owner) + writer.tokenize(stem)
        documents += 1
        lengths += len(terms)
        counts: dict[str, int] = {}
        for t in terms:
            counts[t] = counts.get(t, 0) + 1
        for t, n in counts.items():
            postings.setdefault(t, {}).setdefault(s["path"], []).append([qual, n, len(terms)])
    rows = [{"term": t, "postings": {p: sorted(e) for p, e in sorted(postings[t].items())}}
            for t in sorted(postings)]
    stats = {"documents": documents, "average_length": round(lengths / documents, 4),
             "k1": writer.BM25_K1, "b": writer.BM25_B, "tokenizer": writer.TOKENIZER_VERSION,
             "name_weight": 3}
    return rows, stats


def territory_graph(writer, commit: str = BASE, *, index: bool = True) -> dict:
    symbols = [
        sym(MAIN, "create_app", 1, 30),
        sym(SCHEMAS, "Shape", 1, 20, kind="class"),
        sym(ORDERS, "OrderService", 1, 40, kind="class"),
        sym(ORDERS, "OrderService.total", 10, 20, kind="method"),
        sym(LEGACY, "old_total", 1, 10),
        sym(MAYBE, "maybe_total", 1, 10),
        sym(COCHANGE, "along", 1, 10),
        sym(T_ORDERS, "test_total", 1, 5, kind="test"),
        sym(T_R1, "test_handler_1", 1, 5, kind="test"),
    ]
    edges = []
    for i in range(1, 7):
        symbols.append(sym(ROUTES[i - 1], f"handler_{i}", 1, 10))
        edges.append(edge(HANDLER[i], SHAPE))
        edges.append(edge(CREATE_APP, HANDLER[i]))
        edges.append(edge(MAIN, ROUTES[i - 1], 0.4, "import", kind="import"))
    edges += [
        edge(HANDLER[1], TOTAL),
        edge(f"{LEGACY}#old_total", TOTAL, 0.3, "naming"),
        edge(f"{MAYBE}#maybe_total", TOTAL, 0.3, "ast"),
        edge(f"{COCHANGE}#along", TOTAL, 0.5, "co-change"),
        edge(f"{T_ORDERS}#test_total", TOTAL),
        edge(f"{T_R1}#test_handler_1", HANDLER[1]),
    ]
    for i in range(1, 7):
        path = f"tests/legacy/test_l{i}.py"
        symbols.append(sym(path, f"test_l{i}", 1, 5, kind="test"))
        edges.append(edge(f"{path}#test_l{i}", f"{LEGACY}#old_total"))
    test_map = [
        {"symbol": TOTAL, "test": f"{T_ORDERS}#test_total", "depth": 1, "confidence": 0.95},
        {"symbol": TOTAL, "test": f"{T_R1}#test_handler_1", "depth": 2, "confidence": 0.9},
        {"symbol": HANDLER[1], "test": f"{T_R1}#test_handler_1", "depth": 1,
         "confidence": 0.95},
    ]
    document = graph_document(commit, symbols, edges, test_map)
    if index:
        terms, stats = _postings(writer, symbols)
        document.update(
            terms=terms, term_stats=stats,
            communities=[
                {"id": "c-orders", "label": "app", "files": [ORDERS, ROUTES[0], LEGACY, MAYBE],
                 "size": 4, "symbols": 5, "cohesion": 0.8},
                {"id": "c-routes", "label": "app/routes",
                 "files": [MAIN, SCHEMAS, *ROUTES[1:]], "size": 7, "symbols": 7,
                 "cohesion": 0.7},
            ],
            signatures=[{"symbol": TOTAL, "signature": "(self) -> int",
                         "fingerprint": "sha256:" + "0" * 16}],
            flows=[],
        )
        document["extractor"] = {"name": "swarm-repo-index", "version": "3"}
    return document


INDEX = fixture_index(
    BASE,
    hot_spots=[{"path": ROUTES[1], "changes": 9,
                "co_changed": [ROUTES[2], ROUTES[3], SCHEMAS]}],
)


class TerritoryGitHub(ImpactGitHub):
    """`ImpactGitHub`, plus the open issue and pull request listings `open_work` reads."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.open_pulls: list[dict[str, Any]] = []
        self.listing_status = 200
        self.unreadable: set[int] = set()

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        parts = urlparse(url).path.strip("/").split("/")
        if len(parts) == 6 and parts[3] == "pulls" and int(parts[4]) in self.unreadable:
            # Not 404: `open_work` fails the whole read on a 404, and lists a
            # pull request whose files failed otherwise with `files: None`.
            self.calls.append((url, dict(headers)))
            return 502, b'{"message": "bad gateway"}'
        if len(parts) == 4 and parts[0] == "repos" and parts[3] in ("issues", "pulls"):
            self.calls.append((url, dict(headers)))
            if self.listing_status != 200:
                return self.listing_status, b'{"message": "refused"}'
            if parts[3] == "issues":
                return 200, b"[]"
            listed = [{"number": p["number"], "title": p["title"]} for p in self.open_pulls]
            return 200, json.dumps(listed).encode()
        return super().__call__(url, headers, timeout)

    def add_pull(self, number: int, title: str, files: list[str] | None) -> None:
        self.open_pulls.append({"number": number, "title": title})
        if files is None:
            self.unreadable.add(number)
        else:
            self.pulls[number] = {"base": BASE, "head": HEAD,
                                  "files": [{"filename": f, "status": "modified"}
                                            for f in files]}


@pytest.fixture
def github() -> TerritoryGitHub:
    return TerritoryGitHub(heads={"main": BASE})


@pytest.fixture
def client(db, tokens, group_map, objects, github):
    return make_client(db, tokens, group_map, objects, forge_tokens=SlotTokens(),
                       transport=github)


def _register(client, user: str) -> str:
    created = client.post("/v1/repositories", json={"repository": REPOSITORY},
                          headers=auth_header(user))
    assert created.status_code == 201, created.text
    return created.json()["repository"]["repo_id"]


def _promote(client, db, objects, writer, tmp_path, user: str, document: dict) -> dict:
    repo_id = _register(client, user)
    started = client.post(f"/v1/repositories/{repo_id}/index", json={},
                          headers=auth_header(user))
    assert started.status_code == 202, started.text
    task_id = started.json()["run"]["task_id"]
    tenant = db.docs[f"tasks/{task_id}"]["tenant_id"]
    root = tmp_path / f"promoted-{user}-{secrets.token_hex(4)}"
    report = writer.write_graph(document, writer.LocalStore(root), tenant_id=tenant,
                                repo_id=repo_id)
    for path in sorted(root.rglob("*")):
        if path.is_file():
            objects.put(path.relative_to(root).as_posix(), path.read_bytes())
    finish_index_task(db, objects, task_id, {**INDEX, "graph": {
        "manifest_digest": report["manifest_digest"], "symbols": len(document["symbols"]),
        "edges": len(document["call_edges"]), "top_symbols": []}})
    settled = client.get(f"/v1/repositories/{repo_id}/index", headers=auth_header(user))
    assert settled.status_code == 200, settled.text
    assert db.docs[f"repositories/{repo_id}"]["index"]["current_sha"] == BASE
    return {"repo_id": repo_id, "tenant": tenant}


@pytest.fixture
def indexed(client, db, objects, writer, tmp_path) -> dict:
    """Alice's tenant registers the repository and promotes the format-3 graph."""
    return _promote(client, db, objects, writer, tmp_path, "alice", territory_graph(writer))


def _run(db, tenant: str, run_id: str, state: RunState, steps: list[dict] | None,
         repository: str = REPOSITORY, pull: int | None = None) -> None:
    owner, name = repository.split("/")
    at = NOW - timedelta(hours=1)
    run = IssueRun(
        id=run_id, tenant_id=tenant, created_by="alice@saga.xyz", created_at=at, updated_at=at,
        state=state, issue=IssueRef(owner=owner, repo=name, number=40 + len(run_id)),
        plan_approval="auto", auto_merge=False, fix_rounds=2, planner_task_id="t1",
        plan=None if steps is None else {"summary": "s", "steps": steps},
        pull_request=None if pull is None else {"number": pull, "url": "u", "head_sha": HEAD},
    )
    IssueRuns(db).create(run)


def _territory(client, repo_id: str, user: str = "alice", **body: Any):
    return client.post(f"/v1/repositories/{repo_id}/territory", json=body,
                       headers=auth_header(user))


def _paths(rows: list[dict]) -> list[str]:
    return [r["path"] for r in rows]


# --------------------------------------------------------------------------
# the restated tokenizer and scorer
# --------------------------------------------------------------------------

CORPUS = [
    "planner_prompt", "plannerPrompt", "the planner's prompt", "HTTPServer v2 sha256 base64",
    "OrderService.total", "runs Runs class glass", "", "a an the of", "x", "IDs URLs ID",
    "_open_work_section(work, marker, budget)", "émigré naïve 名前", "123 4567 a1b2",
]


def test_the_tokenizer_and_the_scorer_are_the_writers(writer):
    assert territory.TOKENIZER_VERSION == writer.TOKENIZER_VERSION
    assert (territory.BM25_K1, territory.BM25_B) == (writer.BM25_K1, writer.BM25_B)
    assert territory.TERM_BUCKET_CHARS == writer.TERM_BUCKET_CHARS
    assert territory.WHOLE == writer.WHOLE
    assert territory.STOPWORDS == writer.STOPWORDS
    assert set(territory.INDEX_LAYERS) == set(writer.INDEX_LAYERS)
    assert territory.INDEX_FORMAT == writer.FORMAT_VERSION
    for text in CORPUS:
        assert territory.tokenize(text) == writer.tokenize(text), text
        for term in writer.tokenize(text):
            assert territory.term_bucket(term) == writer.term_bucket(term)
    rows, stats = _postings(writer, territory_graph(writer)["symbols"])
    for query in ("order total", "handler", "create app shape", "nothing here", "Shape"):
        for limit in (1, 3, 20):
            assert territory.bm25_search(rows, stats, query, limit) == \
                writer.bm25_search(rows, stats, query, limit)
    # The control: the comparison can fail -- a query does rank something.
    assert territory.bm25_search(rows, stats, "order total", 3)


@pytest.mark.parametrize("entry, expected", [
    ("app/orders.py", "app/orders.py"), ("./app/orders.py", "app/orders.py"),
    ("app/routes/", "app/routes/"), (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
])
def test_an_entry_is_a_path_or_a_directory_prefix(entry, expected):
    assert territory.normalise_entry(entry) == expected


@pytest.mark.parametrize("entry", ["/etc/passwd", "app/../secrets", "app/*.py", "app//x.py",
                                   "", "app/\x00x", "a" * 401, "app/{a,b}.py"])
def test_an_entry_that_is_not_a_normalised_path_is_refused(entry):
    with pytest.raises(ValueError):
        territory.normalise_entry(entry)


def test_two_entries_conflict_by_path_or_by_directory_prefix():
    assert territory.conflicts("app/x.py", "app/x.py")
    assert territory.conflicts("app/", "app/routes/r1.py")
    assert territory.conflicts("app/routes/r1.py", "app/")
    assert not territory.conflicts("app/x.py", "app/x.pyc")
    assert not territory.conflicts("app", "app/x.py")
    assert not territory.conflicts("apps/", "app/x.py")


# --------------------------------------------------------------------------
# POST .../search
# --------------------------------------------------------------------------

def test_search_ranks_by_bm25_with_module_and_community(client, indexed):
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/search",
                         json={"q": "the order service's total", "limit": 3},
                         headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["method"] == "bm25" and body["reason"] is None
    assert body["format_version"] == 3 and body["index_sha"] == BASE
    assert body["symbols"][0]["id"] == TOTAL
    top = body["symbols"][0]
    assert (top["kind"], top["path"], top["module"]) == ("method", ORDERS, "app")
    assert top["community"] == {"id": "c-orders", "label": "app"}
    assert top["score"] > 0 and len(body["symbols"]) <= 3
    assert body["terms"] == sorted({"order", "service", "total"})
    # Tests are not in the term index: a test is found through what it covers.
    assert not [s for s in body["symbols"] if s["kind"] == "test"]


def test_search_reads_only_the_term_buckets_of_its_own_terms(client, objects, indexed,
                                                             writer, monkeypatch):
    seen = _recording(objects, monkeypatch)
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/search",
                         json={"q": "handler"}, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    manifest_key = next(k for k in seen["keys"] if k.endswith("/manifest.json"))
    manifest = json.loads(objects.objects[manifest_key])
    terms_layer = manifest["index_shards"]["terms"]
    # The control: the index has many buckets, so reading all of them is visible.
    assert len(terms_layer) > 5
    wanted = {terms_layer[writer.term_bucket("handler")]["blob"].split(":")[1]}
    read_terms = {k.rsplit("/", 1)[1].split(".")[0] for k in seen["keys"]
                  if "/blobs/" in k} & {e["blob"].split(":")[1] for e in terms_layer.values()}
    assert read_terms == wanted


def test_a_graph_without_a_term_index_is_searched_by_substring_and_says_so(
    client, db, objects, writer, tmp_path
):
    plain = _promote(client, db, objects, writer, tmp_path, "alice",
                     territory_graph(writer, index=False))
    answer = client.post(f"/v1/repositories/{plain['repo_id']}/search",
                         json={"q": "total"}, headers=auth_header("alice"))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["method"] == "substring" and "substring" in body["reason"]
    assert TOTAL in [s["id"] for s in body["symbols"]]
    communities = client.get(f"/v1/repositories/{plain['repo_id']}/communities",
                             headers=auth_header("alice")).json()
    assert communities["communities"] == [] and communities["reason"]


def test_a_rewritten_term_shard_is_refused_not_searched(client, objects, indexed, writer):
    manifest_key = next(k for k in objects.objects if k.endswith(f"/graph/{BASE}/manifest.json"))
    manifest = json.loads(objects.objects[manifest_key])
    blob = manifest["index_shards"]["terms"][writer.term_bucket("handler")]["blob"]
    key = next(k for k in objects.objects if blob.split(":")[1] in k)
    objects.put(key, objects.objects[key] + b"\x00")
    refused = client.post(f"/v1/repositories/{indexed['repo_id']}/search",
                          json={"q": "handler"}, headers=auth_header("alice"))
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "graph_digest_mismatch"


@pytest.mark.parametrize("body", [{}, {"q": ""}, {"q": "x", "limit": 0},
                                  {"q": "x", "limit": 101}, {"q": "x", "bucket": "b"},
                                  {"q": "x", "sha": "main"}])
def test_a_search_request_is_a_query_and_nothing_else(client, indexed, body):
    answer = client.post(f"/v1/repositories/{indexed['repo_id']}/search", json=body,
                         headers=auth_header("alice"))
    assert answer.status_code in (400, 422), answer.text


# --------------------------------------------------------------------------
# GET .../communities
# --------------------------------------------------------------------------

def test_communities_serves_the_partition_and_one_community(client, indexed):
    path = f"/v1/repositories/{indexed['repo_id']}/communities"
    whole = client.get(path, headers=auth_header("alice"))
    assert whole.status_code == 200, whole.text
    body = whole.json()
    assert [c["id"] for c in body["communities"]] == ["c-orders", "c-routes"]
    assert body["communities"][0]["files"] == sorted([ORDERS, ROUTES[0], LEGACY, MAYBE])
    assert body["communities"][1]["size"] == 7 and body["format_version"] == 3
    one = client.get(f"{path}?id=c-routes", headers=auth_header("alice")).json()
    assert [c["id"] for c in one["communities"]] == ["c-routes"]
    assert client.get(f"{path}?id=c-none", headers=auth_header("alice")).status_code == 404
    assert client.get(f"{path}?id=a%20b", headers=auth_header("alice")).status_code in (400, 422)


# --------------------------------------------------------------------------
# POST .../territory
# --------------------------------------------------------------------------

def test_territory_widens_a_file_to_its_fact_callers_and_tests(client, indexed):
    answer = _territory(client, indexed["repo_id"], files=[ORDERS], pull_requests=False,
                        lanes=False)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["named"] == [ORDERS] and body["unknown"] == []
    assert _paths(body["callers"]) == [ROUTES[0]]
    assert body["callers"][0] == {"path": ROUTES[0], "depth": 1, "via": [TOTAL]}
    assert _paths(body["tests"]) == [T_ORDERS, T_R1]
    # The judged `naming` and `co-change` edges and the ambiguous 0.3 guess are
    # counted, never followed -- the co-change one although it is above the floor.
    assert body["cut"] == {"below_floor": 1, "judged": 2}
    for judged in (LEGACY, MAYBE, COCHANGE):
        assert judged not in json.dumps(body["callers"]) + json.dumps(body["tests"])
    assert body["communities"] == [{"id": "c-orders", "label": "app",
                                    "files": [ORDERS, ROUTES[0]]}]
    assert body["seams"] == [] and body["truncated"] == []


def test_territory_at_depth_two_reaches_the_registration_seam(client, indexed):
    body = _territory(client, indexed["repo_id"], files=[ORDERS], depth=2,
                      pull_requests=False, lanes=False).json()
    assert _paths(body["callers"]) == [ROUTES[0], MAIN]
    assert [(s["path"], s["reasons"], s["where"]) for s in body["seams"]] == \
        [(MAIN, ["fan_out"], "caller")]
    assert body["seams"][0]["fan_out"] == 6


def test_territory_names_the_shared_shapes_and_the_co_change_seams(client, indexed):
    body = _territory(client, indexed["repo_id"], files=[SCHEMAS, ROUTES[1]],
                      pull_requests=False, lanes=False).json()
    seams = {s["path"]: s for s in body["seams"]}
    assert seams[SCHEMAS]["reasons"] == ["fan_in"] and seams[SCHEMAS]["fan_in"] == 6
    assert seams[ROUTES[1]]["reasons"] == ["co_change"] and seams[ROUTES[1]]["changes"] == 9
    assert seams[SCHEMAS]["where"] == seams[ROUTES[1]]["where"] == "named"
    # handler_2's caller is the registration file, a seam it reaches.
    assert seams[MAIN]["where"] == "caller" and set(seams) == {SCHEMAS, ROUTES[1], MAIN}
    # The control: a file only tests depend on is no seam and reaches none.
    leaf = _territory(client, indexed["repo_id"], files=[LEGACY], pull_requests=False,
                      lanes=False).json()
    assert leaf["seams"] == []


def test_territory_takes_a_directory_prefix_and_names_what_the_graph_does_not_know(
    client, indexed
):
    body = _territory(client, indexed["repo_id"], files=["app/routes/", "app/new_thing.py"],
                      pull_requests=False, lanes=False).json()
    assert body["named"] == ["app/new_thing.py", "app/routes/"]
    assert body["unknown"] == ["app/new_thing.py"]
    assert _paths(body["callers"]) == [MAIN]
    assert T_R1 in _paths(body["tests"])


def test_territory_reports_the_overlap_with_open_pull_requests(client, github, indexed):
    github.add_pull(71, "Rework the first handler", [ROUTES[0], "README.md"])
    github.add_pull(72, "Unrelated docs", ["docs/x.md"])
    github.add_pull(73, "Files not read", None)
    github.add_pull(74, "Our own pull request", [ORDERS])
    answer = _territory(client, indexed["repo_id"], files=[ORDERS], lanes=False,
                        exclude_pull_requests=[74])
    assert answer.status_code == 200, answer.text
    overlap = answer.json()["overlap"]
    assert overlap["pull_requests_read"]["ok"] is True
    assert [p["number"] for p in overlap["pull_requests"]] == [71]
    assert overlap["pull_requests"][0]["shared"] == [
        {"path": ROUTES[0], "entry": ROUTES[0], "why": "caller", "seam": False}]
    # Not read is not "no overlap".
    assert overlap["pull_requests_unread"] == [73]
    # Read with the tenant's own token.
    listing = [h for u, h in github.calls if u.split("?")[0].endswith("/pulls")]
    assert listing and all(h["Authorization"].startswith("Bearer ") for h in listing)
    assert all(h["Authorization"].split(" ", 1)[1] not in answer.text for h in listing)


def test_the_open_pull_requests_are_reused_within_their_ttl(client, github, indexed,
                                                           monkeypatch):
    github.add_pull(71, "Rework the first handler", [ROUTES[0]])
    assert _territory(client, indexed["repo_id"], files=[ORDERS], lanes=False).status_code == 200
    listed = len([u for u, _h in github.calls if u.split("?")[0].endswith("/pulls")])
    assert _territory(client, indexed["repo_id"], files=[SCHEMAS], lanes=False).status_code == 200
    assert len([u for u, _h in github.calls if u.split("?")[0].endswith("/pulls")]) == listed
    # The control: past the TTL the listing is read again.
    monkeypatch.setattr(territory, "OPEN_PULLS_TTL_SECONDS", 0.0)
    assert _territory(client, indexed["repo_id"], files=[ORDERS], lanes=False).status_code == 200
    assert len([u for u, _h in github.calls if u.split("?")[0].endswith("/pulls")]) == listed + 1


def test_a_forge_failure_is_reported_never_read_as_no_overlap(client, github, indexed):
    github.listing_status = 503
    answer = _territory(client, indexed["repo_id"], files=[ORDERS], lanes=False)
    assert answer.status_code == 200, answer.text
    overlap = answer.json()["overlap"]
    assert overlap["pull_requests_read"] == {"ok": False, "code": "read_failed",
                                             "read_at": None}
    assert overlap["pull_requests"] == []
    # A failure is not kept: the next request reads again.
    github.listing_status = 200
    github.add_pull(71, "Rework", [ORDERS])
    again = _territory(client, indexed["repo_id"], files=[ORDERS], lanes=False).json()
    assert again["overlap"]["pull_requests_read"]["ok"] is True
    assert [p["number"] for p in again["overlap"]["pull_requests"]] == [71]


def test_territory_reports_the_overlap_with_the_tenants_live_lanes(client, db, indexed):
    tenant = indexed["tenant"]
    _run(db, tenant, "run_a", RunState.RUNNING,
         [{"step_id": "s1", "title": "t", "prompt": "p", "files": ["app/routes/"]},
          {"step_id": "s2", "title": "t", "prompt": "p", "files": ["docs/x.md"]}], pull=80)
    _run(db, tenant, "run_b", RunState.PLANNING, None)
    _run(db, tenant, "run_mine", RunState.RUNNING,
         [{"step_id": "s1", "title": "t", "prompt": "p", "files": [ORDERS]}])
    _run(db, tenant, "run_done", RunState.DONE,
         [{"step_id": "s1", "title": "t", "prompt": "p", "files": [ORDERS]}])
    _run(db, tenant, "run_elsewhere", RunState.RUNNING,
         [{"step_id": "s1", "title": "t", "prompt": "p", "files": [ORDERS]}],
         repository="saga-xyz/other")
    answer = _territory(client, indexed["repo_id"], files=[ORDERS], pull_requests=False,
                        exclude_runs=["run_mine"])
    assert answer.status_code == 200, answer.text
    overlap = answer.json()["overlap"]
    assert [(lane["run_id"], lane["step_id"], lane["pull_request"])
            for lane in overlap["lanes"]] == [("run_a", "s1", 80)]
    assert overlap["lanes"][0]["shared"] == [
        {"path": "app/routes/", "entry": ROUTES[0], "why": "caller", "seam": False}]
    assert [lane["run_id"] for lane in overlap["lanes_undeclared"]] == ["run_b"]
    assert overlap["lanes_truncated"] is False
    # The control: without the exclusion, the caller's own run is an overlap.
    mine = _territory(client, indexed["repo_id"], files=[ORDERS], pull_requests=False).json()
    assert {lane["run_id"] for lane in mine["overlap"]["lanes"]} == {"run_a", "run_mine"}


@pytest.mark.parametrize("body", [
    {}, {"files": []}, {"files": ["app/*.py"]}, {"files": ["../x"]}, {"files": ["/abs"]},
    {"files": [ORDERS], "depth": 3}, {"files": [ORDERS], "image": "busybox"},
    {"files": [ORDERS], "exclude_runs": ["../x"]}, {"files": [f"f{i}.py" for i in range(61)]},
])
def test_a_territory_request_is_paths_and_nothing_that_runs(client, github, indexed, body):
    before = len(github.calls)
    answer = _territory(client, indexed["repo_id"], **body)
    assert answer.status_code in (400, 422), answer.text
    assert len(github.calls) == before


# --------------------------------------------------------------------------
# tenant isolation (invariant 9)
# --------------------------------------------------------------------------

def _three(repo_id: str) -> list[tuple[str, str, dict]]:
    return [
        ("post", f"/v1/repositories/{repo_id}/search", {"json": {"q": "total"}}),
        ("get", f"/v1/repositories/{repo_id}/communities", {}),
        ("post", f"/v1/repositories/{repo_id}/territory", {"json": {"files": [ORDERS]}}),
    ]


def test_another_tenants_repository_answers_as_missing(client, github, objects, indexed,
                                                       monkeypatch):
    github.add_pull(71, "Rework", [ORDERS])
    seen = _recording(objects, monkeypatch)
    before = len(github.calls)
    for method, path, kwargs in _three(indexed["repo_id"]):
        answer = getattr(client, method)(path, headers=auth_header("bob"), **kwargs)
        assert answer.status_code == 404, (path, answer.text)
        assert "OrderService" not in answer.text and ORDERS not in answer.text
        assert "etag" not in answer.headers
    # Missing before any shard or forge read.
    assert seen["keys"] == [] and len(github.calls) == before
    # The control: the owner's tenant reads all three.
    for method, path, kwargs in _three(indexed["repo_id"]):
        assert getattr(client, method)(path, headers=auth_header("alice"),
                                       **kwargs).status_code == 200, path


def test_a_same_named_registration_of_another_tenant_reads_none_of_this_graph(
    client, db, github, indexed
):
    bob_repo = _register(client, "bob")
    assert bob_repo != indexed["repo_id"]
    _run(db, indexed["tenant"], "run_alice", RunState.RUNNING,
         [{"step_id": "s1", "title": "t", "prompt": "p", "files": [ORDERS]}])
    search = client.post(f"/v1/repositories/{bob_repo}/search", json={"q": "total"},
                         headers=auth_header("bob"))
    assert search.status_code == 404 and search.json()["code"] == "no_graph"
    communities = client.get(f"/v1/repositories/{bob_repo}/communities",
                             headers=auth_header("bob"))
    assert communities.status_code == 404 and "c-orders" not in communities.text
    answer = _territory(client, bob_repo, user="bob", files=[ORDERS], pull_requests=False)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["graph_digest"] is None and body["index_sha"] is None
    assert body["callers"] == [] and body["tests"] == [] and body["seams"] == []
    # Alice's live run on the same repository is not Bob's lane.
    assert body["overlap"]["lanes"] == [] and body["overlap"]["lanes_undeclared"] == []
    # The control: it is Alice's.
    alice = _territory(client, indexed["repo_id"], files=[ORDERS], pull_requests=False).json()
    assert [lane["run_id"] for lane in alice["overlap"]["lanes"]] == ["run_alice"]


# --------------------------------------------------------------------------
# ETags, and what a warm read costs (KG3's success measures)
# --------------------------------------------------------------------------

def _hits(client, route: str, method: str, status: str) -> float:
    metric = client.app.state.ctx.metrics.requests.labels(route=route, method=method,
                                                          status=status)
    return metric._value.get()


@pytest.mark.parametrize("method, suffix, kwargs", [
    ("post", "search", {"json": {"q": "order total"}}),
    ("get", "communities", {}),
    ("post", "territory", {"json": {"files": [ORDERS], "lanes": False}}),
])
def test_every_read_carries_an_etag_and_a_match_is_a_counted_304(
    client, db, indexed, method, suffix, kwargs
):
    path = f"/v1/repositories/{indexed['repo_id']}/{suffix}"
    template = "/v1/repositories/{repo_id}/" + suffix
    first = getattr(client, method)(path, headers=auth_header("alice"), **kwargs)
    assert first.status_code == 200, first.text
    etag = first.headers.get("etag")
    assert etag and etag.startswith('"') and "private" in first.headers["cache-control"]
    before = _hits(client, template, method.upper(), "3xx")
    same = getattr(client, method)(path, headers={**auth_header("alice"),
                                                  "If-None-Match": f'"x", W/{etag}'}, **kwargs)
    assert same.status_code == 304 and same.content == b"" and same.headers["etag"] == etag
    assert _hits(client, template, method.upper(), "3xx") == before + 1
    # The control: another tag is a full answer; a moved head is a new tag.
    other = getattr(client, method)(path, headers={**auth_header("alice"),
                                                   "If-None-Match": '"other"'}, **kwargs)
    assert other.status_code == 200 and other.json() == first.json()
    db.docs[f"repositories/{indexed['repo_id']}"]["index"]["head_sha"] = HEAD
    moved = getattr(client, method)(path, headers={**auth_header("alice"),
                                                   "If-None-Match": etag}, **kwargs)
    assert moved.status_code == 200 and moved.headers["etag"] != etag


def test_a_warm_territory_read_is_the_manifest_and_the_named_modules(client, objects, indexed,
                                                                    monkeypatch):
    path = f"/v1/repositories/{indexed['repo_id']}/territory"
    body = {"files": [ORDERS], "pull_requests": False, "lanes": False}
    cold = _recording(objects, monkeypatch)
    assert client.post(path, json=body, headers=auth_header("alice")).status_code == 200
    cold_reads = len(cold["keys"])
    monkeypatch.undo()
    warm = _recording(objects, monkeypatch)
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        assert client.post(path, json=body, headers=auth_header("alice")).status_code == 200
        timings.append(time.perf_counter() - started)
    per_request = len(warm["keys"]) / 20
    # The seam ranks (every callees and files shard, and the index document)
    # are read once per digest: a warm read is far below the cold one.
    assert per_request < cold_reads
    assert not [k for k in warm["keys"] if k.endswith("/repo-index.json")]
    # The manifest is still read and digest-checked on every request.
    assert sum(1 for k in warm["keys"] if k.endswith("/manifest.json")) == 20
    timings.sort()
    # Generous: an in-memory store measured a few milliseconds per read.
    assert timings[int(len(timings) * 0.95) - 1] < 1.0
