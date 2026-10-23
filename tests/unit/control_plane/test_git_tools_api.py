"""The runner git tools' API half (docs/runner-git-tools.md §1.2-§1.4, §2, §4; lane G5 of #1225).

Drives the real route, `POST /v1/attempts/{attempt_id}/git/{tool}`, over the
in-memory Firestore, with a worker identity the static verifier maps to the
tenant's worker service account and an attempt key registered exactly as a
worker registers one (`test_child_tasks_api`). The task's spec is signed by a
local P-256 key the settings trust, so the policy is read from a SIGNED spec
as in production. GitHub is a fake transport under the real
`forgewrite.GitHubWriter`; git is real git against `file://` bare repositories
(`GitExec(local_remotes=True)`, lane G4's seam). No network, no credentials.

Every token is built at runtime (CLAUDE.md: nothing added may look like a
credential).

What each case pins, from §7's row G5:

* an unproven request is 403, the same answer for every cause, and writes
  nothing;
* a stale generation is 409 `git_fenced` and writes a `fenced` audit row;
* a push to `main` is `protected_branch`; to another task's `swarm/` branch,
  `not_this_tasks_branch` (#1225 acceptance); from a `collect` task,
  `strategy_pushes_nothing`; under a read-only grant, `forge_write_refused`;
* `read_private` on a repository not on the allowlist and on one that does
  not exist get identical answers;
* no audit row, and no answer, contains the fake token;
* `pr_checks` returns red with the failing row's redacted log tail;
* Q1 = A: a rewrite of the task's own branch goes through only under a lease
  on the tip swarm-api recorded, is audited as a rewrite, and a branch that
  moved under someone else is refused, never forced;
* GITTOOLS-PLAN403 (#1188): a private repository on a plan without rulesets,
  whose rules read GitHub answers 403 "Upgrade to GitHub Pro or make this
  repository public...", has no rules: the push goes through and `pr_checks`
  answers. Every other 403 on that read is still `forge_refused`, and the
  default branch is still never pushed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite
from swarm_api.admins import AUDIT_COLLECTION
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge import SlotValue
from swarm_api.forgeapp import user_hash
from swarm_api.gitexec import GitExec
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from .conftest import auth_header, seed_grant, seed_tenant
from .forge_fakes import GitHubWrites
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_child_tasks_api import (  # noqa: F401  (worker_tokens is a fixture)
    ATTEMPT,
    KEY,
    OTHER_WORKER_TOKEN,
    PARENT,
    WORKER_TOKEN,
    Attempt,
    ready_parent,
    register,
    seed_parent,
    worker_tokens,
)

SIGNER = LocalSpecSigner()
OWNER, REPO = "acme", "widgets"
OWN_BRANCH = f"swarm/{PARENT}"
OTHER_TASK = "task_0123456789abcdef0123"
AUDIT = "git_tool_audit"

_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "LC_ALL": "C",
}


def make_token() -> str:
    # Runtime-built: no line of this file holds a credential-shaped literal.
    return "ghs" + "_" + secrets.token_hex(18)


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_ENV, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()


def _commit(work: Path, name: str, text: str | None = None) -> str:
    (work / name).write_text((text or name) + "\n")
    _git(work, "add", name)
    _git(work, "commit", "-q", "-m", name)
    return _git(work, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# the fakes: the tenant's slots, and GitHub with a contents route
# --------------------------------------------------------------------------


class SlotTokens:
    """Every slot of every tenant holds a token minted here on first read."""

    def __init__(self) -> None:
        self.read: list[tuple[str, str]] = []
        self.values: dict[tuple[str, str], str] = {}

    def token_for(self, tenant, *, person=None) -> str:
        return self.read_slot(tenant, "git").value

    def read_slot(self, tenant, provider: str, *, known: bool = True) -> SlotValue:
        self.read.append((tenant.tenant_id, provider))
        value = self.values.setdefault((tenant.tenant_id, provider), make_token())
        return SlotValue(value=value, version="1")

    def every_value(self) -> list[str]:
        return list(self.values.values())


class Forge(GitHubWrites):
    """`GitHubWrites`, plus `GET commits?sha=` and `GET contents/{path}?ref=`.

    `repos` is every repository that exists; a repository not in it answers
    404 on every route, as GitHub answers one the token cannot see.
    """

    def __init__(self) -> None:
        super().__init__()
        self.repos: set[str] = {f"{OWNER}/{REPO}"}
        #: (repository, ref) -> sha; (repository, sha, path) -> text.
        self.refs: dict[tuple[str, str], str] = {}
        self.files: dict[tuple[str, str, str], str] = {}
        #: (status, body) the rules read answers instead of `self.rules`.
        self.rules_answer: tuple[int, dict[str, Any]] | None = None

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        repository = "/".join(parts[1:3])
        rest = parts[3:]
        if method == "GET" and rest[:2] == ["rules", "branches"] and self.rules_answer:
            self.calls.append((method, url, dict(headers), body))
            return self.rules_answer[0], json.dumps(self.rules_answer[1]).encode()
        if method == "GET" and repository not in self.repos:
            self.calls.append((method, url, dict(headers), body))
            return 404, json.dumps({"message": "Not Found"}).encode()
        if method == "GET" and rest == ["commits"]:
            self.calls.append((method, url, dict(headers), body))
            wanted = parse_qs(parsed.query).get("sha", [""])[0]
            sha = self.refs.get((repository, wanted))
            if sha is None and any(r == repository and s == wanted for r, s, _ in self.files):
                sha = wanted  # a commit the repository has, named by its sha
            if sha is None:
                return 404, json.dumps({"message": "No commit found"}).encode()
            return 200, json.dumps([{"sha": sha}]).encode()
        if method == "GET" and rest[:1] == ["contents"]:
            self.calls.append((method, url, dict(headers), body))
            path = "/".join(rest[1:])
            ref = parse_qs(parsed.query).get("ref", [""])[0]
            text = self.files.get((repository, ref, path))
            if text is not None:
                raw = text.encode("utf-8")
                return 200, json.dumps({
                    "type": "file", "name": path.rsplit("/", 1)[-1], "path": path,
                    "size": len(raw), "encoding": "base64",
                    "content": base64.b64encode(raw).decode("ascii"),
                }).encode()
            entries = [
                {"name": p[len(path) + 1:].split("/")[0], "path": p, "size": len(t),
                 "type": "dir" if "/" in p[len(path) + 1:] else "file"}
                for (r, s, p), t in self.files.items()
                if r == repository and s == ref and p.startswith(path + "/")
            ]
            if not entries:
                return 404, json.dumps({"message": "Not Found"}).encode()
            unique = {e["name"]: e for e in entries}
            return 200, json.dumps(list(unique.values())).encode()
        return super().__call__(method, url, headers, body, timeout)


# --------------------------------------------------------------------------
# the world: an origin, a work clone, the app
# --------------------------------------------------------------------------


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, Any]:
    """A bare origin whose `main` is at B (history A-B), and a work clone of it."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", str(work))
    a = _commit(work, "a")
    b = _commit(work, "b")
    _git(work, "push", "-q", str(remote), "HEAD:refs/heads/main")
    return {"tmp": tmp_path, "remote": remote, "work": work, "a": a, "b": b}


@pytest.fixture
def forge() -> Forge:
    return Forge()


@pytest.fixture
def slots() -> SlotTokens:
    return SlotTokens()


@pytest.fixture
def app(db, worker_tokens, group_map, objects, forge, slots, origin):  # noqa: F811
    from swarm_api.gittools import GitTools, InMemoryBundles
    from swarm_api.routes import gittools as routes
    from swarm_api.routes.children import _worker_verifier

    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    ctx = build_context(
        settings=_settings(),
        db=db,
        signer=SIGNER,
        verifier=StaticTokenVerifier(worker_tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=slots,
        forge_writer=forgewrite.GitHubWriter(send=forge, locate=forge.locate, fetch=forge.fetch),
    )
    application = create_app(ctx)
    bundles = InMemoryBundles()
    remote = f"file://{origin['remote']}"

    def service() -> GitTools:
        return GitTools.from_context(
            ctx, verifier=_worker_verifier(ctx),
            gitexec=GitExec(scratch_root=origin["tmp"] / "scratch", local_remotes=True),
            remote_url=lambda ref: remote,
            bundles=bundles,
        )

    application.dependency_overrides[routes.git_tools] = service
    application.state.bundles = bundles
    return application


def _settings():
    from .conftest import api_settings

    return api_settings(
        child_key=KEY,
        spec_signing_key_version=KEY_VERSION,
        spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}),
    )


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def running_task(client, db, attempt: Attempt | None = None, *, dispatch: dict | None = None,
                 **extra: Any) -> tuple[Attempt, dict[str, Any]]:
    """A RUNNING task whose worker registered its key, its spec signed."""
    attempt = attempt or Attempt()
    doc = ready_parent(client, db, attempt, runner_profile="claude-code", **extra)
    doc["metadata"] = {"dispatch": dispatch if dispatch is not None else {"strategy": "direct-pr"}}
    SIGNER.sign_document(doc, doc["id"])
    return attempt, doc


def call(client, attempt: Attempt, tool: str, fields: dict[str, Any], *,
         token: str = WORKER_TOKEN, sign: bool = True, signer: Attempt | None = None,
         path_attempt: str | None = None, request_id: str = "req-1",
         at: float | None = None, tamper: bool = False) -> Any:
    """The worker's call, shaped exactly as `agent_worker.gittools.GitTools.call` sends it."""
    t = attempt.tuple
    body = json.dumps({
        "tenant_id": t.tenant_id, "task_id": t.task_id, "attempt_id": t.attempt_id,
        "lease_id": t.lease_id, "generation": t.generation, "request_id": request_id,
        **fields,
    }, sort_keys=True).encode("utf-8")
    path = f"/v1/attempts/{path_attempt or t.attempt_id}/git/{tool}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sign:
        headers.update((signer or attempt).sign("POST", path, body, at=at))
    if tamper:
        body = body.replace(b"req-1", b"req-2")
    return client.post(path, content=body, headers=headers)


def audit_rows(db) -> list[dict[str, Any]]:
    """The rows in the order the calls were made: ids sort newest first."""
    return [doc for path, doc in sorted(db.docs.items(), reverse=True)
            if path.startswith(f"{AUDIT}/")]


def push_fields(origin, *, branch: str | None = None, bases: tuple[str, ...] | None = None,
                head: str | None = None) -> dict[str, Any]:
    """The push body G3's `prepare_push` builds: HEAD's bundle over what origin has."""
    work = origin["work"]
    bases = (origin["b"],) if bases is None else bases
    out = origin["tmp"] / f"push-{secrets.token_hex(4)}.bundle"
    _git(work, "bundle", "create", str(out), "HEAD", *[f"^{b}" for b in bases])
    raw = out.read_bytes()
    return {
        "branch": branch, "old_sha": None, "new_sha": head or _git(work, "rev-parse", "HEAD"),
        "haves": list(bases), "commits": 1, "bundle_bytes": len(raw),
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle": base64.b64encode(raw).decode("ascii"),
    }


def remote_tip(origin, branch: str) -> str | None:
    try:
        return _git(origin["remote"], "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    except subprocess.CalledProcessError:
        return None


def seed_repository(db, owner: str, repo: str, *, readable: bool, tenant_id: str = "eng") -> str:
    repo_id = repo_id_for(tenant_id, owner, repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "forge": "github", "owner": owner,
        "repo": repo, "repository_url": f"https://github.com/{owner}/{repo}",
        "readable_by_tasks": readable, "index": {},
    }
    return repo_id


# --------------------------------------------------------------------------
# authentication: one 403 for every cause, and nothing written
# --------------------------------------------------------------------------


def test_an_unproven_request_is_the_same_403_for_every_cause(client, db):
    attempt, _ = running_task(client, db)
    stranger = Attempt()  # a key nobody registered
    fields = {"ref": "main", "haves": []}
    answers = {
        "no ID token": call(client, attempt, "git_fetch", fields, token=""),
        "a person's ID token": call(client, attempt, "git_fetch", fields, token="token-alice"),
        "another tenant's worker": call(client, attempt, "git_fetch", fields,
                                        token=OTHER_WORKER_TOKEN),
        "no proof": call(client, attempt, "git_fetch", fields, sign=False),
        "a key this attempt never registered": call(client, attempt, "git_fetch", fields,
                                                    signer=stranger),
        "a stale timestamp": call(client, attempt, "git_fetch", fields, at=1_000_000_000),
        "a body the proof did not sign": call(client, attempt, "git_fetch", fields, tamper=True),
        "another attempt in the path": call(client, attempt, "git_fetch", fields,
                                            path_attempt="att_someone_else"),
    }
    unregistered = Attempt(task="task_never_registered", attempt="att_x", lease="lease_x")
    answers["an attempt with no registration"] = call(client, unregistered, "git_fetch", fields)
    statuses = {cause: r.status_code for cause, r in answers.items()}
    assert set(statuses.values()) == {403}, statuses
    bodies = {cause: r.json() for cause, r in answers.items()}
    first = next(iter(bodies.values()))
    assert all(body == first for body in bodies.values()), bodies
    assert first["code"] == "git_tool_unproven"
    # An unproven caller writes nothing: no audit row, no tip, no quota.
    assert not [p for p in db.docs if p.startswith(("git_tool_",))]


# --------------------------------------------------------------------------
# the fence (invariant 5)
# --------------------------------------------------------------------------


def test_a_stale_generation_is_409_and_writes_a_fenced_audit_row(client, db):
    attempt, doc = running_task(client, db)
    # The reconciler reclaimed the attempt; a new generation holds the task.
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_parent_2"
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": []})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "git_fenced"
    rows = audit_rows(db)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["result"] == "fenced" and row["code"] == "git_fenced"
    assert (row["tenant_id"], row["task_id"], row["attempt_id"], row["generation"]) == (
        "eng", PARENT, ATTEMPT, 1)
    assert row["tool"] == "git_fetch"


def test_a_released_lease_is_fenced_too(client, db):
    attempt, _ = running_task(client, db)
    from datetime import datetime, timezone

    db.docs[f"leases/{attempt.tuple.lease_id}"]["released_at"] = datetime.now(timezone.utc)
    response = call(client, attempt, "pr_checks", {"pr": None})
    assert response.status_code == 409 and response.json()["code"] == "git_fenced"


# --------------------------------------------------------------------------
# the push policy, derived from the SIGNED spec (§1.2)
# --------------------------------------------------------------------------


def test_a_push_to_main_is_protected_branch(client, db, origin):
    attempt, _ = running_task(client, db)
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin, branch="main"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "protected_branch"
    assert remote_tip(origin, "main") == origin["b"]
    assert audit_rows(db)[-1]["code"] == "protected_branch"


def test_a_push_to_the_repositorys_default_branch_is_protected_whatever_its_name(
        client, db, origin, forge):
    forge.default_branch = "trunk"
    attempt, _ = running_task(client, db)
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin, branch="trunk"))
    assert response.json()["code"] == "protected_branch"


def test_a_push_to_another_tasks_swarm_branch_is_not_this_tasks_branch(client, db, origin):
    attempt, _ = running_task(client, db)
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, branch=f"swarm/{OTHER_TASK}"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "not_this_tasks_branch"
    assert remote_tip(origin, f"swarm/{OTHER_TASK}") is None


def test_an_unsigned_rewrite_of_the_dispatch_block_moves_no_branch(client, db, origin):
    """The branch set is derived from the SIGNED spec: an agent that rewrites
    `continues` in the task document to aim at another task's branch is refused
    before anything is read from the forge."""
    attempt, doc = running_task(client, db)
    doc["metadata"]["dispatch"] = {"strategy": "direct-pr", "continues": OTHER_TASK}
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch",
                    push_fields(origin, branch=f"swarm/{OTHER_TASK}"))
    assert response.status_code == 422
    assert response.json()["code"] == "spec_unverified"
    assert remote_tip(origin, f"swarm/{OTHER_TASK}") is None


def test_a_push_from_a_collect_task_is_strategy_pushes_nothing(client, db, origin, slots):
    attempt, _ = running_task(client, db, dispatch={"strategy": "collect"})
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "strategy_pushes_nothing"
    assert remote_tip(origin, OWN_BRANCH) is None
    # The promise is kept before the token is read at all.
    assert slots.read == []


def test_a_single_pr_reader_pushes_nothing_either(client, db, origin):
    attempt, _ = running_task(client, db, dispatch={
        "strategy": "single-pr", "pr_role": "reader", "pr_author": OTHER_TASK})
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.json()["code"] == "strategy_pushes_nothing"


def test_a_read_only_grant_is_forge_write_refused(client, db, origin, slots):
    suffix = f"git-u-{user_hash('alice@example.com')}"
    seed_grant(db, "eng", "alice@example.com", f"{OWNER}/{REPO}", mode="read")
    attempt, _ = running_task(client, db, forge_credential=suffix)
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "forge_write_refused"
    assert remote_tip(origin, OWN_BRANCH) is None
    # A refused grant never reads the push credential.
    assert ("eng", suffix) not in slots.read


def test_a_read_access_task_is_forge_write_refused(client, db, origin):
    attempt, _ = running_task(client, db, forge_access="read")
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.json()["code"] == "forge_write_refused"


# --------------------------------------------------------------------------
# a push that goes through, and Q1 = A's lease-guarded rewrite
# --------------------------------------------------------------------------


def test_a_push_lands_on_the_tasks_own_branch_and_is_audited(client, db, origin):
    attempt, _ = running_task(client, db)
    c = _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["branch"] == OWN_BRANCH and body["new_sha"] == c and body["old_sha"] is None
    assert body["rewrite"] is False
    assert remote_tip(origin, OWN_BRANCH) == c
    row = audit_rows(db)[-1]
    assert row["result"] == "ok" and row["tool"] == "git_push_branch"
    assert (row["branch"], row["old_sha"], row["new_sha"]) == (OWN_BRANCH, None, c)
    assert row["slot"] == "git" and row["repository"] == f"{OWNER}/{REPO}"


def test_a_rewrite_of_the_tasks_own_branch_goes_through_under_the_recorded_lease(
        client, db, origin):
    attempt, _ = running_task(client, db)
    work = origin["work"]
    c = _commit(work, "c")
    assert call(client, attempt, "git_push_branch", push_fields(origin)).status_code == 200
    # The agent rebases: C is replaced by C', which does not descend from C.
    _git(work, "reset", "-q", "--hard", origin["b"])
    c2 = _commit(work, "c", "c, rebased")
    response = call(client, attempt, "git_push_branch", push_fields(origin), request_id="req-2")
    assert response.status_code == 200, response.text
    assert response.json()["rewrite"] is True and response.json()["old_sha"] == c
    assert remote_tip(origin, OWN_BRANCH) == c2
    pushes = [r.get("rewrite") for r in audit_rows(db) if r["tool"] == "git_push_branch"]
    assert pushes == [False, True]


def test_a_branch_someone_else_moved_is_refused_not_forced(client, db, origin):
    attempt, _ = running_task(client, db)
    work = origin["work"]
    _commit(work, "c")
    assert call(client, attempt, "git_push_branch", push_fields(origin)).status_code == 200
    # A person pushes D on top of the task's branch.
    other = origin["tmp"] / "other"
    _git(origin["tmp"], "clone", "-q", "--branch", OWN_BRANCH, str(origin["remote"]), str(other))
    d = _commit(other, "d")
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{OWN_BRANCH}")
    # The agent, unaware, rewrites its own C.
    _git(work, "reset", "-q", "--hard", origin["b"])
    _commit(work, "c", "c, rebased")
    response = call(client, attempt, "git_push_branch", push_fields(origin), request_id="req-2")
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "non_fast_forward"
    assert remote_tip(origin, OWN_BRANCH) == d


def test_with_no_recorded_tip_a_non_fast_forward_is_refused(client, db, origin):
    """Never a blind force: a branch swarm-api recorded no tip for is pushed
    only as a fast-forward."""
    attempt, _ = running_task(client, db)
    other = origin["tmp"] / "other"
    _git(origin["tmp"], "clone", "-q", str(origin["remote"]), str(other))
    d = _commit(other, "d")
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{OWN_BRANCH}")
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    assert response.json()["code"] == "non_fast_forward"
    assert remote_tip(origin, OWN_BRANCH) == d


def test_a_rewritten_tip_record_is_not_a_lease(client, db, origin):
    """The recorded tip is attested under `swarm-child-key`: a tenant identity
    that rewrites it to someone else's commit gains no rewrite."""
    attempt, _ = running_task(client, db)
    work = origin["work"]
    _commit(work, "c")
    assert call(client, attempt, "git_push_branch", push_fields(origin)).status_code == 200
    other = origin["tmp"] / "other"
    _git(origin["tmp"], "clone", "-q", "--branch", OWN_BRANCH, str(origin["remote"]), str(other))
    d = _commit(other, "d")
    _git(other, "push", "-q", "origin", f"HEAD:refs/heads/{OWN_BRANCH}")
    tips = [doc for path, doc in db.docs.items() if path.startswith("git_tool_tips/")]
    assert len(tips) == 1
    tips[0]["sha"] = d
    _git(work, "reset", "-q", "--hard", origin["b"])
    _commit(work, "c", "c, rebased")
    response = call(client, attempt, "git_push_branch", push_fields(origin), request_id="req-2")
    assert response.json()["code"] == "non_fast_forward"
    assert remote_tip(origin, OWN_BRANCH) == d


# --------------------------------------------------------------------------
# fetch and rebase
# --------------------------------------------------------------------------


def test_a_fetch_returns_the_resolved_sha_and_a_bundle_of_what_the_haves_lack(
        client, db, origin):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [origin["a"]]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolved_sha"] == origin["b"]
    raw = base64.b64decode(body["bundle"])
    assert hashlib.sha256(raw).hexdigest() == body["bundle_sha256"]
    header = raw[:raw.index(b"\n\n")].decode()
    assert f"-{origin['a']}" in header and origin["b"] in header
    assert audit_rows(db)[-1]["resolved_sha"] == origin["b"]


def test_a_fetch_from_a_long_attempt_negotiates_against_its_newest_haves(client, db, origin):
    """G3 sends every sha it learnt; gitexec negotiates against 16. The
    newest are kept, so a long attempt still fetches only what it lacks."""
    attempt, _ = running_task(client, db)
    stale = [secrets.token_hex(20) for _ in range(40)]
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [*stale, origin["a"]]})
    assert response.status_code == 200, response.text
    raw = base64.b64decode(response.json()["bundle"])
    assert f"-{origin['a']}" in raw[:raw.index(b"\n\n")].decode()


def test_rebase_onto_is_served_as_the_fetch_it_starts_with(client, db, origin):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "git_rebase_onto", {"ref": "main", "haves": [origin["b"]]})
    assert response.status_code == 200, response.text
    assert response.json()["resolved_sha"] == origin["b"]
    assert response.json().get("bundle") is None
    assert audit_rows(db)[-1]["tool"] == "git_rebase_onto"


# a fetch bundle over the inline cap (#1815, GIT-FETCH-BUNDLES)
# --------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", key: str) -> None:
        self.bucket, self.key = bucket, key

    def upload_from_filename(self, filename: str, **kwargs: Any) -> None:
        from google.api_core import exceptions as gexc

        self.bucket.uploads.append((self.key, kwargs))
        if self.bucket.refuse is not None:
            raise self.bucket.refuse
        if kwargs.get("if_generation_match") == 0 and self.key in self.bucket.objects:
            raise gexc.PreconditionFailed("the object already exists")
        self.bucket.objects[self.key] = Path(filename).read_bytes()


class FakeBucket:
    """The artifact bucket as `GcsBundles` drives it: no client, no network."""

    def __init__(self, refuse: Exception | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, dict[str, Any]]] = []
        self.refuse = refuse

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(self, key)


@pytest.fixture
def bucket(app, monkeypatch) -> FakeBucket:
    """Every bundle is "large", and goes to a `GcsBundles` over a fake bucket."""
    from swarm_api import gittools
    from swarm_api.gittools import GcsBundles

    monkeypatch.setattr(gittools, "INLINE_BUNDLE_BYTES", 0)
    fake = FakeBucket()
    store = GcsBundles("swarm-artifacts-test")
    store._bucket = fake
    monkeypatch.setattr(app.state.bundles, "upload", store.upload)
    return fake


def test_a_large_fetch_bundle_is_created_under_the_proven_attempts_own_prefix_only(
        client, db, origin, bucket):
    attempt, _ = running_task(client, db)
    t = attempt.tuple
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [origin["a"]]},
                    request_id="req-big")
    assert response.status_code == 200, response.text
    key = f"tenants/{t.tenant_id}/attempts/{t.attempt_id}/git/req-big.fetch.bundle"
    # MUTATION: build the key from anything but the proven attempt (or another
    # template than FETCH_OBJECT) and the object lands outside the one prefix
    # the git-fetch-bundles-only IAM condition admits.
    assert response.json()["bundle_object"] == key
    assert [k for k, _ in bucket.uploads] == [key]
    raw = bucket.objects[key]
    assert hashlib.sha256(raw).hexdigest() == response.json()["bundle_sha256"]
    # MUTATION: drop if_generation_match=0 from GcsBundles.upload and an
    # existing object would be overwritten (objectCreator refuses it anyway).
    assert bucket.uploads[0][1].get("if_generation_match") == 0


def test_a_bundle_the_bucket_refuses_is_a_named_audited_refusal_the_worker_does_not_retry(
        client, db, origin, bucket):
    from google.api_core import exceptions as gexc

    bucket.refuse = gexc.Forbidden("storage.objects.create denied")
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [origin["a"]]})
    # MUTATION: let the Forbidden escape deliver() and this is a bare 500,
    # which agent_worker's ChildApi.call retries (status >= 500).
    assert 400 <= response.status_code < 500 and response.status_code not in (403, 409, 429), \
        response.text
    # MUTATION: rename or drop the refusal's code and the agent cannot tell it apart.
    assert response.json()["code"] == "fetch_bundle_store_failed"
    assert "denied" not in response.text
    # MUTATION: raise before handle() audits and no row records the failed call.
    row = audit_rows(db)[-1]
    assert (row["tool"], row["result"], row["code"]) == (
        "git_fetch", "refused", "fetch_bundle_store_failed")


def test_a_fetch_bundle_object_that_exists_is_never_overwritten(client, db, origin, bucket):
    attempt, _ = running_task(client, db)
    t = attempt.tuple
    key = f"tenants/{t.tenant_id}/attempts/{t.attempt_id}/git/req-1.fetch.bundle"
    bucket.objects[key] = b"earlier"
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [origin["a"]]})
    # MUTATION: upload without if_generation_match=0 and the fake bucket
    # replaces the object and answers 200.
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "fetch_bundle_store_failed"
    assert bucket.objects[key] == b"earlier"


def test_a_fetch_bundle_within_the_inline_cap_never_touches_the_bucket(
        client, db, origin, app, monkeypatch):
    from swarm_api.gittools import GcsBundles

    fake = FakeBucket()
    store = GcsBundles("swarm-artifacts-test")
    store._bucket = fake
    monkeypatch.setattr(app.state.bundles, "upload", store.upload)
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "git_fetch", {"ref": "main", "haves": [origin["a"]]})
    assert response.status_code == 200, response.text
    # MUTATION: send every bundle through the bucket and this fails.
    assert "bundle" in response.json() and "bundle_object" not in response.json()
    assert fake.uploads == []


def test_a_tool_that_is_not_one_of_the_five_is_404(client, db):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "git_push", {"ref": "main"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# read_private and its allowlist (§2.3)
# --------------------------------------------------------------------------


def test_read_private_reads_an_allowlisted_repository_at_a_sha(client, db, forge):
    attempt, _ = running_task(client, db)
    sha = "c" * 40
    forge.repos.add("acme/backend")
    forge.refs[("acme/backend", "v1.2")] = sha
    forge.files[("acme/backend", sha, "api/schema.json")] = '{"ok": true}'
    seed_repository(db, "acme", "backend", readable=True)
    response = call(client, attempt, "read_private",
                    {"repo": "acme/backend", "path": "api/schema.json", "ref": "v1.2"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "file" and body["text"] == '{"ok": true}' and body["resolved_sha"] == sha
    listing = call(client, attempt, "read_private",
                   {"repo": "acme/backend", "path": "api", "ref": sha}, request_id="req-2")
    assert listing.json()["kind"] == "dir"
    assert [e["name"] for e in listing.json()["entries"]] == ["schema.json"]
    row = audit_rows(db)[-1]
    assert row["repository"] == "acme/backend" and row["path"] == "api"
    assert "text" not in row and "entries" not in row


def test_read_private_answers_identically_off_the_list_and_for_a_missing_repository(
        client, db, forge):
    attempt, _ = running_task(client, db)
    sha = "d" * 40
    # Exists, readable by the credential, NOT on the list.
    forge.repos.add("acme/secret-plans")
    forge.files[("acme/secret-plans", sha, "README.md")] = "plans"
    seed_repository(db, "acme", "secret-plans", readable=False)
    off_list = call(client, attempt, "read_private",
                    {"repo": "acme/secret-plans", "path": "README.md", "ref": sha})
    # On the list, but no such repository on the forge.
    seed_repository(db, "acme", "ghost", readable=True)
    missing = call(client, attempt, "read_private",
                   {"repo": "acme/ghost", "path": "README.md", "ref": sha}, request_id="req-2")
    # Never registered at all.
    unknown = call(client, attempt, "read_private",
                   {"repo": "acme/nowhere", "path": "README.md", "ref": sha}, request_id="req-3")
    assert off_list.status_code == missing.status_code == unknown.status_code == 422
    assert off_list.json() == missing.json() == unknown.json()
    assert off_list.json()["code"] == "repo_not_allowlisted"
    assert "plans" not in off_list.text


def test_read_private_refuses_a_path_that_climbs(client, db, forge):
    attempt, _ = running_task(client, db)
    seed_repository(db, "acme", "backend", readable=True)
    for i, path in enumerate(("../x", "/etc/passwd", "a//b", "a\\b", "a\nb")):
        response = call(client, attempt, "read_private",
                        {"repo": "acme/backend", "path": path, "ref": "e" * 40},
                        request_id=f"req-p{i}")
        assert response.json()["code"] == "bad_path", (path, response.text)


def test_read_private_prefers_the_repositorys_own_slot(client, db, forge, slots):
    from swarm_api.gittokens import Scope, token_id_for

    attempt, _ = running_task(client, db)
    sha = "f" * 40
    forge.repos.add("acme/backend")
    forge.files[("acme/backend", sha, "a.txt")] = "a"
    repo_id = seed_repository(db, "acme", "backend", readable=True)
    token_id = token_id_for("eng", Scope.REPOSITORY, repo_id)
    db.docs[f"git_tokens/{token_id}"] = {"token_id": token_id, "tenant_id": "eng",
                                         "scope": "repository", "repo_ids": [repo_id],
                                         "revoked_at": None}
    response = call(client, attempt, "read_private",
                    {"repo": "acme/backend", "path": "a.txt", "ref": sha})
    assert response.status_code == 200, response.text
    suffix = f"git-r-{repo_id.removeprefix('repo_')}"
    assert slots.read[-1] == ("eng", suffix)
    assert audit_rows(db)[-1]["slot"] == suffix


# --------------------------------------------------------------------------
# pr_checks
# --------------------------------------------------------------------------


def test_pr_checks_returns_red_with_the_failing_rows_redacted_log_tail(client, db, forge, slots):
    attempt, _ = running_task(client, db)
    head = "a" * 40
    forge.open_pull(7, head, ref=OWN_BRANCH)
    forge.check(head, "lint", "success")
    job = forge.check(head, "unit", "failure", app_slug="github-actions")
    # The job echoed the tenant's token (a misconfigured step), then failed.
    token = slots.read_slot(type("T", (), {"tenant_id": "eng"})(), "git").value
    forge.job_logs[job] = (f"curl -H 'Authorization: token {token}'\n"
                           "FAILED tests/test_x.py::test_y - assert 1 == 2\n").encode()
    response = call(client, attempt, "pr_checks", {"pr": None})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "red" and body["pr"] == 7 and body["head_sha"] == head
    rows = {row["name"]: row for row in body["rows"]}
    assert rows["unit"]["conclusion"] == "failure"
    assert "FAILED tests/test_x.py::test_y" in rows["unit"]["log_tail"]
    assert token not in response.text
    assert "log_tail" not in rows["lint"]
    assert audit_rows(db)[-1]["pr"] == 7


def test_pr_checks_with_no_pull_request_is_none(client, db):
    attempt, _ = running_task(client, db)
    response = call(client, attempt, "pr_checks", {"pr": None})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "none" and response.json()["rows"] == []


# --------------------------------------------------------------------------
# GITTOOLS-PLAN403: a plan on which no ruleset can exist
# --------------------------------------------------------------------------

#: GitHub's answer to `rules/branches/{b}` for a private repository owned by a
#: user on the free plan, measured on bogdan-alexandrescu/swarmcloud-sandbox
#: 2026-10-22 08:4xZ with the owner's own token.
PLAN_403 = {
    "message": "Upgrade to GitHub Pro or make this repository public to enable this feature.",
    "documentation_url": "https://docs.github.com/rest/repos/rules#get-rules-for-a-branch",
    "status": "403",
}
#: A 403 that IS the credential: the control that keeps the read fail-closed.
TOKEN_403 = {"message": "Resource not accessible by personal access token", "status": "403"}


def test_a_push_to_a_repository_whose_plan_has_no_rulesets_goes_through(
        client, db, origin, forge):
    forge.rules_answer = (403, PLAN_403)
    attempt, _ = running_task(client, db)
    c = _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: drop the `except RulesUnavailableOnPlan: return []` in
    # forgewrite.GitHubWriter.branch_rules and this is 422 forge_refused.
    assert response.status_code == 200, response.text
    assert remote_tip(origin, OWN_BRANCH) == c
    # MUTATION: same change; the audit row then reads forge_refused.
    assert audit_rows(db)[-1]["result"] == "ok"


def test_pr_checks_on_a_repository_whose_plan_has_no_rulesets_returns_rows(
        client, db, forge):
    forge.rules_answer = (403, PLAN_403)
    attempt, _ = running_task(client, db)
    head = "b" * 40
    forge.open_pull(8, head, ref=OWN_BRANCH)
    forge.check(head, "lint", "success")
    response = call(client, attempt, "pr_checks", {"pr": None})
    # MUTATION: make `rules_unavailable_on_plan` answer False and this is 422 forge_refused.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pr"] == 8 and [row["name"] for row in body["rows"]] == ["lint"]


def test_any_other_403_on_the_rules_read_still_refuses_the_push_and_pr_checks(
        client, db, origin, forge):
    forge.rules_answer = (403, TOKEN_403)
    attempt, _ = running_task(client, db)
    _commit(origin["work"], "c")
    pushed = call(client, attempt, "git_push_branch", push_fields(origin))
    # MUTATION: have branch_rules return [] on every 403 (drop the message
    # test in `rules_unavailable_on_plan`) and this push goes through.
    assert pushed.json()["code"] == "forge_refused", pushed.text
    assert remote_tip(origin, OWN_BRANCH) is None
    forge.open_pull(9, "c" * 40, ref=OWN_BRANCH)
    checked = call(client, attempt, "pr_checks", {"pr": None}, request_id="req-2")
    # MUTATION: same change; pr_checks then answers 200.
    assert checked.json()["code"] == "forge_refused", checked.text


def test_the_plan_path_still_never_pushes_the_default_branch(client, db, origin, forge):
    forge.rules_answer = (403, PLAN_403)
    attempt, _ = running_task(client, db)
    _commit(origin["work"], "c")
    response = call(client, attempt, "git_push_branch", push_fields(origin, branch="main"))
    # MUTATION: move the default-branch test in gittools._push after the rules
    # read and skip it when the plan has no rules, and main moves.
    assert response.json()["code"] == "protected_branch", response.text
    assert remote_tip(origin, "main") == origin["b"]


def test_the_plan_case_is_logged_once_per_repository_at_info(forge, caplog):
    forge.rules_answer = (403, PLAN_403)
    writer = forgewrite.GitHubWriter(send=forge)
    # A repository of its own: the memo of logged repositories is per process.
    ref = forgewrite.IssueRef(owner=OWNER, repo=f"{REPO}-{secrets.token_hex(4)}", number=1)
    forge.repos.add(ref.repository)
    token = make_token()
    with caplog.at_level("INFO", logger="swarm_api.forgewrite"):
        assert writer.branch_rules(ref, "main", token) == []
        assert writer.branch_rules(ref, "swarm/x", token) == []
    named = [r for r in caplog.records if "plan" in r.getMessage()
             and ref.repository in r.getMessage()]
    # MUTATION: log on every read (drop the per-repository memo) and this is 2.
    assert len(named) == 1 and named[0].levelname == "INFO"
    assert token not in caplog.text


def _refusal_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "swarm_api.gittools" and r.getMessage().startswith("git_tool_refused ")]


def test_every_refusal_is_logged_once_with_its_tool_code_and_scrubbed_message(
        client, db, origin, forge, slots, caplog):
    """#1188: on 2026-10-23 pr_checks answered 422 forge_refused and neither
    side logged why, so the acceptance run could not say whether GitHub
    answered 401 or 403, or for which read."""
    forge.rules_answer = (403, TOKEN_403)
    attempt, _ = running_task(client, db)
    forge.open_pull(9, "c" * 40, ref=OWN_BRANCH)
    with caplog.at_level("WARNING", logger="swarm_api.gittools"):
        checked = call(client, attempt, "pr_checks", {"pr": None})
        _commit(origin["work"], "c")
        pushed = call(client, attempt, "git_push_branch", push_fields(origin, branch="main"),
                      request_id="req-2")
    assert checked.json()["code"] == "forge_refused", checked.text
    assert pushed.json()["code"] == "protected_branch", pushed.text
    lines = _refusal_lines(caplog)
    # MUTATION: drop the log line in `GitTools.handle` and this list is empty;
    # log it in each `except` and in `_admit` too and a refusal is logged twice.
    assert len(lines) == 2, lines
    checks_line, push_line = lines
    assert "tool=pr_checks" in checks_line and "code=forge_refused" in checks_line
    assert "status=422" in checks_line
    # MUTATION: log the code without the message and the cause -- which read,
    # which HTTP status -- is not in the log.
    assert "(HTTP 403)" in checks_line and "rules" in checks_line, checks_line
    assert "tool=git_push_branch" in push_line and "code=protected_branch" in push_line
    for token in slots.every_value():
        assert token not in caplog.text


# --------------------------------------------------------------------------
# the token never reaches a row or an answer; the audit read
# --------------------------------------------------------------------------


def test_no_audit_row_or_answer_contains_the_token(client, db, origin, forge, slots):
    attempt, _ = running_task(client, db)
    answers = [call(client, attempt, "git_fetch", {"ref": "main", "haves": []})]
    _commit(origin["work"], "c")
    answers.append(call(client, attempt, "git_push_branch", push_fields(origin),
                        request_id="req-2"))
    answers.append(call(client, attempt, "git_push_branch", push_fields(origin, branch="main"),
                        request_id="req-3"))
    answers.append(call(client, attempt, "git_fetch", {"ref": "no-such-branch", "haves": []},
                        request_id="req-4"))
    answers.append(call(client, attempt, "pr_checks", {"pr": None}, request_id="req-5"))
    tokens = slots.every_value()
    assert tokens, "the test read no token, so it proves nothing"
    stored = json.dumps(audit_rows(db), default=str)
    assert len(audit_rows(db)) == len(answers)
    everything = json.dumps({p: d for p, d in db.docs.items()}, default=str)
    for token in tokens:
        assert token not in stored
        assert token not in everything
        assert all(token not in answer.text for answer in answers)


def test_a_tenant_member_reads_the_tasks_git_calls_and_another_tenant_does_not(client, db):
    attempt, _ = running_task(client, db)
    assert call(client, attempt, "pr_checks", {"pr": None}).status_code == 200
    mine = client.get(f"/v1/tasks/{PARENT}/git-calls", headers=auth_header("alice"))
    assert mine.status_code == 200, mine.text
    calls = mine.json()["calls"]
    assert [c["tool"] for c in calls] == ["pr_checks"]
    assert calls[0]["result"] == "ok" and calls[0]["attempt_id"] == ATTEMPT
    theirs = client.get(f"/v1/tasks/{PARENT}/git-calls", headers=auth_header("bob"))
    assert theirs.status_code == 404


def test_the_audit_lists_an_attempts_calls_newest_first(client, db):
    attempt, _ = running_task(client, db)
    for i in range(3):
        call(client, attempt, "pr_checks", {"pr": None}, request_id=f"req-{i}")
    ids = sorted(p for p in db.docs if p.startswith(f"{AUDIT}/"))
    rows = [db.docs[p]["request_id"] for p in ids]
    assert rows == ["req-2", "req-1", "req-0"]


# --------------------------------------------------------------------------
# the per-attempt limits (Q6 = A)
# --------------------------------------------------------------------------


def test_a_looping_agent_is_rate_limited_per_attempt(client, db):
    from swarm_api import gittools

    attempt, _ = running_task(client, db)
    codes = [call(client, attempt, "pr_checks", {"pr": None}, request_id=f"r-{i}").status_code
             for i in range(gittools.CALLS_PER_HOUR + 1)]
    assert codes[:-1] == [200] * gittools.CALLS_PER_HOUR
    assert codes[-1] == 429
    assert [r["code"] for r in audit_rows(db) if r["result"] == "refused"] == ["rate_limited"]


# --------------------------------------------------------------------------
# the allowlist field on the repository record (Q3 = A)
# --------------------------------------------------------------------------


def test_readable_by_tasks_is_set_by_an_admin_and_audited(client, db):
    repo_id = seed_repository(db, "acme", "backend", readable=False)
    member = client.patch(f"/v1/repositories/{repo_id}", json={"readable_by_tasks": True},
                          headers=auth_header("alice"))
    assert member.status_code == 403, member.text
    assert db.docs[f"repositories/{repo_id}"]["readable_by_tasks"] is False
    admin = client.patch(f"/v1/repositories/{repo_id}", json={"readable_by_tasks": True},
                         headers={**auth_header("root"), "X-Swarm-Tenant": "eng"})
    assert admin.status_code == 200, admin.text
    assert admin.json()["repository"]["readable_by_tasks"] is True
    assert db.docs[f"repositories/{repo_id}"]["readable_by_tasks"] is True
    entries = [d for p, d in db.docs.items() if p.startswith(f"{AUDIT_COLLECTION}/")]
    assert [e["action"] for e in entries] == ["repository_readable_by_tasks_set"]
    assert entries[0]["by"] == "root@example.com"
    assert entries[0]["detail"]["readable_by_tasks"] == {"from": False, "to": True}


def test_a_record_that_never_set_the_field_is_not_on_the_list(client, db):
    from swarm_api.repositories import readable_by_tasks_of, to_api

    assert readable_by_tasks_of({}) is False
    assert readable_by_tasks_of({"readable_by_tasks": "yes"}) is False
    assert to_api({"repo_id": "repo_x"})["readable_by_tasks"] is False
