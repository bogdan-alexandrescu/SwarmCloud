"""A `single-pr` chain as the merge and post-verdict actions find it, for their tests.

implement (author) -> review (reader) -> post-verdict -> fix (amender) ->
proof (reader) -> merge. Every upstream task document holds what its worker
would have recorded, the verdict sits at its derived path in a directory
standing in for the bucket, proof.json is staged, and `FakeGitHub` answers a
pull request that is green in every respect. Each test then breaks exactly
one thing and asserts the refusal that names it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_worker import post_verdict
from agent_worker.objectstore import LocalObjectStore
from agent_worker.specverify import UpstreamSpecUnverified

from fake_github import FakeGitHub, app_secret_payload

TENANT = "eng"
WORKFLOW = "wf_1"
PINNED = "c" * 40
NUMBER = 12
BOT_ID = 4242
REVIEW_APP_ID = 11
MERGE_APP_ID = 21
ACTIONS_APP = 15368
TITLE = "The merge step lands the reviewed head"
IDS = {"author": "task_a", "review": "task_r", "post-verdict": "task_pv", "fix": "task_f",
       "proof": "task_p"}
ENV = {"FORGE_HOST": "api.github.com", "FORGE_OWNER": "acme", "FORGE_REPO": "widgets",
       "REVIEW_APP_ID": str(REVIEW_APP_ID), "REVIEW_APP_BOT_ID": str(BOT_ID)}
REPO = "/repos/acme/widgets"
PR = f"{REPO}/pulls/{NUMBER}"


class Chain:
    def __init__(self, tmp_path: Path) -> None:
        self.store = LocalObjectStore(tmp_path / "gcs", bucket="bucket")
        self.docs: dict[str, dict[str, Any]] = {
            IDS["author"]: {"tenant_id": TENANT, "workflow_id": WORKFLOW, "result_summary": {
                "git": {"pushed_head": PINNED, "pull_request": {"number": NUMBER}}}},
            IDS["review"]: {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                            "metadata": {"dispatch": {"strategy": "single-pr", "pr_role": "reader",
                                                      "pr_author": IDS["author"]}},
                            "result_summary": {"git": {"clone_commit": PINNED}}},
            IDS["post-verdict"]: {"tenant_id": TENANT, "workflow_id": WORKFLOW},
            IDS["fix"]: {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                         "result_summary": {"git": {"clone_commit": PINNED}}},
            IDS["proof"]: {"tenant_id": TENANT, "workflow_id": WORKFLOW,
                           "result_summary": {"git": {"clone_commit": PINNED}}},
        }
        self.review: dict[str, Any] = {"verdict": "MERGE", "sha": PINNED, "title": TITLE,
                                       "summary": "It does what it says."}
        self.proof: dict[str, Any] = {"outcome": "PROVED", "sha": PINNED, "evidence": "tests pass"}
        self.proof_path = tmp_path / "proof.json"
        self.unverified: dict[str, str] = {}
        self.cancel = False
        self.recheck_calls = 0
        self.reaped: tuple[int, ...] = ()
        self.unprotected: str | None = None
        self.human_gate = False
        self.app_id = MERGE_APP_ID
        self.environ = dict(ENV)
        self.slept: list[float] = []
        self.github = FakeGitHub()
        self.pr: dict[str, Any] = {
            "number": NUMBER, "state": "open", "merged": False, "draft": False,
            "title": TITLE, "mergeable": True,
            "head": {"ref": f"swarm/{IDS['author']}", "sha": PINNED,
                     "repo": {"full_name": "acme/widgets"}},
            "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}},
        }
        self.files: list[dict[str, Any]] = [{"filename": "apps/agent-worker/agent_worker/x.py"}]
        self.rules: list[dict[str, Any]] = [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "ci-gate", "integration_id": ACTIONS_APP}]}}]
        self.runs: list[dict[str, Any]] = [
            {"name": "ci-gate", "status": "completed", "conclusion": "success",
             "app": {"id": ACTIONS_APP}},
            {"name": "format / unit tests", "status": "completed", "conclusion": "neutral",
             "app": {"id": ACTIONS_APP}},
        ]
        self.reviews: list[dict[str, Any]] = [
            {"id": 99, "user": {"id": BOT_ID}, "state": "APPROVED", "commit_id": PINNED}]
        self.repository: dict[str, Any] = {"default_branch": "main", "permissions": {"push": True}}
        self.merge_answer: tuple[int, dict[str, str], Any] = (200, {}, {"merged": True,
                                                                       "sha": "d" * 40})
        self.base_after_merge = "main"
        self._routes()

    # -- GitHub ----------------------------------------------------------
    def _routes(self) -> None:
        gh = self.github
        gh.route("GET", REPO, lambda _s: (200, {}, self.repository))
        self.pr_reads = 0

        def pull(_seen):
            self.pr_reads += 1
            body = dict(self.pr)
            if self.github.calls("PUT", f"{PR}/merge"):
                body["base"] = {**self.pr["base"], "ref": self.base_after_merge}
            return 200, {}, body

        gh.route("GET", PR, pull)
        gh.route("GET", f"{PR}/files", lambda _s: (200, {}, self.files))
        gh.route("GET", f"{REPO}/rules/branches/main", lambda _s: (200, {}, self.rules))

        def runs(seen):
            name = seen.query.get("check_name", [None])[0]
            chosen = [r for r in self.runs if name is None or r["name"] == name]
            return 200, {}, {"check_runs": chosen}

        gh.route("GET", f"{REPO}/commits/{PINNED}/check-runs", runs)
        gh.route("GET", f"{PR}/reviews", lambda _s: (200, {}, self.reviews))
        gh.route("PUT", f"{PR}/merge", lambda _s: self.merge_answer)
        gh.route("POST", f"{REPO}/issues/{NUMBER}/comments", (201, {}, {"id": 5}))
        gh.route("POST", f"{PR}/reviews", lambda s: (200, {}, {"id": 1234, **(s.body or {})}))

    # -- the context ------------------------------------------------------
    def write_inputs(self) -> None:
        key = post_verdict.verdict_key(TENANT, WORKFLOW, IDS["review"])
        if self.review is not None:
            self.store.upload_bytes(key, json.dumps(self.review).encode())
        if self.proof is not None:
            self.proof_path.write_text(json.dumps(self.proof))

    def context(self, *, merges: dict[str, str] | None = None,
                dispatch: dict[str, Any] | None = None) -> post_verdict.ActionContext:
        from agent_worker import forge

        self.write_inputs()
        if dispatch is None:
            dispatch = {"strategy": "single-pr", "pr_role": "none",
                        "merges": dict(merges if merges is not None else IDS)}

        def fetch(task_id: str) -> dict[str, Any]:
            return self.docs[task_id]

        def verify(task_id: str, _doc: Any) -> None:
            if task_id in self.unverified:
                raise UpstreamSpecUnverified(task_id, self.unverified[task_id])

        def recheck() -> bool:
            self.recheck_calls += 1
            return self.cancel

        return post_verdict.ActionContext(
            tenant_id=TENANT, task_id="task_m", attempt_id="att_m", workflow_id=WORKFLOW,
            dispatch=dispatch, store=self.store, fetch_upstream=fetch, verify_upstream=verify,
            read_app_key=lambda: forge.parse_app_key(app_secret_payload(self.app_id)),
            environ=self.environ, recheck=recheck, reap=lambda: self.reaped,
            unprotected=self.unprotected, scrub=lambda text: text, log=None,
            staged={"proof.json": self.proof_path} if self.proof is not None else {},
            transport=self.github, sleep=self.slept.append, human_gate=self.human_gate,
        )
