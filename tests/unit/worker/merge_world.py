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

LANE MS3 (docs/merge-step.md "Revised 2026-10-06" §1) adds what a branch that
is behind needs: `PUT .../update-branch`, answered 202 by moving the head to a
merge commit GitHub made (`github_merge`), the commit and compare reads the
worker's first-parent walk makes, a closing-references list longer than a
page, and a fencing recheck that turns stale after `stale_after` calls.

THE TOKEN IS MADE AT RUNTIME (`fake_github.fresh_token`), never written as a
literal: the worker refuses to publish a diff whose added lines look like a
credential.
"""

from __future__ import annotations

import json
import re
from itertools import count
from pathlib import Path
from typing import Any

from agent_worker import post_verdict
from agent_worker.errors import FencedError
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
#: The base branch's tip a GitHub update merges into the head.
BASE_TIP = "b" * 40
#: Who GitHub's own merge commits are committed by (update-branch, the web UI).
GITHUB_COMMITTER = {"name": "GitHub", "email": "noreply@github.com"}

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
            # The merge step's own document: `metadata.merge_wait` once parked.
            TASK: {"tenant_id": TENANT, "workflow_id": WORKFLOW, "metadata": {}},
        }
        self.verdict: dict[str, Any] | None = {"verdict": "MERGE", "findings": []}
        self.verdict_path = tmp_path / VERDICT_FILE
        self.unverified: dict[str, str] = {}
        self.cancel = False
        #: Fencing turns stale after this many rechecks (None: never).
        self.stale_after: int | None = None
        self.rechecks = 0
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
            "title": TITLE, "mergeable": True, "mergeable_state": "clean",
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
        #: When set, the closing references GitHub lists, in place of the body's.
        self.closing: list[dict[str, Any]] | None = None
        self.merge_answer: Any = (200, {}, {"merged": True, "sha": MERGED})
        #: None: GitHub accepts the update and moves the head (`github_merge`).
        self.update_answer: Any = None
        #: Whether the update leaves the branch behind again (a base moving on).
        self.behind_after_update = False
        self._shas = count(1)
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
        self.serve_checks(PINNED)
        gh.route("PUT", f"{PR}/merge",
                 lambda s: self.merge_answer(s) if callable(self.merge_answer) else self.merge_answer)
        gh.route("POST", f"{API}/issues/{NUMBER}/comments", (201, {}, {"id": 5}))

        def graphql(seen):
            if self.closing is not None:
                every = list(self.closing)
            else:
                body = self.pr.get("body") or ""
                numbers = sorted({int(n) for n in _CLOSING.findall(body)})
                every = [{"number": n, "state": self.issues.get(n, "OPEN"),
                          "repository": {"nameWithOwner": f"{OWNER}/{NAME}"}} for n in numbers]
            # GitHub lists `first:` of them, and counts them all.
            page = re.search(r"closingIssuesReferences\(first: (\d+)\)", seen.body["query"])
            nodes = every[: int(page.group(1))] if page else every
            return 200, {}, {"data": {"repository": {"pullRequest": {
                "closingIssuesReferences": {"totalCount": len(every), "nodes": nodes}}}}}

        gh.route("POST", "/graphql", graphql)

        def update(seen):
            if self.update_answer is not None:
                return self.update_answer
            if seen.body.get("expected_head_sha") != self.pr["head"]["sha"]:
                return 422, {}, {"message": "expected head sha didn't match current head ref."}
            self.pr["head"]["sha"] = self.github_merge(self.pr["head"]["sha"])
            if not self.behind_after_update:
                self.pr["mergeable_state"] = "clean"
            return 202, {}, {"message": "Updating pull request branch.", "url": PR}

        gh.route("PUT", f"{PR}/update-branch", update)
        for number in range(1, 120):
            gh.route("POST", f"{API}/issues/{number}/comments", (201, {}, {"id": 100 + number}))
            gh.route("PATCH", f"{API}/issues/{number}", (200, {}, {"state": "closed"}))

    def serve_checks(self, sha: str) -> None:
        """The check runs and statuses at `sha` are this world's `runs` and `statuses`."""
        self.github.route("GET", f"{API}/commits/{sha}/check-runs",
                          lambda _s: (200, {}, {"check_runs": self.runs}))
        self.github.route("GET", f"{API}/commits/{sha}/status",
                          lambda _s: (200, {}, {"state": "success", "statuses": self.statuses}))

    def commit(self, sha: str, parents: list[str], *, committer: dict[str, str] | None = None,
               verified: bool = True) -> None:
        """Serve `GET .../commits/<sha>` with these parents."""
        self.github.route("GET", f"{API}/commits/{sha}", (200, {}, {
            "sha": sha, "parents": [{"sha": p} for p in parents],
            "commit": {"committer": dict(committer or GITHUB_COMMITTER),
                       "verification": {"verified": verified}}}))

    def on_base(self, sha: str, status: str = "behind") -> None:
        """Serve `GET .../compare/main...<sha>`: how `sha` stands against the base."""
        self.github.route("GET", f"{API}/compare/main...{sha}", (200, {}, {"status": status}))

    def github_merge(self, head: str, *, base_tip: str = BASE_TIP, **commit: Any) -> str:
        """A merge commit of the base into `head`, as update-branch makes it.

        Serves its commit read, its second parent's compare, and its checks.
        """
        sha = f"{next(self._shas):040x}"
        self.commit(sha, [head, base_tip], **commit)
        self.on_base(base_tip)
        self.serve_checks(sha)
        return sha

    def update_calls(self) -> list[Any]:
        return self.github.calls("PUT", f"{PR}/update-branch")

    def recheck(self) -> bool:
        self.rechecks += 1
        if self.stale_after is not None and self.rechecks > self.stale_after:
            raise FencedError(1, 2, "superseded while the merge read the forge")
        return self.cancel

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
            read_app_key=no_app_key, environ={}, recheck=self.recheck,
            reap=lambda: self.reaped, unprotected=self.unprotected,
            scrub=lambda text: text, log=self.log, staged=staged,
            transport=self.github, sleep=self.slept.append,
            register_secret=self.log.register_secret,
            read_git_token=read_token, repository_url=self.repository_url,
        )
