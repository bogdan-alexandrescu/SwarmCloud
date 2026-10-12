#!/usr/bin/env bash
# The build mirrors: what an image build reads instead of ghcr.io, Docker Hub
# and GitHub release downloads (OPS-MIRROR, part of #1030;
# docs/runbooks/build-mirror.md). Sourced, never executed. Needs common.sh
# first.
#
# WHY. On 2026-10-12 three release image builds failed inside Cloud Build on
# fetches from outside Google: twice a ghcr.io timeout pulling the uv image
# (`FROM ${UV_IMAGE} AS uvbin`), once a github.com 504 downloading shellcheck
# in agent-runtime-base. Nothing about those images was wrong.
#
# TWO MIRRORS, both in terraform/infra/build_mirror.tf:
#
#   images  Artifact Registry REMOTE repositories, swarm-mirror-ghcr and
#           swarm-mirror-dockerhub. Every Dockerfile names its base as
#           ${MIRROR_GHCR}/<image> or ${MIRROR_DOCKER_HUB}/<image>, at the
#           digest it pinned before, and its `# syntax=` frontend the same
#           way. A remote repository fetches a digest from upstream once and
#           serves it from the cache after that.
#   tools   a GCS bucket, gs://${BUILD_MIRROR_BUCKET}, holding each release
#           file at its upstream URL without `https://`. The list, with the
#           sha256 the Dockerfile checks, is TOOL_MIRROR_MANIFEST. Cloud Build
#           copies them into build/tool-mirror (tool_mirror_step_yaml) before
#           the docker build, which bind-mounts that directory. An object
#           that is not there fails the step, naming it.
#
# A PULL REQUEST'S BUILD (`--build-only --local`, on a GitHub runner) has no
# Google identity by the owner's decision of 2026-10-05, so it can read
# neither mirror. It reads upstream instead: the release files from their
# URLs, checked against the same manifest (tool_mirror_stage_upstream), and
# the images through MIRROR_UPSTREAMS_JSON, which the local replay passes as
# build arguments. That runner is on GitHub's network, where github.com and
# ghcr.io are local.

set -euo pipefail

# The release files agent-runtime-base reads, `<sha256>  <object>` a line --
# the format `sha256sum -c` reads. tests/unit/scripts/test_build_mirror.py
# holds it equal to the Dockerfile's pins.
TOOL_MIRROR_MANIFEST="${REPO_ROOT}/images/agent-runtime-base/tool-mirror.sha256"
# Relative to the build context, which is the repository root -- and, in
# Cloud Build, /workspace, the directory every step starts in.
TOOL_MIRROR_CONTEXT_DIR="build/tool-mirror"

# Each mirror's build argument, the suffix of its repository's name and the
# registry it stands in for. The replay maps by suffix, not by the full path,
# so a Dockerfile that declares no MIRROR_* argument (agent-runtime-browser,
# whose only pull is its `# syntax=` frontend) is mapped the same way.
MIRROR_UPSTREAMS_JSON='{"MIRROR_DOCKER_HUB": {"suffix": "-mirror-dockerhub", "upstream": "docker.io"}, "MIRROR_GHCR": {"suffix": "-mirror-ghcr", "upstream": "ghcr.io"}}'

# tool_mirror_rows -- `<sha256> <object>` for every row of the manifest, or
# die on a row that is not one. An object is a path under one of the two
# hosts the base image fetched from; nothing in it can leave the mirror
# directory or close a quote in the step tool_mirror_step_yaml writes.
tool_mirror_rows() {
  local line sha obj rest n=0
  [[ -f "${TOOL_MIRROR_MANIFEST}" ]] || die "no tool mirror manifest at ${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    read -r sha obj rest <<<"${line}"
    [[ "${sha}" =~ ^[0-9a-f]{64}$ && -z "${rest:-}" ]] \
      || die "${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"}: not '<sha256>  <object>': ${line}"
    [[ "${obj}" =~ ^(github\.com|releases\.hashicorp\.com)/[A-Za-z0-9._+/-]+$ && "${obj}" != *..* ]] \
      || die "${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"}: ${obj} is not a github.com or releases.hashicorp.com path"
    printf '%s %s\n' "${sha}" "${obj}"
    n=$((n + 1))
  done <"${TOOL_MIRROR_MANIFEST}"
  [[ "${n}" -gt 0 ]] || die "${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"} lists nothing"
}

# dockerfile_uses_tool_mirror DOCKERFILE -- whether it bind-mounts the mirror.
# A path relative to the repository is taken from REPO_ROOT.
dockerfile_uses_tool_mirror() {
  local df="$1"
  [[ "${df}" == /* ]] || df="${REPO_ROOT}/${df}"
  [[ -f "${df}" ]] && grep -q "source=${TOOL_MIRROR_CONTEXT_DIR}," "${df}"
}

# sha256_of FILE -- its sha256. coreutils on Linux, shasum on macOS.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{ print $1 }'
  else
    shasum -a 256 "$1" | awk '{ print $1 }'
  fi
}

# tool_mirror_step_yaml -- the Cloud Build step that copies every manifest
# object from gs://${BUILD_MIRROR_BUCKET} into build/tool-mirror and checks
# each against its sha256, as a `steps:` list item.
#
# The script carries no `$` at all: Cloud Build reads every `$NAME` in a
# config as a substitution, and refuses one it was not given. Each object is
# one literal line, so a missing one is named by the line that failed.
tool_mirror_step_yaml() {
  local sha obj rows
  [[ "${BUILD_MIRROR_BUCKET}" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "BUILD_MIRROR_BUCKET is not a bucket name: ${BUILD_MIRROR_BUCKET}"
  rows="$(tool_mirror_rows)"
  cat <<YAML
  # Generated from ${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"} (scripts/lib/build-mirror.sh):
  # the release files the Dockerfile bind-mounts from ${TOOL_MIRROR_CONTEXT_DIR}.
  - id: tool-mirror
    name: gcr.io/cloud-builders/gcloud
    entrypoint: bash
    args:
      - -c
      - |
        set -euo pipefail
        rm -rf ${TOOL_MIRROR_CONTEXT_DIR}
YAML
  while read -r sha obj; do
    printf "        mkdir -p '%s/%s'\n" "${TOOL_MIRROR_CONTEXT_DIR}" "$(dirname "${obj}")"
    printf "        gcloud storage cp 'gs://%s/%s' '%s/%s' || { echo 'build mirror object gs://%s/%s is missing or unreadable: run scripts/refresh-build-mirror.sh (docs/runbooks/build-mirror.md)' >&2; exit 1; }\n" \
      "${BUILD_MIRROR_BUCKET}" "${obj}" "${TOOL_MIRROR_CONTEXT_DIR}" "${obj}" "${BUILD_MIRROR_BUCKET}" "${obj}"
  done <<<"${rows}"
  printf "        (cd %s && sha256sum -c --strict -) <<'SUMS' || { echo 'a build mirror object does not match its sha256 in %s: run scripts/refresh-build-mirror.sh --force' >&2; exit 1; }\n" \
    "${TOOL_MIRROR_CONTEXT_DIR}" "${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"}"
  while read -r sha obj; do
    printf '        %s  %s\n' "${sha}" "${obj}"
  done <<<"${rows}"
  printf '        SUMS\n'
}

# tool_mirror_fetch_upstream SHA OBJECT DEST -- download https://OBJECT to
# DEST and check it against SHA. Fails naming the object, and leaves nothing
# at DEST, when the download fails or the file is not the one pinned.
tool_mirror_fetch_upstream() {
  local sha="$1" obj="$2" dest="$3" got
  mkdir -p "$(dirname "${dest}")"
  rm -f "${dest}" "${dest}.part"
  if ! curl -fsSL --retry 3 --retry-delay 5 -o "${dest}.part" "https://${obj}" 2>"${dest}.err"; then
    redact <"${dest}.err" | head -n 3 >&2
    rm -f "${dest}.part" "${dest}.err"
    err "could not download https://${obj}"
    return 1
  fi
  rm -f "${dest}.err"
  got="$(sha256_of "${dest}.part")"
  if [[ "${got}" != "${sha}" ]]; then
    rm -f "${dest}.part"
    err "https://${obj} has sha256 ${got}, not the ${sha} ${TOOL_MIRROR_MANIFEST#"${REPO_ROOT}/"} pins; refusing it"
    return 1
  fi
  mv "${dest}.part" "${dest}"
}

# tool_mirror_stage_upstream -- what a pull request's local build does in
# place of the Cloud Build step: every manifest object from upstream into
# ${REPO_ROOT}/build/tool-mirror, each checked against its sha256. Dies on
# the first that fails, naming it, before anything is built.
tool_mirror_stage_upstream() {
  local sha obj rows dir n=0
  dir="${REPO_ROOT}/${TOOL_MIRROR_CONTEXT_DIR}"
  rows="$(tool_mirror_rows)"
  rm -rf "${dir}"
  while read -r sha obj; do
    tool_mirror_fetch_upstream "${sha}" "${obj}" "${dir}/${obj}" \
      || die "the tool mirror could not be staged from upstream: ${obj}"
    n=$((n + 1))
  done <<<"${rows}"
  info "staged ${n} release file(s) from upstream into ${TOOL_MIRROR_CONTEXT_DIR}, each matching its pinned sha256"
}

# mirror_image_refs -- every image reference a Dockerfile here pulls through a
# mirror, one per line, sorted: each FROM and each `# syntax=` frontend,
# expanded with the Dockerfile's own ARG defaults, that names a
# *-mirror-ghcr or *-mirror-dockerhub repository.
mirror_image_refs() {
  "${SWARM_PYTHON:-python3}" - "${REPO_ROOT}" "${MIRROR_UPSTREAMS_JSON}" <<'PY'
import json, pathlib, re, sys

root, mirrors = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2])
suffixes = tuple(m["suffix"] for m in mirrors.values())
refs = set()
for df in sorted([*root.glob("apps/*/Dockerfile"), *root.glob("images/*/Dockerfile")]):
    text = df.read_text()
    first = text.splitlines()[0] if text else ""
    m = re.match(r"#\s*syntax\s*=\s*(\S+)", first)
    if m:
        refs.add(m.group(1))
    args, cur = {}, ""
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#"):
            continue
        cur += s.rstrip("\\") + " "
        if s.endswith("\\"):
            continue
        line, cur = cur.strip(), ""
        a = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if a:
            args[a.group(1)] = a.group(2)
            continue
        f = re.match(r"FROM\s+(?:--platform=\S+\s+)?(\S+)", line)
        if f:
            refs.add(re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda k: args.get(k.group(1), k.group(0)), f.group(1)))
for ref in sorted(refs):
    # <region>-docker.pkg.dev/<project>/<repository>/<image...>
    parts = ref.split("/")
    if len(parts) >= 4 and parts[0].endswith("-docker.pkg.dev") and parts[2].endswith(suffixes):
        print(ref)
PY
}
