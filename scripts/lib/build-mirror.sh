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
#           digest it pinned before. A remote repository fetches a digest
#           from upstream once and serves it from the cache after that.
#
#           THE DOCKERFILES NAME NO PROJECT (DAYONE-DESAGA-1;
#           docs/design/day-one-install.md section 7). Their MIRROR_*
#           defaults are empty and their `# syntax=` line is the plain
#           docker/dockerfile reference -- BuildKit reads that line before
#           any build argument, so it cannot name one. The build passes the
#           mirror of the project it builds for (mirror_build_args): each
#           MIRROR_* the Dockerfile declares, and the frontend through the
#           Docker Hub mirror as BUILDKIT_SYNTAX, which BuildKit uses in place
#           of the `# syntax=` line. So the live project's build pulls exactly
#           what it pulled when the Dockerfiles named its mirror, and another
#           project's build reads its own.
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
# The mirror build arguments, in the order they are passed.
MIRROR_BUILD_ARGS=(MIRROR_DOCKER_HUB MIRROR_GHCR)

# mirror_repository NAME -- the repository NAME (MIRROR_DOCKER_HUB or
# MIRROR_GHCR) stands for in the project this build is for:
# <REGION>-docker.pkg.dev/<PROJECT_ID>/<repository>. Dies when PROJECT_ID is
# unset: there is no project whose mirror a build could fall back to.
mirror_repository() {
  local repository
  # Under terraform/infra's default name_prefix, `swarm`: build_mirror.tf
  # names them "${var.name_prefix}-mirror-...".
  case "$1" in
    MIRROR_DOCKER_HUB) repository="swarm-mirror-dockerhub" ;;
    MIRROR_GHCR) repository="swarm-mirror-ghcr" ;;
    *) die "no build mirror is called $1" ;;
  esac
  [[ -n "${PROJECT_ID:-}" ]] || die "PROJECT_ID is not set: a build reads the mirrors of its own project"
  [[ -n "${REGION:-}" ]] || die "REGION is not set: a build reads the mirrors in its own region"
  printf '%s-docker.pkg.dev/%s/%s\n' "${REGION}" "${PROJECT_ID}" "${repository}"
}

# dockerfile_syntax_ref DOCKERFILE -- the frontend its `# syntax=` first line
# names, as written, or nothing. A path relative to the repository is taken
# from REPO_ROOT.
dockerfile_syntax_ref() {
  local df="$1" first=""
  [[ "${df}" == /* ]] || df="${REPO_ROOT}/${df}"
  [[ -f "${df}" ]] || die "no Dockerfile at ${df#"${REPO_ROOT}/"}"
  IFS= read -r first <"${df}" || true
  if [[ "${first}" =~ ^#[[:space:]]*syntax[[:space:]]*=[[:space:]]*([^[:space:]]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

# mirror_syntax_ref DOCKERFILE -- the Dockerfile's `# syntax=` frontend
# through this project's Docker Hub mirror, or nothing when its first line
# names no frontend or names one outside Docker Hub.
mirror_syntax_ref() {
  local ref hub
  ref="$(dockerfile_syntax_ref "$1")"
  [[ -n "${ref}" ]] || return 0
  # A registry host has a dot or a port in its first component; a Docker Hub
  # short name (docker/dockerfile) has neither.
  [[ "${ref%%/*}" != *.* && "${ref%%/*}" != *:* && "${ref%%/*}" != localhost ]] || return 0
  [[ "${ref}" == */* ]] || ref="library/${ref}"
  hub="$(mirror_repository MIRROR_DOCKER_HUB)"
  printf '%s/%s\n' "${hub}" "${ref}"
}

# mirror_build_args DOCKERFILE -- `NAME=VALUE`, one a line, for every mirror
# build argument the Dockerfile declares, then BUILDKIT_SYNTAX for its
# frontend (mirror_syntax_ref). An argument the Dockerfile does not declare is
# not passed: BuildKit warns about every one it was given and did not use.
mirror_build_args() {
  local df="$1" name repo syntax
  [[ "${df}" == /* ]] || df="${REPO_ROOT}/${df}"
  [[ -f "${df}" ]] || die "no Dockerfile at ${df#"${REPO_ROOT}/"}"
  for name in "${MIRROR_BUILD_ARGS[@]}"; do
    if grep -Eq "^[[:space:]]*ARG[[:space:]]+${name}(=|[[:space:]]*$)" "${df}"; then
      repo="$(mirror_repository "${name}")"
      printf '%s=%s\n' "${name}" "${repo}"
    fi
  done
  syntax="$(mirror_syntax_ref "${df}")"
  if [[ -n "${syntax}" ]]; then printf 'BUILDKIT_SYNTAX=%s\n' "${syntax}"; fi
}

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

# mirror_build_args_yaml DOCKERFILE -- mirror_build_args as YAML list items
# for a docker step's args (scripts/build-images.sh generate_config), so the
# live project's build pulls what it pulled when the Dockerfiles named its
# mirror and another project's build reads its own (DAYONE-DESAGA-1; design
# section 7). Nothing for a pull request's local build (LOCAL_BUILD=1): it
# can read no mirror, and its replay passes the upstream registries
# (build-images.sh LOCAL_REPLAY upstream_args), so it needs no project at
# all. Call it before the heredoc it fills, so an unset PROJECT_ID stops the
# run instead of writing an empty argument.
mirror_build_args_yaml() {
  local args line
  [[ "${LOCAL_BUILD:-0}" -eq 0 ]] || return 0
  args="$(mirror_build_args "$1")"
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    printf '%s\n' "      - --build-arg" "      - ${line}"
  done <<<"${args}"
}

# mirror_image_refs -- every image reference a Dockerfile here pulls through a
# mirror, one per line, sorted: each FROM and each `# syntax=` frontend, as
# the build passes them (mirror_build_args: the MIRROR_* arguments name this
# project's mirrors) and otherwise with the Dockerfile's own ARG defaults,
# that names a *-mirror-ghcr or *-mirror-dockerhub repository.
mirror_image_refs() {
  local passed="" name
  for name in "${MIRROR_BUILD_ARGS[@]}"; do
    passed+="${name}=$(mirror_repository "${name}")"$'\n'
  done
  "${SWARM_PYTHON:-python3}" - "${REPO_ROOT}" "${MIRROR_UPSTREAMS_JSON}" "${passed}" <<'PY'
import json, pathlib, re, sys

root, mirrors = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2])
passed = dict(line.split("=", 1) for line in sys.argv[3].splitlines() if line)
suffixes = tuple(m["suffix"] for m in mirrors.values())
refs = set()
for df in sorted([*root.glob("apps/*/Dockerfile"), *root.glob("images/*/Dockerfile")]):
    text = df.read_text()
    first = text.splitlines()[0] if text else ""
    m = re.match(r"#\s*syntax\s*=\s*(\S+)", first)
    if m:
        ref = m.group(1)
        # A Docker Hub short name is passed through the mirror as
        # BUILDKIT_SYNTAX (mirror_syntax_ref).
        head = ref.split("/", 1)[0]
        if "." not in head and ":" not in head and head != "localhost":
            ref = f"{passed['MIRROR_DOCKER_HUB']}/{ref if '/' in ref else 'library/' + ref}"
        refs.add(ref)
    args, cur = dict(passed), ""
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
            # What the build passes wins over a default, as it does in BuildKit.
            if a.group(1) not in passed:
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
