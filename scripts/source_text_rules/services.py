"""Rules moved out of the service tests: mcp, install, common, scheduler, hooks,
speech, voice and terraform (part of issue 73).

Each rule here was an absence-or-confinement sweep in a test file that read
the tree itself. The condition is copied from that test, not loosened: the
`why` names the decision the test's docstring cited and the test it came
from. Where the test also pinned a count or a presence inside the same file
(a confinement that must still hold once), the rule reports that file when
the count differs, so it refuses everything the test refused.

A test's non-vacuity control over its own walk (`visited >= 30`,
`len(modules) > 5`) is the check script's job now: every rule's roots are
read from the tracked tree, and tests/unit/scripts/test_check_source_text.py
runs each rule's samples.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable

from source_text_rules import Rule

Hits = list[tuple[int, str]]

_MCP = "apps/swarm-mcp/swarm_mcp"
_INSTALL = "apps/swarm-install/swarm_install"
_SCHEDULER = "apps/scheduler/scheduler"


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _direct(path: str, directory: str) -> bool:
    """`path` is a file directly in `directory`: the tests' `glob("*.py")`,
    which does not recurse."""
    return path.rsplit("/", 1)[0] == directory


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _substrings(text: str, needles: Iterable[str], message: str) -> Hits:
    hits: Hits = []
    for needle in needles:
        start = text.find(needle)
        while start != -1:
            hits.append((_line(text, start), f"{message}: `{needle}`"))
            start = text.find(needle, start + 1)
    return hits


def _regex(text: str, pattern: re.Pattern[str], message: str) -> Hits:
    return [(_line(text, m.start()), message) for m in pattern.finditer(text)]


# ---------------------------------------------------------------- common

_FIX_ROUNDS = re.compile(r"ParkReason\.FIX_ROUNDS_RUNNING|[\"']FIX_ROUNDS_RUNNING[\"']")


def _check_fix_rounds(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _SCHEDULER):
        return []
    found = list(_FIX_ROUNDS.finditer(text))
    if _name(path) != "loop.py":
        return [(_line(text, m.start()), "only loop.py's `_park_control_plane` may name "
                 "FIX_ROUNDS_RUNNING: no sweep, promotion or expiry reads it") for m in found]
    if len(found) != 1:
        return [(1, f"loop.py names FIX_ROUNDS_RUNNING {len(found)} times; exactly once, "
                 "the park `_park_control_plane` writes")]
    return []


_STORED_TOTALS = re.compile(r"""(["'.])(attempt_totals|current_account)\b""")
_ADMISSION_AND_CAPACITY = (
    "apps/scheduler/scheduler",
    "apps/reconciler/reconciler",
    "apps/quota-broker/quota_broker",
    "apps/common/swarm_common/admission.py",
    "apps/common/swarm_common/states.py",
    "apps/swarm-api/swarm_api/headroom.py",
    "apps/swarm-api/swarm_api/served_limits.py",
    "apps/swarm-api/swarm_api/accountholds.py",
    "apps/swarm-api/swarm_api/waiting.py",
    "apps/agent-worker/agent_worker/accountlease.py",
)


def _check_stored_totals(path: str, text: str, tree: ast.Module | None) -> Hits:
    return [(number, "an admission, leasing, scheduling or capacity module reads a "
             "read-side view (`attempt_totals`/`current_account`)")
            for number, line in enumerate(text.splitlines(), 1) if _STORED_TOTALS.search(line)]


# ---------------------------------------------------------------- install

def _check_desktop_secret(path: str, text: str, tree: ast.Module | None) -> Hits:
    if tree is None:
        return [(1, "does not parse; the secret-read check cannot read it")]
    hits: Hits = []
    name = _name(path)
    if name in {"preflight.py", "edge.py"}:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and ":access" in node.value and name != "preflight.py":
                hits.append((node.lineno, f"{name} reads a secret's value"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and "access_secret" in node.func.attr:
                names = {n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                if "DESKTOP_SECRET" not in names:
                    hits.append((node.lineno,
                                 f"{name} reads a secret other than the Desktop client's"))
                if names & {"APP_SECRETS"}:
                    hits.append((node.lineno, f"{name} reads an App secret"))
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and ":access" in node.value:
            hits.append((node.lineno, f"{name} reads a secret's value"))
        if isinstance(node, (ast.Attribute, ast.Name)):
            ident = node.attr if isinstance(node, ast.Attribute) else node.id
            if "access_secret" in ident:
                hits.append((node.lineno, f"{name} reads a secret's value"))
    return hits


def _check_resume_flag(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _substrings(text, ("swarm install --resume",),
                       "`swarm install` has no --resume; the line says `swarm install`")


_BRANDS_FILES = (f"{_INSTALL}/adopt.py", f"{_INSTALL}/remains.py", f"{_INSTALL}/steps/edge.py")


def _check_brands(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _substrings(text, ("iap_brands", "/brands", "identityAwareProxyClients"),
                       "the IAP brands and OAuth-client APIs are shut down")


def _check_tfvars(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = _substrings(text, ('terraform.tfvars"',),
                       "input generation reads no committed tfvars")
    return hits + _substrings(text.replace("terraform/environments/`", ""), ("environments/",),
                              "input generation reads no committed environments/ file")


def _check_team_prompt(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _substrings(text, ("input(", "getpass"),
                       "`swarm team add` asks for no token: the git value goes through a link")


# ------------------------------------------------------------------ hooks

def _check_receiver(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = _substrings(text, ("== expected", "expected =="),
                       "the webhook MAC is compared with ==, not in constant time")
    if "hmac.compare_digest(" not in text:
        hits.append((1, "the webhook MAC is not compared with hmac.compare_digest"))
    return hits


# -------------------------------------------------------------------- mcp

_CLIENT_BUILT = re.compile(r"\bSwarmClient\(")
_CLIENTS = {"cli.py": 2, "sc.py": 3, "server.py": 1}


def _check_client_built(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _MCP):
        return []
    lines = [number for number, line in enumerate(text.splitlines(), 1)
             if _CLIENT_BUILT.search(line) and not line.lstrip().startswith(("#", "\"", "`"))]
    want = _CLIENTS.get(_name(path), 0)
    if len(lines) == want:
        return []
    if want == 0:
        return [(n, "a SwarmClient is built outside cli.py, sc.py and serve._client")
                for n in lines]
    return [(lines[0] if lines else 1,
             f"{_name(path)} builds SwarmClient {len(lines)} times, not {want}")]


def _string_literals(tree: ast.Module) -> list[ast.Constant]:
    """Every string constant in a module that is not a docstring."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def _check_launcher(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _MCP) or _name(path) == "invocation.py":
        return []
    if tree is None:
        return [(1, "does not parse; the launcher check cannot read it")]
    return [(node.lineno, "a launcher is spelled outside swarm_mcp.invocation")
            for node in _string_literals(tree)
            if "uv run " in node.value or "uv tool run" in node.value]


#: A backticked `swarm <subcommand>` or `sc <subcommand>` in a string the
#: bridge prints. Built from parts, so this file's own text is no hit.
_HAND_SPELLED = re.compile(r"`(?:swarm|sc) [a-z<-][^`]*" + "`")


def _check_hand_spelled(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _MCP) or _name(path) == "invocation.py":
        return []
    if tree is None:
        return [(1, "does not parse; the command check cannot read it")]
    return [(node.lineno, "a command is spelled by hand, not by invocation.terminal_command: "
             + ", ".join(sorted(set(_HAND_SPELLED.findall(node.value)))))
            for node in _string_literals(tree) if _HAND_SPELLED.search(node.value)]


_MCP_SERVERS_REQUEST = re.compile(r'request\(\s*"(\w+)",\s*"/v1/mcp-servers')
_WORKSPACE_SECRETS = re.compile(r"/v1/workspace/secrets[^\"'\s]*")


def _check_registry_and_secrets(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _MCP):
        return []
    name = _name(path)
    hits: Hits = []
    if name == "sc.py":
        if _MCP_SERVERS_REQUEST.findall(text) != ["GET"]:
            hits.append((1, "sc.py must request `/v1/mcp-servers` once, as a GET"))
    elif "/v1/mcp-servers" in text:
        hits.append((_line(text, text.index("/v1/mcp-servers")),
                     "only sc.py may name `/v1/mcp-servers`"))
    found = sorted({m.group(0) for m in _WORKSPACE_SECRETS.finditer(text)})
    if name == "secrets.py":
        if found != ["/v1/workspace/secrets/links"]:
            hits.append((1, f"secrets.py names {found}; only the one-time link mint"))
    elif "/v1/workspace/secrets" in text:
        hits.append((_line(text, text.index("/v1/workspace/secrets")),
                     "only secrets.py may name the workspace secret store"))
    return hits


def _check_tail(path: str, text: str, tree: ast.Module | None) -> Hits:
    return [(number, "builds a tail command itself instead of calling follow.follow_command")
            for number, line in enumerate(text.split("\n"), 1)
            if '"swarm tail' in line or "'swarm tail" in line]


_RELEASE = re.compile(r"0\.5\.5[0-9]")


def _check_release(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _regex(text, _RELEASE, "launch.py names a plugin release for a drop that happened")


_CREDENTIAL_ROUTE = re.compile(r"/credential[\"'?]")


def _check_qa_credential(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = _substrings(text, ('"PUT"', "'PUT'"),
                       "the bridge never calls the route that takes the credential")
    return hits + _regex(text, _CREDENTIAL_ROUTE,
                         "the bridge never calls the route that takes the credential")


_ROLE_WRITE_PATHS = (
    "/v1/workspace/roles/person",
    "/v1/workspace/roles/default",
    "/v1/admin/workspaces/",
)
_ROLE_FORBIDDEN = {"ROLE_WRITES", "_send_role_change", "cmd_roles_set", "cmd_roles_clear",
                   "cmd_roles_default"}


def _names_used(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    return names


def _check_role_writes(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, _MCP):
        return []
    if tree is None:
        return [(1, "does not parse; the role-write check cannot read it")]
    name = _name(path)
    hits: Hits = []
    holders: set[str] = set()
    for top in tree.body:
        holder = top.name if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) \
            else "<module>"
        for node in ast.walk(top):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and any(n in node.value for n in _ROLE_WRITE_PATHS):
                holders.add(holder)
                if name != "sc.py" or holder != "<module>":
                    hits.append((node.lineno, f"{name}:{holder} names a role-write route"))
    if name != "sc.py":
        used = _ROLE_FORBIDDEN & _names_used(tree)
        if used:
            hits.append((1, f"{name} names {sorted(used)}: only sc.py reaches a role write"))
        return hits
    if holders != {"<module>"}:
        hits.append((1, "sc.py must name the role-write routes at module level only"))
    readers = {top.name for top in tree.body
               if isinstance(top, ast.FunctionDef) and "ROLE_WRITES" in _names_used(top)}
    if readers != {"_send_role_change"}:
        hits.append((1, f"ROLE_WRITES is read by {sorted(readers)}, not `_send_role_change`"))
    callers = {top.name for top in tree.body
               if isinstance(top, ast.FunctionDef) and "_send_role_change" in _names_used(top)}
    if callers != {"cmd_roles_set", "cmd_roles_clear", "cmd_roles_default"}:
        hits.append((1, f"`_send_role_change` is reached from {sorted(callers)}, not the "
                     "three typed `sc roles` handlers"))
    return hits


_PANE_FORBIDDEN = ("$.clock.after", "$.clock.sleep", "setTimeout", "setInterval",
                   "$.model.", "$.agent.", "$.prompt.submit", "$.tool.")
_PANE_MODEL = "plugin/hooks/pane/model.ts"


def _check_pane(path: str, text: str, tree: ast.Module | None) -> Hits:
    hits = _substrings(text, _PANE_FORBIDDEN, "would read on a timer or reach the model")
    if path == _PANE_MODEL:
        hits += _substrings(text, ("$.clock.every",), "the model runs no timer; the band's "
                            "one timer is pane.tsx's `startMinute`")
    return hits


_DEBUG_FORBIDDEN = ("storage.googleapis", "logging.googleapis", "gs://", "download(")


def _check_debug_report(path: str, text: str, tree: ast.Module | None) -> Hits:
    if tree is None:
        return [(1, "does not parse; the debug_report check cannot read it")]
    hits: Hits = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "debug_report":
            # inspect.getsource's span: the decorators through the last line.
            first = min([node.lineno] + [d.lineno for d in node.decorator_list])
            lines = text.splitlines(keepends=True)
            body = "".join(lines[first - 1:node.end_lineno])
            hits += [(first - 1 + line, message) for line, message in _substrings(
                body, _DEBUG_FORBIDDEN, "sc.debug_report reads GCS or Cloud Logging directly")]
            return hits
    return [(1, "sc.py defines no top-level debug_report for the check to read")]


def _check_tenant(path: str, text: str, tree: ast.Module | None) -> Hits:
    return _substrings(text, ('environ.get("SWARM_TENANT")', 'os.environ["SWARM_TENANT"]'),
                       "sc.py restates the tenant rule; use client.chosen_tenant")


# -------------------------------------------------------------- scheduler

_FAILED_TO_READY = re.compile(
    r"(?:assert|can)_transition\(\s*TaskState\.FAILED\s*,\s*TaskState\.READY"
    r"|\(\s*TaskState\.FAILED\s*,\s*TaskState\.READY\s*\)"
)


def _check_failed_to_ready(path: str, text: str, tree: ast.Module | None) -> Hits:
    if path.startswith("apps/common/swarm_common/"):
        return []
    return _regex(text, _FAILED_TO_READY, "a writer moves a FAILED task back to READY")


# ---------------------------------------------------------- plugin intake

def _check_intake_mount(path: str, text: str, tree: ast.Module | None) -> Hits:
    if tree is None:
        return [(1, "does not parse; the mount check cannot read it")]
    return [(node.lineno, "the intake job mounts nothing: read the bucket through "
             "PLUGINS_BUCKET, never SWARM_INTAKE_MOUNT")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "SWARM_INTAKE_MOUNT"]


# --------------------------------------------------------- speech, voice

_WATCHDOG_IMPORTS = {"__future__", "json", "logging", "subprocess", "sys", "time", "pathlib"}


def _check_watchdog(path: str, text: str, tree: ast.Module | None) -> Hits:
    if tree is None:
        return [(1, "does not parse")]
    hits: Hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                hits.append((node.lineno, "a relative import would need the package on the host"))
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        for root in sorted(roots - _WATCHDOG_IMPORTS):
            hits.append((node.lineno, f"imports {root!r}: the host has Python and the "
                         "standard library only"))
    try:
        ast.parse(text, feature_version=(3, 10))
    except SyntaxError as error:
        hits.append((error.lineno or 1, "not Python 3.10 syntax: the VM runs 3.10"))
    return hits


_OPENAI_SECRET = re.compile(r"swarm-tenant-[^\"'\s]*openai|-openai\b")


def _check_voice_sign_in(path: str, text: str, tree: ast.Module | None) -> Hits:
    if not _direct(path, "apps/swarm-voice/swarm_voice"):
        return []
    message = "swarm-voice reads no person's own ChatGPT sign-in"
    hits = _regex(text, _OPENAI_SECRET, message)
    hits += _substrings(text, ("auth.openai.com", "refresh_token", ":addVersion"), message)
    if _name(path) != "planner.py":
        hits += _substrings(text.lower(), ("secretmanager",),
                            "only planner.py touches Secret Manager in swarm-voice")
        hits += _substrings(text, ("swarm-tenant-",),
                            "only planner.py touches Secret Manager in swarm-voice")
    return hits


# ------------------------------------------------------------------ rules

_D = "def"  # samples are built from parts where their text would hit this file
#: A storage URL scheme and the tenant secret prefix, built from parts so a
#: sample is not read as a bucket or a secret name by the deployment-values guard.
_GS = "gs" + ":" + "//"
_TS = "swarm" + "-tenant-"

RULES = (
    Rule(
        id="fix-rounds-running-only-in-the-control-plane-park",
        why="FL-SCHED parks a READY fix loop on FIX_ROUNDS_RUNNING once, in loop.py's "
            "_park_control_plane (docs/design/fix-loop.md §4.7); no sweep, promotion or "
            "expiry names it (was tests/unit/common/test_fix_loop_contract.py)",
        roots=(_SCHEDULER,),
        check=_check_fix_rounds,
        bad=(
            (f"{_SCHEDULER}/store.py", "reason = ParkReason.FIX_ROUNDS_RUNNING\n"),
            (f"{_SCHEDULER}/main.py", "if park == 'FIX_ROUNDS_RUNNING':\n    pass\n"),
            (f"{_SCHEDULER}/loop.py", "a = 1\n"),
            (f"{_SCHEDULER}/loop.py",
             "x = ParkReason.FIX_ROUNDS_RUNNING\ny = ParkReason.FIX_ROUNDS_RUNNING\n"),
        ),
        good=(
            (f"{_SCHEDULER}/loop.py", "self._park(ParkReason.FIX_ROUNDS_RUNNING)\n"),
            (f"{_SCHEDULER}/store.py", "# FIX_ROUNDS_RUNNING is the loop's\n"),
            (f"{_SCHEDULER}/sub/x.py", "r = ParkReason.FIX_ROUNDS_RUNNING\n"),
        ),
    ),
    Rule(
        id="no-capacity-module-reads-stored-task-totals",
        why="contract request 132 (accepted 2026-11-01): Task.attempt_totals and "
            "current_account are read-side views no admission or capacity module reads "
            "(invariants 2 and 3; was tests/unit/common/test_stored_task_totals_contract.py)",
        roots=_ADMISSION_AND_CAPACITY,
        requires=_STORED_TOTALS,
        check=_check_stored_totals,
        bad=(
            ("apps/scheduler/scheduler/loop.py", 'x = task.get("current_account")\n'),
            ("apps/reconciler/reconciler/a.py", "y = doc['attempt_totals']\n"),
            ("apps/swarm-api/swarm_api/headroom.py", "if task.attempt_totals:\n    pass\n"),
            ("apps/agent-worker/agent_worker/accountlease.py", "x = task.current_account\n"),
        ),
        good=(
            ("apps/scheduler/scheduler/loop.py",
             'current_account = str(template.service_account or "")\n'),
            ("apps/swarm-api/swarm_api/waiting.py", "x = task.attempts_total\n"),
        ),
    ),
    Rule(
        id="install-reads-only-the-desktop-secret",
        why="no installer module reads a secret's value but preflight's reader, called "
            "from edge.py only with DESKTOP_SECRET (archived issue 2964; was "
            "tests/unit/install/test_switches.py)",
        roots=(_INSTALL,),
        check=_check_desktop_secret,
        bad=(
            (f"{_INSTALL}/steps/infra.py", 'URL = "projects/p/secrets/s/versions/latest:access"\n'),
            (f"{_INSTALL}/steps/infra.py", "value = gcp.access_secret_version(name)\n"),
            (f"{_INSTALL}/steps/edge.py", "v = gcp.access_secret_version(APP_SECRETS[0])\n"),
            (f"{_INSTALL}/steps/edge.py",
             "v = gcp.access_secret_version(DESKTOP_SECRET, APP_SECRETS)\n"),
            (f"{_INSTALL}/steps/edge.py", 'URL = "x:access"\n'),
        ),
        good=(
            (f"{_INSTALL}/steps/preflight.py",
             f'URL = "x/versions/latest:access"\n{_D} access_secret_version(self, name):\n'
             "    return URL\n"),
            (f"{_INSTALL}/steps/edge.py", "v = gcp.access_secret_version(DESKTOP_SECRET)\n"),
            (f"{_INSTALL}/steps/infra.py", "meta = gcp.secret(name)\n"),
        ),
    ),
    Rule(
        id="install-names-no-resume-flag",
        why="`swarm install` has no --resume; it resumes by itself, so no line the "
            "engine hands a person names the flag (archived pull request 18; was "
            "tests/unit/install/test_secret_entry.py)",
        roots=(_INSTALL,),
        requires="--resume",
        check=_check_resume_flag,
        bad=((f"{_INSTALL}/engine.py", 'HINT = "run swarm install --resume"\n'),),
        good=((f"{_INSTALL}/engine.py", 'HINT = "run swarm install"\n'),),
    ),
    Rule(
        id="install-names-no-brands-api",
        why="adopt.py probes no IAP brand and lists no OAuth client: both APIs are shut "
            "down; the Desktop client is adopted from the record (was "
            "tests/unit/install/test_edge_probes.py)",
        roots=_BRANDS_FILES,
        check=_check_brands,
        bad=(
            (f"{_INSTALL}/adopt.py", 'URL = "v1/projects/p/brands"\n'),
            (f"{_INSTALL}/remains.py", "x = iap_brands\n"),
            (f"{_INSTALL}/steps/edge.py", 'K = "identityAwareProxyClients"\n'),
        ),
        good=(
            (f"{_INSTALL}/adopt.py", "client = record.get('desktop_client_id')\n"),
            (f"{_INSTALL}/remains.py", 'URL = "v1/projects/p/oauthClients"\n'),
        ),
    ),
    Rule(
        id="tfvars-reads-no-committed-tfvars",
        why="input generation reads nothing from disk: not terraform.tfvars, not "
            "environments/ (was tests/unit/install/test_tfvars.py)",
        roots=(f"{_INSTALL}/tfvars.py",),
        check=_check_tfvars,
        bad=(
            (f"{_INSTALL}/tfvars.py", "p = root / 'x' / \"terraform.tfvars\"\n"),
            (f"{_INSTALL}/tfvars.py", 'p = "terraform/environments/dev"\n'),
        ),
        good=((f"{_INSTALL}/tfvars.py",
               '"""Never written to `terraform/environments/`; built from state."""\n'),),
    ),
    Rule(
        id="team-add-prompts-for-nothing",
        why="`swarm team add` never prompts for a token: the git value goes through a "
            "one-time link (was tests/unit/install/test_team_add.py)",
        roots=(f"{_INSTALL}/team.py",),
        requires=re.compile(r"input\(|getpass"),
        check=_check_team_prompt,
        bad=(
            (f"{_INSTALL}/team.py", "token = input('token: ')\n"),
            (f"{_INSTALL}/team.py", "import getpass\n"),
        ),
        good=((f"{_INSTALL}/team.py", "print(link)\n"),),
    ),
    Rule(
        id="hooks-receiver-compares-in-constant-time",
        why="the webhook receiver compares the signature with hmac.compare_digest, never "
            "with == (was tests/unit/hooks/test_hooks_receiver.py)",
        roots=("apps/swarm-github-hooks/swarm_github_hooks/receiver.py",),
        check=_check_receiver,
        bad=(
            ("apps/swarm-github-hooks/swarm_github_hooks/receiver.py",
             "ok = signature == expected\n"),
            ("apps/swarm-github-hooks/swarm_github_hooks/receiver.py",
             "ok = hmac.compare_digest(a, b) or expected == signature\n"),
        ),
        good=(("apps/swarm-github-hooks/swarm_github_hooks/receiver.py",
               "ok = hmac.compare_digest(signature, expected)\n"),),
    ),
    Rule(
        id="mcp-builds-its-client-in-one-place",
        why="every SwarmClient the bridge's server can reach is serve._client's; the "
            "others are the sc/swarm entry points (was tests/unit/mcp/test_bridge_pacing.py)",
        roots=(_MCP,),
        check=_check_client_built,
        bad=(
            (f"{_MCP}/workflows.py", "client = SwarmClient(url)\n"),
            (f"{_MCP}/server.py", "a = SwarmClient(u)\nb = SwarmClient(u)\n"),
            (f"{_MCP}/cli.py", "a = SwarmClient(u)\n"),
        ),
        good=(
            (f"{_MCP}/server.py", "# SwarmClient( is built once\n_client = SwarmClient(u)\n"),
            (f"{_MCP}/follow.py", '"""SwarmClient( is the server\'s."""\n'),
        ),
    ),
    Rule(
        id="mcp-launcher-spelled-only-in-invocation",
        why="the launcher spelling is decided in swarm_mcp.invocation; no other module "
            "writes `uv run ` or `uv tool run` into a string (archived issue 189; was "
            "tests/unit/mcp/test_commands_run_where_the_bridge_runs.py)",
        roots=(_MCP,),
        check=_check_launcher,
        bad=(
            (f"{_MCP}/follow.py", 'RUN_PREFIX = "uv run "\n'),
            (f"{_MCP}/sc.py", "x = f'uv tool run --from {spec} sc'\n"),
        ),
        good=(
            (f"{_MCP}/invocation.py", 'RUN_PREFIX = "uv run "\n'),
            (f"{_MCP}/follow.py", '"""Never `uv run ` by hand."""\nx = 1\n'),
        ),
    ),
    Rule(
        id="mcp-no-hand-spelled-command",
        why="no printed string outside swarm_mcp.invocation carries a backticked "
            "`swarm`/`sc` command; each is built by terminal_command (review of archived "
            "pull request 201, archived issue 189; was "
            "tests/unit/mcp/test_commands_run_where_the_bridge_runs.py)",
        roots=(_MCP,),
        check=_check_hand_spelled,
        bad=(
            (f"{_MCP}/sc.py", "MSG = 'see `swarm doctor`'\n"),
            (f"{_MCP}/config.py", "MSG = '`sc context add` writes a fresh one'\n"),
        ),
        good=(
            (f"{_MCP}/invocation.py", "MSG = 'see `swarm doctor`'\n"),
            (f"{_MCP}/sc.py", "MSG = 'the `sc` program'\n"),
            (f"{_MCP}/sc.py", '"""Run `swarm doctor`."""\n'),
        ),
    ),
    Rule(
        id="mcp-reads-the-server-registry-and-mints-only-secret-links",
        why="the bridge names /v1/mcp-servers only in one GET in sc.py and the workspace "
            "secret store only as its link mint in secrets.py (SSC-SC, secrets-scopes.md "
            "§6.4; was tests/unit/mcp/test_mcp_servers_tool.py)",
        roots=(_MCP,),
        check=_check_registry_and_secrets,
        bad=(
            (f"{_MCP}/server.py", 'r = client.request("GET", "/v1/mcp-servers")\n'),
            (f"{_MCP}/sc.py", 'r = client.request("POST", "/v1/mcp-servers")\n'),
            (f"{_MCP}/sc.py", "x = 1\n"),
            (f"{_MCP}/secrets.py",
             'a = "/v1/workspace/secrets/links"\nb = "/v1/workspace/secrets/values"\n'),
            (f"{_MCP}/server.py", 'a = "/v1/workspace/secrets/links"\n'),
        ),
        good=(
            (f"{_MCP}/sc.py", 'r = client.request("GET", "/v1/mcp-servers")\n'),
            (f"{_MCP}/secrets.py", 'a = "/v1/workspace/secrets/links"\n'),
            (f"{_MCP}/server.py", "x = 1\n"),
        ),
    ),
    Rule(
        id="mcp-tail-command-only-through-follow",
        why="server.py and workflows.py build `swarm tail` only through "
            "follow.follow_command (archived issue 189; was "
            "tests/unit/mcp/test_plugin_commands.py)",
        roots=(f"{_MCP}/server.py", f"{_MCP}/workflows.py"),
        requires="swarm tail",
        check=_check_tail,
        bad=(
            (f"{_MCP}/server.py", "cmd = \"swarm tail \" + ids\n"),
            (f"{_MCP}/workflows.py", "cmd = 'swarm tail ' + ids\n"),
        ),
        good=((f"{_MCP}/server.py", "cmd = follow.follow_command(ids)\n"),),
    ),
    Rule(
        id="mcp-launch-names-no-dropped-release",
        why="the attach suffix reader is gone, and so is its promise to drop it in a "
            "0.5.5x plugin (SC-ATTACH-SUFFIX-DROP; was "
            "tests/unit/mcp/test_plugin_row_split_2.py)",
        roots=(f"{_MCP}/launch.py",),
        check=_check_release,
        bad=((f"{_MCP}/launch.py", "# drop it in plugin 0.5.52\n"),),
        good=((f"{_MCP}/launch.py", "# the one-id path\n"),),
    ),
    Rule(
        id="mcp-qa-targets-never-sends-the-credential",
        why="the bridge never calls the route that takes a QA target's credential "
            "(VQ-6, archived issue 1933; was tests/unit/mcp/test_qa_targets_tools.py)",
        roots=(f"{_MCP}/qatargets.py",),
        check=_check_qa_credential,
        bad=(
            (f"{_MCP}/qatargets.py", 'client.request("PUT", f"/v1/qa-targets/{i}/x")\n'),
            (f"{_MCP}/qatargets.py", "client.request('POST', f\"/v1/qa-targets/{i}/credential\")\n"),
        ),
        good=((f"{_MCP}/qatargets.py",
               'client.request("POST", f"/v1/qa-targets/{i}/credential-links")\n'),),
    ),
    Rule(
        id="mcp-no-handler-reaches-a-role-write",
        why="no MCP tool grants or revokes a role: the write routes are named in sc.py "
            "only, read by _send_role_change, reached from the three `sc roles` handlers "
            "(ROLES-MCP, archived issue 1518; was tests/unit/mcp/test_roles_bridge.py)",
        roots=(_MCP,),
        check=_check_role_writes,
        bad=(
            (f"{_MCP}/server.py", 'P = "/v1/workspace/roles/person"\n'),
            (f"{_MCP}/server.py", "from swarm_mcp.sc import _send_role_change\n"),
            (f"{_MCP}/sc.py", f'{_D} f():\n    return "/v1/admin/workspaces/x"\n'),
            (f"{_MCP}/sc.py", 'ROLE_WRITES = ("/v1/workspace/roles/person",)\n'),
        ),
        good=(
            (f"{_MCP}/sc.py",
             'ROLE_WRITES = ("/v1/workspace/roles/person",)\n'
             f"{_D} _send_role_change(c):\n    return ROLE_WRITES\n"
             f"{_D} cmd_roles_set(a):\n    return _send_role_change(a)\n"
             f"{_D} cmd_roles_clear(a):\n    return _send_role_change(a)\n"
             f"{_D} cmd_roles_default(a):\n    return _send_role_change(a)\n"),
            (f"{_MCP}/server.py", 'P = "/v1/workspace/roles"\n'),
        ),
    ),
    Rule(
        id="sc-pane-starts-no-timer-and-reaches-no-model",
        why="drawing reads nothing and the pane reaches no model; its one timer is the "
            "band's (SC-BAND, owner 2026-10-26; was tests/unit/mcp/test_sc_browser_pane.py)",
        roots=(_PANE_MODEL, "plugin/hooks/pane.tsx"),
        check=_check_pane,
        bad=(
            (_PANE_MODEL, "setTimeout(refresh, 30000)\n"),
            ("plugin/hooks/pane.tsx", "await $.model.ask(x)\n"),
            (_PANE_MODEL, "$.clock.every(60, tick)\n"),
        ),
        good=(
            ("plugin/hooks/pane.tsx", "$.clock.every(60, tick)\n"),
            (_PANE_MODEL, "export function frame(state, view) { return [] }\n"),
        ),
    ),
    Rule(
        id="sc-debug-report-reads-only-the-api",
        why="sc debug never reads GCS or Cloud Logging directly, only the four client "
            "reads (was tests/unit/mcp/test_sc_config_and_debug.py)",
        roots=(f"{_MCP}/sc.py",),
        check=_check_debug_report,
        bad=(
            (f"{_MCP}/sc.py", f'{_D} debug_report(api):\n    return "{_GS}b/log"\n'),
            (f"{_MCP}/sc.py", f"{_D} other():\n    pass\n"),
        ),
        good=(
            (f"{_MCP}/sc.py", f'X = "{_GS}e"\n{_D} debug_report(api):\n'
                              "    return api.logs()\n"),
        ),
    ),
    Rule(
        id="sc-reads-no-swarm-tenant-itself",
        why="the tenant rule lives once, in client.chosen_tenant; sc.py does not restate "
            "it (was tests/unit/mcp/test_sc_tenant.py)",
        roots=(f"{_MCP}/sc.py",),
        requires="SWARM_TENANT",
        check=_check_tenant,
        bad=(
            (f"{_MCP}/sc.py", 't = os.environ.get("SWARM_TENANT")\n'),
            (f"{_MCP}/sc.py", 't = os.environ["SWARM_TENANT"]\n'),
        ),
        good=((f"{_MCP}/sc.py", "t = chosen_tenant()\n"),),
    ),
    Rule(
        id="no-writer-moves-failed-to-ready",
        why="the table keeps FAILED -> READY and no component outside swarm_common writes "
            "it: every READY write is from PARKED or a returned lease (was "
            "tests/unit/scheduler/test_mcp_admission.py)",
        roots=("apps",),
        requires="TaskState.FAILED",
        check=_check_failed_to_ready,
        bad=(
            ("apps/scheduler/scheduler/store.py",
             "assert_transition(TaskState.FAILED, TaskState.READY)\n"),
            ("apps/reconciler/reconciler/x.py",
             "EDGES = {(TaskState.FAILED , TaskState.READY )}\n"),
        ),
        good=(
            ("apps/common/swarm_common/states.py",
             "EDGES = {(TaskState.FAILED, TaskState.READY)}\n"),
            ("apps/scheduler/scheduler/store.py",
             "assert_transition(TaskState.PARKED, TaskState.READY)\n"),
        ),
    ),
    Rule(
        id="plugin-intake-reads-no-mount",
        why="live QA finding, 10 November 2026: the intake read a mount the deployed job "
            "never had; it reads the bucket through PLUGINS_BUCKET and no mount "
            "(docs/design/managed-plugins.md §2.2-§2.3; was "
            "tests/unit/terraform/test_plugin_intake_job_access.py)",
        roots=("apps/plugin-intake/plugin_intake/main.py",),
        requires="SWARM_INTAKE_MOUNT",
        check=_check_intake_mount,
        bad=(("apps/plugin-intake/plugin_intake/main.py",
              'root = os.environ.get("SWARM_INTAKE_MOUNT", "/plugins")\n'),),
        good=(("apps/plugin-intake/plugin_intake/main.py",
               'bucket = os.environ.get("PLUGINS_BUCKET")\n'),),
    ),
    Rule(
        id="speech-watchdog-stdlib-only",
        why="the watchdog runs on the VM's Python 3.10 when the container cannot: no "
            "import from the package or outside the standard library (was "
            "tests/unit/speech/test_speech_watchdog.py)",
        roots=("apps/swarm-speech/swarm_speech/watchdog.py",),
        check=_check_watchdog,
        bad=(
            ("apps/swarm-speech/swarm_speech/watchdog.py", "import requests\n"),
            ("apps/swarm-speech/swarm_speech/watchdog.py", "from . import activity\n"),
            ("apps/swarm-speech/swarm_speech/watchdog.py", "from swarm_speech import x\n"),
            ("apps/swarm-speech/swarm_speech/watchdog.py",
             "import json\nmatch json:\n    case _:\n        pass\n"
             "type Alias = int\n"),
        ),
        good=(("apps/swarm-speech/swarm_speech/watchdog.py",
               "from __future__ import annotations\nimport json, sys\n"
               "from pathlib import Path\n"),),
    ),
    Rule(
        id="voice-reads-no-personal-chatgpt-sign-in",
        why="the Voice it pool only (owner, 2026-11-02): no swarm-voice module names an "
            "OpenAI secret, refreshes a ChatGPT token or adds a secret version, and only "
            "planner.py touches Secret Manager (was tests/unit/voice/test_live_session.py)",
        roots=("apps/swarm-voice/swarm_voice",),
        check=_check_voice_sign_in,
        bad=(
            ("apps/swarm-voice/swarm_voice/credential.py", f'S = "{_TS}a-openai"\n'),
            ("apps/swarm-voice/swarm_voice/live.py", 'U = "https://auth.openai.com/t"\n'),
            ("apps/swarm-voice/swarm_voice/live.py", "x = body['refresh_token']\n"),
            ("apps/swarm-voice/swarm_voice/live.py", 'U = "v/1:addVersion"\n'),
            ("apps/swarm-voice/swarm_voice/live.py", "from google.cloud import secretmanager\n"),
            ("apps/swarm-voice/swarm_voice/main.py", f'S = "{_TS}x-key"\n'),
        ),
        good=(
            ("apps/swarm-voice/swarm_voice/planner.py",
             f"from google.cloud import secretmanager\nS = '{_TS}x-anthropic'\n"),
            ("apps/swarm-voice/swarm_voice/live.py", "token = pool.lease()\n"),
        ),
    ),
)
