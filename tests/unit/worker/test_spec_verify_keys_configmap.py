"""The step-spec public keys reach a GKE pod as a ConfigMap nobody in the
tenant can write (contract request 34, #342).

Owner decision, 2026-09-29 (#353): a browser pod's manifest is rendered per
task by the scheduler, so the `{version: PEM}` map is NOT inlined into its
`env:` -- that would be one template placeholder away from a copy the task
shapes. Instead terraform/infra outputs the map (`spec_verify_keys_configmap`),
`kubernetes/render.py tenant --spec-verify-keys-file` turns that output into
the `swarm-spec-verify-keys` ConfigMap in the tenant's own namespace, and
`kubernetes/apply.sh --spec-verify-keys` applies it with the rest of the
namespace. No Kubernetes provider in Terraform. The pod mounts it read-only at
/etc/swarm/spec-verify-keys, where `agent_worker.specverify.read_mount` reads
one file per key (#353's worker and GKE template).

What matters about RBAC is the ABSENCE of a write grant, not a read grant: the
kubelet fetches a volume with the node's credentials and these pods have no
token at all. So this file holds that nothing in a tenant namespace, and
nothing cluster-wide, lets the tenant's KSA or GSA -- by email or by uniqueId
-- update, patch or delete a ConfigMap: neither Kubernetes RBAC nor, since
GKE allows what either one allows, IAM (no `container.configMaps.*` in any
custom role, no ConfigMap-reaching predefined role on the tenant GSA; #346).

Same loading and rendering style as test_kubernetes_manifests.py; a file of
its own so #353's edits to that file and these do not collide.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"

TENANT = "eng"
PROJECT = "saga-agents-staging"
NAMESPACE = f"swarm-tenant-{TENANT}"
TENANT_GSA = f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com"
SCHEDULER_UID = "117405034245659033603"
RECONCILER_UID = "108023754768362642341"

#: The ConfigMap's name and file names, as #353's GKE template and worker
#: spell them (`scheduler.dispatch.SPEC_VERIFY_KEYS_CONFIG_MAP`,
#: `agent_worker.specverify.read_mount`). Written out: neither is on main yet.
CONFIG_MAP = "swarm-spec-verify-keys"
MOUNTED_FILES = ("SPEC_VERIFY_KEYS", "SPEC_SIGNING_KEY")

SIGNING_KEY = (
    f"projects/{PROJECT}/locations/us-central1/keyRings/swarm-dev-specs/cryptoKeys/step-spec"
)
#: A real P-256 public key, so the PEM rule is exercised on the real shape.
PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6ojZWToar3bAf33GAQzfub2F9rJL\n"
    "XmYMWuxVjGKJS8MlrJoz2dIlrz96sPwfmpvnKrhl81k4njMziRjXV9JLTg==\n"
    "-----END PUBLIC KEY-----\n"
)
KEYS = {f"{SIGNING_KEY}/cryptoKeyVersions/1": PEM, f"{SIGNING_KEY}/cryptoKeyVersions/3": PEM}

#: What `terraform output -json spec_verify_keys_configmap` prints.
OUTPUT = {
    "SPEC_VERIFY_KEYS": json.dumps(KEYS, sort_keys=True, separators=(",", ":")),
    "SPEC_SIGNING_KEY": SIGNING_KEY,
    "SPEC_SIGNATURE_MODE": "legacy",
    "SPEC_LEGACY_CUTOVER": "2026-10-01T12:00:00Z",
}

WRITE_VERBS = {"create", "update", "patch", "delete", "deletecollection", "*"}


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_spec", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_renderer()


def documents(text: str) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _tenant_render(*argv: str) -> list[dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(
            [
                "tenant",
                "--tenant",
                TENANT,
                "--project",
                PROJECT,
                "--scheduler-uid",
                SCHEDULER_UID,
                "--reconciler-uid",
                RECONCILER_UID,
                *argv,
            ]
        )
    return documents(buffer.getvalue())


@pytest.fixture()
def output_file(tmp_path: Path):
    def write(payload: Any) -> str:
        path = tmp_path / "spec-verify-keys.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return str(path)

    return write


def _config_maps(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == "ConfigMap"]


# ---------------------------------------------------------------------------
# The ConfigMap
# ---------------------------------------------------------------------------


def test_the_configmap_carries_the_terraform_output_in_the_tenants_namespace(output_file):
    docs = _tenant_render("--spec-verify-keys-file", output_file(OUTPUT))
    maps = _config_maps(docs)
    assert len(maps) == 1
    cm = maps[0]
    assert cm["metadata"]["name"] == CONFIG_MAP
    assert cm["metadata"]["namespace"] == NAMESPACE
    assert cm["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert cm["metadata"]["labels"]["swarm-tenant"] == TENANT
    # The same {version: PEM} map the Cloud Run Jobs carry, byte for byte once
    # parsed, and the key the worker checks each version against.
    assert json.loads(cm["data"]["SPEC_VERIFY_KEYS"]) == KEYS
    assert cm["data"]["SPEC_SIGNING_KEY"] == SIGNING_KEY
    # Every file the worker reads from the mount is there.
    assert set(MOUNTED_FILES) <= set(cm["data"])


def test_the_configmap_is_rendered_after_the_namespace_it_lives_in(output_file):
    docs = _tenant_render("--spec-verify-keys-file", output_file(OUTPUT))
    kinds = [d["kind"] for d in docs]
    assert kinds.index("Namespace") < kinds.index("ConfigMap")


def test_without_the_output_no_configmap_is_rendered():
    # register-tenant.sh and a hand-run apply do not pass it; an apply without
    # it leaves any existing ConfigMap as it is (kubectl apply does not prune).
    assert _config_maps(_tenant_render()) == []


def test_the_pems_survive_the_yaml_round_trip_exactly(output_file):
    docs = _tenant_render("--spec-verify-keys-file", output_file(OUTPUT))
    cm = _config_maps(docs)[0]
    assert all(pem == PEM for pem in json.loads(cm["data"]["SPEC_VERIFY_KEYS"]).values())


@pytest.mark.parametrize(
    "payload",
    [
        # Not the output at all.
        "not json",
        ["a", "list"],
        # A key the worker never reads is a key nobody reviewed.
        {**OUTPUT, "EXTRA": "x"},
        # Missing the map, or the key.
        {k: v for k, v in OUTPUT.items() if k != "SPEC_VERIFY_KEYS"},
        {k: v for k, v in OUTPUT.items() if k != "SPEC_SIGNING_KEY"},
        # An empty map verifies nothing: every signed task would fail CANNOT_START.
        {**OUTPUT, "SPEC_VERIFY_KEYS": "{}"},
        # A version that is not under the signing key.
        {**OUTPUT, "SPEC_VERIFY_KEYS": json.dumps({f"{SIGNING_KEY}-other/cryptoKeyVersions/1": PEM})},
        {**OUTPUT, "SPEC_VERIFY_KEYS": json.dumps({f"{SIGNING_KEY}/cryptoKeyVersions/one": PEM})},
        # YAML smuggled through a PEM or the key name.
        {**OUTPUT, "SPEC_VERIFY_KEYS": json.dumps({f"{SIGNING_KEY}/cryptoKeyVersions/1": PEM + "---\nkind: RoleBinding\n"})},
        {**OUTPUT, "SPEC_VERIFY_KEYS": json.dumps({f"{SIGNING_KEY}/cryptoKeyVersions/1": "not a pem"})},
        {**OUTPUT, "SPEC_SIGNING_KEY": SIGNING_KEY + "\nkind: Role"},
        {**OUTPUT, "SPEC_SIGNING_KEY": "projects/other/locations/us-central1/keyRings/r/cryptoKeys/k"},
        # The rollout settings, checked as the worker would.
        {**OUTPUT, "SPEC_SIGNATURE_MODE": "permissive"},
        {**OUTPUT, "SPEC_LEGACY_CUTOVER": "2026-10-01T12:00:00"},
    ],
)
def test_a_malformed_output_is_refused_before_anything_is_rendered(output_file, payload):
    with pytest.raises((render.RenderError, SystemExit)):
        _tenant_render("--spec-verify-keys-file", output_file(payload))


def test_apply_sh_forwards_the_output_file_to_the_renderer():
    text = (KUBERNETES / "apply.sh").read_text()
    assert "--spec-verify-keys)" in text
    assert "--spec-verify-keys-file" in text


# ---------------------------------------------------------------------------
# Nobody in the tenant can write it
# ---------------------------------------------------------------------------


def _rules_write_configmaps(rules: list[dict[str, Any]]) -> bool:
    for rule in rules or []:
        groups = set(rule.get("apiGroups", []))
        resources = set(rule.get("resources", []))
        verbs = set(rule.get("verbs", []))
        if groups & {"", "*"} and resources & {"configmaps", "*"} and verbs & WRITE_VERBS:
            return True
    return False


@pytest.fixture(scope="module")
def full_render() -> list[dict[str, Any]]:
    # With the older KSA bound, so every identity a tenant pod could run as is
    # in the render.
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(
            [
                "tenant",
                "--tenant",
                TENANT,
                "--project",
                PROJECT,
                "--scheduler-uid",
                SCHEDULER_UID,
                "--reconciler-uid",
                RECONCILER_UID,
                "--bound-ksa",
                render.DEFAULT_KSA_NAME,
                "--bound-ksa",
                render.LEGACY_KSA_NAME,
            ]
        )
    return documents(buffer.getvalue())


def test_no_role_in_a_tenant_namespace_can_write_a_configmap(full_render):
    roles = [d for d in full_render if d.get("kind") == "Role"]
    assert roles, "the control: the render holds the worker, dispatcher and reaper Roles"
    offenders = [r["metadata"]["name"] for r in roles if _rules_write_configmaps(r.get("rules", []))]
    assert offenders == []


def _tenant_subject(subject: dict[str, Any]) -> bool:
    """Is this RBAC subject the tenant, named any way Kubernetes can name it?

    A ServiceAccount subject (the worker KSAs), the tenant GSA by email, or a
    numeric uniqueId -- GKE presents a Google service account reaching the API
    with an OAuth token by its uniqueId. The only uniqueIds this render may
    carry are the scheduler's and the reconciler's, so any other number is
    treated as the tenant's.
    """
    kind, name = subject.get("kind"), str(subject.get("name", ""))
    if kind == "ServiceAccount":
        return True
    if name == TENANT_GSA:
        return True
    return name.isdigit() and name not in {SCHEDULER_UID, RECONCILER_UID}


def test_every_binding_of_a_tenant_identity_names_the_empty_worker_role(full_render):
    roles = {d["metadata"]["name"]: d for d in full_render if d.get("kind") == "Role"}
    bindings = [d for d in full_render if d.get("kind") == "RoleBinding"]
    tenant_bindings = [b for b in bindings if any(_tenant_subject(s) for s in b.get("subjects", []))]
    assert tenant_bindings, "the control: the worker KSAs are bound to something"
    for binding in tenant_bindings:
        role = roles[binding["roleRef"]["name"]]
        assert binding["roleRef"]["kind"] == "Role"
        assert not _rules_write_configmaps(role.get("rules", [])), binding["metadata"]["name"]
        assert role.get("rules", []) == [], binding["metadata"]["name"]


def test_the_tenant_gsa_is_bound_to_no_role_in_the_namespace(full_render):
    # By email or by uniqueId: a binding keyed on either would authorise it.
    for binding in (d for d in full_render if d.get("kind") == "RoleBinding"):
        for subject in binding.get("subjects", []):
            if subject.get("kind") == "User":
                assert subject["name"] != TENANT_GSA
                assert not (subject["name"].isdigit() and subject["name"] not in {SCHEDULER_UID, RECONCILER_UID})


def test_nothing_in_kubernetes_is_a_cluster_role_or_cluster_role_binding():
    # Read as text, not parsed: the templates carry placeholders, and a
    # cluster-scoped grant is refused whether or not its file would parse.
    cluster_scoped = re.compile(r"^\s*kind:\s*[\"']?Cluster(Role|RoleBinding)[\"']?\s*$", re.MULTILINE)
    files = sorted(KUBERNETES.rglob("*.yaml")) + sorted(KUBERNETES.rglob("*.yml"))
    assert files, "the control: kubernetes/ holds manifests"
    found = [str(p.relative_to(REPO)) for p in files if cluster_scoped.search(p.read_text())]
    assert found == []


# ---------------------------------------------------------------------------
# ...and nothing in IAM grants it either (#346, #354 review)
# ---------------------------------------------------------------------------
#
# Kubernetes RBAC is half of what GKE authorises a Google identity by: a
# request is allowed when RBAC OR IAM allows it. A custom role carrying a
# `container.configMaps.*` permission, or a predefined role like
# roles/container.developer granted to the tenant's GSA, would let the tenant
# rewrite the keys its own pod verifies against, whatever the Roles above say.

TERRAFORM = REPO / "terraform"
TENANCY = TERRAFORM / "modules" / "tenancy"

_PERMISSION = re.compile(r'"(container\.[A-Za-z]+\.[A-Za-z]+)"')
#: Every quoted predefined-role literal, not only `role = "roles/..."`: the
#: tenancy module grants its telemetry roles from a list of bare strings
#: iterated as `role = role`, and a ConfigMap-reaching role added to that list
#: is the likeliest way the regression arrives (#354 fix review).
_ROLE = re.compile(r'"(roles/[^"]+)"')

#: Predefined roles that reach ConfigMaps in a cluster (container.configMaps.*
#: is in each of them).
_CONFIGMAP_WRITING_ROLES = re.compile(r"^roles/(container\.(developer|admin)|editor|owner)$")


def _tf_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.tf") if ".terraform" not in p.parts)


def _configmap_permissions(text: str) -> list[str]:
    return [p for p in _PERMISSION.findall(text) if p.startswith("container.configMaps.")]


def test_the_iam_scan_catches_a_configmap_permission():
    """The control for the scan below: a role written the way platform_roles.tf
    writes them, holding a configMaps permission, is caught."""
    role = 'permissions = [\n  "container.jobs.create",\n  "container.' + 'configMaps.update",\n]\n'
    assert _configmap_permissions(role) == ["container.configMaps.update"]


def test_no_custom_role_in_terraform_carries_a_configmap_permission():
    files = _tf_files(TERRAFORM)
    assert files, "the control: terraform/ holds .tf files"
    every = {p for f in files for p in _PERMISSION.findall(f.read_text())}
    assert "container.jobs.create" in every, (
        "the control: the GKE dispatcher's container.* permissions are where the scan looks"
    )
    found = {
        str(f.relative_to(REPO)): _configmap_permissions(f.read_text())
        for f in files
        if _configmap_permissions(f.read_text())
    }
    assert found == {}


def _configmap_reaching_roles(text: str) -> list[str]:
    return sorted({r for r in _ROLE.findall(text) if _CONFIGMAP_WRITING_ROLES.match(r)})


@pytest.mark.parametrize(
    "snippet",
    [
        # A one-off binding.
        'resource "google_project_iam_member" "x" {\n  role   = "roles/container.' + 'developer"\n}\n',
        # An entry in a list of roles iterated as `role = role`, the way
        # tenancy/main.tf grants its telemetry roles.
        'telemetry_roles = [\n  "roles/logging.logWriter",\n  "roles/container.' + 'developer",\n]\n',
    ],
)
def test_the_iam_scan_catches_a_configmap_reaching_role(snippet):
    """The control for the scan below, in both shapes a grant is written."""
    assert _configmap_reaching_roles(snippet) == ["roles/container.developer"]


def test_the_tenant_gsa_is_granted_no_role_that_reaches_configmaps():
    files = _tf_files(TENANCY)
    roles = {r for f in files for r in _ROLE.findall(f.read_text())}
    assert "roles/storage.objectUser" in roles, (
        "the control: the tenancy module's grants are where the scan looks"
    )
    assert "roles/logging.logWriter" in roles, (
        "the control: the list-of-roles grants (telemetry_roles) are scanned too"
    )
    assert sorted(r for r in roles if _CONFIGMAP_WRITING_ROLES.match(r)) == []
