"""forgeperms reports `workflows: write` missing ONLY where `workflow_files` is on (#2029).

BGD, 2026-10-22 (WF-APP-TOKEN, decision 4): the App asks for "Workflows: Read
and write"; an installation that has not accepted it reads as "Workflows: not
granted" in the console -- on a repository whose `workflow_files` switch is
on, and nowhere else. `REQUIRED`, what every other repository needs, is
unchanged.

Offline against the fake GitHub transport of test_forgeperms.py.
"""

from __future__ import annotations

from swarm_api import forgeperms
from swarm_api.forgeperms import REQUIRED, WORKFLOW_FILES_REQUIREMENT, Registered
from swarm_api.repositories import repo_id_for

from .test_forgeperms import FULL, FakeGitHub, _app, _installation, _installed, _read, _service


def _repo(owner: str, repo: str, *, workflow_files: bool) -> Registered:
    return Registered(repo_id=repo_id_for("eng", owner, repo), owner=owner, repo=repo,
                      default_branch="main", workflow_files=workflow_files)


def _row(answer, repo):
    return next(row for row in answer["repositories"] if row["repo"] == repo)


def _setup(granted: dict[str, str]) -> FakeGitHub:
    github = FakeGitHub()
    _app(github, {**FULL, "workflows": "write"})
    _installed(github, "sagaxyz", "on", 7)
    _installed(github, "sagaxyz", "off", 7)
    _installation(github, 7, granted)
    return github


def test_required_is_unchanged_for_every_other_repository():
    # MUTATION: add workflows to REQUIRED and every repository reports it.
    assert all(req.permission != "workflows" for req in REQUIRED)
    assert (WORKFLOW_FILES_REQUIREMENT.permission, WORKFLOW_FILES_REQUIREMENT.access) == \
        ("workflows", "write")
    assert WORKFLOW_FILES_REQUIREMENT.capabilities == ("workflow_files",)


def test_not_granted_is_reported_on_the_switched_on_repository_only():
    github = _setup(FULL)  # the installation has not accepted Workflows
    answer = _read(_service(github), [_repo("sagaxyz", "on", workflow_files=True),
                                      _repo("sagaxyz", "off", workflow_files=False)])
    on, off = _row(answer, "sagaxyz/on"), _row(answer, "sagaxyz/off")
    # MUTATION: never check the switched-on requirement and this is True.
    assert on["capabilities"]["workflow_files"] is False
    assert [gap["permission"] for gap in on["blocked_by"]] == ["workflows"]
    # MUTATION: check workflows on every repository and `off` reports it too.
    assert "workflow_files" not in off["capabilities"]
    assert off["blocked_by"] == []
    # The org-wide and App-wide `missing` still read REQUIRED alone.
    assert all(gap["permission"] != "workflows" for gap in answer["app"]["missing"])
    assert all(gap["permission"] != "workflows"
               for org in answer["orgs"] for gap in org["missing"])


def test_a_granted_installation_reads_workflow_files_true_on_the_switched_on_repository():
    github = _setup({**FULL, "workflows": "write"})
    answer = _read(_service(github), [_repo("sagaxyz", "on", workflow_files=True)])
    row = _row(answer, "sagaxyz/on")
    # MUTATION: compare to "admin" and a write grant reads not granted.
    assert row["capabilities"]["workflow_files"] is True
    assert row["blocked_by"] == []


def test_a_switched_on_repository_without_the_app_cannot_push_workflow_files():
    github = FakeGitHub()
    _app(github, {**FULL, "workflows": "write"})
    # No installation covers it: the tenant token is read for the other capabilities.
    repo = _repo("someone", "solo", workflow_files=True)
    github.on("/repos/someone/solo", 200, {"default_branch": "main",
                                          "permissions": {"push": True}})
    github.on("/repos/someone/solo/commits/main/status", 200, {"state": "success"})
    answer = _read(_service(github), [repo], token="ghp" + "_" + "x" * 36)
    row = _row(answer, "someone/solo")
    # MUTATION: leave a token-backed switched-on repository unmeasured and this is None.
    assert row["capabilities"]["workflow_files"] is False
    assert any("workflow_files" in gap["capabilities"] for gap in row["blocked_by"])


def test_the_route_reads_the_switch_from_the_registration(db):
    from datetime import datetime, timezone

    from swarm_api.deps import AppContext  # noqa: F401  (the type the route reads)

    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    for repo, on in (("on", True), ("off", None), ("str", "true")):
        repo_id = repo_id_for("eng", "sagaxyz", repo)
        doc = {"repo_id": repo_id, "tenant_id": "eng", "owner": "sagaxyz", "repo": repo,
               "default_branch": "main", "index": {}}
        if on is not None:
            doc["workflow_files"] = on
        db.docs[f"repositories/{repo_id}"] = doc

    class Ctx:
        pass

    ctx = Ctx()
    ctx.db = db
    ctx.now = lambda: now
    found = {r.repo: r.workflow_files for r in forgeperms._registered(ctx, "eng")}
    # MUTATION: read the switch with `bool(...)` and "true" is on; drop it and all are off.
    assert found == {"on": True, "off": False, "str": False}
