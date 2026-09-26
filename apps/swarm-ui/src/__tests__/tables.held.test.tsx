// THE HELD COLUMN HAS A FLOOR, NOT ONLY A CEILING (#222, the 2026-09-26 QA).
//
// Measured on dev at release 350c244, 390x844, in both themes: the held first
// column of Capacity › Runtimes, Admin › Tenants and the Timeline's three
// tables -- the Table view, Reliability and Workflows that failed -- resolved
// to 24-29px. That is the cell's 20px of padding and ONE character, so every
// name printed one character per line, down the left edge of a table whose
// whole point below 900px is that the name stays readable while the figures
// scroll beside it.
//
// WHY `width: min(45vw, 20ch)` DID NOT TAKE. In automatic table layout a
// cell's `width` is not a floor. CSS Tables 3 builds a column's MIN-CONTENT
// width from its cells' outer min-content widths, and a cell's is
// `max(min-width, min-content width)` -- `width` enters only the MAX-content
// width. These tables are `width: 100%` of a 358px scrollport (`.ctl-table >
// table`, `table.pools`), and the cells beside the held one are `nowrap`, so
// the columns' min-content widths add up to more than the scrollport. A table
// cannot be narrower than its min-content width, so it is laid out AT it:
// every column gets exactly its min-content width, and the held cell's --
// `white-space: normal; overflow-wrap: anywhere`, so it may break between any
// two characters -- is one character wide. The declared width is a
// max-content figure nothing asked for.
//
// Pools and Profile headroom never showed it because below 900px their tables
// are `width: max-content` (CP-18's override), which hands every column its
// max-content width -- and there the held cell's `width` does count.
//
// THE FIX IS IN THE SHARED RULE, so it reaches every held column, not these
// five: the held cell's `min-width` is its ceiling, `min(45vw, 20ch)`, which
// enters its min-content width and holds the column there whether its table
// is laid out at min-content or at max-content.
//
// These render the screens and ask the cascade (`cssgate.ts`, through
// `marks.ts`) about the cells they drew, because jsdom applies no `@media`
// block. `tables.scroll.test.tsx` holds the same floor on Pools and Profile
// headroom, and `chrome.shared.test.tsx` on API reads.
//
// WHAT NONE OF THIS CAN SEE: a pixel. The spec's column algorithm is the
// argument above, not a measurement; the dev screenshots after the next
// release are the evidence that the column is readable.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Tenant } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { RuntimesScreen } = await import('../Runtimes')
const { TenantsScreen } = await import('../Activity')
const { LedgerTable, ReliabilityCard, WorkflowsFailedCard } = await import('../Ledger')

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const

/** The ceiling and the floor, as the shared rule writes them. */
const HELD = /^min\((\d+(?:\.\d+)?)vw,\s*(\d+(?:\.\d+)?)ch\)$/

/**
 * The held column of one rendered table: the first cell of its first head row
 * and of every body row. A cell that spans the row is not the held column
 * (`:not([colspan])` in the rule), and a grouped head's second row holds
 * nothing (Tenants' `Max active`), so neither is collected here.
 */
function heldColumn(table: Element): Element[] {
  const rows = [...table.querySelectorAll(':scope > thead > tr:first-child, :scope > tbody > tr')]
  const cells = rows
    .map((tr) => tr.firstElementChild)
    .filter((c): c is Element => c !== null && !(c.tagName === 'TD' && c.hasAttribute('colspan')))
  expect(cells.length, 'the table drew no head and no rows').toBeGreaterThan(1)
  return cells
}

/**
 * THE PROPERTY: at 390 every held cell's `min-width` is its `width`, and both
 * are the shared ceiling -- so its min-content width is the ceiling, and a
 * table laid out at min-content still gives the name its 20ch. A phone rule
 * only: at 1440 nothing floors a first column in viewport units.
 *
 * MUTATION: drop `min-width` from the held-column rule in the CH-13 block of
 * styles.css (the 24-29px column on dev), or give it any value but the
 * ceiling.
 */
function expectFloored(table: Element, what: string): number {
  const cells = heldColumn(table)
  for (const cell of cells) {
    const name = `${what}: "${(cell.textContent ?? '').trim().slice(0, 40)}"`
    expect(painted(cell, 'position', PHONE), `${name} is not the held column`).toBe('sticky')
    const width = painted(cell, 'width', PHONE) ?? ''
    const floor = painted(cell, 'min-width', PHONE) ?? ''
    expect(width, `${name}: the held column's width is ${JSON.stringify(width)}, not the ceiling`).toMatch(HELD)
    expect(
      floor,
      `${name}: the held column has no floor (${JSON.stringify(floor)}), so a table laid out at its ` +
        'min-content width squeezes it to one character',
    ).toBe(width)
    const m = HELD.exec(floor)!
    // Under half the viewport, so the columns that scroll always have the rest.
    expect(Number(m[1]), `${name}: the floor may take ${m[1]}vw of a phone`).toBeLessThanOrEqual(50)
    expect(painted(cell, 'min-width', WIDE) ?? 'none', `${name}: a phone floor reached the wide table`).not.toMatch(/vw/)
  }
  return cells.length
}

function tablesIn(root: ParentNode, what: string): Element[] {
  const tables = [...root.querySelectorAll('.is-scroll > table')]
  expect(tables.length, `${what} drew no scrolling table`).toBeGreaterThan(0)
  return tables
}

function tenant(over: Partial<Tenant>): Tenant {
  return {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@saga.xyz',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 20,
    capacity_units: 20,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic'],
    service_account: 'swarm-agent-worker-eng@example.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

describe('the held first column keeps a readable width at 390 (#222)', () => {
  it('floors both Runtimes tables', async () => {
    // The development fixture's runtime catalogue: Backends and Sizing.
    const { container } = render(<RuntimesScreen />)
    await waitFor(() => expect(container.querySelectorAll('.is-scroll > table').length).toBeGreaterThanOrEqual(2), WAIT)
    let visited = 0
    for (const t of tablesIn(container, 'Runtimes')) visited += expectFloored(t, 'Runtimes')
    // COUNTED, so a helper that visited nothing cannot pass as a clean sweep.
    expect(visited, 'the Runtimes sweep visited too few held cells to mean anything').toBeGreaterThanOrEqual(4)
  })

  async function roster(): Promise<Element> {
    api.loadTenants.mockResolvedValue({
      status: 'ok',
      data: {
        tenants: [
          tenant({}),
          tenant({ tenant_id: 'u-sw-c90291', kind: 'user', principal: 'swarm-verify@example.iam.gserviceaccount.com' }),
          tenant({ tenant_id: 'smoke', enabled: false, service_account: null }),
        ],
      },
      fetchedAt: Date.now(),
    } satisfies Result<{ tenants: Tenant[] }>)
    const { container } = render(<TenantsScreen />)
    await screen.findByText('u-sw-c90291', undefined, WAIT)
    const [table] = tablesIn(container, 'Tenants')
    return table!
  }

  it('floors the Tenants roster', async () => {
    expect(expectFloored(await roster(), 'Tenants')).toBe(4)
  })

  it('leaves the Tenants grouped head’s second row out of the held column', async () => {
    // `Max active` is the first cell of the head's second row, a figure's head
    // from the middle of the table: not held, and no floor -- at 20ch it would
    // widen the Configured group for nothing. THE PROPERTY, not the rule that
    // gives it: the held rules name only the head's first row, so nothing here
    // needs undoing. MUTATION: widen the held rule's head branch in the CH-13
    // block back to every head row (`thead > tr > th:first-child`).
    const table = await roster()
    const maxActive = table.querySelector(':scope > thead > tr:nth-child(2) > th:first-child')!
    expect((maxActive.textContent ?? '').trim()).toBe('Max active')
    expect(painted(maxActive, 'position', PHONE) ?? 'static', 'a Configured head is held at the left edge').not.toBe('sticky')
    expect(painted(maxActive, 'min-width', PHONE) ?? '0', 'a Configured head carries the held column’s floor').not.toMatch(HELD)
  })

  it('floors the Timeline’s Table view, Reliability and Workflows that failed', () => {
    const data = ledgerFixture()
    const { container } = render(
      <>
        <LedgerTable data={data} />
        <ReliabilityCard data={data} group="runner_profile" platform={false} onGroup={() => {}} />
        <WorkflowsFailedCard data={data} spanLabel="the last 14 days" />
      </>,
    )
    const tables = tablesIn(container, 'the Timeline')
    expect(tables.length, 'the three Timeline tables did not all draw').toBe(3)
    let visited = 0
    for (const t of tables) visited += expectFloored(t, 'the Timeline')
    // Fourteen day buckets (12-25 Sep) and their head, four profiles and
    // theirs, three workflows and theirs.
    expect(visited).toBe(15 + 5 + 4)
  })
})
