"""Every cloud call the workspace job makes is one swarm-workspace-deployer may make.

Found 2026-10-13 01:40Z: after WS-FIRESTORE, w-7f9285's execution
swarm-workspace-apply-trhmh got through A1-A4 and died at A5 on
`gcloud storage cp - gs://<artifact bucket>/tenants/u-devops/.tenant` ->
403 "does not have storage.objects.get access". The deployer holds
storage.objects.create on tenants/ (roles/storage.objectCreator, conditioned)
and nothing that reads an object, and `gcloud storage cp` reads the
destination before it uploads. No execution had ever completed create mode,
so nothing had exercised A5-A9.

This is the static walk that would have caught it, the sibling of
test_workspace_firestore_permissions.py (which owns the Firestore calls):

* the calls the job's code makes: every gcloud command and every raw curl in
  the code `register-tenant.sh --workspace` runs -- the script up to its
  `workspace_main; exit 0`, scripts/lib/forge-slot.sh (A6), and the
  kubernetes/ scripts A7 runs (apply.sh, cluster-network.sh, and
  scripts/lib/check-cluster-network-parity.sh after a tenant apply). A gcloud
  command apply.sh makes only in another mode is a C0 known_unused entry in
  scripts/lib/workspace-calls.json, which says why the job never runs it.
* the calls the job's guard admits: every shape in workspace-calls.json, the
  WD9 fallback's included, so the grants are proven before the switch is.

Each call is mapped to the IAM permissions Google checks for it and to the
kind of resource it is checked on; each permission must be held by a grant in
terraform/bootstrap/workspace_deployer.tf whose role carries it and whose
scope (project, the artifact bucket, or a condition) reaches that resource.
A call this test cannot map fails it, so a new call cannot arrive without its
grant being looked at. And the guard admits nothing the job does not make:
a shape that reaches a cloud API and matches no call of the job is refused here.

Kubernetes RBAC is held by test_register_tenant_workspace.py's
test_the_deployer_rbac_is_exactly_what_a_run_asks_for, against the calls a
full run makes; this walk holds only the IAM half of a cluster call (GKE
authenticates a caller only if it holds container.clusters.get).

Regex rather than python-hcl2, which is not a dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
REGISTER = REPO / "scripts" / "register-tenant.sh"
FORGE_SLOT = REPO / "scripts" / "lib" / "forge-slot.sh"
APPLY = REPO / "kubernetes" / "apply.sh"
CLUSTER_NETWORK = REPO / "kubernetes" / "cluster-network.sh"
PARITY = REPO / "scripts" / "lib" / "check-cluster-network-parity.sh"
GUARD = REPO / "scripts" / "lib" / "workspace-guard.sh"
CALLS = REPO / "scripts" / "lib" / "workspace-calls.json"
DEPLOYER_TF = REPO / "terraform" / "bootstrap" / "workspace_deployer.tf"
FORGE_SLOTS_TF = REPO / "terraform" / "bootstrap" / "forge_user_slots.tf"
VARIABLES_TF = REPO / "terraform" / "bootstrap" / "variables.tf"

RULES = json.loads(CALLS.read_text())

# ---------------------------------------------------------------------------
# What each call needs: (permissions, the kind of resource they are checked on)
# ---------------------------------------------------------------------------
#
# Resource kinds:
#   local          nothing leaves the machine (or only the metadata server)
#   project        the project itself
#   account        a service account in the project (the worker's, or the
#                  scheduler's and reconciler's uniqueId reads in apply.sh)
#   project-iam    the project's allow policy, for one of the worker's three
#                  project roles (WD9 fallback)
#   bucket         the artifact bucket
#   marker         the object tenants/<tenant>/.tenant in the artifact bucket
#   slot           a secret named swarm-tenant-u-* (the forge slot and its twin)
#   cluster        the swarm Autopilot cluster
#   front-door     the API backend's IAP policy, which a service workspace's S2
#                  admits its account on (docs/design/workspace-kinds.md §4.5)

NONE: frozenset[str] = frozenset()

GCLOUD: dict[tuple[str, ...], tuple[frozenset[str], str]] = {
    ("auth", "print-access-token"): (NONE, "local"),
    ("auth", "list"): (NONE, "local"),
    ("config", "get-value"): (NONE, "local"),
    ("config", "config-helper"): (NONE, "local"),
    ("iam", "service-accounts", "describe"): (frozenset({"iam.serviceAccounts.get"}), "account"),
    ("iam", "service-accounts", "get-iam-policy"): (frozenset({"iam.serviceAccounts.getIamPolicy"}), "account"),
    # The squat inspection (#334): A2 on an account that already exists, and
    # A9 on every run. Key ids and validity only; never key material.
    ("iam", "service-accounts", "keys", "list"): (frozenset({"iam.serviceAccountKeys.list"}), "account"),
    ("iam", "service-accounts", "list"): (frozenset({"iam.serviceAccounts.list"}), "project"),
    ("iam", "service-accounts", "create"): (frozenset({"iam.serviceAccounts.create"}), "project"),
    ("iam", "service-accounts", "add-iam-policy-binding"): (
        frozenset({"iam.serviceAccounts.getIamPolicy", "iam.serviceAccounts.setIamPolicy"}), "account"),
    ("iam", "roles", "describe"): (frozenset({"iam.roles.get"}), "project"),
    ("projects", "get-iam-policy"): (frozenset({"resourcemanager.projects.getIamPolicy"}), "project"),
    ("projects", "add-iam-policy-binding"): (
        frozenset({"resourcemanager.projects.getIamPolicy", "resourcemanager.projects.setIamPolicy"}), "project-iam"),
    ("storage", "buckets", "describe"): (frozenset({"storage.buckets.get"}), "bucket"),
    ("storage", "buckets", "get-iam-policy"): (frozenset({"storage.buckets.getIamPolicy"}), "bucket"),
    ("storage", "buckets", "add-iam-policy-binding"): (
        frozenset({"storage.buckets.getIamPolicy", "storage.buckets.setIamPolicy"}), "bucket"),
    # `gcloud storage cp - gs://...` resolves its destination with a GET of the
    # object before it uploads, and no flag skips that read: w-7f9285's A5,
    # 2026-10-13 01:40Z, 403 on storage.objects.get. Mapped so a cp that comes
    # back to the job's path is judged, not missed.
    ("storage", "cp"): (frozenset({"storage.objects.get", "storage.objects.create"}), "marker"),
    ("secrets", "describe"): (frozenset({"secretmanager.secrets.get"}), "slot"),
    ("secrets", "get-iam-policy"): (frozenset({"secretmanager.secrets.getIamPolicy"}), "slot"),
    ("secrets", "versions", "list"): (frozenset({"secretmanager.versions.list"}), "slot"),
    ("secrets", "create"): (frozenset({"secretmanager.secrets.create"}), "slot"),
    ("secrets", "add-iam-policy-binding"): (
        frozenset({"secretmanager.secrets.getIamPolicy", "secretmanager.secrets.setIamPolicy"}), "slot"),
    ("container", "clusters", "describe"): (frozenset({"container.clusters.get"}), "cluster"),
    ("container", "clusters", "get-credentials"): (frozenset({"container.clusters.get"}), "cluster"),
    # S2 and A9 of a service workspace (§4.3): IAP's own IAM on one backend
    # service, `projects/<number>/iap_web/compute/services/<id>`.
    ("iap", "web", "get-iam-policy"): (frozenset({"iap.webServices.getIamPolicy"}), "front-door"),
    ("iap", "web", "add-iam-policy-binding"): (
        frozenset({"iap.webServices.getIamPolicy", "iap.webServices.setIamPolicy"}), "front-door"),
}

#: GKE authenticates a kubectl caller only if it holds container.clusters.get
#: on the cluster; what it may do inside is RBAC (provisioner-rbac.yaml).
KUBECTL_CLUSTER = (frozenset({"container.clusters.get"}), "cluster")

#: A JSON API upload with ifGenerationMatch=0 is a create that can never
#: replace: storage.objects.create alone. Without the precondition an existing
#: object would be overwritten, which needs storage.objects.delete as well.
MARKER_UPLOAD = (frozenset({"storage.objects.create"}), "marker")
MARKER_UPLOAD_HOST = "https://storage.googleapis.com/upload/storage/v1/b/"
METADATA_SERVER_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

# ---------------------------------------------------------------------------
# What the deployer holds
# ---------------------------------------------------------------------------

#: The permissions of the predefined roles the deployer holds that this walk
#: needs, from Google's role reference. A subset, never the whole role: an
#: entry here is a claim about Google's role, so it says no more than is used.
PREDEFINED: dict[str, frozenset[str]] = {
    "roles/container.clusterViewer": frozenset({"container.clusters.get", "container.clusters.list"}),
    "roles/storage.objectCreator": frozenset({"storage.objects.create"}),
    "roles/resourcemanager.projectIamAdmin": frozenset({
        "resourcemanager.projects.getIamPolicy", "resourcemanager.projects.setIamPolicy"}),
    "roles/logging.logWriter": frozenset({"logging.logEntries.create"}),
}

#: Every resource kind a project-level grant with no condition reaches.
PROJECT_WIDE = frozenset({"project", "account", "project-iam", "bucket", "marker", "slot", "cluster", "front-door"})

#: What each condition in workspace_deployer_conditions lets a grant reach, and
#: a fragment its expression must carry for that to be true.
CONDITIONS: dict[str, tuple[frozenset[str], str]] = {
    "secret_binder": (frozenset({"slot"}), "workspace_personal_secret_prefix"),
    "cluster_viewer": (frozenset({"cluster"}), "workspace_cluster_name"),
    "project_iam_admin": (frozenset({"project-iam"}), "modifiedGrantsByRole"),
    "object_creator": (frozenset({"marker"}), "/objects/tenants/"),
}


def _uncommented(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _block(text: str, opener: str) -> str:
    """The braces-balanced body after OPENER (a regex ending at its `{`)."""
    match = re.search(opener, text)
    assert match, f"{opener!r} not found"
    depth, i = 1, match.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[match.end(): i - 1]


def _permissions(body: str) -> frozenset[str]:
    perms = re.search(r"permissions\s*=\s*\[(.*?)\]", body, re.S)
    assert perms, f"no permissions list in {body[:200]!r}"
    found = frozenset(re.findall(r'"([a-z]+\.[A-Za-z]+\.[A-Za-z]+)"', perms.group(1)))
    assert found, "a permissions list parsed empty"
    return found


def custom_roles() -> dict[str, frozenset[str]]:
    text = _uncommented(DEPLOYER_TF.read_text())
    body = _block(text, r"workspace_custom_roles\s*=\s*\{")
    roles: dict[str, frozenset[str]] = {}
    for match in re.finditer(r"\n\s{4}(\w+)\s*=\s*\{", body):
        roles[match.group(1)] = _permissions(_block(body, re.escape(match.group(0))))
    forge = _block(_uncommented(FORGE_SLOTS_TF.read_text()),
                   r'resource\s+"google_project_iam_custom_role"\s+"forge_slot_creator"\s*\{')
    roles["forge_slot_creator"] = _permissions(forge)
    assert {"workspace_account_admin", "workspace_bucket_iam", "workspace_firestore"} <= set(roles), sorted(roles)
    return roles


def role_permissions(role: str) -> frozenset[str]:
    custom = re.fullmatch(r"module\.custom_role_ids\.names\.(\w+)", role)
    if custom:
        return custom_roles()[custom.group(1)]
    assert role in PREDEFINED, f"the deployer holds {role}, whose permissions this test does not list"
    return PREDEFINED[role]


def conditions() -> dict[str, str]:
    text = _uncommented(DEPLOYER_TF.read_text())
    body = _block(text, r"workspace_deployer_conditions\s*=\s*\{")
    found = dict(re.findall(r'\n\s*(\w+)\s*=\s*("[^\n]*")', body))
    assert set(found) == set(CONDITIONS), f"conditions {sorted(found)}; this test knows {sorted(CONDITIONS)}"
    return found


def roles_on() -> set[str]:
    text = VARIABLES_TF.read_text()
    body = _block(text, r'variable\s+"workspace_deployer_roles"\s*\{')
    default = re.search(r"default\s*=\s*\[(.*?)\]", body, re.S)
    assert default, "workspace_deployer_roles has no default"
    return set(re.findall(r'"([^"]+)"', _uncommented(default.group(1))))


def _scope(condition_key: str | None, base: frozenset[str]) -> frozenset[str]:
    if condition_key is None:
        return base
    reach, fragment = CONDITIONS[condition_key]
    assert fragment in conditions()[condition_key], (
        f"workspace_deployer_conditions.{condition_key} no longer carries {fragment!r}; "
        "re-read what it lets the grant reach")
    return reach & base


def grants() -> list[dict]:
    """Every grant the deployer holds with the default role list, as
    {name, permissions, reach}."""
    text = _uncommented(DEPLOYER_TF.read_text())
    on = roles_on()
    found: list[dict] = []
    body = _block(text, r"workspace_project_grants\s*=\s*\{")
    for match in re.finditer(r'\n\s*"([^"]+)"\s*=\s*\{', body):
        entry = _block(body, re.escape(match.group(0)))
        role = re.search(r"\brole\s*=\s*(\S+)", entry)
        assert role, f"grant {match.group(1)} names no role"
        cond = re.search(r"local\.workspace_deployer_conditions\.(\w+)", entry)
        if match.group(1) in on:
            found.append({"name": match.group(1), "permissions": role_permissions(role.group(1).strip('"')),
                          "reach": _scope(cond and cond.group(1), PROJECT_WIDE)})
    for resource in ("workspace_deployer_bucket_iam", "workspace_deployer_marker"):
        entry = _block(text, rf'resource\s+"google_storage_bucket_iam_member"\s+"{resource}"\s*\{{')
        gate = re.search(r'lookup\(local\.workspace_role_on,\s*"([^"]+)"', entry)
        role = re.search(r"\brole\s*=\s*(\S+)", entry)
        assert gate and role, f"{resource}: no gate or role"
        assert "local.workspace_artifact_bucket" in entry, f"{resource} is not on the artifact bucket"
        cond = re.search(r"local\.workspace_deployer_conditions\.(\w+)", entry)
        if gate.group(1) in on:
            found.append({"name": f"{resource} ({gate.group(1)})",
                          "permissions": role_permissions(role.group(1).strip('"')),
                          "reach": _scope(cond and cond.group(1), frozenset({"bucket", "marker"}))})
    assert len(found) >= 10, f"only {len(found)} grants parsed: {[g['name'] for g in found]}"
    found.append(front_door_grant())
    found.append(front_door_reader_grant())
    return found


def front_door_grant() -> dict:
    """K1 (§4.5): swarmFrontDoorAdmitter on the API backend alone. Counted as
    held because it is what the deployer holds once the owner has turned the
    kinds on (enable_workspace_kinds, and the role added to
    workspace_deployer_roles after O1); without it S2's call is refused by IAP
    and the run stops for the owner as FRONT_DOOR_OWNER, which is the design's
    fallback, not a gap. Its reach is "front-door" only if its condition is an
    EXACT match on the number-form name of one backend."""
    text = _uncommented(DEPLOYER_TF.read_text())
    role = _block(text, r'resource\s+"google_project_iam_custom_role"\s+"front_door_admitter"\s*\{')
    gate = re.search(r'lookup\(local\.workspace_role_on,\s*"([^"]+)"', role)
    assert gate and gate.group(1) == "swarmFrontDoorAdmitter", "K1's role is not gated on its own role name"
    reviewed = _block(VARIABLES_TF.read_text(), r'variable\s+"workspace_deployer_roles"\s*\{')
    assert '"swarmFrontDoorAdmitter"' in reviewed, "K1's role is not in the deployer's reviewed role list"
    member = _block(text, r'resource\s+"google_project_iam_member"\s+"front_door_admitter"\s*\{')
    assert "local.workspace_kinds_conditions.front_door_admit" in member, "K1 is not bound by its condition"
    return {"name": "K1 swarmFrontDoorAdmitter", "permissions": _permissions(role),
            "reach": frozenset({"front-door"}) if front_door_condition_is_exact() else frozenset()}


def front_door_reader_grant() -> dict:
    """FRONT-DOOR-READ (part of #847): swarmFrontDoorReader, the read alone,
    bound on the API backend's own IAP policy for the deployer. It is what the
    deployer holds while the kinds are on and K1 is not, which is the shape
    O1's failure left (docs/workspaces.md §0). Its reach is "front-door" only
    if the binding is on the API backend by name, for the deployer, with no
    condition; anything else reaches nothing here."""
    text = _uncommented(DEPLOYER_TF.read_text())
    role = _block(text, r'resource\s+"google_project_iam_custom_role"\s+"front_door_reader"\s*\{')
    assert 'module.custom_role_ids.ids["front_door_reader"]' in role, "the reader's id is not custom_role_ids'"
    member = _block(text, r'resource\s+"google_iap_web_backend_service_iam_member"\s+"front_door_reader"\s*\{')
    name = re.search(r"\n\s*front_door_api_backend_name\s*=\s*([^\n]*)", text)
    on_api = (re.search(r"\n\s*web_backend_service\s*=\s*local\.front_door_api_backend_name\s*\n", member)
              and name and name.group(1).strip().endswith(
                  "? data.google_compute_backend_service.frontend_iap[local.front_door_api_backend].name : \"\""))
    exact = (bool(on_api)
             and re.search(r"\n\s*member\s*=\s*local\.workspace_deployer_member\s*\n", member) is not None
             and re.search(r"\n\s*condition\s*\{", member) is None)
    return {"name": "swarmFrontDoorReader", "permissions": _permissions(role),
            "reach": frozenset({"front-door"}) if exact else frozenset()}


def front_door_condition_is_exact() -> bool:
    """K1's condition names one IAP backend by `==`, never a prefix, in the
    number form IAP evaluates (§2.1: any other form matched nothing)."""
    text = _uncommented(DEPLOYER_TF.read_text())
    cond = re.search(r'\n\s*front_door_admit\s*=\s*join\(" && ",\s*concat\(\s*\[(.*?)\]', text, re.S)
    resource = re.search(r'\n\s*front_door_api_resource\s*=\s*([^\n]*)', text)
    if not (cond and resource):
        return False
    return (re.fullmatch(r'\s*"resource\.name == \\"\$\{local\.front_door_api_resource\}\\""\s*', cond.group(1))
            is not None
            and "startsWith" not in cond.group(1)
            and "projects/${local.workspace_project_number}/iap_web/compute/services/" in resource.group(1)
            and ".generated_id" in resource.group(1))


def missing(calls: list[tuple[str, tuple[frozenset[str], str]]],
            held: list[dict] | None = None) -> dict[str, list[str]]:
    held = grants() if held is None else held
    gaps: dict[str, list[str]] = {}
    for where, (perms, kind) in calls:
        for perm in sorted(perms):
            if not any(perm in g["permissions"] and kind in g["reach"] for g in held):
                gaps.setdefault(f"{perm} on {kind}", []).append(where)
    return gaps


# ---------------------------------------------------------------------------
# The job's calls, from its code
# ---------------------------------------------------------------------------

GCLOUD_GROUPS = ("auth", "config", "container", "iam", "identity", "projects", "secrets", "storage", "compute", "run",
                 "iap")


def _code(text: str) -> str:
    return re.sub(r"\\\n\s*", " ", _uncommented(text))


def job_code() -> dict[str, str]:
    lines = REGISTER.read_text().splitlines()
    end = next((i for i in range(len(lines) - 1)
                if lines[i].strip() == "workspace_main" and lines[i + 1].strip() == "exit 0"), None)
    assert end is not None, "register-tenant.sh no longer ends --workspace with `workspace_main; exit 0`"
    code = {"scripts/register-tenant.sh --workspace": _code("\n".join(lines[: end + 2]))}
    for path in (FORGE_SLOT, APPLY, CLUSTER_NETWORK, PARITY):
        code[str(path.relative_to(REPO))] = _code(path.read_text())
    return code


def known_unused() -> set[tuple[str, ...]]:
    return {tuple(u["command"]) for r in RULES["rules"] for u in r.get("known_unused", []) if u["tool"] == "gcloud"}


def script_gcloud() -> list[tuple[str, tuple[str, ...]]]:
    found = []
    for where, code in job_code().items():
        for match in re.finditer(r"(?<![\w/.$-])gcloud((?:[ \t]+[a-z][a-z0-9-]*)+)", code):
            words = tuple(match.group(1).split())
            if words and words[0] in GCLOUD_GROUPS and words not in known_unused():
                found.append((where, words))
    return found


def script_curl() -> list[tuple[str, str]]:
    """Every raw curl on the job's path, by the URL it names (fs_request's own
    curl is common.sh's and test_workspace_firestore_permissions.py's)."""
    found = []
    for where, code in job_code().items():
        for line in code.splitlines():
            if re.search(r"(?:^|[|;&(])\s*curl\s+-", line):
                url = re.search(r'"\$\{(\w+)\}"\s*\)?"?\s*(?:\|\||$|\))', line) or re.search(r'"(https?://[^"]+)"', line)
                found.append((where, url.group(1) if url else line.strip()))
    return found


def script_calls() -> list[tuple[str, tuple[frozenset[str], str]]]:
    calls = []
    for where, words in script_gcloud():
        assert words in GCLOUD, f"{where}: gcloud {' '.join(words)} is a call this walk cannot map"
        calls.append((f"{where}: gcloud {' '.join(words)}", GCLOUD[words]))
    for where, url in script_curl():
        assert url == "WS_MARKER_UPLOAD_URL", f"{where}: a curl to {url} this walk cannot map"
        calls.append((f"{where}: curl POST marker upload", MARKER_UPLOAD))
    return calls


# ---------------------------------------------------------------------------
# The job's calls, from its guard
# ---------------------------------------------------------------------------


def guard_calls(include_local: bool = True, rules: dict | None = None) -> list[tuple[str, tuple[frozenset[str], str]]]:
    calls = []
    for rule in (rules or RULES)["rules"]:
        for shape in rule.get("shapes", []):
            tool = shape["tool"]
            where = f"{rule['id']} {tool} {' '.join(shape.get('command', [])) or shape.get('url') or shape.get('op') or 'doc'}"
            if tool == "gcloud":
                words = tuple(shape["command"])
                assert words in GCLOUD, f"{where}: a shape this walk cannot map"
                need = GCLOUD[words]
            elif tool == "kubectl":
                need = (NONE, "local") if shape.get("local") else KUBECTL_CLUSTER
            elif "url" in shape:
                if shape["url"] == METADATA_SERVER_URL:
                    need = (NONE, "local")
                else:
                    assert shape["url"] == "{marker_upload_url}", f"{where}: a URL this walk cannot map"
                    need = MARKER_UPLOAD
            else:
                assert "op" in shape or "doc" in shape, f"{where}: neither a URL nor Firestore"
                continue  # Firestore: test_workspace_firestore_permissions.py
            if include_local or need[1] != "local":
                calls.append((where, need))
    return calls


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def test_the_walk_saw_the_calls_it_was_written_for() -> None:
    # Empty output is not success: each half must find what it walks.
    script = {w for _, w in script_gcloud()}
    for words in (("iam", "service-accounts", "create"), ("storage", "buckets", "add-iam-policy-binding"),
                  ("secrets", "add-iam-policy-binding"), ("container", "clusters", "get-credentials"),
                  ("iam", "service-accounts", "keys", "list")):
        assert words in script, f"the script walk found no gcloud {' '.join(words)}; found {sorted(script)}"
    assert len(script_calls()) >= 20, f"only {len(script_calls())} calls found on the job's path"
    assert len(guard_calls()) >= 25, f"only {len(guard_calls())} guard shapes walked"
    assert {g["name"] for g in grants()} >= {"swarmWorkspaceAccountAdmin", "roles/container.clusterViewer"}


def test_every_call_the_job_makes_is_permitted_to_the_deployer() -> None:
    gaps = missing(script_calls())
    assert not gaps, f"swarm-workspace-deployer lacks what the job's code calls need: {gaps}"


def test_every_call_the_guard_admits_is_permitted_to_the_deployer() -> None:
    gaps = missing(guard_calls())
    assert not gaps, f"swarm-workspace-deployer lacks what workspace-calls.json admits: {gaps}"


def unmade_gcloud(rules: dict, made: set[tuple[str, ...]]) -> list[str]:
    """Every gcloud shape of RULES that reaches a cloud API and is no call in MADE."""
    return sorted({where for where, need in guard_calls(include_local=False, rules=rules)
                   if where.split(" ", 2)[1] == "gcloud" and tuple(where.split(" ", 2)[2].split()) not in made})


def test_the_guard_admits_no_cloud_call_the_job_does_not_make() -> None:
    """Shapes that reach no cloud API (gcloud's local config, kubectl's
    client-side commands, the metadata server) are left to
    test_workspace_guard.py; kubectl's cluster verbs are held against a real
    run in test_register_tenant_workspace.py."""
    unmade = unmade_gcloud(RULES, {w for _, w in script_gcloud()})
    assert not unmade, f"workspace-calls.json admits gcloud calls the job never makes: {unmade}"
    if any(where.split(" ")[1] == "curl" for where, need in guard_calls(include_local=False) if need == MARKER_UPLOAD):
        assert any(url == "WS_MARKER_UPLOAD_URL" for _, url in script_curl()), "the guard admits a marker upload nobody makes"


def test_the_marker_is_written_create_only_and_never_read() -> None:
    """A5's .tenant marker: one upload to tenants/<tenant>/.tenant that cannot
    replace an object, so storage.objects.create on tenants/ is all it needs."""
    code = job_code()["scripts/register-tenant.sh --workspace"]
    assert not re.search(r"(?<![\w/.$-])gcloud[ \t]+storage[ \t]+cp\b", code), (
        "the job writes its marker with gcloud storage cp, which reads the destination first "
        "(storage.objects.get, which the deployer does not hold)")
    expect = re.search(r'marker_upload_url:\s*"([^\n]*)"', GUARD.read_text())
    assert expect, "workspace-guard.sh's expectation names no marker_upload_url"
    url = expect.group(1)
    assert url.startswith(MARKER_UPLOAD_HOST), url
    assert "uploadType=media" in url and "ifGenerationMatch=0" in url, url
    assert "/.tenant" in url and "$prefix" in url, f"the marker is not tenants/<tenant>/.tenant: {url}"
    perms = set().union(*(g["permissions"] for g in grants() if "marker" in g["reach"]))
    assert not {p for p in perms if p.startswith("storage.objects.") and p != "storage.objects.create"}, (
        f"the deployer can do more to objects than create them: {sorted(perms)}")


# ---------------------------------------------------------------------------
# C10, the front-door admit (docs/design/workspace-kinds.md §4.5, lane WK-JOB)
# ---------------------------------------------------------------------------


def test_c10_and_its_read_twin_map_to_k1_and_only_k1() -> None:
    c10 = [s for r in RULES["rules"] if r["id"] == "C10" for s in r["shapes"]]
    assert [tuple(s["command"]) for s in c10] == [("iap", "web", "add-iam-policy-binding")], c10
    assert c10[0]["flags"]["--role"] == "roles/iap.httpsResourceAccessor"
    assert c10[0]["flags"]["--member"] == "serviceAccount:{service_principal}"
    assert c10[0]["flags"]["--service"] == "{front_door_backend}"
    assert set(c10[0]["require"]) >= {"--resource-type", "--service", "--role", "--member", "--condition"}
    calls = [(w, need) for w, need in guard_calls(include_local=False) if need[1] == "front-door"]
    assert {w.split(" ", 2)[2] for w, _ in calls} == {"iap web add-iam-policy-binding", "iap web get-iam-policy"}, calls
    assert not missing(calls), "K1 does not cover what C10 and its read twin admit"
    k1 = front_door_grant()
    assert k1["permissions"] == {"iap.webServices.getIamPolicy", "iap.webServices.setIamPolicy"}, k1
    assert k1["reach"] == {"front-door"}, "K1's condition is not an exact match on one backend"
    # Nothing else the deployer holds reaches an IAP policy but the reader,
    # which reads it.
    reader = front_door_reader_grant()
    others = [g["name"] for g in grants() if g is not None and g["name"] not in (k1["name"], reader["name"])
              and {"iap.webServices.getIamPolicy", "iap.webServices.setIamPolicy"} & g["permissions"]]
    assert not others, others


def test_the_front_door_read_maps_to_the_reader_and_a_change_still_needs_k1() -> None:
    """FRONT-DOOR-READ (part of #847): with K1 not granted, the reader alone
    covers S2's and A9's read of the API backend's IAP policy, and nothing
    else: the change call, C10's add, still needs K1's setIamPolicy, so the
    guard and IAM both still refuse it to a deployer without K1."""
    reader = front_door_reader_grant()
    assert reader["permissions"] == {"iap.webServices.getIamPolicy"}, reader
    assert reader["reach"] == {"front-door"}, "the reader is not bound on the API backend, for the deployer, unconditioned"
    calls = [(w, need) for w, need in guard_calls(include_local=False) if need[1] == "front-door"]
    reads = [(w, need) for w, need in calls if w.endswith("iap web get-iam-policy")]
    changes = [(w, need) for w, need in calls if not w.endswith("iap web get-iam-policy")]
    assert reads and changes, calls
    assert not missing(reads, held=[reader]), "the reader does not cover the front-door read"
    assert missing(changes, held=[reader]) == {
        "iap.webServices.setIamPolicy on front-door": [w for w, _ in changes]}, "the reader reaches a change"
    # The guard still admits exactly one change shape on the front door, C10's add.
    shapes = [tuple(s["command"]) for r in RULES["rules"] for s in r["shapes"]
              if s.get("command", [])[:2] == ["iap", "web"]]
    assert sorted(set(shapes)) == [("iap", "web", "add-iam-policy-binding"), ("iap", "web", "get-iam-policy")], shapes


def test_the_front_door_is_the_api_backend_terraform_grants_k1_on() -> None:
    """The backend C10 names (workspace-calls.json) is the one K1's condition
    resolves: terraform's front_door_api_backend for the default name_prefix.
    The console's backend, `-ui-ui-backend`, is never it."""
    text = _uncommented(DEPLOYER_TF.read_text())
    spelled = re.search(r'\n\s*front_door_api_backend\s*=\s*"([^"]+)"', text)
    prefix = re.search(r'variable\s+"name_prefix"\s*\{[^}]*?default\s*=\s*"([^"]+)"', VARIABLES_TF.read_text(), re.S)
    assert spelled and prefix, "front_door_api_backend or name_prefix's default not found"
    backend = spelled.group(1).replace("${var.name_prefix}", prefix.group(1))
    assert RULES["front_door_backend"] == backend, (RULES["front_door_backend"], backend)
    assert not backend.endswith("-ui-ui-backend")


def test_the_walk_refuses_a_c10_shape_no_call_makes() -> None:
    """Remove S2's call from what the job makes, and C10 is a shape no call
    makes: the walk must say so (the test above holds the real one to empty)."""
    made = {w for _, w in script_gcloud()}
    assert ("iap", "web", "add-iam-policy-binding") in made, "the script walk found no S2 call"
    assert ("iap", "web", "get-iam-policy") in made, "the script walk found no front-door read"
    unmade = unmade_gcloud(RULES, made - {("iap", "web", "add-iam-policy-binding")})
    assert unmade == ["C10 gcloud iap web add-iam-policy-binding"], unmade
