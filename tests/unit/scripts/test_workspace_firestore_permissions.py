"""The workspace deployer holds exactly the Firestore permissions its job's calls need.

Found 2026-10-12: every swarm-workspace-apply execution for w-7f9285 died at
A1 on `POST :beginTransaction` -> 403 PERMISSION_DENIED. The custom role
swarmWorkspaceFirestore (terraform/bootstrap/workspace_deployer.tf) held
entities.create/get/list/update and databases.getMetadata, but Firestore checks
`datastore.databases.get` for beginTransaction and rollback, and nothing in the
role granted it. No execution had ever completed A1, so nothing had noticed.

This walks every Firestore call the job can make, two ways, and maps each to
the permission Google's table says Firestore checks for it
(https://cloud.google.com/firestore/docs/security/iam, "Permissions"):

* the calls `scripts/register-tenant.sh --workspace` makes: every `fs_*`
  helper and `fs_request` in the part of the script that runs before
  `workspace_main; exit 0`. Functions defined after that line do not exist
  when the job runs, so the operator path's calls (fs_database_exists, the
  tenant registration's reads) are not the job's.
* the calls the job's call guard admits: every Firestore `curl` shape in
  `scripts/lib/workspace-calls.json`. The guard refuses any call no shape
  matches, so this is the complete set the job CAN make, whatever the script
  grows into.

and holds the role to their union: a new call without its permission fails
here, and so does a permission no call needs.

Regex rather than python-hcl2, which is not a dependency
(test_tfvars_refusal_switches.py says the same).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEPLOYER_TF = REPO / "terraform" / "bootstrap" / "workspace_deployer.tf"
REGISTER = REPO / "scripts" / "register-tenant.sh"
CALLS = REPO / "scripts" / "lib" / "workspace-calls.json"

# Google's Firestore permission table, for the REST methods this repository's
# helpers reach. A method missing here fails the walk below rather than being
# assumed free.
REST_PERMISSIONS: dict[str, frozenset[str]] = {
    # "datastore.databases.get" is the transaction permission, not the
    # database metadata one (that is getMetadata, for projects.databases.get).
    "beginTransaction": frozenset({"datastore.databases.get"}),
    "rollback": frozenset({"datastore.databases.get"}),
    # A commit's update write needs update when it carries
    # currentDocument.exists=true, create when exists=false, both with no
    # precondition. The guard admits a commit whose writes are updates of a
    # named document with any precondition, so both. A delete write would need
    # entities.delete; the guard refuses one (C8, C9).
    "commit": frozenset({"datastore.entities.create", "datastore.entities.update"}),
    "runQuery": frozenset({"datastore.entities.get", "datastore.entities.list"}),
    "runAggregationQuery": frozenset({"datastore.entities.get", "datastore.entities.list"}),
    "get": frozenset({"datastore.entities.get"}),
    "list": frozenset({"datastore.entities.get", "datastore.entities.list"}),
    # PATCH with no currentDocument precondition is an upsert, which creates
    # a missing document: fs_patch on tenants/ and pools/ relies on that.
    "patch": frozenset({"datastore.entities.create", "datastore.entities.update"}),
    "delete": frozenset({"datastore.entities.delete"}),
    "databases.get": frozenset({"datastore.databases.getMetadata"}),
}

# common.sh's Firestore helpers, by the REST method each one sends.
HELPERS: dict[str, str] = {
    "fs_get": "get",
    "fs_patch": "patch",
    "fs_delete": "delete",
    "fs_list": "list",
    "fs_list_docs": "list",
    "fs_query": "runQuery",
    "fs_count": "runAggregationQuery",
    "fs_count_where": "runAggregationQuery",
    "fs_database_exists": "databases.get",
    "require_fs_database": "databases.get",
}

FS_REQUEST = re.compile(
    r'\bfs_request\s+(?P<verb>GET|POST|PATCH|DELETE)\s+"\$\(fs_base\)(?P<rest>[^"]*)"'
)
HELPER_CALL = re.compile(r"(?<![\w-])(" + "|".join(sorted(HELPERS, key=len, reverse=True)) + r")\b(?!\s*\(\))")


def firestore_role() -> frozenset[str]:
    text = DEPLOYER_TF.read_text()
    block = re.search(r"\n\s*workspace_firestore\s*=\s*\{(.*?)\n\s*\}\n", text, re.S)
    assert block, f"no workspace_firestore role in {DEPLOYER_TF}"
    perms = re.search(r"permissions\s*=\s*\[(.*?)\]", block.group(1), re.S)
    assert perms, "workspace_firestore has no permissions list"
    found = re.findall(r'"([a-z]+\.[A-Za-z.]+)"', perms.group(1))
    assert found, "workspace_firestore's permissions list parsed empty"
    return frozenset(found)


def workspace_part() -> list[tuple[int, str]]:
    """register-tenant.sh's lines up to the job's `workspace_main; exit 0`, comments dropped."""
    lines = REGISTER.read_text().splitlines()
    end = next(
        (i for i in range(len(lines) - 1) if lines[i].strip() == "workspace_main" and lines[i + 1].strip() == "exit 0"),
        None,
    )
    assert end is not None, "register-tenant.sh no longer ends --workspace with `workspace_main; exit 0`"
    return [(n + 1, line) for n, line in enumerate(lines[: end + 2]) if not line.lstrip().startswith("#")]


def rest_method(verb: str, rest: str) -> str:
    if rest.startswith(":"):
        return rest[1:]
    assert rest.startswith("/"), f"fs_base followed by {rest!r}"
    return {"GET": "get", "PATCH": "patch", "DELETE": "delete"}.get(verb, f"{verb} on a document")


def script_calls() -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    for n, line in workspace_part():
        code = line.split(" #", 1)[0]
        matched = list(FS_REQUEST.finditer(code))
        calls += [(n, rest_method(m["verb"], m["rest"])) for m in matched]
        if "fs_request" in code and not matched:
            raise AssertionError(f"register-tenant.sh:{n}: an fs_request this test cannot read: {line.strip()}")
        assert "firestore.googleapis.com" not in code, (
            f"register-tenant.sh:{n} calls Firestore without fs_request; route it through the helpers"
        )
        calls += [(n, HELPERS[m.group(1)]) for m in HELPER_CALL.finditer(code)]
    return calls


def guard_calls() -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for rule in json.loads(CALLS.read_text())["rules"]:
        for shape in rule.get("shapes", []):
            if shape.get("tool") != "curl":
                continue
            if "url" in shape:
                assert "firestore" not in shape["url"], f"{rule['id']} admits a Firestore URL the walk cannot map"
                continue
            if "op" in shape:
                method = shape["op"]
            else:
                assert "doc" in shape, f"{rule['id']}: a curl shape with neither url, op nor doc"
                method = rest_method(shape["method"], "/doc")
            calls.append((rule["id"], method))
    return calls


def required(calls: list[tuple[object, str]]) -> dict[str, set[str]]:
    need: dict[str, set[str]] = {}
    for where, method in calls:
        assert method in REST_PERMISSIONS, f"{where}: Firestore {method} is not in this test's permission table"
        for perm in REST_PERMISSIONS[method]:
            need.setdefault(perm, set()).add(f"{where} {method}")
    return need


def test_the_walk_saw_the_calls_it_was_written_for() -> None:
    # Empty output is not success: the regexes must find the A1 claim's calls.
    script = {m for _, m in script_calls()}
    guard = {m for _, m in guard_calls()}
    for method in ("runQuery", "beginTransaction", "get", "commit", "rollback", "patch"):
        assert method in script, f"the script walk found no {method}; found {sorted(script)}"
        assert method in guard, f"the guard walk found no {method}; found {sorted(guard)}"
    assert len(script_calls()) >= 15, f"only {len(script_calls())} Firestore calls found in register-tenant.sh"


def test_every_call_the_script_makes_has_its_permission() -> None:
    role = firestore_role()
    missing = {p: sorted(by) for p, by in required(script_calls()).items() if p not in role}
    assert not missing, f"swarmWorkspaceFirestore lacks what register-tenant.sh --workspace calls need: {missing}"


def test_every_call_the_guard_admits_has_its_permission() -> None:
    role = firestore_role()
    missing = {p: sorted(by) for p, by in required(guard_calls()).items() if p not in role}
    assert not missing, f"swarmWorkspaceFirestore lacks what workspace-calls.json admits: {missing}"


#: Granted but needed by no call. Owner decision 2026-10-12: the fix for the
#: A1 beginTransaction 403 was applied adds-only, so databases.getMetadata
#: stays until a separate change removes it. Nothing else may be added here
#: without the same kind of decision.
KEPT_UNUSED = frozenset({"datastore.databases.getMetadata"})


def test_the_role_holds_nothing_no_call_needs() -> None:
    need = set(required(script_calls())) | set(required(guard_calls()))
    extra = sorted(firestore_role() - need - KEPT_UNUSED)
    assert not extra, f"swarmWorkspaceFirestore grants {extra}, which no call of the workspace job needs"
    assert "datastore.entities.delete" not in firestore_role()
