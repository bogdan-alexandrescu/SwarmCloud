#!/usr/bin/env bash
set -euo pipefail
n="$1"
line="$(sed -n "${n}p" scratchpad/github-access-s2b/groups.txt)"
read -r -a files <<< "$line"
cd apps/swarm-ui
timeout 580 npx vitest run --pool=forks --poolOptions.forks.singleFork "${files[@]}" > "/tmp/group-${n}.log" 2>&1 || echo "exit $?"
echo "visited ${#files[@]} files"
grep -E "^ (FAIL|×)|Test Files|Tests " "/tmp/group-${n}.log" | head -30
