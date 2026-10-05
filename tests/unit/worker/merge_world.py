"""A workflow's pull request as the `merge` worker action finds it, for its tests.

Contract request 47 (owner decisions 2026-10-04): the merge acts on the
workflow's OWN repository with the tenant's `-git` token. The repository here
is deliberately not SwarmCloud's -- `octo-org/widget-shop` -- so a merge that
quietly reached for a fixed repository would talk to a route this fake does
not serve.

The integrator task recorded its pushed head and its pull request; the review
wrote MERGE; the base branch is protected and requires one check, green at
that head; the pull request closes one open and one already-closed issue.
`MergeWorld.context()` is green in every respect, and each test breaks
exactly one thing.

THE TOKEN IS MADE AT RUNTIME (`fake_github.fresh_token`), never written as a
literal: the worker refuses to publish a diff whose added lines look like a
credential.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_worker import post_verdict
from agent_worker.objectstore import LocalObjectStore
from agent_worker.specverify import UpstreamSpecUnverified

from fake_github import FakeGitHub, fresh_token

TENANT = "eng"
WORKFLOW = "wf_1"
TASK = "task_merge"
OPENER = "task_int"
REVIEW = "task_rev"
OWNER = "octo-org"
NAME = "widget-shop"
REPO_URL = f"https://github.com/{OWNER}/{NAME}.git"
API = f"/repos/{OWNER}/{NAME}"
NUMBER = 41
PR = f"{API}/pulls/{NUMBER}"
PINNED = "c" * 40
OTHER = "e" * 40
MERGED = "d" * 40
TITLE = "The widget shop lists widgets by price"
CHECK = "ci / unit"
ACTIONS_APP = 15368
VERDICT_FILE = "verdict.json"

#: GitHub's closing keywords, as its own `closingIssuesReferences` reads a body.
_CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.IGNORECASE)


class LogCapture:
    """Every line the action logs, and every value registered as a secret."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.secrets: list[str] = []

    def _record(self, level: str, message: str, **fields: Any) -> None:
        self.lines.append(f"{level} {message} {json.dumps(fields, default=str)}")

    def info(self, message: str, **fields: Any) -> None:
        self._record("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._record("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._record("error", message, **fields)

    def register_secret(self, value: str) -> None:
        self.secrets.append(value)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class MergeWorld:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.store = LocalObjectStore(tmp_path / "gcs", bucket="bucket")
        self.token = fresh_token()
        self.token_reads = 0
        self.log = LogCapture()
        self.docs: dict[str, dict[str, Any]] = {
            OPENER: {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                     "metadata": {"dispatch": {"strategy": "integrate", "role": "integrator"}},
                     "result_summary": {"git": {"pushed_head": PINNED,
                                                "pull_request": {"number": NUMBER}}}},
            REVIEW: {"tenant_id": TENANT, "workflow_id": WORKFLOW},
        }
        self.verdict: dict[str, Any] | None = {"verdict": "MERGE", "findings": []}
        self.verdict_path = tmp_path / VERDICT_FILE
        self.unverified: dict[str, str] = {}
        self.cancel = False
        self.reaped: tuple[int, ...] = ()
        self.unprotected: str | None = None
        self.repository_url: str | None = REPO_URL
        self.target: dict[str, Any] = {"pull_request": OPENER, "review": REVIEW,
                                       "verdict_file": VERDICT_FILE}
        self.slept: list[float] = []
        self.github = FakeGitHub()
        self.repository: dict[str, Any] = {"default_branch": "main",
                                           "permissions": {"push": True}}
        self.pr: dict[str, Any] = {
            "number": NUMBER, "state": "open", "merged": False, "draft": False,
            "title": TITLE, "mergeable": True,
            "body": "Lists widgets by price.\n\nCloses #7\nFixes #9",
            "head": {"ref": f"swarm/{OPENER}", "sha": PINNED,
                     "repo": {"full_name": f"{OWNER}/{NAME}"}},
            "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{NAME}"}},
        }
        self.rules: list[dict[str, Any]] = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": CHECK, "integration_id": ACTIONS_APP}]}}]
        self.branch: dict[str, Any] = {"name": "main", "protected": True}
        self.runs: list[dict[str, Any]] = [
            {"name": CHECK, "status": "completed", "conclusion": "success",
             "app": {"id": ACTIONS_APP}},
        ]
        self.statuses: list[dict[str, Any]] = []
        self.issues: dict[int, str] = {7: "OPEN", 9: "CLOSED"}
        self.merge_answer: Any = (200, {}, {"merged": True, "sha": MERGED})
        self._routes()

    # -- GitHub ----------------------------------------------------------
    def _routes(self) -> None:
        gh = self.github
        gh.route("GET", API, lambda _s: (200, {}, self.repository))

        def pull(_seen):
            body = dict(self.pr)
            if gh.calls("PUT", f"{PR}/merge") and self.merge_answer[0] == 200:
                body.update(merged=True, state="closed", merge_commit_sha=MERGED)
            return 200, {}, body

        gh.route("GET", PR, pull)
        gh.route("GET", f"{API}/rules/branches/main", lambda _s: (200, {}, self.rules))
        gh.route("GET", f"{API}/branches/main", lambda _s: (200, {}, self.branch))
        gh.route("GET", f"{API}/commits/{PINNED}/check-runs",
                 lambda _s: (200, {}, {"check_runs": self.runs}))
        gh.route("GET", f"{API}/commits/{PINNED}/status",
                 lambda _s: (200, {}, {"state": "success", "statuses": self.statuses}))
        gh.route("PUT", f"{PR}/merge",
                 lambda s: self.merge_answer(s) if callable(self.merge_answer) else self.merge_answer)
        gh.route("POST", f"{API}/issues/{NUMBER}/comments", (201, {}, {"id": 5}))

        def graphql(seen):
            body = self.pr.get("body") or ""
            numbers = sorted({int(n) for n in _CLOSING.findall(body)})
            nodes = [{"number": n, "state": self.issues.get(n, "OPEN"),
                      "repository": {"nameWithOwner": f"{OWNER}/{NAME}"}} for n in numbers]
            return 200, {}, {"data": {"repository": {"pullRequest": {
                "closingIssuesReferences": {"nodes": nodes}}}}}

        gh.route("POST", "/graphql", graphql)
        for number in range(1, 20):
            gh.route("POST", f"{API}/issues/{number}/comments", (201, {}, {"id": 100 + number}))
            gh.route("PATCH", f"{API}/issues/{number}", (200, {}, {"state": "closed"}))

    def merge_calls(self) -> list[Any]:
        return self.github.calls("PUT", f"{PR}/merge")

    def closed_issues(self) -> list[int]:
        return [int(s.path.rsplit("/", 1)[1]) for s in self.github.seen
                if s.method == "PATCH" and s.path.startswith(f"{API}/issues/")]

    # -- the context ------------------------------------------------------
    def context(self) -> post_verdict.ActionContext:
        staged: dict[str, Path] = {}
        if self.verdict is not None:
            self.verdict_path.write_text(json.dumps(self.verdict))
            staged[VERDICT_FILE] = self.verdict_path

        def fetch(task_id: str) -> dict[str, Any]:
            return self.docs[task_id]

        def verify(task_id: str, _doc: Any) -> None:
            if task_id in self.unverified:
                raise UpstreamSpecUnverified(task_id, self.unverified[task_id])

        def read_token() -> str:
            self.token_reads += 1
            self.log.register_secret(self.token)
            return self.token

        def no_app_key():
            raise AssertionError("the merge read an App key; it reads the tenant's -git token")

        return post_verdict.ActionContext(
            tenant_id=TENANT, task_id=TASK, attempt_id="att_m", workflow_id=WORKFLOW,
            dispatch={"strategy": "integrate", "carrier": "checkpoints",
                      "merge_target": dict(self.target)},
            store=self.store, fetch_upstream=fetch, verify_upstream=verify,
            read_app_key=no_app_key, environ={}, recheck=lambda: self.cancel,
            reap=lambda: self.reaped, unprotected=self.unprotected,
            scrub=lambda text: text, log=self.log, staged=staged,
            transport=self.github, sleep=self.slept.append,
            register_secret=self.log.register_secret,
            read_git_token=read_token, repository_url=self.repository_url,
        )
