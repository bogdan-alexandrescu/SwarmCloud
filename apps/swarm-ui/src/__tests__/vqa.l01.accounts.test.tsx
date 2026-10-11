// VISUAL QA, LANE VQA-L01 (2026-10-11, part of #1038): Accounts, Holders and
// Provider quota. Each case asserts the PROPERTY the finding was about -- the
// rule the cascade picks, the class a cell carries, the words on screen --
// not the markup's shape.
//
//   V004/V014  `.acct-action button` restyled every button inside a panel:
//              the Holding now | History segments and TaskRef's copy icon.
//   V005/V020  Quota ids cut in the middle; the tenant head clips in its column.
//   V006       Holders' tenant filter scrolls itself, not the page.
//   V008       A failure or answer inside the Accounts card is the 13px scale,
//              Try again 12px under it.
//   V010       A write's failure is not drawn as a read's; Add account's 422
//              is a line at the form.
//   V013       `time not recorded` wraps inside Last 429.
//   V015       The phone hides History's When cell by name, not Since.
//   V016       Two Sign in again links are separated.
//   V017       Holding now / History answer from the development fixture.
//   V018       A code from another sign-in says one instruction, not two.
//   V019       The unread drift card is one line: dash, mark, sentence.
//   V065       The chosen row's window suffix uses the selection-safe grey.
//   V066       A re-auth's steps start at 1.
//   V067       A phone marks no row it did not open.
//   V068       The list's foot follows its rows.
//   V069       The add form's groups are 12px apart; steps start a block.
//   V070       An id never breaks at its hyphen.
//   V071       `‹ Accounts` keeps its space.
//   V072       The empty pool is one card: zero in the list, form in the pane.
//   V073       The list head holds one line.
//
// BREAK IT: put `.acct-action button` back -- V004 fails; drop `.acct-hist-when`
// -- V015 fails; drop `gap` from `.acct-back` -- V071 fails; print the tenant
// whole in the quota row head -- V005 fails; number re-auth from 2 -- V066 fails.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, HoldersBoard } from '../api'
import type { ApiError, Result } from '../fetch'
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

const { AccountsScreen, loadAccountHolders, loadAccountHistory, withoutRestart } = await import('../Accounts')
const { HoldersScreen } = await import('../Holders')
const { QuotaDetailScreen } = await import('../QuotaDetail')
const { FailedPanel } = await import('../Shell')
const { errorReassurance } = await import('../fetch')

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 5000 } as const
const hosts: HTMLElement[] = []

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
  for (const h of hosts.splice(0)) h.remove()
  document.body.innerHTML = ''
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

function board(accounts: Account[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'u-bogdan', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

async function accounts(list: Account[]): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(list)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
}

/** Parse an HTML fragment into a live host, for a cascade question about markup the screen draws. */
function html(markup: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = markup
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

describe('V004/V014: a panel styles its own buttons, not every button inside it', () => {
  it('leaves the segmented control and the copy icon to their own rules', () => {
    const seg = build('div.acct-action > div.c-seg > button', hosts)
    expect(painted(seg, 'border', WIDE)).toBe('0')
    expect(painted(seg, 'padding', WIDE)).toBe('0 12px')
    const copy = build('div.acct-action > div.acct-holders > table.acct-hist > tbody > tr > td > span.acct-hist-whoin > span.task-ref > button.task-ref-copy', hosts)
    expect(painted(copy, 'border', WIDE)).toBe('0')
    expect(painted(copy, 'padding', WIDE)).toBe('0')
  })

  it('still styles the panel\'s own controls', () => {
    for (const sel of [
      'div.acct-action > button',
      'div.acct-action > span.limit-edit > button',
      'div.acct-action > div.acct-holders > button',
      'div.acct-action > div.acct-buttons > button',
    ]) {
      expect(painted(build(sel, hosts), 'border-radius', WIDE), sel).toBe('6px')
    }
  })

  it('draws Holding now | History as one segmented control in the open account', async () => {
    await accounts([account({ assigned: 1 })])
    const tabs = document.querySelector('.acct-holding > .c-seg')
    expect(tabs, 'the switch is not the canonical segmented control').not.toBeNull()
    for (const b of tabs!.querySelectorAll('button')) expect(painted(b, 'border', WIDE)).toBe('0')
  })

  it('lets the tenant give way before the task reference', () => {
    const host = html(
      '<table class="acct-hist"><tbody><tr><td class="acct-hist-who"><span class="acct-hist-whoin">' +
        '<span class="mono">u-bogdan</span><span class="task-ref"><a class="ctl-link task-ref-link mono">task_…12345678</a></span>' +
        '<span class="ctl-mark is-partial">unverified</span></span></td></tr></tbody></table>',
    )
    const [tenant, ref, mark] = [...host.querySelector('.acct-hist-whoin')!.children]
    expect(painted(tenant!, 'flex-shrink', WIDE)).toBe('4')
    expect(painted(ref!, 'min-width', WIDE)).toBe('9ch')
    // A mark wraps between its words before the cell clips it; never below its longest word.
    expect(painted(mark!, 'flex', WIDE)).toBe('0 1 auto')
    expect(painted(mark!, 'min-width', WIDE)).toBeNull()
    expect(painted(host.querySelector('.acct-hist-whoin')!, 'flex-wrap', WIDE)).toBe('wrap')
  })
})

describe('V015: the phone hides History\'s When by name', () => {
  it('keeps Holding now\'s Since and hides only the When cell', () => {
    const host = html(
      '<table class="acct-hist acct-holding-now"><tbody><tr><td>a</td><td>1</td><td class="since">14:09</td></tr></tbody></table>' +
        '<table class="acct-hist"><tbody><tr><td>a</td><td class="acct-hist-when"><span></span></td></tr></tbody></table>',
    )
    expect(painted(host.querySelector('td.since')!, 'display', PHONE)).not.toBe('none')
    expect(painted(host.querySelector('td.acct-hist-when')!, 'display', PHONE)).toBe('none')
    expect(painted(host.querySelector('td.acct-hist-when')!, 'display', WIDE)).not.toBe('none')
  })
})

describe('V016: two Sign in again links are separated', () => {
  it('puts a separator between them', async () => {
    await accounts([
      account({ account_id: 'u-bogdan:a', label: 'a', state: 'REAUTH_REQUIRED' }),
      account({ account_id: 'u-bogdan:b', label: 'b', state: 'REAUTH_REQUIRED' }),
    ])
    const body = document.querySelector('.acct-callout-body')!
    expect(body.textContent).toContain('Sign in again to a · Sign in again to b')
  })
})

describe('V017: Holding now and History answer from the development fixture', () => {
  it('serves as many holds as the row says, and every ending in the history', async () => {
    const a = account({ assigned: 2, lend_to: ['eng'] })
    const now = await loadAccountHolders(a.account_id, { account: a, viewer: 'u-bogdan' })
    expect(now.status).toBe('ok')
    if (now.status !== 'ok') return
    expect(now.data.total).toBe(2)
    expect(now.data.holders.map((h) => h.tenant)).toEqual(['u-bogdan', 'eng'])
    const hist = await loadAccountHistory(a.account_id, null, { account: a, viewer: 'u-bogdan' })
    expect(hist.status).toBe('ok')
    if (hist.status !== 'ok') return
    expect(hist.data.spans.map((s) => s.end)).toEqual([null, 'released', 'unusable', 'expired'])
  })

  it('opens Holding now on a fixture board without a request', async () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    api.loadAccountsBoard.mockResolvedValue(ok({ ...board([account({ assigned: 1 })]), fixture: true }))
    render(<AccountsScreen />)
    await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
    fireEvent.click(screen.getByRole('tab', { name: /Holding now/ }))
    await screen.findByText('Holding now · 1', undefined, WAIT)
    expect(document.querySelector('.acct-holding-now tbody tr')).not.toBeNull()
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})

describe('V018: a code from another sign-in carries one instruction', () => {
  it('drops the platform\'s restart sentence and keeps what it refused', () => {
    const e: ApiError = {
      kind: 'invalid', httpStatus: 422, code: 'validation_failed',
      message: 'the pasted code belongs to a different sign-in than the one this page started. Press Add account and sign in again.',
    }
    expect(withoutRestart(e).message).toBe('the pasted code belongs to a different sign-in than the one this page started.')
    const other: ApiError = { ...e, message: 'the sign-in code was not accepted.' }
    expect(withoutRestart(other)).toBe(other)
  })
})

describe('V065: the chosen row\'s window suffix holds AA on the selection', () => {
  it('uses the selection-safe grey', () => {
    const win = build('li.acct-li.is-chosen > button.acct-open > em.acct-window > span.acct-pct-win', hosts)
    expect(painted(win, 'color', { ...WIDE, theme: 'dark' })).toBe('var(--text-dim-on-selected)')
    const plain = build('li.acct-li > button.acct-open > em.acct-window > span.acct-pct-win', hosts)
    expect(painted(plain, 'color', WIDE)).toBe('var(--text-faint)')
  })
})

describe('V066: a re-auth\'s steps start at 1', () => {
  it('numbers Sign in 1 and Paste 2', async () => {
    api.beginAccountSignIn.mockResolvedValue(
      ok({ authorize_url: 'https://claude.com/cai/oauth/authorize?state=s', state: 's'.repeat(43), expires_in_seconds: 900 }),
    )
    await accounts([account({ state: 'REAUTH_REQUIRED' })])
    const pane = document.querySelector('.acct-pane-detail')!
    const start = [...pane.querySelectorAll('button')].find((b) => b.textContent === 'Sign in again')!
    fireEvent.click(start)
    await waitFor(() => expect(pane.textContent).toContain('1 · Sign in to Claude'), WAIT)
    expect(pane.textContent).toContain('2 · Paste the code that page shows you')
    expect(pane.textContent).not.toContain('3 ·')
  })
})

describe('V067: a phone marks no row it did not open', () => {
  it('highlights the default only beside its pane', async () => {
    atPhone()
    await accounts([account(), account({ account_id: 'u-bogdan:b', label: 'b' })])
    expect(document.querySelector('.acct-li.is-chosen')).toBeNull()
    fireEvent.click(document.querySelectorAll('.acct-li > .acct-open')[1]!)
    expect(document.querySelectorAll('.acct-li.is-chosen').length).toBe(1)
  })

  it('still highlights the default on a desktop', async () => {
    await accounts([account(), account({ account_id: 'u-bogdan:b', label: 'b' })])
    expect(document.querySelectorAll('.acct-li.is-chosen').length).toBe(1)
  })
})

describe('V068/V071/V073: the list card', () => {
  it('puts the foot under the rows, not at the bottom of the stretched column', () => {
    const foot = build('div.acct-split > section.acct-list > p.acct-foot', hosts)
    expect(painted(foot, ['margin-top', 'margin'], WIDE)).toBe('0')
  })

  it('keeps the space in ‹ Accounts', () => {
    expect(painted(build('div.acct-pane > button.acct-back', hosts), 'gap', PHONE)).toBe('4px')
  })

  it('holds the list title to one line at 400px, the count giving way', () => {
    const note = build('div.acct-list-head > h2 > span.ctl-card-note', hosts)
    const h2 = note.parentElement!
    expect(painted(h2, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(h2, 'min-width', PHONE)).toBe('0')
    expect(painted(note, 'min-width', PHONE)).toBe('0')
    expect(painted(note, 'text-overflow', PHONE)).toBe('ellipsis')
  })
})

describe('V069: the add form\'s groups and the sign-in steps', () => {
  it('puts 12px between a field\'s rule and the next label, none above the rule', async () => {
    await accounts([])
    const form = document.querySelector('form.acct-add-form')!
    expect(painted(form, 'display', WIDE)).toBe('grid')
    const label = form.querySelector('label[for="acct-lend"]')!
    expect(painted(label, 'margin-top', WIDE)).toBe('12px')
    const rule = form.querySelector('#acct-lend-rule')!
    expect(painted(rule, 'margin-top', WIDE)).toBe('0')
  })

  it('starts each step as a block', () => {
    expect(painted(build('section.section > div.acct-action', hosts), 'margin-top', WIDE)).toBe('16px')
    expect(painted(build('div.acct-action > form.acct-action', hosts), 'margin-top', WIDE)).toBe('8px')
  })
})

describe('V070: an id never breaks at its hyphen', () => {
  it('draws the subtitle\'s and Lending\'s tenants as unbreakable ids', async () => {
    await accounts([account({ lend_to: ['eng-platform'] })])
    const ids = [...document.querySelectorAll('.acct-pane .acct-id')].map((e) => e.textContent)
    expect(ids).toContain('u-bogdan')
    expect(ids).toContain('eng-platform')
    for (const el of document.querySelectorAll('.acct-pane .acct-id')) {
      expect(painted(el, 'white-space', PHONE)).toBe('nowrap')
    }
  })
})

describe('V072: the empty pool is one card', () => {
  it('draws the heading and the zero in the list, the form in the pane', async () => {
    await accounts([])
    const split = document.querySelector('.acct-split')!
    expect(split.querySelector('.acct-list .acct-list-head > h2')!.textContent).toBe('Subscription accounts')
    expect(split.querySelector('.acct-list .state')!.textContent).toContain('No accounts registered')
    expect(split.querySelector('.acct-pane h2')!.textContent).toBe('Add an account')
    // The pane shows on a phone: there is no row to choose, only the form.
    expect(split.classList.contains('is-adding')).toBe(true)
  })
})

describe('V008: inside the card a failure speaks at the card\'s size', () => {
  it('is the 13px scale, with Try again 12px under the panel', () => {
    const p = build('div.acct-split > div.acct-pane > div.state.failed > p', hosts)
    expect(painted(p, 'font-size', WIDE)).toBe('var(--t-meta)')
    const btn = build('div.acct-split > div.acct-pane > div.state.failed > button.c-btn', hosts)
    expect(painted(btn, 'margin-top', WIDE)).toBe('12px')
    // A full-page error keeps the lead size.
    expect(painted(build('div.state > p', hosts), 'font-size', WIDE)).toBe('var(--t-lead)')
  })
})

describe('V010: a write\'s failure is not drawn as a read\'s', () => {
  const refused: ApiError = { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: 'label is bad', method: 'POST' }

  it('says the change was refused, draws no `not read`, and offers no resend of the same input', () => {
    render(<FailedPanel error={refused} onRetry={() => undefined} />)
    const panel = document.querySelector('.state.failed')!
    expect(panel.textContent).toContain('refused this change')
    expect(panel.textContent).not.toContain('failure to read the platform')
    expect(panel.querySelector('.ctl-mark.is-unread')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('keeps Try again for a write the platform did not confirm, and the read wording for a read', () => {
    const lost: ApiError = { kind: 'server_error', httpStatus: 500, code: null, message: 'boom', method: 'POST' }
    expect(errorReassurance(lost)).toContain('did not confirm this change')
    render(<FailedPanel error={lost} onRetry={() => undefined} />)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(errorReassurance({ ...lost, method: undefined })).toContain('failure to read the platform')
  })

  it('draws Add account\'s 422 as a line at the form, not a failure panel', async () => {
    api.beginAccountSignIn.mockResolvedValue({ status: 'error', error: { ...refused, message: "account label 'Bad' must be lowercase" } })
    await accounts([])
    fireEvent.change(document.querySelector('#acct-label')!, { target: { value: 'Bad' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start the sign-in' }))
    const line = await screen.findByRole('alert', undefined, WAIT)
    expect(line.textContent).toContain("account label 'Bad' must be lowercase. Nothing was created.")
    expect(document.querySelector('.acct-add-form .state.failed')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Holders
// ---------------------------------------------------------------------------

function lease(n: number, tenant: string): LeaseRow {
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
  } as LeaseRow
}

function holders(tenants: string[]): HoldersBoard {
  return {
    page: {
      leases: tenants.map((t, i) => lease(i + 1, t)),
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-07T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: tenants.length,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: null,
    poolsDetail: 'not read in this test',
  }
}

describe('V006: the Holders tenant filter scrolls itself, not the page', () => {
  it('is held to its row and scrolls inside it', async () => {
    api.loadHolders.mockResolvedValue(ok(holders(['alpha-team', 'beta-team', 'gamma-team', 'delta-team'])))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const seg = document.querySelector('.ctl-toolbar > .hold-tenants')!
    expect(painted(seg, 'overflow-x', PHONE)).toBe('auto')
    expect(painted(seg, 'min-width', PHONE)).toBe('0')
    expect(painted(seg, 'max-width', PHONE)).toBe('calc(100% - 8px)')
    expect(painted(seg, 'flex', PHONE)).toBe('0 1 auto')
  })
})

describe('V019: the unread drift card is one line', () => {
  it('lays the dash, the mark and what they mean in a row', async () => {
    api.loadHolders.mockResolvedValue(ok(holders(['eng'])))
    render(<HoldersScreen />)
    await screen.findByText('Accounting drift', undefined, WAIT)
    const body = document.querySelector('.hold-drift .hold-unread')!
    expect(painted(body, 'display', WIDE)).toBe('flex')
    expect(body.querySelector('.ctl-figure.is-absent')).not.toBeNull()
    expect(body.querySelector('.ctl-mark.is-unread')!.textContent).toBe('not read')
    expect(body.querySelector('p')!.textContent).toContain('No pool was compared')
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

describe('V005/V020/V013: Provider quota cuts ids in the middle and wraps the 429 mark', () => {
  const LONG = 'u-bogdan-alexandrescu-research'

  async function quotaRow(): Promise<HTMLElement> {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota({ tenant_id: LONG, rate_limit_count: 2 })] }))
    render(<QuotaDetailScreen />)
    const [th] = await screen.findAllByRole('rowheader', { name: LONG }, WAIT)
    return th!.closest('tr') as HTMLElement
  }

  it('keeps the tenant\'s tail and clips its head inside the column', async () => {
    const row = await quotaRow()
    const th = row.querySelector('th.quota-tenant')!
    expect(th.getAttribute('title')).toBe(LONG)
    // Found by its whole name above: the two boxes do not split it for a screen reader.
    expect(painted(th, ['overflow-x', 'overflow'], WIDE)).toBe('hidden')
    expect(th.querySelector('.quota-cut-tail')!.textContent).toBe(LONG.slice(-8))
    const head = th.querySelector('.quota-cut-head')!
    expect(painted(head, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(head, 'flex', WIDE)).toBe('0 1 auto')
    const tail = th.querySelector('.quota-cut-tail')!
    expect(painted(tail, 'flex', WIDE), 'the tail gives way before the head').toBe('none')
    expect(painted(tail, 'max-width', WIDE)).toBe('100%')
  })

  it('keeps the Feeds pool\'s tenant whole and cuts its provider prefix', async () => {
    const row = await quotaRow()
    const link = row.querySelector('.quota-feeds a')!
    expect(link.textContent).toBe(`provider:anthropic:tenant:${LONG}`)
    expect(screen.getByRole('link', { name: `provider:anthropic:tenant:${LONG}` })).toBe(link)
    expect(link.querySelector('.quota-cut-tail')!.textContent).toBe(`tenant:${LONG}`)
    expect(link.querySelector('.quota-cut-head')!.textContent).toBe('provider:anthropic:')
  })

  it('wraps `time not recorded` inside Last 429', async () => {
    const row = await quotaRow()
    const cell = row.querySelector('td[data-label="Last 429"]')!
    const mark = cell.querySelector('.ctl-mark')!
    expect(mark.textContent).toBe('time not recorded')
    expect(painted(mark, 'white-space', WIDE)).toBe('normal')
    expect(painted(cell, 'white-space', WIDE)).toBe('normal')
  })
})
