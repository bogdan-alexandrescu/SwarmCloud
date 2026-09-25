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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", required=True, help="JSON: pod_cidr, service_cidr, cluster_dns_ip, node_local_dns_ip, source")
    parser.add_argument("--policies", required=True, help="`kubectl get networkpolicies -o json` output")
    parser.add_argument("--namespace-prefix", default="swarm-tenant-")
    args = parser.parse_args(argv)

    with open(args.live, encoding="utf-8") as handle:
        live = json.load(handle)
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
