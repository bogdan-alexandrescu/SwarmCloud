# source-text: contract - every path::qualname docs/BUILD_PROMPT_V2.md cites must still be a symbol whose source holds the cited call
"""The spec says what the code does: owner decisions of 2026-10-01.

WHY THIS EXISTS. The spec moved to match the code: the catalogue
(`swarm_common.profiles`) puts GKE Autopilot on the profiles that reach the
internet and Cloud Run Jobs on the rest, and `BackendRouter` in
apps/scheduler/scheduler/dispatch.py routes them there. The owner decided on
2026-10-01 that Cloud Run Jobs STAY primary and GKE Autopilot runs only the
browser runner.

Each test reads the live catalogue where it can, so a profile moving backend
fails here. The `_cited_symbol` and `_cited_anchor` helpers resolve a symbol or
a text anchor in the code and are shared with test_docs_describe_what_was_built.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]


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

    A class member the class inherits from a mixin it imports from a sibling
    module (`lifecycle.Worker`'s, split out of `lifecycle.py` by issue 68)
    resolves there: `Worker._park_for_quota` is still `Worker`'s.
    """
    source = (REPO / path).read_text(encoding="utf-8")
    module = ast.parse(source, filename=path)
    scope: ast.AST = module
    parts = qualname.split(".")
    for index, part in enumerate(parts):
        found = next((node for node in ast.iter_child_nodes(scope) if _defines(node, part)), None)
        if found is None and isinstance(scope, ast.ClassDef):
            inherited = _inherited_member(path, module, scope, ".".join(parts[index:]))
            if inherited is not None:
                return inherited
        assert found is not None, f"{path} has no {qualname} (no {part!r})"
        scope = found
    lines = source.splitlines()
    start = scope.lineno
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = min([start] + [d.lineno for d in scope.decorator_list])
    else:
        while start > 1 and lines[start - 2].strip().startswith("#:"):
            start -= 1
    return "\n".join(lines[start - 1 : scope.end_lineno])


def _inherited_member(path: str, module: ast.Module, cls: ast.ClassDef, member: str) -> str | None:
    """The source of `member` in a base of `cls` imported `from .sibling import Base`, or None."""
    siblings = {
        alias.asname or alias.name: node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        for alias in node.names
    }
    for base in cls.bases:
        if not (isinstance(base, ast.Name) and base.id in siblings):
            continue
        sibling = str(Path(path).parent / f"{siblings[base.id].replace('.', '/')}.py")
        if not (REPO / sibling).is_file():
            continue
        try:
            return _cited_symbol(sibling, f"{base.id}.{member}")
        except AssertionError:
            continue
    return None


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


def test_the_runtime_image_runs_as_a_single_non_root_user():
    """images/agent-runtime-base/Dockerfile switches to `USER swarm:swarm` exactly once."""
    assert _cited_anchor("images/agent-runtime-base/Dockerfile", "USER swarm:swarm").strip() == "USER swarm:swarm"


def test_lease_account_assigns_through_the_account_broker():
    """AccountPoolMixin._lease_account leases by calling self._account_broker.assign(...)."""
    accountpool = "apps/agent-worker/agent_worker/accountpool.py"
    assert "self._account_broker.assign(" in _cited_symbol(accountpool, "AccountPoolMixin._lease_account")


def test_account_credential_env_builds_env_from_the_account():
    """AccountPoolMixin._account_credential_env derives the child's env via credential_env_from_account."""
    accountpool = "apps/agent-worker/agent_worker/accountpool.py"
    assert "credential_env_from_account(" in _cited_symbol(accountpool, "AccountPoolMixin._account_credential_env")


def test_the_supervised_run_loop_checkpoints_periodically():
    """Worker._run_child_supervised calls self._checkpoint("periodic") in its run loop."""
    lifecycle = "apps/agent-worker/agent_worker/lifecycle.py"
    assert 'self._checkpoint("periodic")' in _cited_symbol(lifecycle, "Worker._run_child_supervised")


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
