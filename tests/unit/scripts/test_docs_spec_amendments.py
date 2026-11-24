# source-text: contract - every path::qualname docs/BUILD_PROMPT_V2.md cites must still be a symbol whose source holds the cited call
"""The spec says what the code does: owner decisions of 2026-10-01.

WHY THIS EXISTS. Two documents described a platform this repository does not
run:

  * docs/BUILD_PROMPT_V2.md called itself "specification, not yet built" and
    said the substrate was "GKE Autopilot, and only GKE Autopilot ...
    Everything is a pod". The catalogue (`swarm_common.profiles`) puts four of
    the five profiles on Cloud Run Jobs and `BackendRouter` in
    apps/scheduler/scheduler/dispatch.py routes them there. The owner decided
    on 2026-10-01 that Cloud Run Jobs STAY primary and GKE Autopilot runs only
    the browser runner, and that the spec moves to match the code.
  * docs/cost-control.md told an admin to set `monthly_budget_usd` and promised
    `PARKED(BUDGET_EXHAUSTED)`. The API refuses that field (422) and nothing
    writes that park. The owner decided the same day there are no per-tenant
    budgets, and none are planned.

Each test reads the live catalogue where it can, so a profile moving backend
fails here rather than leaving the spec quietly wrong again.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]
BUILD_PROMPT = REPO / "docs" / "BUILD_PROMPT_V2.md"
CONTRACT = REPO / "CONTRACT.md"
COST = REPO / "docs" / "cost-control.md"
TENANCY = REPO / "docs" / "multi-tenancy.md"
QUOTA = REPO / "docs" / "quota-management.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"
ARCHITECTURE = REPO / "docs" / "architecture.md"

AMENDED = (BUILD_PROMPT, CONTRACT, COST, TENANCY, QUOTA, REQUESTS, ARCHITECTURE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backends() -> dict[str, Backend]:
    return {name: resolve_backend(p) for name, p in RUNNER_PROFILES.items()}


# --------------------------------------------------------------------------
# the catalogue these documents describe
# --------------------------------------------------------------------------


def test_the_catalogue_is_what_the_amendment_describes():
    """The premise: GKE for every profile that reaches the internet, Cloud Run Jobs for the rest.

    If this fails the profiles moved, and every document below is describing
    the old split: amend them before changing the expectation here.
    """
    backends = _backends()
    # claude-code: contract request 53, applied 2026-10-08 after request 55's
    # canary (claude-code-gke, removed by the same change). indexer: contract
    # request 63 (owner, 2026-10-10), the canary for archived issue 939. generic, codex and
    # merge: contract requests 64, 65 and 66 (owner, 2026-10-10, archived issue 939 option A).
    # claude-code-browser: contract request 67 (owner, 2026-10-11), on the
    # browser image, which runs only on GKE. claude-code-large: contract
    # request 113 (owner, 2026-10-22), claude-code on the large class.
    # claude-code-lightpanda: contract request 127 (owner, 2026-10-31),
    # claude-code on the Lightpanda image, the standard class.
    # claude-code-medium: contract request 147 (owner, 2026-11-06), claude-code
    # on the medium class.
    assert {n for n, b in backends.items() if b is Backend.GKE_AUTOPILOT} == {
        "browser",
        "claude-code-browser",
        "claude-code",
        "claude-code-large",
        "claude-code-lightpanda",
        "claude-code-medium",
        "indexer",
        "generic",
        "codex",
        "merge",
    }
    assert {n for n, b in backends.items() if b is Backend.CLOUD_RUN_JOB} == {
        # reaches no internet, so archived issue 939 option A left it.
        "mock",
        # archived issue 295, contract requests 35 and 36: each designed to run as its own
        # account, which a GKE pod cannot be (contract request 66).
        "post-verdict",
        "claude-code-review",
        # Contract requests 93 and 94: a control_plane profile, which nothing
        # runs on any backend; CLOUD_RUN_JOB is the catalogue's statement only.
        "await-input",
        # Contract request 124: the second control_plane profile, the same way.
        "fix-loop",
    }


# --------------------------------------------------------------------------
# BUILD_PROMPT_V2.md
# --------------------------------------------------------------------------


def _cited_symbol(path: str, qualname: str) -> str:
    """The source of the function, class or assigned name `qualname` (dotted, e.g. `Worker._lease_account`) in `path`.

    archived issue 647: a line number in a 10,000-line module moves whenever a lane edits
    above it, so a large Python file is cited as `path::qualname` instead and
    the cited call must sit inside that symbol's body. A call that moves out
    of the function, or a function that is renamed, still fails here.

    The source includes what a reader sees as part of the symbol: a function's
    decorators (a route's `@router.post("/authorize")`), and for a module or
    class level name (`TOKEN_ENDPOINT`, `WorkerConfig.checkpoint_interval_seconds`)
    the `#:` comment block directly above it, the convention that documents a
    name in this repository.
    """
    source = (REPO / path).read_text(encoding="utf-8")
    scope: ast.AST = ast.parse(source, filename=path)
    for part in qualname.split("."):
        scope = next((node for node in ast.iter_child_nodes(scope) if _defines(node, part)), None)
        assert scope is not None, f"{path} has no {qualname} (no {part!r})"
    lines = source.splitlines()
    start = scope.lineno
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = min([start] + [d.lineno for d in scope.decorator_list])
    else:
        while start > 1 and lines[start - 2].strip().startswith("#:"):
            start -= 1
    return "\n".join(lines[start - 1 : scope.end_lineno])


def _defines(node: ast.AST, name: str) -> bool:
    """Whether `node` is the def, class or single-name assignment of `name`."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def _cited_anchor(path: str, text: str, *, once: bool = True) -> str:
    """The line of `path` that holds `text`, which a doc cites as `path` (`text`).

    A non-Python file (a Dockerfile, a script, Terraform, markdown) has no
    symbol to name, so a doc cites the exact text it points at instead of a
    line number: archived pull request 675 added 12 lines to the Dockerfile and pushed
    `USER swarm:swarm` past the +/-150-line window this replaced, which turned
    main red over a citation that was still true. An anchor never drifts; it
    fails only when the text is gone, which is when the doc is wrong.

    `once` (the default) also holds that the text names ONE place: an anchor
    that occurs twice does not say which of the two the doc means.
    """
    assert "\n" not in text, f"an anchor is one line of {path}: {text!r}"
    target = REPO / path
    assert target.is_file(), f"cited {path}, which does not exist"
    source = target.read_text(encoding="utf-8")
    count = source.count(text)
    assert count, f"{path} no longer contains the cited text {text!r}"
    if once:
        assert count == 1, f"{path} contains the cited text {text!r} {count} times: cite one place"
    return next(line for line in source.splitlines() if text in line)


#: The prefixes a citation into this repository starts with.
_ROOTS = r"(?:apps|terraform|kubernetes|scripts|tests|images|plugin|docs|\.github)/"

#: `path:N` or `path:N-M`: the form archived issue 647 and lane CITD retired. Any match fails.
_LINE_CITE = re.compile(rf"`({_ROOTS}[\w./-]+\.\w+:\d+(?:-\d+)?)`")

#: `path::qualname`, a Python function, class or name.
_SYMBOL_CITE = re.compile(rf"`({_ROOTS}[\w./-]+\.py)::([\w.]+)`")

#: `path` (`text`): the exact text a non-Python citation points at. `\s+`, not
#: one space, so a citation the doc wraps between `path` and (`text`) is still
#: checked rather than silently skipped.
_ANCHOR_CITE = re.compile(rf"`({_ROOTS}[\w./-]+)`\s+\(`([^`]+)`\)")

#: Anchors a doc cites that occur more than once in their file, and why each
#: cannot be narrowed to one. Every other anchor must occur exactly once.
_REPEATED_ANCHORS: frozenset[tuple[str, str]] = frozenset()


def _assert_cites_resolve(doc: Path) -> None:
    """Every citation in `doc` is a symbol or an anchor that resolves, and none is a line number."""
    text = _text(doc)
    stale = _LINE_CITE.findall(text)
    assert not stale, f"{doc.name} still cites line numbers, which drift: {stale}"
    for path, qualname in _SYMBOL_CITE.findall(text):
        assert (REPO / path).is_file(), f"{doc.name} cites {path}, which does not exist"
        _cited_symbol(path, qualname)
    for path, anchor in _ANCHOR_CITE.findall(text):
        assert "\n" not in anchor, f"{doc.name} wraps the anchor it cites in {path}: keep it on one line"
        _cited_anchor(path, anchor, once=(path, anchor) not in _REPEATED_ANCHORS)


def test_the_runtime_image_runs_as_a_single_non_root_user():
    """images/agent-runtime-base/Dockerfile switches to `USER swarm:swarm` exactly once."""
    assert _cited_anchor("images/agent-runtime-base/Dockerfile", "USER swarm:swarm").strip() == "USER swarm:swarm"


def test_lease_account_assigns_through_the_account_broker():
    """Worker._lease_account leases by calling self._account_broker.assign(...)."""
    lifecycle = "apps/agent-worker/agent_worker/lifecycle.py"
    assert "self._account_broker.assign(" in _cited_symbol(lifecycle, "Worker._lease_account")


def test_account_credential_env_builds_env_from_the_account():
    """Worker._account_credential_env derives the child's env via credential_env_from_account."""
    lifecycle = "apps/agent-worker/agent_worker/lifecycle.py"
    assert "credential_env_from_account(" in _cited_symbol(lifecycle, "Worker._account_credential_env")


def test_the_supervised_run_loop_checkpoints_periodically():
    """Worker._run_child_supervised calls self._checkpoint("periodic") in its run loop."""
    lifecycle = "apps/agent-worker/agent_worker/lifecycle.py"
    assert 'self._checkpoint("periodic")' in _cited_symbol(lifecycle, "Worker._run_child_supervised")


# --------------------------------------------------------------------------
# every citation an amended document makes resolves, and none is a line number
# --------------------------------------------------------------------------

CHILD_TASKS = REPO / "docs" / "design" / "child-tasks.md"

#: The amended documents, and the child-tasks design, whose lifecycle.py line
#: numbers drifted the same way archived issue 647's did.
CITING = AMENDED + (CHILD_TASKS,)


@pytest.mark.parametrize("doc", CITING, ids=lambda p: p.name)
def test_every_citation_is_a_symbol_or_an_anchor_that_resolves(doc: Path):
    """No `path:N`; every `path::qualname` names a def, class or name; every `path` (`text`) finds its text."""
    _assert_cites_resolve(doc)


def test_an_anchor_that_is_gone_or_ambiguous_fails():
    """The helpers' own failure modes: the reason a moved line no longer turns main red is
    that the anchor is checked for presence instead, so presence must really be checked."""
    dockerfile = "images/agent-runtime-base/Dockerfile"
    with pytest.raises(AssertionError, match="no longer contains"):
        _cited_anchor(dockerfile, "USER swarm:swarm-that-is-not-there")
    with pytest.raises(AssertionError, match="times: cite one place"):
        _cited_anchor(dockerfile, "RUN ")
    assert _cited_anchor(dockerfile, "RUN ", once=False).lstrip().startswith("RUN ")
    with pytest.raises(AssertionError, match="has no"):
        _cited_symbol("kubernetes/render.py", "JOB_FILES_GVISOR_GONE")
    # A name's `#:` block is part of what it cites; a function's decorator is too.
    assert _cited_symbol("kubernetes/render.py", "JOB_FILES_GVISOR").startswith("#: v2: root inside a gVisor sandbox.")
    assert _cited_symbol("apps/swarm-api/swarm_api/routes/platform.py", "runtimes").startswith("@router.get(")
