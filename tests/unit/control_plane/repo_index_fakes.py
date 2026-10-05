"""Fakes for the repository index (repo-index.md §2-§5, lane RI2).

GitHub is a fake TRANSPORT under the real `forge.GitHubIssues`, extending
RI1's `GitHubRepos` with the two reads an index run makes: the default
branch's head (`git/ref/heads/<branch>`) and a compare between two commits.
Commit shas are built at runtime from a word, so no literal in this file is a
40-hex string a credential scan could mistake for a secret.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import unquote, urlparse

from .repo_fakes import GitHubRepos, repo_entry

BUCKET = "swarm-artifacts-saga-agents-staging"
REPOSITORY = "saga-xyz/widgets"


def sha(word: str) -> str:
    """A commit sha, made from a word: `sha("one")` is the same every time."""
    return hashlib.sha1(word.encode("utf-8")).hexdigest()


class IndexGitHub(GitHubRepos):
    """`GitHubRepos`, plus the branch head and the compare API.

    `heads` maps a branch to its head sha. `compares` maps (base, head) to
    what GitHub's compare answers: `status` (ahead / behind / diverged /
    identical), `ahead_by` and the changed file names. A pair not listed is a
    404, as GitHub answers for a sha it does not know.
    """

    def __init__(self, *, heads: dict[str, str] | None = None,
                 compares: dict[tuple[str, str], dict[str, Any]] | None = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("repos", {REPOSITORY: repo_entry(REPOSITORY, default_branch="main")})
        super().__init__(**kwargs)
        self.heads = dict(heads or {})
        self.compares = dict(compares or {})

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        path = urlparse(url).path
        parts = path.strip("/").split("/")
        if len(parts) >= 6 and parts[0] == "repos" and parts[3:5] == ["git", "ref"]:
            self.calls.append((url, dict(headers)))
            branch = unquote("/".join(parts[6:])) if parts[5] == "heads" else ""
            head = self.heads.get(branch)
            if head is None:
                return 404, b'{"message": "Not Found"}'
            body = {"ref": f"refs/heads/{branch}", "object": {"sha": head, "type": "commit"}}
            return 200, json.dumps(body).encode()
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "compare":
            self.calls.append((url, dict(headers)))
            base, _, head = unquote(parts[4]).partition("...")
            found = self.compares.get((base, head))
            if found is None:
                return 404, b'{"message": "Not Found"}'
            body = {
                "status": found["status"],
                "ahead_by": found.get("ahead_by", 0),
                "behind_by": found.get("behind_by", 0),
                "total_commits": found.get("ahead_by", 0),
                "files": [{"filename": name} for name in found.get("files", [])],
            }
            return 200, json.dumps(body).encode()
        return super().__call__(url, headers, timeout)


def fixture_index(commit_sha: str, **overrides: Any) -> dict[str, Any]:
    """A small, complete `swarm.repo-index/v1` document describing `commit_sha`."""
    document: dict[str, Any] = {
        "schema": "swarm.repo-index/v1",
        "commit_sha": commit_sha,
        "branch": "main",
        "built_at": "2026-10-05T09:00:00Z",
        "kind": "full",
        "extractor": {"ran": True, "command": "swarm-repo-extract", "version": "1"},
        "modules": [
            {"path": "src/api", "language": "python", "purpose": "the HTTP API",
             "files": 12, "lines": 2400},
            {"path": "src/worker", "language": "python", "purpose": "the queue worker",
             "files": 5, "lines": 800},
        ],
        "entry_points": [
            {"path": "src/api/main.py", "kind": "service", "started_by": "uvicorn --factory"},
        ],
        "routes": [
            {"kind": "http", "method": "GET", "path": "/v1/users",
             "file": "src/api/routes/users.py"},
        ],
        "test_layout": [
            {"root": "tests/api", "framework": "pytest",
             "command": "uv run pytest tests/api -q", "covers": ["src/api/**"]},
            {"root": "tests", "framework": "pytest", "command": "uv run pytest tests -q",
             "covers": ["src/**"]},
        ],
        "test_map": [
            {"source": "src/api/routes/users.py", "test": "tests/api/test_users.py",
             "evidence": "import", "command": "uv run pytest tests/api/test_users.py -q"},
            {"source": "src/api/routes/*.py", "test": "tests/api/test_routes.py",
             "evidence": "naming"},
            {"source": "src/worker/**", "test": "tests/worker/test_queue.py",
             "evidence": "co-change"},
        ],
        "always_tests": [
            {"target": "tests/unit/test_contract.py", "because": "declared in CLAUDE.md",
             "source": "CLAUDE.md"},
        ],
        "territory": [
            {"path": "src/common/", "rule": "frozen: import, never edit", "source": "CLAUDE.md"},
        ],
        "commands": [
            {"name": "test", "kind": "test", "command": "make test", "source": "Makefile"},
        ],
        "hot_spots": [
            {"path": "src/api/routes/users.py", "changes": 14,
             "co_changed": ["tests/api/test_users.py"]},
        ],
        "notes": [
            {"text": "Never build a Firestore client at import time.", "source": "CLAUDE.md"},
        ],
        "languages": [
            {"language": "python", "files": 17, "grammar": "tree-sitter-python",
             "server": None, "status": "not_run", "fallback": "file level"},
        ],
        "truncated": [],
    }
    document.update(overrides)
    return document


def finish_index_task(db, objects, task_id: str, document: Any, *, state: str = "SUCCEEDED",
                      attempt: str = "att_1") -> str:
    """The index task ends, leaving repo-index.json exactly as a worker uploads it."""
    doc = db.docs[f"tasks/{task_id}"]
    doc["state"] = state
    if document is None:
        doc["result_summary"] = {"artifacts": []} if state == "SUCCEEDED" else None
        return ""
    raw = document if isinstance(document, str) else json.dumps(document)
    key = (f"tenants/{doc['tenant_id']}/tasks/{task_id}/attempts/{attempt}"
           "/artifacts/repo-index.json")
    objects.put(key, raw)
    doc["result_summary"] = {"artifacts": [
        {"name": "repo-index.json", "bytes": len(raw.encode()), "uri": f"gs://{BUCKET}/{key}"}
    ]}
    return key
