#!/usr/bin/env bash
# Run the swarm-ui component tests (Vitest + jsdom + Testing Library).
#
# WHY A SCRIPT AND NOT A LINE IN THE MAKEFILE. There are three different
# reasons this can fail to run, and collapsing them into one message is the
# audit-04 shape CLAUDE.md already records: "terraform not found, or no
# tests/terraform" sent a reader to install a binary they already had when the
# suite had actually been renamed out from under the target.
#
#   1. THE SUITE IS MISSING -> hard failure. That is the component tests
#      silently not running, which is exactly the class of hole this whole
#      repository keeps producing. A guard that turns "I could not find the
#      tests" into a green run is a gate that passes hardest when it is most
#      broken.
#   2. node IS NOT INSTALLED -> skip, loudly, with a reason. The same
#      discrimination `tf-test` makes for terraform: a workstation without the
#      toolchain is a workstation, and CI (.github/workflows/application.yml,
#      job `ui`) still runs it on every push. This is the ONLY skip here.
#   3. THE DEPENDENCIES ARE NOT INSTALLED -> install them, once. `npm ci`
#      reaches the registry, exactly as `uv run --project .` reaches PyPI the
#      first time `make test` is run on a fresh clone. The TESTS are offline --
#      no credentials, no emulator, no network, a global `fetch` that throws --
#      which is the property `make test` promises. Resolving a lockfile is not.
#
# The test run itself passes `--run` so a terminal never drops into Vitest's
# watcher: a gate that waits for a keypress is a gate that hangs CI.
#
# NAMED `ui-component-test`, NOT `ui-test`. Another lane is building the
# in-browser visual QA runner and has taken `scripts/ui-test.sh` for it (it was
# observed running as `./scripts/ui-test.sh --only pools-pools --skip-build`).
# Two different scripts under one name is a merge that silently keeps one of
# them, so this one says which kind of test it runs.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Project, region, paths, the deny-list and the redaction filter all live here
# once. Nothing in this file re-derives any of them.
# shellcheck source=scripts/lib/common.sh
. "${SCRIPT_DIR}/lib/common.sh"

UI_DIR="${REPO_ROOT}/apps/swarm-ui"
TEST_DIR="${UI_DIR}/src/__tests__"

if [ ! -d "${UI_DIR}" ]; then
  die "apps/swarm-ui is missing: the component tests cannot run. If the app moved, point this script at it -- do not let it skip."
fi

if [ ! -d "${TEST_DIR}" ]; then
  err "apps/swarm-ui/src/__tests__ is missing: the component tests cannot run."
  die "If the suite moved, point this script at it -- do not let it skip."
fi

# Named explicitly rather than counted with a glob, so a rename of one file
# fails here instead of quietly shrinking the suite.
REQUIRED_SUITES="fetch.test.ts types.test.ts dispatch.test.ts honesty.screen.test.tsx honesty.capacity.test.tsx honesty.counts.test.tsx"
missing=""
for suite in ${REQUIRED_SUITES}; do
  [ -f "${TEST_DIR}/${suite}" ] || missing="${missing} ${suite}"
done
if [ -n "${missing}" ]; then
  err "these component suites are named in ${BASH_SOURCE[0]##*/} and are not on disk:${missing}"
  die "A suite that is renamed and not re-registered stops running and nothing says so."
fi

NODE_BIN="$(prefer_local_bin node "${SWARM_NODE:-}" || true)"
NPM_BIN="$(prefer_local_bin npm "${SWARM_NPM:-}" || true)"
if [ -z "${NODE_BIN}" ] || [ -z "${NPM_BIN}" ]; then
  warn "node/npm not found on this workstation: skipping the swarm-ui component tests."
  warn "They still run in CI (.github/workflows/application.yml, job 'ui'). Install Node 20+ to run them here."
  exit 0
fi

info "swarm-ui component tests ($("${NODE_BIN}" --version))"

if [ ! -x "${UI_DIR}/node_modules/.bin/vitest" ]; then
  info "installing swarm-ui dev dependencies from package-lock.json (one time; reaches the npm registry)"
  ( cd -- "${UI_DIR}" && "${NPM_BIN}" ci --no-audit --no-fund )
fi

# TYPECHECK FIRST, and this is the first thing in this repository that ever
# compiles the TypeScript. `make lint` and `make test` ran no `tsc` at all, so
# a field renamed in types.ts did not fail to build -- it rendered as
# `undefined`, which this client prints as an em dash. `tsconfig.json` includes
# `src`, which now includes `src/__tests__`, so the type-level assertions in
# fetch.test.ts (no Result member carries both a failure and data) are checked
# here rather than being comments that happen to compile.
info "typechecking swarm-ui (tsc -b --noEmit)"
( cd -- "${UI_DIR}" && "${NPM_BIN}" run --silent typecheck )

# NOT `test -- --run`. `npm test` now CHAINS three runners -- vitest over
# src/**/*.test.ts(x), node:test over tests/, and node --import register-ts
# over test/ -- because three separate lanes each found this app had no test
# runner and each fixed it differently, with mutually incompatible APIs.
# A trailing argument is appended to the LAST command in a chain, so `--run`
# reached the third runner, which looked for a FILE called `--run` and failed
# the whole gate after 272 assertions had already passed.
#
# `vitest run` is already non-watching, so the flag was buying nothing even
# when there was only one runner to give it to.
( cd -- "${UI_DIR}" && "${NPM_BIN}" run --silent test )
