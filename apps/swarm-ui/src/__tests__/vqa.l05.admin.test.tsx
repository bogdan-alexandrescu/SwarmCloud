// VISUAL QA LANE VQA-L05 (part of #1038): Admin › Pool limits, Tenants and
// Platform counts. One case per finding, each asserting the property the
// finding measured as wrong -- a winning declaration, a placement, a drawn
// word -- rather than the markup around it.
//
//   V022  the fallback token card touched an error/empty block above it
//   V023  its two text fields were the ceiling input's 96px
//   V024  opening the editor narrowed the families and reflowed every row
//   V025  on a phone the editor was drawn after all six families
//   V065  faint text on the edited row's tint was 4.33:1 in dark
//   V076  `3h / ago`; a pool id broke at a hyphen on cramped lines
//   V077  59 pools and no filter above 560px
//   V078  a failed admin read drew the same `—` as "nobody changed it"
//   V026, V075  covered in qa.g5.tenants.test.tsx (the identity's cut)
//   V079  Tenants' column split, and an underlined dash with no pool behind it
//   V064  Tenants' half: its admin gate keeps the same head as its loaded
//         roster (title, no refresh to renew a refusal -- #138); People's
//         refresh (L6) and the head's reserved actions slot (L14) are not here
//   V029  a first run blanked the page until the answer came
//   V084  the two count cards scaled their bars separately

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

const { AdminSettingsScreen, FallbackTokenCard } = await import('../AdminSettings')
const { PlatformCountsScreen, forgetLastRun } = await import('../PlatformCounts')
const { TenantsScreen } = await import('../Activity')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function won(el: Element, prop: string | readonly string[], env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, prop, env)
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T10:00:00Z' }
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
    updated_at: '2026-10-11T10:00:00Z',
    ...over,
  }
}

function capacity(pools: Pool[]): Capacity {
  return { pools, runner_profiles: {}, tenant_id: 'eng', generated_at: '2026-10-11T10:00:00Z' } as Capacity
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

function edit(name: string): HTMLElement {
  const row = document.getElementById(`limit-${name}`)!
  fireEvent.click(within(row).getByRole('button', { name: `Edit ceiling for ${name}` }))
  const side = document.querySelector<HTMLElement>('aside.adm-side')
  expect(side, 'edit opened no editor').not.toBeNull()
  return side!
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
// Pool limits
// ---------------------------------------------------------------------------

describe('V022: the fallback token card is spaced off whatever is above it', () => {
  it('carries the section gap on its own top, so an error block above it does not touch it', async () => {
    api.loadCapacity.mockResolvedValue({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 503, code: 'unavailable', message: 'down' },
    })
    render(<AdminSettingsScreen />)
    const failed = await waitFor(() => {
      const s = document.querySelector('.state.failed')
      expect(s).not.toBeNull()
      return s!
    }, WAIT)
    const card = document.querySelector('.adm-fallback-token')!
    expect(failed.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // MUTATION: drop `.adm-fallback-token { margin-top }`: the block's own
    // margin is 0 and the card touches it.
    expect(won(card, ['margin-top', 'margin-block-start', 'margin'], WIDE)).toBe('var(--ctl-s5)')
    expect(won(failed, ['margin-bottom', 'margin'], WIDE)).toBeNull()
  })
})

describe('V023: the fallback token fields are sized for what is typed into them', () => {
  it('draws its own form row, not the ceiling editor’s 96px field', () => {
    render(<FallbackTokenCard />)
    const owner = screen.getByRole('textbox', { name: 'Owner' })
    const token = screen.getByLabelText('Personal access token')
    const form = owner.closest('form')!
    expect(form.classList.contains('adm-side-field')).toBe(false)
    for (const field of [owner, token]) {
      // MUTATION: put the form back on `.adm-side-field`: width 96px wins.
      expect(won(field, 'width', WIDE)).toBeNull()
      expect(won(field, 'min-width', WIDE)).toBe('24ch')
      expect(won(field, ['flex', 'flex-grow'], WIDE)).toBe('1 1 24ch')
      // A phone gives each its own full line.
      expect(won(field, ['flex-basis'], PHONE)).toBe('100%')
    }
  })
})

describe('V024: opening the editor at 1440 reflows no row', () => {
  it('keeps the families in the one grid column and floats the editor over their right', async () => {
    await limits([pool('global', { hard_limit: 40, effective_limit: 40 }), pool('tenant:eng')])
    const split = document.querySelector('.adm-split')!
    const side = edit('tenant:eng')
    expect(split.classList.contains('is-editing')).toBe(true)
    const families = split.querySelector(':scope > .cap-families')!
    expect(side.parentElement).toBe(split)
    // MUTATION: give the editor a column of its own again
    // (`grid-template-columns: minmax(0, 1fr) minmax(280px, 340px)`).
    expect(won(split, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr)')
    expect(won(families, 'grid-area', WIDE)).toBe('1 / 1')
    expect(won(side, 'grid-area', WIDE)).toBe('1 / 1')
    expect(won(side, 'justify-self', WIDE)).toBe('end')
    expect(won(side, 'position', WIDE)).toBe('sticky')
    // `AIMD back-off` never breaks at its hyphen.
    const by = document.getElementById('limit-tenant:eng')!.querySelector('td[data-label="Set by"]')!
    expect(won(by, 'white-space', WIDE)).toBe('nowrap')
  })
})

describe('V025: below 1280px the editor is drawn under the row it edits', () => {
  it('opens on a phone directly after the first record, not after all six families', async () => {
    viewport(390)
    await limits([
      pool('global', { hard_limit: 40, effective_limit: 40 }),
      pool('tenant:eng'),
      pool('tenant:research'),
      pool('runner:claude-code'),
      pool('provider:anthropic'),
    ])
    const side = edit('tenant:eng')
    const family = side.closest('.adm-family')!
    // MUTATION: draw the editor after `.cap-families` again.
    expect(family, 'the editor is not inside the family of the row it edits').not.toBeNull()
    expect(family.querySelector('#limit-tenant\\:eng')).not.toBeNull()
    const tables = [...family.querySelectorAll('table.adm-limits')]
    expect(tables).toHaveLength(2)
    // The edited row ends the first table; the editor follows it; the rest continue.
    const first = [...tables[0]!.querySelectorAll('tbody tr')].map((r) => r.id)
    expect(first[first.length - 1]).toBe('limit-tenant:eng')
    expect(tables[0]!.compareDocumentPosition(side) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(side.compareDocumentPosition(tables[1]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect([...tables[1]!.querySelectorAll('tbody tr')].map((r) => r.id)).toEqual(['limit-tenant:research'])
    // Nothing is drawn beside the families at this width.
    expect(document.querySelector('.adm-split > aside.adm-side')).toBeNull()
  })

  it('keeps it beside the families from 1280px', async () => {
    viewport(1440)
    await limits([pool('global'), pool('tenant:eng')])
    const side = edit('tenant:eng')
    expect(side.parentElement?.classList.contains('adm-split')).toBe(true)
    expect(document.querySelectorAll('.adm-family table.adm-limits')).toHaveLength(2)
  })
})

describe('V065: dim text on the edited row keeps AA in the dark theme', () => {
  it('maps the faint step to the dim one on the row being edited, and only there', async () => {
    await limits([pool('global'), pool('tenant:eng')])
    edit('tenant:eng')
    const row = document.getElementById('limit-tenant:eng')!
    const other = document.getElementById('limit-global')!
    // MUTATION: drop the override: `--text-faint` on `--sk-acs` is 4.33:1.
    for (const theme of ['dark', 'light'] as const) {
      expect(won(row, '--text-faint', { width: 1440, theme })).toBe('var(--text-dim)')
    }
    expect(won(other, '--text-faint', WIDE)).not.toBe('var(--text-dim)')
    // The sub-label the finding measured is painted in the token so remapped.
    expect(won(row.querySelector('th .ctl-sub')!, 'color', WIDE)).toBe('var(--text-faint)')
  })
})

describe('V076: Last changed and the pool id wrap only where they should', () => {
  it('holds `3h ago` together, each id segment whole, and gives a wrapped id its leading', async () => {
    api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
    const at = new Date(Date.now() - 3 * 3_600_000).toISOString()
    await limits([
      pool('provider:anthropic:tenant:u-bogdan', {
        admin_changed_by: 'ops@saga.xyz',
        admin_changed_at: at,
      } as Partial<Pool>),
    ])
    const row = document.getElementById('limit-provider:anthropic:tenant:u-bogdan')!
    const time = row.querySelector('td[data-label="Last changed"] time')!
    expect(time.textContent).toBe('3h ago')
    expect(won(time, 'white-space', WIDE)).toBe('nowrap')
    const sub = row.querySelector('th .ctl-sub')!
    const segs = [...sub.querySelectorAll('.adm-key-seg')]
    expect(segs.map((s) => s.textContent)).toEqual(['provider:', 'anthropic:', 'tenant:', 'u-bogdan'])
    expect(sub.textContent).toBe('provider:anthropic:tenant:u-bogdan')
    // MUTATION: drop the nowrap: `u- / bogdan` is a break again.
    for (const s of segs) expect(won(s, 'white-space', WIDE)).toBe('nowrap')
    expect(won(sub, 'line-height', WIDE)).toBe('var(--lh-micro)')
  })
})

describe('V077: a long pool list gets the filter at every width, and the columns fit what they hold', () => {
  const many = () => Array.from({ length: 21 }, (_, i) => pool(`tenant:t${String(i).padStart(2, '0')}`))

  it('draws the filter on a desktop once there are more pools than a screen holds', async () => {
    viewport(1440)
    await limits(many())
    const box = screen.getByRole('searchbox', { name: /filter pools/i })
    fireEvent.change(box, { target: { value: 't07' } })
    expect([...document.querySelectorAll('tbody tr[id]')].map((r) => r.id)).toEqual(['limit-tenant:t07'])
  })

  it('draws none for a short list on a desktop', async () => {
    viewport(1440)
    await limits(many().slice(0, 20))
    expect(screen.queryByRole('searchbox', { name: /filter pools/i })).toBeNull()
  })

  it('takes the Ceiling column down to its slot and gives Last changed the room', async () => {
    await limits([pool('global')])
    const width = (key: string) => won(document.querySelector(`col.adm-col-${key}`)!, 'width', WIDE)
    expect(width('pool')).toBe('25%')
    expect(width('use')).toBe('15%')
    expect(width('ceiling')).toBe('21%')
    expect(width('by')).toBe('13%')
    expect(width('changed')).toBe('26%')
  })
})

describe('V078: the three kinds of no record look different', () => {
  const lastChanged = () => document.getElementById('limit-global')!.querySelector('td[data-label="Last changed"]')!

  it('draws `not read` where the admin pool read failed', async () => {
    api.loadAdminPools.mockResolvedValue({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 503, code: 'unavailable', message: 'down' },
    })
    await limits([pool('global')])
    // MUTATION: draw the dash for every case again.
    expect(lastChanged().textContent).toBe('not read')
    expect(lastChanged().querySelector('.ctl-mark.is-unread')).not.toBeNull()
    const side = edit('global')
    expect([...side.querySelectorAll('.adm-not-recorded')].map((m) => m.textContent)).toEqual(['not read', 'not read'])
  })

  it('draws `admin only` where the read is refused, and the dash only where nobody changed it', async () => {
    api.loadAdminPools.mockResolvedValue({
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: 'admin_required', message: 'admins only' },
    })
    await limits([pool('global')])
    expect(lastChanged().textContent).toBe('admin only')
    expect(lastChanged().querySelector('.ctl-mark.is-admin')).not.toBeNull()
  })

  it('keeps the dash for a served read with no record', async () => {
    api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
    await limits([pool('global')])
    expect(lastChanged().textContent).toBe('—')
    expect(lastChanged().querySelector('.ctl-mark')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Tenants
// ---------------------------------------------------------------------------

function tenant(over: Partial<Tenant>): Tenant {
  return {
    tenant_id: 'eng',
    kind: 'group',
    principal: 'eng@saga.xyz',
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 45,
    capacity_units: 40,
    monthly_budget_usd: null,
    enabled: true,
    credentials: ['anthropic', 'openai'],
    service_account: 'swarm-agent-worker-eng@saga-agents.iam.gserviceaccount.com',
    gcs_prefix: null,
    namespace: null,
    ...over,
  }
}

describe('V079: Tenants gives the id and the credentials their room, and links only to a pool that exists', () => {
  it('widens Tenant and Credentials, breaks an id at its hyphens only, and draws no link to a missing pool', async () => {
    api.loadTenants.mockResolvedValue(ok({ tenants: [tenant({}), tenant({ tenant_id: 'u-a-rather-long-tenant-id' })] }))
    api.loadCapacity.mockResolvedValue(ok(capacity([pool('tenant:eng', { effective_limit: 30, hard_limit: 40 })])))
    const { container } = render(<TenantsScreen />)
    await screen.findByText('u-a-rather-long-tenant-id', undefined, WAIT)
    const width = (c: string) => won(container.querySelector(`col.ten-col-${c}`)!, 'width', WIDE)
    // MUTATION: put Tenant back to 11% or Credentials to 14%.
    expect(width('tenant')).toBe('15%')
    expect(width('credentials')).toBe('20%')
    const rowOf = (id: string) =>
      [...container.querySelectorAll('tbody th[scope="row"]')].find((h) => h.textContent === id)!.closest('tr')!
    expect(won(rowOf('u-a-rather-long-tenant-id').querySelector('th')!, 'overflow-wrap', WIDE)).toBe('break-word')
    await waitFor(() => expect(rowOf('eng').querySelector('td[data-label="Enforced"]')!.textContent).toBe('30'), WAIT)
    // The pool exists: the figure links to its row.
    expect(rowOf('eng').querySelector('td[data-label="Enforced"] a')).not.toBeNull()
    // No pool: a plain dash with its reason, not an underlined link.
    const none = rowOf('u-a-rather-long-tenant-id').querySelector('td[data-label="Enforced"]')!
    expect(none.querySelector('a')).toBeNull()
    expect(none.querySelector('.ctl-em')?.getAttribute('title')).toBe('no tenant:u-a-rather-long-tenant-id pool in /v1/capacity')
  })
})

describe('V064 (Tenants half): the admin gate keeps the roster page head', () => {
  it('draws the gate under the same head, with no refresh offered to renew a refusal', async () => {
    api.loadTenants.mockResolvedValue({
      status: 'error',
      error: { kind: 'admin_required', httpStatus: 403, code: 'forbidden', message: 'Admin group membership is required.' },
    } satisfies Result<{ tenants: Tenant[] }>)
    api.loadCapacity.mockResolvedValue(ok(capacity([])))
    const { container } = render(<TenantsScreen />)
    const gate = await screen.findByRole('status', undefined, WAIT)
    expect(gate.classList.contains('admin-gate')).toBe(true)
    // One head, the same element the loaded roster draws its title in.
    expect(container.querySelectorAll('.c-phead')).toHaveLength(1)
    expect(container.querySelector('.c-phead h1')?.textContent).toContain('Tenants')
    // MUTATION: draw a refresh on the gate: a 403 is not a read to renew
    // (Shell.tsx `Screen`, #138), so Tenants offers none -- the rule People's
    // gate (L6) is to be brought to, not the other way round.
    expect(container.querySelector('.c-phead .c-refresh')).toBeNull()
    expect(within(gate).queryByRole('button')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Platform counts
// ---------------------------------------------------------------------------

function stats(tenantCounts: Record<string, number>, platform: Record<string, number>): Stats {
  return {
    tenant_id: 'eng',
    tasks_by_state: tenantCounts,
    platform_tasks_by_state: platform,
    dispatch_paused: false,
    limits: {},
    generated_at: '2026-10-11T10:00:00Z',
  } as Stats
}

describe('V029: a first run keeps the page’s shape until the answer lands', () => {
  it('keeps both cards, marked busy, while the count is in flight', async () => {
    let answer: (r: Result<Stats>) => void = () => {}
    api.loadStats.mockReturnValue(new Promise<Result<Stats>>((r) => (answer = r)))
    api.loadMe.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'x' } })
    render(<PlatformCountsScreen />)
    expect(document.querySelectorAll('.counts-notrun')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    // MUTATION: hide the placeholders while busy again (`run === null && !busy`).
    await waitFor(() => expect(document.querySelector('.counts-scopes')?.getAttribute('aria-busy')).toBe('true'))
    expect(document.querySelectorAll('.counts-notrun')).toHaveLength(2)
    answer(ok(stats({ READY: 1 }, { READY: 2 })))
    await waitFor(() => expect(document.querySelectorAll('.counts-notrun')).toHaveLength(0))
    expect(document.querySelectorAll('.counts-card')).toHaveLength(2)
  })
})

describe('V084: both count cards draw their bars on one scale', () => {
  it('scales the tenant’s bars to the largest count on the screen, not its own', async () => {
    api.loadStats.mockResolvedValue(ok(stats({ READY: 7 }, { READY: 185, PARKED: 37 })))
    api.loadMe.mockResolvedValue({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'x' } })
    render(<PlatformCountsScreen />)
    fireEvent.click(screen.getByRole('button', { name: /^Run the count · / }))
    await waitFor(() => expect(document.querySelectorAll('.counts-card')).toHaveLength(2))
    const bar = (card: number, state: string) => {
      const row = [...document.querySelectorAll('.counts-card')[card]!.querySelectorAll('.split-row')].find(
        (r) => r.querySelector('.sr-name')?.textContent === state,
      )!
      return (row.querySelector('.sr-bar > i') as HTMLElement).style.width
    }
    // MUTATION: scale each card to its own max: the tenant's 7 is 100%.
    expect(bar(0, 'READY')).toBe(`${(7 / 185) * 100}%`)
    expect(bar(1, 'READY')).toBe('100%')
    expect(bar(1, 'PARKED')).toBe(`${(37 / 185) * 100}%`)
  })
})
