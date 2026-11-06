"""No service under `apps/` carries a live-deployment literal at runtime.

Lesson 5 of the Day One install (docs/design/day-one/lessons.md): defaults
named the live project, so a new install inherited the live deployment's
identity. docs/design/day-one-plan.md section 1.1, lane DAYONE-DESAGA-3.

`test_no_runtime_literal` parses every Python file under `apps/*/` and reads
each `ast.Constant` string -- the values the code holds when it runs.
Comments are not in the tree and docstrings are skipped, so a comment may
still mention history. Excluded: tests; `apps/common` (frozen, CLAUDE.md
rule 1; its literals are contract requests 127 and 128); `apps/swarm-mcp`
and `apps/swarm-ui` (lanes DAYONE-PLUGIN-1 and DAYONE-DESAGA-4 own them).
The `swarm.saga.xyz/` label and annotation key prefix is exempt by name
(plan R5: renaming a key breaks every live object that matches it).

`EXEMPT` names the module constants that may hold a live literal, each with
why; `test_every_exemption_still_holds_a_literal` fails the day one no longer
does, so the list only shrinks.

The issueruns deny-list is keyed by project (plan R4): the shared project's
names hold changes for the owner only when swarm-api runs in the shared
project (or the project is unknown), and nowhere else.

Offline: parses source and calls pure functions; no credentials, no emulator.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
APPS = ROOT / "apps"

#: What the live deployment is called: its project, its Workspace domain, its
#: forge owner and its tailnet.
LIVE = re.compile(r"saga-agents-staging|saga\.xyz|bogdan-alexandrescu|taile0bda9")
#: Plan R5: a label or annotation key, not a deployment value.
KEY_PREFIX = "swarm.saga.xyz/"
EXCLUDED_APPS = {"common", "swarm-mcp", "swarm-ui"}

#: (file, module constant) -> why it may name the live deployment.
EXEMPT: dict[tuple[str, str], str] = {
    ("apps/swarm-api/swarm_api/issueruns.py", "SHARED_PROJECT_ID"): (
        "the key of the project-keyed deny-list (plan R4, CLAUDE.md rule 2): the guard "
        "must name the project it protects, and it applies only there"
    ),
    ("apps/scheduler/scheduler/judgment.py", "DECISION_API_TAILNET_NAME"): (
        "the internal decision API add-on's one accepted host, off unless "
        "INTERNAL_DECISION_API_URL is set; making it per install crosses "
        "test_dispatch_judgment.py, test_judgment_backend.py and the worker's jev "
        "wrapper (question 2 of this lane)"
    ),
    ("apps/swarm-api/swarm_api/judgment.py", "INTERNAL_DECISION_ORIGIN"): (
        "restates scheduler.judgment.INTERNAL_DECISION_ORIGIN (same question)"
    ),
}


def _sources() -> list[Path]:
    found = []
    for path in sorted(APPS.glob("*/**/*.py")):
        rel = path.relative_to(APPS).parts
        if rel[0] in EXCLUDED_APPS or "tests" in rel or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        found.append(path)
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, owners) and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _exempt_constants(tree: ast.Module, rel: str) -> tuple[set[int], set[str]]:
    """The constants inside an exempt module-level assignment, and its names."""
    ids: set[int] = set()
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and (rel, target.id) in EXEMPT and node.value is not None:
                names.add(target.id)
                ids |= {id(sub) for sub in ast.walk(node.value)}
    return ids, names


def _live(value: str) -> bool:
    return bool(LIVE.search(value.replace(KEY_PREFIX, "")))


def _runtime_literals() -> tuple[list[str], set[tuple[str, str]]]:
    hits: list[str] = []
    exempt_seen: set[tuple[str, str]] = set()
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        skip = _docstrings(tree)
        exempt_ids, exempt_names = _exempt_constants(tree, rel)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in skip:
                continue
            if not _live(node.value):
                continue
            if id(node) in exempt_ids:
                exempt_seen |= {(rel, name) for name in exempt_names}
                continue
            hits.append(f"{rel}:{node.lineno}: {node.value[:100]!r}")
    return hits, exempt_seen


def test_the_scan_reads_the_services():
    # An empty walk would pass the next test vacuously.
    rels = {p.relative_to(ROOT).as_posix() for p in _sources()}
    assert len(rels) > 100
    assert "apps/swarm-api/swarm_api/issueruns.py" in rels
    assert not any(r.startswith(("apps/common/", "apps/swarm-ui/", "apps/swarm-mcp/")) for r in rels)


def test_no_runtime_literal():
    hits, _ = _runtime_literals()
    # MUTATION: put "saga-agents-staging" back in any service string and this lists it.
    assert hits == [], "live-deployment literals a service holds at runtime:\n" + "\n".join(hits)


def test_every_exemption_still_holds_a_literal():
    _, seen = _runtime_literals()
    # A stale exemption would let the next literal in under its name.
    assert seen == set(EXEMPT)


def test_the_key_prefix_and_comments_are_not_literals():
    assert not _live(KEY_PREFIX + "tenant")
    assert _live(KEY_PREFIX + "x saga.xyz")
    tree = ast.parse('"""saga-agents-staging, in history."""\n# saga.xyz\nX = 1\n')
    assert all(id(n) in _docstrings(tree) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))


# --------------------------------------------------------------------------
# the issueruns deny-list, keyed by project (plan R4)
# --------------------------------------------------------------------------

@pytest.fixture
def issueruns():
    """swarm_api.issueruns, imported here: tests/unit/scripts collects without
    the service stack (test_collection_stays_cheap.py)."""
    from swarm_api import issueruns as module

    return module


def _shared_hold(issueruns, name: str):
    path = "terraform/infra/network.tf"
    patch = f'--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n unchanged\n+  network = "{name}"\n'
    return issueruns.hard_stop_hold(
        [path], platform=True, hard_stop_paths=[], patches={path: patch}, prompt_text="",
    )


def test_the_deny_list_is_empty_for_another_project(monkeypatch, issueruns):
    monkeypatch.setenv("PROJECT_ID", "acme-swarm-prod")
    # MUTATION: return the shared list whatever the project, and both are red.
    assert issueruns.shared_deny_list() == ()
    assert _shared_hold(issueruns, "agents-staging-vpc") is None


@pytest.mark.parametrize("project", ["shared", ""])
def test_the_deny_list_is_whole_in_the_shared_project_or_an_unknown_one(monkeypatch, issueruns, project):
    monkeypatch.setenv("PROJECT_ID", issueruns.SHARED_PROJECT_ID if project else "")
    # The guard never weakens where it applies: an unset project is not another project.
    assert issueruns.shared_deny_list() == issueruns.SHARED_DENY_LIST
    held = _shared_hold(issueruns, "agents-staging-vpc")
    assert held is not None and held["reasons"] == ["shared"]


def test_the_shared_list_names_its_project_once(issueruns):
    # Every entry that names the shared project is built from SHARED_PROJECT_ID.
    project = issueruns.SHARED_PROJECT_ID
    assert f"{project}_cloudbuild" in issueruns.SHARED_DENY_LIST
    assert f"api-service@{project}.iam.gserviceaccount.com" in issueruns.SHARED_DENY_LIST
