"""The tenant side of the TypeSafe forwarder's path (docs/design/workspace-secrets.md
§3.5, SEC-MIGRATE-TYPESAFE; lane TS-MIGRATE-POD, part of #1347).

`swarm-allow-typesafe-fwd`, in every tenant namespace, lets a pod the
dispatcher labelled `swarm-typesafe: inject` (`scheduler.dispatch.
TYPESAFE_POD_LABEL`) open TCP 8080 to the `swarm-typesafe-fwd` pods in
`swarm-system`, and nothing more. Built like `swarm-allow-judge-egress`: the
internet rule in allow-egress.yaml is an ipBlock, and pod traffic is never
covered by one, so reaching a pod in swarm-system needs one peer naming ONE
destination by both selectors.

The forwarder's own side, `swarm-typesafe-fwd-ingress` in
kubernetes/swarm-system/swarm-typesafe-fwd.yaml, admits the same label from a
tenant namespace on the same port; the two are held equal here.

The render appends the file LAST, so every tenant document before it renders
as it did; the workspace deployer's scope policy names it, or every `u-*`
onboarding is refused at the API server once the render carries it.

No cluster, no credentials, no network.
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
POLICY_FILE = "network-policies/allow-typesafe-fwd.yaml"
POLICY = KUBERNETES / POLICY_FILE
FORWARDER = KUBERNETES / "swarm-system" / "swarm-typesafe-fwd.yaml"
SCOPE = KUBERNETES / "policies" / "workspace-provisioner-scope.yaml"
NAME = "swarm-allow-typesafe-fwd"
LABEL = {"swarm-typesafe": "inject"}
APP = {"app.kubernetes.io/name": "swarm-typesafe-fwd"}
PORT = 8080


@functools.cache
def _render() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_tsfwd", KUBERNETES / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _values(tenant: str) -> dict[str, str]:
    parser = argparse.ArgumentParser()
    _render().add_tenant_arguments(parser)
    return _render().tenant_values(parser.parse_args(["--tenant", tenant]))


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def _template() -> dict[str, Any]:
    (policy,) = _docs(POLICY.read_text())
    return policy


def _forwarder_ingress() -> dict[str, Any]:
    (policy,) = [d for d in _docs(FORWARDER.read_text())
                 if d.get("kind") == "NetworkPolicy"
                 and d["metadata"]["name"] == "swarm-typesafe-fwd-ingress"]
    return policy


def test_the_policy_opens_8080_to_the_forwarder_only_from_a_labelled_pod():
    policy = _template()
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["name"] == NAME
    assert policy["metadata"]["namespace"] == "__NAMESPACE__"
    assert policy["metadata"]["labels"] == {"managed-by": "swarm-terraform",
                                            "swarm-tenant": "__TENANT_ID__"}
    spec = policy["spec"]
    # Only labelled pods: an empty selector would open the forwarder to every
    # agent pod in the namespace, which is what the label exists to prevent.
    assert spec["podSelector"] == {"matchLabels": LABEL}
    assert spec["policyTypes"] == ["Egress"]
    assert "ingress" not in spec
    assert spec["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "swarm-system"}},
                    "podSelector": {"matchLabels": APP},
                }
            ],
            "ports": [{"protocol": "TCP", "port": PORT}],
        }
    ]


def test_the_label_is_the_one_the_dispatcher_sets():
    from scheduler.dispatch import TYPESAFE_POD_LABEL

    assert _template()["spec"]["podSelector"]["matchLabels"] == dict([TYPESAFE_POD_LABEL])


def test_the_forwarder_admits_exactly_the_pods_and_port_this_policy_opens():
    """The other side, which this lane reads and does not edit: one peer with
    both selectors -- a tenant namespace AND a labelled pod -- on 8080."""
    tenant = _template()["spec"]["egress"][0]
    ingress = _forwarder_ingress()["spec"]
    assert ingress["podSelector"]["matchLabels"] == tenant["to"][0]["podSelector"]["matchLabels"]
    assert ingress["policyTypes"] == ["Ingress"]
    (rule,) = ingress["ingress"]
    assert rule["ports"] == tenant["ports"]
    (peer,) = rule["from"]
    assert peer["podSelector"] == {"matchLabels": LABEL}
    assert peer["namespaceSelector"]["matchLabels"] == {"app.kubernetes.io/part-of": "swarm"}
    assert peer["namespaceSelector"]["matchExpressions"] == [
        {"key": "swarm-tenant", "operator": "Exists"}]


def test_every_tenant_namespace_carries_the_labels_the_forwarder_admits():
    (namespace,) = [d for d in _docs(_render().render_files(
        ("namespaces/tenant-namespace.yaml",), _values("eng"))) if d["kind"] == "Namespace"]
    labels = namespace["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == "swarm"
    assert labels["swarm-tenant"] == "eng"


@pytest.mark.parametrize("tenant", ["eng", "research", "u-alice"])
def test_the_policy_renders_into_every_tenant_namespace(tenant):
    values = _values(tenant)
    (policy,) = _docs(_render().render_files((POLICY_FILE,), values))
    assert policy["metadata"]["name"] == NAME
    assert policy["metadata"]["namespace"] == values["NAMESPACE"]
    assert policy["metadata"]["labels"]["swarm-tenant"] == tenant
    assert policy["spec"]["podSelector"] == {"matchLabels": LABEL}


def test_a_tenant_render_holds_it_and_nothing_else_selects_the_label():
    docs = _docs(_render().render_files(_render().TENANT_FILES, _values("eng")))
    named = [d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == NAME]
    assert len(named) == 1
    selecting = [d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy"
                 and "swarm-typesafe" in (d["spec"].get("podSelector", {}).get("matchLabels") or {})]
    assert selecting == [NAME]


def test_render_appends_it_last_so_everything_before_renders_as_it_did():
    files = _render().TENANT_FILES
    assert files[-1] == POLICY_FILE
    assert files.count(POLICY_FILE) == 1
    values = _values("eng")
    before = _render().render_files(files[:-1], values)
    after = _render().render_files(files, values)
    assert after == before.rstrip("\n") + "\n---\n" + _render().render_files((POLICY_FILE,), values)


def _scope_policy_names() -> list[str]:
    found = re.search(r'"networking\.k8s\.io/networkpolicies":\s*\[([^\]]*)\]', SCOPE.read_text())
    assert found, "the scope policy lists no networkpolicies"
    return re.findall(r'"([^"]+)"', found.group(1))


def test_the_workspace_deployer_may_write_it():
    assert NAME in _scope_policy_names()


def test_the_scope_policy_names_exactly_the_policies_the_tenant_render_holds():
    """Parity both ways: a policy the render holds and the scope does not name
    is refused on every `u-*` onboarding; one the scope names and the render
    does not is a name the deployer may write for no reason."""
    docs = _docs(_render().render_files(_render().TENANT_FILES, _values("u-alice")))
    rendered = sorted(d["metadata"]["name"] for d in docs if d["kind"] == "NetworkPolicy")
    assert sorted(_scope_policy_names()) == rendered
