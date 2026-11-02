"""Where api.typesafe.ai is named as an egress destination, and where it is not
(the TypeSafe egress allowlist; docs/runbooks/typesafe-egress.md; part of
#2688).

The allowlist is TWO GKE FQDNNetworkPolicies and nothing else in kubernetes/:

  * `swarm-typesafe-fwd-egress-hosts`, in swarm-system, for the forwarder's
    pods (kubernetes/network-policies/swarm-system-typesafe-fwd.yaml), which
    `swarm-typesafe-fwd-egress` also puts under egress isolation: DNS, the
    metadata server, and by name api.typesafe.ai and Secret Manager only;
  * `swarm-allow-typesafe-direct`, in a tenant namespace, for a pod the
    dispatcher labelled `swarm-jev: typesafe-direct`
    (kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml): a pod that
    calls TypeSafe itself because no forwarder serves its tenant.

So, judged here by selecting every FQDNNetworkPolicy under kubernetes/ against
a pod's namespace and labels:

  * a worker pod with neither Jev label has NO TypeSafe allow;
  * a pod labelled `swarm-typesafe: inject` has NO TypeSafe allow either: it
    reaches TypeSafe only through the forwarder, on 8080;
  * a pod labelled `swarm-jev: typesafe-direct` has one;
  * the forwarder's pod has one, and is the only swarm-system workload that
    does.

WHAT THIS DOES NOT PROVE, said where a reader of the test will look:
allow-egress.yaml rule 4 still admits every tenant pod to the public internet
on 443, and api.typesafe.ai answers from shared Cloudflare addresses, so an
unlabelled pod's packets to it are not dropped. NetworkPolicies and
FQDNNetworkPolicies are allow-only unions; there is no rule here that could
take one host back out of rule 4. The runbook states the gap and the owner's
question about closing it.

No cluster, no credentials, no network.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
POLICIES = KUBERNETES / "network-policies"
FORWARDER_POLICY = POLICIES / "swarm-system-typesafe-fwd.yaml"
DIRECT_POLICY = POLICIES / "fqdn" / "allow-typesafe-direct.yaml"
FORWARDER = KUBERNETES / "swarm-system" / "swarm-typesafe-fwd.yaml"
FORWARDER_APP = REPO / "apps" / "swarm-typesafe-fwd" / "swarm_typesafe_fwd"
RUNBOOK = REPO / "docs" / "runbooks" / "typesafe-egress.md"
DATA_FLOW = REPO / "docs" / "jev-data-flow.md"

TYPESAFE = "api.typesafe.ai"
#: A tenant template's namespace, as every file in network-policies/ spells it.
TENANT_NS = "__NAMESPACE__"
#: The dispatcher's two Jev labels (scheduler.dispatch), restated so the
#: policy matrix reads without the scheduler; `test_the_labels_are_the_dispatchers`
#: holds the two equal.
TYPESAFE_POD_LABEL = ("swarm-typesafe", "inject")
TYPESAFE_DIRECT_POD_LABEL = ("swarm-jev", "typesafe-direct")
#: What `GkeJobDispatcher._manifest` labels every worker pod with.
WORKER = {"swarm-task": "task-1", "swarm-tenant": "eng"}


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def _every_doc() -> Iterator[tuple[Path, dict[str, Any]]]:
    for path in sorted(KUBERNETES.rglob("*.y*ml")):
        for doc in _docs(path):
            yield path, doc


def _selects(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """matchLabels only: every FQDN policy here is written that way, and
    `test_every_fqdn_policy_selects_by_match_labels_only` holds it so."""
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items())


def _fqdn_allows(namespace: str, labels: dict[str, str], host: str = TYPESAFE) -> list[str]:
    """The FQDNNetworkPolicies under kubernetes/ that admit `host` for a pod."""
    found = []
    for path, doc in _every_doc():
        if doc.get("kind") != "FQDNNetworkPolicy":
            continue
        if (doc.get("metadata") or {}).get("namespace") != namespace:
            continue
        if not _selects((doc.get("spec") or {}).get("podSelector") or {}, labels):
            continue
        for rule in (doc.get("spec") or {}).get("egress") or []:
            for match in rule.get("matches") or []:
                if match.get("name") == host or (
                        match.get("pattern") and fnmatch.fnmatchcase(host, match["pattern"])):
                    found.append(f"{path.relative_to(REPO)}:{doc['metadata']['name']}")
    return found


def _forwarder_pod_labels() -> dict[str, str]:
    (deploy,) = [d for d in _docs(FORWARDER) if d["kind"] == "Deployment"]
    return dict(deploy["spec"]["template"]["metadata"]["labels"])


# --- the red-first pair -----------------------------------------------------


def test_a_worker_pod_without_the_jev_label_has_no_typesafe_allow():
    assert _fqdn_allows(TENANT_NS, WORKER) == []


def test_the_forwarders_pod_has_the_typesafe_allow():
    assert _fqdn_allows("swarm-system", _forwarder_pod_labels()) == [
        "kubernetes/network-policies/swarm-system-typesafe-fwd.yaml:swarm-typesafe-fwd-egress-hosts"]


# --- the rest of the matrix -------------------------------------------------


def test_a_forwarded_pod_has_no_direct_typesafe_allow():
    """`swarm-typesafe: inject` opens swarm-typesafe-fwd on 8080, and that is
    its only way to TypeSafe that any policy here names."""
    labels = {**WORKER, TYPESAFE_POD_LABEL[0]: TYPESAFE_POD_LABEL[1]}
    assert _fqdn_allows(TENANT_NS, labels) == []


def test_a_direct_pod_has_the_typesafe_allow_and_nothing_else():
    labels = {**WORKER, TYPESAFE_DIRECT_POD_LABEL[0]: TYPESAFE_DIRECT_POD_LABEL[1]}
    assert _fqdn_allows(TENANT_NS, labels) == [
        "kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml:swarm-allow-typesafe-direct"]
    (policy,) = _docs(DIRECT_POLICY)
    (rule,) = policy["spec"]["egress"]
    assert rule["matches"] == [{"name": TYPESAFE}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]


def test_the_labels_are_the_dispatchers():
    from scheduler import dispatch

    assert dispatch.TYPESAFE_POD_LABEL == TYPESAFE_POD_LABEL
    assert dispatch.TYPESAFE_DIRECT_POD_LABEL == TYPESAFE_DIRECT_POD_LABEL


def test_the_direct_label_is_not_the_forwarders_key():
    """The two labels are different keys, so the forwarder's ingress (which
    selects `swarm-typesafe: inject` exactly) never admits a direct pod."""
    assert TYPESAFE_DIRECT_POD_LABEL[0] != TYPESAFE_POD_LABEL[0]


def test_no_other_swarm_system_workload_is_allowed_typesafe():
    for name in ("swarm-voice", "swarm-egress", "swarm-live", "swarm-graph", "decision-api-egress"):
        assert _fqdn_allows("swarm-system", {"app.kubernetes.io/name": name}) == [], name


def test_exactly_two_policies_name_typesafe_anywhere_in_kubernetes():
    named = sorted(
        f"{path.relative_to(REPO)}:{doc['metadata']['name']}"
        for path, doc in _every_doc()
        if doc.get("kind") in ("FQDNNetworkPolicy", "NetworkPolicy") and TYPESAFE in yaml.safe_dump(doc)
    )
    assert named == [
        "kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml:swarm-allow-typesafe-direct",
        "kubernetes/network-policies/swarm-system-typesafe-fwd.yaml:swarm-typesafe-fwd-egress-hosts",
    ]


def test_every_fqdn_policy_selects_by_match_labels_only():
    for path, doc in _every_doc():
        if doc.get("kind") == "FQDNNetworkPolicy":
            selector = doc["spec"]["podSelector"]
            assert set(selector) == {"matchLabels"}, path
            assert selector["matchLabels"], f"{path}: an empty selector is every pod"


# --- the forwarder's egress isolation ---------------------------------------


def _forwarder_policy(kind: str) -> dict[str, Any]:
    (doc,) = [d for d in _docs(FORWARDER_POLICY) if d["kind"] == kind]
    return doc


def test_the_forwarder_policies_select_the_forwarders_pods():
    pod = _forwarder_pod_labels()
    for kind in ("NetworkPolicy", "FQDNNetworkPolicy"):
        doc = _forwarder_policy(kind)
        assert doc["metadata"]["namespace"] == "swarm-system"
        assert doc["metadata"]["labels"]["managed-by"] == "swarm-terraform"
        assert doc["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "swarm-typesafe-fwd"}}
        assert _selects(doc["spec"]["podSelector"], pod)


def test_the_forwarder_is_egress_isolated_with_no_open_internet():
    """Without the NetworkPolicy's Egress isolation the FQDN rule would add to
    swarm-system's unrestricted egress and restrict nothing."""
    policy = _forwarder_policy("NetworkPolicy")
    assert policy["spec"]["policyTypes"] == ["Egress"]
    blocks = [peer["ipBlock"] for rule in policy["spec"]["egress"]
              for peer in rule.get("to") or [] if "ipBlock" in peer]
    assert blocks, "the metadata server is reached by address"
    for block in blocks:
        assert block["cidr"].endswith("/32"), block
        assert "except" not in block
    ports = {(p["protocol"], p["port"]) for rule in policy["spec"]["egress"] for p in rule["ports"]}
    assert ports == {("UDP", 53), ("TCP", 53), ("TCP", 80), ("TCP", 8080)}


def _constant(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            assert isinstance(node.value, ast.Constant)
            return str(node.value.value)
    raise AssertionError(f"{name} is not a constant in {path}")


def test_the_forwarders_hosts_are_exactly_the_ones_its_code_dials():
    """The upstream and Secret Manager, read from the forwarder's own source,
    so a new host there fails here before it fails closed in the cluster."""
    upstream = _constant(FORWARDER_APP / "upstream.py", "UPSTREAM_HOST")
    secrets = _constant(FORWARDER_APP / "main.py", "SECRET_MANAGER").split("/")[2]
    (rule,) = _forwarder_policy("FQDNNetworkPolicy")["spec"]["egress"]
    assert sorted(m["name"] for m in rule["matches"]) == sorted([upstream, secrets])
    assert rule["ports"] == [{"protocol": "TCP", "port": 443}]


# --- the record ------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "kubernetes/network-policies/swarm-system-typesafe-fwd.yaml",
    "kubernetes/network-policies/fqdn/allow-typesafe-direct.yaml",
    "swarm-jev: typesafe-direct",
    "tests/unit/scripts/test_typesafe_egress_allowlist.py",
])
def test_the_runbook_and_the_data_flow_record_name_the_allowlist(phrase):
    assert phrase in RUNBOOK.read_text()
    assert phrase in DATA_FLOW.read_text()


def test_the_data_flow_record_no_longer_says_no_allowlist_exists():
    assert "no host allowlist exists for TypeSafe" not in DATA_FLOW.read_text()
    assert "no Jev-specific egress rule exists" not in DATA_FLOW.read_text()
