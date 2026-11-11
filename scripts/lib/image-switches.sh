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
# SWITCHED_IMAGES maps an image to the top-level object in
# terraform/environments/<env>/<env>.tfvars whose `enabled` switches on the one
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
# is not literally `false`: the image counts as deployed.

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

# Does environment $2's tfvars say, literally, `<switch $1> = { ... enabled =
# false ... }`? Comments are dropped first, so a commented-out line switches
# nothing; the block ends where its braces balance.
switch_is_off() {
  local switch="$1" env="$2" tfvars
  tfvars="${REPO_ROOT}/terraform/environments/${env}/${env}.tfvars"
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
