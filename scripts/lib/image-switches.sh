#!/usr/bin/env bash
# The images an environment deploys only while a tfvars switch is on, and how
# that switch is read. Sourced, never executed, by build-images.sh (a release
# does not rebuild a switched-off image) and push-images.sh (a switched-off
# image is not promoted and does not block). Needs common.sh first.
#
# WHY ONE PLACE. Two copies of this table, or of the reading of `enabled =
# false`, would be a build that leaves an image out while the promotion still
# expects it, or the reverse.
#
# SWITCHED_IMAGES maps an image to the top-level object in the environment's
# <env>.tfvars whose `enabled` switches on the one
# thing that runs it: swarm-speech is pulled only by the speech machine, which
# voice_speech.tf makes only while voice_speech.enabled (locals.tf renders
# SWARM_SPEECH_IMAGE "" otherwise).
#
# swarm-plugin-intake is deliberately NOT here: terraform/infra/plugins.tf
# plans its intake job on every deployment once image_refs carries the digest,
# and no tfvars block switches the job off (plugins_switch is swarm-api's
# runtime setting, not a deployment's), so every release builds and promotes it.
#
# Only a literal `enabled = false` in that environment's own tfvars switches an
# image off. An absent file or block, a file that cannot be read, a value that
# is not literally `false`: the image counts as deployed -- the safe direction,
# since a deployed image is then still gated, never quietly dropped.
#
# WHICH FILE. The real tfvars are not tracked (docs/design/
# generic-repository.md, sections 2.2-2.3): they live under config/<env>/ in
# the state bucket. The release actions fetch them with
# `scripts/lib/resolve.sh tfvars <env>` (which fails closed, naming the object)
# and pass the path on as SWARM_TFVARS_FILE, so a release reads exactly the
# file its plan reads. SWARM_TFVARS_FILE is that ENVIRONMENT's file only: for
# another environment, or when it is unset (a laptop), the operator's copy at
# terraform/environments/<env>/<env>.tfvars is read, which git ignores. A
# SWARM_TFVARS_FILE that names no readable file is said on stderr, and the
# image counts as deployed.

set -euo pipefail

SWITCHED_IMAGES=("swarm-speech=voice_speech")

# The tfvars switch for image $1, or nothing when no switch deploys it.
image_switch() {
  local entry
  for entry in "${SWITCHED_IMAGES[@]}"; do
    if [[ "${entry%%=*}" == "$1" ]]; then
      printf '%s' "${entry#*=}"
      return 0
    fi
  done
}

# The tfvars file environment $1's switches are read from (see WHICH FILE).
switch_tfvars() {
  local env="$1"
  if [[ -n "${SWARM_TFVARS_FILE:-}" && "${env}" == "${ENVIRONMENT:-}" ]]; then
    [[ -f "${SWARM_TFVARS_FILE}" && -r "${SWARM_TFVARS_FILE}" ]] \
      || printf 'image-switches: SWARM_TFVARS_FILE names no readable file; every switched image counts as deployed in %s\n' "${env}" >&2
    printf '%s' "${SWARM_TFVARS_FILE}"
    return 0
  fi
  printf '%s' "${REPO_ROOT}/terraform/environments/${env}/${env}.tfvars"
}

# Does environment $2's tfvars say, literally, `<switch $1> = { ... enabled =
# false ... }`? Comments are dropped first, so a commented-out line switches
# nothing; the block ends where its braces balance.
switch_is_off() {
  local switch="$1" env="$2" tfvars
  tfvars="$(switch_tfvars "${env}")"
  [[ -f "${tfvars}" && -r "${tfvars}" ]] || return 1
  awk -v key="${switch}" '
    !open && !closed && $0 ~ ("^" key "[[:space:]]*=[[:space:]]*[{]") { open = 1; depth = 0 }
    open {
      line = $0
      sub(/(#|\/\/).*/, "", line)
      if (line ~ /(^|[{,[:space:]])enabled[[:space:]]*=[[:space:]]*false([[:space:],}]|$)/) off = 1
      if (line ~ /(^|[{,[:space:]])enabled[[:space:]]*=/ && line !~ /enabled[[:space:]]*=[[:space:]]*false([[:space:],}]|$)/) on = 1
      depth += gsub(/[{]/, "{", line) - gsub(/[}]/, "}", line)
      if (depth <= 0) { open = 0; closed = 1 }
    }
    END { exit !(closed && off && !on) }
  ' "${tfvars}" 2>/dev/null
}

# The image $1 if environment $2 switches it off, else nothing: the switch, so
# a caller can say which one.
image_switched_off() {
  local switch
  switch="$(image_switch "$1")"
  [[ -n "${switch}" ]] && switch_is_off "${switch}" "$2" && printf '%s' "${switch}"
}
