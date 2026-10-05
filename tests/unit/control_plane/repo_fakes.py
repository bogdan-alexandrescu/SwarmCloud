"""Fakes for repository registration (repo-index.md §1, lane RI1).

The secret reader is a fake; GitHub is a fake TRANSPORT under the real
`forge.GitHubIssues`, so the client's status mapping, paging and host pin are
the shipped code. Every token is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import api_settings


def make_token() -> str:
    return "ghp_" + secrets.token_hex(18)


class TenantTokens:
    """One `-git` secret per tenant named in `have`; any other tenant has none."""

    def __init__(self, have: tuple[str, ...] = ("eng", "research")) -> None:
        self.issued = {f"swarm-tenant-{t}-git": make_token() for t in have}
        self.asked: list[str] = []

    def token_for(self, tenant) -> str:
        secret_id = tenant.secret_name(forge.GIT_PROVIDER)
        self.asked.append(secret_id)
        if secret_id not in self.issued:
            raise forge.NoForgeCredential(
                f"tenant {tenant.tenant_id!r} has no forge credential ({secret_id})"
            )
        return self.issued[secret_id]


def repo_entry(
    full_name: str, *, private: bool = True, default_branch: str = "main",
    pull: bool = True, push: bool = True, admin: bool = False, archived: bool = False,
) -> dict[str, Any]:
    owner, name = full_name.split("/")
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "private": private,
        "visibility": "private" if private else "public",
        "default_branch": default_branch,
        "archived": archived,
        "permissions": {"pull": pull, "push": push, "admin": admin},
    }


class GitHubRepos:
    """`GET /repos/{owner}/{repo}` and `GET /user/repos`, in memory.

    `repos` maps a lower-cased `owner/repo` to what GitHub answers for it, per
    token: a repository absent from it is a 404, as GitHub answers for a private
    repository the token cannot see. `listing` is `/user/repos` in order.
    `status` maps a path suffix to a status every matching request answers.
    """

    def __init__(
        self,
        *,
        repos: dict[str, dict[str, Any]] | None = None,
        listing: list[dict[str, Any]] | None = None,
        status: dict[str, int] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.repos = {k.lower(): v for k, v in (repos or {}).items()}
        self.listing = listing or []
        self.status = status or {}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append((url, dict(headers)))
        if self.raises is not None:
            raise self.raises
        parsed = urlparse(url)
        path = parsed.path
        for suffix, status in self.status.items():
            if path.endswith(suffix):
                return status, json.dumps({"message": "refused"}).encode()
        if path == "/user/repos":
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            chunk = self.listing[(page - 1) * per_page: page * per_page]
            return 200, json.dumps(chunk).encode()
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "repos":
            found = self.repos.get(f"{parts[1]}/{parts[2]}".lower())
            if found is None:
                return 404, b'{"message": "Not Found"}'
            return 200, json.dumps(found).encode()
        return 404, b"{}"


def make_client(db, tokens, group_map, objects, *, forge_tokens, transport) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_tokens,
        forge=forge.GitHubIssues(send=transport),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)
