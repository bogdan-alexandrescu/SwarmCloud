"""The live browser's network path: only TCP 8443, only to the relay, only from
a live-browser pod (docs/design/live-browser.md §8.3; lane LB-L6, part of #1030).

Two NetworkPolicies open the one path the design adds, and a third closes
everything else in `swarm-system`:

  * `swarm-allow-live-egress`, in every tenant namespace, lets a pod labelled
    `swarm-live: enabled` -- which the dispatcher sets only on a
    `claude-code-browser` pod (`scheduler.dispatch.LIVE_POD_LABEL`) -- open TCP
    8443 to `app.kubernetes.io/name=swarm-live` pods in `swarm-system`. The
    existing tenant policies are untouched: NetworkPolicies are unions, so this
    adds a path and removes nothing, and they must render BYTE-IDENTICAL.
  * `swarm-system-default-deny-ingress` and `swarm-allow-live-ingress`, in
    `swarm-system`: nothing reaches a pod there except TCP 8443 to the relay,
    from a labelled pod in a tenant namespace (one peer, both selectors -- two
    peers would be an OR) or from swarm-api's Direct VPC egress range, which
    `kubernetes/apply.sh --swarm-system` READS from swarm-api's service and its
    subnet, never takes from a flag.

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

from scheduler.dispatch import LIVE_POD_LABEL

REPO = Path(__file__).resolve().parents[3]
KUBERNETES = REPO / "kubernetes"
APPLY = KUBERNETES / "apply.sh"
LIVE_EGRESS_FILE = "network-policies/allow-live-egress.yaml"
SYSTEM_FILE = "network-policies/swarm-system-live.yaml"

#: A swarm-api subnet range that appears nowhere in the repository, so a
#: rendered value that matches it can only have been READ.
FAKE_API_CIDR = "10.123.16.0/20"
FAKE_SUBNET = "swarm-fake-subnet"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = _load("swarm_k8s_render_live", KUBERNETES / "render.py")
parity = _load("swarm_k8s_parity_live", KUBERNETES / "network_parity.py")


def _run(*argv: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        assert render.main(list(argv)) == 0
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


# --- the tenant side ---------------------------------------------------------


def test_a_tenant_render_holds_the_live_egress_policy_and_it_passes_the_shape_check(tenant_docs):
    policy = _one(tenant_docs, parity.LIVE_EGRESS_POLICY)
    assert policy["metadata"]["namespace"] == "swarm-tenant-eng"
    assert policy["metadata"]["labels"]["managed-by"] == "swarm-terraform"
    assert parity.check_live_egress(policy) == []


def test_the_live_egress_policy_is_exactly_tcp_8443_to_the_relay_from_labelled_pods(tenant_docs):
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
            "ports": [{"protocol": "TCP", "port": 8443}],
        }
    ]


def test_the_policy_selects_the_label_the_dispatcher_sets():
    """`scheduler.dispatch.LIVE_POD_LABEL` is what a live-browser pod carries. A
    policy keyed on any other spelling selects nothing: the pod would have no
    live path, and the hand-off would time out with no error."""
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
PRE_L6_TENANT_RENDER = "91fad5854e2d1cc9ad0fac3a528641350ef75184a866538a20de7e80b8f66ad8"


def _tenant_values() -> dict[str, str]:
    import argparse

    parser = argparse.ArgumentParser()
    render.add_tenant_arguments(parser)
    return render.tenant_values(parser.parse_args(["--tenant", "eng"]))


def test_the_existing_policies_render_byte_identical():
    values = _tenant_values()
    for name, digest in PRE_L6_DIGESTS.items():
        rendered = render.render_files((name,), values)
        assert hashlib.sha256(rendered.encode()).hexdigest() == digest, f"{name} no longer renders as it did"


def test_the_live_policy_is_purely_appended_to_the_tenant_render():
    values = _tenant_values()
    assert render.TENANT_FILES[-1] == LIVE_EGRESS_FILE
    before = render.render_files(render.TENANT_FILES[:-1], values)
    assert hashlib.sha256(before.encode()).hexdigest() == PRE_L6_TENANT_RENDER
    after = render.render_files(render.TENANT_FILES, values)
    live = render.render_files((LIVE_EGRESS_FILE,), values)
    assert after == before.rstrip("\n") + "\n---\n" + live


def test_the_workspace_deployer_may_write_the_new_tenant_policy():
    """A personal workspace is applied by the workspace deployer, whose scope
    policy names every object the tenant render holds. Without the new name
    there, every `u-*` onboarding is refused at the API server."""
    text = (KUBERNETES / "policies" / "workspace-provisioner-scope.yaml").read_text()
    assert f'"{parity.LIVE_EGRESS_POLICY}"' in text


# --- the swarm-system side ---------------------------------------------------


def test_swarm_system_denies_every_ingress_by_default(system_docs):
    deny = _one(system_docs, parity.SYSTEM_DENY_POLICY)
    assert deny["metadata"]["namespace"] == "swarm-system"
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert parity.check_system_deny(deny) == []


def test_the_relay_admits_only_tcp_8443_from_labelled_pods_and_swarm_apis_range(system_docs):
    policy = _one(system_docs, parity.LIVE_INGRESS_POLICY)
    assert policy["metadata"]["namespace"] == "swarm-system"
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
        assert rule["ports"] == [{"protocol": "TCP", "port": 8443}]


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
        render.main(["swarm-system", *argv])
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
    mutant("a rule with no source")["spec"]["ingress"].append({"ports": [{"protocol": "TCP", "port": 8443}]})
    return mutants


def test_the_ingress_check_refuses_every_widening(system_docs):
    policy = _one(system_docs, parity.LIVE_INGRESS_POLICY)
    mutants = _mutants_of_ingress(policy)
    assert len(mutants) == 12
    passed = [name for name, m in mutants.items() if not parity.check_live_ingress(m, FAKE_API_CIDR)]
    assert passed == [], f"check_live_ingress accepted: {passed}"


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
    mutants["extra rule"]["spec"]["egress"].append({"ports": [{"protocol": "TCP", "port": 8443}]})
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


def test_the_live_ingress_comparison_passes_on_the_render_and_fails_on_drift(tmp_path, system_docs):
    live = {"swarm_api_egress_cidr": FAKE_API_CIDR, "source": "fake"}
    code, out = _parity_main(tmp_path, live, system_docs)
    assert code == 0, out
    code, out = _parity_main(tmp_path, {**live, "swarm_api_egress_cidr": "10.99.0.0/20"}, system_docs)
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
  *"config view"*) printf 'gke_saga-agents-staging_us-central1_swarm-autopilot' ;;
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

LEAKY = (
    "PROJECT_ID", "REGION", "ZONE", "ENVIRONMENT", "GKE_CLUSTER", "GKE_LOCATION", "API_SERVICE",
    "KUBECTL", "SWARM_KUBECTL", "KUBECONFIG", "SWARM_ENV_FILE", "GITHUB_ACTIONS",
    "CLOUDSDK_CONFIG", "GOOGLE_APPLICATION_CREDENTIALS",
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
    for name, body in (("kubectl", FAKE_KUBECTL), ("gcloud", FAKE_GCLOUD)):
        (b / name).write_text(body)
        (b / name).chmod(0o755)
    (d / "service.json").write_text(json.dumps(_service(f"projects/saga-agents-staging/regions/us-central1/subnetworks/{FAKE_SUBNET}")))
    (d / "subnet.json").write_text(json.dumps({"name": FAKE_SUBNET, "ipCidrRange": FAKE_API_CIDR}))
    env = {k: v for k, v in os.environ.items() if k not in LEAKY}
    env.update(
        PATH=f"{b}:{env.get('PATH', '')}",
        FAKE_CLUSTER_DIR=str(d),
        SWARM_KUBECTL=str(b / "kubectl"),
        SWARM_ENV_FILE=str(tmp_path / "no.env"),
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
    assert "read from" in result.stderr
    assert not (cluster["dir"] / "applied-dry-run.yaml").exists()


def test_apply_refuses_when_swarm_api_has_no_direct_vpc_subnet(cluster):
    (cluster["dir"] / "service.json").write_text(json.dumps(_service(None)))
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "subnetwork" in result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()


def test_apply_confirm_refuses_without_the_namespace(cluster):
    (cluster["dir"] / "no-namespace").write_text("")
    result = _apply(cluster, "--confirm")
    assert result.returncode != 0
    assert "swarm-system" in result.stderr
    assert not (cluster["dir"] / "applied.yaml").exists()
