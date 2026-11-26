"""Rules moved out of tests/unit/control_plane/test_[n-z]*.py (part of issue 73).

Each rule below was a test that walked swarm-api, the worker or the console's
source and asserted a pattern ABSENT, or confined to named files or functions.
The condition is copied from the test it replaces (named in the rule's `why`),
so the rule refuses everything that test refused. Where the test also held
that each allowed writer or caller still EXISTS, a path-level allowance is the
rule's `exempt` (a stale one fails the check); a function-level allowance is
checked inside the rule, against the file that names it.

Sample paths are built from the rule's own root; a token a rule forbids is
split in this file wherever spelling it whole would make this module a match
for another rule's root (it is not under any of them, but a copy might be).
"""

from __future__ import annotations

import ast
import re

from source_text_rules import Rule

API = "apps/swarm-api/swarm_api"
UI = "apps/swarm-ui/src"


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _first_line(text: str, needle: str) -> int:
    at = text.find(needle)
    return _line_of(text, at) if at >= 0 else 1


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_of(path: str) -> str:
    """apps/swarm-api/swarm_api/routes/people.py -> swarm_api.routes.people."""
    rel = path.removeprefix("apps/swarm-api/").removesuffix(".py")
    return rel.replace("/", ".").removesuffix(".__init__")


def _top_level(path: str, suffix: str) -> bool:
    """A file directly in apps/swarm-ui/src (what `UI.glob("*.tsx")` reads)."""
    rest = path.removeprefix(UI + "/")
    return rest != path and "/" not in rest and rest.endswith(suffix)


# ------------------------------------------------- test_no_route_serves_credentials

#: Every module that reads a Secret Manager PAYLOAD, and what it does with it:
#: SECRET_PAYLOAD_READERS of the test this replaces. The release serves one,
#: and so does the QA target issue, to its fenced attempt only; the others use
#: the value inside swarm-api.
SECRET_PAYLOAD_READERS = frozenset(f"{API}/{p}" for p in (
    # The release: the carve-out, and the only reader that answers with it.
    "routes/attempts_secrets.py",
    # The platform's search key, sent to the provider and scrubbed from every answer.
    "search.py",
    # The forge App's private key, used to mint installation tokens.
    "forgeapp.py",
    # A tenant's forge token, sent to GitHub for issue previews and writes.
    "forge.py",
    # A QA target's test credential (docs/design/visual-qa-any-app.md §2.1,
    # §2.2): read to merge a new one in or drop a deleted target's, and served
    # only to the worker of a proven, fenced attempt whose run names the target,
    # at qatargets.ISSUE_ROUTE, which main.UNCAPTURED_ROUTES keeps out of capture.
    "qatargets.py",
    # The Desktop OAuth client's secret, read once at start and served in the
    # unauthenticated discovery document ONLY when DISCOVERY_SERVE_CLIENT_SECRET
    # is true: the owner's Q1 decision (docs/design/day-one-plan.md §6.3; a
    # Desktop client's secret is classified non-confidential). A separate
    # app, swarm-discovery, never mounted on swarm-api's routes.
    "discovery.py",
    # A workspace's TypeSafe key, read only when the workspace chose the
    # TypeSafe Jev backend and opted in to the contract review at submission
    # (jev-contract.md section 4), and sent only as the bearer of that one
    # call; the client's sanitizer drops it from the state, and no answer,
    # record or log carries it.
    "contractreview.py",
    # gittools.py read the `-git-wf` slot until archived issue 2029 removed it:
    # a workflow-touching push now uses an App installation token minted per
    # push, never a stored secret. MUTATION: keep the slot reader and
    # gittools.py is a hit of this rule.
))
_PAYLOAD_CALL = "access_secret" + "_version"
_READER_MAKER = "SecretManager" + "Reader("


def _check_payload_readers(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if _PAYLOAD_CALL not in text:
        return []
    return [(_first_line(text, _PAYLOAD_CALL),
             "reads a Secret Manager payload; name the module in SECRET_PAYLOAD_READERS "
             "with what it does with the value, and never serve one outside "
             "CREDENTIAL_SERVING_ROUTES")]


def _check_reader_maker(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if _READER_MAKER not in text:
        return []
    return [(_first_line(text, _READER_MAKER),
             "builds its own secret reader; only routes/attempts_secrets.py may, so the "
             "route walk in test_no_route_serves_credentials.py sees every route that serves one")]


# ------------------------------------------------------------- test_plugin_bundles

_BUILD_WORDS = ("cloudbuild" + ".googleapis.com", "gcr.io/" + "cloud-builders", "Cloud" + "Build")


def _check_no_cloud_build(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    if tree is not None:
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            if any("cloudbuild" in n or "cloud_build" in n for n in names):
                hits.append((node.lineno, "imports a Cloud Build client; a bundle build is a "
                                          "Cloud Run job"))
    for word in _BUILD_WORDS:
        if word in text:
            hits.append((_first_line(text, word), f"names {word}; a bundle build is a Cloud Run job"))
    return hits


# --------------------------------------------------------- test_probe_admin_routes

#: The functions that may call `probe_admin_tenants` (module, function):
#: TENANTS_READERS of the test this replaces.
TENANTS_READERS = frozenset({
    ("swarm_api.roles", "probe_admin_scope"),
    ("swarm_api.routes.people", "administered_workspaces"),
    ("swarm_api.routes.people", "people_scope"),
    ("swarm_api.routes.people", "join_scope"),
})


def _calls_in_functions(tree: ast.AST) -> list[tuple[ast.FunctionDef, ast.Call]]:
    """Every call with its innermost enclosing function; a module-level call
    has none and is not listed (the test's `_calls`, unchanged)."""
    found: list[tuple[ast.FunctionDef, ast.Call]] = []

    def visit(node: ast.AST, fn: ast.FunctionDef | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Call) and inner is not None:
                found.append((inner, child))
            visit(child, inner)

    visit(tree, None)
    return found


def _check_probe_admin_tenants(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return []
    module = _module_of(path)
    readers = {(module, fn.name): call for fn, call in _calls_in_functions(tree)
               if _call_name(call.func) == "probe_admin_tenants"}
    hits = [(call.lineno, f"{m}.{f} reads probe_admin_tenants; only the probe admin "
                          "list's routes may (owner decision 2026-11-20)")
            for (m, f), call in readers.items() if (m, f) not in TENANTS_READERS]
    # The test's control, per module that hosts a listed reader: the
    # enumeration still finds one, so the allowance is not a list of nothing.
    if any(m == module for m, _ in TENANTS_READERS) and not set(readers) & TENANTS_READERS:
        hits.append((1, "no listed function here calls probe_admin_tenants any more; the "
                        "enumeration reads nothing: update TENANTS_READERS"))
    return hits


# -------------------------------------------------------- test_settings_mac_config

_MAC_READERS = ("JudgmentSettings", "MergeSettingsStore", "CodeIntelligenceStore")


def _check_settings_mac(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", None)
        if name not in _MAC_READERS:
            continue
        mac = [k.value for k in node.keywords if k.arg == "mac"]
        if not mac:
            hits.append((node.lineno, f"builds {name} without the settings' mac"))
        elif not (isinstance(mac[0], ast.Call)
                  and getattr(mac[0].func, "id", None) == "settings_mac"):
            hits.append((node.lineno, f"builds {name} with a mac not built from its settings"))
    return hits


# ------------------------------------------------- test_spec_signing_every_submitter

#: (path under apps/, enclosing qualname) that may create a task document:
#: WRITERS_OF_A_NEW_TASK of the test this replaces.
WRITERS_OF_A_NEW_TASK = frozenset({
    ("swarm-api/swarm_api/store.py", "Store.create_tasks"),
    ("swarm-api/swarm_api/store.py", "Store.create_workflow"),
    ("swarm-api/swarm_api/service.py", "SubmissionService.submit_tasks"),
    ("swarm-api/swarm_api/service.py", "SubmissionService.submit_workflow"),
    ("swarm-api/swarm_api/children.py", "ChildService.submit._apply"),
    # A step re-run's new tasks (archived pull request 3123), signed by
    # `steprerun.rerun` before the store call.
    ("swarm-api/swarm_api/store.py", "Store.rerun_workflow_steps._apply"),
})
_CREATING_CALLS = frozenset({"task_to_firestore", "create_tasks", "create_workflow"})
#: A file is parsed for the rule when it may call one, or when it defines a
#: class whose method is a known writer (so a writer that stopped creating
#: is a hit in its own file).
_MAY_CREATE = re.compile(
    r"\b(?:task_to_firestore|create_tasks|create_workflow)\b"
    r"|^class (?:Store|SubmissionService|ChildService)\b", re.MULTILINE)


class _QualnameCalls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.found: list[tuple[str, str, int]] = []

    def _scoped(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name in _CREATING_CALLS:
            self.found.append((".".join(self.stack), name, node.lineno))
        self.generic_visit(node)


def _check_task_writers(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    rel = path.removeprefix("apps/")
    known = {q for r, q in WRITERS_OF_A_NEW_TASK if r == rel}
    if tree is None or "/node_modules/" in path or "/.venv/" in path:
        return []
    scan = _QualnameCalls()
    scan.visit(tree)
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for qualname, name, line in scan.found:
        if qualname == "" or (rel, qualname) == ("swarm-api/swarm_api/codec.py", "task_to_firestore"):
            continue  # an import or the definition's own module scope
        if qualname in known:
            seen.add(qualname)
        else:
            hits.append((line, f"{qualname} calls {name}: a task document is created outside "
                               "the signing paths; route it through SubmissionService."
                               "submit_tasks/submit_workflow, or the worker refuses it as unsigned"))
    for qualname in sorted(known - seen):
        hits.append((1, f"a known writer moved: {qualname} no longer creates a task document "
                        "here; update WRITERS_OF_A_NEW_TASK"))
    return hits


# ----------------------------------------------------------- test_platform_secrets

_VALUE_READS = ("access_secret" + "_version", "access", "payload", "add_secret" + "_version",
                "create_secret", "delete_secret", "set_iam_policy", "list_secrets")


def _check_platform_secrets(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return [(1, "platform_secrets.py does not parse; its syntax tree cannot be checked")]
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        word = node.attr if isinstance(node, ast.Attribute) else \
            node.id if isinstance(node, ast.Name) else None
        if word in _VALUE_READS:
            hits.append((node.lineno, f"names {word}; the status reader calls get_secret and "
                                      "list_secret_versions and nothing else"))
    return hits


# ------------------------------------------------------------ test_tenant_isolation

def _check_raw_tenant_id(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if tree is None:
        return []
    return [
        (node.lineno, "take the tenant from the tenant_scope dependency, not from auth: two "
                      "verified principals can derive the same tenant_id")
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "tenant_id"
        and isinstance(node.value, ast.Name) and node.value.id == "auth"
    ]


# --------------------------------------------------- test_stop_and_output_ui_surface

def _strip_ts_comments(text: str) -> str:
    """Block comments, and lines that START with `//` (the test's `code()`)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


# `write()` takes the value `route()` builds, never a bare string (CH-18,
# swarm-ui fetch.ts), so the target is `route('/v1/tasks/{id}/cancel', ...)`;
# a bare template literal is still accepted in case one comes back. The
# workflow's cancel route is a second ROUTE, not a second caller of the task's
# (Workflows V2, 2026-10-01): each is counted on its own, and a third /cancel
# write still fails the total.
_CANCEL_WRITE = re.compile(r"write\(\s*(?:route\(\s*)?['\"`][^'\"`]*?/cancel['\"`]")


def _check_cancel_writes(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if path != f"{UI}/api.ts":
        return []
    writes = _CANCEL_WRITE.findall(_strip_ts_comments(text))
    tasks = [w for w in writes if "/v1/tasks/" in w]
    flows = [w for w in writes if "/v1/workflows/" in w]
    hits: list[tuple[int, str]] = []
    if len(tasks) != 1:
        hits.append((1, f"{len(tasks)} write() calls target the task /cancel path; the cancel "
                        "route has exactly one client function"))
    if len(flows) != 1:
        hits.append((1, f"{len(flows)} write() calls target the workflow /cancel path; it has "
                        "exactly one client function"))
    if len(writes) != 2:
        hits.append((1, f"a /cancel route not named here is written to: {writes}"))
    return hits


def _confined_caller(token: str, owner: str):
    def check(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
        if not _top_level(path, ".tsx") or token not in _strip_ts_comments(text):
            return []
        return [(_first_line(text, token),
                 f"{token} is used here; it must be used only from {owner}, which owns the "
                 "confirmation")]
    return check


# ------------------------------------------------------ test_workflow_step_measurements

_UTIL_TRACK = re.compile(r"""className=[{"'`][^>]*\bctl-util-track\b""")


def _check_util_track(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    rest = path.removeprefix(UI + "/")
    if not (_top_level(path, ".tsx") or (rest.startswith("components/")
                                         and "/" not in rest.removeprefix("components/"))):
        return []
    found = _UTIL_TRACK.search(text)
    if found is None:
        return []
    return [(_line_of(text, found.start()),
             "draws a utilisation track outside the shared primitive (components/UsageBar.tsx); "
             "each copy is a place the hatch, the baseline tick or the over-ceiling segment "
             "can be lost")]


# ------------------------------------------------------------- test_outcomes_end_cause

def _check_outcomes_names_no_profile(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    hits = []
    for literal in ('"mock"', "'mock'"):
        if literal in text:
            hits.append((_first_line(text, literal),
                         "swarm_api/outcomes.py names a profile; DECLARED_COST_PROFILES must "
                         "come from RunnerProfile.cost_declared"))
    return hits


# ------------------------------------------------------------ test_typesafe_ticket_api

_TICKET_SECRET_READS = ("access_secret" + "_version", "SecretManager" + "ServiceClient", ".access(")


def _check_ticket_reads_no_secret(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    return [(_first_line(text, call), f"names {call}; the ticket names a secret and swarm-api "
                                       "never reads one for it")
            for call in _TICKET_SECRET_READS if call in text]


# --------------------------------------------------------- test_task_fencing_round_trip

_STALE_NOTE = "NOT on " + "the task"


def _check_stale_generation_note(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if _STALE_NOTE not in text:
        return []
    return [(_first_line(text, _STALE_NOTE),
             "types.ts still records current_generation as absent from the task API; "
             "task_to_api sends it")]


_VIS_IMPORT = re.compile(r"""(from|import\()\s*['"]vis-network""")


def _check_vis_network_import(path: str, text: str, tree: ast.Module | None) -> list[tuple[int, str]]:
    if "/__tests__/" in path:
        return []
    found = _VIS_IMPORT.search(text)
    if not found:
        return []
    return [(_line_of(text, found.start()),
             "vis-network is imported here: only the repository graph's Network view "
             "(RepoGraphNetwork.tsx) may draw with it, never the workflow DAG")]


# ----------------------------------------------------------------------- samples

_SM = "SecretManager" + "Reader"
_AV = "access_secret" + "_version"

RULES = (
    Rule(
        id="secret-payload-read-only-by-named-modules",
        why="only the modules named, each with what it does with the value, read a Secret "
            "Manager payload (owner decisions Q2 of archived issue 1347 and Q9 of 1433; "
            "was test_no_route_serves_credentials)",
        roots=(API,),
        suffixes=(".py",),
        requires=_PAYLOAD_CALL,
        exempt=SECRET_PAYLOAD_READERS,
        check=_check_payload_readers,
        bad=((f"{API}/routes/tasks.py", f"def f(client):\n    return client.{_AV}(name='x')\n"),),
        good=((f"{API}/routes/tasks.py", "def f(client):\n    return client.get_secret(name='x')\n"),),
    ),
    Rule(
        id="secret-reader-built-only-by-the-release",
        why="a route that builds its own secret reader is outside the credential route walk "
            "(owner decision Q2 of archived issue 1347; was test_no_route_serves_credentials)",
        roots=(API,),
        suffixes=(".py",),
        requires=_READER_MAKER,
        exempt=frozenset({f"{API}/routes/attempts_secrets.py"}),
        check=_check_reader_maker,
        bad=((f"{API}/routes/mcp.py", f"reader = {_SM}(client)\n"),),
        good=((f"{API}/routes/mcp.py", f"from .attempts_secrets import {_SM}\n"),),
    ),
    Rule(
        id="no-cloud-build-in-swarm-api",
        why="a bundle build is a Cloud Run job: no Cloud Build client, API or builder image "
            "(docs/design/managed-plugins.md 1.4; was test_plugin_bundles)",
        roots=(API,),
        suffixes=(".py",),
        requires=re.compile(r"cloudbuild|cloud_build|gcr\.io/cloud-builders|CloudBuild"),
        check=_check_no_cloud_build,
        bad=(
            (f"{API}/bundles.py", "from google.cloud.devtools.cloud" + "build_v1 import Client\n"),
            (f"{API}/bundles.py", "import google_cloud" + "_build\n"),
            (f"{API}/bundles.py", f"URL = 'https://{_BUILD_WORDS[0]}/v1'\n"),
            (f"{API}/bundles.py", f"IMAGE = '{_BUILD_WORDS[1]}/docker'\n"),
            (f"{API}/bundles.py", f"class {_BUILD_WORDS[2]}Jobs:\n    pass\n"),
        ),
        good=((f"{API}/bundles.py", "URL = 'https://run.googleapis.com/v2/jobs:run'\n"),),
    ),
    Rule(
        id="probe-admin-tenants-read-only-by-the-listed-routes",
        why="a scoped probe admin is admin only on the routes roles.PROBE_ADMIN_ROUTES lists "
            "(owner decision 2026-11-20, docs/design/workspace-roles.md section 8; was "
            "test_probe_admin_routes)",
        roots=(API,),
        suffixes=(".py",),
        requires="probe_admin_tenants",
        check=_check_probe_admin_tenants,
        bad=(
            (f"{API}/routes/secrets.py",
             "def put_secret(ctx, auth):\n    return roles.probe_admin_tenants(ctx.settings, auth.email)\n"),
            (f"{API}/routes/people.py",
             "def people_scope(ctx, auth):\n    def inner():\n"
             "        return roles.probe_admin_tenants(ctx.settings, auth.email)\n    return inner\n"),
            (f"{API}/roles.py",
             "def probe_admin_tenants(settings, email):\n    return frozenset()\n"),
        ),
        good=(
            (f"{API}/routes/people.py",
             "def join_scope(ctx, auth):\n    return roles.probe_admin_tenants(ctx.settings, auth.email)\n"),
            (f"{API}/roles.py",
             "def probe_admin_tenants(settings, email):\n    return frozenset()\n\n"
             "def probe_admin_scope(settings, auth, tenant_id):\n"
             "    return tenant_id in probe_admin_tenants(settings, auth.email)\n"),
        ),
    ),
    Rule(
        id="settings-readers-built-with-the-settings-mac",
        why="every JudgmentSettings, MergeSettingsStore and CodeIntelligenceStore is built "
            "with judgment.settings_mac(settings) (archived issue 1457, "
            "docs/design/tenant-settings-integrity.md 4.5; was test_settings_mac_config)",
        roots=(API,),
        suffixes=(".py",),
        requires=re.compile(r"JudgmentSettings|MergeSettingsStore|CodeIntelligenceStore"),
        check=_check_settings_mac,
        bad=(
            (f"{API}/live.py", "store = JudgmentSettings(db, now=now)\n"),
            (f"{API}/live.py", "store = judgment.MergeSettingsStore(db, mac=MAC)\n"),
            (f"{API}/live.py", "store = CodeIntelligenceStore(db, mac=judgment.settings_mac(s))\n"),
        ),
        good=(
            (f"{API}/live.py", "store = JudgmentSettings(db, mac=settings_mac(ctx.settings))\n"),
            (f"{API}/judgment.py", "class JudgmentSettings:\n    pass\n"),
        ),
    ),
    Rule(
        id="task-documents-created-only-by-the-signing-paths",
        why="a task created unsigned after the signing cutover is refused, so only the "
            "signing paths create a task document (archived issue 355; was "
            "test_spec_signing_every_submitter)",
        roots=("apps",),
        suffixes=(".py",),
        requires=_MAY_CREATE,
        check=_check_task_writers,
        bad=(
            ("apps/swarm-api/swarm_api/mergewake.py",
             "def wake(store, tasks):\n    store.create_tasks(tasks)\n"),
            ("apps/agent-worker/agent_worker/children.py",
             "class C:\n    def go(self, t):\n        return task_to_firestore(t)\n"),
            ("apps/swarm-api/swarm_api/children.py",
             "class ChildService:\n    def submit(self):\n        def _apply(tx):\n"
             "            return tx\n        return _apply\n"),
        ),
        good=(
            ("apps/swarm-api/swarm_api/children.py",
             "class ChildService:\n    def submit(self):\n        def _apply(tx):\n"
             "            self.store.create_tasks(tx)\n        return _apply\n"),
            ("apps/swarm-api/swarm_api/codec.py",
             "def task_to_firestore(task):\n    return {}\n\n\n"
             "def other(task):\n    return {}\n"),
            ("apps/swarm-api/swarm_api/routes/runs.py",
             "from ..codec import task_to_firestore\n\n\ndef f(svc):\n    svc.submit_tasks([])\n"),
        ),
    ),
    Rule(
        id="platform-secrets-names-no-value-read",
        why="the platform secret status reader calls get_secret and list_secret_versions "
            "and nothing else, checked in its syntax tree (docs/design/secrets-scopes.md "
            "5 and 11 Q7; was test_platform_secrets)",
        roots=(f"{API}/platform_secrets.py",),
        suffixes=(".py",),
        check=_check_platform_secrets,
        bad=(
            (f"{API}/platform_secrets.py", f"def read(client, name):\n    return client.{_AV}(name)\n"),
            (f"{API}/platform_secrets.py", "def read(v):\n    return v.payload\n"),
            (f"{API}/platform_secrets.py", "def read(client):\n    return client.list_secrets()\n"),
        ),
        good=((f"{API}/platform_secrets.py",
               '"""Never calls access_secret_version or reads a payload."""\n\n'
               "def read(client, name):\n    return client.get_secret(name=name)\n"),),
    ),
    Rule(
        id="task-routes-take-the-tenant-from-tenant-scope",
        why="a task or workflow route takes the tenant from the tenant_scope dependency, "
            "never auth.tenant_id (was test_tenant_isolation)",
        roots=(f"{API}/routes/tasks.py", f"{API}/routes/workflows.py"),
        suffixes=(".py",),
        requires="tenant_id",
        check=_check_raw_tenant_id,
        bad=((f"{API}/routes/tasks.py",
              "def logs(auth, store):\n    return store.logs(auth.tenant_id)\n"),),
        good=(
            (f"{API}/routes/tasks.py",
             '"""Never auth.tenant_id."""\n\ndef logs(tenant, store):\n'
             "    return store.logs(tenant.tenant_id)\n"),
        ),
    ),
    Rule(
        id="one-client-function-per-cancel-route",
        why="each cancel route has exactly one client function in api.ts, so its confirmation "
            "cannot be bypassed (B28; was test_stop_and_output_ui_surface)",
        roots=(f"{UI}/api.ts",),
        suffixes=(".ts",),
        check=_check_cancel_writes,
        bad=(
            (f"{UI}/api.ts", "export function cancelTask(id) {\n"
                             "  return write(route('/v1/tasks/{id}/cancel', id));\n}\n"),
            (f"{UI}/api.ts", "export function cancelTask(id) {\n"
                             "  return write(route('/v1/tasks/{id}/cancel', id));\n}\n"
                             "export function cancelWorkflow(id) {\n"
                             "  return write(route('/v1/workflows/{id}/cancel', id));\n}\n"
                             "export function again(id) {\n"
                             "  return write(`/v1/tasks/${id}/cancel`);\n}\n"),
        ),
        good=((f"{UI}/api.ts", "export function cancelTask(id) {\n"
                               "  return write(route('/v1/tasks/{id}/cancel', id));\n}\n"
                               "// write('/v1/tasks/x/cancel') in prose\n"
                               "export function cancelWorkflow(id) {\n"
                               "  return write(route('/v1/workflows/{id}/cancel', id));\n}\n"),),
    ),
    Rule(
        id="cancel-workflow-only-from-workflows",
        why="cancelWorkflow is used only from Workflows.tsx, which owns the confirmation "
            "(Workflows V2, 2026-10-01; was test_stop_and_output_ui_surface)",
        roots=(UI,),
        suffixes=(".tsx",),
        requires="cancelWorkflow",
        exempt=frozenset({f"{UI}/Workflows.tsx"}),
        check=_confined_caller("cancelWorkflow", "Workflows.tsx"),
        bad=((f"{UI}/AgentDetail.tsx", "import { cancelWorkflow } from './api';\n"),),
        good=(
            (f"{UI}/AgentDetail.tsx", "/* cancelWorkflow lives in Workflows */\n// cancelWorkflow\n"),
            (f"{UI}/components/Menu.tsx", "import { cancelWorkflow } from '../api';\n"),
        ),
    ),
    Rule(
        id="cancel-task-only-from-stop-run",
        why="cancelTask() is called only from StopRun.tsx, which owns the confirmation "
            "(B28; was test_stop_and_output_ui_surface)",
        roots=(UI,),
        suffixes=(".tsx",),
        requires="cancelTask(",
        exempt=frozenset({f"{UI}/StopRun.tsx"}),
        check=_confined_caller("cancelTask(", "StopRun.tsx"),
        bad=((f"{UI}/AgentDetail.tsx", "const stop = () => cancelTask(task.id);\n"),),
        good=((f"{UI}/AgentDetail.tsx", "import { cancelTask } from './api';\n"
                                        "/* cancelTask(id) only from StopRun */\n"),),
    ),
    Rule(
        id="utilisation-track-drawn-once",
        why="`.ctl-util-track` is drawn only by components/UsageBar.tsx, so the hatch, the "
            "baseline tick and the over-ceiling segment cannot be lost on one screen "
            "(archived issue 503; was test_workflow_step_measurements)",
        roots=(UI,),
        suffixes=(".tsx",),
        requires="ctl-util-track",
        exempt=frozenset({f"{UI}/components/UsageBar.tsx"}),
        check=_check_util_track,
        bad=(
            (f"{UI}/Capacity.tsx", '<div className="ctl-util-track">\n'),
            (f"{UI}/components/Holders.tsx", "<div className={`ctl-util-track ${x}`}>\n"),
        ),
        good=(
            (f"{UI}/Capacity.tsx", "<UsageTrack value={v} />\n// ctl-util-track via UsageBar\n"),
            (f"{UI}/components/deep/Other.tsx", '<div className="ctl-util-track">\n'),
        ),
    ),
    Rule(
        id="outcomes-names-no-runner-profile",
        why="the outcome ledger reads RunnerProfile.cost_declared and names no profile of "
            "its own (was test_outcomes_end_cause)",
        roots=(f"{API}/outcomes.py",),
        suffixes=(".py",),
        requires="mock",
        check=_check_outcomes_names_no_profile,
        bad=(
            (f"{API}/outcomes.py", 'DECLARED = frozenset({"mock"})\n'),
            (f"{API}/outcomes.py", "DECLARED = frozenset({'mock'})\n"),
        ),
        good=((f"{API}/outcomes.py", "# the mock profile declares its cost\nX = 1\n"),),
    ),
    Rule(
        id="typesafe-ticket-reads-no-secret-value",
        why="the TypeSafe ticket names a secret and swarm-api never reads one to mint it "
            "(docs/design/workspace-secrets.md 3.5, archived issue 1347; was "
            "test_typesafe_ticket_api)",
        roots=(f"{API}/typesafe_ticket.py",),
        suffixes=(".py",),
        check=_check_ticket_reads_no_secret,
        bad=(
            (f"{API}/typesafe_ticket.py", f"def mint(c):\n    return c.{_AV}(name='x')\n"),
            (f"{API}/typesafe_ticket.py", "def mint(v):\n    return v.access()\n"),
        ),
        good=((f"{API}/typesafe_ticket.py", "def mint(name, version):\n    return {'secret': name}\n"),),
    ),
    Rule(
        id="types-ts-records-no-stale-generation-note",
        why="task_to_api sends current_generation, and the note in types.ts that said it does "
            "not was the reason no screen rendered it (was test_task_fencing_round_trip)",
        roots=(f"{UI}/types.ts",),
        suffixes=(".ts",),
        check=_check_stale_generation_note,
        bad=((f"{UI}/types.ts", f"// {_STALE_NOTE}: `current_generation`\n"),),
        good=((f"{UI}/types.ts", "  current_generation: number;\n"),),
    ),
    Rule(
        id="vis-network-only-in-the-repository-graph",
        why="vis-network reaches the bundle through the repository graph's Network view "
            "alone, never the workflow DAG (was test_workflow_graph_ui_surface)",
        roots=(UI,),
        suffixes=(".ts", ".tsx"),
        requires="vis-network",
        exempt=frozenset({f"{UI}/RepoGraphNetwork.tsx"}),
        check=_check_vis_network_import,
        bad=(
            (f"{UI}/WorkflowGraph.tsx", "import { Network } from 'vis-network/standalone'\n"),
            (f"{UI}/dag.ts", 'const m = await import("vis-network")\n'),
        ),
        good=(
            (f"{UI}/dag.ts", "// not vis-network: six boxes are drawn as plain SVG\n"),
            (f"{UI}/__tests__/graph.test.tsx", "import { Network } from 'vis-network'\n"),
        ),
    ),
)
