#!/usr/bin/env bash
set -euo pipefail
# Runs the named vitest chunk files one after another, single fork.
root=/workspace/att_1a4b2a8f12ff427788ed/work/repo
for c in "$@"; do
  files=()
  while read -r f; do files+=("$f"); done < "$root/scratchpad/mine-names/chunk.$c"
  echo "chunk $c: ${#files[@]} files"
  (cd "$root/apps/swarm-ui" && timeout 570 npx vitest run --pool=forks --poolOptions.forks.singleFork "${files[@]}" 2>&1 | grep -E "Test Files|Tests |FAIL |^ ❯ src" | sort -u | tail -8) || echo "chunk $c: exit $?"
done
