#!/usr/bin/env bash
set -euo pipefail
k="$1"; size=18
files=()
while IFS= read -r f; do files+=("$f"); done < <(sed -n "$(( (k-1)*size + 1 )),$(( k*size ))p" scratchpad/github-access-s2b/files.txt)
cd apps/swarm-ui
rc=0
timeout 580 npx vitest run --pool=forks --poolOptions.forks.singleFork "${files[@]}" > "/tmp/chunk-${k}.log" 2>&1 || rc=$?
echo "chunk $k: visited ${#files[@]} files, exit $rc, files passed: $(grep -cE '^ ✓ ' "/tmp/chunk-${k}.log")"
grep -E "^ (FAIL|×)|Test Files|Tests |AssertionError" "/tmp/chunk-${k}.log" | head -20
