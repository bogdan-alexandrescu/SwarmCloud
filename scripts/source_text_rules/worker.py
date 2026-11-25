"""Rules over the agent worker's source and the images it ships (part of issue 73).

Each rule here was a sweep in a test under tests/unit/worker, moved so the
tree is read once per run; the test file that held it names the rule id where
the sweep was.

worker-no-dead-diff-credential-helper (archived issue 361, was in
test_worker_robustness_findings.py). `_adds_a_credential` and its parser
`_added_by_file` had no caller in the worker: both publish scans run
`_DiffLeakScanner`. A second parser of the same diff, held by tests alone, is
one a fix can miss, so no worker module may define or name either.

forge-token-modules-use-the-one-opener (archived issue 645, part of archived
issue 307, was in test_forge_no_redirect.py). urllib's default opener follows
a redirect with the original headers, `Authorization` included, so every
request carrying the forge token goes through `forge._NO_REDIRECT_OPENER`:
each package's forge.py makes exactly one `build_opener` call, and no module
that imports forge calls `urlopen` or `build_opener` of its own.

issue-reads-github-hosts-from-forge (same issue, same file). issue.py reads
`GITHUB_HOSTS` from forge; a second assignment is a list that can drift.

gate-wait-hook-standard-library-only (CAPTCHA-HANDOFF-2, part of archived
issue 2058, was in test_claude_gate_wait_hook.py). claude-code runs the hook
with `python3 -I`, which drops the package's path: an import from the worker
would fail every call, silently (the hook fails open), and the agent would
never be told a person was asked.

repo-index-imports-only-its-pins (was in test_repo_index_extract.py). The
indexer image installs its tree-sitter pins with `--require-hashes` into its
own environment; an import outside them fails only in the image. The script
may import its pins, the LSP pass (RI10) and the shard writer (KG2) shipped
beside it; the writer and the LSP package are standard library only.

merge-no-not-mergeable-code (was in test_merge_action.py). `from_fork` and
`merge_conflict` were folded into `pull_request_not_this_workflows` and
`not_mergeable`, then split out again as codes of their own; nothing in
merge.py ends on `not_mergeable` any more, so a caller reading the code learns
which refusal it was.

render-takes-name-class-from-identity (was in test_kubernetes_manifests.py).
The reconciler reverse-maps a Job label by re-slugging it with
swarm_common.identity's class, so kubernetes/render.py imports that class and
never compiles its own: `re.compile` caches, so a local copy is the same
object and no identity assertion can tell it apart.

browser-runner-no-script-evaluation (was in test_runners.py). Arbitrary script
execution in a browser holding a tenant's session is an exfiltration
primitive; the browser runner's action list is a closed set.
"""

from __future__ import annotations

import ast
import re
import sys

from source_text_rules import Rule

# Names in the samples are split so this file's own text is no hit for a
# reader grepping the tree for them.
_ADDS = "_adds_a" + "_credential"
_ADDED = "_added" + "_by_file"


# ------------------------------------------- worker-no-dead-diff-credential-helper

#: The test's own pattern, unchanged.
_DEAD_HELPER = re.compile(r"\b_adds_a_credential\b|\b_added_by_file\b")


def _check_dead_helper(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    return [(number, "the dead whole-diff credential helper is named again; the publish "
                     "scans run _DiffLeakScanner (archived issue 361)")
            for number, line in enumerate(text.splitlines(), 1) if _DEAD_HELPER.search(line)]


# ------------------------------------------- forge-token-modules-use-the-one-opener

_FORGE_PACKAGES = ("apps/agent-worker/agent_worker", "apps/swarm-api/swarm_api")


def _imports_forge(tree: ast.AST) -> bool:
    """True when the module imports its package's forge module, in any spelling."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[-1] == "forge" or any(a.name == "forge" for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[-1] == "forge" for a in node.names):
                return True
    return False


def _opener_calls(tree: ast.AST) -> list[tuple[str, int]]:
    """Every call to `urlopen` or `build_opener`, by name and line."""
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("urlopen", "build_opener"):
                calls.append((name, node.lineno))
    return calls


def _check_forge_opener(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return [(1, "does not parse, so its openers cannot be checked")]
    if any(path == f"{package}/forge.py" for package in _FORGE_PACKAGES):
        calls = _opener_calls(tree)
        if [name for name, _ in calls] != ["build_opener"]:
            return [(calls[0][1] if calls else 1,
                     f"forge.py must build exactly the one opener, found {calls}")]
        return []
    if not _imports_forge(tree):
        return []
    return [(line, f"a token-carrying module opens URLs with its own `{name}`; "
                   "send through forge._NO_REDIRECT_OPENER")
            for name, line in _opener_calls(tree)]


# ------------------------------------------- issue-reads-github-hosts-from-forge

def _check_github_hosts(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return [(1, "does not parse, so GITHUB_HOSTS cannot be checked")]
    return [
        (node.lineno, "issue.py assigns GITHUB_HOSTS; read forge.GITHUB_HOSTS instead")
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(getattr(t, "id", None) == "GITHUB_HOSTS"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
    ]


# ------------------------------------------- gate-wait-hook-standard-library-only

_HOOK = "apps/agent-worker/agent_worker/runners/gate_wait_hook.py"
_HOOK_IMPORTS = frozenset({"json", "os", "re", "stat", "sys", "__future__"})


def _check_hook_imports(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith(("import ", "from ")):
            continue
        name = line.split()[1].split(".")[0]
        if name not in _HOOK_IMPORTS:
            hits.append((number, f"the hook imports {name!r}; it runs under `python3 -I` and "
                                 f"may import only {sorted(_HOOK_IMPORTS)}"))
    return hits


# ------------------------------------------- repo-index-imports-only-its-pins

_TOOL_DIR = "images/agent-runtime-indexer/repo-index"
_SCRIPT = f"{_TOOL_DIR}/repo_index_extract.py"
_WRITER = f"{_TOOL_DIR}/repo_graph_shards.py"
_LSP_GLOB = f"{_TOOL_DIR}/lsp/*.py"
_IMPORT = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z_][\w]*)", re.MULTILINE)
_SCRIPT_ALLOWED = frozenset({
    "tree_sitter", "tree_sitter_python", "tree_sitter_javascript",
    "tree_sitter_typescript", "tree_sitter_go", "tree_sitter_hcl", "__future__",
    # The LSP pass (RI10), shipped beside the script in the image.
    "lsp",
    # The shard writer's tokenizer (KG2), shipped beside the script too.
    "repo_graph_shards",
})


def _check_index_imports(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    allowed = _SCRIPT_ALLOWED if path == _SCRIPT else frozenset({"__future__"})
    return [(text.count("\n", 0, m.start()) + 1,
             f"imports {m.group(1)!r}, outside the standard library and {sorted(allowed)}")
            for m in _IMPORT.finditer(text)
            if m.group(1) not in sys.stdlib_module_names and m.group(1) not in allowed]


# ------------------------------------------- browser-runner-no-script-evaluation

_NO_EVAL = ("page.evaluate", "add_init_script")


def _check_browser_eval(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    return [(number, f"`{needle}` runs arbitrary script in a page holding a tenant's session")
            for number, line in enumerate(text.splitlines(), 1)
            for needle in _NO_EVAL if needle in line]


# ------------------------------------------- merge-no-not-mergeable-code

_MERGE = "apps/agent-worker/agent_worker/merge.py"
_OLD_CODE = '"' + "not_" + 'mergeable"'


def _check_old_code(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    return [(number, "merge.py ends on the folded refusal code `not_mergeable`; use the split "
                     "code that names the refusal")
            for number, line in enumerate(text.splitlines(), 1) if _OLD_CODE in line]


# ------------------------------------------- render-takes-name-class-from-identity

_RENDER = "kubernetes/render.py"
_LOCAL_CLASS = "_NAME_SAFE = re.compile"


def _check_local_class(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    return [(number, "render.py compiles its own name class; import _TENANT_SAFE from "
                     "swarm_common.identity")
            for number, line in enumerate(text.splitlines(), 1) if _LOCAL_CLASS in line]


RULES = (
    Rule(
        id="worker-no-dead-diff-credential-helper",
        why="the dead whole-diff credential helper and its parser stay gone (archived issue 361)",
        roots=("apps/agent-worker/agent_worker",),
        suffixes=(".py",),
        requires=_DEAD_HELPER,
        check=_check_dead_helper,
        bad=(
            ("apps/agent-worker/agent_worker/lifecycle.py", f"def {_ADDS}(diff):\n    return False\n"),
            ("apps/agent-worker/agent_worker/publish_guard.py",
             f"# was {_ADDED}, see the scanner\nx = 1\n"),
        ),
        good=(
            ("apps/agent-worker/agent_worker/lifecycle.py", "def _decodes_as_a_jwt(t):\n    return True\n"),
            ("apps/agent-worker/agent_worker/lifecycle.py", f"x = '{_ADDS}_more'\n"),
        ),
    ),
    Rule(
        id="forge-token-modules-use-the-one-opener",
        why="every request carrying the forge token goes through forge's one no-redirect "
            "opener (archived issue 645, part of archived issue 307)",
        roots=_FORGE_PACKAGES,
        suffixes=(".py",),
        # A hit is an opener call, so a file naming neither call cannot be
        # one -- save a forge.py that lost its opener, which still names
        # `_NO_REDIRECT_OPENER` (its behaviour tests, against real servers,
        # fail too). Most of the two packages is then never parsed.
        requires=re.compile(r"urlopen|build_opener|_NO_REDIRECT_OPENER"),
        check=_check_forge_opener,
        bad=(
            ("apps/agent-worker/agent_worker/issue.py",
             "import urllib.request\nfrom agent_worker import forge\n\n"
             "def f(r):\n    return urllib.request.urlopen(r)\n"),
            ("apps/swarm-api/swarm_api/x.py",
             "from .forge import GITHUB_HOSTS\nimport urllib.request as u\n"
             "OPENER = u.build_opener()\n"),
            ("apps/agent-worker/agent_worker/forge.py",
             "import urllib.request\nA = urllib.request.build_opener()\n"
             "B = urllib.request.build_opener()\n"),
            ("apps/agent-worker/agent_worker/forge.py",
             "import urllib.request\n_NO_REDIRECT_OPENER = None\n"),
        ),
        good=(
            ("apps/agent-worker/agent_worker/forge.py",
             "import urllib.request\n_NO_REDIRECT_OPENER = urllib.request.build_opener()\n"),
            ("apps/agent-worker/agent_worker/issue.py",
             "from agent_worker import forge\n\ndef f(r):\n    return forge._NO_REDIRECT_OPENER.open(r)\n"),
            # Not a forge module: its own opener is not this rule's.
            ("apps/agent-worker/agent_worker/other.py",
             "import urllib.request\n# forge is not imported here\nurllib.request.urlopen('x')\n"),
        ),
    ),
    Rule(
        id="issue-reads-github-hosts-from-forge",
        why="issue.py reads GITHUB_HOSTS from forge rather than restating it "
            "(archived issue 645, part of archived issue 307)",
        roots=("apps/agent-worker/agent_worker/issue.py",),
        suffixes=(".py",),
        requires="GITHUB_HOSTS",
        check=_check_github_hosts,
        bad=(
            ("apps/agent-worker/agent_worker/issue.py",
             "GITHUB_HOSTS = frozenset({'github.com'})\n"),
            ("apps/agent-worker/agent_worker/issue.py",
             "GITHUB_HOSTS: frozenset[str] = frozenset()\n"),
        ),
        good=(
            ("apps/agent-worker/agent_worker/issue.py",
             "from agent_worker.forge import GITHUB_HOSTS\n\nHOSTS = GITHUB_HOSTS\n"),
        ),
    ),
    Rule(
        id="gate-wait-hook-standard-library-only",
        why="the gate-wait hook runs under `python3 -I` and fails open, so a worker import "
            "silently disables it (CAPTCHA-HANDOFF-2, part of archived issue 2058)",
        roots=(_HOOK,),
        suffixes=(".py",),
        check=_check_hook_imports,
        bad=(
            (_HOOK, "from __future__ import annotations\nfrom agent_worker import handoff\n"),
            (_HOOK, "import json\nimport pathlib\n"),
            (_HOOK, "from . import handoff\n"),
        ),
        good=(
            (_HOOK, "from __future__ import annotations\n\nimport json\nimport os\nimport os.path\n"
                    "import re\nimport stat\nimport sys\n\ndef f():\n    import pathlib\n"),
        ),
    ),
    Rule(
        id="repo-index-imports-only-its-pins",
        why="the indexer image installs only its hashed pins; the script imports nothing "
            "else and its shard writer and LSP package are standard library only",
        roots=(_SCRIPT, _WRITER, _LSP_GLOB),
        suffixes=(".py",),
        check=_check_index_imports,
        bad=(
            (_SCRIPT, "import json\nimport requests\n"),
            (_WRITER, "from __future__ import annotations\nimport tree_sitter\n"),
            (f"{_TOOL_DIR}/lsp/client.py", "import json\n    from yaml import safe_load\n"),
        ),
        good=(
            (_SCRIPT, "from __future__ import annotations\nimport json\nimport tree_sitter\n"
                      "import tree_sitter_hcl\nfrom lsp import driver\nimport repo_graph_shards\n"),
            (_WRITER, "from __future__ import annotations\nimport hashlib\nimport json\n"),
            (f"{_TOOL_DIR}/lsp/servers.py", "import subprocess\nfrom pathlib import Path\n"),
        ),
    ),
    Rule(
        id="merge-no-not-mergeable-code",
        why="merge.py never ends on the folded refusal code `not_mergeable`; each refusal "
            "has its own code",
        roots=(_MERGE,),
        suffixes=(".py",),
        requires=_OLD_CODE,
        check=_check_old_code,
        bad=((_MERGE, f"def refuse():\n    return Refusal({_OLD_CODE})\n"),),
        good=((_MERGE, 'def refuse():\n    return Refusal("merge_conflict")\n'
                       "# not_mergeable, unquoted, is prose\n"),),
    ),
    Rule(
        id="render-takes-name-class-from-identity",
        why="kubernetes/render.py slugs names with swarm_common.identity's class, which the "
            "reconciler re-slugs with, never a local copy",
        roots=(_RENDER,),
        suffixes=(".py",),
        requires=_LOCAL_CLASS,
        check=_check_local_class,
        bad=((_RENDER, 'import re\n_NAME_SAFE = re.compile(r"[^a-z0-9-]+")\n'),),
        good=((_RENDER, "from swarm_common.identity import _TENANT_SAFE as _NAME_SAFE\n"),),
    ),
    Rule(
        id="browser-runner-no-script-evaluation",
        why="the browser runner never evaluates script in a page holding a tenant's session; "
            "its action list is a closed set",
        roots=("apps/agent-worker/agent_worker/runners/browser.py",),
        suffixes=(".py",),
        check=_check_browser_eval,
        bad=(
            ("apps/agent-worker/agent_worker/runners/browser.py",
             "def act(page, js):\n    return page.evaluate(js)\n"),
            ("apps/agent-worker/agent_worker/runners/browser.py",
             "context.add_init_script('x')\n"),
        ),
        good=(
            ("apps/agent-worker/agent_worker/runners/browser.py",
             "def act(page, url):\n    page.goto(url)\n    return page.inner_text('body')\n"),
        ),
    ),
)
