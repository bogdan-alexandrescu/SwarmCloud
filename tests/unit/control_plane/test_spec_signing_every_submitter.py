"""Every path that creates a task document signs it first (#355).

Once the legacy cutover is the signing release, a task created unsigned after
it is refused `spec_signature_invalid`. So every running path that writes a new
task must sign -- the `sc` plugin and the MCP server (both HTTP clients of
`POST /v1/tasks` and `/v1/workflows`), and swarm-api's own internal
submitters: issue runs (`routes/runs.py`, `issueci.py`), merge-wake fix rounds
(`mergewake.py`), ci-fix continuations (`cifix.py`, inside `submit_workflow`),
the repository index (`repoindex.py`) and worker-submitted children
(`children.py`).

ASSERTED STRUCTURALLY, over every package in apps/, not over a list of
submitters restated here: the only code that turns a `Task` into a stored
document (`task_to_firestore`) or calls the store's `create_tasks` /
`create_workflow` is the set below, and each of those signs before it writes.
A new submitter that writes around `SubmissionService` turns this red, which a
list of the submitters known today could not do. The behaviour -- the stored
document verifies under the signature -- is asserted by
`test_spec_signing_submission.py` for `submit_tasks` and `submit_workflow`,
the two methods every internal submitter calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
APPS = REPO / "apps"

#: (path under apps/, enclosing qualname) -> what makes it signed.
WRITERS_OF_A_NEW_TASK = {
    ("swarm-api/swarm_api/store.py", "Store.create_tasks"),
    ("swarm-api/swarm_api/store.py", "Store.create_workflow"),
    ("swarm-api/swarm_api/service.py", "SubmissionService.submit_tasks"),
    ("swarm-api/swarm_api/service.py", "SubmissionService.submit_workflow"),
    ("swarm-api/swarm_api/children.py", "ChildService.submit._apply"),
}

#: The call names that create a task document.
CREATING_CALLS = {"task_to_firestore", "create_tasks", "create_workflow"}


def _python_files() -> list[Path]:
    return sorted(
        p for p in APPS.rglob("*.py")
        if "node_modules" not in p.parts and ".venv" not in p.parts
    )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[str, str, int]] = []
        self.functions: dict[str, ast.AST] = {}

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.functions[".".join(self.stack)] = node
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name in CREATING_CALLS:
            self.found.append((".".join(self.stack), name, node.lineno))
        self.generic_visit(node)


def _scan(path: Path) -> _Scan:
    scan = _Scan()
    scan.visit(ast.parse(path.read_text(), filename=str(path)))
    return scan


def _sign_lines(function: ast.AST) -> list[int]:
    """Lines of `<anything>._sign(...)` / `sign_task_specs(...)` calls."""
    return [
        node.lineno for node in ast.walk(function)
        if isinstance(node, ast.Call) and _call_name(node) in ("_sign", "sign_task_specs")
    ]


def test_the_scan_reads_every_app():
    files = _python_files()
    assert len(files) > 100, f"scanned only {len(files)} files under {APPS}"
    assert any(p.parts[-3:] == ("swarm-api", "swarm_api", "store.py") for p in files)


def test_only_the_signing_paths_create_a_task_document():
    seen: set[tuple[str, str]] = set()
    stray: list[str] = []
    for path in _python_files():
        rel = path.relative_to(APPS).as_posix()
        for qualname, name, line in _scan(path).found:
            if qualname == "" or (rel, qualname) == ("swarm-api/swarm_api/codec.py", "task_to_firestore"):
                continue  # an import or the definition's own module scope
            if (rel, qualname) in WRITERS_OF_A_NEW_TASK:
                seen.add((rel, qualname))
            else:
                stray.append(f"apps/{rel}:{line} {qualname} calls {name}")
    assert not stray, (
        "a path creates a task document outside the signing paths; route it through "
        "SubmissionService.submit_tasks/submit_workflow, or the worker refuses it as "
        "unsigned:\n" + "\n".join(stray)
    )
    assert seen == WRITERS_OF_A_NEW_TASK, f"a known writer moved: {WRITERS_OF_A_NEW_TASK - seen}"


def test_each_submission_method_signs_before_it_stores():
    scan = _scan(APPS / "swarm-api/swarm_api/service.py")
    for method in ("SubmissionService.submit_tasks", "SubmissionService.submit_workflow"):
        function = scan.functions[method]
        stores = [line for q, name, line in scan.found if q == method
                  and name in ("create_tasks", "create_workflow")]
        signs = _sign_lines(function)
        assert stores and signs, f"{method} stores {stores} and signs {signs}"
        assert max(signs) < min(stores), f"{method} stores before it signs"


def test_a_child_is_signed_before_the_transaction_that_stores_it():
    scan = _scan(APPS / "swarm-api/swarm_api/children.py")
    submit = scan.functions["ChildService.submit"]
    build = [n.lineno for n in ast.walk(submit)
             if isinstance(n, ast.Call) and _call_name(n) == "_build_child"]
    apply_ = scan.functions["ChildService.submit._apply"]
    assert build and build[0] < apply_.lineno, "the child is built after its transaction"
    assert _sign_lines(scan.functions["ChildService._build_child"]), (
        "ChildService._build_child no longer signs the child it builds"
    )


def test_the_internal_submitters_go_through_the_submission_service():
    """Issue runs, merge-wake fix rounds, ci-fix continuations and the repo
    index each submit through `submit_tasks` / `submit_workflow`."""
    expected = {
        "swarm-api/swarm_api/routes/runs.py": {"submit_tasks", "submit_workflow"},
        "swarm-api/swarm_api/issueci.py": {"submit_workflow"},
        "swarm-api/swarm_api/mergewake.py": {"submit_workflow"},
        "swarm-api/swarm_api/repoindex.py": {"submit_tasks"},
    }
    for rel, names in expected.items():
        tree = ast.parse((APPS / rel).read_text())
        called = {_call_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert names <= called, f"apps/{rel} no longer calls {names - called}"
    # ci-fix stamps inside submit_workflow; it never submits on its own.
    cifix = ast.parse((APPS / "swarm-api/swarm_api/cifix.py").read_text())
    assert not {_call_name(n) for n in ast.walk(cifix) if isinstance(n, ast.Call)} & CREATING_CALLS
