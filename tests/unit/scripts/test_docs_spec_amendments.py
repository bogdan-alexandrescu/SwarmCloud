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


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


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
    # request 63 (owner, 2026-10-10), the canary for #939. generic, codex and
    # merge: contract requests 64, 65 and 66 (owner, 2026-10-10, #939 option A).
    assert {n for n, b in backends.items() if b is Backend.GKE_AUTOPILOT} == {
        "browser",
        "claude-code",
        "indexer",
        "generic",
        "codex",
        "merge",
    }
    assert {n for n, b in backends.items() if b is Backend.CLOUD_RUN_JOB} == {
        # reaches no internet, so #939 option A left it.
        "mock",
        # #295, contract requests 35 and 36: each designed to run as its own
        # account, which a GKE pod cannot be (contract request 66).
        "post-verdict",
        "claude-code-review",
    }


# --------------------------------------------------------------------------
# BUILD_PROMPT_V2.md
# --------------------------------------------------------------------------


def test_build_prompt_no_longer_claims_to_be_unbuilt():
    status = next(line for line in _text(BUILD_PROMPT).splitlines() if line.startswith("**Status:**"))
    assert "not yet built" not in status
    assert "built" in status


def test_build_prompt_substrate_is_cloud_run_primary_with_gke_for_browser():
    text = _text(BUILD_PROMPT)
    assert "only GKE Autopilot" not in text
    assert "Everything is a pod" not in text
    heading = next(line for line in text.splitlines() if line.startswith("### 2.1 "))
    assert "Cloud Run Jobs" in heading
    body = _section(text, heading)
    for name, backend in _backends().items():
        label = "Cloud Run Jobs" if backend is Backend.CLOUD_RUN_JOB else "GKE Autopilot"
        row = next((line for line in body.splitlines() if line.startswith(f"| `{name}` ")), None)
        assert row is not None, f"§2.1 has no row for the {name!r} profile"
        assert label in row, f"§2.1 puts {name!r} somewhere the catalogue does not: {row}"
    assert "swarm_common/profiles.py" in body
    assert "apps/scheduler/scheduler/dispatch.py" in body
    assert "2026-10-01" in body


def test_build_prompt_records_why_and_the_workspace_storage_correction():
    body = _section(_text(BUILD_PROMPT), next(
        line for line in _text(BUILD_PROMPT).splitlines() if line.startswith("### 2.1 ")
    ))
    # WHY: the v1 rationale in architecture.md §5, cited.
    assert "docs/architecture.md" in body
    assert "no nodes" in body
    # The CLAUDE.md "Correction (workspace storage)", not softened.
    for phrase in ("tmpfs", "GA", "live migration", "Preview"):
        assert phrase in body, phrase
    # Checkpointing stays mandatory, for the reasons CLAUDE.md gives.
    for reason in ("park-and-exit", "cancellation", "stale generation", "crash"):
        assert reason in body, reason
    assert "mandatory" in body.lower()


def test_build_prompt_no_longer_lists_cloud_run_jobs_for_deletion():
    text = _text(BUILD_PROMPT)
    deleted = _section(text, "## 6. What gets deleted")
    assert "terraform/modules/cloud_run_jobs/" not in deleted or "NOT deleted" in deleted
    assert "CLOUD_RUN_JOB` member and every branch" not in deleted


def _cited_symbol(path: str, qualname: str) -> str:
    """The source of the function, class or assigned name `qualname` (dotted, e.g. `Worker._lease_account`) in `path`.

    #647: a line number in a 10,000-line module moves whenever a lane edits
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
    line number: #675 added 12 lines to the Dockerfile and pushed
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

#: `path:N` or `path:N-M`: the form #647 and lane CITD retired. Any match fails.
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


def test_build_prompt_marks_the_unbuilt_root_gvisor_shape():
    """§2.2, §2.6.3 and §3 describe root + gVisor, an init container and a
    sidecar: none of it runs. Each carries a dated note saying what does, and
    the lines those notes cite still say what the notes claim."""
    text = _text(BUILD_PROMPT)
    isolation = _section(text, "### 2.2 Isolation: root inside the pod, gVisor underneath")
    assert "Amended 2026-10-01" in isolation
    assert "`images/agent-runtime-base/Dockerfile` (`USER swarm:swarm`)" in isolation
    assert _cited_anchor("images/agent-runtime-base/Dockerfile", "USER swarm:swarm").strip() == "USER swarm:swarm"
    assert "`kubernetes/render.py::JOB_FILES_GVISOR`" in isolation
    gvisor = _cited_symbol("kubernetes/render.py", "JOB_FILES_GVISOR")
    # One line says both: that `--runtime gvisor` selects it and that it is
    # NOT the default. Two phrases anywhere in the symbol's comment block
    # would pass a comment that split or dropped the negation (#453).
    assert any("--runtime gvisor" in line and "NOT the" in line for line in gvisor.splitlines()), (
        "kubernetes/render.py JOB_FILES_GVISOR no longer says, on one line, that "
        "`--runtime gvisor` selects it and it is NOT the default"
    )

    dispatch = _section(text, "#### 2.6.3 Dispatch — the pod starts already logged in")
    assert "Amended 2026-10-01" in dispatch
    assert "no init container" in dispatch
    lifecycle = "apps/agent-worker/agent_worker/lifecycle.py"
    accountlease = "apps/agent-worker/agent_worker/accountlease.py"
    assert f"`{lifecycle}::Worker._lease_account`" in dispatch
    assert "self._account_broker.assign(" in _cited_symbol(lifecycle, "Worker._lease_account")
    assert f"`{lifecycle}::Worker._account_credential_env`" in dispatch
    assert "credential_env_from_account(" in _cited_symbol(lifecycle, "Worker._account_credential_env")
    assert f"`{accountlease}::credential_env_from_account`" in dispatch
    assert _cited_symbol(accountlease, "credential_env_from_account").startswith(
        "def credential_env_from_account("
    )

    built = _section(text, "## 5. What gets built")
    assert f"`{lifecycle}::Worker._run_child_supervised`" in built
    assert 'self._checkpoint("periodic")' in _cited_symbol(lifecycle, "Worker._run_child_supervised")
    # #647: no line number into lifecycle.py is left for a lane to shift.
    assert not re.search(r"lifecycle\.py:\d", text)

    architecture = _section(text, "## 3. Architecture")
    assert "Amended 2026-10-01" in architecture
    assert "`browser`" in architecture and "`claude-code`" in architecture


# --------------------------------------------------------------------------
# CONTRACT.md
# --------------------------------------------------------------------------


def test_contract_names_the_backend_split_per_profile():
    decisions = _section(_text(CONTRACT), "## Platform decisions")
    line = next(line for line in decisions.split("\n- ") if "Primary backend" in line)
    for name in ("mock", "generic", "claude-code", "codex", "browser"):
        assert f"`{name}`" in line, name
    assert "swarm_common/profiles.py" in line
    assert "dispatch.py" in line
    assert "2026-10-01" in line


def test_contract_keeps_the_live_migration_constraint_and_its_correction():
    text = _text(CONTRACT)
    # CLAUDE.md: must stay in the docs and must not be softened.
    assert "Cloud Run ephemeral disk is Preview and" in text
    assert "disables live migration" in text
    assert "Correction (workspace storage)" in text


def test_contract_says_what_pod_means_on_each_backend():
    text = _text(CONTRACT)
    assert "Cloud Run Job execution" in text


# --------------------------------------------------------------------------
# budgets: cost-control.md, multi-tenancy.md, quota-management.md
# --------------------------------------------------------------------------


def test_cost_control_does_not_advertise_a_budget():
    text = _text(COST)
    assert '"monthly_budget_usd": 2000' not in text
    assert "A tenant over budget gets" not in text
    assert "per-tenant budgets |" not in text
    for phrase in ("not built and not planned", "2026-10-01", "record_spend", "BUDGET_EXHAUSTED", "422"):
        assert phrase in text, phrase


def test_multi_tenancy_does_not_say_nothing_attributes_cost():
    text = _text(TENANCY)
    assert "nothing attributes cost" not in text
    for phrase in ("record_spend", "2026-10-01", "BUDGET_EXHAUSTED"):
        assert phrase in text, phrase


def test_quota_management_marks_budget_exhausted_never_written():
    row = next(line for line in _text(QUOTA).splitlines() if line.startswith("| `BUDGET_EXHAUSTED` |"))
    assert "never written" in row
    assert "states.py" in row


# --------------------------------------------------------------------------
# contract-change-requests.md
# --------------------------------------------------------------------------


def test_budget_exhausted_is_recorded_as_a_request_not_an_edit():
    text = _text(REQUESTS)
    heading = next(
        (line for line in text.splitlines() if line.startswith("## ") and "BUDGET_EXHAUSTED" in line),
        None,
    )
    assert heading is not None, "no entry records BUDGET_EXHAUSTED as unused"
    number = re.match(r"## (\d+)\.", heading).group(1)
    assert re.search(rf"^\| {number} \| .*BUDGET_EXHAUSTED.* \| (?:open|PROPOSED)", text, flags=re.M)
    body = _section(text, heading)
    assert "**Status:" in body or "**Status:**" in text[text.index(heading):]
    entry = text[text.index(heading):]
    for part in ("### What is true today", "### The requested change", "### If it is declined"):
        assert part in entry, part


# --------------------------------------------------------------------------
# every citation an amended document makes resolves, and none is a line number
# --------------------------------------------------------------------------

CHILD_TASKS = REPO / "docs" / "design" / "child-tasks.md"

#: The amended documents, and the child-tasks design, whose lifecycle.py line
#: numbers drifted the same way #647's did.
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
