"""Fakes for the issue run's forge reads (#454): the tenant's token, and GitHub.

The secret reader is a fake; GitHub is a fake TRANSPORT under the real
`forge.GitHubIssues`, so the client's paging, caps, status mapping and host
pin are the shipped code. Every token is built at runtime.
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import parse_qs, urlparse

from swarm_api import forge


def make_token() -> str:
    return "ghp_" + secrets.token_hex(18)


class AnyTenantTokens:
    """A `-git` secret for every tenant: the tenant's id is in its token."""

    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = set(missing)
        self.asked: list[str] = []
        self.issued: dict[str, str] = {}

    def token_for(self, tenant) -> str:
        secret_id = tenant.secret_name(forge.GIT_PROVIDER)
        self.asked.append(secret_id)
        if secret_id in self.missing:
            raise forge.NoForgeCredential(
                f"tenant {tenant.tenant_id!r} has no forge credential ({secret_id})"
            )
        return self.issued.setdefault(secret_id, make_token())


def issue(number: int, title: str = "") -> dict[str, Any]:
    return {"number": number, "title": title or f"issue {number}"}


def pull(number: int, title: str = "") -> dict[str, Any]:
    return {"number": number, "title": title or f"pull {number}"}


class GitHub:
    """api.github.com's list routes over in-memory data, paged as GitHub pages.

    `issues` is `/issues` as GitHub serves it: pull requests mixed in, marked
    with a `pull_request` key. `status` maps a path suffix to an HTTP status
    every matching request answers instead.
    """

    def __init__(
        self,
        *,
        issues: list[dict[str, Any]] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        files: dict[int, list[str]] | None = None,
        status: dict[str, int] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.issues = issues or []
        self.pulls = pulls or []
        self.files = files or {}
        self.status = status or {}
        self.raises = raises
        self.calls: list[tuple[str, dict[str, str]]] = []

    def paths(self) -> list[str]:
        return [urlparse(url).path for url, _ in self.calls]

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append((url, dict(headers)))
        if self.raises is not None:
            raise self.raises
        parsed = urlparse(url)
        path = parsed.path
        for suffix, status in self.status.items():
            if path.endswith(suffix):
                return status, json.dumps({"message": "refused"}).encode()
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        per_page = int(query.get("per_page", ["30"])[0])
        if path.endswith("/issues"):
            data: list[Any] = self.issues
        elif path.endswith("/pulls"):
            data = self.pulls
        elif path.endswith("/files"):
            number = int(path.rstrip("/").split("/")[-2])
            data = [{"filename": name} for name in self.files.get(number, [])]
        else:
            return 404, b"{}"
        chunk = data[(page - 1) * per_page: page * per_page]
        return 200, json.dumps(chunk).encode()


class GitHubWrites:
    """api.github.com's comment and pull-request routes, in memory, for `GitHubWriter`.

    `comments` is every comment on every issue by id; `pulls` is the pull
    requests by number. `status` maps an HTTP method to the status every
    request with it answers instead (`{"POST": 403}`), so a refusal is the
    shipped client's mapping, not a mocked exception.
    """

    def __init__(self, *, login: str = "swarm-bot", status: dict[str, int] | None = None) -> None:
        self.login = login
        self.status = status or {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self._next = 9000

    def writes(self) -> list[tuple[str, str]]:
        return [(m, urlparse(u).path) for m, u, _, _ in self.calls if m != "GET"]

    def on_issue(self, number: int) -> list[dict[str, Any]]:
        return [c for c in self.comments.values() if c["issue"] == number]

    def add_comment(self, number: int, body: str, *, login: str = "") -> int:
        self._next += 1
        self.comments[self._next] = {
            "id": self._next, "issue": number, "body": body,
            "user": {"login": login or self.login},
            "html_url": f"https://github.com/o/r/issues/{number}#issuecomment-{self._next}",
        }
        return self._next

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if method in self.status:
            return self.status[method], json.dumps({"message": "refused"}).encode()
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")  # repos/o/r/...
        rest = parts[3:]
        payload = json.loads(body.decode()) if body else {}
        if rest[:1] == ["issues"] and len(rest) == 3 and rest[2] == "comments":
            number = int(rest[1])
            if method == "POST":
                cid = self.add_comment(number, payload["body"])
                return 201, json.dumps(self.comments[cid]).encode()
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            per_page = int(parse_qs(parsed.query).get("per_page", ["30"])[0])
            listed = sorted(self.on_issue(number), key=lambda c: c["id"])
            return 200, json.dumps(listed[(page - 1) * per_page: page * per_page]).encode()
        if rest[:2] == ["issues", "comments"] and len(rest) == 3:
            cid = int(rest[2])
            if cid not in self.comments:
                return 404, b"{}"
            if method == "DELETE":
                del self.comments[cid]
                return 204, b""
            self.comments[cid]["body"] = payload["body"]
            return 200, json.dumps(self.comments[cid]).encode()
        if rest[:1] == ["pulls"] and len(rest) == 2:
            number = int(rest[1])
            if number not in self.pulls:
                return 404, b"{}"
            if method == "PATCH":
                self.pulls[number]["body"] = payload["body"]
            return 200, json.dumps(self.pulls[number]).encode()
        return 404, b"{}"
