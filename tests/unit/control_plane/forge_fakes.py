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
        files: dict[int, list[Any]] | None = None,
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
            # An entry is a name, or `(name, patch)` for a file the forge sends a patch for.
            data = [
                {"filename": e} if isinstance(e, str) else {"filename": e[0], "patch": e[1]}
                for e in self.files.get(number, [])
            ]
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
        #: `(method, path)` -> the status that one route answers instead, so a
        #: test can refuse one write (closing an issue) and not the others.
        self.refuse: dict[tuple[str, str], int] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.pulls: dict[int, dict[str, Any]] = {}
        #: Each issue an `already_on_main` run closed (#646), by number: the
        #: fields the PATCH set (`state`, `state_reason`).
        self.issues: dict[int, dict[str, Any]] = {}
        # CI (#454's loop): the default branch's rules, the check runs and
        # commit statuses at each sha, each check run's annotations, and the
        # Actions job logs `locate` redirects to and `fetch` serves.
        self.rules: list[dict[str, Any]] = []
        self.check_runs: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.annotations: dict[int, list[dict[str, Any]]] = {}
        self.job_logs: dict[int, bytes] = {}
        self.located: list[tuple[str, dict[str, str]]] = []
        self.fetched: list[tuple[str, dict[str, str]]] = []
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self._next = 9000
        #: The repository itself and its branches by name -> sha (#748): what
        #: a pull request opened from an already-pushed branch reads and makes.
        self.default_branch = "main"
        self.branches: dict[str, str] = {}

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
        if (method, parsed.path) in self.refuse:
            return self.refuse[(method, parsed.path)], json.dumps({"message": "refused"}).encode()
        parts = parsed.path.strip("/").split("/")  # repos/o/r/...
        rest = parts[3:]
        payload = json.loads(body.decode()) if body else {}
        if rest == ["issues"]:
            # Opening an issue (the observer's epic) and listing them, newest first.
            repository = "/".join(parts[1:3])
            if method == "POST":
                self._next += 1
                self.issues[self._next] = {
                    "number": self._next, "state": "open", "repository": repository,
                    "title": payload["title"], "body": payload["body"],
                    "labels": [{"name": name} for name in payload.get("labels", [])],
                    "html_url": f"https://github.com/{repository}/issues/{self._next}",
                }
                return 201, json.dumps(self.issues[self._next]).encode()
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            listed = sorted((i for i in self.issues.values() if i.get("repository") == repository),
                            key=lambda i: -i["number"])
            return 200, json.dumps(listed[(page - 1) * per_page: page * per_page]).encode()
        if rest[:1] == ["issues"] and len(rest) == 3 and rest[2] == "comments":
            number = int(rest[1])
            if method == "POST":
                cid = self.add_comment(number, payload["body"])
                return 201, json.dumps(self.comments[cid]).encode()
            page = int(parse_qs(parsed.query).get("page", ["1"])[0])
            per_page = int(parse_qs(parsed.query).get("per_page", ["30"])[0])
            listed = sorted(self.on_issue(number), key=lambda c: c["id"])
            return 200, json.dumps(listed[(page - 1) * per_page: page * per_page]).encode()
        if rest[:1] == ["issues"] and len(rest) == 2 and rest[1].isdigit():
            number = int(rest[1])
            if method == "PATCH":
                self.issues.setdefault(number, {"number": number, "state": "open"}).update(payload)
            return 200, json.dumps(self.issues.get(number, {"number": number, "state": "open"})).encode()
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
                for key in ("body", "title"):
                    if key in payload:
                        self.pulls[number][key] = payload[key]
            return 200, json.dumps(self.pulls[number]).encode()
        if not rest and method == "GET":
            return 200, json.dumps({"full_name": "/".join(parts[1:3]),
                                    "default_branch": self.default_branch}).encode()
        if rest[:3] == ["git", "ref", "heads"] and method == "GET":
            name = "/".join(rest[3:])
            if name not in self.branches:
                return 404, b"{}"
            return 200, json.dumps({"ref": f"refs/heads/{name}",
                                    "object": {"sha": self.branches[name]}}).encode()
        if rest == ["git", "refs"] and method == "POST":
            name = payload["ref"].removeprefix("refs/heads/")
            if name in self.branches:
                return 422, json.dumps({"message": "Reference already exists"}).encode()
            self.branches[name] = payload["sha"]
            return 201, json.dumps({"ref": payload["ref"], "object": {"sha": payload["sha"]}}).encode()
        if rest == ["pulls"]:
            if method == "POST":
                if any(p["head"]["ref"] == payload["head"] and p["state"] == "open"
                       for p in self.pulls.values()):
                    return 422, json.dumps({"message": "A pull request already exists"}).encode()
                self._next += 1
                self.open_pull(self._next, self.branches.get(payload["head"], ""),
                               ref=payload["head"], body=payload["body"])
                self.pulls[self._next].update(title=payload["title"],
                                              base={"ref": payload["base"]})
                return 201, json.dumps(self.pulls[self._next]).encode()
            head = parse_qs(parsed.query).get("head", [""])[0].partition(":")[2]
            listed = [p for p in self.pulls.values()
                      if p["head"]["ref"] == head and p["state"] == "open"]
            return 200, json.dumps(listed[:1]).encode()
        query = parse_qs(parsed.query)
        page = int(query.get("page", ["1"])[0])
        per_page = int(query.get("per_page", ["30"])[0])
        if rest[:2] == ["rules", "branches"]:
            return 200, json.dumps(self.rules).encode()
        if rest[:1] == ["commits"] and len(rest) == 3 and rest[2] == "check-runs":
            runs = self.check_runs.get(rest[1], [])
            chunk = runs[(page - 1) * per_page: page * per_page]
            return 200, json.dumps({"total_count": len(runs), "check_runs": chunk}).encode()
        if rest[:1] == ["commits"] and len(rest) == 3 and rest[2] == "status":
            return 200, json.dumps({"statuses": self.statuses.get(rest[1], [])}).encode()
        if rest[:1] == ["check-runs"] and len(rest) == 3 and rest[2] == "annotations":
            return 200, json.dumps(self.annotations.get(int(rest[1]), [])[:per_page]).encode()
        return 404, b"{}"

    # -- the Actions job log: a redirect from api.github.com, then a token-less read

    LOG_HOST = "https://pipelines.actions.githubusercontent.com/logs/"

    def locate(self, url, headers, timeout):
        self.located.append((url, dict(headers)))
        parts = urlparse(url).path.strip("/").split("/")  # repos/o/r/actions/jobs/<id>/logs
        job_id = int(parts[-2])
        if job_id not in self.job_logs:
            return 404, ""
        return 302, f"{self.LOG_HOST}{job_id}?sig=signed"

    def fetch(self, url, headers, timeout):
        self.fetched.append((url, dict(headers)))
        job_id = int(urlparse(url).path.rstrip("/").split("/")[-1])
        return 200, self.job_logs[job_id]

    # -- helpers for a test's CI

    def open_pull(self, number: int, sha: str, *, ref: str = "swarm/x", body: str = "") -> None:
        self.pulls[number] = {
            "number": number, "html_url": f"https://github.com/saga-xyz/widgets/pull/{number}",
            "head": {"sha": sha, "ref": ref}, "base": {"ref": "main"}, "body": body,
            "state": "open", "merged": False,
        }

    def require(self, *contexts: str, app_id: int | None = 15368) -> None:
        """The default branch requires `contexts`, pinned to `app_id` (None: unpinned)."""
        self.rules = [{
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [
                {"context": c, **({"integration_id": app_id} if app_id is not None else {})}
                for c in contexts
            ]},
        }]

    def check(
        self, sha: str, name: str, conclusion: str | None, *, status: str = "completed",
        check_id: int | None = None, output: dict[str, Any] | None = None,
        app_id: int = 15368, app_slug: str = "github-actions",
    ) -> int:
        self._next += 1
        run_id = check_id or self._next
        self.check_runs.setdefault(sha, []).append({
            "id": run_id, "name": name, "head_sha": sha, "status": status,
            "conclusion": conclusion, "app": {"id": app_id, "slug": app_slug},
            "output": output or {"title": None, "summary": None, "text": None},
        })
        return run_id
