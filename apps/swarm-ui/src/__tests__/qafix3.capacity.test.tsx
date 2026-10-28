// VISUAL QA RUN 3 (wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of
// main at 51d99c53), LANE QAFIX3-CAPACITY (part of #1680): Capacity, Provider
// quota and Accounts. Each case holds the property the finding was about --
// the rule the cascade picks for the rendered element, the accessible name,
// the words in the cell -- not the markup's shape.
//
//   capacity-012       At 390 a Pools record keeps its Use meter.
//   a11y-keyboard-003  Every capacity table is named by the heading above it.
//   capacity-004       An `unknown` quota row is not drawn in the exhausted red.
//   capacity-002       Accounts' remove button is drawn off until it is armed.
//   pools Set by       The configured case is the faint dot on Pools; the word
//                      is Pool limits' alone (owner decision 2026-10-22, c14).
//
//   capacity-007      A scroll stops a Pools or matrix row below the sticky
//                     head, not under it (lane QAFIX3-CAPACITY-HEADER).

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage, Capacity, Pool, QuotaState } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted, stateHueIn } from './marks'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadLeases: vi.fn(),
  loadAdminQuota: vi.fn(),
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
  setAccountModelClasses: vi.fn(),
  loadTask: vi.fn(async () => ({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'not read in this test' },
  })),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { CapacityScreen } = await import('../Capacity')
const { QuotaDetailScreen } = await import('../QuotaDetail')
const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-21T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-21T10:00:00Z', ...over,
  }
}

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-21T10:00:00Z',
    pools_complete: true,
    pools: [
      pool('global', 1, 40),
      pool('tenant:eng', 1, 10, { hard_limit: 20, adaptive_target: 10 }),
      pool('runner:claude-code', 0, 10),
      pool('backend:CLOUD_RUN_JOB', 1, 30),
      pool('provider:anthropic', 1, 30),
    ],
    runner_profiles: {},
  } as Capacity
}

async function renderPools(): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(capacity()))
  api.loadLeases.mockResolvedValue(ok({ leases: [], next_cursor: null }))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function rowOf(name: string): HTMLElement {
  const th = [...document.querySelectorAll<HTMLElement>('.cap-families th[scope="row"]')].find(
    (t) => t.getAttribute('title') === name,
  )
  expect(th, `no Pools row for ${name}`).toBeTruthy()
  return th!.closest('tr')!
}

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: new Date(Date.now() - 60_000).toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
  }
}

/** The fixture build's two `openai` rows, which the operator compared. */
async function renderQuota(): Promise<void> {
  api.loadAdminQuota.mockResolvedValue(ok({
    quota: [
      quota({ provider: 'anthropic', tenant_id: 'eng' }),
      quota({ provider: 'openai', tenant_id: 'eng', state: 'EXHAUSTED', effective_limit: 0, rate_limit_count: 41 }),
      quota({ provider: 'openai', tenant_id: 'u-bogdan', state: 'UNKNOWN', effective_limit: 0, success_count: 0 }),
    ],
  }))
  render(<QuotaDetailScreen />)
  await screen.findAllByRole('rowheader', { name: 'u-bogdan' }, WAIT)
}

function quotaRow(provider: string, tenant: string): HTMLElement {
  const table = screen.getByRole('table', { name: `${provider} quota, by tenant` })
  const th = [...table.querySelectorAll<HTMLElement>('th[scope="row"]')].find((t) => t.getAttribute('title') === tenant)
  expect(th, `no ${provider} row for ${tenant}`).toBeTruthy()
  return th!.closest('tr')!
}

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'eng:alice-personal',
    owner_tenant: 'eng',
    label: 'alice-personal',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 2,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

async function renderAccounts(a: Account = account()): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(
    ok({
      page: { accounts: [a], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard),
  )
  render(<AccountsScreen />)
  await screen.findAllByText(a.account_id, undefined, WAIT)
}

describe('capacity-012: at 390 a Pools record keeps its Use meter', () => {
  it('draws the track beside the figure in the stacked record, as at 1440', () => {
    const track = build(
      'div.ctl-table.cap-pools.is-stacked > table > tbody > tr > td > span.cap-use > span.ctl-util-track',
      hosts,
    )
    // MUTATION: drop the phone `display` on `.cap-pools.is-stacked .cap-use >
    // .ctl-util-track` (styles/capacity.css) -- styles.css's phone block hides
    // every track again, and with it the meter a screen reader reads.
    expect(painted(track, 'display', PHONE)).not.toBe('none')
    expect(painted(track, 'display', WIDE)).not.toBe('none')
    // MUTATION: put `display: block` back on the phone `.cap-use` -- the track
    // and its figure stack instead of sharing the line, the figure under a bar.
    expect(painted(track.parentElement!, 'display', PHONE)).toBe('grid')
  })
})

describe('capacity-007: a scroll stops a row below the sticky head, not under it', () => {
  // The head sticks above 900px (V091). A scroll that brings a row into view
  // -- Tab to a link in it, Page Down, `scrollIntoView`, an anchor -- stops it
  // at the scroller's top padding, so that padding has to be the head's own
  // height (and the bell bar's, where one is drawn), read from the rule that
  // sets the head's height rather than restated as pixels.
  const SAFE = { width: 800 } as const
  const headRow = (sel: string): HTMLElement => build(`${sel} > table > thead > tr`, hosts)

  it('makes each sticky head exactly one row tall, from a token', () => {
    for (const sel of ['div.cap-families > div.ctl-table.is-scroll', 'div.cap-mx > div.ctl-table.is-scroll']) {
      // MUTATION: drop `height: var(--row-h)` from the sticky heads' row
      // (styles/capacity.css) -- the head is its content's ~28px and the
      // padding below no longer names the height it is drawn at.
      expect(painted(headRow(sel), 'height', WIDE), sel).toBe('var(--row-h)')
    }
  })

  it('pads the page scroller by the bar and the Pools head, so the first row clears it', () => {
    const head = headRow('div.ctl-frame > div.ctl-scroll > div.app > div.cap-families > div.ctl-table.is-scroll')
    const h = painted(head, 'height', WIDE) ?? 'no height on the head row'
    const scroller = head.closest('.ctl-scroll')!
    // MUTATION: drop the `.ctl-scroll:has(.cap-families ...)` rule -- the
    // scroller keeps styles.css's bar-only padding (0px on Capacity) and a
    // row scrolled into view stops under the head, as capacity-007 captured.
    expect(painted(scroller, 'scroll-padding-top', WIDE)).toBe(`calc(0px + ${h})`)
    const barred = headRow('div.ctl-frame.has-ctl-bar > div.ctl-scroll > div.cap-families > div.ctl-table.is-scroll')
    // MUTATION: replace `var(--ctl-bar-h, 0px)` with 0 -- under an open
    // object's bar the row stops 48px short, under the bar and the head.
    expect(painted(barred.closest('.ctl-scroll')!, 'scroll-padding-top', WIDE)).toBe(`calc(48px + ${h})`)
    // MUTATION: lift the rule out of `@media (min-width: 900px)` -- below 900
    // the head does not stick, and a row would stop a head's height short.
    expect(painted(scroller, 'scroll-padding-top', SAFE)).toBe('0px')
  })

  it('pads the matrix\'s own scroller by its head', () => {
    const head = headRow('div.cap-mx > div.ctl-table.is-scroll')
    const wrap = head.closest('.ctl-table')!
    // MUTATION: drop `scroll-padding-top` from `.cap-mx > .ctl-table.is-scroll`
    // -- the matrix scrolls itself (V091), so the page's padding never reaches
    // it and its rows stop under its head.
    expect(painted(wrap, 'scroll-padding-top', WIDE)).toBe(painted(head, 'height', WIDE) ?? 'no height on the head row')
  })
})

describe('a11y-keyboard-003: every capacity table has an accessible name', () => {
  it('names each Pools family table by its heading', async () => {
    await renderPools()
    // MUTATION: drop `aria-labelledby` from PoolTable's <table> -- each of
    // these is an unnamed table again and none is found.
    // `tenant:eng` is lowered (10 of 20), so it leads in Needs a look (VQ-031,
    // BGD 2026-10-26) and Tenants has no table of its own.
    for (const name of ['Needs a look · 1', 'Global', 'Runner profiles', 'Backends', 'Providers']) {
      expect(screen.getByRole('table', { name }), `the ${name} table has no name`).toBeTruthy()
    }
    const tables = screen.getAllByRole('table')
    for (const t of tables) expect(t.getAttribute('aria-labelledby') ?? t.getAttribute('aria-label'), 'an unnamed capacity table').toBeTruthy()
  })

  it('names each Provider quota table by its provider', async () => {
    await renderQuota()
    // MUTATION: drop the `aria-label` from QuotaDetail's <table>.
    expect(screen.getByRole('table', { name: 'anthropic quota, by tenant' })).toBeTruthy()
    expect(screen.getByRole('table', { name: 'openai quota, by tenant' })).toBeTruthy()
  })

  it('names Accounts\' Holding now and History tables', async () => {
    const since = new Date(Date.now() - 30 * 60_000).toISOString()
    reads.read.mockImplementation(async (target: { url: string }) => {
      if (target.url.includes('/holders')) {
        return ok({ account_id: 'eng:alice-personal', viewer: 'owner', total: 1, others: 0, holders: [{ since, task_id: 'task_abc', attempt: 1, recorded: true, verified: true }] })
      }
      if (target.url.includes('/history')) {
        return ok({
          account_id: 'eng:alice-personal', viewer: 'owner', from: new Date(Date.now() - 7 * 86_400_000).toISOString(), to: new Date().toISOString(),
          spans: [{ since, until: null, end: null, mine: true, task_id: 'task_abc', attempt: 1, recorded: true, verified: true }], others: 0, next_cursor: null,
        })
      }
      throw new Error(`unexpected read ${target.url}`)
    })
    await renderAccounts()
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
    // MUTATION: drop the `aria-label` from HoldingNow's <table> in the sheet (#2222).
    expect(await screen.findByRole('table', { name: 'Holding now · 1' }, WAIT)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    // MUTATION: drop `aria-labelledby` from HoldingHistory's <table>.
    expect(await screen.findByRole('table', { name: 'History' }, WAIT)).toBeTruthy()
  })
})

describe('capacity-004: an `unknown` quota row is not drawn in the exhausted red', () => {
  it('washes u-bogdan\'s unknown row amber and leaves eng\'s exhausted row red, in both themes', async () => {
    await renderQuota()
    const unknown = quotaRow('openai', 'u-bogdan')
    const exhausted = quotaRow('openai', 'eng')
    for (const theme of THEMES) {
      const env = { ...WIDE, theme }
      // MUTATION: let `admitsNothing` decide the row before the unknown state
      // does (QuotaDetail.tsx Row) -- the unknown row takes `is-bad` and reads
      // `--bad`, the same shading as the exhausted row beside it.
      expect(stateHueIn(painted(unknown, ['background', 'background-color'], env), theme)).toBe('--warn')
      // MUTATION: wash every cap-0 row amber -- an exhausted row stops being red.
      expect(stateHueIn(painted(exhausted, ['background', 'background-color'], env), theme)).toBe('--bad')
    }
  })
})

describe('capacity-002: Accounts\' remove button is drawn off until it is armed', () => {
  // OWNER DECISION 2026-10-22 ~01:15Z, "Danger outline unarmed" (box comment
  // 6314115845, part of #1680; lane ACCOUNT-NAME): the plain grey control this
  // case first pinned is now a danger OUTLINE -- the danger ink on its edge,
  // no fill -- so it still reads as off, never as the armed fill.
  it('is the danger outline, unfaded, before the label is typed, and the filled red once it is', async () => {
    reads.read.mockResolvedValue(ok({ account_id: 'eng:alice-personal', viewer: 'owner', total: 0, holders: [], others: 0 }))
    await renderAccounts()
    // Remove is in the account's sheet (#2222), which opens on its row.
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
    const btn = screen.getByRole('button', { name: 'Remove alice-personal' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    for (const theme of THEMES) {
      const env = { ...WIDE, theme }
      // MUTATION: draw the unarmed button as `danger-filled` again (Accounts.tsx
      // Remove) -- `.c-btn.is-danger-filled:disabled` paints the pale red
      // `--s-badb` ground under a red edge, which reads as armed.
      expect(painted(btn, ['background', 'background-color'], env)).toBe('transparent')
      // MUTATION: draw it `secondary` again -- the edge is the grey `--line`.
      expect(painted(btn, ['border-color', 'border'], env)).toBe('var(--s-bad)')
    }
    // MUTATION: drop `.acct-action > .limit-edit > .c-btn:disabled { opacity: 1 }`
    // (styles/capacity.css) -- `.limit-edit button:disabled` fades it to .4,
    // the faded disabled look V256 retired.
    expect(painted(btn, 'opacity', WIDE)).toBe('1')

    fireEvent.change(screen.getByRole('textbox', { name: 'Type alice-personal to confirm removal' }), {
      target: { value: 'alice-personal' },
    })
    const armed = screen.getByRole('button', { name: 'Remove alice-personal' }) as HTMLButtonElement
    expect(armed.disabled).toBe(false)
    // MUTATION: draw it secondary when armed too -- the one irreversible
    // control on the screen loses its red once it can fire.
    expect(armed.className).toContain('is-danger-filled')
    expect(painted(armed, ['background', 'background-color'], WIDE)).toBe('var(--s-bad)')
  })
})

describe('Pools Set by: the configured case is the faint dot, the word only on Pool limits (owner decision 2026-10-22, c14)', () => {
  it('draws `configured` as a faint `·` named configured, and an override in full ink', async () => {
    await renderPools()
    const cfg = rowOf('global').querySelector<HTMLElement>('td[data-label="Set by"]')!
    // MUTATION: put the word `configured` back in the cell (Capacity.tsx
    // PoolRow, as PR 1853 had it) -- owner decision 2026-10-22, c14: the dot
    // on Pools, the word on Pool limits only.
    expect(cfg.textContent).toBe('·')
    // MUTATION: drop `role="img"` or its `aria-label` -- a reader hears a
    // bare middle dot, not what set the ceiling.
    const dot = within(cfg).getByRole('img', { name: 'configured' })
    expect(dot.className).toBe('cap-setby-cfg')
    // MUTATION: drop `title={by.detail}` from the cell -- the dot loses the
    // only place that says where the ceiling was configured.
    expect(cfg.getAttribute('title')).not.toBeNull()
    for (const theme of THEMES) {
      // MUTATION: draw the dot in `--text-dim` (PR 1853's word ink) -- c14
      // asks for the faint dot, quieter than a word.
      expect(painted(dot, 'color', { ...WIDE, theme })).toBe('var(--text-faint)')
    }
    expect(rowOf('tenant:eng').querySelector('td[data-label="Set by"]')!.textContent).toBe('lowered automatically after errors')
  })
})
