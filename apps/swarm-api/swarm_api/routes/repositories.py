"""`/v1/repositories`: register, list, read, change and unregister a repository (lane RI1).

docs/repo-index.md §6.1. Tenant-scoped exactly as issue runs are: the caller's
tenant comes from `tenant_scope`, never from the body or the path, and another
tenant's `repo_id` is a 404 indistinguishable from a missing one.

`GET /v1/repositories/readable` is the Register C picker's list (PICKS.md,
2026-10-05): what the tenant's git token can read, one GitHub page per call,
capped at `repositories.MAX_READABLE_PAGES`. Everything about the token is
`swarm_api.forge`'s and `swarm_api.repositories`'s; these routes log the
outcome's code -- never the token, never the forge's text.

Who may register: any member of the tenant, as for issue runs. The design's
"an admin of the tenant" (repo-index.md §1, git-tokens.md §1) names a role
this API does not have yet; a platform admin acts in a tenant only as a member
of it, like everywhere else here.

THE INDEX ROUTES (lane RI2, repo-index.md §6.1) are below the registration
routes: `GET/POST /{repo_id}/index` (and `POST /{repo_id}/index:run`, the
design's name for "Index now"), `GET /{repo_id}/index/runs` and
`POST /{repo_id}/tests:select`. Each starts from the registration read with
the caller's tenant, so another tenant's `repo_id` is the same 404 before any
forge read or submission. Everything else is `swarm_api.repoindex`'s.

THE IMPACT AND GRAPH ROUTES (lane RI11, repo-index.md §4.3a, §6.1) are last:
`POST /{repo_id}/impact` (a pull request, a commit or a base..head range ->
the test plan), and the graph explorer's reads `GET /{repo_id}/graph`,
`GET /{repo_id}/symbols`, `GET /{repo_id}/languages` and the paged
`GET /{repo_id}/test-map?path=&cursor=` (QA G4-07). Same rule: the
registration first, with the caller's tenant. Everything else is
`swarm_api.impact`'s; these routes log counts and codes, never a token,
a path or a symbol name.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from swarm_common.models import Tenant

from ..auth import AuthContext
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..errors import ValidationFailed
from ..forge import ForgeReadError
from ..impact import (
    DEPTH_MAX,
    NEIGHBOURHOOD_DEPTH_DEFAULT,
    SEARCH_LIMIT_DEFAULT,
    SEARCH_LIMIT_MAX,
    ImpactRequest,
    ImpactService,
    check_symbol_id,
    languages_table,
    module_graph,
    neighbourhood,
    search_symbols,
    symbol_tests,
)
from ..repograph import NoGraph
from ..repoindex import (
    MAX_CURSOR_CHARS,
    RUNS_PAGE_MAX,
    TEST_MAP_PAGE_DEFAULT,
    TEST_MAP_PAGE_MAX,
    IndexRunRequest,
    RepoIndex,
    SelectRequest,
    check_run_kind,
    freshness,
    page_test_map,
    render_markdown,
    run_to_api,
    select_tests,
    version_to_api,
)
from ..repositories import (
    MAX_READABLE_PAGES,
    Repositories,
    RepositoryCreate,
    RepositoryPatch,
    readable,
    register,
    to_api,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/repositories", tags=["repositories"])


def _store(ctx: AppContext) -> Repositories:
    return Repositories(ctx.db, now=ctx.now)


def _tenant(ctx: AppContext, tenant_id: str, auth: AuthContext) -> Tenant:
    # The secret is named from the tenant id through the frozen
    # `Tenant.secret_name`; a tenant declared in Terraform may have no
    # Firestore document yet, and its secret is named the same either way
    # (the issue preview's rule, routes/issues.py).
    return ctx.store.get_tenant(tenant_id) or Tenant(
        tenant_id=tenant_id,
        kind="group",
        principal=auth.tenant_principal or auth.email,
        created_at=ctx.now(),
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_repository(
    body: RepositoryCreate,
    response: Response,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        record, created = register(
            body, tenant, created_by=auth.email, store=_store(ctx),
            tokens=ctx.forge_tokens, forge=ctx.forge, now=ctx.now,
        )
    except ForgeReadError as refused:
        log.info("repository register tenant=%s outcome=%s", tenant_id, refused.code)
        raise
    if not created:
        response.status_code = status.HTTP_200_OK
    log.info(
        "repository register tenant=%s repo_id=%s outcome=%s",
        tenant_id, record["repo_id"], "created" if created else "existing",
    )
    return {"repository": to_api(record), "created": created, "tenant_id": tenant_id}


@router.get("")
def list_repositories(
    limit: int | None = Query(default=None),
    page_token: str | None = Query(default=None, max_length=256),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    rows, next_token = _store(ctx).list(
        tenant_id, limit=paged_limit(ctx, limit), page_token=page_token
    )
    return {
        "repositories": [to_api(row) for row in rows],
        "next_page_token": next_token,
        "tenant_id": tenant_id,
    }


# Declared before `/{repo_id}`, which would otherwise match "readable".
@router.get("/readable")
def readable_repositories(
    page: int = Query(default=1, ge=1, le=MAX_READABLE_PAGES),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        body = readable(
            tenant, page, store=_store(ctx), tokens=ctx.forge_tokens, forge=ctx.forge
        )
    except ForgeReadError as refused:
        log.info("repository readable tenant=%s page=%d outcome=%s", tenant_id, page, refused.code)
        raise
    log.info(
        "repository readable tenant=%s page=%d listed=%d outcome=ok",
        tenant_id, page, len(body["repositories"]),
    )
    return {**body, "tenant_id": tenant_id}


@router.get("/{repo_id}")
def get_repository(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"repository": to_api(_store(ctx).get(tenant_id, repo_id)), "tenant_id": tenant_id}


@router.patch("/{repo_id}")
def patch_repository(
    repo_id: str,
    body: RepositoryPatch,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    record = _store(ctx).patch(tenant_id, repo_id, body)
    log.info(
        "repository patch tenant=%s repo_id=%s by=%s fields=%s", tenant_id, record["repo_id"],
        auth.email, ",".join(sorted(body.model_dump(exclude_none=True))),
    )
    return {"repository": to_api(record), "tenant_id": tenant_id}


@router.delete("/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(
    repo_id: str,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> Response:
    _store(ctx).delete(tenant_id, repo_id)
    log.info("repository delete tenant=%s repo_id=%s by=%s", tenant_id, repo_id, auth.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# the index (lane RI2)
# --------------------------------------------------------------------------

def _index(ctx: AppContext) -> RepoIndex:
    return RepoIndex.from_context(ctx)


def _start_index(
    repo_id: str, body: IndexRunRequest, tenant_id: str, auth: AuthContext, ctx: AppContext
) -> dict:
    kind = check_run_kind(body.kind)
    service = _index(ctx)
    # The registration first: another tenant's id is a 404 before the forge.
    service.registrations.get(tenant_id, repo_id)
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        answer = service.request_run(auth, tenant_id, repo_id, tenant=tenant, kind=kind)
    except ForgeReadError as refused:
        log.info("repository index run tenant=%s repo_id=%s outcome=%s",
                 tenant_id, repo_id, refused.code)
        raise
    log.info(
        "repository index run tenant=%s repo_id=%s by=%s outcome=%s", tenant_id, repo_id,
        auth.email, "coalesced" if answer["coalesced"] else "submitted",
    )
    return {**answer, "repo_id": repo_id, "tenant_id": tenant_id}


@router.post("/{repo_id}/index", status_code=status.HTTP_202_ACCEPTED)
def index_now(
    repo_id: str,
    body: IndexRunRequest | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return _start_index(repo_id, body or IndexRunRequest(), tenant_id, auth, ctx)


@router.post("/{repo_id}/index:run", status_code=status.HTTP_202_ACCEPTED)
def index_run(
    repo_id: str,
    body: IndexRunRequest | None = None,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return _start_index(repo_id, body or IndexRunRequest(), tenant_id, auth, ctx)


@router.get("/{repo_id}/index")
def get_index(
    repo_id: str,
    sha: str | None = Query(default=None, min_length=40, max_length=40),
    format: Literal["markdown", "json"] = Query(default="markdown"),
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The promoted index (or a kept one, `?sha=`), its freshness, the run that made it.

    Reading settles the registration's runs first -- a finished run is
    promoted, a pending head submitted -- the way reading an issue run
    advances it, so the answer is never older than the tasks behind it.
    """
    service = _index(ctx)
    service.registrations.get(tenant_id, repo_id)
    tenant = _tenant(ctx, tenant_id, auth)
    service.settle(tenant_id, repo_id, auth=auth, tenant=tenant)
    service.refresh_relation(tenant_id, repo_id, tenant)
    record = service.registrations.get(tenant_id, repo_id)
    index_state = dict(record.get("index") or {})
    in_flight_id = index_state.get("in_flight_task_id")
    in_flight = None
    if in_flight_id:
        runs = [r for r in service.runs(tenant_id, repo_id) if r.get("task_id") == in_flight_id]
        in_flight = run_to_api(runs[0]) if runs else None
    version = service.version(tenant_id, repo_id, sha)
    if version is not None and sha is not None and sha != index_state.get("current_sha"):
        # A kept version: its own sha against the head, its own build time.
        index_state.update(current_sha=sha, current_built_at=version.get("built_at"))
    fresh = freshness(index_state, now=ctx.now())
    body: dict = {
        "repo_id": repo_id,
        "tenant_id": tenant_id,
        "index": None,
        "summary": None,
        "document": None,
        "freshness": fresh,
        "produced_by": None,
        "in_flight": in_flight,
        "pending_sha": index_state.get("pending_sha"),
    }
    if version is None:
        return body
    document = service.read_version(tenant_id, version)
    produced = [r for r in service.runs(tenant_id, repo_id, limit=RUNS_PAGE_MAX)
                if r.get("task_id") == version.get("task_id")]
    body.update(
        index=version_to_api(version),
        produced_by={
            "task_id": version.get("task_id"),
            "attempt_id": version.get("attempt_id"),
            "run": run_to_api(produced[0]) if produced else None,
        },
    )
    if format == "json":
        body["document"] = document
    else:
        body["summary"] = render_markdown(
            document, repository=f"{record['owner']}/{record['repo']}", freshness=fresh
        )
    return body


@router.get("/{repo_id}/index/runs")
def list_index_runs(
    repo_id: str,
    limit: int = Query(default=20, ge=1, le=RUNS_PAGE_MAX),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    service = _index(ctx)
    service.registrations.get(tenant_id, repo_id)
    return {
        "repo_id": repo_id,
        "tenant_id": tenant_id,
        "runs": [run_to_api(row) for row in service.runs(tenant_id, repo_id, limit=limit)],
    }


@router.post("/{repo_id}/tests:select")
def select_repository_tests(
    repo_id: str,
    body: SelectRequest,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Which tests cover the changed paths, from the promoted index (§4.3)."""
    service = _index(ctx)
    record = service.registrations.get(tenant_id, repo_id)
    fresh = freshness(record.get("index") or {}, now=ctx.now())
    answer = {
        "repo_id": repo_id,
        "tenant_id": tenant_id,
        "index_sha": fresh["index_sha"],
        "head_sha": fresh["head_sha"],
        "behind_by": fresh["behind_by"],
        "stale": fresh["stale"],
        "freshness": fresh,
    }
    version = service.version(tenant_id, repo_id)
    if version is None:
        paths = list(dict.fromkeys(body.paths))
        return {**answer, "tests": [], "always": [], "unmapped": paths, "fallback": None,
                "fallback_covers_every_unmapped_path": None,
                "reason": "no index has been promoted for this repository, so no path is mapped"}
    document = service.read_version(tenant_id, version)
    return {**answer, **select_tests(document, body.paths)}


# --------------------------------------------------------------------------
# impact and the graph explorer (lane RI11)
# --------------------------------------------------------------------------

def _impact(ctx: AppContext) -> ImpactService:
    return ImpactService.from_context(ctx)


@router.post("/{repo_id}/impact")
def repository_impact(
    repo_id: str,
    body: ImpactRequest,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The test plan for a pull request, a commit or a range (§4.3a)."""
    service = _impact(ctx)
    # The registration first: another tenant's id is a 404 before the forge.
    record = service.index.registrations.get(tenant_id, repo_id)
    tenant = _tenant(ctx, tenant_id, auth)
    try:
        plan = service.query(record, body, tenant=tenant,
                             tenant_doc=ctx.store.get_tenant(tenant_id))
    except ForgeReadError as refused:
        log.info("repository impact tenant=%s repo_id=%s outcome=%s",
                 tenant_id, repo_id, refused.code)
        raise
    log.info(
        "repository impact tenant=%s repo_id=%s plan=%s changed=%d affected=%d selected=%d "
        "selection=%s triggers=%d", tenant_id, repo_id, plan["plan_id"],
        plan["changed_symbols"], plan["affected_callers"], plan["targeted"],
        plan["selection"], len(plan["fallback_triggers"]),
    )
    return {**plan, "repo_id": repo_id, "tenant_id": tenant_id}


def _graph_read(ctx: AppContext, tenant_id: str, repo_id: str, sha: str | None):
    """(service, version, graph, document, the staleness values) for a graph route."""
    service = _impact(ctx)
    _record, version, fresh = service.version_and_freshness(tenant_id, repo_id, sha)
    if version is None:
        raise NoGraph("no index has been promoted for this repository, so it has no graph")
    graph = service.open(tenant_id, repo_id, version)
    if graph is None:
        raise NoGraph(f"the index of commit {version.get('commit_sha')} has no graph")
    document = service.index.read_version(tenant_id, version)
    return service, version, graph, document, fresh


def _staleness(version: dict, fresh: dict) -> dict:
    return {"index_sha": version.get("commit_sha"), "head_sha": fresh["head_sha"],
            "behind_by": fresh["behind_by"], "stale": fresh["stale"], "freshness": fresh}


#: The layers the module graph reads: every shard of each, fetched at once.
_DRAWING_LAYERS = ("symbols", "tests", "callees")


def _etag_of(body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return '"' + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32] + '"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 §13.1.2: a weak comparison, so `W/"x"` matches `"x"`."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


@router.get("/{repo_id}/graph")
def repository_graph(
    repo_id: str,
    request: Request,
    sha: str | None = Query(default=None, min_length=40, max_length=40),
    cluster: Literal["module", "package"] = Query(default="module"),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> Response:
    """The module dependency graph, aggregated for drawing (§6.1, Graph A).

    QA G4-10 (2026-10-07) measured 11 s for a 24 KB answer: every shard read
    one after another (~190 for 64 modules, two GCS round trips each), then
    the 419 KB index document only for its hot spots. Now the manifest is
    still read and digest-checked on every request (`service.open`, so a
    rewritten or deleted graph is refused exactly as before), and the drawing
    is computed once per graph digest, index digest and cluster -- both
    digests are fixed at promotion, so the drawing cannot change under them.
    A cold drawing reads its shards and the document concurrently. The ETag
    is the body's, which carries the freshness, so a moved head is a new tag.
    """
    service = _impact(ctx)
    _record, version, fresh = service.version_and_freshness(tenant_id, repo_id, sha)
    if version is None:
        raise NoGraph("no index has been promoted for this repository, so it has no graph")
    graph = service.open(tenant_id, repo_id, version)
    if graph is None:
        raise NoGraph(f"the index of commit {version.get('commit_sha')} has no graph")

    def draw() -> dict:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="repo-index") as pool:
            document = pool.submit(service.index.read_version, tenant_id, version)
            graph.prefetch(_DRAWING_LAYERS)
            return module_graph(graph, document.result(), cluster)

    drawing = service.graphs.view(
        (tenant_id, repo_id, graph.digest, version.get("digest"), "module_graph", cluster), draw)
    body = {"repo_id": repo_id, "tenant_id": tenant_id, **_staleness(version, fresh),
            "graph_digest": graph.digest, **drawing}
    etag = _etag_of(body)
    # private: the answer is one tenant's. no-cache: a browser keeps it but
    # asks every time, and the manifest check above runs on that ask.
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(body, headers=headers)


@router.get("/{repo_id}/symbols")
def repository_symbols(
    repo_id: str,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    id: str | None = Query(default=None, min_length=1, max_length=500),
    depth: int = Query(default=NEIGHBOURHOOD_DEPTH_DEFAULT, ge=1, le=DEPTH_MAX),
    direction: Literal["callers", "callees", "both"] = Query(default="both"),
    tests: bool = Query(default=False),
    limit: int = Query(default=SEARCH_LIMIT_DEFAULT, ge=1, le=SEARCH_LIMIT_MAX),
    sha: str | None = Query(default=None, min_length=40, max_length=40),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """`?q=` search; `?id=&depth=&direction=` one symbol's call graph; `?id=&tests=1`
    its test map."""
    if (q is None) == (id is None):
        raise ValidationFailed("name exactly one of q (a search) or id (a symbol)")
    _service, version, graph, document, fresh = _graph_read(ctx, tenant_id, repo_id, sha)
    answer: dict = {"repo_id": repo_id, "tenant_id": tenant_id, **_staleness(version, fresh),
                    "graph_digest": graph.digest}
    if q is not None:
        return {**answer, **search_symbols(graph, q, limit)}
    symbol_id = check_symbol_id(id or "")
    if tests:
        return {**answer, **symbol_tests(graph, document, symbol_id)}
    return {**answer, **neighbourhood(graph, symbol_id, depth=depth, direction=direction)}


@router.get("/{repo_id}/languages")
def repository_languages(
    repo_id: str,
    sha: str | None = Query(default=None, min_length=40, max_length=40),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The `languages` table: per language, grammar, server, status and fallback."""
    service = _impact(ctx)
    _record, version, fresh = service.version_and_freshness(tenant_id, repo_id, sha)
    answer: dict = {"repo_id": repo_id, "tenant_id": tenant_id, "index_sha": None,
                    "head_sha": fresh["head_sha"], "behind_by": fresh["behind_by"],
                    "stale": fresh["stale"], "freshness": fresh, "languages": [],
                    "source": None}
    if version is None:
        return {**answer, "reason": "no index has been promoted for this repository"}
    graph = service.open(tenant_id, repo_id, version)
    document = service.index.read_version(tenant_id, version) if graph is None else None
    return {**answer, **_staleness(version, fresh),
            "languages": languages_table(graph, document),
            "source": "graph" if graph is not None else "index"}


@router.get("/{repo_id}/test-map")
def repository_test_map(
    repo_id: str,
    path: str = Query(min_length=1, max_length=400),
    cursor: str | None = Query(default=None, min_length=1, max_length=MAX_CURSOR_CHARS),
    limit: int = Query(default=TEST_MAP_PAGE_DEFAULT, ge=1, le=TEST_MAP_PAGE_MAX),
    sha: str | None = Query(default=None, min_length=40, max_length=40),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of the test-map edges for a source path or glob (QA G4-07).

    From the graph's file-level map when the index has a graph -- the whole
    map, each edge with its confidence and other evidence -- plus the
    document's edges; from the document alone when it has none.
    `repoindex.page_test_map` says how a path and a glob match.
    """
    service = _impact(ctx)
    _record, version, fresh = service.version_and_freshness(tenant_id, repo_id, sha)
    answer: dict = {"repo_id": repo_id, "tenant_id": tenant_id, "index_sha": None,
                    "head_sha": fresh["head_sha"], "behind_by": fresh["behind_by"],
                    "stale": fresh["stale"], "freshness": fresh, "graph_digest": None}
    if version is None:
        return {**answer, "path": path, "glob": None, "edges": [], "total": 0, "limit": limit,
                "next_cursor": None, "sources": {"graph": False, "index": False},
                "reason": "no index has been promoted for this repository, so no path is mapped"}
    graph = service.open(tenant_id, repo_id, version)
    document = service.index.read_version(tenant_id, version)
    page = page_test_map(document, graph, path, cursor=cursor, limit=limit,
                         commit_sha=version.get("commit_sha"))
    # Counts only: a path or a test name is the tenant's, never a log line's.
    log.info("repository test map tenant=%s repo_id=%s graph=%s edges=%d total=%d",
             tenant_id, repo_id, graph is not None, len(page["edges"]), page["total"])
    return {**answer, **_staleness(version, fresh),
            "graph_digest": graph.digest if graph is not None else None, **page}
