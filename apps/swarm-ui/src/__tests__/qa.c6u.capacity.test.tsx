// QA G5 (2026-10-07), Capacity and Admin, lane C6U: five findings.
//
//   G5-08  At 390 every capacity and admin table scrolled sideways and hid its
//          answer column. Below the phone breakpoint each one is now the
//          `data-label` record Holders' drift table draws (`.is-stacked`), and
//          the profile matrix is one card per profile: name, Can start, and
//          the pool it runs out on with leased/ceiling.
//   G5-15  Three spellings of one task id. One `TaskRef`: the title when
//          known, else `task_…` and the last eight; Holders' second line is
//          labelled `lease …`.
//   G5-20  A 0% Use track was outlined and read as fuller than 5%. On Pools
//          the zero is a 1px tick with no outline.
//   G5-22  Pools and Pool limits ordered the same pools differently. One
//          comparator: family, problems first, this tenant, name.
//   G5-23  Wording drifts: `given out 44h ago`, `ws of mem`, `1u` under
//          `Weight (units)`, `Quota cap` with no unit, `429s (this run)`
//          wrapping, the list % with no window, Platform counts with no
//          footer, the pool name and `N holders` linking to one place, and
//          the Holders tenant and the chosen account not in the address.
//
// BREAK IT: render `is-scroll` at every width -- every G5-08 case fails; print
// `l.task_id.slice(-10)` again -- G5-15 fails; drop the `.cap-use` zero rule --
// G5-20 fails; sort Pool limits by name -- G5-22 fails; put back any of the
// G5-23 words -- its case fails.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, HoldersBoard, RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type {
  Account,
  AccountsPage,
  Capacity,
  LeasePage,
  LeaseRow,
  Me,
  Pool,
  ProfileAdmission,
  QuotaState,
  RunnerProfile,
  Runtime,
  Tenant,
} from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  loadMe: vi.fn(),
  setPoolLimit: vi.fn(),
  loadHolders: vi.fn(),
  loadAdminQuota: vi.fn(),
  loadTenants: vi.fn(),
  loadAccountsBoard: vi.fn(),
  loadRuntimeTopology: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')
const { ProfileMatrix } = await import('../ProfileMatrix')
const { HoldersScreen } = await import('../Holders')
const { QuotaDetailScreen } = await import('../QuotaDetail')
const { AdminSettingsScreen } = await import('../AdminSettings')
const { TenantsScreen } = await import('../Activity')
const { AccountsScreen, agoPastDay } = await import('../Accounts')
const { RuntimesScreen } = await import('../Runtimes')
const { PlatformCountsScreen } = await import('../PlatformCounts')
const { TaskRef, shortRef } = await import('../TaskRef')
const { comparePools } = await import('../capacityPoll')
const { spacedAge } = await import('../Shell')
const { canonical, fromHash } = await import('../App')

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
})

/** A phone: `(max-width: 560px)` matches, as it does at 390. */
function atPhone(): void {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)',
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-07T10:00:00Z', ...over,
  }
}

function capacity(pools: Pool[], profiles: Record<string, RunnerProfile> = {}): Capacity {
  return { tenant_id: 'eng', generated_at: '2026-10-07T10:00:00Z', pools_complete: true, pools, runner_profiles: profiles } as Capacity
}

const POOLS = [
  pool('global', 1, 40),
  pool('tenant:smoke', 0, 5),
  pool('tenant:eng', 2, 5),
  pool('runner:zeta', 10, 10),
  pool('runner:alpha', 1, 10),
  pool('runner:beta', 7, 10),
  pool('provider:anthropic', 0, 30),
]

const ADMIN_ME = {
  tenant: { tenant_id: 'eng' },
  principal: { email: 'a@saga.xyz', domain: 'saga.xyz', groups: [], is_admin: true },
  environment: 'dev', environment_declared: false,
} as unknown as Me

/**
 * THE RECORD, read through the shipped sheet at 390 (G5-08): no sideways
 * scroller, the table a block, every field a key/value grid with the column
 * name drawn as its key, and the row's name no longer a held sticky column.
 */
function expectRecord(wrapper: Element, label: string): void {
  expect(wrapper.classList.contains('is-stacked'), `${label} is not stacked at 390`).toBe(true)
  expect(wrapper.classList.contains('is-scroll'), `${label} still scrolls at 390`).toBe(false)
  expect(painted(wrapper, 'overflow-x', PHONE)).toBe('visible')
  const table = wrapper.querySelector(':scope > table')!
  expect(painted(table, 'display', PHONE)).toBe('block')
  expect(painted(table, 'width', PHONE), `${label}'s table is wider than the phone`).not.toBe('max-content')
  const td = wrapper.querySelector('tbody > tr > td[data-label]')!
  expect(painted(td, 'display', PHONE)).toBe('grid')
  expect(painted(td, 'content', PHONE, 'before')).toBe('attr(data-label)')
  const th = wrapper.querySelector('tbody > tr > th')
  if (th !== null) expect(painted(th, 'position', PHONE)).not.toBe('sticky')
}

/** The control: the same table above the phone breakpoint keeps CH-13's scroll. */
function expectScroll(wrapper: Element): void {
  expect(wrapper.classList.contains('is-scroll')).toBe(true)
  expect(wrapper.classList.contains('is-stacked')).toBe(false)
}

// ---------------------------------------------------------------------------
// Fixtures for the screens
// ---------------------------------------------------------------------------

function lease(n: number, tenant = 'eng'): LeaseRow {
  const tail = String(n).padStart(4, '0')
  return {
    lease_id: `lease_9f1c2d3e4b5a${tail}77aa`,
    task_id: `task_7e07e56d1732${tail}6f2c`,
    attempt_id: `att-${n}`,
    tenant_id: tenant,
    generation: 1,
    pools: ['global'],
    units: n,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-07T09:00:00Z',
    dispatch_deadline: '2026-10-07T09:05:00Z',
    expires_at: '2026-10-07T11:00:00Z',
    heartbeat_at: '2026-10-07T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  } as LeaseRow
}

function holders(): HoldersBoard {
  return {
    page: {
      leases: [lease(1), lease(2, 'research')],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-07T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 3,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: null,
    poolsDetail: 'not read in this test',
  }
}

function quota(over: Partial<QuotaState> = {}): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: new Date().toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
  }
}

function tenant(id: string): Tenant {
  return {
    tenant_id: id, kind: 'group', principal: `${id}@saga.xyz`, display_name: null,
    created_at: '2026-09-24T09:00:00Z', max_active: 10, capacity_units: 8, monthly_budget_usd: null,
    enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
  }
}

function account(id: string, label: string, over: Partial<Account> = {}): Account {
  const now = Date.now()
  return {
    account_id: id, owner_tenant: 'eng', label, provider: 'anthropic-subscription', state: 'AVAILABLE',
    reason: '', lend_to: [], assigned: 0,
    windows: {
      five_hour: { utilization: 0.69, resets_at: new Date(now + 2 * HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.01, resets_at: new Date(now + 90 * HOUR).toISOString(), reset: false },
    },
    observed_at: new Date(now - 60_000).toISOString(), stale: false, unreadable_by: [], unreadable_now: [],
    last_assigned_at: new Date(now - 44 * HOUR - 5 * 60_000).toISOString(),
    ...over,
  } as Account
}

function accountsBoard(accounts: Account[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [], tenantsDetail: null, readAt: Date.now(),
  } as AccountsBoard
}

function runtime(name: string): Runtime {
  return {
    name, available: true, disabled_reason: '', image: 'agent-runtime-base', backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB', provider: null, secrets: [], secrets_any_of: false, timeout_seconds: 600,
    resource_class: 'small', resources: { name: 'small', cpu: 1, memory_gib: 2, disk_gib: 1, units: 3 },
  } as Runtime
}

function topology(): RuntimeTopology {
  return {
    runtimes: { alpha: runtime('alpha') },
    classes: { small: { name: 'small', cpu: 1, memory_gib: 2, disk_gib: 1, units: 3 } },
    classesDetail: null, pools: [], poolsDetail: null,
    profilePools: { alpha: ['global'] }, profileUncapped: { alpha: [] },
  } as unknown as RuntimeTopology
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 1, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

const MATRIX = capacity(
  [pool('global', 1, 40), pool('tenant:eng', 37, 40), pool('runner:codex', 2, 10)],
  {
    codex: {
      resource_class: 'standard', backend: 'cloudrun', provider: null, units: 1,
      pools: ['global', 'tenant:eng', 'runner:codex'],
      admission: admission({ headroom: 3, binding: ['tenant:eng'] }),
    } as RunnerProfile,
  },
)

async function pools(): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(capacity(POOLS)))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

async function limits(): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(capacity(POOLS)))
  api.loadMe.mockResolvedValue(ok(ADMIN_ME))
  render(<AdminSettingsScreen />)
  await waitFor(() => expect(document.getElementById('limit-global')).not.toBeNull(), WAIT)
}

async function holdersScreen(): Promise<HTMLElement> {
  api.loadHolders.mockResolvedValue(ok(holders()))
  render(<HoldersScreen />)
  await screen.findByText('Every holder', undefined, WAIT)
  return screen.getByText('Every holder').closest('section')!
}

async function quotaScreen(): Promise<void> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota()] }))
  render(<QuotaDetailScreen />)
  await screen.findAllByRole('rowheader', { name: 'eng' }, WAIT)
}

async function accountsScreen(accounts: Account[]): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(accountsBoard(accounts)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
}

function listItem(label: string): HTMLElement {
  const btn = [...document.querySelectorAll('.acct-li > .acct-open')].find((b) => b.textContent?.includes(label))
  expect(btn, `no account ${label}`).toBeTruthy()
  return btn!.closest('li') as HTMLElement
}

// ---------------------------------------------------------------------------

describe('G5-08: at 390 a capacity or admin table is a record per row', () => {
  it('stacks every Pools family table, and keeps the scroll above the phone', async () => {
    atPhone()
    await pools()
    const tables = [...document.querySelectorAll('.cap-families .cap-pools')]
    expect(tables.length).toBeGreaterThan(1)
    for (const t of tables) expectRecord(t, 'Pools')
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    await pools()
    for (const t of document.querySelectorAll('.cap-families .cap-pools')) expectScroll(t)
  })

  it('draws the profile matrix as one card per profile: name, Can start, runs out first with leased/ceiling', () => {
    atPhone()
    const { container } = render(<ProfileMatrix capacity={MATRIX} />)
    const wrapper = container.querySelector('.cap-mx > .ctl-table')!
    expectRecord(wrapper, 'the profile matrix')
    const row = container.querySelector('tr[data-profile="codex"]')!
    // No family cell on the card: the answer is the card.
    expect([...row.querySelectorAll('td')].map((c) => c.getAttribute('data-label'))).toEqual(['Can start', 'Runs out first'])
    expect(row.querySelector('td[data-label="Can start"]')!.textContent).toBe('3')
    const first = row.querySelector('td[data-label="Runs out first"]')!.textContent!.replace(/\s+/g, ' ').trim()
    expect(first).toBe('eng (37/40) fits 3')
    // The per-family figures are in the expander.
    fireEvent.click(within(row as HTMLElement).getByRole('button'))
    const fits = container.querySelector('.cap-mx-fits')!
    expectRecord(fits, 'the opened Fits table')
    expect([...fits.querySelectorAll(':scope > table > tbody th')].map((t) => t.getAttribute('title'))).toEqual(['global', 'tenant:eng', 'runner:codex'])
  })

  it('keeps the matrix a table with its family columns on a desktop', () => {
    const { container } = render(<ProfileMatrix capacity={MATRIX} />)
    expectScroll(container.querySelector('.cap-mx > .ctl-table')!)
    expect(container.querySelectorAll('tr[data-profile="codex"] td').length).toBeGreaterThan(2)
  })

  it('stacks Holders, with Heartbeat on the card', async () => {
    atPhone()
    const section = await holdersScreen()
    const wrapper = section.querySelector('.ctl-table')!
    expectRecord(wrapper, 'Holders')
    expect(wrapper.querySelector('td[data-label="Heartbeat"]')).not.toBeNull()
  })

  it('stacks Provider quota, its fixed table no wider than the phone', async () => {
    atPhone()
    await quotaScreen()
    const wrapper = document.querySelector('.quota-table')!
    expectRecord(wrapper, 'Provider quota')
    expect(painted(wrapper.querySelector(':scope > table')!, 'width', PHONE)).toBe('auto')
  })

  it('stacks Pool limits, so edit is whole', async () => {
    atPhone()
    await limits()
    const wrapper = document.getElementById('limit-global')!.closest('.ctl-table')!
    expectRecord(wrapper, 'Pool limits')
  })

  it('stacks the Tenants roster', async () => {
    atPhone()
    api.loadTenants.mockResolvedValue(ok({ tenants: [tenant('eng'), tenant('smoke')] }))
    api.loadCapacity.mockResolvedValue(ok(capacity([pool('tenant:eng', 1, 5)])))
    render(<TenantsScreen />)
    await screen.findByText('smoke', undefined, WAIT)
    const wrapper = document.querySelector('table.ten-table')!.parentElement!
    expectRecord(wrapper, 'Tenants')
  })
})

describe('G5-15: one task reference across Capacity', () => {
  const ID = 'task_7e07e56d17324cc686f2'

  it('is task_… and the last eight, the whole id in its title and its copy', () => {
    expect(shortRef(ID)).toBe('task_…4cc686f2')
    const { container } = render(<TaskRef id={ID} />)
    const link = container.querySelector('a')!
    expect(link.textContent).toBe('task_…4cc686f2')
    expect(link.getAttribute('title')).toBe(ID)
    expect(link.getAttribute('href')).toBe(`#work/task/${ID}`)
    expect(container.querySelector('button')!.getAttribute('aria-label')).toBe(`Copy task id ${ID}`)
  })

  it('is the task title when it is known', () => {
    const { container } = render(<TaskRef id={ID} title="Fix lease heartbeat" />)
    expect(container.querySelector('a')!.textContent).toBe('Fix lease heartbeat')
    expect(container.querySelector('a')!.getAttribute('title')).toBe(ID)
  })

  it('copies the whole id', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<TaskRef id={ID} />)
    fireEvent.click(screen.getByRole('button', { name: `Copy task id ${ID}` }))
    expect(writeText).toHaveBeenCalledWith(ID)
    await screen.findByText('task id copied')
  })

  it('is what Holders prints, with its second line labelled as the lease', async () => {
    const section = await holdersScreen()
    const row = [...section.querySelectorAll('tbody tr')].find((r) => r.querySelector('a[title="task_7e07e56d173200016f2c"]'))!
    expect(row.querySelector('th a')!.textContent).toBe(shortRef('task_7e07e56d173200016f2c'))
    const lease = row.querySelector('.lease-ref')!
    expect(lease.textContent).toBe('lease …000177aa')
    expect(lease.getAttribute('title')).toBe('lease_9f1c2d3e4b5a000177aa')
  })
})

describe('G5-20: a 0% Use track is lighter than a 5% one', () => {
  it('draws the zero as a 1px tick with no outline on Pools, and keeps the shared mark elsewhere', async () => {
    await pools()
    const zero = document.querySelector('.cap-use .ctl-util-track.is-zero')!
    expect(zero, 'no 0% row in the fixture').not.toBeNull()
    expect(painted(zero, 'box-shadow', WIDE)).toBe('none')
    expect(painted(zero.querySelector('.ctl-util-zero')!, 'width', WIDE)).toBe('1px')
    // Control: outside Pools the measured-zero mark is unchanged.
    const host = document.createElement('span')
    host.className = 'ctl-util-track is-zero'
    document.body.appendChild(host)
    expect(painted(host, 'box-shadow', WIDE)).toContain('inset')
  })
})

describe('G5-22: one pool order on Pools and Pool limits', () => {
  it('sorts family, then problems first, then this tenant, then name', () => {
    const order = comparePools('eng')
    const names = [...POOLS].sort(order).map((p) => p.name)
    expect(names).toEqual([
      'global',
      'tenant:eng',
      'tenant:smoke',
      'runner:zeta',
      'runner:alpha',
      'runner:beta',
      'provider:anthropic',
    ])
  })

  it('draws the same order on both screens', async () => {
    await pools()
    const onPools = [...document.querySelectorAll('.cap-families tbody th[title]')].map((t) => t.getAttribute('title'))
    document.body.innerHTML = ''
    await limits()
    const onLimits = [...document.querySelectorAll('.cap-families tbody tr[id^="limit-"]')].map((r) => r.id.slice('limit-'.length))
    // Pools pulls "Needs action" (the full runner:zeta) to the top; the rest
    // is in one order on both.
    const rest = onPools.filter((n) => n !== 'runner:zeta')
    expect(onLimits.filter((n) => n !== 'runner:zeta')).toEqual(rest)
    expect(onLimits.indexOf('runner:zeta')).toBeLessThan(onLimits.indexOf('runner:alpha'))
  })
})

describe('G5-23: format and wording', () => {
  it('says a one-minute cadence as `1 min`, never `1m 0s` (already on main)', () => {
    expect(spacedAge(60_000)).toBe('1 min')
  })

  it('says a hand-out past a day in days and hours', async () => {
    const now = Date.parse('2026-10-07T10:00:00Z')
    expect(agoPastDay(new Date(now - 44 * HOUR).toISOString(), now)).toBe('1d 20h ago')
    expect(agoPastDay(new Date(now - 48 * HOUR).toISOString(), now)).toBe('2d ago')
    expect(agoPastDay(new Date(now - 3 * HOUR).toISOString(), now)).toBe('3h ago')
    await accountsScreen([account('eng:devops-main', 'devops-main')])
    expect(listItem('devops-main').querySelector('.acct-given')!.textContent).toBe('given out 1d 20h ago')
  })

  it('names the window beside the list figure', async () => {
    await accountsScreen([account('eng:devops-main', 'devops-main')])
    const fig = listItem('devops-main').querySelector('[data-label="5h used"]')!
    expect(fig.textContent!.replace(/\s+/g, ' ').trim()).toBe('69% 5h')
  })

  it('puts the chosen account in the address, and opens the account the address names', async () => {
    await accountsScreen([account('eng:a', 'alpha'), account('eng:b', 'bravo')])
    fireEvent.click(listItem('bravo').querySelector('.acct-open')!)
    expect(window.location.hash).toBe('#capacity/accounts?account=eng%3Ab')
    document.body.innerHTML = ''
    window.history.replaceState(null, '', '/capacity/accounts?account=eng%3Ab')
    await accountsScreen([account('eng:a', 'alpha'), account('eng:b', 'bravo')])
    expect(listItem('bravo').classList.contains('is-chosen')).toBe(true)
    expect(listItem('alpha').classList.contains('is-chosen')).toBe(false)
  })

  it('puts the Holders tenant chip in the address, and filters by the tenant it names', async () => {
    let section = await holdersScreen()
    fireEvent.click(within(section.querySelector<HTMLElement>('.hold-tenants')!).getByRole('button', { name: 'research' }))
    expect(window.location.hash).toBe('#capacity/holders?tenant=research')
    document.body.innerHTML = ''
    window.history.replaceState(null, '', '/capacity/holders?tenant=research')
    section = await holdersScreen()
    await waitFor(() => expect(section.querySelectorAll('tbody tr')).toHaveLength(1), WAIT)
    expect(section.querySelector('tbody th a')!.getAttribute('title')).toBe('task_7e07e56d173200026f2c')
  })

  it('keeps tenant and account on their addresses through the router', () => {
    window.location.hash = '#capacity/holders?pool=global&tenant=eng&x=1'
    expect(canonical(fromHash())).toBe('capacity/holders?pool=global&tenant=eng')
    window.location.hash = '#capacity/accounts?account=eng%3Ab'
    expect(canonical(fromHash())).toBe('capacity/accounts?account=eng%3Ab')
    // Control: a key a pane does not read is still dropped.
    window.location.hash = '#capacity/pools?tenant=eng'
    expect(canonical(fromHash())).toBe('capacity/pools')
    window.location.hash = ''
  })

  it('links the pool name to its own row, not to the holders link beside it', async () => {
    await pools()
    const th = document.querySelector('.cap-families tbody th[title="runner:alpha"]')!
    const name = th.querySelector('a')!.getAttribute('href')
    const held = [...th.closest('tr')!.querySelectorAll('td.cap-links a')].map((a) => a.getAttribute('href'))
    expect(name).toBe(`#capacity/pools?pool=${encodeURIComponent('runner:alpha')}`)
    expect(held).not.toContain(name)
  })

  it('says workspace on a runtime card, and a bare figure under Weight (units)', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const keys = [...document.querySelectorAll('.ctl-fact > b')].map((b) => b.textContent)
    expect(keys).toContain('workspace')
    expect(keys).not.toContain('ws of mem')
    expect(document.querySelector('td[data-label="Weight (units)"]')!.textContent).toBe('3')
  })

  it('gives Quota cap its unit, and holds both figure heads on one line', async () => {
    await quotaScreen()
    const heads = [...document.querySelectorAll('.quota-table thead th')]
    const cap = heads.find((h) => h.textContent === 'Quota cap (units)')
    const runs = heads.find((h) => h.textContent === '429s (this run)')
    expect(cap, heads.map((h) => h.textContent).join(' | ')).toBeTruthy()
    expect(painted(cap!, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(runs!, 'white-space', WIDE)).toBe('nowrap')
  })

  it('ends Platform counts with the footer every sibling page has', () => {
    api.loadMe.mockResolvedValue(ok(ADMIN_ME))
    render(<PlatformCountsScreen />)
    expect(screen.getByText('Reading this screen:')).toBeTruthy()
  })
})
