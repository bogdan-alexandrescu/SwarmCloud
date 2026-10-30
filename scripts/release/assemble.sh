#!/usr/bin/env bash
# Assemble a SwarmCloud release bundle from this checkout (DAYONE-RELEASE-1;
# docs/design/day-one-plan.md section 7.2 step 4, design section 6).
#
#   scripts/release/assemble.sh --version V --images build/images-<env>.json --out DIR [--notes FILE]
#
# V is a release name, YYYY.MM.N or a candidate YYYY.MM.N-rc.N. --images is
# the build record scripts/build-images.sh writes (and the release-images
# action has scanned): every image's name, digest and source ref. DIR must not
# exist or be empty. Nothing here reaches Google Cloud; the publish is
# scripts/release/publish.sh, run by a person (D22).
#
# DIR then holds exactly what is uploaded to gs://swarmcloud/releases/V/:
#
#   module.tgz            terraform/{bootstrap,infra,modules}: no tfvars, no
#                         environments/ (design 5.1: the engine writes its own)
#   swarm_mcp-V-*.whl     the bridge: `swarm`, `sc`, the plugin's MCP server
#   swarm_install-V-*.whl the engine (two tool wheels, plan R6)
#   swarm_common-V-*.whl, swarm_redaction-V-*.whl
#                         the in-repo libraries those two require. They are
#                         not on PyPI, so they come from the release itself:
#                         a bare name would be an index lookup, and somebody
#                         else's code the day the name is registered
#   manifest.yaml         install/manifest.yaml merged with install/manifest.d/
#   images.json           {image: "sha256:<digest>"}: digests only (H28)
#   kubernetes.tgz        kubernetes/ (render.py, policies/, swarm-system/,
#                         rbac/, the tenant templates) plus the three packages
#                         render.py imports, so `python3 render.py` runs from
#                         the bundle with no checkout (review finding 3)
#   plugin/               the plugin, its bridge pointed at this release's
#                         wheels, with compat.json (plan 6.4-6.5)
#   compat.json           the deployment versions the plugin supports
#   marketplace.json      the marketplace pinned to V (source ./plugin)
#   cve-acceptances.json  every accepted CVE of .trivyignore.yaml, with its
#                         expiry (lesson 27; preflight.release reads it)
#   install.sh            install/install.sh, promoted to the bucket's root
#   RELEASE.md            the commit, the digests, the accepted CVEs, the notes
#   SHA256SUMS            every file above; publish.sh signs it
#
# Only files git tracks are shipped: a local tfvars, a .terraform/ directory or
# a __pycache__ never reaches a release. The tars are reproducible (owners,
# times and modes normalised), so one commit assembles one module.tgz digest.
#
# Exit codes: 0 assembled, 1 refused or failed, 2 bad arguments.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR/..
# shellcheck source=lib/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

VERSION=""; IMAGES=""; OUT=""; NOTES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) [[ $# -ge 2 ]] || usage; VERSION="$2"; shift 2 ;;
    --images)  [[ $# -ge 2 ]] || usage; IMAGES="$2"; shift 2 ;;
    --out)     [[ $# -ge 2 ]] || usage; OUT="$2"; shift 2 ;;
    --notes)   [[ $# -ge 2 ]] || usage; NOTES="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) err "unknown argument: $1"; usage ;;
  esac
done
[[ -n "${VERSION}" && -n "${IMAGES}" && -n "${OUT}" ]] || usage
if ! [[ "${VERSION}" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  err "--version ${VERSION} is not a release name (YYYY.MM.N or YYYY.MM.N-rc.N)"
  exit 2
fi
[[ -f "${IMAGES}" ]] || die "--images ${IMAGES}: no such file"
[[ -z "${NOTES}" || -f "${NOTES}" ]] || die "--notes ${NOTES}: no such file"
if [[ -e "${OUT}" ]] && [[ -n "$(ls -A "${OUT}")" ]]; then
  die "--out ${OUT} is not empty; a bundle is assembled into an empty directory"
fi
require_cmd git
require_cmd uv

COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
DIRTY=0
git_dirty && DIRTY=1
[[ "${DIRTY}" -eq 0 ]] || warn "the checkout has uncommitted changes; a release is assembled from a clean tag"

mkdir -p "${OUT}"
OUT="$(cd -- "${OUT}" && pwd)"
IMAGES="$(cd -- "$(dirname -- "${IMAGES}")" && pwd)/$(basename -- "${IMAGES}")"
WORK="$(mktemp -d)"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

# On failure nothing half-assembled is left to be mistaken for a bundle.
fail_clean() { rm -rf "${OUT:?}"/* "${OUT:?}"/.[!.]* 2>/dev/null || true; }
trap 'fail_clean; cleanup' ERR

step "Assembling SwarmCloud ${VERSION} from ${COMMIT:0:12}"
read -r -a PY <<<"$(swarm_python)"

if ! REPO_ROOT="${REPO_ROOT}" OUT="${OUT}" WORK="${WORK}" VERSION="${VERSION}" IMAGES="${IMAGES}" \
  NOTES="${NOTES}" COMMIT="${COMMIT}" DIRTY="${DIRTY}" "${PY[@]}" - <<'PY'
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import yaml

REPO = Path(os.environ["REPO_ROOT"])
OUT = Path(os.environ["OUT"])
WORK = Path(os.environ["WORK"])
VERSION = os.environ["VERSION"]
DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class Refused(Exception):
    pass


def say(text):
    print(f"  {text}", file=sys.stderr)


spec = importlib.util.spec_from_file_location("release_marketplace", REPO / "scripts" / "release" / "marketplace.py")
marketplace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marketplace)

tracked = [f for f in subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], check=True,
                                     capture_output=True).stdout.decode().split("\0")
           if f and (REPO / f).is_file()]


def under(prefix):
    return [f for f in tracked if f.startswith(prefix)]


def tgz(path, members):
    """members: [(name in archive, source file)]; reproducible."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, source in sorted(members):
            info = tarfile.TarInfo(name)
            data = source.read_bytes()
            info.size = len(data)
            info.mode = 0o755 if os.access(source, os.X_OK) else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as fh, gzip.GzipFile(filename="", fileobj=fh, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())


# -- images.json: digests only ----------------------------------------------------
record = json.loads(Path(os.environ["IMAGES"]).read_text())
pinned = json.loads((REPO / "terraform" / "infra" / "images.json").read_text())
required = [*pinned["platform"], *pinned["gated"]]
images, problems = {}, []
for entry in record.get("images") or []:
    name, digest, ref = str(entry.get("name") or ""), str(entry.get("digest") or ""), str(entry.get("ref") or "")
    if not name:
        problems.append("an entry has no name")
    elif name in images:
        problems.append(f"{name} is listed twice")
    elif not DIGEST.match(digest) or not ref.endswith("@" + digest):
        problems.append(f"{name}: {digest or 'no digest'} is not an image digest (a tag is never shipped, H28)")
    else:
        images[name] = digest
missing = [n for n in required if n not in images]
if missing:
    problems.append(f"the build record has no digest for {', '.join(missing)} (terraform/infra/images.json pins them)")
if problems:
    raise SystemExit("assemble: --images refused: " + "; ".join(problems))
(OUT / "images.json").write_text(json.dumps(dict(sorted(images.items())), indent=2) + "\n")
say(f"images.json: {len(images)} digests")

# -- cve-acceptances.json: every acceptance ends (lesson 27) --------------------------
today = dt.datetime.now(dt.timezone.utc).date()
accepted, problems = [], []
for item in (yaml.safe_load((REPO / ".trivyignore.yaml").read_text()) or {}).get("vulnerabilities") or []:
    cve, expires = str(item.get("id") or ""), item.get("expired_at")
    if not expires:
        problems.append(f"{cve} has no expiry")
        continue
    expires = dt.date.fromisoformat(str(expires))
    if expires < today:
        problems.append(f"{cve} expired on {expires.isoformat()}")
        continue
    accepted.append({"cve": cve, "package": "the release's images",
                     "reason": " ".join(str(item.get("statement") or "").split()), "expires": expires.isoformat()})
if problems:
    raise SystemExit("assemble: .trivyignore.yaml refused: " + "; ".join(problems)
                     + " (an acceptance always ends; renew it or fix the image)")
(OUT / "cve-acceptances.json").write_text(json.dumps(accepted, indent=2) + "\n")
say(f"cve-acceptances.json: {len(accepted)} accepted, each with its expiry")

# -- the wheels (plan R6) -----------------------------------------------------------------
wheel_version = marketplace.wheel_version(VERSION)
src = WORK / "src"
# The engine's wheel force-includes ../../install/manifest.yaml and manifest.d/
# (apps/swarm-install/pyproject.toml), so the copied tree keeps install/ two
# levels above src/swarm-install, as in the checkout.
for f in ["install/manifest.yaml"] + under("install/manifest.d/"):
    target = WORK / f
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / f, target)
for app in ("common", "redaction", "swarm-mcp", "swarm-install"):
    for f in under(f"apps/{app}/"):
        if "/tests/" in f:
            continue
        target = src / f[len("apps/"):]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, target)
    pyproject = src / app / "pyproject.toml"
    text, n = re.subn(r'(?m)^version = "[^"]*"$', f'version = "{wheel_version}"', pyproject.read_text(), count=1)
    if n != 1:
        raise SystemExit(f"assemble: apps/{app}/pyproject.toml has no version line to set")
    pyproject.write_text(text)
    built = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(WORK / "dist"), str(src / app)],
                           capture_output=True, text=True)
    if built.returncode != 0:
        raise SystemExit(f"assemble: uv build apps/{app} failed:\n{built.stderr[-2000:]}")
for wheel in sorted((WORK / "dist").glob("*.whl")):
    shutil.copy2(wheel, OUT / wheel.name)
    say(wheel.name)

# -- module.tgz (design 5.1) ------------------------------------------------------------------
def configures_a_deployment(f):
    parts = f.split("/")
    return ("environments" in parts or ".terraform" in parts or "tfstate" in f
            or f.endswith((".tfvars", ".tfvars.json")))

module = [(f, REPO / f) for root in ("terraform/bootstrap/", "terraform/infra/", "terraform/modules/")
          for f in under(root) if not configures_a_deployment(f)]
tgz(OUT / "module.tgz", module)
say(f"module.tgz: {len(module)} files")

# -- kubernetes.tgz (review finding 3) ------------------------------------------------------------
kube = [(f[len("kubernetes/"):], REPO / f) for f in under("kubernetes/")]
for package, home in (("swarm_common", "apps/common/swarm_common/"),
                      ("quota_broker", "apps/quota-broker/quota_broker/"),
                      ("scheduler", "apps/scheduler/scheduler/")):
    kube += [(package + "/" + f[len(home):], REPO / f) for f in under(home)]
tgz(OUT / "kubernetes.tgz", kube)
say(f"kubernetes.tgz: {len(kube)} files")

# -- manifest.yaml: the base and every fragment, merged ---------------------------------------------
from swarm_install.manifest import load

merged = load()
document = {
    "version": 1,
    "generated_from": f"install/manifest.yaml and install/manifest.d/, SwarmCloud {VERSION}",
    "entries": [{k: v for k, v in e.model_dump(mode="json", exclude_defaults=True).items() if k != "replace"}
                for e in merged.entries],
    "ignored": {k: [i.model_dump(mode="json", exclude_defaults=True) for i in v] for k, v in merged.ignored.items()},
    "env": {k: v.model_dump(mode="json", exclude_defaults=True) for k, v in merged.env.items()},
}
(OUT / "manifest.yaml").write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=120))
say(f"manifest.yaml: {len(merged.entries)} entries")

# -- the plugin, its compat.json and the pinned marketplace --------------------------------------------
for f in under("plugin/"):
    if f.endswith(".test.ts") or "/node_modules/" in f:
        continue
    target = OUT / f
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / f, target)
marketplace.prepare_plugin(OUT / "plugin", VERSION, REPO / "install" / "compat.json")
shutil.copy2(OUT / "plugin" / "compat.json", OUT / "compat.json")
(OUT / "marketplace.json").write_text(json.dumps(marketplace.marketplace(VERSION, "./plugin"), indent=2) + "\n")
shutil.copy2(REPO / "install" / "install.sh", OUT / "install.sh")
compat = json.loads((OUT / "compat.json").read_text())["deployments"]

# -- RELEASE.md ---------------------------------------------------------------------------------------------
notes_file = os.environ.get("NOTES") or ""
notes = Path(notes_file).read_text().strip() if notes_file else (
    "No upgrade notes or migrations were given for this release (`assemble.sh --notes`).")
commit = os.environ["COMMIT"] + (" (with uncommitted changes)" if os.environ.get("DIRTY") == "1" else "")
lines = [
    f"# SwarmCloud {VERSION}", "",
    f"Built from commit `{commit}`.", "",
    "## Install", "",
    "```", "curl -fsSL https://storage.googleapis.com/swarmcloud/install.sh | sh",
    f"swarm install --project <project> --env prod --version {VERSION} --domain <host>", "```", "",
    "## Notes, upgrades and migrations", "", notes, "",
    "## Accepted CVEs", "",
    "Each acceptance ends on its date; an installer refuses a release past one (lesson 27).", "",
]
lines += [f"- {a['cve']}, until {a['expires']}: {a['reason']}" for a in accepted] or ["None."]
lines += ["", "## Images", "", "| image | digest |", "|---|---|"]
lines += [f"| {n} | `{d}` |" for n, d in sorted(images.items())]
lines += ["", "## Plugin", "",
          f"`sc@swarmcloud` {VERSION}. It supports deployments from {compat['min'] or 'any earlier release'} "
          f"to {('before ' + compat['max_exclusive']) if compat['max_exclusive'] else 'any later release'}.", ""]
(OUT / "RELEASE.md").write_text("\n".join(lines))

# -- SHA256SUMS --------------------------------------------------------------------------------------------------
sums = []
for path in sorted(p for p in OUT.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
    sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(OUT).as_posix()}")
(OUT / "SHA256SUMS").write_text("\n".join(sums) + "\n")
say(f"SHA256SUMS: {len(sums)} files")
PY
then
  fail_clean
  die "release ${VERSION} was not assembled"
fi
ok "release ${VERSION} assembled in ${OUT}"
