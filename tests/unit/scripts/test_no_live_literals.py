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
A label or annotation key prefix (`swarm.<domain>/`) is exempt (plan R5:
renaming a key breaks every live object that matches it).

WHAT IS LIVE comes from the deployment pattern list, never from this file
(docs/design/generic-repository.md sections 1.4 and 4.5): a guard that spells
the value is itself a finding. The list is read the way the deployment-values
guard reads it (`DEPLOYMENT_PATTERNS`, or the file `DEPLOYMENT_PATTERNS_FILE`
names) and parsed by scripts/guards/deployment_values.py, digest line and all.
With no list the scans are skipped, and the skip says why; the guard job is
the one that fails closed on a missing list (section 4.2).

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
import importlib.util
import os
import re
import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
APPS = ROOT / "apps"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass resolves its annotations through sys.modules.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


guard = _load("swarm_guards_deployment_values", ROOT / "scripts" / "guards" / "deployment_values.py")
placeholders = _load("swarm_tests_placeholders", ROOT / "tests" / "placeholders.py")

#: Plan R5: a label or annotation key prefix, not a deployment value.
KEY_PREFIX = re.compile(r"\bswarm\.[a-z0-9.-]+/")
EXCLUDED_APPS = {"common", "swarm-mcp", "swarm-ui"}


def _pattern_list_text() -> str | None:
    path = os.environ.get(guard.LIST_FILE_VARIABLE)
    if path:
        return Path(path).read_text(encoding="utf-8")
    text = os.environ.get(guard.LIST_VARIABLE)
    return text if text and text.strip() else None


def _listed() -> list:
    """The pattern list's entries; skips the scan when no list is given."""
    text = _pattern_list_text()
    if text is None:
        pytest.skip("no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): "
                    "nothing says what is live")
    return guard.parse_list(text).entries


#: (file, module constant) -> why it may name the live deployment.
EXEMPT: dict[tuple[str, str], str] = {
    ("apps/swarm-api/swarm_api/issueruns.py", "SHARED_PROJECT_ID"): (
        "the key of the project-keyed deny-list (plan R4, CLAUDE.md rule 2): the guard "
        "must name the project it protects, and it applies only there"
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


def _live(value: str, entries: list) -> bool:
    rest = KEY_PREFIX.sub("", value)
    return any(entry.pattern.search(rest) for entry in entries)


def _runtime_literals() -> tuple[list[str], set[tuple[str, str]]]:
    entries = _listed()
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
            if not _live(node.value, entries):
                continue
            if id(node) in exempt_ids:
                exempt_seen |= {(rel, name) for name in exempt_names}
                continue
            # Where, and which list line, never the value (section 4.1).
            rule = next(f"list:{e.line}" for e in entries if e.pattern.search(KEY_PREFIX.sub("", node.value)))
            hits.append(f"{rel}:{node.lineno} {rule}")
    return hits, exempt_seen


def test_the_scan_reads_the_services():
    # An empty walk would pass the next test vacuously.
    rels = {p.relative_to(ROOT).as_posix() for p in _sources()}
    assert len(rels) > 100
    assert "apps/swarm-api/swarm_api/issueruns.py" in rels
    assert not any(r.startswith(("apps/common/", "apps/swarm-ui/", "apps/swarm-mcp/")) for r in rels)


def test_no_runtime_literal():
    hits, _ = _runtime_literals()
    # MUTATION: put a listed value back in any service string and this lists it.
    assert hits == [], "live-deployment literals a service holds at runtime:\n" + "\n".join(hits)


def test_every_exemption_still_holds_a_literal():
    _, seen = _runtime_literals()
    # A stale exemption would let the next literal in under its name.
    assert seen == set(EXEMPT)


def test_the_key_prefix_and_comments_are_not_literals():
    # A planted entry, built at run time: the test itself names nothing.
    word = "w" + secrets.token_hex(4)
    body = f"C9\tliteral\t{word}.com\n"
    entries = guard.parse_list(f"# sha256 {guard.digest_of(body)}\n{body}").entries
    assert not _live(f"swarm.{word}.com/tenant", entries)
    assert _live(f"swarm.{word}.com/x {word}.com", entries)
    assert _live(f"{word}_COM", entries)
    tree = ast.parse(f'"""{word}.com, in history."""\n# {word}.com\nX = 1\n')
    assert all(id(n) in _docstrings(tree) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def test_a_missing_list_skips_rather_than_passes(monkeypatch):
    monkeypatch.delenv(guard.LIST_VARIABLE, raising=False)
    monkeypatch.delenv(guard.LIST_FILE_VARIABLE, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _listed()


def test_an_altered_list_is_refused(monkeypatch):
    body = f"C9\tliteral\tw{secrets.token_hex(4)}\n"
    monkeypatch.delenv(guard.LIST_FILE_VARIABLE, raising=False)
    monkeypatch.setenv(guard.LIST_VARIABLE, f"# sha256 {guard.digest_of('other')}\n{body}")
    with pytest.raises(guard.ListError):
        _listed()


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


def _shared_network(issueruns) -> str:
    """The shared project's other-team network, read from the deny-list itself."""
    return next(name for name in issueruns.SHARED_DENY_LIST if name.endswith("-vpc"))


def test_the_deny_list_is_empty_for_another_project(monkeypatch, issueruns):
    monkeypatch.setenv("PROJECT_ID", placeholders.PROJECT)
    # MUTATION: return the shared list whatever the project, and both are red.
    assert issueruns.shared_deny_list() == ()
    assert _shared_hold(issueruns, _shared_network(issueruns)) is None


@pytest.mark.parametrize("project", ["shared", ""])
def test_the_deny_list_is_whole_in_the_shared_project_or_an_unknown_one(monkeypatch, issueruns, project):
    monkeypatch.setenv("PROJECT_ID", issueruns.SHARED_PROJECT_ID if project else "")
    # The guard never weakens where it applies: an unset project is not another project.
    assert issueruns.shared_deny_list() == issueruns.SHARED_DENY_LIST
    held = _shared_hold(issueruns, _shared_network(issueruns))
    assert held is not None and held["reasons"] == ["shared"]


def test_the_shared_list_names_its_project_once(issueruns):
    # Every entry that names the shared project is built from SHARED_PROJECT_ID.
    project = issueruns.SHARED_PROJECT_ID
    assert f"{project}_cloudbuild" in issueruns.SHARED_DENY_LIST
    assert f"api-service@{project}.iam.gserviceaccount.com" in issueruns.SHARED_DENY_LIST
