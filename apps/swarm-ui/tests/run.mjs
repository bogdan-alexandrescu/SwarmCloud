#!/usr/bin/env node
/**
 * The swarm-ui test runner.
 *
 * WHY THIS EXISTS RATHER THAN vitest + jsdom + @testing-library. `make test`
 * in this repository is offline by contract -- no credentials, no emulator,
 * nothing fetched -- and that has to include the test runner itself. jsdom
 * cannot be installed from a cold npm cache (verified: `npm install jsdom
 * --offline` fails ENOTCACHED on a transitive tarball), so a suite that needs
 * it is a suite that only runs where someone has already been online. The
 * repository has been burned by exactly this shape twice: a terraform suite
 * and an integration suite that existed, were wired into nothing, and rotted.
 *
 * So the suite uses only what `package.json` already declares -- react and
 * react-dom -- plus node's own `node:test`, and esbuild, which arrives with
 * vite. Nothing here adds a dependency.
 *
 * WHAT THAT COSTS, STATED PLAINLY. There is no DOM, so no test here dispatches
 * a real browser event. The components are written so that is not a gap worth
 * pretending about: `HelpCard.tsx` keeps its whole behaviour in a pure
 * transition and its whole markup in a component that takes a state, so every
 * state a browser could reach is one this suite can render and assert. What is
 * NOT covered is the wiring between a real focus event and React's onFocus --
 * that is React's, not ours.
 *
 * Usage:  node tests/run.mjs          (from apps/swarm-ui)
 */

import { build } from 'esbuild'
import { spawnSync } from 'node:child_process'
import { mkdirSync, readdirSync, rmSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const out = join(root, '.test-build')

const entries = readdirSync(here)
  .filter((f) => /\.test\.(ts|tsx)$/.test(f))
  .map((f) => join(here, f))

// A suite that finds no tests is a suite that passes hardest when it is most
// broken -- the class of failure this repository keeps producing. Fail loudly.
if (entries.length === 0) {
  console.error('tests/run.mjs: no *.test.ts(x) files found in tests/. Nothing ran.')
  process.exit(1)
}

rmSync(out, { recursive: true, force: true })
mkdirSync(out, { recursive: true })

await build({
  entryPoints: entries,
  outdir: out,
  bundle: true,
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  target: 'node20',
  sourcemap: 'inline',
  logLevel: 'warning',
  // EVERY node_modules PACKAGE STAYS EXTERNAL. react-dom/server is CommonJS
  // and `require`s node builtins lazily; bundled into an ESM file those become
  // esbuild's "Dynamic require of \"stream\" is not supported" at import time.
  // Left external, node loads them itself and the interop is node's problem,
  // which it has already solved. Only our own src/ is bundled.
  packages: 'external',
  define: {
    // React reads this at import time and throws on undefined in some builds.
    'process.env.NODE_ENV': '"test"',
    // `api.ts` computes USE_FIXTURES from `import.meta.env`, which is vite's
    // and does not exist under node. DEV false, so importing a screen starts
    // no fixture timers: these tests hand their components a value directly
    // and must never depend on a fixture the app also ships.
    'import.meta.env': '{"DEV":false,"VITE_LIVE":""}',
  },
  outExtension: { '.js': '.mjs' },
})

// THE BUILT FILES, NAMED ONE BY ONE -- never the directory. `node --test <dir>`
// ran every test file inside it on Node 20. From Node 21 a positional argument
// is a glob PATTERN, and a directory matches only itself, so Node 24 tried to
// load `.test-build` as a module and failed with MODULE_NOT_FOUND (CI run
// 36045421908, the first run of this suite on Node 24). Listing the files is
// what both versions agree on.
const built = readdirSync(out)
  .filter((f) => f.endsWith('.test.mjs'))
  .sort()
  .map((f) => join(out, f))

if (built.length !== entries.length) {
  console.error(
    `tests/run.mjs: built ${built.length} test file(s) from ${entries.length} entr${
      entries.length === 1 ? 'y' : 'ies'
    }. Refusing to run a partial suite.`,
  )
  process.exit(1)
}

const res = spawnSync(process.execPath, ['--test', ...built], { stdio: 'inherit' })
process.exit(res.status === null ? 1 : res.status)
