"""Rules moved out of tests/unit/control_plane/test_a*.py to test_m*.py.

Each was a scan in one of those tests; its condition and its exempt paths are
the test's own, and its `why` names the test that keeps the behaviour beside
it. A rule over one module is rooted at that module's path, so renaming the
module must move the rule with it:
tests/unit/scripts/test_source_text_rules_control_plane_am.py holds that every
rule here still covers the files it was written for.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

from source_text_rules import Rule

Hit = tuple[int, str]

_API = "apps/swarm-api/swarm_api"


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _refuse(pattern: re.Pattern[str], message: str) -> Callable[..., list[Hit]]:
    """A check that hits every match of `pattern` in the file's text."""

    def check(path: str, text: str, tree: ast.Module | None) -> list[Hit]:
        return [(_line(text, m.start()), message) for m in pattern.finditer(text)]

    return check


# ------------------------------------------------ the MCP client's headers

_ADD_HEADER = re.compile(r"add_header\(\s*(?:\"([^\"]+)\"|([A-Za-z_.]+))")

#: `Accept-Encoding` is content negotiation (API-GZIP, archived issue 1822): it
#: asks for a gzip body and can raise nothing. `User-Agent` is `swarm-mcp/
#: <version> (<kind>)` (archived issue 2688): it names the client build for the
#: version-skew check and carries no credential, so it can raise nothing either.
_MCP_HEADERS = frozenset({
    "Authorization", "Accept", "Accept-Encoding", "Content-Type", "Content-Length",
    "TENANT_HEADER", "User-Agent",
})


def _check_mcp_headers(path: str, text: str, tree: ast.Module | None) -> list[Hit]:
    hits = []
    for m in _ADD_HEADER.finditer(text):
        name = m.group(1) or m.group(2).rsplit(".", 1)[-1]
        if name not in _MCP_HEADERS:
            hits.append((_line(text, m.start()),
                         f"the MCP sets {name}, a header beyond the caller's identity"))
    return hits


# ------------------------------------------- the dead artifacts subcollection

_ARTIFACTS_SUBCOLLECTION = re.compile(
    r"\.collection\(ARTIFACTS\)|\.collection\(\"artifacts\"\)")

# ----------------------------------------------------- the serialiser's claim

_STALE_NULLS = re.compile(re.escape("Null until the worker fix ships"))

# --------------------------------------------------- swarm-api and the worker

# Import statements only: several modules name agent_worker in prose,
# describing the worker's side of a shared format.
_IMPORTS_WORKER = re.compile(r"^\s*(?:from|import)\s+agent_worker\b", re.MULTILINE)

# ----------------------------------------------------- the AccountPool Protocol


def _check_account_pool(path: str, text: str, tree: ast.Module | None) -> list[Hit]:
    """Any `_call` in the text of a method of a class named AccountPool, its
    decorators and its comments included (`inspect.getsource` gave the test
    those too). A comment between two members is prose about the Protocol, and
    may name the call."""
    message = ("AccountPool is a Protocol: it declares the interface, and an implementer "
               "need not have a _call at all")
    lines = text.splitlines()
    if tree is None:
        return [(n, message) for n, line in enumerate(lines, 1) if "_call" in line]
    hits = []
    for pool in ast.walk(tree):
        if not (isinstance(pool, ast.ClassDef) and pool.name == "AccountPool"):
            continue
        for member in pool.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            first = min([member.lineno, *(d.lineno for d in member.decorator_list)])
            last = member.end_lineno or member.lineno
            # A comment indented under the method, after its last statement.
            while last < len(lines) and (
                    not lines[last].strip()
                    or len(lines[last]) - len(lines[last].lstrip()) > member.col_offset):
                last += 1
            hits.extend((n, message) for n in range(first, last + 1) if "_call" in lines[n - 1])
    return hits


# ------------------------------------------------------- corrected comments

#: The sentence an audit found false, by the file whose docstring or comment
#: stated it. test_comment_truth_177.py holds the true sentence that replaced
#: each.
_FALSE_SENTENCES: dict[str, tuple[str, ...]] = {
    "apps/quota-broker/quota_broker/credentials.py": ("~1,700 times per token",),
    "apps/quota-broker/quota_broker/accounts.py": ("the only moment it was free",),
    "apps/quota-broker/quota_broker/main.py": ("1,741 and 1,698",),
    "apps/reconciler/reconciler/store.py": ("Returns None rather than raising",),
    "apps/reconciler/reconciler/__main__.py": ("when any repair failed",),
    "apps/agent-worker/agent_worker/forge.py": ("unrecognised host", "no credential at all"),
}

#: What a space of a sentence is in the source: itself, or a line break with
#: the next line's indent and comment marker, so a wrapped sentence is one.
_FOLD = r"(?: |\s*\n\s*(?:#:?|//)?\s*)"


def _wrapped(sentence: str) -> str:
    return _FOLD.join(re.escape(word) for word in sentence.split(" "))


def _check_false_sentences(path: str, text: str, tree: ast.Module | None) -> list[Hit]:
    return [(_line(text, m.start()), f"states {sentence!r} again, which the code contradicts")
            for sentence in _FALSE_SENTENCES.get(path, ())
            for m in re.finditer(_wrapped(sentence), text)]


# ------------------------------------------------ one module free of a pattern

# `contract_review.MODE_ENV` by value, or the module's name.
_CONTRACT_REVIEW = re.compile(r"SWARM_CONTRACT_REVIEW|contract_review")
_API_WORKERS = re.compile("api_workers")
_WINDOW_LITERAL = re.compile(r"\b(?:max_wait_minutes|min_prs)\s*=\s*\d")

# -------------------------------------------------------------- forge slots

#: A slot read that names the tenant `-git` slot itself, bypassing the resolver.
_DIRECT_GIT_READ = re.compile(
    r"read_slot\(\s*[^,()]+,\s*(?:_?forge\.)?(?:GIT_PROVIDER|[\"']git[\"'])\s*[,)]")

# ------------------------------------------------------- grant attestations

_GRANT_FORM = re.compile(
    r"workspace_grant_key|FORM_VERSION|canonical(_of)?\([^)]*consumer"
    r"|\[\s*[\"']v1[\"']\s*,[^\]]*secret_id")
_GRANT_KEY_BUILT = re.compile("hmac|canonical")
_MAC_EQUALITY = re.compile(r"_mac\([^)]*\)\s*==|==\s*_mac\(")

# -------------------------------------------------------- the issue-run mark

_ENTERS_MARK = re.compile(r"issue_run_mark_allowed\(")

# ---------------------------------------------------- the one number rule


def _check_mask_literals(path: str, text: str, tree: ast.Module | None) -> list[Hit]:
    message = ("json_masking names _mask_literals: a number is masked by "
               "JsonMasker.number, the one rule both paths use")
    if tree is None:
        return [(_line(text, m.start()), message) for m in re.finditer("_mask_literals", text)]
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_mask_literals":
            hits.append((node.lineno, message))
        elif isinstance(node, ast.ImportFrom) and any(
                a.name == "_mask_literals" for a in node.names):
            hits.append((node.lineno, message))
    return hits


RULES = (
    Rule(
        id="mcp-headers-are-the-callers",
        why="no key acts as an admin for a non-admin: the MCP sends the caller's bearer token, "
            "the workspace the API checks, or content negotiation "
            "(test_admin_surface_refuses_non_admins.py)",
        roots=("apps/swarm-mcp/swarm_mcp",),
        suffixes=(".py",),
        requires="add_header(",
        check=_check_mcp_headers,
        bad=(
            ("apps/swarm-mcp/swarm_mcp/a.py", 'req.add_header("X-Swarm-Admin", "1")\n'),
            ("apps/swarm-mcp/swarm_mcp/b.py", "req.add_header(client.ADMIN_HEADER, key)\n"),
            ("apps/swarm-mcp/swarm_mcp/c.py", 'req.add_header(\n    "Cookie", value)\n'),
        ),
        good=(
            ("apps/swarm-mcp/swarm_mcp/a.py",
             'req.add_header("Authorization", bearer)\n'
             "req.add_header(client_module.TENANT_HEADER, tenant)\n"
             "req.add_header(TENANT_HEADER, tenant)\n"
             'req.add_header("Accept-Encoding", "gzip")\n'
             'req.add_header("User-Agent", agent)\n'),
        ),
    ),
    Rule(
        id="no-task-artifacts-subcollection",
        why="nothing writes tasks/<id>/artifacts; the manifest is "
            "task.result_summary['artifacts'] (test_artifacts_read_path.py)",
        roots=("apps",),
        suffixes=(".py",),
        requires=_ARTIFACTS_SUBCOLLECTION,
        check=_refuse(_ARTIFACTS_SUBCOLLECTION,
                      "nothing writes tasks/<id>/artifacts; read "
                      "task.result_summary['artifacts']"),
        bad=(
            ("apps/swarm-api/swarm_api/a.py", "ref.collection(ARTIFACTS).stream()\n"),
            ("apps/reconciler/reconciler/a.py", 'ref.collection("artifacts").stream()\n'),
        ),
        good=(
            ("apps/swarm-api/swarm_api/a.py", 'task.result_summary["artifacts"]\n'),
            ("apps/swarm-api/swarm_api/b.py", "ref.collection(ATTEMPTS).stream()\n"),
        ),
    ),
    Rule(
        id="spend-nulls-await-no-worker-fix",
        why="the worker fix shipped, and the stale comment is why the cost columns were removed "
            "from the UI (test_attempt_spend_round_trip.py)",
        roots=(_API,),
        suffixes=(".py",),
        requires=_STALE_NULLS,
        check=_refuse(_STALE_NULLS, "the spend fields are not null pending a worker fix: "
                                    "it shipped"),
        bad=((f"{_API}/codec.py", "# Null until the worker fix ships.\n"),),
        good=((f"{_API}/codec.py", "# Null when the attempt was never measured.\n"),),
    ),
    Rule(
        id="swarm-api-imports-no-worker",
        why="only the tenant's worker may read its git secret, so swarm-api holds no path to one "
            "(owner decision 2026-10-02, D13; test_branches_carrier_read_only.py)",
        roots=(_API,),
        suffixes=(".py",),
        requires=_IMPORTS_WORKER,
        check=_refuse(_IMPORTS_WORKER, "swarm_api must not import agent_worker"),
        bad=(
            (f"{_API}/a.py", "from agent_worker import forge\n"),
            (f"{_API}/b.py", "def f():\n    import agent_worker.forge\n"),
            (f"{_API}/routes/c.py", "from agent_worker.forge import probe_repository\n"),
        ),
        good=(
            (f"{_API}/a.py", "# agent_worker.inputs reads the same manifest\n"),
            (f"{_API}/b.py", "from agent_worker_shapes import X\n"),
        ),
    ),
    Rule(
        id="account-pool-protocol-only-declares",
        why="BrokerClient's broken sign-in methods were pasted from bodies AccountPool should "
            "not have had (test_broker_client_signatures.py)",
        roots=(_API,),
        suffixes=(".py",),
        requires="class AccountPool",
        check=_check_account_pool,
        bad=(
            (f"{_API}/brokerclient.py",
             "class AccountPool(Protocol):\n"
             "    def refresh(self, account_id):\n"
             '        return self._call("POST", account_id)\n'),
            (f"{_API}/brokerclient.py",
             "class AccountPool(Protocol):\n"
             "    def remove(self, account_id):\n"
             "        ...\n"
             "        # self._call answers it\n"),
            (f"{_API}/brokerclient.py", "class AccountPool(:\n    self._call()\n"),
        ),
        good=(
            (f"{_API}/brokerclient.py",
             "class AccountPool(Protocol):\n"
             "    # Once a body that called self._call.\n"
             "    def refresh(self, account_id): ...\n\n"
             "class BrokerClient:\n"
             "    def refresh(self, account_id):\n"
             '        return self._call("POST", account_id)\n'),
        ),
    ),
    Rule(
        id="corrected-comment-stays-corrected",
        why="each sentence contradicted the code next to it (archived issue 177; "
            "test_comment_truth_177.py holds what replaced it)",
        roots=tuple(_FALSE_SENTENCES),
        suffixes=(".py",),
        check=_check_false_sentences,
        requires=re.compile("|".join(
            _wrapped(s) for group in _FALSE_SENTENCES.values() for s in group)),
        bad=(
            *((path, f'"""It is {sentence}."""\n')
              for path, group in _FALSE_SENTENCES.items() for sentence in group),
            ("apps/quota-broker/quota_broker/main.py", "# it read 1,741 and\n#: 1,698 times\n"),
            ("apps/reconciler/reconciler/store.py",
             'def f():\n    """Returns None\n    rather than raising."""\n'),
        ),
        good=(
            ("apps/quota-broker/quota_broker/credentials.py",
             '"""Written about 60 times per token."""\n'),
            ("apps/reconciler/reconciler/__main__.py", '"""1 when `report.errors` is set."""\n'),
            # The sentence is false of forge.py's None, not of every module.
            ("apps/quota-broker/quota_broker/main.py", "# no credential at all\n"),
        ),
    ),
    Rule(
        id="contract-review-mode-is-not-a-refusals-switch",
        why="the contract review's mode is its own setting, outside refusals.py, whose switch is "
            "keyed on the code (answer 7; test_contract_review_submission.py)",
        roots=(f"{_API}/refusals.py",),
        suffixes=(".py",),
        requires=_CONTRACT_REVIEW,
        check=_refuse(_CONTRACT_REVIEW, "refusals.py must not name the contract review"),
        bad=(
            (f"{_API}/refusals.py", 'MODE = os.environ.get("SWARM_CONTRACT_REVIEW")\n'),
            (f"{_API}/refusals.py", "from . import contract_review\n"),
        ),
        good=((f"{_API}/refusals.py", "SWITCHES = {'invalid_dispatch': 'enforce'}\n"),),
    ),
    Rule(
        id="installer-renders-no-api-workers",
        why="a fresh deployment needs no setting: infra's default of 1 is today's one worker "
            "(the owner, 2026-11-10; test_dev_api_workers_tfvars.py)",
        roots=("apps/swarm-install/swarm_install",),
        suffixes=(".py",),
        requires=_API_WORKERS,
        check=_refuse(_API_WORKERS, "the installer renders no api_workers; infra's default is 1"),
        bad=(("apps/swarm-install/swarm_install/tfvars.py",
              'lines.append("api_workers = 1")\n'),),
        good=(("apps/swarm-install/swarm_install/tfvars.py",
               'lines.append("environment = dev")\n'),),
    ),
    Rule(
        id="git-slot-read-through-the-resolver",
        why="one resolver decides whose git credential a request reads; a direct read of the "
            "tenant `-git` slot bypasses it (test_forge_slot_resolution.py)",
        roots=(_API,),
        suffixes=(".py",),
        requires=_DIRECT_GIT_READ,
        check=_refuse(_DIRECT_GIT_READ, "read the git slot through the resolver, never by name"),
        bad=(
            (f"{_API}/a.py", "self.read_slot(tenant, GIT_PROVIDER).value\n"),
            (f"{_API}/routes/b.py", 'tokens.read_slot(tenant, "git")\n'),
            (f"{_API}/c.py", "tokens.read_slot(\n    tenant,\n    forge.GIT_PROVIDER,\n)\n"),
        ),
        good=(
            (f"{_API}/a.py", "self.read_slot(tenant, slot).value\n"),
            (f"{_API}/b.py", 'tokens.read_slot(tenant, "github-app")\n'),
        ),
    ),
    Rule(
        id="grant-form-stated-once",
        why="no module but grant_attest.py builds the grant's MAC input: none names the grant "
            "key's setting, makes an HMAC over a grant or spells the form "
            "(test_grant_attest.py); settings.py declares the key's fields only",
        roots=(_API,),
        suffixes=(".py",),
        exempt=frozenset({f"{_API}/grant_attest.py", f"{_API}/settings.py"}),
        requires=_GRANT_FORM,
        check=_refuse(_GRANT_FORM, "the grant's canonical form and key belong to "
                                   "grant_attest.py alone"),
        bad=(
            (f"{_API}/a.py", "key = settings.workspace_grant_key\n"),
            (f"{_API}/b.py", "from .grant_attest import FORM_VERSION\n"),
            (f"{_API}/c.py", "mac = canonical_of(tenant, consumer)\n"),
            (f"{_API}/d.py", "x = canonical(\n    tenant,\n    consumer)\n"),
            (f"{_API}/e.py", 'x = ["v1", tenant_id, secret_id]\n'),
        ),
        good=(
            (f"{_API}/a.py", "keys.check(grant)\n"),
            (f"{_API}/b.py", "x = canonical(doc)\n"),
            (f"{_API}/c.py", 'x = ["v1", tenant_id]\n'),
        ),
    ),
    Rule(
        id="grant-key-settings-build-nothing",
        why="settings.py, exempt from grant-form-stated-once, declares the grant key's fields "
            "and builds nothing from them (test_grant_attest.py)",
        roots=(f"{_API}/settings.py",),
        suffixes=(".py",),
        requires=_GRANT_KEY_BUILT,
        check=_refuse(_GRANT_KEY_BUILT, "settings.py declares the grant key; grant_attest.py "
                                        "makes every MAC"),
        bad=(
            (f"{_API}/settings.py", "import hmac\n"),
            (f"{_API}/settings.py", "x = canonical(grant)\n"),
        ),
        good=((f"{_API}/settings.py", 'workspace_grant_key: str = field(default="")\n'),),
    ),
    Rule(
        id="grant-mac-compared-in-constant-time",
        why="an attestation is compared by hmac.compare_digest, never by == "
            "(test_grant_attest.py)",
        roots=(f"{_API}/grant_attest.py",),
        suffixes=(".py",),
        requires=_MAC_EQUALITY,
        check=_refuse(_MAC_EQUALITY, "compare a MAC with hmac.compare_digest"),
        bad=(
            (f"{_API}/grant_attest.py", "ok = _mac(key, form) == presented\n"),
            (f"{_API}/grant_attest.py", "ok = presented == _mac(key, form)\n"),
        ),
        good=((f"{_API}/grant_attest.py",
               "ok = hmac.compare_digest(_mac(key, form), presented)\n"),),
    ),
    Rule(
        id="issue-run-mark-entered-by-issueruns",
        why="metadata.issue_run is written only by the issue-run code (the owner, 2026-11-04): "
            "issueruns.submitting alone enters the exemption validation.py defines "
            "(test_issue_run_reserved.py)",
        roots=(_API,),
        suffixes=(".py",),
        exempt=frozenset({f"{_API}/issueruns.py", f"{_API}/validation.py"}),
        requires=_ENTERS_MARK,
        check=_refuse(_ENTERS_MARK, "submit through issueruns.submitting; only it enters "
                                    "issue_run_mark_allowed"),
        bad=(
            (f"{_API}/schedules.py", "with issue_run_mark_allowed(run_id):\n    pass\n"),
            (f"{_API}/routes/runs.py", "validation.issue_run_mark_allowed(run_id)\n"),
        ),
        good=((f"{_API}/routes/runs.py", "with issueruns.submitting(run_id):\n    pass\n"),),
    ),
    Rule(
        id="json-masking-keeps-no-number-rule",
        why="both paths mask a number through JsonMasker.number (archived issue 387; "
            "test_learned_number_literal_387.py); the copy json_masking kept disagreed with it",
        roots=(f"{_API}/json_masking.py",),
        suffixes=(".py",),
        requires="_mask_literals",
        check=_check_mask_literals,
        bad=(
            (f"{_API}/json_masking.py", "from .redaction import _mask_literals\n"),
            (f"{_API}/json_masking.py", "text, count = _mask_literals(text, literals)\n"),
            (f"{_API}/json_masking.py", "def f():\n    from .redaction import a, _mask_literals\n"),
        ),
        good=(
            (f"{_API}/json_masking.py", "count = masker.number(text)\n"),
            (f"{_API}/json_masking.py", '"""JsonMasker calls _mask_literals."""\n'),
        ),
    ),
    Rule(
        id="merge-train-window-has-one-source",
        why="the gather window is merge_train_gather.json's, read by the script and the API "
            "both; trainboard.py states no second copy (test_merge_train_board_on_time.py)",
        roots=(f"{_API}/trainboard.py",),
        suffixes=(".py",),
        requires=_WINDOW_LITERAL,
        check=_refuse(_WINDOW_LITERAL, "read the window from merge_train_gather.json"),
        bad=(
            (f"{_API}/trainboard.py", "max_wait_minutes = 5\n"),
            (f"{_API}/trainboard.py", "Gather(min_prs=8, max_wait_minutes=w)\n"),
        ),
        good=((f"{_API}/trainboard.py",
               'Gather(min_prs=data["min_prs"], max_wait_minutes=data["max_wait_minutes"])\n'),),
    ),
)
