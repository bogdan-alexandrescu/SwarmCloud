#!/bin/sh
# shellcheck disable=SC2329  # each phase function is invoked through phase "$@"
# The recording-runner spike's FIXED WORKLOAD (docs/design/recording-runner.md
# §5.2 C, risk 3; docs/runbooks/recording-runner-spike.md).
#
# scripts/recording-runner-spike.sh pipes this file, unchanged, into
# `sh -s` inside each spike pod: once in the sandboxed pod (GKE Sandbox) and
# once in the control pod (the normal runtime), same image, same 16-core and
# 64 GiB shape. The two wall times are the comparison. Nothing here may depend
# on which pod it runs in, and nothing here may read the network: a workload
# that pulls something measures the network, not the sandbox.
#
# What it does is what a planted-defect test suite does to a kernel, in the
# proportions that make gVisor slow (docs/BUILD_PROMPT_V2.md measured 2.5x on
# file work, 5-8x on metadata-heavy traversal): fork and exec many short
# processes, create, stat and delete tens of thousands of small files, copy a
# tree, and hash data on every core. POSIX sh and busybox/coreutils only, so it
# runs in the daemon image without installing anything.
#
# `set -e` is deliberately ABSENT, as in kubernetes/benchmarks/gvisor-overhead.yaml:
# a phase that fails in one runtime and not the other is itself a finding, so
# every phase runs and reports its exit code. The script's last lines are the
# ones the spike script parses:
#   SPIKE_PHASE name=<phase> seconds=<n> rc=<rc>     (one per phase)
#   SPIKE_WALL_SECONDS=<n>
#   SPIKE_WORKLOAD_RC=<0 when every phase exited 0>
set -u

ROOT="${SPIKE_WORK_DIR:-/work}/spike-workload"
WORKERS=16          # one per requested core: the pod is 16 dedicated cores
DIRS_PER_WORKER=4   # 16 x 4 x 1000 = 64,000 files
FILES_PER_DIR=1000
FORKS=20000
HASH_MIB=1024       # per worker

FAILED=0

phase() {
  name="$1"; shift
  start=$(date +%s)
  "$@" >/dev/null 2>&1
  rc=$?
  end=$(date +%s)
  echo "SPIKE_PHASE name=${name} seconds=$((end - start)) rc=${rc}"
  [ "${rc}" -eq 0 ] || FAILED=1
}

fork_exec() {
  i=0
  while [ "${i}" -lt "${FORKS}" ]; do
    /bin/true || return 1
    i=$((i + 1))
  done
}

write_tree() {
  pids=""
  w=0
  while [ "${w}" -lt "${WORKERS}" ]; do
    (
      d=0
      while [ "${d}" -lt "${DIRS_PER_WORKER}" ]; do
        dir="${ROOT}/tree/w${w}/d${d}"
        mkdir -p "${dir}" || exit 1
        f=0
        while [ "${f}" -lt "${FILES_PER_DIR}" ]; do
          echo "suite ${w} case ${d} file ${f}" >"${dir}/f${f}.txt" || exit 1
          f=$((f + 1))
        done
        d=$((d + 1))
      done
    ) &
    pids="${pids} $!"
    w=$((w + 1))
  done
  status=0
  for p in ${pids}; do wait "${p}" || status=1; done
  return "${status}"
}

walk_tree() {
  n=0
  while [ "${n}" -lt 5 ]; do
    count=$(find "${ROOT}/tree" -type f | wc -l) || return 1
    [ "${count}" -eq $((WORKERS * DIRS_PER_WORKER * FILES_PER_DIR)) ] || return 1
    ls -lR "${ROOT}/tree" >/dev/null || return 1
    n=$((n + 1))
  done
}

copy_tree() {
  mkdir -p "${ROOT}/copy" || return 1
  (cd "${ROOT}/tree" && tar cf - .) | (cd "${ROOT}/copy" && tar xf -)
}

hash_cores() {
  pids=""
  w=0
  while [ "${w}" -lt "${WORKERS}" ]; do
    (head -c $((HASH_MIB * 1024 * 1024)) /dev/zero | sha256sum >/dev/null) &
    pids="${pids} $!"
    w=$((w + 1))
  done
  status=0
  for p in ${pids}; do wait "${p}" || status=1; done
  return "${status}"
}

remove_tree() {
  rm -rf "${ROOT}"
}

rm -rf "${ROOT}"
mkdir -p "${ROOT}" || { echo "SPIKE_WALL_SECONDS=0"; echo "SPIKE_WORKLOAD_RC=1"; exit 1; }

echo "SPIKE_KERNEL $(uname -r)"
wall_start=$(date +%s)
phase fork_exec   fork_exec
phase write_tree  write_tree
phase walk_tree   walk_tree
phase copy_tree   copy_tree
phase hash_cores  hash_cores
phase remove_tree remove_tree
wall_end=$(date +%s)

echo "SPIKE_WALL_SECONDS=$((wall_end - wall_start))"
echo "SPIKE_WORKLOAD_RC=${FAILED}"
exit "${FAILED}"
