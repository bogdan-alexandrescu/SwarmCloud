// QA1B-13 (part of #2596, the console visual QA pass 1 epic): pass 1 lanes
// wrote these fixes as patches for types.ts, help.ts and styles.css because
// those files were outside their territory. This file holds each one.
//
//   capacity-21         Pools' Set by said `AIMD back-off`: the algorithm's
//                       name, not what happened. It says the ceiling was
//                       lowered automatically after errors (QA1-FIX-03).
//   capacity-14         a backend pool printed the API's enum,
//                       `CLOUD_RUN_JOB`, as its name: poolLabel says
//                       `Cloud Run` / `GKE Autopilot` (QA1-FIX-08).
//   admin-shell-17      no help topic said where the platform owner and the
//                       config-group admins come from: People and roles'
//                       scope topic names PLATFORM_OWNER and ADMIN_GROUPS
//                       (QA1-FIX-17).
//   runs-submit         Repositories' help named an `Add new repository`
//                       button the screen no longer draws; it names
//                       `Choose more on GitHub` (QA1-FIX-21).
//   overview-agents-59  the Now card drew a not-started task's state word in
//                       the mono 600 reading face; it is the line's plain
//                       sans (QA1-FIX-32).
//
// The review of this lane (2026-10-28) found capacity-21's plain words did
// not fit the columns sized for `AIMD back-off`: three lines on Pools (a 61px
// row against 46px), `lowered autom…` on Pool limits. The cases under
// `capacity-21's words fit` hold the rebalanced columns.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { HELP } from '../help'
import { LivenessBadge } from '../Liveness'
import { BACKEND_POOL_WORD, poolLabel, setBy } from '../types'
import type { Pool, Task } from '../types'
import { backendWord } from '../words'
import WORDS_TS from '../words.ts?raw'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { cellStyle, lengthPx, textPx } from './tablefit'

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

const WIDE: CascadeEnv = { width: 1440 }

function pool(over: Partial<Pool>): Pool {
  return {
    name: 'tenant:eng',
    hard_limit: 10,
    effective_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    ...over,
  } as Pool
}

describe('capacity-21: a ceiling AIMD lowered says so in words', () => {
  it('names neither AIMD nor back-off', () => {
    const by = setBy(pool({ adaptive_target: 3, effective_limit: 3 }))
    // MUTATION: restore `AIMD back-off`.
    expect(by.term).toBe('lowered automatically after errors')
    expect(by.detail).toBe('Lowered automatically to 3 after provider errors; configured is 10.')
    expect(`${by.term} ${by.detail}`).not.toMatch(/AIMD|back-off/i)
  })
})

describe('capacity-14: a backend pool is named in words', () => {
  it.each([
    ['backend:CLOUD_RUN_JOB', 'Cloud Run'],
    ['backend:GKE_AUTOPILOT', 'GKE Autopilot'],
    // A backend this console has no word for keeps its id rather than a guess.
    ['backend:SOMETHING_NEW', 'SOMETHING_NEW'],
  ] as ReadonlyArray<[string, string]>)('%s -> %s', (name, label) => {
    expect(poolLabel(name)).toBe(label)
  })

  it('leaves every other kind as it was', () => {
    expect(poolLabel('tenant:eng')).toBe('eng')
    expect(poolLabel('provider:anthropic:tenant:u-x')).toBe('anthropic · u-x')
  })

  it('says each backend as words.ts backendWord does, for every backend either names', () => {
    // words.ts's BACKEND_WORD keys, read from its source: it is not exported,
    // and words.ts cannot be imported by types.ts (StatePill's cycle).
    const block = /const BACKEND_WORD[^=]*=\s*\{([^}]*)\}/.exec(WORDS_TS)?.[1]
    expect(block, 'words.ts no longer declares BACKEND_WORD').toBeDefined()
    const named = [...block!.matchAll(/^\s*([A-Z_]+):/gm)].map((m) => m[1]!)
    // `AUTO` is a choice made at dispatch, never a pool.
    const pools = named.filter((id) => id !== 'AUTO')
    expect(pools.length).toBeGreaterThan(0)
    // MUTATION: add a third backend to BACKEND_WORD alone, or to BACKEND_POOL_WORD alone.
    expect(Object.keys(BACKEND_POOL_WORD).sort()).toEqual([...pools].sort())
    for (const id of pools) expect(poolLabel(`backend:${id}`), id).toBe(backendWord(id))
  })
})

describe('admin-shell-17: help says where the platform roles come from', () => {
  it('People and roles names PLATFORM_OWNER and ADMIN_GROUPS', () => {
    const text = HELP['people-and-roles-scope'].long.join(' ')
    expect(text).toContain('PLATFORM_OWNER')
    expect(text).toContain('ADMIN_GROUPS')
    expect(text).toContain('the Admins card does not list them')
  })

  it('names the screen it means, never `this page` or `here`, which on Help is Help', () => {
    const text = HELP['people-and-roles-scope'].long.join(' ')
    // MUTATION: put back `it is not switched here`.
    expect(text).not.toMatch(/\bthis page\b|\bswitched here\b/)
    expect(text).toContain('no switch on People and roles changes it')
  })
})

describe('Repositories help names the button the screen draws', () => {
  it('says Choose more on GitHub, not Add new repository', () => {
    const short = HELP['config-repositories'].short
    expect(short).toContain('Choose more on GitHub (Choose on GitHub while nothing is chosen) opens GitHub access')
    expect(short).not.toContain('Add new repository')
  })
})

describe('overview-agents-59: a not-started state word is the plain sans', () => {
  const task = (state: string) => ({ task_id: 'task_1', state }) as unknown as Task

  it('draws `ready` in the line face, not the mono reading face', () => {
    const { container } = render(<LivenessBadge task={task('READY')} events={[]} now={Date.now()} />)
    const badge = container.querySelector('.liveness[data-liveness="not-started"]')!
    expect(badge, 'no not-started badge').not.toBeNull()
    const word = badge.querySelector('.lv-word')!
    expect(word.textContent).toBe('ready')
    // MUTATION: drop the not-started rule.
    expect(painted(word, 'font-family', { width: 1440 })).toBe('var(--font)')
    expect(painted(word, 'font-weight', { width: 1440 })).toBe('400')
  })

  it('keeps a live reading in the mono face', () => {
    const { container } = render(<LivenessBadge task={task('RUNNING')} events={null} now={Date.now()} />)
    const word = container.querySelector('.liveness .lv-word')!
    expect(painted(word, 'font-family', { width: 1440 })).not.toBe('var(--font)')
  })
})

/**
 * The families table on Pools at 1440 with the nav open: the review measured
 * Set by's 11% at 114px, so the table is ~1036px. Pool limits' families are
 * 1022px (qafix6.admin.test.tsx `FAMILIES_1440`).
 */
const POOLS_1440 = 1036
const FAMILIES_1440 = 1024 - 2
/** The tablefit model's upper bound on a phrase's width in `cell`. */
const content = (col: Element, cell: Element, basis: number): number => {
  const st = cellStyle(cell, WIDE)
  return lengthPx(painted(col, 'width', WIDE), basis)! - st.pl - st.pr
}

describe("capacity-21's words fit the columns sized for `AIMD back-off`", () => {
  it('Pools: Set by holds `lowered automatically` on one line, so the term is two lines at most', () => {
    const col = build('div.ctl-table.cap-pools > table > colgroup > col.cap-c-by', hosts)
    const cell = build('div.cap-families > div.ctl-table.cap-pools > table > tbody > tr > td', hosts)
    cell.setAttribute('data-label', 'Set by')
    // MUTATION: put .cap-c-by back to 11% -- the term is three lines, the row 61px.
    expect(content(col, cell, POOLS_1440)).toBeGreaterThanOrEqual(textPx('lowered automatically', cell, WIDE))
    expect(textPx('after errors', cell, WIDE)).toBeLessThan(textPx('lowered automatically', cell, WIDE))
  })

  it('Pool limits: Set by wraps between words instead of cutting the phrase', () => {
    const cell = build('div.cap-families > div.ctl-table > table.adm-limits > tbody > tr > td.adm-setby', hosts)
    // MUTATION: put back `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`.
    expect(painted(cell, 'white-space', WIDE)).not.toBe('nowrap')
    expect(painted(cell, 'text-overflow', WIDE)).not.toBe('ellipsis')
    // `.cap-families`' `overflow-wrap: anywhere` would split a word; this cell never does.
    expect(painted(cell, 'overflow-wrap', WIDE)).toBe('normal')
    const col = build('div.cap-families > div.ctl-table > table.adm-limits > colgroup > col.adm-col-by', hosts)
    // Its longest word fits whole, and the column grew past the 12% sized for `AIMD back-off`.
    expect(content(col, cell, FAMILIES_1440)).toBeGreaterThanOrEqual(textPx('automatically', cell, WIDE))
    expect(lengthPx(painted(col, 'width', WIDE), 100)).toBeGreaterThan(12)
  })
})
