"""Rules moved out of the tests of tests/unit/scripts, tests/integration and
apps/plugin-intake/tests (part of issue 73).

Each was an assertion that something is ABSENT from a named source file, or
confined to an allowed set, made by reading that file as text inside a test.
The condition, the allowed names and the files are copied from the test it
replaces; a rule reports what the test refused, never less.

  plugin-intake-no-network-client             apps/plugin-intake/tests/test_intake_pinned.py
  ui-copy-says-no-real-zero                   tests/unit/scripts/test_no_real_zero_in_ui_copy.py
  no-workflow-state-writer                    tests/integration/test_e2e_suite_can_fail.py
  caller-mcp-judge-impersonates-nobody        tests/unit/scripts/test_caller_mcp_acceptance.py
  workspace-apply-reads-no-kms                tests/unit/scripts/test_workspace_apply_approval.py
  workspace-apply-stdlib-and-frozen-specsign  tests/unit/scripts/test_workspace_apply_approval.py
  verify-identity-imports-stdlib-only         tests/unit/scripts/test_acceptance_await_own_tenant.py
  iso-probe-says-acceptance-runs-it           tests/unit/scripts/test_iso_accept_wiring.py
  no-model-classes-enabled-flag               tests/unit/scripts/test_docs_describe_what_was_built.py

`strip_comments` and `real_zero_hits` are also what
tests/unit/scripts/test_no_real_zero_in_ui_copy.py runs its planted copy
through, so the scanner the rule uses is the scanner those tests prove.
"""

from __future__ import annotations

import ast
import re

from source_text_rules import Rule

Hits = list[tuple[int, str]]


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _substring_hits(text: str, needle: str, message: str, start: int = 0) -> Hits:
    hits = []
    at = text.find(needle, start)
    while at >= 0:
        hits.append((_line(text, at), message))
        at = text.find(needle, at + 1)
    return hits


# --------------------------------------------------- plugin-intake-no-network-client

_INTAKE_DIR = "apps/plugin-intake/plugin_intake/"
_NETWORK_MODULES = frozenset(
    {"socket", "ssl", "http", "urllib.request", "requests", "httpx", "subprocess", "ctypes"})
_NETWORK_PACKAGES = frozenset({"http", "ssl", "requests", "httpx"})
#: file name -> the forbidden names it alone may import. nonet.py is the one
#: place sockets are closed off; bucket.py is the bucket's one channel, the
#: metadata server and Cloud Storage only, through the sockets nonet leaves it.
_INTAKE_ALLOWED = {
    "nonet.py": frozenset({"socket"}),
    "bucket.py": frozenset({"socket", "ssl", "http.client"}),
}


def _check_intake(path: str, text: str, tree: ast.Module | None) -> Hits:
    rest = path[len(_INTAKE_DIR):]
    if "/" in rest:  # the test globbed `plugin_intake/*.py`, not deeper
        return []
    if tree is None:
        return [(1, "does not parse, so its imports cannot be shown network-free")]
    name_of_file = rest
    hits = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name in _INTAKE_ALLOWED.get(name_of_file, ()):
                continue
            if name in _NETWORK_MODULES or name.split(".")[0] in _NETWORK_PACKAGES:
                hits.append((node.lineno, f"imports {name}: the intake job opens no network "
                                          "client (only nonet.py and bucket.py's named ones)"))
    return hits


# --------------------------------------------------------- ui-copy-says-no-real-zero

UI_SRC = "apps/swarm-ui/src/"
PHRASE = re.compile(r"real\s+zero", re.IGNORECASE)


def strip_comments(text: str, *, css: bool = False) -> str:
    """Return ``text`` with every comment replaced by spaces, newlines kept.

    Line numbers survive because only non-newline characters are blanked.
    Strings and template literals are tracked so a `//` or `/*` inside one is
    not taken for a comment; a template's `${...}` is code again, and may hold
    its own strings and templates, so the state is a stack.
    """
    out = list(text)
    i, n = 0, len(text)
    # Each frame: ("code", brace_depth) or ("tpl", 0).
    stack: list[list] = [["code", 0]]

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        mode = stack[-1][0]
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if mode == "tpl":
            if c == "\\":
                i += 2
            elif c == "`":
                stack.pop()
                i += 1
            elif c == "$" and nxt == "{":
                stack.append(["code", 0])
                i += 2
            else:
                i += 1
            continue
        # code
        if c == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c == "/" and nxt == "/" and not css:
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c in "'\"":
            # A quote never runs past its line: an apostrophe in JSX text
            # costs at most the rest of that line.
            j = i + 1
            while j < n and text[j] not in (c, "\n"):
                j += 2 if text[j] == "\\" else 1
            i = j + 1
        elif c == "`" and not css:
            stack.append(["tpl", 0])
            i += 1
        elif c == "{":
            stack[-1][1] += 1
            i += 1
        elif c == "}":
            if stack[-1][1] == 0 and len(stack) > 1:
                stack.pop()  # the end of a template's ${...}
            else:
                stack[-1][1] -= 1
            i += 1
        else:
            i += 1
    return "".join(out)


def real_zero_hits(text: str, *, css: bool = False) -> list[tuple[int, str]]:
    """(line, the source line stripped) for each "real zero" a user can be shown."""
    shown = strip_comments(text, css=css)
    lines = text.splitlines()
    found = []
    for m in PHRASE.finditer(shown):
        line = _line(shown, m.start())
        found.append((line, lines[line - 1].strip()))
    return found


def _check_real_zero(path: str, text: str, tree: ast.Module | None) -> Hits:
    if "__tests__" in path[len(UI_SRC):].split("/"):
        return []  # they assert the old words are gone, so they name them
    return [(line, f'user-visible copy says "real zero": {shown}')
            for line, shown in real_zero_hits(text, css=path.endswith(".css"))]


# ----------------------------------------------------------- no-workflow-state-writer

def _check_workflow_state(path: str, text: str, tree: ast.Module | None) -> Hits:
    if "def create_workflow" not in text:
        return [(1, "store.py defines no create_workflow: this rule no longer reads "
                    "the code that writes workflows")]
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        if "WORKFLOWS" in line and (".update(" in line or ".set(" in line) and '"state"' in line:
            hits.append((number, "something now writes a workflow's state; scripts/e2e-test.sh's "
                                 "workflow check should start passing, and "
                                 "tests/integration/test_e2e_suite_can_fail.py should assert "
                                 "the new behaviour rather than its absence"))
    return hits


# ------------------------------------------------ caller-mcp-judge-impersonates-nobody

def _check_judge_token(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = _substring_hits(text, "generateIdToken",
                           "the judge mints its own ID token from the metadata server; "
                           "it never calls generateIdToken")
    head, sep, _ = text.partition("def mint_id_token")
    if not sep:
        return hits + [(1, "the judge defines no mint_id_token")]
    return hits + _substring_hits(
        text, "serviceAccountOpenIdTokenCreator",
        "mint_id_token and what follows it need no serviceAccountOpenIdTokenCreator grant",
        start=len(head))


# ---------------------------------------------------- workspace-apply-reads-no-kms

def _check_no_kms(path: str, text: str, tree: ast.Module | None) -> Hits:
    message = ("the job verifies against SPEC_VERIFY_KEYS, never Cloud KMS at run time "
               "(owner decision 2026-10-19)")
    return _substring_hits(text, "cloudkms", message) + _substring_hits(text, "publicKey", message)


# ---------------------------------------------- workspace-apply-stdlib-and-frozen-specsign

_ENTRY_IMPORT = re.compile(r"^(?:from|import) ([a-z0-9_.]+)", re.M)
_ENTRY_ALLOWED = frozenset({
    "__future__", "base64", "hashlib", "json", "os", "re", "sys", "tempfile", "urllib",
    "collections", "datetime", "pathlib", "typing", "swarm_common"})
_ENTRY_SPECSIGN = re.compile(r"^from swarm_common import specsign\b", re.M)


def _check_entry_imports(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = [(_line(text, m.start()),
             f"imports {m.group(1)}: the image's Debian python3 has nothing installed, so "
             "entry.py imports the standard library and swarm_common only")
            for m in _ENTRY_IMPORT.finditer(text) if m.group(1).split(".")[0] not in _ENTRY_ALLOWED]
    if not _ENTRY_SPECSIGN.search(text):
        hits.append((1, "entry.py no longer imports the frozen swarm_common.specsign: the "
                        "canonical bytes and the digest must be the contract's"))
    for call in ("specsign.jcs(", "specsign.spec_digest("):
        if call not in text:
            hits.append((1, f"entry.py no longer calls {call[:-1]}: the canonical bytes and "
                            "the digest must be the frozen contract's"))
    hits += _substring_hits(text, "json.dumps(payload",
                            "a second RFC 8785 spelling beside the frozen one")
    return hits


# ---------------------------------------------- verify-identity-imports-stdlib-only

_IDENTITY_IMPORT = re.compile(r"^(?:from|import) (\S+)", re.M)
_IDENTITY_ALLOWED = frozenset({"__future__", "hashlib", "re", "collections.abc", "dataclasses"})


def _check_identity_imports(path: str, text: str, tree: ast.Module | None) -> Hits:
    return [(_line(text, m.start()),
             f"imports {m.group(1)}: the swarm-verify image carries identity.py alone, so it "
             "imports only the standard library it already names")
            for m in _IDENTITY_IMPORT.finditer(text) if m.group(1) not in _IDENTITY_ALLOWED]


# ---------------------------------------------- iso-probe-says-acceptance-runs-it

def _check_probe_wording(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _substring_hits(text, "does not ask today",
                           "acceptance runs the probe on every run (owner decision 2026-10-20); "
                           "the probe must not say it does not ask")


# --------------------------------------------------- no-model-classes-enabled-flag

_FLAG = "MODEL_CLASSES" + "_ENABLED"  # split, so this module is no match itself


def _check_no_model_classes_flag(path: str, text: str, tree: ast.Module | None) -> Hits:
    if "/node_modules/" in f"/{path}":
        return []
    return _substring_hits(text, _FLAG, "model classes ship with no feature flag (R4); "
                                        "nothing under apps/ or terraform/ reads one")


# ------------------------------------------------------------------------ rules

_IMP = "imp" + "ort"  # split, so the samples below read as no import of this file
_RZ = "real" + " zero"
_GEN = "generate" + "IdToken"
_CREATOR = "serviceAccount" + "OpenIdTokenCreator"
_KMS = "cloud" + "kms"
_ASK = "does not " + "ask today"
_ENTRY = "images/workspace-apply/entry.py"
_IDENTITY = "apps/common/swarm_common/identity.py"
_JUDGE = "tests/acceptance/test_caller_mcp.py"
_PROBE = "tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py"
_STORE = "apps/swarm-api/swarm_api/store.py"
_CALLS = "b = specsign.jcs(x)\nd = specsign.spec_digest(x)\n"

RULES = (
    Rule(
        id="plugin-intake-no-network-client",
        why="the plugin intake job runs with no network: only nonet.py may import socket and "
            "only bucket.py socket, ssl and http.client (apps/plugin-intake)",
        roots=(_INTAKE_DIR.rstrip("/"),),
        suffixes=(".py",),
        check=_check_intake,
        bad=(
            (_INTAKE_DIR + "intake.py", f"{_IMP} socket\n"),
            (_INTAKE_DIR + "archive.py", f"from urllib.request {_IMP} urlopen\n"),
            (_INTAKE_DIR + "tree.py", f"def f():\n    {_IMP} http.client\n"),
            (_INTAKE_DIR + "nonet.py", f"{_IMP} socket\n{_IMP} ssl\n"),
            (_INTAKE_DIR + "bucket.py", f"{_IMP} requests\n"),
            (_INTAKE_DIR + "main.py", f"{_IMP} subprocess\n"),
            (_INTAKE_DIR + "limits.py", f"{_IMP} ctypes\n"),
            (_INTAKE_DIR + "refusals.py", "x = (\n"),
        ),
        good=(
            (_INTAKE_DIR + "nonet.py", f"{_IMP} socket\n"),
            (_INTAKE_DIR + "bucket.py", f"{_IMP} socket\n{_IMP} ssl\n{_IMP} http.client\n"),
            (_INTAKE_DIR + "intake.py", f"{_IMP} json\nfrom urllib.parse {_IMP} quote\n"),
            (_INTAKE_DIR + "sub/deeper.py", f"{_IMP} socket\n"),
        ),
    ),
    Rule(
        id="ui-copy-says-no-real-zero",
        why="owner decision 2026-10-11: the console never says \"real zero\" to a user; "
            "the mark reads `none` and a sentence says what happened",
        roots=(UI_SRC.rstrip("/"),),
        suffixes=(".ts", ".tsx", ".css"),
        requires=re.compile(r"real\s+zero", re.IGNORECASE),
        check=_check_real_zero,
        bad=(
            (UI_SRC + "primitives.tsx", f"export const MARK = {{ zero: '{_RZ.title()}' }}\n"),
            (UI_SRC + "help.ts", f'export const H = "the read came back a {_RZ}"\n'),
            (UI_SRC + "A.tsx", "export const A = () => <p>No pools · real\n  zero</p>\n"),
            (UI_SRC + "x.css", f".a::after {{ content: '{_RZ}'; }}\n"),
        ),
        good=(
            (UI_SRC + "help.ts", f"// a {_RZ}, the design term\nexport const H = 'none'\n"),
            (UI_SRC + "x.css", f"/* .is-zero  a {_RZ} */\n.a {{ color: red; }}\n"),
            (UI_SRC + "__tests__/a.test.tsx", f"expect(x).not.toContain('{_RZ}')\n"),
            (UI_SRC + "B.tsx", "export const cls = 'real-zero'\n"),
        ),
    ),
    Rule(
        id="no-workflow-state-writer",
        why="scripts/e2e-test.sh's workflow check fails by design while nothing advances a "
            "workflow's state; the absence is the finding (tests/integration/"
            "test_e2e_suite_can_fail.py)",
        roots=(_STORE,),
        suffixes=(".py",),
        check=_check_workflow_state,
        bad=(
            (_STORE, "def create_workflow(self):\n"
                     "    self._db.collection(WORKFLOWS).document(i).update({\"state\": s})\n"),
            (_STORE, "def create_workflow(self):\n"
                     "    db.collection(WORKFLOWS).document(i).set({\"state\": \"done\"})\n"),
            (_STORE, "WORKFLOWS = 'workflows'\n"),
        ),
        good=(
            (_STORE, "def create_workflow(self):\n"
                     "    db.collection(WORKFLOWS).document(i).set({\"spec\": spec})\n"
                     "    ref.update({\"state\": s})\n"),
        ),
    ),
    Rule(
        id="caller-mcp-judge-impersonates-nobody",
        why="owner decision 2026-10-18 (archived issue 1433): the suite's MCP credential is its "
            "own ID token from the metadata server, nothing impersonated",
        roots=(_JUDGE,),
        suffixes=(".py",),
        check=_check_judge_token,
        bad=(
            (_JUDGE, f"URL = '{_GEN}'\ndef mint_id_token():\n    pass\n"),
            (_JUDGE, f"def mint_id_token():\n    role = '{_CREATOR}'\n"),
            (_JUDGE, "def mint():\n    pass\n"),
        ),
        good=(
            (_JUDGE, f'"""No {_CREATOR} grant is needed."""\n'
                     "def mint_id_token():\n    return fetch(METADATA_IDENTITY_URL)\n"),
        ),
    ),
    Rule(
        id="workspace-apply-reads-no-kms",
        why="owner decision 2026-10-19 (archived issue 847, WK-APPROVAL-KEYS): the workspace job "
            "verifies approvals against SPEC_VERIFY_KEYS, never Cloud KMS at run time",
        roots=(_ENTRY,),
        suffixes=(".py",),
        check=_check_no_kms,
        bad=(
            (_ENTRY, f"URL = 'https://{_KMS}.googleapis.com/v1/'\n"),
            (_ENTRY, "key = response['public" + "Key']\n"),
        ),
        good=(
            (_ENTRY, "KEYS = os.environ['SPEC_VERIFY_KEYS']\n"),
        ),
    ),
    Rule(
        id="workspace-apply-stdlib-and-frozen-specsign",
        why="the workspace-apply image's Debian python3 has nothing installed: entry.py imports "
            "the standard library and the frozen swarm_common.specsign only, and spells RFC 8785 "
            "through specsign alone (archived issue 847)",
        roots=(_ENTRY,),
        suffixes=(".py",),
        check=_check_entry_imports,
        bad=(
            (_ENTRY, f"{_IMP} requests\nfrom swarm_common {_IMP} specsign\n" + _CALLS),
            (_ENTRY, f"from google.cloud {_IMP} kms\nfrom swarm_common {_IMP} specsign\n" + _CALLS),
            (_ENTRY, f"{_IMP} json\n" + _CALLS),
            (_ENTRY, f"from swarm_common {_IMP} specsign\nb = specsign.jcs(x)\n"),
            (_ENTRY, f"from swarm_common {_IMP} specsign\n" + _CALLS + "b = json.dumps(payload)\n"),
        ),
        good=(
            (_ENTRY, f"from __future__ {_IMP} annotations\n{_IMP} urllib.request\n"
                     f"from collections.abc {_IMP} Mapping\nfrom swarm_common {_IMP} specsign\n"
                     + _CALLS),
        ),
    ),
    Rule(
        id="verify-identity-imports-stdlib-only",
        why="owner decision 2026-10-19 (archived issue 1436): the swarm-verify image carries "
            "identity.py alone, so it imports only the standard library",
        roots=(_IDENTITY,),
        suffixes=(".py",),
        check=_check_identity_imports,
        bad=(
            (_IDENTITY, f"{_IMP} hashlib\n{_IMP} json\n"),
            (_IDENTITY, f"from swarm_common.models {_IMP} Tenant\n"),
            (_IDENTITY, f"from collections {_IMP} OrderedDict\n"),
        ),
        good=(
            (_IDENTITY, f"from __future__ {_IMP} annotations\n{_IMP} hashlib\n{_IMP} re\n"
                        f"from collections.abc {_IMP} Iterable\nfrom dataclasses {_IMP} dataclass\n"),
        ),
    ),
    Rule(
        id="iso-probe-says-acceptance-runs-it",
        why="owner decision 2026-10-20 (archived issue 1457): acceptance runs the cross-tenant "
            "probe on every run, so the probe must not say acceptance does not ask",
        roots=(_PROBE,),
        suffixes=(".py",),
        check=_check_probe_wording,
        bad=(
            (_PROBE, f'"""The acceptance workflow {_ASK}."""\n'),
        ),
        good=(
            (_PROBE, '"""accept.yml\'s `acceptance (tenant-isolation)` job runs it."""\n'),
        ),
    ),
    Rule(
        id="no-model-classes-enabled-flag",
        why="R4: model classes ship with no feature flag, so nothing under apps/ or "
            "terraform/ reads one (was test_docs_describe_what_was_built)",
        roots=("apps", "terraform"),
        suffixes=(".py", ".tf", ".ts", ".tsx"),
        requires=_FLAG,
        check=_check_no_model_classes_flag,
        bad=(
            ("apps/scheduler/scheduler/dispatch.py", f'ON = os.environ.get("{_FLAG}")\n'),
            ("terraform/infra/variables.tf", f'locals {{\n  on = "{_FLAG}"\n}}\n'),
        ),
        good=(
            ("apps/scheduler/scheduler/dispatch.py", 'CLASSES = ("haiku", "sonnet")\n'),
        ),
    ),
)
