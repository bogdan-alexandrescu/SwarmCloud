"""One SwarmCloud, served by a fake client, for the build board's tests (BOARD-CORE, #1773).

The same fixture renders `artifacts/board-sample.html` for the owner's
operator: many lanes, three feature groups (#101, #202 and "No issue"), one
item of each kind "Waiting on someone" lists, a long title and a Unicode one
-- with markup in it, so the sample shows it escaped.

Every shape below is the one the API serves: a `GET /v1/workflows` row, a
`GET /v1/workflows/{id}` envelope (`workflow` + `tasks`), a task as
`GET /v1/tasks` serves it, an issue run as `GET /v1/runs?view=summary`
serves it. Nothing here is a credential: the one secret-shaped value is built
at runtime (`SECRET_IN_INPUT`), so a test can prove it never reaches the page.
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_mcp.client import SwarmError

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
ME = "dev@example.com"
ORIGIN = "https://github.com/acme/app.git"
REPO = "https://github.com/acme/app"
OTHER_REPO = "https://github.com/acme/other"
CONSOLE = "https://console.example.test"

#: A prompt that must never reach the page: step input is not a field the
#: board renders. Built at runtime, never one literal.
SECRET_IN_INPUT = "sk-" + secrets.token_hex(20)

LONG_TITLE = (
    "Rework the merge gate so that a pull request whose checks were re-run after a "
    "force-push is judged on the newest head only, and say so in the step's refusal "
    "instead of reporting the stale result from the previous head"
)
UNICODE_TITLE = "Überprüfung der Größen — 日本語のテスト ✓ <script>alert(1)</script> & co"


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def ago(**kw: float) -> str:
    return iso(NOW - timedelta(**kw))


def _task(tid: str, state: str, *, wf: str | None, step: str | None, profile: str = "claude-code",
          created: str, started: str | None = None, completed: str | None = None,
          title: str | None = None, unit: str | None = None, issue: Any = None,
          pr: int | None = None, questions: int = 0, park: str | None = None,
          refusal: str | None = None, repo: str = REPO, by: str = ME,
          metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = dict(metadata or {})
    if title:
        meta["title"] = title
    if unit:
        meta["unit"] = unit
    summary: dict[str, Any] = {}
    if pr is not None and profile != "merge":
        summary["git"] = {"pull_request": {"number": pr, "url": f"{REPO}/pull/{pr}"}}
    if pr is not None and profile == "merge":
        summary["pull_request"] = pr
    if questions:
        summary["questions"] = questions
    if refusal:
        summary["refusal"] = {"code": refusal, "message": f"{refusal}: the merge was refused"}
    task_input: dict[str, Any] = {"prompt": f"do the work {SECRET_IN_INPUT}"}
    if issue is not None:
        task_input["issue"] = issue
    return {
        "id": tid, "task_id": tid, "tenant_id": "t-acme", "state": state, "runner_profile": profile,
        "workflow_id": wf, "step_id": step, "created_at": created, "updated_at": created,
        "started_at": started, "completed_at": completed, "park_reason": park,
        "metadata": meta, "input": task_input, "repository_url": repo,
        "result_summary": summary or None, "submitted_by": by,
        "links": {"console": f"{CONSOLE}/agents/{tid}"},
    }


def _workflow(wid: str, state: str, created: str, tasks: list[dict[str, Any]], *,
              by: str = ME, deps: dict[str, list[str]] | None = None) -> dict[str, Any]:
    steps = [{
        "step_id": t["step_id"], "runner_profile": t["runner_profile"], "task_id": t["id"],
        "depends_on": (deps or {}).get(t["step_id"], []),
        "links": {"console": f"{CONSOLE}/agents/{t['id']}"},
    } for t in tasks]
    return {
        "workflow_id": wid, "tenant_id": "t-acme", "state": state, "stored_state": state,
        "state_source": "derived", "created_at": created, "updated_at": created,
        "submitted_by": by, "steps": steps,
        "links": {"console": f"{CONSOLE}/workflows/{wid}"},
    }


def build() -> dict[str, Any]:
    """`{workflows: [(row, tasks)], single_tasks: [...], runs: [...], events: {...}}`."""
    wfs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

    def add(wid: str, state: str, created: str, tasks: list[dict[str, Any]], **kw: Any) -> None:
        wfs.append((_workflow(wid, state, created, tasks, **kw), tasks))

    # #101: an issue run, merged, whose implement step asked two questions.
    add("wf_a1", "SUCCEEDED", ago(days=3), [
        _task("t_a1_impl", "SUCCEEDED", wf="wf_a1", step="implement", created=ago(days=3),
              started=ago(days=3), completed=ago(days=2, hours=20), issue=101, pr=501, questions=2,
              title="acme/app#101: Speed up the login page"),
        _task("t_a1_merge", "SUCCEEDED", wf="wf_a1", step="merge", profile="merge", created=ago(days=3),
              started=ago(days=2, hours=19), completed=ago(days=2, hours=18), issue=101, pr=501,
              title="acme/app#101: Speed up the login page"),
    ], deps={"merge": ["implement"]})
    # #101 again, named by its title only, still running.
    add("wf_a2", "RUNNING", ago(hours=2), [
        _task("t_a2_impl", "RUNNING", wf="wf_a2", step="implement", created=ago(hours=2),
              started=ago(hours=1), title="Cache the session lookup (#101)", unit="LOGIN-CACHE"),
    ])
    # #202: merge parked on CI for two hours -- In CI, and listed.
    add("wf_b1", "RUNNING", ago(hours=5), [
        _task("t_b1_impl", "SUCCEEDED", wf="wf_b1", step="implement", created=ago(hours=5),
              started=ago(hours=5), completed=ago(hours=3), issue=202, pr=612, title=LONG_TITLE),
        _task("t_b1_merge", "PARKED", wf="wf_b1", step="merge", profile="merge", created=ago(hours=5),
              park="CI_PENDING", issue=202, pr=612, title=LONG_TITLE),
    ], deps={"merge": ["implement"]})
    # #202: merge refused on red checks.
    add("wf_b2", "FAILED", ago(days=1), [
        _task("t_b2_impl", "SUCCEEDED", wf="wf_b2", step="implement", created=ago(days=1),
              started=ago(days=1), completed=ago(hours=22), issue=202, pr=613, title="Gate the merge (#202)"),
        _task("t_b2_merge", "FAILED", wf="wf_b2", step="merge", profile="merge", created=ago(days=1),
              started=ago(hours=21), completed=ago(hours=20, minutes=30), issue=202, pr=613,
              refusal="checks_failed", title="Gate the merge (#202)"),
    ], deps={"merge": ["implement"]})
    # #202: merge parked on CI for half an hour -- In CI, NOT listed (under 90 min).
    add("wf_b3", "RUNNING", ago(hours=1), [
        _task("t_b3_merge", "PARKED", wf="wf_b3", step="merge", profile="merge", created=ago(hours=1),
              park="CI_PENDING", issue=202, pr=614, title="Gate the merge, part two (#202)"),
    ])
    # No issue: a step waiting for a person's answer, with a Unicode title.
    add("wf_c1", "RUNNING", ago(hours=4), [
        _task("t_c1_plan", "SUCCEEDED", wf="wf_c1", step="plan", created=ago(hours=4),
              started=ago(hours=4), completed=ago(hours=3, minutes=30), title=UNICODE_TITLE),
        _task("t_c1_review", "PARKED", wf="wf_c1", step="review", created=ago(hours=4),
              park="AWAITING_INPUT", title=UNICODE_TITLE,
              metadata={"await_input": {"opened_at": ago(hours=3), "expires_at": ago(hours=-21)}}),
        _task("t_c1_ship", "QUEUED", wf="wf_c1", step="ship", created=ago(hours=4), title=UNICODE_TITLE),
    ], deps={"review": ["plan"], "ship": ["review"]})
    # No issue: a park only a person promotes.
    add("wf_c2", "RUNNING", ago(hours=6), [
        _task("t_c2_impl", "PARKED", wf="wf_c2", step="implement", created=ago(hours=6),
              park="HUMAN_REQUIRED", title="Rotate the staging fixtures"),
    ])
    # Queued: nothing has started.
    add("wf_q", "QUEUED", ago(minutes=10), [
        _task("t_q_impl", "QUEUED", wf="wf_q", step="implement", created=ago(minutes=10),
              title="Tidy the docs index (#101)"),
    ])
    # Failed outside a merge: a failed step is NOT listed as waiting.
    add("wf_f", "FAILED", ago(days=2), [
        _task("t_f_impl", "FAILED", wf="wf_f", step="implement", created=ago(days=2),
              started=ago(days=2), completed=ago(days=1, hours=23), title="Bump the base image"),
    ])
    add("wf_x", "CANCELLED", ago(days=4), [
        _task("t_x_impl", "CANCELLED", wf="wf_x", step="implement", created=ago(days=4),
              completed=ago(days=4), title="Try the other cache"),
    ])
    # Not the caller's: left out.
    add("wf_theirs", "RUNNING", ago(hours=1), [
        _task("t_theirs", "RUNNING", wf="wf_theirs", step="implement", created=ago(hours=1),
              title="A teammate's lane", by="mate@example.com"),
    ], by="mate@example.com")
    # Finished 40 days ago: past the window, left out.
    add("wf_old", "SUCCEEDED", ago(days=41), [
        _task("t_old", "SUCCEEDED", wf="wf_old", step="implement", created=ago(days=41),
              started=ago(days=41), completed=ago(days=40), title="Ancient work"),
    ])

    singles = [
        _task("t_s1", "SUCCEEDED", wf=None, step=None, created=ago(hours=30), started=ago(hours=30),
              completed=ago(hours=29), title="Fix the typo on the login page (#101)", pr=777, questions=1),
        _task("t_s2", "RUNNING", wf=None, step=None, created=ago(minutes=40), started=ago(minutes=35),
              unit="flaky-hunt"),
        # Another repository: left out.
        _task("t_s3", "SUCCEEDED", wf=None, step=None, created=ago(hours=3), started=ago(hours=3),
              completed=ago(hours=2), title="Elsewhere", repo=OTHER_REPO),
        # A workflow's step, served by the task listing too: never a lane of its own.
        dict(next(t for _, ts in wfs for t in ts if t["id"] == "t_a2_impl")),
        # Finished 35 days ago: left out.
        _task("t_s4", "SUCCEEDED", wf=None, step=None, created=ago(days=35), started=ago(days=35),
              completed=ago(days=35), title="Old single"),
    ]
    runs = [{
        "id": "run_202", "state": "IMPLEMENTING",
        "issue": {"ref": "acme/app#202", "owner": "acme", "repo": "app", "number": 202,
                  "url": f"{REPO}/issues/202", "repository_url": REPO},
        "issue_read": {"title": "Ship the merge gate", "state": "open"},
    }, {
        # Another repository's #101: must not title this project's #101.
        "id": "run_x", "state": "DONE",
        "issue": {"ref": "acme/other#101", "owner": "acme", "repo": "other", "number": 101,
                  "url": f"{OTHER_REPO}/issues/101", "repository_url": OTHER_REPO},
        "issue_read": {"title": "Not this project's issue"},
    }]
    questions = {
        "t_a1_impl": [
            {"question": "Keep the old login cache for one release?",
             "options": [{"label": "Keep it", "description": "one more release"},
                         {"label": "Drop it", "description": "now"}],
             "recommended": "Drop it", "context": "the new cache covers every caller"},
            {"question": "Which metric gates the rollout?",
             "options": [{"label": "p95", "description": ""}, {"label": "p99", "description": ""}],
             "recommended": "p95", "context": ""},
        ],
        "t_s1": [
            {"question": "Also fix the same typo in the docs?",
             "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}],
             "recommended": "Yes", "context": ""},
        ],
    }
    events = {
        "t_b1_merge": [{"type": "parked", "at": ago(hours=2)}, {"type": "running", "at": ago(hours=2, minutes=5)}],
        "t_b3_merge": [{"type": "parked", "at": ago(minutes=30)}],
        "t_c2_impl": [{"type": "parked", "at": ago(hours=5)}],
    }
    return {"workflows": wfs, "single_tasks": singles, "runs": runs, "questions": questions,
            "events": events}


class FakeClient:
    """The API the board reads, and a record of every read it made."""

    tenant = None

    def __init__(self, data: dict[str, Any] | None = None, *, page: int = 4,
                 echo_repository: bool = True, fail: str | None = None) -> None:
        self.data = data or build()
        self.page = page
        self.echo_repository = echo_repository
        self.fail = fail
        self.reads: list[str] = []

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    # -- the listing calls ------------------------------------------------
    def workflows(self, *, states=(), active=False, limit=None, page_token=None,
                  claude_session=None, repository=None) -> dict[str, Any]:
        self.reads.append(f"workflows active={active} repository={repository} page={page_token}")
        if self.fail == "workflows":
            raise SwarmError("GET /v1/workflows answered HTTP 503")
        rows = [row for row, tasks in self.data["workflows"]]
        if repository and self.echo_repository:
            rows = [row for row, tasks in self.data["workflows"]
                    if any(t["repository_url"] == REPO for t in tasks)]
        if active:
            rows = [r for r in rows if r["state"] not in ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")]
        rows = sorted(rows, key=lambda r: (r["created_at"], r["workflow_id"]), reverse=True)
        start = int(page_token or 0)
        chunk = rows[start:start + self.page]
        following = str(start + self.page) if start + self.page < len(rows) else None
        out: dict[str, Any] = {"workflows": [dict(r) for r in chunk], "next_page_token": following,
                               "tenant_id": "t-acme", "filter": {"active": active}}
        if repository and self.echo_repository:
            out["filter"]["repository"] = "github.com/acme/app"
            out["filter"]["left_out"] = {}
        return out

    def artifact_content(self, task_id: str, name: str, *, offset: int = 0,
                         limit_bytes: int | None = None) -> dict[str, Any]:
        self.reads.append(f"artifact {task_id} {name}")
        if self.fail == "artifacts":
            raise SwarmError(f"GET /v1/tasks/{task_id}/artifacts answered HTTP 503")
        asked = self.data["questions"].get(task_id)
        if asked is None:
            return {"status": "absent"}
        return {"status": "ok", "content": json.dumps(asked), "truncated": False}

    # -- everything else goes through request ----------------------------
    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> Any:
        assert method == "GET", f"the board is read-only, and sent {method} {path}"
        self.reads.append(path)
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query)
        parts = url.path.strip("/").split("/")
        if url.path == "/v1/tenants/me":
            return {"tenant": {"tenant_id": "t-acme"}, "principal": {"email": ME}}
        if url.path == "/v1/runs":
            if self.fail == "runs":
                raise SwarmError("GET /v1/runs answered HTTP 404")
            return {"runs": self.data["runs"], "next_page_token": None}
        if parts[:2] == ["v1", "workflows"] and len(parts) == 3:
            for row, tasks in self.data["workflows"]:
                if row["workflow_id"] == parts[2]:
                    return {"workflow": dict(row), "tasks": [dict(t) for t in tasks], "tenant_id": "t-acme"}
            raise SwarmError(f"GET {path} answered HTTP 404", status=404)
        if url.path == "/v1/tasks":
            if self.fail == "tasks":
                raise SwarmError("GET /v1/tasks answered HTTP 500")
            assert query.get("submitted_by") == ["me"], "single tasks are the caller's own"
            rows = list(self.data["single_tasks"])
            if "state" in query:
                rows = [t for t in rows if t["state"] == query["state"][0]]
            rows.sort(key=lambda t: (t["created_at"], t["id"]), reverse=True)
            start = int((query.get("page_token") or ["0"])[0])
            chunk = rows[start:start + 2]
            following = str(start + 2) if start + 2 < len(rows) else None
            return {"tasks": [dict(t) for t in chunk], "next_page_token": following, "tenant_id": "t-acme"}
        if parts[:2] == ["v1", "tasks"] and len(parts) == 4 and parts[3] == "events":
            return {"events": list(self.data["events"].get(parts[2], []))}
        raise AssertionError(f"the board read {path}, which this fixture does not serve")
