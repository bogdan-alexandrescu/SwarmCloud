"""The TypeSafe hostname policies are applied by the code that applies their
neighbours, behind a cluster check (docs/runbooks/typesafe-egress.md; the
owner's pick "Wire behind a cluster check", 2026-11-02; part of #2688).

  * kubernetes/render.py renders the tenant's `swarm-allow-typesafe-direct`
    (an FQDNNetworkPolicy) only with `--fqdn-network-policy`, last, so every
    document before it renders as it did;
  * kubernetes/apply.sh --tenant passes that flag only when the cluster's API
    serves fqdnnetworkpolicies.networking.gke.io, and otherwise renders
    without it, says so in one line, and the tenant's apply goes ahead; the
    flag is refused on its command line, because it is read from the cluster;
  * terraform/infra/typesafe_fwd.tf's `swarm_typesafe_fwd_manifest` carries the
    forwarder's two egress objects only where gke_enable_fqdn_network_policy
    is set (the terraform test, tests/terraform/typesafe_fwd.tftest.hcl), and
    apply.sh --swarm-typesafe-fwd refuses that manifest, whole, on a cluster
    that does not serve the kind.

The deployer's grant for the one policy name is in
tests/unit/worker/test_workspace_provisioner_scope.py.

No cluster, no credentials, no network: apply.sh runs against the fake
clusters of test_cluster_network_is_read_not_typed.py and
test_roll_typesafe_fwd.py.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import io
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from . import test_roll_typesafe_fwd as fwd
from .test_cluster_network_is_read_not_typed import APPLY, FAKE, Cluster

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
FORWARDER_POLICY = KUBERNETES / "network-policies" / "swarm-system-typesafe-fwd.yaml"

RESOURCE = "fqdnnetworkpolicies.networking.gke.io"
DIRECT = "swarm-allow-typesafe-direct"
NAMESPACE = "swarm-tenant-eng"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


@functools.cache
def _renderer() -> Any:
    """render.py imports quota_broker and scheduler, so it loads on first use, not at collection."""
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_tswired", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _render(*extra: str) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        assert _renderer().main(["tenant", "--tenant", "eng", *extra]) == 0
    return buffer.getvalue()


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def _hostname_policies(text: str) -> list[tuple[str, str]]:
    return [(d["metadata"]["namespace"], d["metadata"]["name"])
            for d in _docs(text) if d["kind"] == "FQDNNetworkPolicy"]


# ---------------------------------------------------------------------------
# render.py
# ---------------------------------------------------------------------------


def test_a_render_without_the_flag_holds_no_hostname_policy():
    assert _hostname_policies(_render()) == []


def test_a_render_with_the_flag_holds_the_one_policy_last_and_changes_nothing_before_it():
    plain, wired = _render(), _render("--fqdn-network-policy")
    assert _hostname_policies(wired) == [(NAMESPACE, DIRECT)]
    assert _docs(wired)[-1]["kind"] == "FQDNNetworkPolicy"
    assert _docs(wired)[:-1] == _docs(plain)
    policy = _docs(wired)[-1]
    assert policy["metadata"]["labels"] == {"managed-by": "swarm-terraform", "swarm-tenant": "eng"}


def test_the_resource_apply_sh_asks_for_is_the_renderers():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert _renderer().main(["fqdn-resource"]) == 0
    assert buffer.getvalue() == f"{RESOURCE}\n"
    assert _renderer().TENANT_FQDN_FILES == ("network-policies/fqdn/allow-typesafe-direct.yaml",)


# ---------------------------------------------------------------------------
# apply.sh --tenant
# ---------------------------------------------------------------------------


def _applied(cluster: Cluster) -> str:
    return (cluster.dir / "applied-dry-run.yaml").read_text()


def test_on_a_cluster_without_the_kind_the_render_has_no_hostname_policy_and_onboarding_goes_on(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    result = cluster.run(APPLY, "--tenant", "eng")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _hostname_policies(_applied(cluster)) == []
    # The render is the rest of the namespace, as before.
    assert any(d["kind"] == "NetworkPolicy" for d in _docs(_applied(cluster)))
    assert f"hostname policy  skipped: the cluster does not serve {RESOURCE}" in out, out
    assert "api-resources --api-group=networking.gke.io -o name" in cluster.log("kubectl")


def test_on_a_cluster_that_serves_the_kind_the_render_holds_it_named_exactly(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "api-resources").write_text(f"{RESOURCE}\nmanagedcertificates.networking.gke.io\n")
    result = cluster.run(APPLY, "--tenant", "eng")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _hostname_policies(_applied(cluster)) == [(NAMESPACE, DIRECT)]
    assert f"the cluster serves {RESOURCE}: rendering {DIRECT}" in out, out


def test_a_cluster_serving_only_other_gke_kinds_gets_no_hostname_policy(tmp_path):
    """Matched whole: a resource whose name merely contains the kind's is not it."""
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "api-resources").write_text(f"managedcertificates.networking.gke.io\nx{RESOURCE}\n")
    result = cluster.run(APPLY, "--tenant", "eng")
    assert result.returncode == 0, result.stderr
    assert _hostname_policies(_applied(cluster)) == []


def test_a_discovery_failure_skips_the_policy_with_a_warning_and_onboarding_goes_on(tmp_path):
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "api-resources").write_text(f"{RESOURCE}\n")
    (cluster.dir / "api-resources-fails").write_text("")
    result = cluster.run(APPLY, "--tenant", "eng")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _hostname_policies(_applied(cluster)) == []
    assert f"skipped: could not read whether the cluster serves {RESOURCE}" in out, out


@pytest.mark.parametrize("arg", ["--fqdn-network-policy", "--fqdn", "--fqdn-network-policy=1"])
def test_the_flag_is_read_from_the_cluster_never_passed(tmp_path, arg):
    cluster = Cluster(tmp_path, FAKE)
    (cluster.dir / "api-resources").write_text("")
    result = cluster.run(APPLY, "--tenant", "eng", arg)
    assert result.returncode != 0
    assert "--fqdn-network-policy" in result.stderr and "cannot be" in result.stderr, result.stderr
    assert not (cluster.dir / "applied-dry-run.yaml").exists()


# ---------------------------------------------------------------------------
# apply.sh --swarm-typesafe-fwd: the forwarder's policy rides with it
# ---------------------------------------------------------------------------


def _fwd_with_policy() -> str:
    return fwd._fwd_manifest() + "---\n" + FORWARDER_POLICY.read_text()


def test_the_forwarder_with_its_policy_is_applied_where_the_cluster_serves_the_kind(tmp_path):
    cluster = fwd.Cluster(tmp_path)
    cluster.serve(True, _fwd_with_policy())
    (cluster.dir / "api-resources").write_text(f"{RESOURCE}\n")
    result = cluster.apply("--confirm")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    applied = _docs((cluster.dir / "applied.yaml").read_text())
    assert [(d["kind"], d["metadata"]["name"]) for d in applied if "Policy" in d["kind"]][-2:] == [
        ("NetworkPolicy", "swarm-typesafe-fwd-egress"),
        ("FQDNNetworkPolicy", "swarm-typesafe-fwd-egress-hosts"),
    ]
    assert f"the cluster serves {RESOURCE}: the forwarder's egress policy is applied with it" in out, out


def test_the_forwarder_with_its_policy_is_refused_whole_where_the_cluster_lacks_the_kind(tmp_path):
    """The NetworkPolicy alone would isolate the forwarder with no host to
    reach. MUTATION: drop the check and the cluster receives both, refuses
    the FQDNNetworkPolicy, and keeps the isolation."""
    cluster = fwd.Cluster(tmp_path)
    cluster.serve(True, _fwd_with_policy())
    result = cluster.apply("--confirm")
    assert result.returncode != 0
    assert f"not serve {RESOURCE}" in result.stderr and "Nothing was applied" in result.stderr, result.stderr
    assert not cluster.applied()


def test_the_forwarder_without_its_policy_asks_the_cluster_nothing_new(tmp_path):
    cluster = fwd.Cluster(tmp_path)
    result = cluster.apply("--confirm")
    assert result.returncode == 0, result.stderr
    assert "api-resources" not in cluster.log("kubectl")
