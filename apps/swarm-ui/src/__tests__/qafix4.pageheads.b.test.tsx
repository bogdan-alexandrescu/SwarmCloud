// VISUAL QA RUN 4 LANE QAFIX4-PAGEHEADS-B (part of #1680): ONE GAP UNDER
// EVERY ADMIN PAGE'S HEAD.
//
//   N3 (run-4 admin-012)  The admin pages started their first line at three
//        distances under the page title. People, Runner sizes and Workspaces
//        you admin (and every other `.ur-page`) sat ~12 px lower than Pool
//        limits' count: `.ur-page` is a flex column with a `--ctl-s3` gap, and
//        `.c-phead` keeps its own `--ctl-s3` bottom margin, and flex items'
//        margins do not collapse, so the head was followed by both (24 px).
//        A `Screen` route (Pool limits, Tenants) and Platform counts sit in
//        the block `main.work`, where the head's margin is the whole gap
//        (12 px). The head's margin is now the one gap everywhere: a
//        `.ur-page` drops it and keeps the page's gap, which is the same
//        `--ctl-s3`.
//
// jsdom lays nothing out, so each case reads what decides the distance: the
// head's bottom margin, the gap its parent puts between flex or grid items,
// and the next element's top margin, as the real sheets resolve them at the
// width the finding was measured (`painted`, every sheet in the bundle's
// order, with the phone rules applied at 390).
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', window.location.pathname)
})

/** The px a length resolves to once its tokens are read; anything else is a loud failure. */
function px(v: string | null): number {
  if (v === null || v === '0') return 0
  const r = resolveVars(v, TABLES.dark).trim()
  if (r === '0') return 0
  const m = /^(-?\d+(?:\.\d+)?)px$/.exec(r)
  if (m === null) throw new Error(`not a px length: ${JSON.stringify(v)} -> ${JSON.stringify(r)}`)
  return Number(m[1])
}

/** One side of a 1-4 value box shorthand. */
function sideOf(value: string, which: 'top' | 'bottom'): string {
  const parts = value.trim().split(/\s+/)
  if (which === 'top') return parts[0]!
  return parts.length >= 3 ? parts[2]! : parts[0]!
}

/** The margin the cascade gives an element's top or bottom, from its longhand or its shorthand. */
function margin(el: Element, which: 'top' | 'bottom', env: CascadeEnv): number {
  const v = painted(el, [`margin-${which}`, 'margin'], env)
  if (v === null) return 0
  return px(v === painted(el, 'margin', env) ? sideOf(v, which) : v)
}

/** The gap a flex or grid container puts between its rows; 0 for a block. */
function rowGap(el: Element, env: CascadeEnv): number {
  const display = painted(el, 'display', env) ?? 'block'
  if (!/flex|grid/.test(display)) return 0
  const v = painted(el, ['row-gap', 'gap'], env)
  if (v === null) return 0
  return px(v === painted(el, 'gap', env) ? v.trim().split(/\s+/)[0]! : v)
}

/**
 * THE SPACE BETWEEN THE PAGE HEAD AND THE FIRST LINE UNDER IT. In a flex or
 * grid parent the head's bottom margin, the parent's row gap and the next
 * item's top margin add up; in a block parent the two margins collapse to the
 * larger. A bare wrapper (a `div` with no class, as `Screen` puts round its
 * count and its children) is looked through to the line it starts with.
 */
function headGap(head: Element, env: CascadeEnv): number {
  const parent = head.parentElement!
  let next = head.nextElementSibling
  expect(next, 'something follows the page head').not.toBeNull()
  const flexy = rowGap(parent, env) > 0 || /flex|grid/.test(painted(parent, 'display', env) ?? 'block')
  if (flexy) return margin(head, 'bottom', env) + rowGap(parent, env) + margin(next!, 'top', env)
  let top = margin(next!, 'top', env)
  while (next!.className === '' && next!.firstElementChild !== null) {
    next = next!.firstElementChild
    top = Math.max(top, margin(next!, 'top', env))
  }
  return Math.max(margin(head, 'bottom', env), top)
}

/** Mount a page where the app mounts it: inside `main.work`. */
function mount(page: ReactElement) {
  const main = document.createElement('main')
  main.className = 'work'
  document.body.appendChild(main)
  return render(page, { container: main })
}

/** Every read answers 404: the heads and the intros draw before any read lands. */
async function people() {
  serve(() => null)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { PeopleScreen } = await import('../People')
  mount(<PeopleScreen />)
  return document.querySelector('.ur-page > .c-phead')!
}

/** A `Screen` with a summary, as Pool limits and Tenants are: the count is the first line. */
async function screenWithSummary() {
  serve(() => null)
  vi.resetModules()
  const { Screen } = await import('../Shell')
  mount(
    <Screen<{ n: number }>
      title="Pool limits"
      load={async () => ({ status: 'ok', data: { n: 3 }, fetchedAt: Date.now() })}
      summary={(d) => `${d.n} pools`}
    >
      {() => <section className="ctl-card">rows</section>}
    </Screen>,
  )
  const note = await vi.waitFor(() => {
    const n = document.querySelector('.c-count-note')
    if (n === null) throw new Error('the count has not drawn yet')
    return n
  })
  expect(note.textContent).toBe('3 pools')
  return document.querySelector('main.work > .c-phead')!
}

async function platformCounts() {
  serve(() => null)
  vi.resetModules()
  const { PlatformCountsScreen } = await import('../PlatformCounts')
  mount(<PlatformCountsScreen />)
  return document.querySelector('main.work > .c-phead')!
}

describe('N3: one gap under every admin page head (run-4 admin-012)', () => {
  for (const [label, env] of [
    ['1440', WIDE],
    ['390', PHONE],
  ] as const) {
    it(`at ${label} a .ur-page page (People) puts the same space under its head as a Screen with a summary`, async () => {
      const screenGap = headGap(await screenWithSummary(), env)
      cleanup()
      document.body.innerHTML = ''
      const head = await people()
      expect(head, 'People draws its head in a .ur-page').not.toBeNull()
      expect(head.nextElementSibling?.classList.contains('ur-sub'), 'the intro is the first line under the head').toBe(true)
      // MUTATION: drop `.ur-page > .c-phead { margin-bottom: 0 }` from repositories.css (the head's 12 px margin and the page's 12 px gap add up to 24).
      expect(headGap(head, env)).toBe(screenGap)
    })

    it(`at ${label} Platform counts puts the same space under its head as a Screen with a summary`, async () => {
      const screenGap = headGap(await screenWithSummary(), env)
      cleanup()
      document.body.innerHTML = ''
      const head = await platformCounts()
      expect(head, 'Platform counts draws the shared head').not.toBeNull()
      // MUTATION: give `.counts-scopes` a `margin-top: var(--ctl-s3)` in styles/admin.css.
      expect(headGap(head, env)).toBe(screenGap)
    })

    it(`at ${label} that one gap is --ctl-s3 (12 px), the head's own margin`, async () => {
      // MUTATION: set `.c-phead { margin: 0 0 var(--ctl-s2) }` in styles.css (every page moves, and the Screen pages lose the gap the .ur-page pages keep).
      expect(headGap(await screenWithSummary(), env)).toBe(12)
      cleanup()
      document.body.innerHTML = ''
      // MUTATION: set `.ur-page { gap: var(--ctl-s2) }` in repositories.css.
      expect(headGap(await people(), env)).toBe(12)
    })
  }
})
