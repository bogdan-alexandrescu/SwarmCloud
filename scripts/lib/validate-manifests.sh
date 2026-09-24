#!/usr/bin/env bash
# Validate every Kubernetes manifest this repository can produce.
#
# The manifests in kubernetes/ are TEMPLATES with `__TOKEN__` placeholders, not
# applyable YAML -- `__SECRET_ENV__` is a structural block that is several lines
# or nothing at all, so a raw template does not even parse as YAML. Running
# `kubectl apply --dry-run` over the files on disk therefore reports errors that
# are not errors, which is worse than not checking at all: it trains people to
# ignore the output.
#
# So this renders them with kubernetes/render.py -- the same renderer an operator
# uses, reading sizing from the frozen catalogue -- and validates the result
# against a pinned kubectl (old clients silently DROP fields they do not
# understand, so a manifest can "validate" while losing its security context).
#
# NOTE ON SHAPE: every check writes its render to a file and is invoked as a
# plain command, never `render | check`. A function on the right of a pipe runs
# in a SUBSHELL, so a failure count incremented there is discarded and the
# script reports success while printing errors. That bug was real here once.
#
# Usage: scripts/lib/validate-manifests.sh [--tenant lint]

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

TENANT="lint"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)  TENANT="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

RENDER="${REPO_ROOT}/kubernetes/render.py"
[[ -f "${RENDER}" ]] || die "no ${RENDER}"

require_cmd python3

WORK="$(mktemp -d "${TMPDIR:-/tmp}/swarm-manifests.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

FAILED=0

# check DESCRIPTION -- COMMAND...
# Renders with COMMAND, then validates. Runs in THIS shell, so FAILED sticks.
check() {
  local description="$1"; shift
  local out="${WORK}/render.yaml"
  local errfile="${WORK}/render.err"

  if ! "$@" >"${out}" 2>"${errfile}"; then
    err "${description}: render failed"
    sed 's/^/    /' <"${errfile}" >&2
    FAILED=1
    return 0
  fi

  # render.py refuses to emit an unrendered placeholder; this is a second,
  # independent check on the same property, because a placeholder that survives
  # into a cluster is a pod running with a literal "__TENANT_ID__".
  if grep -qE '__[A-Z0-9_]+__' "${out}"; then
    err "${description}: unrendered placeholder(s):"
    grep -oE '__[A-Z0-9_]+__' "${out}" | sort -u | sed 's/^/    /' >&2
    FAILED=1
    return 0
  fi

  # OFFLINE, with no cluster. `kubectl apply --dry-run=client` is not offline:
  # it still calls the API server to resolve kinds through the RESTMapper, so in
  # CI it fails with `couldn't get current server API group list:
  # Get "http://localhost:8080/api"`. It passed on a laptop only because a
  # kubeconfig happened to point somewhere reachable -- green locally, red in CI,
  # for a reason that had nothing to do with the manifests.
  #
  # What actually matters here is structural soundness plus the security
  # properties asserted below, and neither needs a cluster.
  if ! python3 - "${out}" >"${WORK}/names" 2>"${WORK}/err" <<'VALIDATE'; then
import sys, yaml

path = sys.argv[1]
try:
    docs = [d for d in yaml.safe_load_all(open(path)) if d]
except yaml.YAMLError as exc:
    print(f"not parseable as YAML: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not docs:
    print("rendered no objects", file=sys.stderr)
    raise SystemExit(1)

problems = []
for i, doc in enumerate(docs):
    if not isinstance(doc, dict):
        problems.append(f"document {i} is {type(doc).__name__}, not a mapping")
        continue
    for field in ("apiVersion", "kind"):
        if not doc.get(field):
            problems.append(f"document {i} has no {field}")
    name = (doc.get("metadata") or {}).get("name")
    generate = (doc.get("metadata") or {}).get("generateName")
    if not name and not generate:
        problems.append(f"{doc.get('kind', 'document')} {i} has no metadata.name")
    if name:
        print(f"{str(doc.get('kind','?')).lower()}/{name}")
    elif generate:
        print(f"{str(doc.get('kind','?')).lower()}/{generate}(generated)")

if problems:
    for p in problems:
        print(p, file=sys.stderr)
    raise SystemExit(1)
VALIDATE
    err "${description}:"
    sed 's/^/    /' <"${WORK}/err" >&2
    FAILED=1
    return 0
  fi

  ok "${description} ($(grep -c . <"${WORK}/names") objects)"
}

check "cluster policies"            python3 "${RENDER}" policies
check "tenant namespace (${TENANT})" python3 "${RENDER}" tenant --tenant "${TENANT}"

# One Job per runner profile, because the profiles differ in exactly the fields
# that are easy to get wrong: image, resource class, secret block and backend.
PROFILES="$(python3 - "${REPO_ROOT}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "apps" / "common"))
from swarm_common.profiles import RUNNER_PROFILES
print(" ".join(sorted(RUNNER_PROFILES)))
PY
)"
[[ -n "${PROFILES}" ]] || die "could not read RUNNER_PROFILES from the frozen catalogue"

for profile in ${PROFILES}; do
  check "worker job (${profile})" \
    python3 "${RENDER}" job \
      --tenant "${TENANT}" \
      --profile "${profile}" \
      --task "tsk_lint" \
      --attempt "att_lint" \
      --lease "lease_lint" \
      --generation 1
done

# The v2 shape too. It is not yet the substrate in service, and a template that
# nothing renders is a template that is broken the day it IS wanted -- which is
# the day someone is trying to dispatch a real agent and has no appetite for a
# YAML bug. Rendered at `baseline` because a root pod is refused at
# `restricted`, which is the check itself, not a workaround for it.
for profile in ${PROFILES}; do
  check "worker job v2/gvisor (${profile})" \
    python3 "${RENDER}" job \
      --tenant "${TENANT}" \
      --profile "${profile}" \
      --task "tsk_lint" \
      --attempt "att_lint" \
      --lease "lease_lint" \
      --generation 1 \
      --runtime gvisor \
      --pss-enforce baseline \
      --account "lint-account"
done

# And the refusal itself, because it is a safety property rather than a
# convenience: rendering a root pod for a `restricted` namespace must FAIL.
# Without this, the guard could be deleted and every other check would pass.
if python3 "${RENDER}" job --tenant "${TENANT}" --profile mock \
     --task tsk_lint --attempt att_lint --lease lease_lint --generation 1 \
     --runtime gvisor >/dev/null 2>&1; then
  err "a gvisor job rendered at pss-enforce=restricted; that pod would be refused by the API server"
  FAILED=$((FAILED + 1))
else
  ok "a root pod is refused for a restricted namespace"
fi

# ---------------------------------------------------------------------------
# Security posture, per isolation class
# ---------------------------------------------------------------------------
#
# WHY THIS LIVES HERE AND NOT ONLY IN CI. It used to be inline in
# .github/workflows/application.yml and nowhere else, so `make lint` passed on
# a tree that failed CI -- which is the exact shape CLAUDE.md warns about:
# "every rule that got restated in a second place here has since drifted." The
# workflow calls this script now, so there is one statement of the rule.
#
# WHY THE RULE HAS TWO BRANCHES. The old form asserted `runAsNonRoot: true` and
# `readOnlyRootFilesystem: true` on EVERY worker template. worker-job-v2.yaml
# sets the opposite on purpose -- `runAsUser: 0`, `readOnlyRootFilesystem:
# false`, "Root with a writable root filesystem -- the point of v2" -- and pays
# for it with an isolation boundary the other two do not have:
# `runtimeClassName: gvisor`, itself commented as the load-bearing line in the
# file. One posture for both classes cannot be satisfied by both.
#
# So each class is held to the posture that matches its isolation. THIS IS NOT
# AN OPT-OUT: a gvisor template is not exempted, it is checked against a
# different and equally specific list, and a template that names gvisor while
# granting capabilities or leaving its uid implicit fails here. The one thing
# this must never become is "skip the checks when the word gvisor appears",
# which would turn the gate off for the template that most needs one.
step "Worker template security posture"

TEMPLATES="${REPO_ROOT}/kubernetes/worker-templates"
if [[ ! -d "${TEMPLATES}" ]]; then
  # Absence is a failure of the check, not a pass -- the same reasoning
  # security.yml gives for its own Spot check on this directory.
  err "kubernetes/worker-templates is missing; the posture check cannot run"
  FAILED=1
else
  posture_found=0
  for f in "${TEMPLATES}"/*.yaml; do
    [[ -e "${f}" ]] || continue
    posture_found=$((posture_found + 1))
    name="$(basename "${f}")"

    # Required of EVERY template, whatever isolates it.
    grep -q 'allowPrivilegeEscalation: false' "${f}" \
      || { err "${name}: missing allowPrivilegeEscalation: false"; FAILED=1; }
    grep -q 'drop: \["ALL"\]' "${f}" \
      || { err "${name}: does not drop ALL capabilities"; FAILED=1; }
    if grep -q 'restartPolicy' "${f}"; then
      grep -q 'restartPolicy: Never' "${f}" \
        || { err "${name}: restartPolicy must be Never"; FAILED=1; }
    fi

    if grep -q 'runtimeClassName: gvisor' "${f}"; then
      # SANDBOXED CLASS. Root is permitted because the kernel boundary is not
      # the host's. What is required instead is that the privilege is
      # DELIBERATE and STATED: an implicit uid here is indistinguishable from
      # an oversight, and that is the thing review has to be able to see.
      grep -qE 'runAsUser: [0-9]+' "${f}" \
        || { err "${name}: gvisor template must state runAsUser explicitly"; FAILED=1; }
      ok "${name}: sandboxed class (gvisor), privilege stated explicitly"
    else
      # UNSANDBOXED CLASS. Shares the node's kernel, so it gets the full
      # posture the gate has always required.
      grep -q 'runAsNonRoot: true' "${f}" \
        || { err "${name}: missing runAsNonRoot: true (no gvisor sandbox)"; FAILED=1; }
      grep -q 'readOnlyRootFilesystem: true' "${f}" \
        || { err "${name}: missing readOnlyRootFilesystem: true (no gvisor sandbox)"; FAILED=1; }
      ok "${name}: unsandboxed class, non-root and read-only"
    fi
  done
  if [[ "${posture_found}" -eq 0 ]]; then
    err "no worker templates found under kubernetes/worker-templates"
    FAILED=1
  fi
fi

if [[ "${FAILED}" -ne 0 ]]; then
  die "kubernetes manifest validation failed"
fi
ok "every rendered manifest is valid"
