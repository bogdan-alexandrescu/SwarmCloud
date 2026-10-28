#!/usr/bin/env python3
"""Compare applied tenant egress policies with the live cluster's network.

    python3 kubernetes/network_parity.py --live live.json --policies policies.json

`scripts/lib/check-cluster-network-parity.sh` is the caller: it reads the live
network with `kubernetes/cluster-network.sh` (the same reader `apply.sh`
renders from), lists every `swarm-allow-worker-egress` in the cluster, and
hands both here. Standard library only, so a bare `python3` runs it.

WHY IT EXISTS. The repository's mirrored-copy rule (docs/mirrored-values.md):
a value written in two places with nothing asserting the copies agree has cost
this platform three outages. The egress policy is a copy of four cluster
values -- pod range, service range, kube-dns Service IP, NodeLocal DNSCache
address -- and on 2026-09-24 the applied copy agreed with none of them: it was
rendered from renderer defaults, and its only DNS rule reached kube-dns pods
the cluster's DNS never uses. A worker ran for 390 s with every lookup dropped.

TWO THINGS ARE COMPARED, because they fail differently:

  * WHAT THE POLICY WAS RENDERED FOR -- the `swarm.saga.xyz/*` annotations the
    template records. Exact equality with the live values. A policy rendered
    for another network fails here even where it would still isolate (a pod
    range inside 10/8 is excepted either way), because the next difference may
    not be so forgiving; that is the mirrored-copy rule, not a style point.
  * WHAT THE POLICY ENFORCES -- the spec. Both live DNS addresses must be
    reachable on UDP and TCP 53, and both live ranges must be excepted from the
    internet rule and reachable through no rule that is not DNS-only. An
    annotation can say anything; the spec is what anetd enforces.

THE LIVE BROWSER'S PATH (docs/design/live-browser.md §8.3, lane LB-L6), with
`--live-ingress`. `kubernetes/apply.sh --swarm-system --confirm` lists the
NetworkPolicies in `swarm-system` and passes swarm-api's Direct VPC egress
range, read from the service, as `swarm_api_egress_cidr`. The relay's ingress
policy must admit exactly that range and labelled tenant pods, on TCP 8080
only, and the namespace's default deny must be there beside it. So must the
relay manifest's own copy of that allow (`swarm-live-ingress`, rendered by
terraform/infra/live.tf from the subnet terraform gives swarm-api): the same
shape, judged by the same function, against the same range read from the
service -- which is how a terraform subnet that is not the one swarm-api
actually egresses from is caught.

THE GRAPH SERVICE'S STATS PORT (docs/design/graph-answer-cache.md §5.3, lane
CACHE-2-NETWORK), with `--graph-ingress`. `swarm-graph-ingress`
(kubernetes/swarm-system/swarm-graph.yaml, rendered by
terraform/infra/graph_service.tf from the same subnet as the relay's) admits
tenant namespaces on TCP 8080 (/mcp), and swarm-api's range and the
swarm-graph pods of swarm-system on TCP 8081 (the stats), each rule one port
and nothing else: swarm-api never reaches /mcp and a tenant never reaches the
stats. `check_graph_ingress` judges it, against the same `--live` input.

The shape rules for those policies -- `check_live_egress`, `check_live_ingress`,
`check_system_deny` and `check_graph_ingress` -- live here, once: tests/unit/scripts/test_live_netpol.py
judges the RENDERED policies with the same functions, so a rule cannot be
restated in the tests and drift from what is checked against the cluster.
They compare exact shapes, not "allows at least": every widening -- a second
port, a peer split into two (an OR), a selector dropped -- is a failure.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Any

POLICY_NAME = "swarm-allow-worker-egress"
SOURCE_ANNOTATION = "swarm.saga.xyz/network-source"
OFFLINE_SOURCE = "offline-render-not-for-apply"

#: (live key, annotation) for each rendered value.
RENDERED = (
    ("pod_cidr", "swarm.saga.xyz/pod-cidr"),
    ("service_cidr", "swarm.saga.xyz/service-cidr"),
    ("cluster_dns_ip", "swarm.saga.xyz/cluster-dns-ip"),
    ("node_local_dns_ip", "swarm.saga.xyz/node-local-dns-ip"),
)

DNS = {("UDP", 53), ("TCP", 53)}

# -- the live browser's path (LB-L6) ------------------------------------------

#: The tenant policy (network-policies/allow-live-egress.yaml) and the two in
#: swarm-system (network-policies/swarm-system-live.yaml).
LIVE_EGRESS_POLICY = "swarm-allow-live-egress"
LIVE_INGRESS_POLICY = "swarm-allow-live-ingress"
SYSTEM_DENY_POLICY = "swarm-system-default-deny-ingress"
#: The relay manifest's own ingress policy (kubernetes/swarm-system/swarm-live.yaml,
#: rendered by terraform/infra/live.tf; lane LB-L8e). The same allow as
#: LIVE_INGRESS_POLICY, so the relay is ingress-isolated from the moment its
#: manifest is applied, before apply.sh's policies follow it.
RELAY_INGRESS_POLICY = "swarm-live-ingress"
SYSTEM_NAMESPACE = "swarm-system"
API_CIDR_ANNOTATION = "swarm.saga.xyz/swarm-api-egress-cidr"

#: The relay's one port: plain ws on TCP 8080, fixed for both integration
#: lanes by owner decision 2026-10-11 (b) (docs/design/live-browser.md §3.3;
#: swarm_live.main.PORT).
LIVE_PORT = 8080
LIVE_PORTS = [{"protocol": "TCP", "port": LIVE_PORT}]
#: The pods the dispatcher marks live (scheduler.dispatch.LIVE_POD_LABEL; the
#: test holds the two together -- this file is stdlib-only and cannot import it).
LIVE_POD_SELECTOR = {"matchLabels": {"swarm-live": "enabled"}}
#: The relay's pods, in SYSTEM_NAMESPACE.
RELAY_POD_SELECTOR = {"matchLabels": {"app.kubernetes.io/name": "swarm-live"}}
RELAY_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": SYSTEM_NAMESPACE}},
    "podSelector": RELAY_POD_SELECTOR,
}
#: A namespace a tenant render made: namespaces/tenant-namespace.yaml sets both.
TENANT_NAMESPACE_SELECTOR = {
    "matchLabels": {"app.kubernetes.io/part-of": "swarm"},
    "matchExpressions": [{"key": "swarm-tenant", "operator": "Exists"}],
}
TENANT_POD_PEER = {"namespaceSelector": TENANT_NAMESPACE_SELECTOR, "podSelector": LIVE_POD_SELECTOR}

# -- the graph service's stats port (CACHE-2-NETWORK) ----------------------------

#: kubernetes/swarm-system/swarm-graph.yaml's ingress policy.
GRAPH_INGRESS_POLICY = "swarm-graph-ingress"
#: /mcp (swarm_graph.server.PORT) and the stats (swarm_graph.stats.STATS_PORT);
#: tests/unit/scripts/test_graph_service_manifest.py holds both to the service.
GRAPH_PORT = 8080
GRAPH_STATS_PORT = 8081
GRAPH_POD_SELECTOR = {"matchLabels": {"app.kubernetes.io/name": "swarm-graph"}}
#: Any pod of a tenant namespace (graph-service.md §2.1): a namespace selector
#: alone, no pod selector.
GRAPH_TENANT_PEER = {"namespaceSelector": TENANT_NAMESPACE_SELECTOR}
#: The swarm-graph pods of the policy's own namespace: a bare pod selector.
GRAPH_PEER_PODS = {"podSelector": GRAPH_POD_SELECTOR}


def namespace_selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """Whether a namespace with `labels` matches `selector` (matchLabels and the
    Exists/DoesNotExist/In/NotIn expressions Kubernetes defines)."""
    if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression.get("key"), expression.get("operator")
        values = expression.get("values") or []
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        if operator == "In" and labels.get(key) not in values:
            return False
        if operator == "NotIn" and key in labels and labels[key] in values:
            return False
    return True


def _rules_admit_only(
    rules: list[dict[str, Any]], side: str, allowed: list[dict[str, Any]], what: str
) -> list[str]:
    """Every rule is LIVE_PORTS exactly, and every peer is one of `allowed`
    exactly. A rule with no ports allows every port; one with no peers allows
    every source or destination."""
    problems: list[str] = []
    if not rules:
        problems.append(f"{what}: no rule at all -- the path this policy exists for is closed")
    for i, rule in enumerate(rules):
        if rule.get("ports") != LIVE_PORTS:
            problems.append(
                f"{what}: rule {i} allows {rule.get('ports') or 'every port'}, not exactly TCP {LIVE_PORT}"
            )
        peers = rule.get(side)
        if not peers:
            problems.append(f"{what}: rule {i} names no peer, which is every peer")
            continue
        for peer in peers:
            if peer not in allowed:
                problems.append(f"{what}: rule {i} admits {json.dumps(peer, sort_keys=True)}")
    return problems


def check_live_egress(policy: dict[str, Any]) -> list[str]:
    """`swarm-allow-live-egress`: a labelled pod may reach the relay on 8080,
    and the policy grants nothing more."""
    what = LIVE_EGRESS_POLICY
    spec = policy.get("spec") or {}
    problems: list[str] = []
    if spec.get("podSelector") != LIVE_POD_SELECTOR:
        problems.append(f"{what}: selects {spec.get('podSelector')}, not {LIVE_POD_SELECTOR}")
    if spec.get("policyTypes") != ["Egress"]:
        problems.append(f"{what}: policyTypes {spec.get('policyTypes')}, not ['Egress']")
    if "ingress" in spec:
        problems.append(f"{what}: carries ingress rules; a worker listens on nothing")
    problems += _rules_admit_only(spec.get("egress") or [], "to", [RELAY_PEER], what)
    return problems


def check_live_ingress(policy: dict[str, Any], api_cidr: str, what: str = LIVE_INGRESS_POLICY) -> list[str]:
    """`swarm-allow-live-ingress` (or the relay manifest's `swarm-live-ingress`,
    named by `what`): the relay admits labelled tenant pods and swarm-api's
    range `api_cidr`, on 8080, and nothing else."""
    metadata = policy.get("metadata") or {}
    spec = policy.get("spec") or {}
    problems: list[str] = []
    if metadata.get("namespace") != SYSTEM_NAMESPACE:
        problems.append(f"{what}: in namespace {metadata.get('namespace')}, not {SYSTEM_NAMESPACE}")
    recorded = (metadata.get("annotations") or {}).get(API_CIDR_ANNOTATION)
    if recorded != api_cidr:
        problems.append(f"{API_CIDR_ANNOTATION}: rendered {recorded}, swarm-api's is {api_cidr}")
    if spec.get("podSelector") != RELAY_POD_SELECTOR:
        problems.append(f"{what}: selects {spec.get('podSelector')}, not {RELAY_POD_SELECTOR}")
    if spec.get("policyTypes") != ["Ingress"]:
        problems.append(f"{what}: policyTypes {spec.get('policyTypes')}, not ['Ingress']")
    rules = spec.get("ingress") or []
    api_peer = {"ipBlock": {"cidr": api_cidr}}
    problems += _rules_admit_only(rules, "from", [TENANT_POD_PEER, api_peer], what)
    peers = [peer for rule in rules for peer in rule.get("from") or []]
    for wanted, name in ((TENANT_POD_PEER, "labelled tenant pods"), (api_peer, f"swarm-api's {api_cidr}")):
        if wanted not in peers:
            problems.append(f"{what}: does not admit {name}")
    return problems


def check_graph_ingress(policy: dict[str, Any], api_cidr: str) -> list[str]:
    """`swarm-graph-ingress`: exactly three rules, each one peer on one port --
    tenant namespaces on 8080, swarm-api's range `api_cidr` on 8081, the
    swarm-graph pods of swarm-system on 8081 -- and nothing more."""
    what = GRAPH_INGRESS_POLICY
    metadata = policy.get("metadata") or {}
    spec = policy.get("spec") or {}
    problems: list[str] = []
    if metadata.get("namespace") != SYSTEM_NAMESPACE:
        problems.append(f"{what}: in namespace {metadata.get('namespace')}, not {SYSTEM_NAMESPACE}")
    if spec.get("podSelector") != GRAPH_POD_SELECTOR:
        problems.append(f"{what}: selects {spec.get('podSelector')}, not {GRAPH_POD_SELECTOR}")
    if spec.get("policyTypes") != ["Ingress"]:
        problems.append(f"{what}: policyTypes {spec.get('policyTypes')}, not ['Ingress']")
    wanted = [
        (GRAPH_TENANT_PEER, GRAPH_PORT, "tenant namespaces"),
        ({"ipBlock": {"cidr": api_cidr}}, GRAPH_STATS_PORT, f"swarm-api's {api_cidr}"),
        (GRAPH_PEER_PODS, GRAPH_STATS_PORT, "the swarm-graph pods"),
    ]
    expected = [{"from": [peer], "ports": [{"protocol": "TCP", "port": port}]} for peer, port, _ in wanted]
    rules = spec.get("ingress") or []
    # Exact rules, in any order: a second port, a second peer (an OR), a
    # selector widened or dropped, or a rule with no ports or no peer (every
    # port, every peer) is a rule that is not one of these three.
    for i, rule in enumerate(rules):
        if rule not in expected:
            problems.append(f"{what}: rule {i} admits {json.dumps(rule, sort_keys=True)}, which is none of its three rules")
    for rule, (_, port, name) in zip(expected, wanted):
        if rules.count(rule) != 1:
            problems.append(f"{what}: does not admit {name} on TCP {port} exactly once")
    return problems


def check_system_deny(policy: dict[str, Any]) -> list[str]:
    """`swarm-system-default-deny-ingress`: every pod, ingress-isolated, no rule."""
    what = SYSTEM_DENY_POLICY
    problems: list[str] = []
    if (policy.get("metadata") or {}).get("namespace") != SYSTEM_NAMESPACE:
        problems.append(f"{what}: not in {SYSTEM_NAMESPACE}")
    if policy.get("spec") != {"podSelector": {}, "policyTypes": ["Ingress"]}:
        problems.append(f"{what}: spec is {json.dumps(policy.get('spec'), sort_keys=True)}, not a bare deny")
    return problems


def _ports(rule: dict[str, Any]) -> set[tuple[str, Any]]:
    return {(p.get("protocol", "TCP"), p.get("port")) for p in rule.get("ports") or []}


def _allows(rule: dict[str, Any], protocol: str, port: int) -> bool:
    """A rule with no `ports` allows every port."""
    return not rule.get("ports") or (protocol, port) in _ports(rule)


def _dns_only(rule: dict[str, Any]) -> bool:
    return bool(rule.get("ports")) and _ports(rule) <= DNS


def _blocks(rule: dict[str, Any]) -> list[dict[str, Any]]:
    return [peer["ipBlock"] for peer in rule.get("to") or [] if peer.get("ipBlock")]


def _reaches(rule: dict[str, Any], address: str) -> bool:
    ip = ipaddress.ip_address(address)
    for block in _blocks(rule):
        if ip not in ipaddress.ip_network(block["cidr"], strict=False):
            continue
        if any(ip in ipaddress.ip_network(e, strict=False) for e in block.get("except") or []):
            continue
        return True
    return False


def check_policy(policy: dict[str, Any], live: dict[str, str]) -> list[str]:
    """Every way `policy` disagrees with `live`; empty when it matches."""
    problems: list[str] = []
    annotations = (policy.get("metadata") or {}).get("annotations") or {}
    rules = (policy.get("spec") or {}).get("egress") or []

    # -- what it was rendered for ---------------------------------------------
    for key, annotation in RENDERED:
        rendered = annotations.get(annotation)
        if rendered is None:
            problems.append(
                f"{annotation}: not recorded -- rendered before the network was read from "
                f"the cluster; the cluster's is {live[key]}"
            )
        elif rendered != live[key]:
            problems.append(f"{annotation}: rendered {rendered}, the cluster's is {live[key]}")
    if annotations.get(SOURCE_ANNOTATION) == OFFLINE_SOURCE:
        problems.append(
            f"{SOURCE_ANNOTATION}: {OFFLINE_SOURCE} -- an offline render, with documentation "
            "addresses, was applied to a cluster"
        )

    # -- what it enforces: DNS --------------------------------------------------
    for key, what in (
        ("node_local_dns_ip", "the NodeLocal DNSCache address"),
        ("cluster_dns_ip", "the kube-dns Service IP"),
    ):
        address = live[key]
        if not any(
            _reaches(r, address) and _allows(r, "UDP", 53) and _allows(r, "TCP", 53) for r in rules
        ):
            problems.append(
                f"DNS: no rule reaches {what} {address} on UDP and TCP 53 -- every name lookup "
                "a worker makes is dropped, and anetd drops without a response"
            )

    # -- what it enforces: the cluster's ranges ---------------------------------
    internet = [b for r in rules for b in _blocks(r) if b.get("cidr") == "0.0.0.0/0"]
    for key, name in (("pod_cidr", "pod-cidr"), ("service_cidr", "service-cidr")):
        wanted = live[key]
        if not any(wanted in (b.get("except") or []) for b in internet):
            excepted = sorted({e for b in internet for e in b.get("except") or []})
            problems.append(
                f"{name}: the cluster's {wanted} is not an except entry of the internet rule "
                f"(it excepts {', '.join(excepted) or 'nothing'})"
            )
        network = ipaddress.ip_network(wanted)
        for rule in rules:
            if _dns_only(rule):
                continue
            for block in _blocks(rule):
                cidr = ipaddress.ip_network(block["cidr"], strict=False)
                if not cidr.overlaps(network):
                    continue
                # CIDRs are nested or disjoint, so the overlap is the smaller one.
                overlap = network if network.subnet_of(cidr) else cidr
                excepts = [ipaddress.ip_network(e, strict=False) for e in block.get("except") or []]
                if not any(overlap.subnet_of(e) for e in excepts):
                    ports = ", ".join(f"{p}/{n}" for p, n in sorted(_ports(rule), key=str)) or "every port"
                    problems.append(
                        f"{name}: {overlap} (the cluster's {wanted}) is reachable through "
                        f"{block['cidr']} on {ports}"
                    )
    return problems


def _items(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return document
    return list(document.get("items") or [])


def _main_live_ingress(live: dict[str, Any], items: list[dict[str, Any]]) -> int:
    """The relay's ingress in swarm-system, against swarm-api's live range."""
    api_cidr = live.get("swarm_api_egress_cidr")
    source = live.get("source", "swarm-api's subnet")
    if not api_cidr:
        print("FAIL the live input has no swarm_api_egress_cidr; nothing was compared")
        return 1
    in_system = {
        (p.get("metadata") or {}).get("name"): p
        for p in items
        if (p.get("metadata") or {}).get("namespace") == SYSTEM_NAMESPACE
    }
    differ = 0
    checks = (
        (SYSTEM_DENY_POLICY, check_system_deny),
        (LIVE_INGRESS_POLICY, lambda policy: check_live_ingress(policy, api_cidr)),
        (RELAY_INGRESS_POLICY, lambda policy: check_live_ingress(policy, api_cidr, RELAY_INGRESS_POLICY)),
    )
    for name, check in checks:
        where = f"{SYSTEM_NAMESPACE}/{name}"
        policy = in_system.get(name)
        # A missing object is a failure, never a pass: an allow without the
        # deny is not isolation, and a deny without the allow closes the relay.
        problems = check(policy) if policy is not None else [f"{where} does not exist"]
        if problems:
            differ += 1
            print(f"FAIL {where}")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"  ok {where} matches {api_cidr} ({source})")
    print(f"compared {len(checks)} policies in {SYSTEM_NAMESPACE} with {source}: {differ} differ")
    return 1 if differ else 0


def _main_graph_ingress(live: dict[str, Any], items: list[dict[str, Any]]) -> int:
    """The graph service's ingress in swarm-system, against swarm-api's live range."""
    api_cidr = live.get("swarm_api_egress_cidr")
    source = live.get("source", "swarm-api's subnet")
    if not api_cidr:
        print("FAIL the live input has no swarm_api_egress_cidr; nothing was compared")
        return 1
    where = f"{SYSTEM_NAMESPACE}/{GRAPH_INGRESS_POLICY}"
    found = [
        p for p in items
        if (p.get("metadata") or {}).get("namespace") == SYSTEM_NAMESPACE
        and (p.get("metadata") or {}).get("name") == GRAPH_INGRESS_POLICY
    ]
    # A missing policy is a failure, never a pass: nothing was compared.
    problems = check_graph_ingress(found[0], api_cidr) if found else [f"{where} does not exist"]
    if problems:
        print(f"FAIL {where}")
        for problem in problems:
            print(f"       {problem}")
        return 1
    print(f"  ok {where} matches {api_cidr} ({source})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", required=True, help="JSON: pod_cidr, service_cidr, cluster_dns_ip, node_local_dns_ip, source")
    parser.add_argument("--policies", required=True, help="`kubectl get networkpolicies -o json` output")
    parser.add_argument("--namespace-prefix", default="swarm-tenant-")
    parser.add_argument(
        "--live-ingress",
        action="store_true",
        help=(
            f"compare {SYSTEM_NAMESPACE}'s {LIVE_INGRESS_POLICY}, {RELAY_INGRESS_POLICY} and {SYSTEM_DENY_POLICY} with "
            "--live's swarm_api_egress_cidr instead of the tenant egress policies"
        ),
    )
    parser.add_argument(
        "--graph-ingress",
        action="store_true",
        help=(
            f"compare {SYSTEM_NAMESPACE}'s {GRAPH_INGRESS_POLICY} with --live's swarm_api_egress_cidr "
            "instead of the tenant egress policies"
        ),
    )
    args = parser.parse_args(argv)

    with open(args.live, encoding="utf-8") as handle:
        live = json.load(handle)
    if args.live_ingress:
        with open(args.policies, encoding="utf-8") as handle:
            return _main_live_ingress(live, _items(json.load(handle)))
    if args.graph_ingress:
        with open(args.policies, encoding="utf-8") as handle:
            return _main_graph_ingress(live, _items(json.load(handle)))
    missing = [key for key, _ in RENDERED if not live.get(key)]
    if missing:
        print(f"FAIL the live network is incomplete: missing {', '.join(missing)}")
        return 1
    with open(args.policies, encoding="utf-8") as handle:
        items = _items(json.load(handle))

    selected = sorted(
        (
            p for p in items
            if (p.get("metadata") or {}).get("name") == POLICY_NAME
            and str((p.get("metadata") or {}).get("namespace", "")).startswith(args.namespace_prefix)
        ),
        key=lambda p: p["metadata"].get("namespace", ""),
    )
    source = live.get("source", "the live cluster")
    if not selected:
        # An empty sweep is not agreement. It is nothing compared, and a check
        # that passes on nothing reports an agreement it never established.
        print(
            f"FAIL no {POLICY_NAME} in any {args.namespace_prefix}* namespace; nothing was "
            f"compared with {source}"
        )
        return 1

    differ = 0
    for policy in selected:
        where = f"{policy['metadata'].get('namespace')}/{POLICY_NAME}"
        rendered_for = ((policy.get("metadata") or {}).get("annotations") or {}).get(
            SOURCE_ANNOTATION, "(not recorded)"
        )
        problems = check_policy(policy, live)
        if problems:
            differ += 1
            print(f"FAIL {where} (rendered for: {rendered_for})")
            for problem in problems:
                print(f"       {problem}")
        else:
            print(f"  ok {where} matches {source}")

    noun = "policy" if len(selected) == 1 else "policies"
    print(f"compared {len(selected)} {noun} with {source}: {differ} differ")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main())
