"""swarm-verify may read Jobs in a tenant namespace, and nothing else there.

Lane VERIFY-GKE-READ, part of #1754; owner decision 2026-10-20. e2e-test.sh
[10] compares a GKE-backed capacity holder with its Job. The IAM half (the
cluster read, conditioned to swarm-autopilot) is
tests/terraform/verify_gke_read.tftest.hcl; this is the Kubernetes half,
kubernetes/rbac/verify-rbac.yaml as render.py renders it:

  * one Role, `get` and `list` on `jobs.batch`, nothing else: no Secret, no Pod
    (whose spec carries its env), no log, no write, no wildcard;
  * one RoleBinding, to swarm-verify by email and by numeric uniqueId -- the
    subject GKE presents for an OAuth access token, which is how the gate
    authenticates -- and to nobody else;
  * rendered only when the operator's apply passes `--verify-uid` -- so not in
    the workspace deployer's renders, nor in register-tenant.sh's A9 re-render,
    which expects every object it renders to exist -- and only in the eng and
    smoke tenants' own namespaces (owner decision 2026-10-21,
    VERIFY-GKE-READ-2): never in a `swarm-tenant-u-*` namespace, a
    `--workspace-id` render or any other tenant's, even when asked.
    kubernetes/policies/workspace-provisioner-scope.yaml admits only the Roles
    and subjects the deployer's render had before, so the read there would be
    refused and fail the deployer's next apply;
  * `render.py verify-read` names the account kubernetes/apply.sh resolves the
    uniqueId of -- swarm-verify where the read is rendered, nothing elsewhere --
    so apply.sh asks rather than restating which namespaces carry it.

Offline: the renderer is imported by path and run through `main()`, the path
kubernetes/apply.sh takes.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
PROJECT = "saga-agents-staging"
VERIFY_GSA = f"swarm-verify@{PROJECT}.iam.gserviceaccount.com"
#: Shaped like a uniqueId; not any real account's.
VERIFY_UID = "100000000000000000077"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_verify", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load_renderer()


def _render(tenant: str, *argv: str) -> list[dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(["tenant", "--tenant", tenant, "--project", PROJECT, *argv])
    return [d for d in yaml.safe_load_all(buffer.getvalue()) if d]


def _verify_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d["metadata"]["name"] == "swarm-verify-reader"]


def _one(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [d for d in _verify_docs(docs) if d["kind"] == kind]
    assert len(found) == 1, f"expected one {kind} swarm-verify-reader, found {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def eng_docs() -> list[dict[str, Any]]:
    return _render("eng", "--verify-uid", VERIFY_UID)


def test_the_role_reads_jobs_and_nothing_else(eng_docs):
    role = _one(eng_docs, "Role")
    # MUTATION: add "watch", "pods", "secrets" or a "*" to verify-rbac.yaml and
    # this fails. Exact, not "contains": a wider rule is the defect.
    assert role["rules"] == [{"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list"]}]


def test_the_role_can_never_read_a_secret_a_pod_or_a_log(eng_docs):
    """The attacker's lens: a Secret is a credential; a Pod's spec carries its
    env; a log carries whatever the agent printed. None of them is a Job."""
    role = _one(eng_docs, "Role")
    for rule in role["rules"]:
        # MUTATION: widen resources to ["jobs", "pods"] and this fails.
        assert not set(rule["resources"]) & {"secrets", "pods", "pods/log", "configmaps", "*"}, rule
        assert "*" not in rule["apiGroups"] and "*" not in rule["verbs"], rule
        assert not set(rule["verbs"]) & {"create", "update", "patch", "delete", "deletecollection",
                                         "watch", "escalate", "bind", "impersonate"}, rule


def test_the_binding_names_swarm_verify_by_email_and_by_unique_id(eng_docs):
    binding = _one(eng_docs, "RoleBinding")
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                  "name": "swarm-verify-reader"}
    # MUTATION: bind the email alone and the gate, which reaches the API with an
    # OAuth access token, is presented as its uniqueId and refused -- the
    # 2026-09-24 dispatch failure, in the gate.
    assert binding["subjects"] == [
        {"kind": "User", "name": VERIFY_GSA, "apiGroup": "rbac.authorization.k8s.io"},
        {"kind": "User", "name": VERIFY_UID, "apiGroup": "rbac.authorization.k8s.io"},
    ]


def test_without_a_unique_id_nothing_is_rendered():
    """An email-only binding applies cleanly and authorises nobody on the gate's
    OAuth path; and the renders that pass no uniqueId -- the deployer's,
    register-tenant.sh's A9 check -- must render exactly what they did before."""
    docs = _render("eng")
    # MUTATION: render the read unconditionally and A9 looks for a Role the
    # deployer never applied, and fails the workspace (VERIFY_FAILED).
    assert _verify_docs(docs) == []
    before = [d for d in yaml.safe_load_all(render.render_files(render.tenant_files(), _values("eng", ""))) if d]
    assert docs == before


@pytest.mark.parametrize(
    "uid",
    [
        "12345",  # too short to be a uniqueId
        f"swarm-scheduler@{PROJECT}.iam.gserviceaccount.com",  # another identity
        VERIFY_GSA,  # the email is already the first subject; this slot is the number
        f"{VERIFY_UID}\n  - kind: Group",  # YAML injection
        "agents-staging",
    ],
)
def test_a_verify_uid_that_is_not_swarm_verifys_is_refused(uid):
    # MUTATION: loosen VERIFY_UID's pattern to any address and the scheduler,
    # or a second document injected by newline, is bound.
    with pytest.raises(SystemExit):
        _render("eng", "--verify-uid", uid)


def test_the_read_is_in_the_tenants_namespace_and_carries_the_marker(eng_docs):
    for doc in _verify_docs(eng_docs):
        assert doc["metadata"]["namespace"] == "swarm-tenant-eng", doc["kind"]
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform", doc["kind"]
        assert doc["metadata"]["labels"]["swarm-tenant"] == "eng", doc["kind"]
    kinds = {d["kind"] for d in eng_docs}
    assert not kinds & {"ClusterRole", "ClusterRoleBinding"}


def test_the_read_is_rendered_last_so_every_earlier_document_is_unchanged(eng_docs):
    """test_live_netpol.py and its siblings hold the earlier documents by
    digest; appending keeps them exactly as they rendered before."""
    before = [d for d in yaml.safe_load_all(render.render_files(
        render.tenant_files(), _values("eng", VERIFY_UID))) if d]
    assert eng_docs[: len(before)] == before
    # MUTATION: insert verify-rbac.yaml into TENANT_FILES' middle and this fails.
    assert [d["kind"] for d in eng_docs[len(before):]] == ["Role", "RoleBinding"]


def _values(tenant: str, uid: str) -> dict[str, str]:
    import argparse

    parser = argparse.ArgumentParser()
    render.add_tenant_arguments(parser)
    argv = ["--tenant", tenant, "--project", PROJECT] + (["--verify-uid", uid] if uid else [])
    return render.tenant_values(parser.parse_args(argv))


#: (tenant, extra render arguments, whether the read is rendered).
WHERE = [
    ("eng", (), True),
    ("smoke", (), True),
    # Any other tenant, Terraform's or registered: owner decision 2026-10-21,
    # the gate reads Jobs in eng and smoke and nowhere else.
    ("research", (), False),
    # eng's id with another namespace inside the prefix (--namespace is
    # forwarded by apply.sh): not eng's namespace, so not the read.
    ("eng", ("--namespace", "swarm-tenant-eng-canary"), False),
    # The workspace deployer's two shapes: a person's namespace, and a group
    # workspace's (labelled by --workspace-id). Its scope policy admits
    # neither the Role name nor the subject, so not even when asked.
    ("u-alice", (), False),
    ("eng", ("--workspace-id", "w-000000"), False),
]
WHERE_IDS = ["eng", "smoke", "other-tenant", "eng-other-namespace", "personal", "group-workspace"]


@pytest.mark.parametrize(("tenant", "argv", "rendered"), WHERE, ids=WHERE_IDS)
def test_the_read_is_rendered_only_in_eng_and_smoke(tenant, argv, rendered):
    docs = _render(tenant, "--verify-uid", VERIFY_UID, *argv)
    # MUTATION: render it everywhere and the personal and workspace renders
    # carry objects the deployer's scope policy refuses, failing its apply;
    # drop the tenant allow-list and research's namespace gains the read the
    # owner decided against.
    assert bool(_verify_docs(docs)) is rendered, [d["kind"] for d in _verify_docs(docs)]


def _verify_read(tenant: str, *argv: str) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render.main(["verify-read", "--tenant", tenant, "--project", PROJECT, *argv])
    return buffer.getvalue()


@pytest.mark.parametrize(("tenant", "argv", "rendered"), WHERE, ids=WHERE_IDS)
def test_verify_read_names_the_account_exactly_where_the_read_is_rendered(tenant, argv, rendered):
    """kubernetes/apply.sh resolves swarm-verify's uniqueId only where the
    render will bind it, and asks render.py where that is: a workspace
    deployer's apply never looks the account up (its guard admits no such
    describe), and the rule is stated once, in the renderer.
    MUTATION: print the account for every tenant, or for none, and this is red."""
    out = _verify_read(tenant, *argv)
    assert out == (f"{VERIFY_GSA}\n" if rendered else "\n"), out


def test_the_rbac_file_names_no_other_identity_and_no_other_cluster():
    text = (KUBERNETES / "rbac" / "verify-rbac.yaml").read_text()
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "agents-staging" not in live
    # Only the two verify placeholders are subjects; no literal identity.
    assert "@" not in live, "an identity is spelled literally; it must come from __VERIFY_GSA__/__VERIFY_UID__"
    assert "__VERIFY_GSA__" in live and "__VERIFY_UID__" in live
