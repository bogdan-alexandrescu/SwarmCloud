// The lint gate's baseline, exercised rather than read (part of #77).
//
// `npm run lint` is green only because eslint-suppressions.json lists the
// violations main already had. The gate is worth something only if a NEW
// violation still fails it, so each test below writes one throwaway file with
// a known violation, runs the real ESLint CLI over it with the real config, and
// reads the exit code. The mutation each test catches:
//
//   - outside the baseline: a config that set the rules to `warn`, or a lint
//     script that suppressed everything, would exit 0 here.
//   - inside the baseline: proves it is the suppressions file that makes the
//     difference, not a rule that never fires.
//   - one over the count: a baseline that matched by file alone would let a
//     baselined file collect any number of new violations.

import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_DIR = fileURLToPath(new URL('..', import.meta.url))
const ESLINT_BIN = join(PACKAGE_DIR, 'node_modules', 'eslint', 'bin', 'eslint.js')
const BASELINE = join(PACKAGE_DIR, 'eslint-suppressions.json')
// One rule and one rule only reports a `debugger` statement, so the counts
// below are exact.
const RULE = 'no-debugger'

// One file per test, named at runtime so parallel runs never share it. test/
// sits outside tsconfig.json, so the config types it through the default
// project: no whole-program build for a three-line file.
function withProbe(violations, body) {
  const name = `test/lint-probe-${randomBytes(6).toString('hex')}.tsx`
  const lines = []
  for (let i = 0; i < violations; i += 1) lines.push('  debugger')
  writeFileSync(
    join(PACKAGE_DIR, name),
    `export function probe(): number {\n${lines.join('\n')}\n  return 1\n}\n`,
  )
  const dir = mkdtempSync(join(tmpdir(), 'swarm-ui-lint-'))
  try {
    return body(name, dir)
  } finally {
    rmSync(join(PACKAGE_DIR, name), { force: true })
    rmSync(dir, { recursive: true, force: true })
  }
}

function lint(file, suppressions) {
  const args = [ESLINT_BIN, '--max-warnings', '0', '--no-warn-ignored', file]
  if (suppressions) args.splice(1, 0, '--suppressions-location', suppressions)
  return spawnSync(process.execPath, args, { cwd: PACKAGE_DIR, encoding: 'utf8' })
}

// The committed baseline plus `extra`, written where the test can throw it away.
function baselineWith(dir, extra) {
  const merged = { ...JSON.parse(readFileSync(BASELINE, 'utf8')), ...extra }
  const path = join(dir, 'suppressions.json')
  writeFileSync(path, JSON.stringify(merged))
  return path
}

test('a violation outside the baseline fails the lint gate', () => {
  withProbe(1, (name) => {
    const run = lint(name)
    assert.equal(run.status, 1, `expected exit 1, got ${run.status}\n${run.stdout}${run.stderr}`)
    assert.match(run.stdout, new RegExp(RULE))
    assert.match(run.stdout, /\berror\b/)
  })
})

test('the same violation passes once the baseline lists it', () => {
  withProbe(1, (name, dir) => {
    const run = lint(name, baselineWith(dir, { [name]: { [RULE]: { count: 1 } } }))
    assert.equal(run.status, 0, `expected exit 0, got ${run.status}\n${run.stdout}${run.stderr}`)
  })
})

test('one violation more than the baseline counts fails', () => {
  withProbe(2, (name, dir) => {
    const run = lint(name, baselineWith(dir, { [name]: { [RULE]: { count: 1 } } }))
    assert.equal(run.status, 1, `expected exit 1, got ${run.status}\n${run.stdout}${run.stderr}`)
    assert.match(run.stdout, new RegExp(RULE))
  })
})

// The tests above run the CLI directly; this holds `npm run lint` to the same
// gate. `--suppress-all` or `--suppress-rule` would write every new violation
// into the baseline and pass, and `--pass-on-unpruned-suppressions` would let
// the baseline stop shrinking.
test('npm run lint lints the whole package against the committed baseline', () => {
  const { scripts } = JSON.parse(readFileSync(join(PACKAGE_DIR, 'package.json'), 'utf8'))
  const words = scripts.lint.split(/\s+/)
  assert.ok(words.includes('eslint'), scripts.lint)
  assert.equal(words.at(-1), '.', scripts.lint)
  assert.equal(words[words.indexOf('--max-warnings') + 1], '0', scripts.lint)
  for (const flag of ['--suppress-all', '--suppress-rule', '--pass-on-unpruned-suppressions', '--suppressions-location']) {
    assert.ok(!words.includes(flag), `${flag} in: ${scripts.lint}`)
  }
})
