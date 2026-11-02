"""The live browser's network path: only TCP 8080 (plain ws), only to the relay, only from
a live-browser pod (docs/design/live-browser.md §8.3; lane LB-L6, part of #1030).

Two NetworkPolicies open the one path the design adds, and a third closes
everything else in `swarm-system`:

  * `swarm-allow-live-egress`, in every tenant namespace, lets a pod labelled
    `swarm-live: enabled` -- which the dispatcher sets only on a
    `claude-code-browser` pod (`scheduler.dispatch.LIVE_POD_LABEL`) -- open TCP
    8080 to `app.kubernetes.io/name=swarm-live` pods in `swarm-system`. The
    existing tenant policies are untouched: NetworkPolicies are unions, so this
    adds a path and removes nothing, and they must render BYTE-IDENTICAL.
  * `swarm-system-default-deny-ingress` and `swarm-allow-live-ingress`, in
    `swarm-system`: nothing reaches a pod there except TCP 8080 to the relay,
    from a labelled pod in a tenant namespace (one peer, both selectors -- two
    peers would be an OR) or from swarm-api's Direct VPC egress range, which
    `kubernetes/apply.sh --swarm-system` READS from swarm-api's service and its
    subnet, never takes from a flag.
  * `swarm-live-ingress`, the relay manifest's own copy of that allow
    (kubernetes/swarm-system/swarm-live.yaml, rendered by terraform/infra/live.tf
    with terraform's subnet range; lane LB-L8e). `apply.sh --swarm-system`
    applies that manifest FIRST -- it creates the namespace -- and refuses when
    terraform's range is not the one it read from swarm-api.

The port is 8080, plain ws, since owner decision 2026-10-11 (b)
(docs/design/live-browser.md §3.3); L6 built it on 8443.

The shape rules live once, in `kubernetes/network_parity.py`, which judges the
rendered policies here and the applied ones after `apply.sh --swarm-system
--confirm`. The mutation tests below feed it the policies a careless edit
would produce and require it to say so.

No cluster, no credentials, no network: `gcloud` and `kubectl` are fakes on
PATH, as in test_cluster_network_is_read_not_typed.py.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import hashlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
APPLY = KUBERNETES / "apply.sh"
LIVE_EGRESS_FILE = "network-policies/allow-live-egress.yaml"
PROXY_EGRESS_FILE = "network-policies/allow-egress-proxy.yaml"
JUDGE_EGRESS_FILE = "network-policies/allow-judge-egress.yaml"
GRAPH_EGRESS_FILE = "network-policies/allow-graph-service.yaml"
TYPESAFE_FWD_FILE = "network-policies/allow-typesafe-fwd.yaml"
SYSTEM_FILE = "network-policies/swarm-system-live.yaml"

#: A swarm-api subnet range that appears nowhere in the repository, so a
#: rendered value that matches it can only have been READ.
FAKE_API_CIDR = "10.123.16.0/20"
FAKE_SUBNET = "swarm-fake-subnet"
RELAY_TEMPLATE = KUBERNETES / "swarm-system" / "swarm-live.yaml"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@functools.cache
def _render() -> Any:
    """kubernetes/render.py, loaded on first use: it imports scheduler and
    quota_broker, and test_collection_stays_cheap holds collection of this
    directory to stdlib weight."""
    return _load("swarm_k8s_render_live", KUBERNETES / "render.py")


parity = _load("swarm_k8s_parity_live", KUBERNETES / "network_parity.py")


def _run(*argv: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        assert _render().main(list(argv)) == 0
    return out.getvalue()


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def _one(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [d for d in docs if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one NetworkPolicy {name}, found {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def tenant_docs() -> list[dict[str, Any]]:
    return _docs(_run("tenant", "--tenant", "eng"))


@pytest.fixture(scope="module")
def system_docs() -> list[dict[str, Any]]:
    return _docs(
        _run(
            "swarm-system",
            "--swarm-api-egress-cidr", FAKE_API_CIDR,
            "--swarm-api-egress-source", f"run/saga-agents-staging/us-central1/swarm-api/{FAKE_SUBNET}",
        )
    )


def _relay_manifest(api_cidr: str = FAKE_API_CIDR) -> str:
    """kubernetes/swarm-system/swarm-live.yaml with its placeholders filled the
    way terraform/infra/live.tf fills them (tests/terraform/live.tftest.hcl
    holds that rendering; this one only needs plausible values). The digest is
    built here, never written out as one literal."""
    values = {
        "__SWARM_LIVE_IMAGE__": "us-central1-docker.pkg.dev/saga-agents-staging/swarm-images/swarm-live@sha256:" + "7" * 64,
        "__PROJECT_ID__": "saga-agents-staging",
        "__ARTIFACT_BUCKET__": "saga-agents-staging-swarm-artifacts",
        "__LIVE_KEY_VERSION__": "projects/saga-agents-staging/secrets/swarm-live-key/versions/latest",
        "__SWARM_LIVE_ILB_IP__": "10.123.16.42",
        "__SWARM_API_EGRESS_CIDR__": api_cidr,
    }
    text = RELAY_TEMPLATE.read_text()
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text


@pytest.fixture(scope="module")
def relay_docs() -> list[dict[str, Any]]:
    return _docs(_relay_manifest())


# --- the tenant side ---------------------------------------------------------


def test_a_tenant_render_holds_the_live_egress_policy_and_it_passes_the_shape_check(tenant_docs):
    policy = _one(tenant_docs, parity.LIVE_EGRESS_POLICY)
    assert policy["metadata"]["namespace"] == "swarm-tenant-eng"
    assert policy["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert parity.check_live_egress(policy) == []


def test_the_live_egress_policy_is_exactly_tcp_8080_to_the_relay_from_labelled_pods(tenant_docs):
    """Spelled out here as well as in the checker, so the checker cannot drift
    into agreeing with a wrong policy without this test noticing."""
    spec = _one(tenant_docs, parity.LIVE_EGRESS_POLICY)["spec"]
    assert spec["podSelector"] == {"matchLabels": {"swarm-live": "enabled"}}
    assert spec["policyTypes"] == ["Egress"]
    assert "ingress" not in spec
    assert spec["egress"] == [
        {
            "to": [
                {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "swarm-system"}},
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "swarm-live"}},
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]


def test_the_policy_selects_the_label_the_dispatcher_sets():
    """`scheduler.dispatch.LIVE_POD_LABEL` is what a live-browser pod carries. A
    policy keyed on any other spelling selects nothing: the pod would have no
    live path, and the hand-off would time out with no error."""
    # Imported here, not at module top: scheduler.dispatch pulls the service
    # stack, and test_collection_stays_cheap holds collection to stdlib weight.
    from scheduler.dispatch import LIVE_POD_LABEL

    key, value = LIVE_POD_LABEL
    assert parity.LIVE_POD_SELECTOR == {"matchLabels": {key: value}}


def test_every_tenant_namespace_carries_the_label_the_relay_admits_and_swarm_system_would_not(tenant_docs):
    """The relay's ingress names tenant namespaces by a label the tenant template
    sets. Held to the rendered Namespace, so renaming the label in one place
    and not the other fails here rather than as a silent drop on anetd."""
    namespace = next(d for d in tenant_docs if d["kind"] == "Namespace")
    labels = namespace["metadata"]["labels"]
    selector = parity.TENANT_NAMESPACE_SELECTOR
    assert all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())
    for expression in selector.get("matchExpressions", []):
        assert expression["operator"] == "Exists"
        assert expression["key"] in labels
    # swarm-system itself is not a tenant, and must not match.
    assert not parity.namespace_selector_matches(
        selector, {"kubernetes.io/metadata.name": "swarm-system", "app.kubernetes.io/part-of": "swarm"}
    )
    assert parity.namespace_selector_matches(selector, labels)


#: sha256 of each pre-existing tenant document, and of the whole tenant render
#: without the new file, as rendered at 28d6988 (main before this lane) with
#: `render.py tenant --tenant eng` and no network (the offline values). The
#: acceptance criterion of LB-L6: "the existing policy renders byte-identical".
#: A deliberate later change to one of these files updates its digest here,
#: with the reason in the commit.
PRE_L6_DIGESTS = {
    "network-policies/default-deny.yaml": "3cafd43f2f6c5412cc53eeb2f222bd4d9a5f84c688bfaa79f027125bc33bbb68",
    "network-policies/allow-egress.yaml": "0a3754646d11bba7a0abb3961b8f9c3ac698f7ae1ca19f2b1d95cc0d26382d28",
}
#: Re-read for CACHE-PVC (2026-10-15): the tenant ResourceQuota admits one cache
#: PVC per Job on the cache's StorageClass, where it held PVCs at 0.
#: Re-read for SIZES-SCHED-1 (2026-10-20): the quota's limits.cpu is
#: CPU_BURST_MAX_RATIO x requests.cpu and the LimitRange's CPU max is 16.
#: Re-read for PLATFORM-REFUSED-APPLY (2026-10-28): a comment in the tenant
#: ResourceQuota now says the finished-Job TTL is ten minutes; no value moved.
#: Re-read for DAYONE-K8S-1 (2026-10-29): an offline render has no default
#: project, so its identities name render.py's OFFLINE_PROJECT where they named
#: the live one; with that name swapped back the render is byte-identical.
PRE_L6_TENANT_RENDER = "f79c97afab5dca95390390edf399242c9d1917f21754f16fd67b3f127fd981ad"


def _tenant_values() -> dict[str, str]:
    import argparse

    parser = argparse.ArgumentParser()
    _render().add_tenant_arguments(parser)
    return _render().tenant_values(parser.parse_args(["--tenant", "eng"]))


def test_the_existing_policies_render_byte_identical():
    values = _tenant_values()
    for name, digest in PRE_L6_DIGESTS.items():
        rendered = _render().render_files((name,), values)
        assert hashlib.sha256(rendered.encode()).hexdigest() == digest, f"{name} no longer renders as it did"


def test_the_live_policy_is_purely_appended_to_the_tenant_render():
    values = _tenant_values()
    files = _render().TENANT_FILES
    at = files.index(LIVE_EGRESS_FILE)
    # Only browser egress's policy (docs/browser-egress.md §2.5), the
    # internal judge's (lane JUDGE-NET), the graph service's (lane GS-INFRA)
    # and the TypeSafe forwarder's (lane TS-MIGRATE-POD) may follow it,
    # appended the same way, so everything before the live path still renders
    # as it did before L6.
    assert files[at + 1:] == (PROXY_EGRESS_FILE, JUDGE_EGRESS_FILE, GRAPH_EGRESS_FILE,
                              TYPESAFE_FWD_FILE)
    before = _render().render_files(files[:at], values)
    assert hashlib.sha256(before.encode()).hexdigest() == PRE_L6_TENANT_RENDER
    with_live = _render().render_files(files[:at + 1], values)
    live = _render().render_files((LIVE_EGRESS_FILE,), values)
    assert with_live == before.rstrip("\n") + "\n---\n" + live
    with_proxy = _render().render_files(files[:at + 2], values)
    proxy = _render().render_files((PROXY_EGRESS_FILE,), values)
    assert with_proxy == with_live.rstrip("\n") + "\n---\n" + proxy
    with_judge = _render().render_files(files[:at + 3], values)
    judge = _render().render_files((JUDGE_EGRESS_FILE,), values)
    assert with_judge == with_proxy.rstrip("\n") + "\n---\n" + judge
    with_graph = _render().render_files(files[:at + 4], values)
    graph = _render().render_files((GRAPH_EGRESS_FILE,), values)
    assert with_graph == with_judge.rstrip("\n") + "\n---\n" + graph
    after = _render().render_files(files, values)
    typesafe = _render().render_files((TYPESAFE_FWD_FILE,), values)
    assert after == with_graph.rstrip("\n") + "\n---\n" + typesafe


def test_the_workspace_deployer_may_write_the_new_tenant_policy():
    """A personal workspace is applied by the workspace deployer, whose scope
    policy names every object the tenant render holds. Without the new name
    there, every `u-*` onboarding is refused at the API server."""
    text = (KUBERNETES / "policies" / "workspace-provisioner-scope.yaml").read_text()
    assert f'"{parity.LIVE_EGRESS_POLICY}"' in text


# --- the swarm-system side ---------------------------------------------------


def test_swarm_system_denies_every_ingress_by_default(system_docs):
    deny = _one(system_docs, parity.SYSTEM_DENY_POLICY)
    assert deny["metadata"]["namespace"] == parity.SYSTEM_NAMESPACE
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert parity.check_system_deny(deny) == []


def test_the_relay_admits_only_tcp_8080_from_labelled_pods_and_swarm_apis_range(system_docs):
    policy = _one(system_docs, parity.LIVE_INGRESS_POLICY)
    assert policy["metadata"]["namespace"] == parity.SYSTEM_NAMESPACE
    assert policy["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert parity.check_live_ingress(policy, FAKE_API_CIDR) == []
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "swarm-live"}}
    assert spec["policyTypes"] == ["Ingress"]
    assert "egress" not in spec
    peers = [peer for rule in spec["ingress"] for peer in rule["from"]]
    assert {"namespaceSelector": parity.TENANT_NAMESPACE_SELECTOR, "podSelector": parity.LIVE_POD_SELECTOR} in peers
    assert {"ipBlock": {"cidr": FAKE_API_CIDR}} in peers
    assert len(peers) == 2
    for rule in spec["ingress"]:
        assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_the_rendered_range_is_recorded_on_the_policy(system_docs):
    annotations = _one(system_docs, parity.LIVE_INGRESS_POLICY)["metadata"]["annotations"]
    assert annotations[parity.API_CIDR_ANNOTATION] == FAKE_API_CIDR
    assert annotations[parity.SOURCE_ANNOTATION] == f"run/saga-agents-staging/us-central1/swarm-api/{FAKE_SUBNET}"


def test_an_offline_swarm_system_render_is_marked_not_for_apply():
    docs = _docs(_run("swarm-system"))
    annotations = _one(docs, parity.LIVE_INGRESS_POLICY)["metadata"]["annotations"]
    assert annotations[parity.SOURCE_ANNOTATION] == parity.OFFLINE_SOURCE


@pytest.mark.parametrize(
    "argv",
    [
        ["--swarm-api-egress-cidr", "0.0.0.0/0"],          # everything
        ["--swarm-api-egress-cidr", "10.0.0.0/7"],         # wider than private space
        ["--swarm-api-egress-cidr", "34.118.224.0/20"],    # public space
        ["--swarm-api-egress-cidr", "10.123.16.1/20"],     # not canonical
        ["--swarm-api-egress-cidr", "10.123.16.0/20\n  - ipBlock: {cidr: 0.0.0.0/0}"],
        ["--swarm-api-egress-source", "run/p/r/s/x"],      # a source with no range
        ["--swarm-api-egress-cidr", FAKE_API_CIDR,
         "--swarm-api-egress-source", "offline-render-not-for-apply"],
    ],
)
def test_the_renderer_refuses_a_range_that_is_not_one_private_subnet(argv):
    with pytest.raises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
        _render().main(["swarm-system", *argv])
    assert caught.value.code not in (0, None)


# --- the checker catches what a careless edit would do -----------------------


def _mutants_of_ingress(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mutants: dict[str, dict[str, Any]] = {}

    def mutant(name: str) -> dict[str, Any]:
        mutants[name] = copy.deepcopy(policy)
        return mutants[name]

    mutant("a second port")["spec"]["ingress"][0]["ports"].append({"protocol": "TCP", "port": 5900})
    del mutant("every port")["spec"]["ingress"][0]["ports"]
    m = mutant("namespace and pod as two peers (an OR)")
    rule = m["spec"]["ingress"][0]
    rule["from"] = [{"namespaceSelector": rule["from"][0]["namespaceSelector"]},
                    {"podSelector": rule["from"][0]["podSelector"]}]
    del mutant("unlabelled pods")["spec"]["ingress"][0]["from"][0]["podSelector"]
    mutant("any namespace")["spec"]["ingress"][0]["from"][0]["namespaceSelector"] = {}
    mutant("the internet")["spec"]["ingress"][-1]["from"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})
    mutant("a range that is not swarm-api's")["spec"]["ingress"][-1]["from"][0]["ipBlock"]["cidr"] = "10.40.0.0/16"
    mutant("every pod in swarm-system")["spec"]["podSelector"] = {}
    mutant("egress too")["spec"]["policyTypes"].append("Egress")
    mutant("an annotation that disagrees")["metadata"]["annotations"][parity.API_CIDR_ANNOTATION] = "10.1.0.0/20"
    mutant("another namespace")["metadata"]["namespace"] = "default"
    mutant("a rule with no source")["spec"]["ingress"].append({"ports": [{"protocol": "TCP", "port": parity.LIVE_PORT}]})
    return mutants


def test_the_ingress_check_refuses_every_widening(system_docs):
    policy = _one(system_docs, parity.LIVE_INGRESS_POLICY)
    mutants = _mutants_of_ingress(policy)
    assert len(mutants) == 12
    passed = [name for name, m in mutants.items() if not parity.check_live_ingress(m, FAKE_API_CIDR)]
    assert passed == [], f"check_live_ingress accepted: {passed}"


def test_the_relay_manifest_carries_the_same_allow_and_passes_the_same_check(relay_docs, system_docs):
    """The relay's own policy (LB-L8e) is judged by the function that judges
    swarm-allow-live-ingress, and admits exactly what it admits."""
    relay = _one(relay_docs, parity.RELAY_INGRESS_POLICY)
    assert relay["metadata"]["namespace"] == parity.SYSTEM_NAMESPACE
    assert relay["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert parity.check_live_ingress(relay, FAKE_API_CIDR, parity.RELAY_INGRESS_POLICY) == []
    assert relay["spec"] == _one(system_docs, parity.LIVE_INGRESS_POLICY)["spec"]
    for rule in relay["spec"]["ingress"]:
        assert rule["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_the_relay_manifest_has_no_template_placeholder_left_once_rendered():
    """Every placeholder the template names is one live.tf fills: a sixth
    one added to the YAML alone would reach apply.sh unrendered."""
    import re

    assert re.findall(r"__[A-Z0-9_]+__", _relay_manifest()) == []


def test_the_relay_policy_check_refuses_every_widening(relay_docs):
    policy = _one(relay_docs, parity.RELAY_INGRESS_POLICY)
    mutants = _mutants_of_ingress(policy)
    assert len(mutants) == 12
    passed = [
        name for name, m in mutants.items()
        if not parity.check_live_ingress(m, FAKE_API_CIDR, parity.RELAY_INGRESS_POLICY)
    ]
    assert passed == [], f"check_live_ingress accepted: {passed}"


def test_the_relay_listens_where_the_policies_admit(relay_docs):
    """The Services, the container and the probes are on the port both
    policies open; a relay on any other port is closed by them."""
    ports = set()
    for doc in relay_docs:
        if doc["kind"] == "Service":
            ports |= {p["port"] for p in doc["spec"]["ports"]} | {p["targetPort"] for p in doc["spec"]["ports"]}
        if doc["kind"] == "StatefulSet":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                ports |= {p["containerPort"] for p in container["ports"]}
                for probe in ("readinessProbe", "livenessProbe"):
                    handler = container[probe].get("httpGet") or container[probe].get("tcpSocket")
                    ports.add(handler["port"])
    assert ports == {parity.LIVE_PORT}


def test_the_egress_check_refuses_every_widening(tenant_docs):
    policy = _one(tenant_docs, parity.LIVE_EGRESS_POLICY)
    mutants: dict[str, dict[str, Any]] = {}
    for name in ("port", "no ports", "any pod", "any namespace", "ipBlock", "all pods", "ingress", "extra rule"):
        mutants[name] = copy.deepcopy(policy)
    mutants["port"]["spec"]["egress"][0]["ports"][0]["port"] = 443
    del mutants["no ports"]["spec"]["egress"][0]["ports"]
    del mutants["any pod"]["spec"]["egress"][0]["to"][0]["podSelector"]
    del mutants["any namespace"]["spec"]["egress"][0]["to"][0]["namespaceSelector"]
    mutants["ipBlock"]["spec"]["egress"][0]["to"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})
    mutants["all pods"]["spec"]["podSelector"] = {}
    mutants["ingress"]["spec"]["policyTypes"].append("Ingress")
    mutants["extra rule"]["spec"]["egress"].append({"ports": [{"protocol": "TCP", "port": 8080}]})
    passed = [name for name, m in mutants.items() if not parity.check_live_egress(m)]
    assert passed == [], f"check_live_egress accepted: {passed}"


def test_the_deny_check_refuses_a_deny_that_allows_something(system_docs):
    deny = copy.deepcopy(_one(system_docs, parity.SYSTEM_DENY_POLICY))
    deny["spec"]["ingress"] = [{}]
    assert parity.check_system_deny(deny)
    deny = copy.deepcopy(_one(system_docs, parity.SYSTEM_DENY_POLICY))
    deny["spec"]["podSelector"] = {"matchLabels": {"app.kubernetes.io/name": "swarm-live"}}
    assert parity.check_system_deny(deny)


# --- the live comparison, after apply.sh --swarm-system --confirm ------------


def _parity_main(tmp_path: Path, live: dict[str, str], items: list[dict[str, Any]]) -> tuple[int, str]:
    (tmp_path / "live.json").write_text(json.dumps(live))
    (tmp_path / "policies.json").write_text(json.dumps({"items": items}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = parity.main(["--live", str(tmp_path / "live.json"), "--policies",
                            str(tmp_path / "policies.json"), "--live-ingress"])
    return code, out.getvalue()


def test_the_live_ingress_comparison_passes_on_the_render_and_fails_on_drift(tmp_path, system_docs, relay_docs):
    live = {"swarm_api_egress_cidr": FAKE_API_CIDR, "source": "fake"}
    applied = system_docs + relay_docs
    code, out = _parity_main(tmp_path, live, applied)
    assert code == 0, out
    assert "compared 3 policies" in out, out
    # The relay manifest's own policy is required: --swarm-system applies it.
    code, out = _parity_main(tmp_path, live, system_docs)
    assert code == 1 and parity.RELAY_INGRESS_POLICY in out, out
    code, out = _parity_main(tmp_path, {**live, "swarm_api_egress_cidr": "10.99.0.0/20"}, applied)
    assert code == 1 and "10.99.0.0/20" in out
    # An empty sweep is nothing compared, never agreement.
    code, out = _parity_main(tmp_path, live, [])
    assert code == 1 and "FAIL" in out
    # The relay's allow without the namespace's deny is not isolation.
    allow_only = [d for d in system_docs if d["metadata"]["name"] == parity.LIVE_INGRESS_POLICY]
    code, out = _parity_main(tmp_path, live, allow_only)
    assert code == 1 and parity.SYSTEM_DENY_POLICY in out


# --- apply.sh --swarm-system reads the range ---------------------------------

FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/kubectl.log"
case "$*" in
  *"version --client"*) printf 'Client Version: v1.36.3\n' ;;
  *"config current-context"*) printf 'swarm-dev' ;;
  *"config view"*)
    if [[ -f "${d}/cluster-entry" ]]; then cat "${d}/cluster-entry"; else printf 'gke_saga-agents-staging_us-central1_swarm-autopilot'; fi ;;
  *"get namespace swarm-system"*) [[ -f "${d}/no-namespace" ]] && exit 1; printf 'namespace/swarm-system\n' ;;
  *"get networkpolicies"*) cat "${d}/applied.json" ;;
  *"apply --dry-run=client"*) cat >"${d}/applied-dry-run.yaml" ;;
  *"apply --dry-run=server"*) cat >/dev/null; printf 'networkpolicy.networking.k8s.io/swarm-allow-live-ingress created (server dry run)\n' ;;
  *"diff -f -"*) cat >"${d}/diff.yaml" ;;
  *"apply -f -"*)
    cat >"${d}/applied.yaml"
    python3 -c 'import json,sys,yaml; print(json.dumps({"items":[d for d in yaml.safe_load_all(open(sys.argv[1])) if d]}))' \
      "${d}/applied.yaml" >"${d}/applied.json"
    printf 'networkpolicy.networking.k8s.io/swarm-allow-live-ingress created\n' ;;
  *) printf 'fake kubectl: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/gcloud.log"
case "$*" in
  *"auth list"*) : ;;
  *"run services describe swarm-api"*) cat "${d}/service.json" ;;
  *"compute networks subnets describe"*) cat "${d}/subnet.json" ;;
  *) printf 'fake gcloud: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

#: terraform, for `output -raw swarm_live_manifest` and `output -json
#: live_relay`: the relay manifest is served from relay.yaml, and its absence
#: is what terraform says about a null output.
FAKE_TERRAFORM = r"""#!/usr/bin/env bash
set -euo pipefail
d="${FAKE_CLUSTER_DIR:?}"
printf '%s\n' "$*" >>"${d}/terraform.log"
case "$*" in
  *"output -raw swarm_live_manifest"*)
    if [[ ! -f "${d}/relay.yaml" ]]; then
      printf 'Error: Unsupported value for raw output\n\nThe value for output value "swarm_live_manifest" is null, so -raw mode cannot print it.\n' >&2
      exit 1
    fi
    cat "${d}/relay.yaml" ;;
  *"output -json live_relay"*) cat "${d}/live_relay.json" ;;
  *) printf 'fake terraform: unexpected call: %s\n' "$*" >&2; exit 3 ;;
esac
"""

LEAKY = (
    "PROJECT_ID", "REGION", "ZONE", "ENVIRONMENT", "GKE_CLUSTER", "GKE_LOCATION", "API_SERVICE",
    "KUBECTL", "SWARM_KUBECTL", "KUBECONFIG", "SWARM_ENV_FILE", "GITHUB_ACTIONS",
    "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS", "SWARM_TERRAFORM", "TF_DATA_DIR",
    "TF_STATE_BUCKET", "TF_STATE_PREFIX",
)


def _service(subnetwork: str | None) -> dict[str, Any]:
    """`gcloud run services describe swarm-api --format=json`, v1 (Knative)
    shape, reduced to the Direct VPC egress annotation."""
    interfaces = [{"network": "swarm-vpc", **({"subnetwork": subnetwork} if subnetwork else {})}]
    return {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {"name": "swarm-api"},
        "spec": {"template": {"metadata": {"annotations": {
            "run.googleapis.com/network-interfaces": json.dumps(interfaces),
            "run.googleapis.com/vpc-access-egress": "all-traffic",
        }}}},
    }


@pytest.fixture
def cluster(tmp_path: Path) -> dict[str, Any]:
    d = tmp_path / "cluster"
    b = tmp_path / "bin"
    d.mkdir()
    b.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD), ("terraform", FAKE_TERRAFORM)):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    (d / "service.json").write_text(json.dumps(_service(f"projects/saga-agents-staging/regions/us-central1/subnetworks/{FAKE_SUBNET}")))
    (d / "subnet.json").write_text(json.dumps({"name": FAKE_SUBNET, "ipCidrRange": FAKE_API_CIDR}))
    (d / "relay.yaml").write_text(_relay_manifest())
    (d / "live_relay.json").write_text(json.dumps({"enabled": True, "port": 8080, "swarm_api_egress_cidr": FAKE_API_CIDR}))
    # terraform/infra counts as initialised when its data directory exists;
    # TF_DATA_DIR is terraform's own override for where that is.
    (tmp_path / "tfdata").mkdir()
    env = {k: v for k, v in os.environ.items() if k not in LEAKY}
    env.update(
        PATH=f"{b}:{env.get('PATH', '')}",
        FAKE_CLUSTER_DIR=str(d),
        SWARM_KUBECTL=str(b / "kubectl"),
        SWARM_TERRAFORM=str(b / "terraform"),
        TF_DATA_DIR=str(tmp_path / "tfdata"),
        SWARM_ENV_FILE=str(tmp_path / "no.env"),
        # apply.sh has no default project (DAYONE-K8S-1): the one this fake
        # cluster's context and identities name is given, as an operator would.
        PROJECT_ID="saga-agents-staging",
        HOME=str(tmp_path),
    )
    return {"dir": d, "env": env}


def _apply(cluster: dict[str, Any], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(APPLY), "--swarm-system", *argv],
        capture_output=True, text=True, env=cluster["env"], timeout=120, check=False,
    )


def test_apply_renders_the_range_it_read_from_swarm_apis_subnet(cluster):
    result = _apply(cluster)
    assert result.returncode == 0, result.stderr
    rendered = _docs((cluster["dir"] / "applied-dry-run.yaml").read_text())
    policy = _one(rendered, parity.LIVE_INGRESS_POLICY)
    assert parity.check_live_ingress(policy, FAKE_API_CIDR) == []
    gcloud = (cluster["dir"] / "gcloud.log").read_text()
    assert f"compute networks subnets describe {FAKE_SUBNET}" in gcloud
    assert not (cluster["dir"] / "applied.yaml").exists(), "a dry run applied something"


def test_apply_confirm_applies_and_compares_with_the_live_range(cluster):
    result = _apply(cluster, "--confirm")
    assert result.returncode == 0, result.stderr
    assert (cluster["dir"] / "applied.yaml").exists()
    assert "get networkpolicies" in (cluster["dir"] / "kubectl.log").read_text()
    assert f"matches {FAKE_API_CIDR}" in result.stdout + result.stderr


@pytest.mark.parametrize("flag", ["--swarm-api-egress-cidr=10.9.0.0/20", "--swarm-api-egress-source", "--swarm-api"])
def test_apply_refuses_the_range_from_its_command_line(cluster, flag):
    result = _apply(cluster, flag, "x")
    assert result.returncode != 0
    assert "it cannot be passed" in result.stderr
    assert not (cluster["dir"] / "applied-dry-run.yaml").exists()


def test_apply_refuses_when_swarm_api_has_no_direct_vpc_subnet(cluster):
    (cluster["dir"] / "service.json").write_text(json.dumps(_service(None)))
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "subnetwork" in result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()


# --- apply.sh --swarm-system applies the relay first (LB-L8e) -----------------


def _kinds_and_names(text: str) -> list[tuple[str, str]]:
    return [(d["kind"], d["metadata"]["name"]) for d in _docs(text)]


@pytest.mark.parametrize("confirm", [False, True])
def test_apply_applies_the_relay_manifest_before_the_policies(cluster, confirm):
    """One apply, in order: the relay's manifest from `terraform output -raw
    swarm_live_manifest` -- which creates the namespace -- and then
    swarm-system's policies. A dry run previews the same stream."""
    (cluster["dir"] / "no-namespace").write_text("")
    result = _apply(cluster, *(["--confirm"] if confirm else []))
    assert result.returncode == 0, result.stderr
    assert "output -raw swarm_live_manifest" in (cluster["dir"] / "terraform.log").read_text()
    stream = (cluster["dir"] / ("applied.yaml" if confirm else "applied-dry-run.yaml")).read_text()
    order = _kinds_and_names(stream)
    assert order[0] == ("Namespace", "swarm-system"), order
    relay = _kinds_and_names(_relay_manifest())
    assert order[: len(relay)] == relay, order
    policies = [o for o in order[len(relay):]]
    assert policies == [
        ("NetworkPolicy", parity.SYSTEM_DENY_POLICY),
        ("NetworkPolicy", parity.LIVE_INGRESS_POLICY),
    ], order
    assert order.index(("StatefulSet", "swarm-live")) < order.index(("NetworkPolicy", parity.SYSTEM_DENY_POLICY))
    if confirm:
        out = result.stdout + result.stderr
        assert f"{parity.SYSTEM_NAMESPACE}/{parity.RELAY_INGRESS_POLICY} matches {FAKE_API_CIDR}" in out, out
        assert "compared 3 policies" in out, out
    else:
        assert not (cluster["dir"] / "applied.yaml").exists(), "a dry run applied something"


@pytest.mark.parametrize(
    "how",
    [
        ("--context", "gke_saga-agents-staging_us-central1-a_agents-staging"),
        ("label",),
    ],
)
def test_apply_refuses_the_shared_cluster_before_reading_the_relay(cluster, how):
    """agents-staging is another team's cluster (CLAUDE.md rule 2). Refused by
    the deny-list in scripts/lib/common.sh -- whether named as the context or
    reached through our own label -- before terraform is asked for anything
    and before anything is applied."""
    other = "gke_saga-agents-staging_us-central1-a_agents-staging"
    if how[0] == "label":
        (cluster["dir"] / "cluster-entry").write_text(other)
        result = _apply(cluster, "--confirm")
    else:
        result = _apply(cluster, "--confirm", *how)
    assert result.returncode != 0
    assert "agents-staging" in result.stderr and "deny-list" in result.stderr, result.stderr
    assert not (cluster["dir"] / "terraform.log").exists(), "the relay manifest was read for a refused cluster"
    assert not (cluster["dir"] / "applied.yaml").exists()
    assert not (cluster["dir"] / "applied-dry-run.yaml").exists()


def test_apply_confirm_refuses_without_a_relay_manifest(cluster):
    """enable_live_browser off: the output is null, and the policies alone
    would select a relay that does not exist in a namespace nothing made."""
    (cluster["dir"] / "relay.yaml").unlink()
    (cluster["dir"] / "no-namespace").write_text("")
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "no relay manifest" in result.stderr, result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()


def test_a_dry_run_without_a_relay_manifest_previews_the_policies_alone(cluster):
    (cluster["dir"] / "relay.yaml").unlink()
    result = _apply(cluster)
    assert result.returncode == 0, result.stderr
    assert "--confirm will refuse" in result.stderr
    order = _kinds_and_names((cluster["dir"] / "applied-dry-run.yaml").read_text())
    assert all(kind == "NetworkPolicy" for kind, _ in order), order


def test_apply_refuses_when_terraforms_range_is_not_swarm_apis(cluster):
    """The relay's policy admits terraform's subnet_cidr; the policies after it
    admit the range read from swarm-api. One fact, two sources: held equal."""
    (cluster["dir"] / "live_relay.json").write_text(json.dumps({"swarm_api_egress_cidr": "10.40.0.0/20"}))
    for argv in ((), ("--confirm",)):
        result = _apply(cluster, *argv)
        assert result.returncode != 0
        assert "10.40.0.0/20" in result.stderr and FAKE_API_CIDR in result.stderr, result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()


@pytest.mark.parametrize(
    "mutate, said",
    [
        (lambda text: text.replace("namespace: swarm-system", "namespace: default", 1), "other than swarm-system"),
        (lambda text: text + "---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata:\n  name: x\n", "RBAC"),
        (lambda text: text + "---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: swarm-system\ndata:\n  a: __UNFILLED__\n", "placeholder"),
    ],
    ids=["another-namespace", "rbac", "placeholder"],
)
def test_apply_refuses_a_relay_manifest_that_reaches_past_swarm_system(cluster, mutate, said):
    relay = cluster["dir"] / "relay.yaml"
    relay.write_text(mutate(relay.read_text()))
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert said in result.stderr, result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()


# --- apply.sh --swarm-system checks its preconditions first (OPS-SMALL, #1194) -


def test_an_uninitialised_terraform_root_is_refused_first_with_the_init_command(cluster, tmp_path):
    """Measured 2026-10-12: from a checkout whose terraform/infra was never
    initialised, --swarm-system failed late, after kubectl and gcloud, or (a
    stale checkout) with an unrelated "--tenant is required". It now says what
    is missing, with the exact command, before it contacts anything."""
    cluster["env"]["TF_DATA_DIR"] = str(tmp_path / "never-initialised")
    result = _apply(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "terraform/infra is not initialised" in result.stderr, result.stderr
    assert (
        "init -input=false -reconfigure -backend-config=bucket=swarm-tfstate-saga-agents-staging"
        " -backend-config=prefix=infra/dev"
    ) in result.stderr, result.stderr
    assert "--tenant" not in out, out
    for tool in ("kubectl", "gcloud", "terraform"):
        assert not (cluster["dir"] / f"{tool}.log").exists(), f"{tool} ran before the precondition check"


def test_a_missing_relay_manifest_is_refused_before_gcloud_is_asked_anything(cluster):
    """terraform is initialised but has no swarm_live_manifest: --confirm
    refuses with one actionable line, before swarm-api is described."""
    (cluster["dir"] / "relay.yaml").unlink()
    result = _apply(cluster, "--confirm")
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "terraform output swarm_live_manifest is missing" in result.stderr, result.stderr
    assert "enable_live_browser" in result.stderr, result.stderr
    assert "--tenant" not in out, out
    assert not (cluster["dir"] / "gcloud.log").exists(), "swarm-api was described before the precondition check"
    assert not (cluster["dir"] / "applied.yaml").exists()


def test_swarm_system_never_asks_for_a_tenant(cluster):
    result = _apply(cluster)
    assert result.returncode == 0, result.stderr
    assert "--tenant" not in result.stdout + result.stderr
