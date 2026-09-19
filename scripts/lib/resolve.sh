#!/usr/bin/env bash
# Resolve one tool path, or one configuration value, exactly as the scripts do.
#
# The Makefile needs both, and used to derive both itself: it hard-coded its own
# `if [ -x $HOME/.local/bin/terraform ]` probes and read no .env at all. Two
# consequences, both silent:
#
#   * SWARM_TERRAFORM / SWARM_TFLINT / SWARM_CHECKOV / SWARM_TRIVY /
#     SWARM_KUBECTL are documented in .env.example and honoured by common.sh,
#     but had no effect on `make lint`, `make tf-plan` or `make security`. On a
#     machine where the wrong version wins $PATH -- which is this one, for both
#     kubectl and checkov -- make and the scripts would run different binaries.
#   * with ENVIRONMENT=staging in .env, `make tf-plan` planned dev while
#     `make status` reported staging, because only the scripts read .env.
#
# So the Makefile asks this, and this asks common.sh. One implementation.
#
# Usage:
#   scripts/lib/resolve.sh tool terraform      # absolute path, or empty
#   scripts/lib/resolve.sh env  ENVIRONMENT    # value after .env is applied
#
# Prints nothing and exits 0 when a tool is absent: the Makefile treats an empty
# value as "skip that step", and a missing tflint must not break `make lint`.

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

KIND="${1:-}"
NAME="${2:-}"
[[ -n "${KIND}" && -n "${NAME}" ]] || { printf 'usage: resolve.sh {tool|env} NAME\n' >&2; exit 64; }

case "${KIND}" in
  tool)
    case "${NAME}" in
      # kubectl has its own resolver because the requirement is a VERSION, not a
      # path: 1.22 and 1.25 win $PATH here and a 1.22 client silently drops
      # fields it does not understand while reporting success.
      # A subshell, not a bare call: kubectl_bin's failure path is `die`, which
      # calls `exit`, not `return`. `exit` inside a function terminates the
      # whole process -- a `||` after a bare call never gets a chance to run,
      # so the version diagnosis (candidates checked, the 1.22/1.25 explanation,
      # the SWARM_KUBECTL hint) has to reach stderr before that happens, and
      # the "|| true" that follows has to be catching a real subshell exit
      # status, not dead code. Running it in `( )` confines the exit to the
      # subshell so `|| true` actually applies, while stdout (empty on
      # failure) and stderr (the diagnosis) both still propagate normally.
      kubectl)   ( kubectl_bin ) || true ;;
      terraform) prefer_local_bin terraform "${SWARM_TERRAFORM:-}" || true ;;
      tflint)    prefer_local_bin tflint    "${SWARM_TFLINT:-}"    || true ;;
      checkov)   prefer_local_bin checkov   "${SWARM_CHECKOV:-}"   || true ;;
      trivy)     prefer_local_bin trivy     "${SWARM_TRIVY:-}"     || true ;;
      *) printf 'resolve.sh: unknown tool %s\n' "${NAME}" >&2; exit 64 ;;
    esac
    ;;
  env)
    case "${NAME}" in
      ENVIRONMENT|PROJECT_ID|REGION|ZONE|FIRESTORE_DATABASE|ARTIFACT_BUCKET|\
      ARTIFACT_REGISTRY|TF_STATE_BUCKET|TF_STATE_PREFIX|GKE_CLUSTER|GKE_LOCATION)
        printf '%s' "${!NAME}" ;;
      *) printf 'resolve.sh: %s is not an exported configuration value\n' "${NAME}" >&2; exit 64 ;;
    esac
    ;;
  *)
    printf 'usage: resolve.sh {tool|env} NAME\n' >&2; exit 64 ;;
esac
