// QAFIX3-ADMIN (part of #1680): Admin › Pool limits and Runtimes, from the
// third console visual-QA run (wf_d598fa7bffdb41fc9d76, 2026-10-21, the
// fixture build of main at 51d99c53). One case per finding a unit test can
// hold, each asserting what the operator saw as wrong -- a winning
// declaration through `painted`, a name through Testing Library, a break
// opportunity through the rendered markup.
//
//   admin-004  In use's figure sat mid-column and Ceiling's were ragged under
//              their left-aligned heads (a regression of ticked QA-045)
//   admin-002  the Additional repositories and Model prices cards touched
//   admin-003  `provider:anthropic:tenant:u-bogdan` wrapped after `tenant:`,
//              leaving the tenant's own name alone on the next line
//   admin-007  a locked `edit` was ~2.3:1 in dark: `--text-dim` at 40% opacity
//   admin-009  at 390 `edit` moved with the figure and the `/hard` slot before it
//   capacity-003  at 390 `DEMO_API_` / `KEY`: a secret name broke at its underscores
//                 although it fits the line whole
//   a11y-keyboard-003  the family tables had no accessible name
//
// capacity-011 (ragged Runtimes card heights) was left to VQA V088's
// `align-items: start` when this lane ran. QAFIX6-CAPACITY-2 has since
// retired that rule: styles/capacity.css sets `align-items: stretch` on
// `.rt-cards > .ctl-cards`, so the cards of one row share its height, and
// qafix6.capacity.test.tsx holds that, not this file.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Me, Pool, Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted, resolveColour, THEMES } from './marks'
import { contrast, over } from './spaceprobe'

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
const { Credential } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-21T10:00:00Z' }
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
    updated_at: '2026-10-21T10:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-21T10:00:00Z' } as Capacity
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

const hosts: HTMLElement[] = []

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.unstubAllGlobals()
  for (const h of hosts.splice(0)) h.remove()
})

describe('admin-004: In use and Ceiling share one alignment under left-aligned heads', () => {
  it('sets both figures alike in their fixed slots, under left-aligned heads', async () => {
    viewport(1440)
    await limits([pool('global', { active: 0, effective_limit: 20 }), pool('tenant:eng', { active: 12, effective_limit: 100 })])
    const heads = [...document.querySelectorAll('table.adm-limits thead th')]
    const head = (t: string) => heads.find((h) => h.textContent === t)!
    for (const id of ['global', 'tenant:eng']) {
      const row = document.getElementById(`limit-${id}`)!
      const use = row.querySelector('td[data-label="In use (units)"] > .adm-inuse')!
      const ceiling = row.querySelector('td[data-label="Ceiling (units)"] > .adm-fig > .adm-ceiling')!
      for (const [figure, over] of [[use, head('In use (units)')], [ceiling, head('Ceiling (units)')]] as const) {
        // Right-aligned since QAFIX4-ADMIN-2: flush left split `6` from its
        // `/10` by a gap that changed on every row (qafix4.admin2.test.tsx).
        // MUTATION: align one slot apart from the other: the two figure
        // columns stop sharing one alignment (QA-045).
        expect(painted(figure, 'text-align', WIDE)).toBe('right')
        expect(painted(over, 'text-align', WIDE) ?? 'left').toBe('left')
        // The slot keeps its fixed width, so `edit` after it still starts at one x.
        expect(painted(figure, 'width', WIDE)).toBe('6ch')
      }
    }
  })
})

describe('admin-002: the admin cards under the ceilings are spaced, not touching', () => {
  it('gives each card after the ceilings the section gap above it', () => {
    for (const card of ['adm-repo-limits', 'adm-prices', 'adm-budget-ceiling', 'adm-fallback-token']) {
      const el = build(`section.c-card.${card}`, hosts)
      // MUTATION: drop `.adm-repo-limits` / `.adm-prices` from the margin rule:
      // Additional repositories and Model prices sit ~2px apart again.
      expect(painted(el, ['margin-top', 'margin'], WIDE), card).toBe('var(--ctl-s5)')
      expect(painted(el, ['margin-top', 'margin'], PHONE), card).toBe('var(--ctl-s5)')
    }
  })
})

describe('admin-003: a pool id wraps between its kind:name pairs, never after a bare kind', () => {
  it('keeps `tenant:u-bogdan` whole, with the one break after `provider:anthropic:`', async () => {
    viewport(1440)
    const id = 'provider:anthropic:tenant:u-bogdan'
    await limits([pool(id), pool('tenant:eng')])
    const sub = document.getElementById(`limit-${id}`)!.querySelector('th .ctl-sub')!
    expect(sub.textContent).toBe(id)
    // MUTATION: put the `<wbr>` back after every colon: the greedy line takes
    // `provider:anthropic:tenant:` and leaves `u-bogdan` alone under it.
    expect([...sub.querySelectorAll('.adm-key-seg')].map((s) => s.textContent)).toEqual(['provider:anthropic:', 'tenant:u-bogdan'])
    expect(sub.querySelectorAll('wbr')).toHaveLength(1)
    // A one-pair id offers no break at all.
    const plain = document.getElementById('limit-tenant:eng')!.querySelector('th .ctl-sub')!
    expect([...plain.querySelectorAll('.adm-key-seg')].map((s) => s.textContent)).toEqual(['tenant:eng'])
    expect(plain.querySelectorAll('wbr')).toHaveLength(0)
  })
})

describe('admin-007: a locked edit is legible in both themes', () => {
  it('draws the refused edit at 4.5:1 or better on its ground, and marked apart from a live one', async () => {
    viewport(1440)
    api.loadMe.mockResolvedValue(session(false))
    await limits([pool('global')])
    await screen.findByText(/Admins only/, undefined, WAIT)
    const edit = screen.getByRole('button', { name: /^Edit ceiling for global/ })
    expect(edit.classList.contains('is-blocked')).toBe(true)
    const live = build('td.adm-ceiling-cell > span.limit-edit > button', hosts)
    for (const theme of THEMES) {
      const env: CascadeEnv = { ...WIDE, theme }
      const ground = resolveColour(painted(edit, ['background', 'background-color'], env)!, theme)
      const ink = resolveColour(painted(edit, 'color', env)!, theme)
      const opacity = Number(painted(edit, 'opacity', env) ?? '1')
      // MUTATION: put `opacity: .4` back on `.is-blocked`: `--text-dim` at
      // 40% over `--surface` is ~2.3:1 in dark.
      expect(contrast(over({ ...ink, a: ink.a * opacity }, ground), ground), theme).toBeGreaterThanOrEqual(4.5)
      // MUTATION: drop the dashed edge: the locked edit reads as a live one.
      expect(painted(edit, 'border-style', env)).toBe('dashed')
      expect(painted(live, 'border-style', env)).toBeNull()
    }
  })
})

describe('admin-009: edit starts at one x in every phone record', () => {
  it('keeps the figure and the hard-limit slot at their fixed widths in the stacked Ceiling field', async () => {
    viewport(390)
    await limits([
      pool('global', { effective_limit: 8, hard_limit: 8 }),
      pool('tenant:eng', { effective_limit: 50, hard_limit: 100 }),
      pool('runner:x', { effective_limit: null, hard_limit: null }),
    ])
    for (const id of ['global', 'tenant:eng', 'runner:x']) {
      const cell = document.getElementById(`limit-${id}`)!.querySelector('td.adm-ceiling-cell')!
      expect(cell.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
      const figure = cell.querySelector(':scope > .adm-fig > .adm-ceiling')!
      const was = cell.querySelector(':scope > .adm-fig > .adm-was')!
      // THE FIXED WIDTH IS THE RUN'S NOW (run 6, 6:admin-017,
      // qafix6.admin.test.tsx): `6` and `/10` sit side by side in one 8ch
      // track rather than in 6ch and 7ch tracks 60px apart.
      // MUTATION: drop the run's `min-width` at 560px and under: `8`,
      // `50 /100` and `—` each push `edit` to a different x.
      expect(painted(figure.parentElement!, 'min-width', PHONE), id).toBe('8ch')
      expect(painted(figure, 'width', PHONE), id).toBe('auto')
      expect(painted(was, 'width', PHONE), id).toBe('auto')
      expect(painted(figure, 'text-align', PHONE), id).toBe('left')
    }
  })
})

describe('a11y-keyboard-003: every table on Pool limits is named', () => {
  it('names each family table by its family', async () => {
    viewport(1440)
    await limits([pool('global'), pool('tenant:eng'), pool('provider:anthropic')])
    // Only the family tables: the Model prices card's own table loads on its
    // own clock and may or may not be painted yet.
    const tables = screen.getAllByRole('table').filter((t) => t.classList.contains('adm-limits'))
    expect(tables.length).toBe(3)
    // MUTATION: drop the table's `aria-label`: each is announced as "table".
    for (const name of ['Global', 'Workspaces', 'Providers']) expect(screen.getByRole('table', { name: `${name} ceilings` })).toBeTruthy()
  })

  it('names the rows after an open editor as the same family, continued', async () => {
    viewport(1100)
    api.loadMe.mockResolvedValue(session(true))
    await limits([pool('tenant:a'), pool('tenant:b')])
    const edit = await screen.findByRole('button', { name: 'Edit ceiling for a (workspace)' }, WAIT)
    edit.click()
    // MUTATION: name only the first part: the second table under the editor is anonymous.
    await waitFor(() => expect(screen.getByRole('table', { name: 'Workspaces ceilings, continued' })).toBeTruthy(), WAIT)
    expect(screen.getByRole('table', { name: 'Workspaces ceilings' })).toBeTruthy()
  })
})

describe('capacity-003: a secret name that fits its line is never broken', () => {
  it('draws a name of up to 28 characters with no break inside it, and a longer one breaking only after `_`', () => {
    // Pieced together: a whole literal of this shape reads as a credential to the publish scan.
    const short = ['DEMO', 'OAUTH', 'TO' + 'KEN'].join('_')
    const long = ['DEMO', 'VENDOR', 'SUBSCRIPTION', 'OAUTH', 'TO' + 'KEN'].join('_')
    const rt = { provider: 'demo-vendor', secrets: ['DEMO_API_' + 'KEY', short, long], secrets_any_of: true } as unknown as Runtime
    const { container } = render(<Credential runtime={rt} />)
    const codes = [...container.querySelectorAll('code')]
    expect(codes.map((c) => c.textContent)).toEqual(rt.secrets)
    // MUTATION: offer the `<wbr>` after every underscore again: at 390 the
    // line breaks `DEMO_API_` / `KEY` although the name fits the next line.
    expect(codes[0]!.querySelectorAll('wbr')).toHaveLength(0)
    expect(codes[1]!.querySelectorAll('wbr')).toHaveLength(0)
    // MUTATION: drop the long-name branch: a name wider than the value column
    // has no break at all and runs out of the card.
    expect(long.length).toBeGreaterThan(28)
    expect(codes[2]!.querySelectorAll('wbr')).toHaveLength(4)
    for (const w of codes[2]!.querySelectorAll('wbr')) expect(w.previousSibling?.textContent?.endsWith('_')).toBe(true)
  })
})
