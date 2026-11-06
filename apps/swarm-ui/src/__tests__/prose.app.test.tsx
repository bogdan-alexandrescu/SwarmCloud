// THE PROSE RATCHET'S OWN RULES: what it counts, what it compares with, and
// that it is merge-safe (BGD's pick, 2026-11-06, part of #2688).
//
// The slices (prose.app.<group>.test.tsx) render every page twice, on the
// change's base and on its head, and fail a page whose visible words or arrow
// links went up (proseapp.tsx; the base: prosebase.ts). There is no table of
// ceilings to edit, so two changes that each pass against their own base pass
// together: each added nothing, so together they add nothing. The fixtures
// below are pages as HTML, the way a change would leave them.

import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { afterEach, describe, expect, it } from 'vitest'

import { measure, newWords, PAGES, pieces, proseDelta, type Measure } from './proseapp'
import { chooseBase, extractApp, firstParent } from './prosebase'

const HERE = dirname(fileURLToPath(import.meta.url))

function page(html: string): Measure {
  const root = document.createElement('div')
  root.innerHTML = html
  return measure(root)
}

/** Each page's failures when `head` is compared with `base`, page by page; an empty list is a pass. */
function check(base: Record<string, string>, head: Record<string, string>): string[] {
  return Object.entries(head).flatMap(([key, html]) =>
    proseDelta(key, base[key] === undefined ? null : page(base[key]), page(html)),
  )
}

const BASE = {
  'capacity/pools': '<h1>Pools</h1> <p>two pools are full</p> <a href="#a">Open</a>',
  'admin/setup': '<h1>Setup</h1> <p>one step left before ready</p>',
}

describe('the prose ratchet compares a change with its base', () => {
  it('fails a change that adds words to one page, naming the page and the words', () => {
    const head = { ...BASE, 'capacity/pools': '<h1>Pools</h1> <p>two pools are full</p> <p>Raise them in Config</p> <a href="#a">Open</a>' }
    const bad = check(BASE, head)
    expect(bad).toHaveLength(1)
    expect(bad[0]).toContain('capacity/pools')
    expect(bad[0]).toContain('Raise them in Config')
    expect(bad[0]).not.toContain('admin/setup')
  })

  it('passes two changes that each remove words, alone and together', () => {
    const a = { ...BASE, 'capacity/pools': '<h1>Pools</h1> <p>two full</p> <a href="#a">Open</a>' }
    const b = { ...BASE, 'admin/setup': '<h1>Setup</h1> <p>one step left</p>' }
    const both = { 'capacity/pools': a['capacity/pools'], 'admin/setup': b['admin/setup'] }
    expect(check(BASE, a)).toEqual([])
    expect(check(BASE, b)).toEqual([])
    // A train runs the second against the first, and main runs both against the commit before.
    expect(check(a, both)).toEqual([])
    expect(check(BASE, both)).toEqual([])
  })

  it('passes a change that lowers a page while another adds nothing to it, which a hand-edited ceiling failed', () => {
    const lowered = { ...BASE, 'admin/setup': '<h1>Setup</h1> <p>one step</p>' }
    const elsewhere = { ...BASE, 'capacity/pools': '<h1>Pools</h1> <p>two full</p> <a href="#a">Open</a>' }
    const both = { 'capacity/pools': elsewhere['capacity/pools'], 'admin/setup': lowered['admin/setup'] }
    expect(check(BASE, lowered)).toEqual([])
    expect(check(BASE, elsewhere)).toEqual([])
    expect(check(lowered, both)).toEqual([])
  })

  it('passes a change that moves words behind a tooltip', () => {
    const head = {
      ...BASE,
      'admin/setup':
        '<h1>Setup</h1> <button aria-label="About setup">i</button>' +
        '<span data-help-description>one step left before ready</span>',
    }
    expect(check(BASE, head)).toEqual([])
  })

  it('fails a change that draws a new text link with an arrow, naming it', () => {
    const head = { ...BASE, 'admin/setup': '<h1>Setup</h1> <p>one step left</p> <a href="#s">Next →</a>' }
    const bad = check(BASE, head)
    expect(bad).toHaveLength(1)
    expect(bad[0]).toContain('admin/setup')
    expect(bad[0]).toContain('"Next →"')
  })

  it('passes a page the base did not have: there is nothing to compare it with', () => {
    expect(check(BASE, { ...BASE, 'admin/new-page': '<h1>New</h1> <p>a page of its own</p>' })).toEqual([])
  })

  it('keeps no table of ceilings for lanes to edit', () => {
    expect(existsSync(join(HERE, 'prose.ceilings.json'))).toBe(false)
    expect(existsSync(join(HERE, 'prose.words.json'))).toBe(false)
  })

  it('measures each page once: no two pages share a key', () => {
    const keys = PAGES.map((p) => p.key)
    expect(new Set(keys).size).toBe(keys.length)
  })
})

// A throwaway repository per test, under the system's temporary directory.
let repos: string[] = []
afterEach(() => {
  for (const r of repos) rmSync(r, { recursive: true, force: true })
  repos = []
})

function sh(cwd: string, ...args: string[]): string {
  return execFileSync('git', ['-c', 'user.name=prose', '-c', 'user.email=prose@example.invalid', ...args], {
    cwd,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim()
}

function repo(): string {
  const r = mkdtempSync(join(tmpdir(), 'prose-repo-'))
  repos.push(r)
  sh(r, 'init', '-q', '-b', 'main')
  return r
}

function commit(r: string, files: Record<string, string>, message: string): string {
  for (const [path, body] of Object.entries(files)) {
    mkdirSync(dirname(join(r, path)), { recursive: true })
    writeFileSync(join(r, path), body)
  }
  sh(r, 'add', '-A')
  sh(r, 'commit', '-q', '-m', message)
  return sh(r, 'rev-parse', 'HEAD')
}

describe('the base a change is compared with', () => {
  it("is HEAD's first parent on main, in a train and in a pull request's merge commit", () => {
    const r = repo()
    const one = commit(r, { 'app/src/a.ts': 'one' }, 'one')
    const two = commit(r, { 'app/src/a.ts': 'two' }, 'two')
    sh(r, 'update-ref', 'refs/remotes/origin/main', two)
    expect(firstParent(r, two)).toBe(one)
    expect(chooseBase(join(r, 'app'), {}).sha).toBe(one)
  })

  it('is the merge base with origin/main on a branch, however many commits it has', () => {
    const r = repo()
    const one = commit(r, { 'app/src/a.ts': 'one' }, 'one')
    sh(r, 'update-ref', 'refs/remotes/origin/main', one)
    commit(r, { 'app/src/a.ts': 'two' }, 'two')
    commit(r, { 'app/src/a.ts': 'three' }, 'three')
    expect(chooseBase(join(r, 'app'), {})).toEqual({ sha: one, why: 'the merge base with origin/main' })
  })

  it("is HEAD under an uncommitted edit to main's tip, and PROSE_BASE when it is set", () => {
    const r = repo()
    const one = commit(r, { 'app/src/a.ts': 'one' }, 'one')
    const two = commit(r, { 'app/src/a.ts': 'two' }, 'two')
    sh(r, 'update-ref', 'refs/remotes/origin/main', two)
    writeFileSync(join(r, 'app/src/a.ts'), 'edited')
    expect(chooseBase(join(r, 'app'), {}).sha).toBe(two)
    expect(chooseBase(join(r, 'app'), { PROSE_BASE: one }).sha).toBe(one)
    expect(() => chooseBase(join(r, 'app'), { PROSE_BASE: 'no-such-ref' })).toThrow(/names no commit/)
  })

  it('copies the base app without its tests, beside the head node_modules', () => {
    const r = repo()
    const one = commit(
      r,
      { 'app/src/a.ts': 'one', 'app/src/__tests__/t.test.ts': 'test', 'app/package.json': '{}', 'app/tsconfig.json': '{}' },
      'one',
    )
    commit(r, { 'app/src/a.ts': 'two' }, 'two')
    mkdirSync(join(r, 'app/node_modules'))
    const tree = extractApp(join(r, 'app'), one)
    try {
      expect(readFileSync(join(tree.app, 'src/a.ts'), 'utf8')).toBe('one')
      expect(existsSync(join(tree.app, 'src/__tests__'))).toBe(false)
      expect(existsSync(join(tree.app, 'node_modules'))).toBe(true)
    } finally {
      tree.remove()
    }
    expect(existsSync(tree.app)).toBe(false)
  })
})

describe('what the prose meter counts', () => {
  it('counts visible words, not the help copy, the stylesheet or the script', () => {
    // Sibling elements' text runs together ("Poolstwo"), as prose.operator
    // explains: data costs little, a sentence a token per word. Spaced here.
    const root = document.createElement('div')
    root.innerHTML =
      '<h1>Pools</h1> <p>two words</p>' +
      '<span data-help-description>an explanation nobody sees until they ask</span>' +
      '<style>.a { color: red }</style><script>var x = 1</script>'
    expect(measure(root).words).toBe(3)
  })

  it('counts a text link with an arrow as an action that should be a button, and a button not', () => {
    const root = document.createElement('div')
    root.innerHTML =
      '<a href="#a">All workflows →</a><a href="#b">Open › setup</a><a href="#c">plain</a>' +
      '<span role="link">Next →</span><button>Retry →</button>'
    const m = measure(root)
    expect(m.arrows).toBe(3)
    expect(m.arrowLinks).toEqual(['All workflows →', 'Open › setup', 'Next →'])
  })

  it('names the words a page added since its base, once per extra occurrence', () => {
    expect(newWords(['a', 'b', 'b', 'c', 'd'], ['b', 'a', 'c'])).toEqual(['b', 'd'])
    expect(newWords(['a'], ['a', 'a'])).toEqual([])
  })

  it('cuts a run of two elements at the capital, and keeps a word whole', () => {
    expect(pieces(['usageAgainst', 'GitHub', 'ok', 'Profile?'])).toEqual(['usage', 'Against', 'Git', 'Hub', 'ok', 'Profile?'])
  })
})
