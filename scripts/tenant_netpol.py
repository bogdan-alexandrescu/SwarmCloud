"""The tenant NetworkPolicy set, and ONLY it, rendered for one tenant namespace.

    python3 scripts/tenant_netpol.py --list
    python3 scripts/tenant_netpol.py render --tenant eng --namespace swarm-tenant-eng \
        --pod-cidr ... --service-cidr ... --cluster-dns-ip ... \
        --node-local-dns-ip ... --network-source gke/... --summary objects.tsv

Used by `scripts/apply-tenant-network-policies.sh` (lane H1-NETPOL), which the
release runs over every namespace labelled `swarm-tenant`.

WHY NOT THE WHOLE TENANT RENDER. `kubernetes/apply.sh --tenant` renders the
namespace, its ResourceQuota, its accounts and RBAC as well, with this
renderer's defaults for anything not passed. A workspace tenant
(`swarm-tenant-u-*`) is provisioned with its own quota and GSA
(`scripts/register-tenant.sh --workspace`); re-rendering it with defaults would
overwrite them. The NetworkPolicies take nothing from those inputs -- only the
namespace, the tenant id and the cluster's network -- so they are the one part
of a tenant render that can be re-applied to every tenant from what the
cluster itself says.

THE SET IS DERIVED, NEVER LISTED HERE: every `network-policies/` entry of
`kubernetes/render.py` TENANT_FILES, the same tuple a full tenant apply and the
workspace provisioner render. tests/unit/scripts/test_tenant_netpol_apply.py
holds that set equal to the files in kubernetes/network-policies/ whose
templates are namespaced `__NAMESPACE__`, so a new tenant policy file that is
not in TENANT_FILES fails a test instead of silently missing every namespace
(measured 2026-10-11: `swarm-allow-live-egress` was applied by hand to eng,
smoke and u-sw-c90291).
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
RENDER = REPO / "kubernetes" / "render.py"
POLICY_DIR = "network-policies/"


def load_render() -> ModuleType:
    spec = importlib.util.spec_from_file_location("swarm_k8s_render_netpol", RENDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def policy_files(render: ModuleType) -> tuple[str, ...]:
    """The tenant NetworkPolicy templates, in TENANT_FILES' apply order."""
    files = tuple(name for name in render.TENANT_FILES if name.startswith(POLICY_DIR))
    if not files:
        raise SystemExit("tenant_netpol: kubernetes/render.py TENANT_FILES names no network policy")
    return files


_KIND = re.compile(r"^kind:[ \t]*(\S+)[ \t]*$", re.M)
_NAME = re.compile(r"^  name:[ \t]*(\S+)[ \t]*$", re.M)
_NAMESPACE = re.compile(r"^  namespace:[ \t]*(\S+)[ \t]*$", re.M)


def summary(manifest: str) -> list[tuple[str, str, str]]:
    """`(kind, name, namespace)` per document, read from the top-level `kind:`
    and the first two-space `name:`/`namespace:` (metadata's, in these
    templates). No YAML library: this runs on a release runner's bare python3,
    as render.py does. The test holds it equal to a YAML parse of the render.
    """
    rows = []
    for document in manifest.split("\n---\n"):
        kind, name, namespace = (
            pattern.search(document) for pattern in (_KIND, _NAME, _NAMESPACE)
        )
        if not (kind and name and namespace):
            raise SystemExit(
                "tenant_netpol: a rendered document has no kind, metadata.name or "
                "metadata.namespace; refusing to apply what cannot be checked"
            )
        rows.append((kind.group(1), name.group(1), namespace.group(1)))
    return rows


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    render = load_render()
    if argv == ["--list"]:
        sys.stdout.write("".join(f"{name}\n" for name in policy_files(render)))
        return 0
    if not argv or argv[0] != "render":
        print("usage: tenant_netpol.py --list | render <render.py tenant flags>", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="tenant_netpol.py render", allow_abbrev=False)
    render.add_tenant_arguments(parser)
    parser.add_argument(
        "--summary",
        type=Path,
        required=True,
        help="write `kind<TAB>name<TAB>namespace` per rendered document here",
    )
    args = parser.parse_args(argv[1:])
    try:
        values = render.tenant_values(args)
        if values["NETWORK_SOURCE"] == render.OFFLINE_NETWORK_SOURCE:
            # The egress policy would carry documentation addresses.
            raise render.RenderError(
                "tenant_netpol: no cluster network was given; refusing to render a "
                "policy marked offline-render-not-for-apply"
            )
        manifest = render.render_files(policy_files(render), values)
    except render.RenderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = summary(manifest)
    # Belt and braces: only NetworkPolicies, only in the namespace asked for.
    for kind, name, namespace in rows:
        if kind != "NetworkPolicy" or namespace != values["NAMESPACE"]:
            print(
                f"tenant_netpol: rendered {kind} {namespace}/{name}; only NetworkPolicies in "
                f"{values['NAMESPACE']} may be applied",
                file=sys.stderr,
            )
            return 1
    args.summary.write_text("".join(f"{k}\t{n}\t{ns}\n" for k, n, ns in rows))
    sys.stdout.write(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
