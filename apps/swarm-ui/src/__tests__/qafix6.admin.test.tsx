// QAFIX6-ADMIN (part of #1680): Pool limits, Runner sizes and Tenants, from
// console visual QA run 6 (wf_31aca00c977e4d37a935, 2026-10-23, the fixture
// build at 1440 and 390, light and dark) and the boxes earlier runs left open.
// One `describe` per item of the lane, each named by its finding and its
// #1680 comment:
//
//   a  6:admin-006 (6343762428), 5b:QA-052 (6343784689)  Model prices was
//      wider than its card at 1440, as admin and as member, and Set by was cut
//      to 'ops@example.c'.
//   b  6:admin-014 (new)        the tiered row's figures sat right, the
//      inputs' figures left, in one price column.
//   c  5b (6343789520)           Providers wrapped only
//      provider:anthropic:tenant:u-bogdan, a lone tall row.
//   d  6:admin-010 (6343787880)  Additional repositories' labels touched their
//      inputs.
//   e  6:admin-016, 6:admin-017 (new)  at 390 'full' dropped under `edit`, and
//      '6' and '/10' were drawn about 60px apart.
//   f  QA-044 (6283714920)       the phone card's field labels were mono.
//   g  6:admin-012 (new)         Runner sizes' class cards stretched to the
//      tallest.
//   h  6:admin-013 (new)         a class its pool admits none of (0 → 0) was
//      not marked.
//   i  5b:QA-019 (6343763163)    'What the pools admit' at 390 (verified fixed
//      on main by QAFIX5; pinned here).
//   j  6:admin-008 (6343786219), 6:admin-009 (6343785475)  Tenants cut
//      Principal at 1440 and broke addresses mid-word at 390.
//
// Widths are read off the shipped sheets through `painted` (the cascade, with
// its `@media` blocks) and laid out by the table-fit model (tablefit.ts),
// whose text widths are upper bounds. Every assertion names the mutation that
// turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, Pool, Tenant } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { cellStyle, fixedColumns, fontPx, lengthPx, textPx, type CellStyle } from './tablefit'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  setPoolLimit: vi.fn(),
  loadMe: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen, ModelPricesCard, addressBreaks } = await import('../AdminSettings')
const { PoolsTable } = await import('../RunnerSizes')
const { TenantIdent, TenantsScreen } = await import('../Activity')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

/**
 * THE 1440 CARD WIDTHS. admin.css records Pool limits' work column at 1440
 * (84+236px nav) as about 1024px. A Card is a 1px border and 16px of body
 * padding a side, and `.ctl-table` a 1px border: the price table is 988px.
 * The families sit in `.cap-families`, whose card body has no side padding:
 * 1022px.
 */
const PRICES_1440 = 1024 - 2 - 32 - 2
const FAMILIES_1440 = 1024 - 2
/** The phone record's width at 390: the page's 16px a side and the record's 16px a side. */
const RECORD_390 = 390 - 32 - 32
/** styles.css's stacked key track, `calc(var(--t-micro) * 10.5)` at 12px. */
const KEY_TRACK = 12 * 10.5
/** The stacked field's column gap, `--ctl-s3`. */
const FIELD_GAP = 12

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-23T09:00:00Z' }
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
    updated_at: '2026-10-23T09:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-23T09:00:00Z' } as Capacity
}

function tenant(over: Partial<Tenant>): Tenant {
  return {
    tenant_id: 'u-bogdan',
    kind: 'user',
    principal: 'alice@example.com',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 2,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: 'swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  } as Tenant
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
  cleanup()
  vi.unstubAllGlobals()
  for (const h of hosts.splice(0)) h.remove()
})

/** The longest run of `text` a line cannot break inside, at the breaks a `normal` cell takes (spaces and hyphens). */
function longestRun(text: string): string {
  return text
    .split(/\s+|(?<=-)/)
    .reduce((a, b) => (b.length > a.length ? b : a), '')
}

// ---------------------------------------------------------------------------
// a. Model prices fits its card
// ---------------------------------------------------------------------------

describe('a. 6:admin-006, 5b:QA-052: Model prices fits its 1440 card, as admin and as member', () => {
  for (const admin of [true, false]) {
    it(`lays nine columns inside ${PRICES_1440}px with every value whole (${admin ? 'admin' : 'member'})`, async () => {
      render(<ModelPricesCard admin={admin} />)
      const table = await screen.findByRole<HTMLTableElement>('table', { name: 'Model prices' }, WAIT)
      await waitFor(() => expect(table.querySelector('tbody tr')).not.toBeNull(), WAIT)
      // MUTATION: drop `table-layout: fixed` or the heads' widths from
      // `.adm-price-table`: the table is sized by its content again and
      // `fixedColumns` refuses it, or its min-width passes the card.
      const { width, cols } = fixedColumns(table, PRICES_1440, WIDE)
      expect(width, 'the table is wider than its card: it scrolls').toBeLessThanOrEqual(PRICES_1440)
      // READ ONCE PER COLUMN AND KIND OF CELL: the cascade is slow, and each
      // column's cells share their rules (tablefit.ts says the same).
      const styles = new Map<string, CellStyle>()
      const style = (el: Element, key: string): CellStyle => {
        if (!styles.has(key)) styles.set(key, cellStyle(el, WIDE))
        return styles.get(key)!
      }
      const heads = [...table.querySelectorAll('thead th')]
      cols.forEach((box, i) => {
        const th = heads[i]!
        const st = style(th, `head:${th.className}`)
        const room = box.end - box.start - st.pl - st.pr
        // A head may take two lines, never a cut word.
        for (const word of (th.textContent ?? '').split(/\s+/)) {
          expect([...word].length * st.em, `head ${th.textContent} cuts '${word}'`).toBeLessThanOrEqual(room)
        }
      })
      let fieldFont: number | null = null
      for (const row of table.querySelectorAll('tbody tr')) {
        const cells = [...row.children]
        cells.forEach((cell, i) => {
          const box = cols[i]!
          const st = style(cell, `${i}:${cell.tagName}:${cell.className}`)
          const room = box.end - box.start - st.pl - st.pr
          const input = cell.querySelector<HTMLInputElement>('input')
          if (input !== null) {
            // The field is its declared `Nch + 20px`, in its own face.
            const w = painted(input, 'width', WIDE)!
            const m = /^calc\((\d+)ch \+ (\d+)px\)$/.exec(w)
            expect(m, `${input.getAttribute('aria-label')} width ${w}`).not.toBeNull()
            fieldFont ??= fontPx(input, WIDE)
            const px = Number(m![1]) * 0.6 * fieldFont + Number(m![2])
            expect(px, `${input.getAttribute('aria-label')} is wider than its column`).toBeLessThanOrEqual(room)
            return
          }
          if (cell.getAttribute('data-label') === 'Set by') {
            // MUTATION: give Set by less than its address: 'ops@example.c'.
            const text = cell.textContent ?? ''
            if (text === '—') return
            expect([...text].length * st.em, `Set by cuts '${text}'`).toBeLessThanOrEqual(room)
            return
          }
          // The model's name and tier lines, Currency and Source wrap at spaces
          // and hyphens; none of their runs may be wider than the column.
          const tiers = [...cell.querySelectorAll('li')]
          const own = tiers.length > 0 ? (cell.firstChild?.textContent ?? '') : (cell.textContent ?? '')
          const lines: [string, number][] = [[own, st.em], ...tiers.map((li): [string, number] => [li.textContent ?? '', style(li, 'tier').em])]
          for (const [line, em] of lines) {
            const run = longestRun(line)
            expect([...run].length * em, `${cell.getAttribute('data-label')} cuts '${run}'`).toBeLessThanOrEqual(room)
          }
        })
      }
      expect(within(table).getAllByText((_, el) => el?.textContent === 'ops@example.com' && el.tagName === 'TD')).toHaveLength(1)
    }, 30_000)
  }

  it("lets Set by break after '@' and nowhere else", async () => {
    render(<ModelPricesCard admin={false} />)
    const table = await screen.findByRole('table', { name: 'Model prices' }, WAIT)
    const cell = await waitFor(() => {
      const c = [...table.querySelectorAll('td[data-label="Set by"]')].find((td) => td.textContent === 'ops@example.com')
      expect(c).toBeDefined()
      return c!
    }, WAIT)
    // MUTATION: draw the address as bare text: no <wbr>, and the cell's
    // hyphen and dot are break points (or, nowrap, it can never wrap).
    const wbr = cell.querySelectorAll('wbr')
    expect(wbr).toHaveLength(1)
    expect(wbr[0]!.previousSibling?.textContent).toBe('ops@')
    for (const seg of cell.querySelectorAll('span')) expect(painted(seg, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(cell, 'white-space', WIDE)).toBe('normal')
  })
})

// ---------------------------------------------------------------------------
// b. One alignment per price column
// ---------------------------------------------------------------------------

describe('b. 6:admin-014: a price column draws its figures at one alignment', () => {
  it('right-aligns the inputs’ figures as the read-only ones are, in tabular figures', async () => {
    render(<ModelPricesCard admin />)
    const table = await screen.findByRole('table', { name: 'Model prices' }, WAIT)
    const input = await waitFor(() => {
      const i = table.querySelector<HTMLInputElement>('td.is-num input')
      expect(i).not.toBeNull()
      return i!
    }, WAIT)
    const readOnly = [...table.querySelectorAll('td.is-num')].find((td) => td.querySelector('input') === null)!
    expect(readOnly, 'the tiered row draws its figures read-only').toBeDefined()
    // MUTATION: drop `text-align: right` from `.adm-price-table td.is-num >
    // .adm-price-in`: the field's figure sits at its left, under a right one.
    expect(painted(readOnly, 'text-align', WIDE)).toBe('right')
    expect(painted(input, 'text-align', WIDE)).toBe(painted(readOnly, 'text-align', WIDE))
    expect(painted(input, 'font-variant-numeric', WIDE)).toBe('tabular-nums')
  })
})

// ---------------------------------------------------------------------------
// c. Providers' lone tall row
// ---------------------------------------------------------------------------

describe('c. 5b (6343789520): the Pool column holds provider:anthropic:tenant:u-bogdan at 1440', () => {
  it('fits the raw key on one line, and keeps the wrap point between pairs', async () => {
    viewport(1440)
    const name = 'provider:anthropic:tenant:u-bogdan'
    await limits([pool(name)])
    const row = document.getElementById(`limit-${name}`)!
    const th = row.querySelector('th.pool-name')!
    const sub = th.querySelector('.ctl-sub')!
    const col = row.closest('table')!.querySelector('col.adm-col-pool')!
    const st = cellStyle(th, WIDE)
    const room = lengthPx(painted(col, 'width', WIDE), FAMILIES_1440)! - st.pl - st.pr
    // MUTATION: put the Pool column back to 25% (255px): the 34-character key
    // (245px in its 12px mono) and the cell's padding no longer fit, and the
    // key wraps between its pairs, the one tall row.
    expect(room).toBeGreaterThanOrEqual(textPx(name, sub, WIDE))
    // The decided wrap point stays: a <wbr> after each pair's colon.
    expect(sub.querySelectorAll('wbr')).toHaveLength(1)
    // The five columns still add up to the table.
    const total = [...row.closest('table')!.querySelectorAll('col')].reduce((n, c) => n + parseFloat(painted(c, 'width', WIDE) ?? '0'), 0)
    expect(total).toBeCloseTo(100, 5)
  })
})

// ---------------------------------------------------------------------------
// d. Labels touching inputs
// ---------------------------------------------------------------------------

describe('d. 6:admin-010: an Additional repositories label is 8px from its field', () => {
  it('lays each label and its input out with a gap', () => {
    const label = build('div.adm-repo-limits form.adm-token-form > label.ur-small', hosts)
    // MUTATION: drop the rule: the label is inline text and its field starts
    // at the last letter.
    expect(painted(label, 'display', WIDE)).toBe('inline-flex')
    expect(painted(label, 'gap', WIDE)).toBe('8px')
    expect(painted(label, 'display', PHONE)).toBe('inline-flex')
  })
})

describe('d. BUDGET-UI-SETTINGS review: an Agent budget ceiling label is 8px from its field', () => {
  it('lays each label and its input out with a gap', () => {
    const label = build('div.adm-budget-ceiling form.adm-token-form > label.ur-small', hosts)
    // MUTATION: drop `.adm-budget-ceiling` from the rule: 'Dollars per agent,
    // at most' runs into its input's border.
    expect(painted(label, 'display', WIDE)).toBe('inline-flex')
    expect(painted(label, 'gap', WIDE)).toBe('8px')
    expect(painted(label, 'display', PHONE)).toBe('inline-flex')
  })
})

// ---------------------------------------------------------------------------
// e. The phone ceiling row
// ---------------------------------------------------------------------------

describe('e. 6:admin-016, 6:admin-017: at 390 the ceiling row keeps "6 /10" together and "full" after edit', () => {
  it('draws the figure and its target as one nowrap element', async () => {
    viewport(390)
    await limits([pool('tenant:eng', { effective_limit: 6, hard_limit: 10 })])
    const cell = document.getElementById('limit-tenant:eng')!.querySelector('td.adm-ceiling-cell')!
    expect(cell.closest('.ctl-table')!.classList.contains('is-stacked')).toBe(true)
    const ceiling = cell.querySelector('.adm-ceiling')!
    const was = cell.querySelector('.adm-was')!
    const run = ceiling.parentElement!
    expect(was.parentElement).toBe(run)
    expect(run.textContent).toBe('6/10')
    // MUTATION: put `display: contents` back on the phone `.adm-fig`: its two
    // slots are grid columns 2 and 3 again, the 6ch slot between '6' and '/10'.
    expect(painted(run, 'display', PHONE)).not.toBe('contents')
    expect(painted(run, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(ceiling, 'width', PHONE)).toBe('auto')
    expect(painted(was, 'width', PHONE)).toBe('auto')
    // `edit` still starts at one x: the run is one fixed track.
    expect(painted(run, 'min-width', PHONE)).toBe('8ch')
  })

  it("leaves room for 'full' after edit on the ceiling's row", async () => {
    viewport(390)
    await limits([pool('provider:anthropic:tenant:u-bogdan', { active: 5, effective_limit: 5, available: 0 })])
    const cell = document.getElementById('limit-provider:anthropic:tenant:u-bogdan')!.querySelector('td.adm-ceiling-cell')!
    const run = cell.querySelector('.adm-fig')!
    const button = cell.querySelector('.limit-edit > button')!
    const full = cell.querySelector('.limit-edit > .adm-full')!
    expect(full.textContent).toBe('full')
    const [bl, br] = [10, 10]
    const editPx = textPx('edit', button, PHONE) + bl + br + 2
    const fullPx = textPx('full', full, PHONE) + 2 * 4 + 2
    const runPx = 8 * 0.6 * fontPx(run, PHONE)
    // MUTATION: keep the 6ch and 7ch slots as two grid tracks: the row is
    // KEY + 12 + 50 + 12 + 50 + 12 + edit + 6 + full, past the record, and
    // 'full' drops to a line of its own under `edit`.
    const tracks = painted(cell, 'grid-template-columns', PHONE) ?? ''
    expect(tracks.endsWith(') auto minmax(0, 1fr)'), tracks).toBe(true)
    expect(tracks).not.toMatch(/auto auto/)
    expect(KEY_TRACK + FIELD_GAP + runPx + FIELD_GAP + editPx + 6 + fullPx).toBeLessThanOrEqual(RECORD_390)
    expect(painted(cell.querySelector('.limit-edit')!, 'grid-column', PHONE)).toBe('3')
  })
})

// ---------------------------------------------------------------------------
// f. Monospace labels on the phone card
// ---------------------------------------------------------------------------

describe('f. QA-044: the phone card’s field labels are set in the sans face', () => {
  it('draws Pool limits’ stacked keys in the prose face, as the column heads are', async () => {
    viewport(390)
    await limits([pool('global')])
    const cells = [...document.getElementById('limit-global')!.querySelectorAll('td[data-label]')]
    expect(cells.map((c) => c.getAttribute('data-label'))).toEqual(['In use (units)', 'Ceiling (units)', 'Set by', 'Last changed'])
    for (const c of cells) {
      // The key face is console-wide since BGD's decision of 2026-10-23
      // (QA-044, task_7f0095fb2fd9481790cc q0): styles.css's shared stacked
      // rule draws it sans and admin.css carries no override of its own
      // (qa044.sanskeys.test.tsx holds the other pages).
      // MUTATION: put `var(--mono)` back in styles.css's stacked
      // `td[data-label]::before` font: Pool limits' keys are mono again.
      const face = painted(c, ['font-family', 'font'], PHONE, 'before') ?? ''
      expect(face, c.getAttribute('data-label')!).toMatch(/var\(--font\)/)
      expect(face).not.toMatch(/--mono/)
    }
  })
})

// ---------------------------------------------------------------------------
// g. Runner sizes' cards
// ---------------------------------------------------------------------------

describe('g. 6:admin-012: each class card is as tall as its own content', () => {
  it('does not stretch the cards to the tallest one', () => {
    const grid = build('div.rs-classes', hosts)
    // MUTATION: drop `align-items: start`: the grid stretches every card to
    // the browser card's height, 540-680px of empty card under the others.
    expect(painted(grid, 'align-items', WIDE)).toBe('start')
  })
})

// ---------------------------------------------------------------------------
// h. A class its pool cannot admit
// ---------------------------------------------------------------------------

describe('h. 6:admin-013: a class whose units exceed its pool’s ceiling is marked too small', () => {
  const pools = [{ pool: 'resource:large', hard_limit: 2, admits_before: { large: 0, standard: 2 }, admits_after: { large: 0, standard: 2 } }]
  const classes = {
    large: { units_before: 4, units_after: 4, cost_hour_before: 1, cost_hour_after: 1 },
    standard: { units_before: 1, units_after: 1, cost_hour_before: 0.25, cost_hour_after: 0.25 },
  }

  it("carries Pools' 'too small' mark and says none of it can start", () => {
    render(<PoolsTable pools={pools} classes={classes} />)
    const table = screen.getByRole('table', { name: 'What the pools admit' })
    const large = table.querySelector('tr[data-class="large"]')!
    // MUTATION: drop the mark: a save that leaves `large` unrunnable is silent.
    const mark = within(large as HTMLElement).getByText('too small')
    expect(mark.closest('[data-tone]')?.getAttribute('data-tone')).toBe('paused')
    expect(large.textContent).toContain('none of it can start')
    expect(large.textContent).toContain('4 units')
    // A class the pool admits is not marked.
    const standard = table.querySelector('tr[data-class="standard"]')!
    expect(within(standard as HTMLElement).queryByText('too small')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// i. 'What the pools admit' at 390 (verify first)
// ---------------------------------------------------------------------------

describe("i. 5b:QA-019: 'What the pools admit' is a stacked labelled record at 390", () => {
  it('lays each row out as labelled fields under the phone media rule', () => {
    viewport(390)
    render(<PoolsTable pools={[{ pool: 'global', hard_limit: 40, admits_before: { standard: 20 }, admits_after: { standard: 20 } }]} classes={null} />)
    const table = screen.getByRole('table', { name: 'What the pools admit' })
    expect(table.parentElement!.classList.contains('is-stacked')).toBe(true)
    // MUTATION: drop `.rs-pools-wrap.is-stacked { overflow-x: visible }` or
    // the stacked display: the four columns overflow the 390 card again.
    expect(painted(table.parentElement!, 'overflow-x', PHONE)).toBe('visible')
    for (const td of table.querySelectorAll('tbody td')) {
      expect(painted(td, 'display', PHONE)).toBe('grid')
      expect(td.getAttribute('data-label')).not.toBeNull()
    }
  })
})

// ---------------------------------------------------------------------------
// j. Tenants' addresses
// ---------------------------------------------------------------------------

describe('j. 6:admin-009, 6:admin-008: Tenants shows principals whole at 1440 and breaks addresses only after @ and . at 390', () => {
  /** The roster's table width at 1440, measured in Chromium (vqa2.r3.admin.test.tsx). */
  const TABLE = 1054

  it('gives the Principal column room for alice@example.com and its copy control', async () => {
    viewport(1440)
    api.loadTenants.mockResolvedValue(ok({ tenants: [tenant({})] }))
    api.loadCapacity.mockResolvedValue(ok(capacity([])))
    const { container } = render(<TenantsScreen />)
    await screen.findByText('u-bogdan', undefined, WAIT)
    const cell = container.querySelector('td[data-label="Principal"]')!
    const ident = cell.querySelector('.ten-ident')!
    const copy = cell.querySelector('button.ten-copy')!
    const col = container.querySelector('col.ten-col-principal')!
    const st = cellStyle(cell, WIDE)
    const room = lengthPx(painted(col, 'width', WIDE), TABLE)! - st.pl - st.pr
    const copyPx = textPx('copy', copy, WIDE) + 16 + 2
    // MUTATION: put Principal back to 19% (200px): 'alice@example.com' at
    // 14px mono, the 8px gap and the copy control are 218px, and the address
    // is cut 'alice@example…'.
    expect(room).toBeGreaterThanOrEqual(textPx('alice@example.com', ident, WIDE) + 8 + copyPx)
    // The columns still add up to the table.
    const total = [...container.querySelectorAll('col')].reduce((n, c) => n + parseFloat(painted(c, 'width', WIDE) ?? '0'), 0)
    expect(total).toBeCloseTo(100, 5)
  })

  it('cuts the domain by the same rule in every row', async () => {
    viewport(1440)
    render(<TenantIdent value="swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com" noun="service account" tenant="u-bogdan" phone={false} />)
    const head = document.querySelector('.ten-ident-head')!
    const tail = document.querySelector('.ten-ident-tail')!
    // MUTATION: weight the tail by its length again: flexbox shrinks it by
    // its weight times its length, so every row's domain is cut at a
    // different letter ('swa…u-bogdan@e…').
    // NOT CUT AT ALL NOW (visual QA pass 2, admin-shell-18): the domain
    // wraps on its own line, so every row shows all of it.
    expect(painted(tail, 'flex', WIDE)).toBe('0 1 auto')
    expect(painted(tail, 'max-width', WIDE)).toBe('100%')
    // The prefix is no longer the second cut (visual QA pass 1,
    // admin-shell-18): it is whole on its own line, cut only when it alone
    // is wider than the column (qa1fix03.tenants.test.tsx).
    expect(painted(head, 'flex', WIDE)).toBe('0 1 auto')
    expect(painted(head, 'max-width', WIDE)).toBe('100%')
    // The copy control carries the whole value.
    const copy = screen.getByRole('button', { name: /^Copy service account / })
    expect(copy.getAttribute('title')).toContain('swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com')
  })

  it("puts a <wbr> before the key and after each '@' and '.', and nowhere else, at 390", () => {
    viewport(390)
    const value = 'swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com'
    render(<TenantIdent value={value} noun="service account" tenant="u-bogdan" phone />)
    const ident = document.querySelector('.ten-ident')!
    // The whole value is still the element's text: a selection copies all of it.
    expect(ident.textContent).toBe(value)
    const before = [...ident.querySelectorAll('wbr')].map((w) => (w.previousSibling?.textContent ?? '').slice(-1))
    // MUTATION: break the segments at hyphens too, or draw the value bare.
    // The first break is before the key (visual QA pass 1, admin-shell-02):
    // `swarm-agent-worker-` / `u-bogdan@` / `example-project.` / ...
    expect(before).toEqual(['-', '@', '.', '.', '.'])
    expect([...ident.querySelectorAll('.ten-seg')].map((s) => s.textContent)).toEqual([
      'swarm-agent-worker-',
      'u-bogdan@',
      'example-project.',
      'iam.',
      'gserviceaccount.',
      'com',
    ])
    for (const seg of ident.querySelectorAll('.ten-seg')) {
      // Each run is unbroken; one wider than the line is cut, never broken.
      expect(painted(seg, 'white-space', PHONE)).toBe('nowrap')
      expect(painted(seg, 'display', PHONE)).toBe('inline-block')
      expect(painted(seg, 'text-overflow', PHONE)).toBe('ellipsis')
    }
    // MUTATION: leave `overflow-wrap: anywhere` reaching the identity: it
    // breaks 'bogdan@exampl' / 'e-'.
    expect(painted(ident, 'overflow-wrap', PHONE)).toBe('normal')
  })

  it('splits a value after the characters it is asked to, keeping every character', () => {
    const { container } = render(<span>{addressBreaks('ops@example.com', '@.', 'x')}</span>)
    expect(container.textContent).toBe('ops@example.com')
    expect([...container.querySelectorAll('.x')].map((s) => s.textContent)).toEqual(['ops@', 'example.', 'com'])
  })
})
