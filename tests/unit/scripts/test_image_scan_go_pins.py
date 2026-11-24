"""Every image passes the release's image scan on the pins that can pass it.

WHY THIS EXISTS. On 2026-10-31 the acceptances for CVE-2026-78667,
CVE-2026-78669 and CVE-2026-97031 in .trivyignore.yaml expired, and pull
request 2877's build (job 124436228578) refused agent-runtime-browser on them,
in the vendor-built Go binaries docker, gh, kubectl, terraform, trivy and
Google's gcloud-crc32c and gke-gcloud-auth-plugin. The same pull request had
locked pyasn1 0.6.5 in the root uv.lock only, which left apps/swarm-live's lock
behind it. Lane IMAGE-SCAN-NOV (part of archived issue 2688) pinned every binary whose vendor
had shipped a fixed build, and accepted, for one week, only what no vendor had.

WHAT IS ASSERTED. No pin is at a release measured to carry a finding a newer
release fixes: each set below is a release read on 2026-10-31 (`go version -m`
on the vendor's own download, checked against its published checksum, and
`trivy rootfs --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed` over
it). The Dockerfiles record which version fixes what beside each pin.

NOT asserted: that a pin is free of advisories. That is a fact about a
vulnerability database on a date; the release's trivy step decides it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
BASE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
WORKSPACE_APPLY = REPO / "images" / "workspace-apply" / "Dockerfile"
INDEXER = REPO / "images" / "agent-runtime-indexer" / "Dockerfile"

#: Releases measured on 2026-10-31 to carry a HIGH a newer vendor release fixes.
REFUSED = {
    # go1.27.1, x/net v0.58.0 (CVE-2026-78667, -78669); 2.103.3 is go1.27.3, x/net v0.60.0,
    # but CVE-2026-97054 (net/http MaxBytesReader); 2.105.0 is go1.27.4 (measured 2026-11-20).
    "GH_VERSION": {"2.102.0", "2.103.3"},
    # go1.26.8 (CVE-2026-78667, -78669 in the stdlib); 29.9.2 is go1.26.10, but
    # CVE-2026-97054; 29.10.0 is go1.27.4 (measured 2026-11-20).
    "DOCKER_CLI_VERSION": {"29.8.2", "29.9.2"},
    # 1.16.4: go1.26.8, x/net v0.58.0; 1.16.7: go-jose 4.1.4 (CVE-2026-116587);
    # 1.17.1 is go1.26.10, x/net v0.62.0, go-jose 4.1.6.
    "TERRAFORM_VERSION": {"1.16.4", "1.16.7"},
    # 0.75.0: go1.27.1, x/net v0.59.0 (0.74.0 failed earlier, archived issue 442); 0.76.1 is go1.27.3, x/net v0.60.0
    # but go-jose 4.1.5 (CVE-2026-116587, refused once its acceptance expired 2026-11-11);
    # 0.76.2 is go1.27.3, x/net v0.62.0, go-jose 4.1.6 (measured 2026-11-11).
    "TRIVY_VERSION": {"0.74.0", "0.75.0", "0.76.1"},
    # gcloud-crc32c on go1.26.6 in 587.0.0; on go1.27.2 in 591.0.0.
    "GCLOUD_VERSION": {"587.0.0"},
    # gke-gcloud-auth-plugin 591.0.0: go1.27.1, x/net v0.59.0 (CVE-2026-78667,
    # -78669, -97031); 592.0.0 is go1.27.3, x/net v0.61.0 (measured 2026-11-06).
    "GKE_PLUGIN_VERSION": {"591.0.0"},
}

#: The Go that links the indexer's gopls and terraform-ls, and ships as its
#: toolchain: CVE-2026-78667, -78669 and -97031 are fixed in 1.27.2,
#: CVE-2026-111943 in 1.27.3 and CVE-2026-97054 in 1.27.4.
MIN_GO = (1, 27, 4)

#: pyasn1 0.6.4 carries the findings 0.6.5 fixes on the swarm-api image.
MIN_PYASN1 = (0, 6, 5)

#: kubectl releases measured to link golang.org/x/net below 0.57.0, the release that
#: fixes CVE-2026-39821 (x/net/idna): 1.34.13, 1.35.10 and 1.36.6 link v0.56.0, read
#: from each binary's build info and by trivy rootfs on 2026-11-24. 1.37.2 links
#: v0.57.0, but kubectl supports one minor either side of the 1.35 control plane.
KUBECTL_IDNA_UNFIXED = {"1.34.13", "1.35.10", "1.36.6"}


def _args(path: Path) -> dict[str, str]:
    return dict(re.findall(r"^ARG ([A-Z0-9_]+)=(\S+)$", path.read_text(), re.M))


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def test_no_agent_runtime_pin_is_at_a_release_the_scan_refuses() -> None:
    args = _args(BASE)
    checked = 0
    for var, refused in sorted(REFUSED.items()):
        assert var in args, f"{var} is not pinned in {BASE.relative_to(REPO)}"
        assert args[var] not in refused, (
            f"{var}={args[var]} carries a HIGH a newer vendor release fixes (measured 2026-10-31)"
        )
        checked += 1
    assert checked == len(REFUSED)


def test_workspace_apply_takes_agent_runtime_bases_gcloud_and_its_image() -> None:
    base, apply = _args(BASE), _args(WORKSPACE_APPLY)
    for var in ("GCLOUD_VERSION", "GKE_PLUGIN_VERSION", "GKE_PLUGIN_POOL_ID", "GKE_PLUGIN_SHA256",
                "KUBECTL_VERSION", "KUBECTL_SHA256"):
        assert apply[var] == base[var], f"workspace-apply's {var} is not agent-runtime-base's"
    image = apply["GCLOUD_IMAGE"]
    assert f"google-cloud-cli:{base['GCLOUD_VERSION']}-stable@sha256:" in image, image


def test_the_idna_acceptance_covers_kubectl_alone_and_leaves_with_the_unfixed_pin() -> None:
    # 2026-11-24: the scan refused six images on this id in usr/local/bin/kubectl
    # and in nothing else, so the acceptance names that path (an id alone would
    # hide it in every binary, ours included) and goes once the pin is fixed.
    entries = [e for e in yaml.safe_load((REPO / ".trivyignore.yaml").read_text())["vulnerabilities"]
               if e["id"] == "CVE-2026-39821"]
    unfixed = _args(BASE)["KUBECTL_VERSION"] in KUBECTL_IDNA_UNFIXED
    assert [e.get("paths") for e in entries] == ([["usr/local/bin/kubectl"]] if unfixed else []), entries
    assert all(e.get("statement") and e.get("expired_at") for e in entries), entries


def test_the_indexers_go_carries_the_fixed_standard_library() -> None:
    go = _args(INDEXER)["GO_VERSION"]
    assert _version(go) >= MIN_GO, f"GO_VERSION={go} is below {'.'.join(map(str, MIN_GO))}"


def test_every_lock_that_carries_pyasn1_locks_the_fixed_release() -> None:
    locks = sorted(p for p in REPO.glob("**/uv.lock") if "node_modules" not in p.parts and ".venv" not in p.parts)
    carrying = {}
    for lock in locks:
        for pkg in tomllib.loads(lock.read_text())["package"]:
            if pkg["name"] == "pyasn1":
                carrying[str(lock.relative_to(REPO))] = pkg["version"]
    assert "uv.lock" in carrying and "apps/swarm-live/uv.lock" in carrying, carrying
    stale = {path: v for path, v in carrying.items() if _version(v) < MIN_PYASN1}
    assert stale == {}, f"pyasn1 below 0.6.5 in {stale}"
