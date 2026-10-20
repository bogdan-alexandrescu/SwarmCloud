// QAFIX-ADMIN (part of #1680): Admin › Pool limits and Platform counts, from
// the first console visual-QA run (wf_9e9bf74834634db7bcc3, 2026-10-20). One
// case per finding a unit test can hold, each asserting what the operator saw
// as wrong in the crop -- a winning declaration through `painted`, a name or a
// state through Testing Library -- rather than the markup around it.
//
//   QA-037  every `Edit ceiling for …` was natively disabled, so its reason
//           (`Admins only…`) could not be reached by keyboard
//   QA-043  the phone card's Last changed wrapped to a line starting `· 40 → 20`
//   QA-045  In use and Ceiling drew their figures in two faces, two alignments
//   QA-099  `configured` was a 3px `·` a sighted reader took for an empty cell
//   QA-042  `Platform counts` and `Absent, zero, unread and stale` read as one link
//   QA-098  `Platform ticks` sat 18px under the cards with nothing marking a
//           change of subject
//
// QA-044 (the stacked keys in mono) is the platform-wide key face of every
// stacked record (styles.css, `td[data-label]::before`) and is asked in the
// PR rather than changed here.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
  loadStats: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen } = await import('../AdminSettings')
const { PlatformCountsScreen, forgetLastRun } = await import('../PlatformCounts')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-20T10:00:00Z' }
}

function session(isAdmin: boolean): Result<Me> {
  return ok({
    tenant: { tenant_id: 'eng' },
    principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
    environment: 'dev',
    environment_declared: false,
  } as unknown as Me)
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 20,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 20,
    active: 0,
    available: 20,
    enabled: true,
    updated_at: '2026-10-20T10:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-20T10:00:00Z' } as Capacity
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

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  document.body.innerHTML = ''
  forgetLastRun()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('QA-037: a refused Edit is reachable, and says why', () => {
  it('keeps every Edit in the tab order, named with its reason, and opens nothing on a press', async () => {
    viewport(1440)
    api.loadMe.mockResolvedValue(session(false))
    await limits([pool('global'), pool('tenant:eng'), pool('provider:anthropic')])
    await screen.findByText(/Admins only/, undefined, WAIT)
    const edits = screen.getAllByRole('button', { name: /^Edit ceiling for / })
    expect(edits).toHaveLength(3)
    for (const edit of edits as HTMLButtonElement[]) {
      // MUTATION: put `disabled={…}` back on the button: a keyboard never
      // lands on it and the `Admins only` sentence on it is never read.
      expect(edit.disabled).toBe(false)
      // MUTATION: drop `aria-disabled`: a screen reader announces a live control.
      expect(edit.getAttribute('aria-disabled')).toBe('true')
      expect(edit.getAttribute('aria-label')).toMatch(/Admins only: changing a ceiling needs the platform admin group\.$/)
      edit.focus()
      expect(document.activeElement).toBe(edit)
      fireEvent.click(edit)
      // MUTATION: let the press through `onOpen`: the editor opens for a
      // reader the route will refuse.
      expect(document.querySelector('aside.adm-side')).toBeNull()
      expect(edit.getAttribute('aria-expanded')).toBe('false')
      // It still looks unavailable. MUTATION: drop `.is-blocked`'s opacity.
      expect(painted(edit, 'opacity', WIDE)).toBe('.4')
      expect(painted(edit, 'cursor', WIDE)).toBe('not-allowed')
    }
  })
})

describe('QA-043: the phone card’s Last changed never starts a line on a separator', () => {
  it('sets each part on a line of its own with no `·` drawn, and keeps the one-line run at 1440', async () => {
    viewport(390)
    const at = new Date(Date.now() - 3 * 3_600_000).toISOString()
    api.loadAdminPools.mockResolvedValue(
      ok({
        pools: [
          {
            ...pool('global'),
            admin_changed_by: 'ops@example.com',
            admin_changed_at: at,
            admin_change: { hard_limit: { from: 40, to: 20 } },
          },
        ],
      }),
    )
    await limits([pool('global')])
    const cell = document.querySelector('#limit-global td[data-label="Last changed"]')!
    await waitFor(() => expect(cell.textContent).toContain('ops@example.com'), WAIT)
    expect(cell.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    const line = cell.querySelector('.adm-changed')!
    expect(line.textContent).toBe('ops@example.com · 40 → 20 · 3h ago')
    const by = line.querySelector(':scope > .adm-changed-by')!
    expect(by.textContent).toBe('ops@example.com')
    const parts = [...line.querySelectorAll(':scope > .adm-changed-part')]
    expect(parts).toHaveLength(2)
    // MUTATION: drop the phone rule: the author and its parts share lines, and
    // a break after the author leaves `· 40 → 20 · 3h ago` under it.
    expect(painted(by, 'display', PHONE)).toBe('block')
    for (const p of parts) {
      expect(painted(p, 'display', PHONE)).toBe('block')
      const sep = p.querySelector(':scope > .adm-changed-sep')!
      expect(sep.textContent).toBe('·')
      expect(painted(sep, 'display', PHONE)).toBe('none')
      // The table keeps its one line of `·`-led runs (VQA V191).
      expect(painted(sep, 'display', WIDE)).toBeNull()
      expect(painted(p, 'display', WIDE)).toBeNull()
    }
  })
})

describe('QA-045: In use and Ceiling draw their figures alike', () => {
  it('sets both figures in the mono figure face, in one slot shape, under heads aligned alike', async () => {
    viewport(1440)
    await limits([pool('global', { active: 0, effective_limit: 20 })])
    const row = document.getElementById('limit-global')!
    const use = row.querySelector('td[data-label="In use (units)"] > .adm-inuse')!
    const ceiling = row.querySelector('td[data-label="Ceiling (units)"] > .adm-ceiling')!
    expect(use.textContent).toBe('0')
    expect(ceiling.textContent).toBe('20')
    for (const figure of [use, ceiling]) {
      // MUTATION: drop the mono face from `.adm-ceiling`: its `20` is sans
      // beside In use's mono `0`.
      expect(painted(figure, 'font-family', WIDE)).toBe('var(--mono)')
      expect(painted(figure, 'font-variant-numeric', WIDE)).toBe('tabular-nums')
      // MUTATION: leave In use a right-aligned `td.is-num`: one figure at its
      // column's right edge, the other at 6ch from its column's left.
      expect(painted(figure, 'width', WIDE)).toBe('6ch')
      expect(painted(figure, 'text-align', WIDE)).toBe('right')
      expect(painted(figure, 'display', WIDE)).toBe('inline-block')
    }
    const heads = [...document.querySelectorAll('table.adm-limits thead th')]
    const head = (t: string) => heads.find((h) => h.textContent === t)!
    // MUTATION: keep `num: true` on In use: its head is right-aligned over a
    // figure at the slot, the Ceiling head left over the same slot.
    expect(head('In use (units)').classList.contains('is-num')).toBe(false)
    expect(painted(head('In use (units)'), 'text-align', WIDE)).toBe(painted(head('Ceiling (units)'), 'text-align', WIDE))
  })

  it('starts both figures at the value column of the phone record', async () => {
    viewport(390)
    await limits([pool('global', { active: 0, effective_limit: 20 })])
    const row = document.getElementById('limit-global')!
    expect(row.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    for (const figure of [row.querySelector('.adm-inuse')!, row.querySelector('.adm-ceiling')!]) {
      // MUTATION: drop the phone `.adm-inuse` rule: `0` sits 5ch into the
      // value column while `20` starts at its edge.
      expect(painted(figure, 'text-align', PHONE)).toBe('left')
      expect(painted(figure, 'width', PHONE)).toBe('auto')
    }
  })
})

describe('QA-099: `configured` is a mark a sighted reader can see', () => {
  it('draws a 6px disc in the dim ink in place of the glyph, named for a screen reader', async () => {
    viewport(1440)
    await limits([pool('global')])
    const dot = document.querySelector('#limit-global td[data-label="Set by"] .adm-setby-cfg')!
    expect(screen.getByRole('img', { name: 'configured' })).toBe(dot)
    // The glyph stays in the text, as Pools' dot has it (qa.c6d.pools), but
    // the mark a reader sees is the drawn disc.
    expect(dot.textContent).toBe('·')
    for (const env of [WIDE, PHONE]) {
      // MUTATION: leave the glyph at the cell's size: a 3px `·` over the disc.
      expect(painted(dot, 'font-size', env)).toBe('0')
      // MUTATION: put the `·` glyph back as the only mark: no box is drawn.
      expect(painted(dot, 'display', env)).toBe('inline-block')
      expect(painted(dot, 'width', env)).toBe('6px')
      expect(painted(dot, 'height', env)).toBe('6px')
      expect(painted(dot, 'border-radius', env)).toBe('50%')
      // MUTATION: paint it `--text-faint`, the tone that read as nothing.
      expect(painted(dot, ['background-color', 'background'], env)).toBe('var(--text-dim)')
    }
  })
})

describe('QA-042: the two help links on Platform counts read as two', () => {
  it('rules a separator between the links without adding a word to either name', async () => {
    api.loadMe.mockResolvedValue(session(true))
    render(<PlatformCountsScreen />)
    const first = screen.getByRole('link', { name: 'Platform counts' })
    const second = screen.getByRole('link', { name: 'Absent, zero, unread and stale' })
    expect(first.closest('.counts-help')).not.toBeNull()
    // MUTATION: drop the `.counts-help a + a` rule: the links sit 8px apart,
    // both underlined, and read as one phrase.
    expect(painted(second, 'border-left-style', WIDE)).toBe('solid')
    expect(painted(second, 'border-left-width', WIDE)).toBe('1px')
    expect(painted(second, 'padding-left', WIDE)).toBe('var(--ctl-s2)')
    expect(painted(first, 'border-left-style', WIDE)).toBeNull()
  })
})

describe('QA-098: Platform ticks opens a new subject under the cards', () => {
  it('takes the large break and the hairline above its heading', () => {
    api.loadMe.mockResolvedValue(session(true))
    render(<PlatformCountsScreen />)
    const ticks = screen.getByRole('heading', { name: 'Platform ticks' }).closest('section')!
    expect(ticks.classList.contains('counts-ticks')).toBe(true)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `.counts-ticks`' margin: the heading sits 8px (the
      // h2's own) under the cards.
      expect(painted(ticks, 'margin-top', env)).toBe('var(--ctl-s5)')
      expect(painted(ticks, 'border-top-style', env)).toBe('solid')
      expect(painted(ticks, 'border-top-width', env)).toBe('1px')
      expect(painted(ticks, 'padding-top', env)).toBe('var(--ctl-s5)')
    }
  })
})
