// NO FILE UNDER src/ NAMES THE LIVE REPOSITORY (DAYONE-DESAGA-4, lesson 5).
//
// WHY. The console is installed by companies that are not the one it was built
// in. The fixture data in `api.ts` served the live deployment's own GitHub
// repository as the repository a task cloned, the one a pull request was
// opened on and the one an agent's git summary reported; and two comments
// (`IssueSubmit.tsx`, `styles/intake.css`) quoted it as their example. A
// fixture is what a new company's operator sees on a preview build and what a
// contributor copies into the next one, so a default that names another org's
// repository travels. Lesson 5 of docs/design/day-one/lessons.md: no
// live-project default anywhere an install reads. Examples use `example-org`.
//
// THE SCAN reads every file under src/ except the tests (`__tests__/` and any
// `*.test.*` file): a test may quote a reference the API really served, and
// that names no default. Comments are read too, because a comment is where
// the next fixture is copied from.
//
// THE NEEDLE is built from its parts so this file does not carry it whole.
//
// `join(__dirname, '..')`, the way filenames.case.test.ts does it.

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

const LIVE_OWNER = ['bogdan', 'alexandrescu'].join('-')

const SRC = join(__dirname, '..')

function isTest(path: string): boolean {
  return path.split('/').includes('__tests__') || /\.test\.[^/]+$/.test(path)
}

function walk(dir: string, out: string[]): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, out)
    else out.push(relative(SRC, full).split(sep).join('/'))
  }
  return out
}

/** Every `path:line` under src/ (tests excluded) whose text names `needle`. */
function namings(needle: string): string[] {
  const hits: string[] = []
  for (const path of walk(SRC, []).filter((p) => !isTest(p)).sort()) {
    readFileSync(join(SRC, path), 'utf8').split('\n').forEach((line, i) => {
      if (line.includes(needle)) hits.push(`${path}:${i + 1}`)
    })
  }
  return hits
}

describe('no file under src/ names the live repository', () => {
  it('reads the source files it guards, and not the tests', () => {
    const files = walk(SRC, []).filter((p) => !isTest(p))
    // An empty scan passes every needle: prove it read the files that held one.
    expect(files).toContain('api.ts')
    expect(files).toContain('IssueSubmit.tsx')
    expect(files).toContain('styles/intake.css')
    expect(files.some((p) => p.startsWith('__tests__/'))).toBe(false)
    expect(isTest('__tests__/qa.u12.runs.test.tsx')).toBe(true)
    expect(isTest('components/Thing.test.tsx')).toBe(true)
    expect(isTest('api.ts')).toBe(false)
  })

  it('finds a needle it is given, so a clean result means the text is absent', () => {
    // `loadCapacity` is exported by api.ts: the scan sees file text.
    expect(namings('export async function loadCapacity').some((h) => h.startsWith('api.ts:'))).toBe(true)
  })

  it("names no live org's repository in a fixture, a default or a comment", () => {
    expect(namings(LIVE_OWNER)).toEqual([])
  })
})
