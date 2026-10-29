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

import SHEET from '../styles.css?raw'
import ADMIN from '../styles/admin.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { HELP } from '../help'
import type { AdminWorkspaceView, Capacity, Pool, Profile, Tenant } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { painted } from './marks'

// The shell's sheet and the Admin section's own (styles/admin.css), in the
// order the app loads them.
const STYLES = `${SHEET}\n${ADMIN}`

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
  loadCapacity: vi.fn(),
  // Admin › Tenants' New workspace and "Being created" (WK-UI-TENANTS): who
  // the caller is, and the records the job is still building.
  loadProfile: vi.fn(),
  loadAdminWorkspaces: vi.fn(),
  approveWorkspace: vi.fn(),
  denyWorkspace: vi.fn(),
  retryWorkspace: vi.fn(),
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
    principal: 'eng@example.com',
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
    principal: 'swarm-smoke@example.com',
    max_active: 10,
    capacity_units: 4,
    enabled: false,
    credentials: [],
    service_account: null,
  }),
]

/**
 * Each tenant's pool as `/v1/capacity` serves it, its limit the one every
 * writer of the pool sets: the smaller of the two configured values. Enforced
 * prints the pool's figure (G5-13), so this is where the figures come from.
 */
function tenantPool(t: Tenant): Pool {
  const limit = Math.min(t.max_active, t.capacity_units)
  return {
    name: `tenant:${t.tenant_id}`,
    hard_limit: limit,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: limit,
    active: 0,
    available: limit,
    enabled: t.enabled,
    updated_at: '2026-09-24T09:00:00Z',
  }
}

async function roster(): Promise<HTMLElement> {
  api.loadTenants.mockResolvedValue({
    status: 'ok',
    data: { tenants: ROSTER },
    fetchedAt: Date.now(),
  } satisfies Result<{ tenants: Tenant[] }>)
  api.loadCapacity.mockResolvedValue({
    status: 'ok',
    data: { pools: ROSTER.map(tenantPool), runner_profiles: {} } as Capacity,
    fetchedAt: Date.now(),
  } satisfies Result<Capacity>)
  const { container } = render(<TenantsScreen />)
  await screen.findByText('u-sw-c90291')
  // Enforced is read beside the roster (G5-13): every tenant here has its
  // pool, so no cell is still the reading dash once it lands.
  await waitFor(() => expect(container.querySelector('td[data-label="Enforced"] .ctl-em')).toBeNull())
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
      // At 1440, a clipped box that gives way to its column (#503): a flex
      // item allowed to shrink below its text. The ellipsis is on its pieces
      // since G5-12 (qa.g5.tenants.test.tsx), and the domain, its last piece,
      // is no longer cut at all: it wraps on its own line (visual QA pass 2,
      // admin-shell-18). MUTATION: drop any of these, or the shrink.
      expect(won(value!.lastElementChild!, 'text-overflow', WIDE)).toBeNull()
      expect(won(value!.lastElementChild!, 'overflow-wrap', WIDE)).toBe('anywhere')
      expect(won(value!, ['overflow', 'overflow-x'], WIDE)).toBe('hidden')
      expect(won(value!, 'white-space', WIDE)).toBe('nowrap')
      expect(won(value!, 'min-width', WIDE), 'the identity cannot shrink below its text').toBe('0')
      expect(won(value!.parentElement!, 'display', WIDE)).toBe('flex')
      // At 390 the table scrolls with the tenant column held (CH-13), and the
      // identity shows WHOLE on its line: a cut identity is a different
      // identity, and a scrolling table has room for it.
      // MUTATION: apply the ellipsis at every width.
      expect(won(value!.lastElementChild!, 'text-overflow', PHONE), 'the phone record cuts the identity').toBeNull()
    }
    // A tenant with no service account still says so in words.
    expect(visible(row(c, 'smoke').querySelector('td[data-label="Identity"]'))).toBe('no service account')
  })
})

describe('Tenants fits at 1440 and keeps every copy control on screen (#503)', () => {
  it('lays the roster out fixed above 900px, from a colgroup of one col per head', async () => {
    const c = await roster()
    const table = c.querySelector('table')!
    expect(table.classList.contains('ten-table'), `the table is ${table.className}`).toBe(true)
    // One head row, as the Tenants frame draws it: a two-row head was what
    // the held-column rule had to be taught to skip (CH-13).
    expect(c.querySelectorAll('thead tr')).toHaveLength(1)
    const heads = [...c.querySelectorAll('thead th')].map(visible)
    expect(heads).toEqual(['Tenant', 'Status', 'Kind', 'Principal', 'Enforced', 'Configured', 'Credentials', 'Identity'])
    const cols = [...table.querySelectorAll(':scope > colgroup > col')]
    expect(cols, 'the colgroup does not match the head').toHaveLength(heads.length)
    // MUTATION: drop `table-layout: fixed` from `.ten-table` in admin.css.
    expect(won(table, 'table-layout', WIDE)).toBe('fixed')
    expect(won(table, 'width', WIDE)).toBe('100%')
    // Every column has a width, and they add up to the table: a fixed table
    // whose widths came to more than 100% would overflow its panel again.
    const widths = cols.map((col) => won(col, 'width', WIDE) ?? '')
    for (const w of widths) expect(w, 'a column with no width').toMatch(/^\d+(\.\d+)?%$/)
    expect(widths.reduce((n, w) => n + parseFloat(w), 0)).toBe(100)
    // Below 900px it is sized by its content and scrolls (CH-13).
    expect(won(table, 'table-layout', PHONE)).not.toBe('fixed')
    // The rule is in the Admin section's own sheet, not the shell's.
    expect(ADMIN).toMatch(/table\.ten-table\s*\{[^}]*table-layout:\s*fixed/)
  })

  it('draws the copy control outside the span that truncates, so the ellipsis never cuts it', async () => {
    const c = await roster()
    for (const label of ['Principal', 'Identity']) {
      const cell = row(c, 'u-sw-c90291').querySelector(`td[data-label="${label}"]`)!
      const ident = cell.querySelector('.ten-ident')!
      const button = cell.querySelector('button.ten-copy')!
      // MUTATION: put the button inside `.ten-ident`.
      expect(ident.contains(button), `the ${label} copy control is inside the truncating span`).toBe(false)
      expect(button.parentElement).toBe(ident.parentElement)
      // It keeps its width while the identity gives way.
      expect(won(button, ['flex', 'flex-shrink'], WIDE)).toMatch(/^(none|0)$/)
    }
  })

  it('holds the tenant column at 390', async () => {
    const c = await roster()
    const wrap = c.querySelector('table.pools')!.parentElement!
    expect(wrap.classList.contains('is-scroll'), `the wrapper is ${wrap.className}`).toBe(true)
    expect(wrap.classList.contains('is-stacked')).toBe(false)
    const head = [...c.querySelectorAll('thead th')].find((th) => visible(th) === 'Tenant')!
    expect(won(head, 'position', PHONE)).toBe('sticky')
    expect(won(row(c, 'eng').querySelector('th')!, 'position', PHONE)).toBe('sticky')
  })
})

describe('Tenants shows the ceiling admission enforces (AH-12)', () => {
  it('prints Enforced, the tenant pool’s served limit, for every tenant', async () => {
    const c = await roster()
    const enforced = (id: string) => visible(row(c, id).querySelector('td[data-label="Enforced"]'))
    // Equal, max active smaller, units smaller -- each the minimum.
    expect(enforced('eng')).toBe('20')
    expect(enforced('u-sw-c90291')).toBe('2')
    expect(enforced('smoke')).toBe('4')
  })

  it('puts the registry values in Configured, after Enforced, max active then units', async () => {
    const c = await roster()
    const labels = [...c.querySelectorAll('thead th')].map(visible)
    const enforcedAt = labels.indexOf('Enforced')
    const configuredAt = labels.indexOf('Configured')
    expect(enforcedAt, `no Enforced column in ${labels.join(' | ')}`).toBeGreaterThan(-1)
    expect(configuredAt, `no Configured column in ${labels.join(' | ')}`).toBe(enforcedAt + 1)
    const cell = row(c, 'u-sw-c90291').querySelector('td[data-label="Configured"]')!
    expect(visible(cell)).toBe('2 · 4u')
    // Both values by name, for a reader who does not know the short form.
    expect(cell.querySelector('span')!.getAttribute('aria-label')).toBe('max active 2, capacity units 4')
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
    expect(link.textContent).toBe(HELP['tenant-fields'].subject)
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
    // A SENTENCE, IN THE BODY FONT (visual QA pass 1, admin-shell-18): the
    // mono `no budget column · no budget can be set` read as debug output.
    // MUTATION: restore the mono fragment, or drop `font-family` from
    // `.ctl-panel-note.ten-budget`.
    expect(words).toBe('Tenants have no budget column: no budget can be set here.')
    expect(painted(note!, 'font-family', WIDE)).toBe('var(--font)')
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

// ---------------------------------------------------------------------------
// #134: the owner's decisions on the roster, 2026-09-29. Each block was
// written before the change it demands.
// ---------------------------------------------------------------------------

describe('Tenants counts what a reader scans the roster for (#134)', () => {
  it('says N tenants · N with no tenant key · N disabled', async () => {
    const c = await roster()
    // The count is the note over the first card since #138 (owner ruling
    // 2026-10-07), not the line under the title, and it carries no age: the
    // read's age is on the head's refresh control. So it is exactly the count.
    const summary = visible(c.querySelector('p.c-count-note'))
    // ROSTER: three tenants, two with no credential registered, one disabled.
    expect(summary).toBe('3 tenants · 2 with no tenant key · 1 disabled')
  })

  it('never says a tenant cannot run: a lent account can still run it', async () => {
    const c = await roster()
    expect(c.textContent ?? '').not.toMatch(/cannot run|can't run|can’t run/i)
  })
})

describe('Tenants copies a whole identity and says whether it landed (#134)', () => {
  function clipboard(write: (text: string) => Promise<void>): void {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: write } })
  }

  it('puts a copy control beside the principal and the service account, copying the full value', async () => {
    const writes: string[] = []
    clipboard(async (text) => {
      writes.push(text)
    })
    const c = await roster()
    const r = row(c, 'u-sw-c90291')
    for (const [label, full, noun] of [
      ['Principal', LONG_PRINCIPAL, 'principal'],
      ['Identity', LONG_SA, 'service account'],
    ] as const) {
      const cell = r.querySelector(`td[data-label="${label}"]`)!
      const button = cell.querySelector('button.ten-copy')
      expect(button, `no copy control beside the ${label}`).not.toBeNull()
      expect(button!.getAttribute('aria-label')).toBe(`Copy ${noun} ${full}`)
      // Beside the shortened value, not in place of it.
      expect(cell.querySelector('.ten-ident')!.textContent).toBe(full)
      await act(async () => {
        ;(button as HTMLButtonElement).click()
      })
      expect(writes.at(-1)).toBe(full)
      expect(visible(cell.querySelector('[role="status"]'))).toBe(`${noun} copied`)
    }
    // No identity, nothing to copy.
    expect(row(c, 'smoke').querySelector('td[data-label="Identity"] button')).toBeNull()
  })

  it('says so when the browser refuses the copy', async () => {
    clipboard(() => Promise.reject(new Error('denied')))
    const c = await roster()
    const cell = row(c, 'eng').querySelector('td[data-label="Principal"]')!
    await act(async () => {
      cell.querySelector<HTMLButtonElement>('button.ten-copy')!.click()
    })
    expect(visible(cell.querySelector('[role="status"]'))).toMatch(/^copy refused/)
  })

  it('clears what it said after a few seconds, so old outcomes do not pile up', async () => {
    clipboard(async () => {})
    const c = await roster()
    const cell = row(c, 'eng').querySelector('td[data-label="Principal"]')!
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      await act(async () => {
        cell.querySelector<HTMLButtonElement>('button.ten-copy')!.click()
      })
      expect(visible(cell.querySelector('[role="status"]'))).toBe('principal copied')
      act(() => {
        vi.advanceTimersByTime(5000)
      })
      expect(visible(cell.querySelector('[role="status"]'))).toBe('')
    } finally {
      vi.useRealTimers()
    }
  })

  it('makes the copy control a 44px target at phone width', async () => {
    const c = await roster()
    const button = row(c, 'eng').querySelector('td[data-label="Principal"] button.ten-copy')!
    expect(won(button, 'min-height', PHONE)).toBe('44px')
    expect(won(button, 'min-width', PHONE)).toBe('44px')
  })
})

describe('Tenants links each Enforced figure to its pool on Pool limits (#134)', () => {
  it('points at the tenant pool’s row by the id Pool limits gives it', async () => {
    const c = await roster()
    for (const [id, figure] of [
      ['eng', '20'],
      ['u-sw-c90291', '2'],
      ['smoke', '4'],
    ] as const) {
      const link = row(c, id).querySelector<HTMLAnchorElement>('td[data-label="Enforced"] a')
      expect(link, `${id}: the Enforced figure is not a link`).not.toBeNull()
      // A path, not the legacy hash (QA2 admin2-21, 2026-10-29).
      expect(link!.getAttribute('href')).toBe(`/admin/limits?pool=${encodeURIComponent(`tenant:${id}`)}`)
      expect(link!.textContent).toBe(figure)
      expect(link!.getAttribute('title')).toMatch(/^\d+ agents at once configured · /)
      expect(link!.getAttribute('title')).toContain(`${figure} units enforced`)
      expect(link!.className).toContain('ctl-link')
    }
  })
})

describe('Tenants draws status and credentials in the brand marks (admin-help.html, 2026-10-01)', () => {
  it('draws a disabled tenant with the parked mark and an enabled one as the plain word', async () => {
    const c = await roster()
    const smoke = row(c, 'smoke').querySelector('td[data-label="Status"]')!
    const mark = smoke.querySelector('[data-mark]')
    // Held on purpose, not failed: the violet pause bars, never the red diamond.
    expect(mark?.getAttribute('data-mark')).toBe('parked')
    expect(mark?.getAttribute('data-hue')).toBe('park')
    expect(visible(smoke)).toBe('disabled')
    const eng = row(c, 'eng').querySelector('td[data-label="Status"]')!
    expect(visible(eng)).toBe('enabled')
    expect(eng.querySelector('svg'), 'the normal case carries a glyph').toBeNull()
  })

  it('warns, in amber, on a tenant with no key of its own, and lists the keys of one that has them', async () => {
    const c = await roster()
    const none = row(c, 'smoke').querySelector('td[data-label="Credentials"]')!
    expect(none.querySelector('[data-mark="warn"]')).not.toBeNull()
    expect(visible(none)).toBe('none registered')
    // A warning, never a failure: a lent account can still run its work.
    expect(none.querySelector('[data-hue="bad"]')).toBeNull()
    const keys = row(c, 'eng').querySelector('td[data-label="Credentials"]')!
    expect(keys.querySelector('[data-mark="warn"]')).toBeNull()
    expect(keys.querySelectorAll('.tags > .tag')).toHaveLength(2)
  })
})

describe('D16: a head cut at a narrow width keeps its name', () => {
  it('gives every Tenants head its whole label as a title', async () => {
    const c = await roster()
    const heads = [...c.querySelectorAll('table.ten-table thead th')]
    expect(heads.length).toBeGreaterThan(0)
    for (const th of heads) expect(th.getAttribute('title'), th.textContent!).toBe(th.textContent!.trim())
  })
})

// ADMIN › TENANTS › NEW WORKSPACE AND "BEING CREATED" (#847, lane
// WK-UI-TENANTS; docs/design/workspace-kinds.md §4.2.4, §4.8, Q2 (a)).
//
//   * the New workspace button is an admin's: a non-admin sees none, and the
//     list of records is not even read for them;
//   * "Being created" lists every record that is not `ready`, each with its
//     kind and `w-` id, and no email -- not the person's, the group's or the
//     service account's (the principal stays off this page);
//   * Approve on a SERVICE row is the platform owner's alone (Q2 (a)): an
//     admin who is not the owner sees none there, and still sees it on a
//     group row;
//   * a `needs_owner` row shows the step the guard stopped at, and the API's
//     code and copy word for word.
//
// Every address below is a fixture; nothing here is token-shaped.

function profileOf(role: 'owner' | 'admin' | null): Profile {
  return {
    identity: {
      email: 'someone@example.com',
      display_name: null,
      display_name_suggested: null,
      domain: 'example.com',
      platform_role: role,
      is_admin: role !== null,
      is_admin_unresolved: false,
    },
    workspaces: [],
    revision: 1,
    unavailable: [],
  } as unknown as Profile
}

function ws(over: Partial<AdminWorkspaceView>): AdminWorkspaceView {
  return {
    state: 'applying',
    kind: 'group',
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    workspace_id: 'w-a1b2c3',
    display_name: 'Research',
    principal: 'research@example.com',
    requested_at: '2026-10-19T08:00:00Z',
    decision: null,
    held: null,
    limits: { max_active: 8, capacity_units: 8 },
    steps: { A1: { state: 'done', at: '2026-10-19T08:01:00Z', code: null } },
    failure: null,
    ...over,
  }
}

const STOPPED_COPY = 'The call guard refused a call the job tried to make. The platform owner reads it in the restricted log.'

const RECORDS: AdminWorkspaceView[] = [
  ws({}),
  ws({ state: 'requested', workspace_id: 'w-group1', display_name: 'Platform team', principal: 'platform@example.com', steps: {} }),
  ws({
    state: 'requested',
    kind: 'service',
    workspace_id: 'w-svc001',
    display_name: 'Reports bot',
    principal: 'reports-bot@example-project.iam.gserviceaccount.com',
    steps: {},
  }),
  ws({
    state: 'needs_owner',
    kind: 'personal',
    workspace_id: 'w-pers01',
    display_name: 'Ada',
    principal: 'ada@example.com',
    needs_owner_step: 'A7',
    steps: { A1: { state: 'done' }, A7: { state: 'held', code: 'GUARD_REFUSED' } },
    failure: { code: 'GUARD_REFUSED', step: 'A7', retryable: false, at: '2026-10-19T08:05:00Z', copy: STOPPED_COPY },
  }),
  ws({ state: 'ready', workspace_id: 'w-done01', display_name: 'Done already', principal: 'done@example.com' }),
]

async function tenantsAs(role: 'owner' | 'admin' | null): Promise<HTMLElement> {
  api.loadProfile.mockResolvedValue({ status: 'ok', data: profileOf(role), fetchedAt: Date.now() } satisfies Result<Profile>)
  api.loadAdminWorkspaces.mockResolvedValue({ status: 'ok', data: RECORDS, fetchedAt: Date.now() } satisfies Result<AdminWorkspaceView[]>)
  return roster()
}

async function beingCreated(c: HTMLElement): Promise<HTMLElement> {
  await waitFor(() => expect(c.querySelector('[data-testid="being-created"] [data-workspace-id]')).not.toBeNull())
  return c.querySelector('[data-testid="being-created"]') as HTMLElement
}

function wsRow(section: HTMLElement, id: string): HTMLElement {
  const r = section.querySelector(`[data-workspace-id="${id}"]`)
  expect(r, `no "Being created" row for ${id}`).toBeTruthy()
  return r as HTMLElement
}

describe('Tenants: New workspace is an admin’s button (WK-UI-TENANTS)', () => {
  it('draws no New workspace button and reads no records for a non-admin', async () => {
    const c = await tenantsAs(null)
    await waitFor(() => expect(api.loadProfile).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByRole('button', { name: 'New workspace' })).toBeNull()
    expect(c.querySelector('[data-testid="being-created"]')).toBeNull()
    expect(api.loadAdminWorkspaces).not.toHaveBeenCalled()
  })

  it('draws it for an admin, and it opens the New workspace dialog', async () => {
    await tenantsAs('admin')
    const button = await screen.findByRole('button', { name: 'New workspace' })
    fireEvent.click(button)
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByRole('tab', { name: 'Service' })).toBeTruthy()
  })
})

// THE HEADING AND ITS BUTTON ARE ONE ROW (#1678). Both sat in `.ob-acts`,
// onboarding's actions row (inline-flex, packed right, no `align-items`), and
// the `h2` kept the browser's top margin, so New workspace was drawn about
// 18px above "Being created" and beside it. `painted` reads every shipped
// sheet in the bundle's order, phone rules included.
describe('Tenants draws Being created and New workspace on one line (#1678)', () => {
  async function header(): Promise<{ section: HTMLElement; h2: HTMLElement; button: HTMLElement; row: HTMLElement }> {
    const section = await beingCreated(await tenantsAs('admin'))
    const h2 = within(section).getByRole('heading', { name: 'Being created' })
    const button = within(section).getByRole('button', { name: 'New workspace' })
    return { section, h2, button, row: h2.parentElement as HTMLElement }
  }

  it('puts the heading and the button in one header row of the section, outside onboarding’s actions row', async () => {
    const { section, h2, button, row } = await header()
    // MUTATION: wrap the button in its own element and this is false.
    expect(button.parentElement).toBe(row)
    // MUTATION: nest the row inside another wrapper and this is false.
    expect(row.parentElement).toBe(section)
    // MUTATION: put `ob-acts` back on the row and this finds it.
    expect(h2.closest('.ob-acts')).toBeNull()
  })

  it('lays the row out across the section, centred, with the heading at no top margin', async () => {
    const { h2, row } = await header()
    // MUTATION: leave the row as `.ob-acts` (inline-flex) and this reads inline-flex.
    expect(painted(row, 'display', WIDE)).toBe('flex')
    // MUTATION: drop `align-items` and the button rides to the row's top again.
    expect(painted(row, 'align-items', WIDE)).toBe('center')
    // MUTATION: `flex-end` packs both to the right, the `.ob-acts` defect.
    expect(painted(row, 'justify-content', WIDE)).toBe('space-between')
    // MUTATION: drop the h2's `margin: 0` and the browser's ~18px returns
    // (the resolver reads that as no rule at all: null).
    expect(painted(h2, ['margin-top', 'margin'], WIDE)?.split(/\s+/)[0]).toMatch(/^0(px)?$/)
    // MUTATION: drop the row's bottom margin and the list sits on the heading.
    // The space is the one a sibling section's heading gets from `.section > h2`.
    const sibling = document.createElement('section')
    sibling.className = 'section'
    const siblingH2 = sibling.appendChild(document.createElement('h2'))
    document.body.appendChild(sibling)
    try {
      const below = (el: Element) => painted(el, ['margin-bottom', 'margin'], WIDE)?.split(/\s+/).at(-1)
      expect(below(siblingH2)).toBe('10px')
      expect(below(row)).toBe(below(siblingH2))
    } finally {
      sibling.remove()
    }
  })

  it('wraps the button under the heading at phone width, full width and a 44px target', async () => {
    const { button, row } = await header()
    // MUTATION: drop `flex-wrap: wrap` and a long heading squeezes the button.
    expect(painted(row, 'flex-wrap', PHONE)).toBe('wrap')
    // MUTATION: drop the phone rule's min-height and the button is a 30px target.
    expect(painted(button, 'min-height', PHONE)).toBe('44px')
    // MUTATION: drop the phone rule's basis and `.c-btn`'s `flex: none` keeps it word-wide.
    expect(painted(button, ['flex', 'flex-basis'], PHONE)).toMatch(/(^|\s)100%$/)
  })
})

describe('Tenants lists the workspaces being created (WK-UI-TENANTS, §4.2.4)', () => {
  it('lists every record that is not ready, each with its kind and w- id', async () => {
    const section = await beingCreated(await tenantsAs('admin'))
    const ids = [...section.querySelectorAll('[data-workspace-id]')].map((r) => r.getAttribute('data-workspace-id'))
    expect(ids).toEqual(['w-a1b2c3', 'w-group1', 'w-svc001', 'w-pers01'])
    expect(section.querySelector('[data-workspace-id="w-done01"]')).toBeNull()
    expect(visible(wsRow(section, 'w-svc001'))).toContain('service')
    expect(visible(wsRow(section, 'w-svc001'))).toContain('w-svc001')
    expect(visible(wsRow(section, 'w-pers01'))).toContain('personal')
    expect(visible(wsRow(section, 'w-a1b2c3'))).toContain('group')
  })

  it('shows no email: not the person’s, the group’s or the service account’s', async () => {
    const section = await beingCreated(await tenantsAs('owner'))
    expect(section.textContent).not.toContain('@')
    for (const el of section.querySelectorAll('*')) {
      for (const a of el.attributes) expect(a.value, `${el.tagName} ${a.name}`).not.toContain('@')
    }
  })

  it('draws each row’s live steps', async () => {
    const section = await beingCreated(await tenantsAs('admin'))
    const steps = wsRow(section, 'w-a1b2c3').querySelector('[aria-label="Workspace job steps"]')
    expect(steps).toBeTruthy()
    expect(steps!.querySelector('[data-step="A1"]')!.getAttribute('data-state')).toBe('done')
  })
})

describe('Tenants: only the platform owner approves a service workspace (Q2 (a))', () => {
  it('draws no Approve on a service row for an admin who is not the owner, and draws it on a group row', async () => {
    const section = await beingCreated(await tenantsAs('admin'))
    expect(within(wsRow(section, 'w-svc001')).queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(visible(wsRow(section, 'w-svc001'))).toContain('Waiting for the platform owner')
    expect(within(wsRow(section, 'w-group1')).getByRole('button', { name: 'Approve' })).toBeTruthy()
  })

  it('draws Approve on a service row for the owner, and it approves by w- id', async () => {
    api.approveWorkspace.mockResolvedValue({ status: 'ok', data: { workspace: RECORDS[2]! }, fetchedAt: Date.now() })
    const section = await beingCreated(await tenantsAs('owner'))
    fireEvent.click(within(wsRow(section, 'w-svc001')).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(api.approveWorkspace).toHaveBeenCalledWith('w-svc001'))
  })
})

describe('Tenants shows the step a stopped workspace is held at (§4.8)', () => {
  it('names the step, and the API’s code and copy word for word, on a needs_owner row', async () => {
    const section = await beingCreated(await tenantsAs('admin'))
    const text = visible(wsRow(section, 'w-pers01'))
    expect(text).toContain('Stopped at A7')
    expect(text).toContain('GUARD_REFUSED')
    expect(text).toContain(STOPPED_COPY)
  })
})
