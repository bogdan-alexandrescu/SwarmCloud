// NO TWO FILES UNDER src/ MAY SHARE A PATH IGNORING CASE AND EXTENSION.
//
// WHY. PR 2357 added the Account health page as `AccountHealth.tsx` while main
// already carried the shared incident rule as `accountHealth.ts` (PR 2359). The
// two names differ only in case and extension, and on a case-insensitive file
// system -- the owner's Mac, and the CI runner that built PR 2357 -- an import
// of `./accountHealth` resolved to the page instead of the rule. The build
// failed with 33 `No matching export in "src/AccountHealth.tsx"` errors
// (run 40266498186). Two such files cannot even coexist in a macOS checkout.
// Every unit test passed on Linux, where the names are distinct; only a scan of
// the names says the pair exists. The page is `AccountHealthPage.tsx` now.
//
// THE KEY drops the extension (`.ts`, `.tsx`, `.css`, ...) because an import
// specifier names a module without one: `./accountHealth` is ambiguous between
// `accountHealth.ts` and `AccountHealth.tsx` exactly as it would be between two
// `.ts` files. `foo.test.ts` keys as `foo.test`, so a test never collides with
// the module it tests.
//
// `join(__dirname, '..')`, the way keyboard.test.tsx and nav.links.test.tsx do
// it: `new URL('..', import.meta.url)` resolves to `/src` under vitest.

import { readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

/** The path a collision is judged by: lower-cased, last extension dropped. */
function caseKey(path: string): string {
  const slash = path.lastIndexOf('/')
  const dot = path.lastIndexOf('.')
  const stem = dot > slash + 1 ? path.slice(0, dot) : path
  return stem.toLowerCase()
}

/** Every group of two or more paths that share a `caseKey`, sorted. */
function caseCollisions(paths: readonly string[]): string[][] {
  const groups = new Map<string, string[]>()
  for (const p of paths) {
    const k = caseKey(p)
    groups.set(k, [...(groups.get(k) ?? []), p])
  }
  return [...groups.values()].filter((g) => g.length > 1).map((g) => [...g].sort())
}

function walk(dir: string, root: string, out: string[]): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, root, out)
    else out.push(relative(root, full).split(sep).join('/'))
  }
  return out
}

describe('file names under src/', () => {
  it('catches the pair that broke PR 2357: AccountHealth.tsx beside accountHealth.ts', () => {
    expect(caseCollisions(['AccountHealth.tsx', 'accountHealth.ts', 'accountHealthModel.ts', 'App.tsx'])).toEqual([
      ['AccountHealth.tsx', 'accountHealth.ts'],
    ])
  })

  it('judges whole paths, so the same name in two directories is not a collision', () => {
    expect(caseCollisions(['styles/account-health.css', 'account-health.ts'])).toEqual([])
    expect(caseCollisions(['__tests__/Foo.test.ts', '__tests__/foo.test.tsx'])).toEqual([
      ['__tests__/Foo.test.ts', '__tests__/foo.test.tsx'],
    ])
    expect(caseCollisions(['foo.ts', 'foo.test.ts', 'AccountHealthPage.tsx', 'accountHealth.ts'])).toEqual([])
  })

  it('has no two files whose paths are equal ignoring case and extension', () => {
    const root = join(__dirname, '..')
    const paths = walk(root, root, [])
    // The walk ran over the tree, not an empty or wrong directory.
    expect(paths).toContain('App.tsx')
    expect(paths.length).toBeGreaterThan(100)
    expect(caseCollisions(paths)).toEqual([])
  })
})
