// ADMIN › TENANTS, AS BEHAVIOUR (#86: AH-11, AH-12, AH-21).
//
// The 2026-09-25 visual QA found the roster failing at the one question it is
// for -- can this tenant run work, and how much of it:
//
//   * at 1440 the Status column (enabled/disabled) sat past the panel edge,
//     behind a scrollbar this platform never paints, pushed there by two
//     55-65 character service-account emails in `nowrap` cells (AH-11);
//   * `Max active` and `Units` were printed as bare registry values, and the
//     ceiling admission actually applies -- the smaller of the two, which is
//     what the tenant's pool is set to -- was nowhere (AH-12);
//   * the budget note printed a schema field and a rationale
//     (`no monthly_budget_usd column · no cost attribution source`) under a
//     `not measured` mark, although a budget is a setting and not a
//     measurement (AH-21).
//
// The api module is replaced so the table renders from tenants built here.
// Every block was pushed before the change it demands.

import STYLES from '../styles.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import type { Result } from '../fetch'
import { HELP } from '../help'
import type { Tenant } from '../types'
import { cascade, type CascadeEnv } from './cssgate'

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TenantsScreen } = await import('../Activity')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

/**
 * Identities as long as the live roster's: service accounts of 55-65
 * characters, and a principal that is itself a service account.
 */
const LONG_SA = 'swarm-agent-worker-u-sw-c90291@example.iam.gserviceaccount.com'
const LONG_PRINCIPAL = 'swarm-verify@example-project.iam.gserviceaccount.com'

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
    credentials: ['anthropic', 'openai'],
    service_account: 'swarm-agent-worker-eng@example.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

/**
 * Three tenants, one per case the Enforced column has to get right: the two
 * configured values equal, max active the smaller, units the smaller.
 */
const ROSTER: Tenant[] = [
  tenant({}),
  tenant({
    tenant_id: 'u-sw-c90291',
    kind: 'user',
    principal: LONG_PRINCIPAL,
    max_active: 2,
    capacity_units: 4,
    credentials: [],
    service_account: LONG_SA,
  }),
  tenant({
    tenant_id: 'smoke',
    principal: 'swarm-smoke@saga.xyz',
    max_active: 10,
    capacity_units: 4,
    enabled: false,
    credentials: [],
    service_account: null,
  }),
]

async function roster(): Promise<HTMLElement> {
  api.loadTenants.mockResolvedValue({
    status: 'ok',
    data: { tenants: ROSTER },
    fetchedAt: Date.now(),
  } satisfies Result<{ tenants: Tenant[] }>)
  const { container } = render(<TenantsScreen />)
  await screen.findByText('u-sw-c90291')
  return container
}

/** The text a reader sees: the help card's hidden copy is not on the glass. */
function visible(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const hidden of copy.querySelectorAll('[data-help-description], button[aria-label^="Help: "]')) hidden.remove()
  return (copy.textContent ?? '').replace(/\s+/g, ' ').trim()
}

function row(container: HTMLElement, id: string): HTMLTableRowElement {
  const th = [...container.querySelectorAll('tbody th[scope="row"]')].find((h) => h.textContent === id)
  expect(th, `no row for ${id}`).toBeTruthy()
  return th!.closest('tr') as HTMLTableRowElement
}

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

describe('Tenants puts the status beside the name and fits at 1440 (AH-11)', () => {
  it('reads Tenant, then Status, before anything else', async () => {
    const c = await roster()
    const heads = [...c.querySelectorAll('thead tr:first-child th')].map(visible)
    expect(heads.slice(0, 2), `the head row is ${heads.join(' | ')}`).toEqual(['Tenant', 'Status'])
    // ...and every row agrees with its head: the first cell after the name is
    // the state, so a disabled tenant is visible without scrolling.
    const cell = row(c, 'smoke').querySelector('td')
    expect(cell?.getAttribute('data-label')).toBe('Status')
    expect(visible(cell)).toBe('disabled')
  })

  it('shortens the long identities on the wide table and keeps their full value', async () => {
    const c = await roster()
    const r = row(c, 'u-sw-c90291')
    for (const [label, full] of [
      ['Identity', LONG_SA],
      ['Principal', LONG_PRINCIPAL],
    ] as const) {
      const value = r.querySelector(`td[data-label="${label}"] .ten-ident`)
      expect(value, `the ${label} value is not the shortened element`).not.toBeNull()
      // IN COPY: the element holds every character, so a selection copies the
      // whole address -- the ellipsis is paint, not text.
      expect(value!.textContent).toBe(full)
      // ON HOVER: the full value is the element's title.
      expect(value!.getAttribute('title')).toBe(full)
      // At 1440, a bounded, clipped, one-line box with an ellipsis.
      // MUTATION: drop any of the four, or the width cap.
      expect(won(value!, 'text-overflow', WIDE)).toBe('ellipsis')
      expect(won(value!, ['overflow', 'overflow-x'], WIDE)).toBe('hidden')
      expect(won(value!, 'white-space', WIDE)).toBe('nowrap')
      expect(won(value!, 'max-width', WIDE) ?? '', 'no width cap').toMatch(/^\d+ch$/)
      // At 390 the table scrolls with the tenant column held (CH-13), and the
      // identity shows WHOLE on its line: a cut identity is a different
      // identity, and a scrolling table has room for it.
      // MUTATION: apply the ellipsis at every width.
      expect(won(value!, 'text-overflow', PHONE), 'the phone record cuts the identity').toBeNull()
    }
    // A tenant with no service account still says so in words.
    expect(visible(row(c, 'smoke').querySelector('td[data-label="Identity"]'))).toBe('no service account')
  })
})

describe('Tenants scrolls at 390 with the tenant held, under its two-row head (CH-13, AH-12)', () => {
  it('holds the tenant column and nothing from the Configured group', async () => {
    const c = await roster()
    // A data table of nine columns: it scrolls sideways, it does not stack.
    const wrap = c.querySelector('table.pools')!.parentElement!
    expect(wrap.classList.contains('is-scroll'), `the wrapper is ${wrap.className}`).toBe(true)
    expect(wrap.classList.contains('is-stacked')).toBe(false)
    // The held column is the name: its head (which spans both head rows) and
    // every row's header cell.
    const head = [...c.querySelectorAll('thead tr:first-child th')].find((th) => visible(th) === 'Tenant')!
    expect(won(head, 'position', PHONE)).toBe('sticky')
    expect(won(row(c, 'eng').querySelector('th')!, 'position', PHONE)).toBe('sticky')
    // THE SECOND HEAD ROW'S FIRST CELL IS `Max active`, a figure's head from
    // the middle of the table, and the held-column rule names cells by
    // `:first-child`. Held, it sat over the Tenant head at every offset.
    // MUTATION: widen the held rule's head branch in the CH-13 block of
    // styles.css from `thead > tr:first-child` back to every head row.
    const maxActive = c.querySelector('thead tr:nth-child(2) th:first-child')!
    expect(visible(maxActive)).toBe('Max active')
    expect(won(maxActive, 'position', PHONE), 'a Configured head is held at the left edge').not.toBe('sticky')
    expect(won(maxActive, 'white-space', PHONE)).toBe('nowrap')
  })
})

describe('Tenants shows the ceiling admission enforces (AH-12)', () => {
  it('prints Enforced, the smaller of the two configured values, for every tenant', async () => {
    const c = await roster()
    const enforced = (id: string) => visible(row(c, id).querySelector('td[data-label="Enforced"]'))
    // Equal, max active smaller, units smaller -- each the minimum.
    expect(enforced('eng')).toBe('20')
    expect(enforced('u-sw-c90291')).toBe('2')
    expect(enforced('smoke')).toBe('4')
  })

  it('groups the registry values under Configured, after Enforced', async () => {
    const c = await roster()
    const top = [...c.querySelectorAll('thead tr:first-child th')]
    const labels = top.map(visible)
    const enforcedAt = labels.indexOf('Enforced')
    const configuredAt = labels.indexOf('Configured')
    expect(enforcedAt, `no Enforced column in ${labels.join(' | ')}`).toBeGreaterThan(-1)
    expect(configuredAt, `no Configured group in ${labels.join(' | ')}`).toBeGreaterThan(enforcedAt)
    const group = top[configuredAt] as HTMLTableCellElement
    expect(group.colSpan, 'the group does not span the two values').toBe(2)
    expect(group.getAttribute('scope')).toBe('colgroup')
    const under = [...c.querySelectorAll('thead tr:nth-child(2) th')].map(visible)
    expect(under).toEqual(['Max active', 'Units'])
  })

  it('explains Enforced through a help link, outside the column head and at every width', async () => {
    const c = await roster()
    const head = [...c.querySelectorAll('thead th')].find((th) => visible(th) === 'Enforced')
    expect(head, 'there is no Enforced column head').toBeTruthy()
    // THE COLUMN HEAD IS ITS LABEL AND NOTHING ELSE. #161 first put a `?` in
    // it: a glyph from the rationed set rather than the help link the owner
    // decided, hidden below 900px, and with its `HelpNote` inside the `<th>`,
    // so a screen reader read the topic's whole short form as part of the
    // column's name on every cell. MUTATION: put the HelpCard back in the head.
    expect(head!.querySelector('button, a, [data-help-description]'), 'the Enforced head carries more than its label').toBeNull()
    expect((head!.textContent ?? '').trim()).toBe('Enforced')

    // THE HELP LINK: a link to the Tenants fields topic that is neither in the
    // head row nor the budget note's `Why →`, which is about the budget.
    const links = [...c.querySelectorAll<HTMLAnchorElement>('a[href="#help/tenant-fields"]')].filter(
      (a) => a.closest('thead') === null && a.closest('.ctl-panel-note') === null,
    )
    expect(links, 'no help link reaches the Tenants fields topic').toHaveLength(1)
    const link = links[0]!
    expect(link.textContent).toBe(HELP['tenant-fields'].title)
    // AT EVERY WIDTH. While this table was stacked below 900px, §B6.3 hid
    // its head row, which is why a link there would have been the CP-11
    // defect again: focusable and invisible. It scrolls now (CH-13), and
    // nothing in the sheet hides this one either way.
    for (const env of [WIDE, PHONE]) {
      for (const el of [link, link.parentElement!]) {
        expect(cascade(STYLES, el, 'display', env).winner?.value ?? null, `hidden at ${env.width}`).not.toBe('none')
      }
    }
    // And it costs no glyph: Tenants draws no `?` at all now.
    expect(c.querySelector('button[aria-label^="Help: "]'), 'Tenants still draws a help glyph').toBeNull()
  })
})

describe('Tenants says there is no budget in the reader’s words (AH-21)', () => {
  it('is one plain line, no schema field and no measurement mark, with the way to the reason', async () => {
    const c = await roster()
    const note = [...c.querySelectorAll('.ctl-panel-note')].find((p) => /budget/.test(p.textContent ?? ''))
    expect(note, 'there is no budget note').toBeTruthy()
    const link = note!.querySelector('a')
    expect(link?.getAttribute('href')).toBe('#help/tenant-fields')
    expect(link?.textContent).toBe('Why →')
    const words = visible(note!).replace('Why →', '').trim()
    expect(words).toBe('no budget column · no budget can be set')
    // A budget is a setting, not a measurement: no `not measured` mark.
    expect(note!.querySelector('.ctl-mark'), 'a setting is drawn as an unmeasured figure').toBeNull()
    // The field name is the API's, not the reader's.
    expect(note!.querySelector('code')).toBeNull()
    expect(c.textContent ?? '').not.toContain('monthly_budget_usd')
    // The accessible name is the same fact and the one clause that makes it
    // true -- not the old account of the 422.
    const label = note!.getAttribute('aria-label') ?? ''
    expect(label).toMatch(/no budget can be set/i)
    expect(label).toContain('left out rather than drawn empty')
    expect(label).not.toContain('monthly_budget_usd')
  })

  it('is drawn in plain ink, not dimmed', async () => {
    // PLAIN INK, as the owner decided it: AG-5 defines a plain fact as no mark
    // and NO DIMMING. `.ctl-panel-note` is `--text-faint`, the tone of a
    // qualifier under a figure; this line is not a qualifier, it is the fact
    // that a column is absent, and #161 shipped it faint.
    // MUTATION: drop the Tenants rule and let `.ctl-panel-note` paint it.
    const c = await roster()
    const note = [...c.querySelectorAll('.ctl-panel-note')].find((p) => /budget/.test(p.textContent ?? ''))
    expect(note, 'there is no budget note').toBeTruthy()
    for (const env of [WIDE, PHONE]) {
      expect(won(note!, 'color', env), `the budget note is dimmed at ${env.width}`).toBe('var(--text)')
    }
  })
})
