// VISUAL QA RE-RUN LANE VQA2-R3 (part of #1427): Admin › Pool limits, Tenants
// and Platform counts. One case per finding, each asserting the property the
// 2026-10-16 re-run measured as wrong -- a winning declaration, a title, a
// drawn word -- rather than the markup around it.
//
//   V026  a long Principal's key was clipped mid-letter, no ellipsis
//   V079  a long tenant id broke into five lines while Status/Kind sat empty
//   V186  no identity column showed a whole value at 1440
//   V077  the filter scrolled away with the top of a 59-pool page
//   V187  the family heads stuck under the bell bar
//   V160  a long pool id segment ran past the phone card
//   V190  the configured dot was named for a screen reader only
//   V191  Last changed wrapped with a `·` alone at the line end
//   V192  the cards said `not run yet` while the head said `counting…`
//   V193  the edited record's tint stopped 16px short of the card

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, Stats, Tenant } from '../types'
import { cascade, type CascadeEnv } from './cssgate'

const STYLES = `${SHEET}\n${ADMIN}`

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
  loadStats: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen } = await import('../AdminSettings')
const { PlatformCountsScreen, forgetLastRun } = await import('../PlatformCounts')
const { TenantsScreen } = await import('../Activity')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-16T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 4,
    available: 6,
    enabled: true,
    updated_at: '2026-10-16T10:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-16T10:00:00Z' } as Capacity
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

/**
 * Pool limits inside the frame's scroller, under a bell bar that holds
 * something (`bell`) or nothing -- the two cases `--adm-stick` tells apart.
 */
async function limits(pools: Pool[], bell = true): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(capacity(pools)))
  render(
    <div className="ctl-scroll">
      <header className="ctl-bar">{bell && <span className="nt">bell</span>}</header>
      <AdminSettingsScreen />
    </div>,
  )
  await waitFor(() => expect(document.querySelector('table.adm-limits')).not.toBeNull(), WAIT)
}

const many = () => Array.from({ length: 59 }, (_, i) => pool(`tenant:t-${String(i).padStart(2, '0')}-team`))

function tenant(over: Partial<Tenant>): Tenant {
  return {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@example.com',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 45,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic', 'openai', 'github'],
    service_account: 'swarm-agent-worker-eng@example-project.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  document.body.innerHTML = ''
  forgetLastRun()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

async function roster(tenants: Tenant[]): Promise<HTMLElement> {
  api.loadTenants.mockResolvedValue(ok({ tenants }))
  api.loadCapacity.mockResolvedValue(ok(capacity([])))
  const { container } = render(<TenantsScreen />)
  await screen.findByText(tenants[0]!.tenant_id, undefined, WAIT)
  return container
}

const LONG = 'swarm-platform-integration-acceptance'

describe('V026, V186: the key is whole unless the column cannot hold it, then cut with an ellipsis', () => {
  it('never hands the key a share of the overflow, and caps it at the column less the two floors', async () => {
    const c = await roster([tenant({ tenant_id: LONG, principal: `${LONG}@example.com` })])
    const ident = c.querySelector('td[data-label="Principal"] .ten-ident')!
    const key = ident.querySelector('.ten-ident-key')!
    const tail = ident.querySelector('.ten-ident-tail')!
    expect(key.textContent).toBe(`${LONG}@`)
    const shrink = (el: Element) => {
      const v = won(el, ['flex', 'flex-shrink'], WIDE)!
      // `flex: 0 1 auto` or a bare `flex-shrink`: the factor is the number
      // after the grow factor, or the whole value.
      const parts = v.trim().split(/\s+/)
      return Number(parts.length === 1 ? parts[0] : parts[1])
    }
    // ANY SHRINK FACTOR ABOVE 0 GIVES THE KEY A SHARE OF THE OVERFLOW WHILE
    // THE HEAD IS STILL WIDE (V186): at 1440, 0.05px of `u-bogdan@`, which
    // drew `u-bogda…` with client 72 == scroll 72 (Chromium, 2026-10-16).
    // MUTATION: `flex: 0 1 auto` on the key (the first fix for V026) reads 1.
    expect(shrink(key)).toBe(0)
    expect(shrink(tail)).toBeGreaterThan(0)
    // A KEY LONGER THAN THE COLUMN IS STILL CUT, WITH ITS OWN ELLIPSIS (V026),
    // not clipped mid-letter by `.ten-ident`: its cap is the column less the
    // head's and the tail's 2ch floors. MUTATION: drop `max-width` -- this
    // reads null, and the key paints past the cell's edge again.
    expect(won(key, 'max-width', WIDE)).toBe('calc(100% - 4ch)')
    expect(won(tail, 'min-width', WIDE)).toBe('2ch')
    expect(won(key, ['overflow', 'overflow-x'], WIDE)).toBe('hidden')
    expect(won(key, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})

describe('V079, V186: every Tenants column is sized to what it holds at 1440', () => {
  // Measured in headless Chromium at 1440 (a 1054px table, Inter Tight and
  // DM Mono): the widths each column's content needs, padding included.
  const TABLE = 1054
  const NEEDS: Record<string, number> = {
    status: 60, // `enabled`
    kind: 56, // `group`
    principal: 194, // `alice@example.com` + its copy control
    enforced: 72, // the head `Enforced`
    configured: 85, // the head `Configured`
    credentials: 174, // `anthropic` and `openai` on one line
  }

  it('gives the slack of Status, Kind and Tenant to the identities, and cuts no head', async () => {
    const c = await roster([tenant({}), tenant({ tenant_id: LONG })])
    const pct = (col: string) => {
      const w = won(c.querySelector(`col.ten-col-${col}`)!, 'width', WIDE)
      expect(w, `col.ten-col-${col}`).toMatch(/%$/)
      return parseFloat(w!)
    }
    const cols = ['tenant', 'status', 'kind', 'principal', 'enforced', 'configured', 'credentials', 'identity']
    expect(cols.reduce((sum, col) => sum + pct(col), 0)).toBeCloseTo(100, 5)
    // MUTATION: put Principal back to 15% (158px): `alice@example.com` is cut
    // to `alice@ex…` again. Or Configured back to 8%: its head is cut.
    for (const [col, px] of Object.entries(NEEDS)) {
      expect((pct(col) / 100) * TABLE, `${col} is narrower than what it holds`).toBeGreaterThanOrEqual(px)
    }
    // NO SLACK LEFT IN THE SHORT COLUMNS (V186): Status and Kind are within
    // 20px of their word. MUTATION: Status back to 10% (105px).
    expect((pct('status') / 100) * TABLE - NEEDS.status!).toBeLessThan(20)
    expect((pct('kind') / 100) * TABLE - NEEDS.kind!).toBeLessThan(20)
    // The Identity keeps its key whole: 2ch of prefix, `…u-bogdan@…` (72px)
    // and 2ch of domain beside its copy control (V186: `sw… u-bogda… s…`).
    expect((pct('identity') / 100) * TABLE).toBeGreaterThanOrEqual(16 + 72 + 16 + 8 + 46 + 20)
    // A long tenant id still breaks at its hyphens only (V079).
    const th = [...c.querySelectorAll('tbody th[scope="row"]')].find((h) => h.textContent === LONG)!
    expect(won(th, 'overflow-wrap', WIDE)).toBe('break-word')
  })
})

// ---------------------------------------------------------------------------
// Pool limits
// ---------------------------------------------------------------------------

describe('V187: the family heads stick below the bell bar', () => {
  it('sticks each head at the bar’s height, and at 0 where the bar is not drawn', async () => {
    viewport(1440)
    await limits(many().slice(0, 12))
    const section = document.querySelector('section.adm-ceilings')!
    expect(section.classList.contains('has-filter')).toBe(false)
    const head = document.querySelector('table.adm-limits > thead')!
    // MUTATION: drop the head's `position: sticky` (or leave it at Capacity's
    // `top: 0`, under the bar): no head on screen halfway down the list.
    expect(won(head, 'position', WIDE)).toBe('sticky')
    expect(won(head, 'top', WIDE)).toBe('calc(var(--adm-stick, 48px) + var(--adm-filter-h, 0px))')
    expect(won(head.closest('.ctl-table')!, ['overflow', 'overflow-y'], WIDE)).toBe('clip')
    expect(won(section, '--adm-stick', WIDE)).toBe('var(--ctl-bar-h, 48px)')
    expect(won(section, '--adm-filter-h', WIDE)).toBe('0px')
    // Not sticky in the phone records, which hide the head row.
    expect(won(head, 'position', PHONE)).not.toBe('sticky')
  })

  it('opens no gap over the rows when the bar holds nothing', async () => {
    viewport(1440)
    await limits(many().slice(0, 12), false)
    // MUTATION: drop the `.ctl-bar:empty` rule: the heads stick 48px down
    // with rows showing through above them.
    expect(won(document.querySelector('section.adm-ceilings')!, '--adm-stick', WIDE)).toBe('0px')
  })
})

describe('V077: the filter of a long list stays in reach down the page', () => {
  it('draws the filter for 59 pools at 1440 and sticks it, with the heads below it', async () => {
    viewport(1440)
    await limits(many())
    const box = screen.getByRole('searchbox', { name: /filter pools/i })
    const bar = box.closest('.adm-filter')!
    const section = document.querySelector('section.adm-ceilings')!
    expect(section.classList.contains('has-filter')).toBe(true)
    // MUTATION: drop the filter's `position: sticky`: it scrolls away with
    // the top of the page and 59 rows down there is nothing to narrow them by.
    expect(won(bar, 'position', WIDE)).toBe('sticky')
    expect(won(bar, 'top', WIDE)).toBe('var(--adm-stick, 48px)')
    expect(won(bar, 'height', WIDE)).toBe('var(--adm-filter-h, 0px)')
    expect(won(section, '--adm-filter-h', WIDE)).toBe('60px')
    // It paints over the rows that scroll under it.
    expect(won(bar, ['background', 'background-color'], WIDE)).toBe('var(--bg)')
    fireEvent.change(box, { target: { value: 't-07' } })
    expect([...document.querySelectorAll('tbody tr[id]')].map((r) => r.id)).toEqual(['limit-tenant:t-07-team'])
  })
})

describe('V160: a pool id segment longer than the line wraps inside the card', () => {
  it('lays each segment as an inline block no wider than its cell', async () => {
    viewport(400)
    const id = 'tenant:swarm-platform-integration-acceptance-environment-long-name'
    await limits([pool(id)])
    const segs = [...document.getElementById(`limit-${id}`)!.querySelectorAll('th .ctl-sub > .adm-key-seg')]
    expect(segs.map((s) => s.textContent)).toEqual(['tenant:', 'swarm-platform-integration-acceptance-environment-long-name'])
    for (const env of [PHONE, WIDE]) {
      for (const s of segs) {
        // MUTATION: put `white-space: nowrap` back as the winner: the segment
        // runs past the card and the records scroll sideways (446/400).
        expect(won(s, 'white-space', env)).toBe('normal')
        expect(won(s, 'display', env)).toBe('inline-block')
        expect(won(s, 'max-width', env)).toBe('100%')
        expect(won(s, 'overflow-wrap', env)).toBe('anywhere')
      }
    }
  })
})

describe('V190: the configured dot says what it means to a mouse', () => {
  it('carries its own title, with the word and the sentence', async () => {
    viewport(1440)
    await limits([pool('global', { hard_limit: 20, effective_limit: 20 })])
    const dot = document.querySelector('#limit-global td[data-label="Set by"] .adm-setby-cfg')!
    expect(dot.getAttribute('aria-label')).toBe('configured')
    // MUTATION: drop the span's `title`.
    expect(dot.getAttribute('title')).toBe('configured. The configured hard limit of 20 is what applies.')
    expect(won(dot, 'cursor', WIDE)).toBe('help')
  })
})

describe('V191: Last changed never ends a line on a separator', () => {
  it('keeps every `·` with the part after it, in one unbreakable run', async () => {
    viewport(400)
    const at = new Date(Date.now() - 3 * 3_600_000).toISOString()
    api.loadAdminPools.mockResolvedValue(
      ok({
        pools: [
          {
            ...pool('global', { hard_limit: 20, effective_limit: 20 }),
            admin_changed_by: 'ops@example.com',
            admin_changed_at: at,
            admin_change: { hard_limit: { from: 40, to: 20 } },
          },
        ],
      }),
    )
    await limits([pool('global', { hard_limit: 20, effective_limit: 20 })])
    const cell = document.querySelector('#limit-global td[data-label="Last changed"]')!
    await waitFor(() => expect(cell.textContent).toContain('ops@example.com'), WAIT)
    const line = cell.querySelector('.adm-changed')!
    expect(line.textContent).toBe('ops@example.com · 40 → 20 · 3h ago')
    const parts = [...line.querySelectorAll(':scope > .adm-changed-part')]
    expect(parts.map((p) => p.textContent)).toEqual(['· 40 → 20', '· 3h ago'])
    for (const p of parts) expect(won(p, 'white-space', PHONE)).toBe('nowrap')
    // MUTATION: put the separators back after each part (`40 → 20 · `): a
    // text node of the line itself carries a `·` that a break can follow.
    for (const node of line.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) expect(node.textContent).not.toContain('·')
    }
  })
})

describe('V193: the record being edited is tinted to its edges on a phone', () => {
  it('tints the record and not the cells inside its padding', async () => {
    viewport(400)
    await limits([pool('global', { hard_limit: 20, effective_limit: 20 })])
    const row = document.getElementById('limit-global')!
    fireEvent.click(within(row).getByRole('button', { name: 'Edit ceiling for global' }))
    await waitFor(() => expect(row.classList.contains('is-editing')).toBe(true), WAIT)
    expect(row.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    const bg = ['background', 'background-color'] as const
    for (const theme of ['light', 'dark'] as const) {
      const env: CascadeEnv = { ...PHONE, theme }
      // MUTATION: drop the phone rule: the tint is on the cells, 16px inside
      // the card, and the record itself is not tinted.
      expect(won(row, bg, env)).toBe('var(--sk-acs)')
      for (const cell of row.querySelectorAll(':scope > th, :scope > td')) expect(won(cell, bg, env)).toBe('none')
    }
    // The table keeps the tint on its cells.
    expect(won(row.querySelector('td')!, bg, WIDE)).toBe('var(--sk-acs)')
  })
})

// ---------------------------------------------------------------------------
// Platform counts
// ---------------------------------------------------------------------------

describe('V192: while the first run counts, the cards say so', () => {
  it('reads `counting now` and the reading mark in both cards, not `not run yet`', async () => {
    let answer: (r: Result<Stats>) => void = () => {}
    api.loadStats.mockReturnValue(new Promise<Result<Stats>>((r) => (answer = r)))
    api.loadMe.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'x' } })
    render(<PlatformCountsScreen />)
    const cards = () => [...document.querySelectorAll('.counts-notrun')]
    for (const c of cards()) expect(c.textContent).toContain('not run yet')
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelector('.counts-prov')?.textContent).toBe('counting…'), WAIT)
    expect(cards()).toHaveLength(2)
    for (const c of cards()) {
      // MUTATION: draw `not run yet` and `not measured` whatever `counting` is.
      expect(c.textContent).not.toContain('not run yet')
      expect(c.textContent).toContain('counting now. One exact count per state, 12 reads.')
      expect(c.querySelector('.ctl-mark.is-pending')?.textContent).toBe('reading')
      expect(c.querySelector('.ctl-mark.is-absent')).toBeNull()
    }
    answer({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'x' } })
  })

  it('still says why a non-admin’s platform card has no figure while counting', async () => {
    api.loadStats.mockReturnValue(new Promise<Result<Stats>>(() => {}))
    api.loadMe.mockResolvedValue(
      ok({ principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: false } } as never),
    )
    render(<PlatformCountsScreen />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run the count · 12 reads/ })).toBeTruthy(), WAIT)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelector('.counts-prov')?.textContent).toBe('counting…'), WAIT)
    const [mine, every] = [...document.querySelectorAll('.counts-notrun')]
    expect(mine!.textContent).toContain('counting now')
    expect(every!.textContent).toContain('Only an admin’s run counts every tenant.')
  })
})
