// QAFIX4-ADMIN-2 (part of #1680): Admin › Pool limits, one owner decision and
// three leftovers of the third console visual-QA run (wf_d598fa7bffdb41fc9d76,
// 2026-10-21). Each case asserts what the operator saw as wrong, through the
// cascade (`painted`) or the rendered markup.
//
//   admin-005  a pool at its ceiling (anthropic · u-bogdan, 5 of 5) had no
//              mark; decided 2026-10-22 (Console Design Calls 2, c04): a
//              neutral 'full' tag after edit, where 'over ceiling' goes
//   admin-004  'N /M' -- `6 /10` -- drew its figure flush left in a 6ch slot,
//              so the figure and its `/10` sat apart by a different gap on
//              every row, and the two slots could break across lines
//   admin-003  a pool id could still break right after `tenant:`
//   limits-header  the scroller holding `.adm-ceilings` pads by the bar, the
//              filter and the head (owner, 2026-10-22, "Yes, follow-up lane")

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-22T02:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 5,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 5,
    active: 0,
    available: 5,
    enabled: true,
    updated_at: '2026-10-22T02:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-22T02:00:00Z' } as Capacity
}

/** `matchMedia`, which jsdom lacks, answering each query by the width it names. */
function viewport(width: number): void {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    const matches = (max === null || width <= Number(max[1])) && (min === null || width >= Number(min[1]))
    return {
      matches,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  })
}

async function limits(pools: Pool[]): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(capacity(pools)))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.querySelector('table.adm-limits')).not.toBeNull(), WAIT)
}

/** The row's tags after `edit`, by their text. */
function tags(id: string): Map<string, HTMLElement> {
  const edit = document.getElementById(`limit-${id}`)!.querySelector('td.adm-ceiling-cell > .limit-edit')!
  return new Map([...edit.querySelectorAll<HTMLElement>(':scope > span')].map((s) => [s.textContent ?? '', s]))
}

const hosts: HTMLElement[] = []

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
  for (const h of hosts.splice(0)) h.remove()
})

describe('admin-005 (c04): a pool at its ceiling is marked full, neutrally', () => {
  it("tags 5 of 5 'full' after edit, titled with the row's own numbers", async () => {
    viewport(1440)
    await limits([pool('provider:anthropic:tenant:u-bogdan', { active: 5, effective_limit: 5, available: 0 })])
    const full = tags('provider:anthropic:tenant:u-bogdan').get('full')
    // MUTATION: drop the `active === effective_limit` tag from PoolRow: a pool
    // at 5 of 5 shows nothing after edit again.
    expect(full).toBeDefined()
    // It follows `edit`, in the slot 'over ceiling' takes.
    // MUTATION: draw the tag before the `edit` button: it moves `edit`.
    expect(full!.previousElementSibling?.tagName).toBe('BUTTON')
    // MUTATION: drop the title, or write it without the row's figures.
    expect(full!.getAttribute('title')).toBe('At its ceiling: 5 of 5 units in use. New work that needs this pool waits.')
  })

  it("draws 'full' in the over-ceiling tag's neutral style", async () => {
    viewport(1440)
    await limits([pool('tenant:eng', { active: 5 })])
    const full = tags('tenant:eng').get('full')!
    const over = build('td.adm-ceiling-cell > span.limit-edit > span.adm-over', hosts)
    for (const prop of ['display', 'border', 'border-radius', 'color', 'font-size', 'padding', 'white-space']) {
      // MUTATION: give `.adm-full` a rule of its own in `--warn` (or none at
      // all): 'full' reads as a fault, or as a bare word, beside 'over ceiling'.
      expect(painted(full, prop, WIDE), prop).toBe(painted(over, prop, WIDE))
    }
    // MUTATION: paint the tag in a state hue: a full pool is not a fault.
    expect(painted(full, 'color', WIDE)).toBe('var(--text-dim)')
  })

  it("marks 4 of 5 with nothing, and 6 of 5 'over ceiling' and not 'full'", async () => {
    viewport(1440)
    await limits([pool('tenant:a', { active: 4 }), pool('tenant:b', { active: 6 }), pool('tenant:c', { active: 1, effective_limit: 1 })])
    // MUTATION: test `active >= effective_limit - 1`: a pool with room is full.
    expect([...tags('tenant:a').keys()]).toEqual([])
    // MUTATION: test `active >= effective_limit` for 'full': an over-ceiling
    // pool wears both tags.
    expect([...tags('tenant:b').keys()]).toEqual(['over ceiling'])
    // One unit reads in the singular.
    // MUTATION: write the title with a bare `units`: '1 of 1 units'.
    expect(tags('tenant:c').get('full')!.getAttribute('title')).toBe('At its ceiling: 1 of 1 unit in use. New work that needs this pool waits.')
  })
})

describe('admin-004: the in-use and ceiling numerals line up, and N /M stays whole', () => {
  it('aligns every figure alike, the ceiling hard against its /M', async () => {
    viewport(1440)
    await limits([
      pool('global', { active: 0, effective_limit: 6, hard_limit: 10 }),
      pool('tenant:eng', { active: 12, effective_limit: 100, hard_limit: 100 }),
    ])
    for (const id of ['global', 'tenant:eng']) {
      const row = document.getElementById(`limit-${id}`)!
      const use = row.querySelector('td[data-label="In use (units)"] .adm-inuse')!
      const ceiling = row.querySelector('td.adm-ceiling-cell .adm-ceiling')!
      // MUTATION: put `text-align: left` back on `.adm-ceiling` (PR 1854): `6`
      // sits 5ch before its `/10`, `100` 3ch, so the pair splits by a gap
      // that differs on every row.
      expect(painted(ceiling, 'text-align', WIDE), id).toBe('right')
      // MUTATION: align `.adm-inuse` apart from `.adm-ceiling`: the two
      // figure columns stop sharing one alignment.
      expect(painted(use, 'text-align', WIDE), id).toBe(painted(ceiling, 'text-align', WIDE))
      for (const figure of [use, ceiling]) {
        // MUTATION: drop `tabular-nums` from the slots: `1` and `8` differ in width.
        expect(painted(figure, 'font-variant-numeric', WIDE), id).toBe('tabular-nums')
        expect(painted(figure, 'width', WIDE), id).toBe('6ch')
      }
    }
  })

  it('holds the figure and its /M in one unbreakable run', async () => {
    viewport(1440)
    await limits([pool('global', { active: 6, effective_limit: 6, hard_limit: 10 })])
    const cell = document.getElementById('limit-global')!.querySelector('td.adm-ceiling-cell')!
    const ceiling = cell.querySelector('.adm-ceiling')!
    const was = cell.querySelector('.adm-was')!
    expect(ceiling.textContent).toBe('6')
    expect(was.textContent).toBe('/10')
    // MUTATION: draw `.adm-ceiling` and `.adm-was` as the cell's own children
    // again: their nearest common box is the cell, which wraps, so `6` and
    // `/10` can land on two lines.
    const run = ceiling.parentElement!
    expect(run.classList.contains('adm-fig')).toBe(true)
    expect(was.parentElement).toBe(run)
    // MUTATION: drop `white-space: nowrap` from `.adm-fig`.
    expect(painted(run, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(was, 'white-space', WIDE)).toBe('nowrap')
    // `edit` and its tags are outside the run, free to wrap under it.
    // MUTATION: put `.limit-edit` inside `.adm-fig`: 'full' could no longer wrap.
    expect(cell.querySelector('.adm-fig .limit-edit')).toBeNull()
  })

  it('keeps the phone record a figure, a /M slot and edit on the value line', async () => {
    viewport(390)
    await limits([pool('global', { effective_limit: 6, hard_limit: 10 })])
    const cell = document.getElementById('limit-global')!.querySelector('td.adm-ceiling-cell')!
    expect(cell.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    // MUTATION: drop `display: contents` from the phone `.adm-fig` rule: the
    // run becomes one grid item and its slots leave columns 2 and 3.
    expect(painted(cell.querySelector('.adm-fig')!, 'display', PHONE)).toBe('contents')
    // MUTATION: drop the phone `text-align: left`: the figure leaves the value column.
    expect(painted(cell.querySelector('.adm-ceiling')!, 'text-align', PHONE)).toBe('left')
    expect(painted(cell.querySelector('.adm-ceiling')!, 'grid-column', PHONE)).toBe('2')
    expect(painted(cell.querySelector('.adm-was')!, 'grid-column', PHONE)).toBe('3')
  })
})

describe('admin-003: a pool id never breaks right after a colon', () => {
  it('holds each kind:name pair whole and cuts one too long, the whole id on its title', async () => {
    viewport(1440)
    const id = 'provider:anthropic:tenant:u-bogdan'
    await limits([pool(id)])
    const sub = document.getElementById(`limit-${id}`)!.querySelector<HTMLElement>('th .ctl-sub')!
    const segs = [...sub.querySelectorAll('.adm-key-seg')]
    expect(segs.map((s) => s.textContent)).toEqual(['provider:anthropic:', 'tenant:u-bogdan'])
    for (const env of [WIDE, PHONE]) {
      for (const s of segs) {
        // MUTATION: put `white-space: normal` with `overflow-wrap: anywhere`
        // back on `.adm-key-seg`: a pair wider than its line breaks anywhere
        // in itself, `tenant:` / `u-bogdan` included.
        expect(painted(s, 'white-space', env)).toBe('nowrap')
        expect(painted(s, 'overflow-wrap', env) ?? 'normal').not.toMatch(/anywhere|break-word/)
        // A pair too long for the cell is cut, never run past the card (V160).
        // MUTATION: drop the ellipsis: a long pair runs out of the card.
        expect(painted(s, 'max-width', env)).toBe('100%')
        expect(painted(s, 'overflow', env)).toBe('hidden')
        expect(painted(s, 'text-overflow', env)).toBe('ellipsis')
      }
    }
    // MUTATION: drop the title from the id: a cut pair cannot be read whole.
    expect(sub.getAttribute('title')).toBe(id)
  })
})

describe('limits-header: the scroller holding .adm-ceilings clears the bar, filter and head', () => {
  it('pads the scroll by the bar plus the filter plus the head row', () => {
    const head = build(
      'div.ctl-frame.has-ctl-bar > div.ctl-scroll > div.app > section.section.adm-ceilings.has-filter > div.cap-families > div.ctl-table.is-scroll > table.adm-limits > thead > tr',
      hosts,
    )
    // MUTATION: drop `height: var(--row-h)` from the sticky head row.
    expect(painted(head, 'height', WIDE)).toBe('var(--row-h)')
    // MUTATION: drop the `.ctl-scroll:has(.adm-ceilings …)` scroll-padding
    // rule: a row scrolled into view stops under the filter and the head.
    expect(painted(head.closest('.ctl-scroll')!, 'scroll-padding-top', WIDE)).toBe('calc(48px + 60px + var(--row-h))')
    // MUTATION: add `scroll-snap-type` to the scroller: BGD chose no snapping.
    expect(painted(head.closest('.ctl-scroll')!, 'scroll-snap-type', WIDE)).toBeNull()
  })
})
