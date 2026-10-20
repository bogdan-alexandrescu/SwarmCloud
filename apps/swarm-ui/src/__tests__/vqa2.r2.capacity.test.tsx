// VISUAL QA RE-RUN, LANE VQA2-R2 (2026-10-16, part of #1427): Accounts,
// Holders and Provider quota. Each case asserts the PROPERTY the finding was
// about -- the rule the cascade picks, the element a value is drawn in, the
// words on screen, where focus and the page land -- not the markup's shape.
//
//   V004  History's figure columns give the phone's Holder cell their slack.
//   V005  Quota's columns follow what they hold: Tenant is no longer 11%.
//   V013  `time not recorded` holds one line in a column as wide as it.
//   V020  A long Feeds pool keeps its tenant's tail; one underline.
//   V068  Neither half of the Accounts card stretches to the other.
//   V070  The state-change modal and the Replace copy draw ids unbroken.
//   V153  An unbreakable id is cut inside the pane, not widening it.
//   V154  Lending's commas ride inside their ids; the list shrinks to the pane.
//   V155  Holding now's tenants are cut in the middle and held to the card.
//   V156  `(at last report)` is the text face, so the count fits its column.
//   V157  The desktop pane sticks in view and opens at its top.
//   V158  A phone lands on the chosen account's head, focus on `‹ Accounts`.
//   V179  Above a phone the Holders tenant filter wraps instead of scrolling.
//   V180  A stale quota cell is one element: one value line in a record.
//   V181  Lending's placeholder fits; the field takes its own line on a phone.
//   V182  A finished sign-in shows its receipt, not the empty form under it.
//   V183  A heartbeat duration never wraps inside itself.
//   V184  The paste field is one width in a re-auth and in Add account.
//
// BREAK IT: put `.quota-c-tenant` back to 11% -- V005 fails; `align-items:
// stretch` on `.acct-split` -- V068 fails; `{i > 0 && ', '}` outside the id
// -- V154 fails; draw the form under the receipt -- V182 fails; a plain space
// in `unbroken` -- V183 fails; `location.hash =` in `choose` -- V158 fails.

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage, LeasePage, LeaseRow, QuotaState } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
  loadTask: vi.fn(async () => ({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'not read in this test' },
  })),
  loadHolders: vi.fn(),
  loadAdminQuota: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { AccountsScreen } = await import('../Accounts')
const { HoldersScreen } = await import('../Holders')
const { QuotaDetailScreen } = await import('../QuotaDetail')

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const
const hosts: HTMLElement[] = []
const LONG_TENANT = 'data-platform-ingestion-team-europe-west'

const scrolled: { el: Element; arg: unknown }[] = []
beforeEach(() => {
  scrolled.length = 0
  // jsdom implements no `scrollIntoView`; record who asked to be shown.
  Element.prototype.scrollIntoView = function (this: Element, arg?: unknown) {
    scrolled.push({ el: this, arg })
  } as Element['scrollIntoView']
})

afterEach(() => {
  // Unmount before the body is wiped: a Dialog portals into <body>, and React
  // removing a portal the wipe already took throws.
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  for (const h of hosts.splice(0)) h.remove()
  document.body.innerHTML = ''
  delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
})

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

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'u-bogdan:laptop',
    owner_tenant: 'u-bogdan',
    label: 'laptop',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {
      five_hour: { utilization: 0.4, resets_at: new Date(Date.now() + 3_600_000).toISOString(), reset: false },
    },
    observed_at: new Date().toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  } as Account
}

function board(accounts: Account[], fixture = false): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'u-bogdan', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
    ...(fixture ? { fixture: true } : {}),
  } as AccountsBoard
}

async function accounts(list: Account[], fixture = false): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(list, fixture)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
}

function many(n: number): Account[] {
  return Array.from({ length: n }, (_, i) => {
    const label = `acct-${String(i).padStart(2, '0')}`
    return account({ account_id: `u-bogdan:${label}`, label })
  })
}

function row(id: string): HTMLButtonElement {
  return [...document.querySelectorAll<HTMLButtonElement>('.acct-open')].find((b) => b.dataset.account === id)!
}

/** Parse an HTML fragment into a live host, for a cascade question about markup the screen draws. */
function html(markup: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = markup
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

// ---------------------------------------------------------------------------
// Accounts
// ---------------------------------------------------------------------------

describe('V004: History\'s figures give the phone\'s Holder their slack', () => {
  it('sizes Started, Held and Ended to their longest values at 400', () => {
    const host = html(
      '<table class="acct-hist"><thead><tr><th data-col="holder">Holder</th><th data-col="started">Started</th>' +
        '<th data-col="held">Held</th><th data-col="ended">Ended</th><th data-col="when">When</th></tr></thead></table>',
    )
    const width = (col: string, env: CascadeEnv) => painted(host.querySelector(`th[data-col="${col}"]`)!, 'width', env)
    expect(width('started', PHONE)).toBe('84px')
    expect(width('held', PHONE)).toBe('62px')
    expect(width('ended', PHONE)).toBe('64px')
    // 400 - 2 x 17 of card and pane padding leaves Holder at least 130px.
    expect(366 - 24 - 84 - 62 - 64).toBeGreaterThanOrEqual(130)
    // The desktop keeps its widths: there the When bar takes the slack.
    expect(width('ended', WIDE)).toBe('104px')
  })
})

describe('V068/V157: neither half of the card stretches; the pane sticks in view', () => {
  it('sizes each half to what it holds, the divider on both edges', () => {
    const split = build('div.acct-split', hosts)
    expect(painted(split, 'align-items', WIDE)).toBe('start')
    // `clip`: a sticky pane sticks to the page's scroller, not to the card.
    expect(painted(split, ['overflow', 'overflow-y'], WIDE)).toBe('clip')
    const pane = build('div.acct-split > div.acct-pane', hosts)
    expect(painted(pane, 'border-left', WIDE)).toBe('1px solid var(--line)')
    expect(painted(pane, 'margin-left', WIDE)).toBe('-1px')
    expect(painted(build('div.acct-split > section.acct-list', hosts), 'border-right', WIDE)).toBe('1px solid var(--line)')
  })

  it('sticks the desktop pane under the page head and scrolls a long detail inside it', () => {
    const pane = build('div.acct-split > div.acct-pane', hosts)
    expect(painted(pane, 'position', WIDE)).toBe('sticky')
    expect(painted(pane, 'top', WIDE)).toBe('0')
    expect(painted(pane, 'overflow-y', WIDE)).toBe('auto')
    expect(painted(pane, 'max-height', WIDE)).toBe('calc(100vh - 48px)')
    // A phone's pane is the page: it neither sticks nor scrolls itself.
    expect(painted(pane, 'position', PHONE)).not.toBe('sticky')
  })

  it('opens a chosen account at its top and brings the pane into view', async () => {
    await accounts(many(40))
    const pane = document.querySelector<HTMLElement>('.acct-split > .acct-pane')!
    pane.scrollTop = 900
    scrolled.length = 0
    fireEvent.click(row('u-bogdan:acct-38'))
    await waitFor(() => expect(document.querySelector('.acct-pane-detail h3')!.textContent).toBe('acct-38'), WAIT)
    expect(pane.scrollTop).toBe(0)
    const shown = scrolled.filter((s) => s.el === pane)
    expect(shown.map((s) => s.arg)).toEqual([{ block: 'nearest' }])
  })
})

describe('V158: a phone lands on the chosen account\'s head', () => {
  it('scrolls the page to the pane and puts focus on ‹ Accounts', async () => {
    atPhone()
    await accounts(many(40))
    scrolled.length = 0
    fireEvent.click(row('u-bogdan:acct-38'))
    await waitFor(() => expect(document.querySelector('.acct-split.is-detail')).not.toBeNull(), WAIT)
    const pane = document.querySelector('.acct-split > .acct-pane')!
    expect(scrolled.filter((s) => s.el === pane).map((s) => s.arg)).toEqual([{ block: 'start' }])
    expect(document.activeElement).toBe(pane.querySelector('.acct-back'))
  })

  it('follows the choice in the address without a popstate the shell would read as Back', async () => {
    await accounts(many(3))
    const pops = vi.fn()
    const hashes = vi.fn()
    window.addEventListener('popstate', pops)
    window.addEventListener('hashchange', hashes)
    try {
      fireEvent.click(row('u-bogdan:acct-01'))
      await waitFor(() => expect(window.location.hash).toContain('account=u-bogdan%3Aacct-01'), WAIT)
      expect(hashes).toHaveBeenCalled()
      // A popstate makes the shell put back the scroll it recorded for the
      // address -- for three seconds, over the scroll above.
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20))
      })
      expect(pops).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('popstate', pops)
      window.removeEventListener('hashchange', hashes)
    }
  })
})

describe('V153: an id is cut inside the pane, not widening it', () => {
  it('gives the detail and every panel one column as wide as the pane', () => {
    expect(painted(build('div.acct-split div.acct-detail', hosts), 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(build('div.acct-split div.acct-action', hosts), 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })

  it('caps a panel\'s content-sized children, the measured prose included, at the column', () => {
    expect(painted(build('div.acct-split div.acct-action > ul.ctl-facts', hosts), 'max-width', PHONE)).toBe('min(620px, 100%)')
    expect(painted(build('div.acct-split div.acct-action > p.muted.small', hosts), 'max-width', PHONE)).toBe(
      'min(var(--measure), 100%)',
    )
  })

  it('lets `remove <label>` take a second line rather than leave the card', () => {
    const btn = build('div.acct-split div.acct-action > span.limit-edit > button.c-btn', hosts)
    expect(painted(btn, 'white-space', PHONE)).toBe('normal')
    expect(painted(btn, 'max-width', PHONE)).toBe('100%')
    expect(painted(btn, 'height', PHONE)).toBe('auto')
  })
})

describe('V154: Lending\'s serves list', () => {
  it('keeps each comma inside its id, so no line starts with one', async () => {
    await accounts([account({ lend_to: [LONG_TENANT, 'eng'] })])
    const serves = [...document.querySelectorAll('.acct-pane .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'serves')!
    const ids = [...serves.querySelectorAll('.acct-id')].map((e) => e.textContent)
    expect(ids).toEqual(['u-bogdan,', `${LONG_TENANT},`, 'eng'])
    expect(serves.querySelector('.acct-ids')!.textContent).toBe(`u-bogdan, ${LONG_TENANT}, eng`)
    // Whole in each title, without the comma.
    expect(serves.querySelectorAll('.acct-id')[1]!.getAttribute('title')).toBe(LONG_TENANT)
  })

  it('shrinks the fact to the pane so a long id is cut at its edge', () => {
    const fact = build('div.acct-split div.acct-action > ul.ctl-facts > li.ctl-fact', hosts)
    expect(painted(fact, 'max-width', PHONE)).toBe('100%')
    expect(painted(build('div.acct-split div.acct-action li.ctl-fact > span.acct-ids', hosts), 'min-width', PHONE)).toBe('0')
  })
})

describe('V181: Lending\'s field', () => {
  it('says what it takes in words that fit, the Add form\'s', async () => {
    await accounts([account()])
    const input = document.querySelector<HTMLInputElement>('input[aria-label^="Tenants"]')!
    expect(input.placeholder).toBe('tenant ids, comma separated')
  })

  it('takes its own line on a phone, `save lending` under it', () => {
    const input = build('div.acct-split div.acct-action > span.limit-edit > input.acct-wide', hosts)
    expect(painted(input, 'flex', PHONE)).toBe('1 1 100%')
    expect(painted(input, 'max-width', PHONE)).toBe('none')
    expect(painted(input, 'flex', WIDE)).toBe('1 1 auto')
  })
})

describe('V155: Holding now\'s tenants', () => {
  it('cuts a long tenant in its middle, keeping its last eight', async () => {
    await accounts([account({ assigned: 2, lend_to: [LONG_TENANT] })], true)
    fireEvent.click(screen.getByRole('tab', { name: /Holding now/ }))
    await screen.findByText('Holding now · 2', undefined, WAIT)
    const cut = document.querySelector('.acct-holding-now .acct-cut')!
    expect(cut.querySelector('.acct-cut-tail')!.textContent).toBe(LONG_TENANT.slice(-8))
    expect(cut.textContent).toBe(LONG_TENANT)
    expect(painted(cut.querySelector('.acct-cut-head')!, 'text-overflow', PHONE)).toBe('ellipsis')
    expect(painted(cut.querySelector('.acct-cut-tail')!, 'flex', PHONE)).toBe('none')
    // A short tenant is one box, as it was.
    const short = [...document.querySelectorAll('.acct-holding-now .acct-hist-whoin > .mono')].find((e) => e.textContent === 'u-bogdan')!
    expect(short.classList.contains('acct-cut')).toBe(false)
  })

  it('holds the by-tenant list to the card', () => {
    expect(painted(build('ul.acct-holder-tenants > li', hosts), 'max-width', PHONE)).toBe('100%')
  })
})

describe('V070: the state-change modal and the Replace copy draw ids unbroken', () => {
  it('names the owner as an id in the modal\'s warning', async () => {
    await accounts([account({ assigned: 2 })])
    fireEvent.click(screen.getByRole('button', { name: 'move to paused' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    const warn = dialog.querySelector('.warn-text')!
    expect(warn.textContent).toContain('Every agent in u-bogdan loses it for new work.')
    expect([...warn.querySelectorAll('.acct-id')].map((e) => e.textContent)).toEqual(['u-bogdan'])
  })

  it('names the account as an id in Replace the credential', async () => {
    await accounts([account()])
    const head = [...document.querySelectorAll('.acct-pane h4')].find((h) => h.textContent === 'Replace the credential')!
    const copy = head.parentElement!.querySelector('p.muted.small')!
    // The full stop inside the id's box, so a cut id never leaves it to start a line.
    expect(copy.querySelector('.acct-id.mono')!.textContent).toBe('u-bogdan:laptop.')
    expect(copy.querySelector('.acct-id.mono')!.getAttribute('title')).toBe('u-bogdan:laptop')
    expect(copy.textContent).toContain('Replaces the credential behind u-bogdan:laptop. Nothing here needs doing')
    expect(copy.querySelector('code')).toBeNull()
  })

  it('names it as an id when only a person can replace it', async () => {
    await accounts([account({ state: 'REAUTH_REQUIRED' })])
    const head = [...document.querySelectorAll('.acct-pane h4')].find((h) => h.textContent === 'Sign in again')!
    expect(head.parentElement!.querySelector('p.muted.small .acct-id')!.textContent).toBe('u-bogdan:laptop')
  })
})

describe('V184: the paste field is one width wherever the sign-in runs', () => {
  it('stretches a re-auth\'s inner panel and holds every wide field to 420px', () => {
    const inner = build('div.acct-split div.acct-action > form.acct-action', hosts)
    expect(painted(inner, 'justify-self', WIDE)).toBe('stretch')
    const reauth = build('div.acct-split div.acct-action > form.acct-action > input.acct-wide', hosts)
    const add = build('div.acct-split section.acct-add-panel > form.acct-action > input.acct-wide', hosts)
    expect(painted(reauth, 'max-width', WIDE)).toBe('min(420px, 100%)')
    expect(painted(add, 'max-width', WIDE)).toBe('min(420px, 100%)')
  })
})

describe('V182: a finished sign-in shows its receipt, not the form', () => {
  it('draws the receipt alone, and done closes the panel', async () => {
    const added = account({ account_id: 'u-bogdan:work-laptop', label: 'work-laptop', observed_at: null })
    api.beginAccountSignIn.mockResolvedValue(
      ok({ authorize_url: 'https://claude.com/cai/oauth/authorize?state=s', state: 's'.repeat(43), expires_in_seconds: 900 }),
    )
    api.finishAccountSignIn.mockResolvedValue(ok({ account: added, expires_at: null }))
    await accounts([account()])
    fireEvent.click(document.querySelector('.acct-add')!)
    fireEvent.change(document.querySelector('#acct-label')!, { target: { value: 'work-laptop' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start the sign-in' }))
    const paste = await screen.findByRole('textbox', { name: 'The code the Claude callback page displayed' }, WAIT)
    fireEvent.change(paste, { target: { value: 'code-from-the-page' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add this account' }))
    const receipt = await screen.findByText(/Signed in — u-bogdan:work-laptop/, undefined, WAIT)
    const panel = document.querySelector('.acct-add-panel')!
    expect(panel.contains(receipt)).toBe(true)
    // No empty form beside the receipt, Start disabled under it.
    expect(panel.querySelector('form.acct-add-form')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start the sign-in' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'done' }))
    await waitFor(() => expect(document.querySelector('.acct-add-panel')).toBeNull(), WAIT)
    expect(document.activeElement).toBe(document.querySelector('.acct-add'))
  })
})

// ---------------------------------------------------------------------------
// Holders
// ---------------------------------------------------------------------------

function lease(n: number, tenant: string, over: Partial<LeaseRow> = {}): LeaseRow {
  return {
    lease_id: `lease_${String(n).padStart(8, '0')}`,
    task_id: `task_${String(n).padStart(12, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: tenant,
    generation: 1,
    pools: ['global'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-07T09:00:00Z',
    dispatch_deadline: '2026-10-07T09:05:00Z',
    heartbeat_ever: true,
    silent_seconds: 0,
    ...over,
  } as LeaseRow
}

function holders(leases: LeaseRow[]): HoldersBoard {
  return {
    page: {
      leases,
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: new Date().toISOString(),
      active_only: true,
      tenant_id: null,
      units_held: leases.length,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: null,
    poolsDetail: 'not read in this test',
  }
}

describe('V179: above a phone the tenant filter wraps', () => {
  it('takes as many rows as it needs at 1440, ruled between segments', async () => {
    const tenants = Array.from({ length: 12 }, (_, i) => `tenant-${i}`)
    api.loadHolders.mockResolvedValue(ok(holders(tenants.map((t, i) => lease(i + 1, t)))))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const seg = document.querySelector('.ctl-toolbar > .hold-tenants')!
    expect(painted(seg, 'flex-wrap', WIDE)).toBe('wrap')
    expect(painted(seg, 'height', WIDE)).toBe('auto')
    expect(painted(seg, 'overflow-x', WIDE)).toBe('hidden')
    const btn = seg.querySelector('button')!
    expect(painted(btn, 'box-shadow', WIDE)).toBe('1px 0 0 var(--line), 0 1px 0 var(--line)')
    expect(painted(btn, 'min-height', WIDE)).toBe('var(--h-ctl)')
    // A phone keeps V006's one scrolling row.
    expect(painted(seg, 'overflow-x', PHONE)).toBe('auto')
  })
})

describe('V183: a heartbeat duration is one word', () => {
  it('joins a duration\'s units with a no-break space', async () => {
    api.loadHolders.mockResolvedValue(ok(holders([lease(1, 'eng', { silent_seconds: 1286 })])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const cell = document.querySelector('td[data-label="Heartbeat"]')!
    expect(cell.textContent).toMatch(/21m \d+s/)
    expect(cell.textContent).not.toMatch(/\dm \d/)
  })

  it('does the same for a starting lease\'s deadline', async () => {
    const created = new Date(Date.now() - 1_286_000).toISOString()
    const deadline = new Date(Date.now() + 185_000).toISOString()
    api.loadHolders.mockResolvedValue(
      ok(holders([lease(1, 'eng', { dispatch_state: 'LEASED', heartbeat_ever: false, created_at: created, dispatch_deadline: deadline })])),
    )
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const cell = document.querySelector('td[data-label="Heartbeat"]')!
    expect(cell.textContent).toMatch(/no beat yet, 21m \d+s/)
    expect(cell.textContent).toMatch(/deadline in 3m \d+s/)
  })
})

// ---------------------------------------------------------------------------
// Provider quota
// ---------------------------------------------------------------------------

function quota(over: Partial<QuotaState> = {}): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: new Date().toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
  }
}

async function quotaRow(over: Partial<QuotaState>): Promise<HTMLElement> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota(over)] }))
  render(<QuotaDetailScreen />)
  const tenant = over.tenant_id ?? 'eng'
  const [th] = await screen.findAllByRole('rowheader', { name: tenant }, WAIT)
  return th!.closest('tr') as HTMLElement
}

const STALE = () => new Date(Date.now() - 17 * 60_000).toISOString()

describe('V005/V013: the quota columns follow what they hold', () => {
  it('gives Tenant and Last 429 the room State and Quota cap did not need', () => {
    const host = html(
      '<div class="quota-table"><table><colgroup>' +
        ['tenant', 'state', 'cap', 'feeds', 'left', '429s', 'last', 'rep'].map((c) => `<col class="quota-c-${c}">`).join('') +
        '</colgroup></table></div>',
    )
    const widths = [...host.querySelectorAll('col')].map((c) => painted(c, 'width', WIDE))
    // Tenant keeps its share; the bounded columns are as wide as what they
    // hold; Feeds pool sets no width and takes what is left (QA-057).
    expect(widths).toEqual(['16%', '168px', '116px', null, '104px', '124px', '136px', '76px'])
    // At the ~1054px table they were measured on, they leave Feeds pool its 15%.
    const fixed = widths.reduce((n, w) => n + (w === null ? 0 : w.endsWith('%') ? (parseFloat(w) / 100) * 1054 : parseFloat(w)), 0)
    expect(1054 - fixed).toBeGreaterThanOrEqual(0.15 * 1054)
    // Rendered at 1440 (nav open, a ~1054px table) the `time not recorded`
    // pill is 112px; at 11% the cell gave it 96px inside its 10px gutters and
    // clipped its right border. Its column holds it whole.
    const last = parseFloat(widths[6] ?? '0') - 2 * 10
    expect(last).toBeGreaterThanOrEqual(112)
  })

  it('holds `time not recorded` to one line', async () => {
    const row = await quotaRow({ rate_limit_count: 2 })
    const cell = row.querySelector('td[data-label="Last 429"]')!
    const mark = cell.querySelector('.ctl-mark')!
    expect(mark.textContent).toBe('time not recorded')
    expect(painted(mark, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(cell, 'white-space', WIDE)).toBe('nowrap')
  })

  it('holds Requests left\'s head to one line', async () => {
    await quotaRow({})
    const head = screen.getByRole('columnheader', { name: 'Requests left' })
    expect(head.classList.contains('quota-nowrap')).toBe(true)
  })
})

describe('V156/V180: a stale reading\'s cells', () => {
  it('draws `(at last report)` in the text face, not the figures\' mono', () => {
    expect(painted(build('td.is-num > span.quota-val > span.quota-basis', hosts), 'font-family', WIDE)).toBe('var(--font)')
  })

  it('draws the state and its age as one element, and the count and its basis as one', async () => {
    const row = await quotaRow({ updated_at: STALE(), rate_limit_count: 9 })
    const state = row.querySelector('td[data-label="State"]')!
    expect(state.children.length).toBe(1)
    expect(state.children[0]!.classList.contains('quota-val')).toBe(true)
    expect(state.children[0]!.querySelector('.ctl-stale-mark')!.textContent).toBe('17m old')
    const count = [...row.querySelectorAll('td')].find((td) => td.querySelector('.quota-basis') !== null)!
    expect(count.children.length).toBe(1)
    expect(count.children[0]!.textContent).toBe('9 (at last report)')
    expect(painted(count.children[0]!, 'display', PHONE)).toBe('inline-flex')
  })
})

describe('V020: the Feeds pool keeps its tenant and draws one underline', () => {
  const POOL = `provider:anthropic:tenant:${LONG_TENANT}`

  it('cuts a long tail in its middle, keeping the tenant\'s last eight', async () => {
    const row = await quotaRow({ tenant_id: LONG_TENANT })
    const link = row.querySelector('.quota-feeds a')!
    expect(link.textContent).toBe(POOL)
    expect(link.getAttribute('aria-label')).toBe(POOL)
    const tail = link.querySelector('.quota-cut-tail.is-split')!
    expect(tail.querySelector('.quota-cut-mid')!.textContent).toBe(`tenant:${LONG_TENANT.slice(0, -8)}`)
    expect(tail.querySelector('.quota-cut-end')!.textContent).toBe(LONG_TENANT.slice(-8))
    expect(painted(tail.querySelector('.quota-cut-mid')!, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(tail.querySelector('.quota-cut-end')!, 'flex', WIDE)).toBe('none')
  })

  it('underlines the name once, as wide as it is drawn, and keeps the head\'s end', async () => {
    const row = await quotaRow({ tenant_id: 'eng' })
    const link = row.querySelector('.quota-feeds a')!
    expect(painted(link, 'text-decoration', WIDE)).toBe('none')
    const cut = link.querySelector('.quota-cut')!
    expect(painted(cut, 'border-bottom', WIDE)).toBe('1px solid currentColor')
    expect(painted(cut, 'display', WIDE)).toBe('inline-flex')
    const head = cut.querySelector('.quota-cut-head')!
    expect(painted(head, 'direction', WIDE)).toBe('rtl')
    expect(painted(head.firstElementChild!, 'unicode-bidi', WIDE)).toBe('isolate')
  })
})
