// ACCOUNT-HEALTH-PAGE (part of #2302): CAPACITY › ACCOUNT HEALTH, AS BGD PICKED IT.
//
// F1 (proposal B): four tiles per status word that agree with the list, a
// fifth that says whether the pool can take work and links to Accounts, then
// the accounts worst first. F3 (B): the 7-day swimlane, one mark per served
// event, its total agreeing with the counts. F4 (A): no events yet, the record
// not served (a dash with its reason, never a zero), an account that needs
// re-auth, an unreadable read. F5 (D) at 390: the switch, the summary line,
// the open incidents first with their fixes, then the rest.
//
// BREAK IT: count a tile from a different field than the list's, sort
// alphabetically, draw one mark per hour instead of per event, print 0 where
// the record is not served, or put the closed incidents first on a phone -- a
// case below turns red.

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AccountHealthBody } from '../AccountHealth'
import { loadAccountHealthBoard, type AccountHealthBoard } from '../api'
import {
  HEALTH_KINDS,
  type AccountHealthRecord,
  type HealthAccount,
  type HealthEvent,
  type HealthKind,
  type HealthStatus,
  type KindCounts,
} from '../accountHealthModel'

afterEach(cleanup)

const NOW = Date.UTC(2026, 9, 25, 7, 10)
const MIN = 60_000
const H = 60 * MIN

function counts(events: HealthEvent[], hours: number): KindCounts {
  const c = Object.fromEntries(HEALTH_KINDS.map((k) => [k, 0])) as KindCounts
  for (const e of events) if (Date.parse(e.at) >= NOW - hours * H) c[e.kind] += 1
  return c
}

function ev(minutesAgo: number, kind: HealthKind, o: { window?: string; resetIn?: number; message?: string | null } = {}): HealthEvent {
  return {
    at: new Date(NOW - minutesAgo * MIN).toISOString(),
    kind,
    tenant_id: 'eng',
    task_id: `task_${kind}_${minutesAgo}`,
    window: o.window ?? null,
    reset_at: o.resetIn === undefined ? null : new Date(NOW + o.resetIn * MIN).toISOString(),
    message: o.message === undefined ? `${kind} message` : o.message,
    own: true,
  }
}

function acct(label: string, status: HealthStatus | null | undefined, events: HealthEvent[], over: Partial<HealthAccount> = {}): HealthAccount {
  const row: HealthAccount = {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
    provider: 'anthropic',
    state: status === 'reauth_needed' ? 'REAUTH_REQUIRED' : 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 1,
    windows: {},
    observed_at: new Date(NOW - 5 * MIN).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    max_agents: 6,
    can_take_new_agent: status === 'healthy' || status === 'erroring',
    blocked_by: [],
    ...over,
  }
  if (status !== undefined) {
    row.health_status = status
    row.health_24h = counts(events, 24)
  }
  return row
}

function rec(a: HealthAccount, events: HealthEvent[]): AccountHealthRecord {
  const top = Math.floor(NOW / H) * H
  const sorted = [...events].sort((x, y) => Date.parse(y.at) - Date.parse(x.at))
  return {
    account_id: a.account_id,
    label: a.label,
    owner_tenant: 'eng',
    status: (a.health_status as HealthStatus | null | undefined) ?? null,
    limited_until: null,
    counts: { last_1h: counts(events, 1), last_24h: counts(events, 24), last_7d: counts(events, 168) },
    hourly: Array.from({ length: 168 }, (_, i) => {
      const at = top - (167 - i) * H
      const c: Partial<KindCounts> = {}
      for (const e of events) {
        const t = Date.parse(e.at)
        if (t >= at && t < at + H) c[e.kind] = (c[e.kind] ?? 0) + 1
      }
      return { hour: new Date(at).toISOString(), counts: c, total: Object.values(c).reduce((n, v) => n + (v ?? 0), 0) }
    }),
    events: sorted.slice(0, 50),
    observed_at: new Date(NOW).toISOString(),
  }
}

/** Seven accounts, one of each state, as the mock-ups' example pool. */
function pool(): { accounts: HealthAccount[]; events: Map<string, HealthEvent[]> } {
  const plan: Array<[string, HealthStatus, HealthEvent[]]> = [
    ['saga-team', 'healthy', [ev(600, 'network')]],
    ['aieng-claude1', 'limited', [ev(80, 'rate_limit', { window: 'five_hour', resetIn: 120 }), ev(40, 'assign_refused', { window: 'five_hour' })]],
    ['ai-eng-2', 'erroring', [ev(30, 'overloaded'), ev(25, 'overloaded'), ev(20, 'server_error'), ev(15, 'overloaded')]],
    ['ai-eng-3', 'reauth_needed', [ev(62, 'auth'), ev(61, 'reauth'), ev(30, 'assign_refused')]],
    ['devops-main', 'healthy', []],
    ['devops-team', 'healthy', [ev(3000, 'overloaded')]],
    ['saga-personal', 'limited', [ev(200, 'rate_limit', { window: 'seven_day', resetIn: 1500 })]],
  ]
  const events = new Map<string, HealthEvent[]>()
  const accounts = plan.map(([label, s, evs]) => {
    events.set(`eng:${label}`, evs)
    return acct(label, s, evs)
  })
  return { accounts, events }
}

function board(
  accounts: HealthAccount[],
  events: Map<string, HealthEvent[]>,
  health: 'ok' | 'not_served' | 'unreadable' = 'ok',
): AccountHealthBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 },
    health:
      health === 'ok'
        ? {
            kind: 'ok',
            pool: {
              tenant_id: 'eng',
              accounts: accounts.map((a) => rec(a, events.get(a.account_id) ?? [])),
              events_max: 50,
              truncated: false,
              observed_at: null,
            },
          }
        : health === 'not_served'
          ? { kind: 'not_served', reason: 'GET /v1/accounts/health answers 404.' }
          : { kind: 'unreadable', reason: 'The health read timed out.' },
    readAt: NOW,
  }
}

function desktop(b: AccountHealthBoard) {
  return render(<AccountHealthBody board={b} now={NOW} phone={false} />)
}

describe('F1: the tiles and the list', () => {
  it('counts each status tile from the same statuses the list draws', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const rows = [...container.querySelectorAll<HTMLElement>('tr[data-status]')]
    expect(rows).toHaveLength(7)
    for (const s of ['healthy', 'limited', 'erroring', 'reauth_needed']) {
      const tile = container.querySelector(`[data-tile="${s}"] .ah-tile-n`)!
      const listed = rows.filter((r) => r.dataset.status === s).length
      expect(Number(tile.textContent), s).toBe(listed)
    }
    expect(Number(container.querySelector('[data-tile="healthy"] .ah-tile-n')!.textContent)).toBe(3)
  })

  it('sorts the accounts worst first: re-auth, erroring, limited, healthy', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const order = [...container.querySelectorAll<HTMLElement>('tr[data-status]')].map((r) => r.dataset.account)
    expect(order).toEqual([
      'eng:ai-eng-3',
      'eng:ai-eng-2',
      'eng:aieng-claude1',
      'eng:saga-personal',
      'eng:saga-team',
      // Two quiet healthy accounts: by id, so the order is stable between polls.
      'eng:devops-main',
      'eng:devops-team',
    ])
  })

  it('draws the fifth tile from the Accounts answer and links to Accounts', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const tile = container.querySelector<HTMLElement>('[data-tile="verdict"]')!
    expect(tile.textContent).toContain('Can the pool take work now?')
    expect(tile.textContent).toMatch(/Yes · 4 accounts/)
    expect(within(tile).getByRole('link', { name: 'Accounts ›' }).getAttribute('href')).toBe('#capacity/accounts')
  })

  it('opens a row on the account’s side sheet on Accounts, and shows its last event', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const row = container.querySelector<HTMLElement>('tr[data-account="eng:ai-eng-2"]')!
    expect(within(row).getByRole('link', { name: 'ai-eng-2' }).getAttribute('href')).toBe('#capacity/accounts?account=eng%3Aai-eng-2')
    expect(row.textContent).toContain('Overloaded (529)')
    expect(row.textContent).toContain('15 min ago')
  })
})

describe('F3: the swimlane', () => {
  it('draws one mark per served event, and its total agrees with the counts', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const served = [...events.values()].reduce((n, e) => n + e.length, 0)
    const marks = container.querySelectorAll('[data-mark]')
    expect(marks).toHaveLength(served)
    const note = container.querySelector<HTMLElement>('.ah-timeline [data-timeline-note]')!
    expect(note.dataset.marks).toBe(String(served))
    expect(note.textContent).toContain(`${served} events on 6 accounts in 7 days`)
    expect(container.querySelector('[data-older]')).toBeNull()
    // The pool strip sums to the same events.
    const strip = [...container.querySelectorAll<SVGGElement>('[data-pool-hour]')].reduce((n, g) => n + Number(g.dataset.poolHour), 0)
    expect(strip).toBe(served)
  })

  it('says how many older events count only in the totals when a record is past its last 50', () => {
    const { accounts, events } = pool()
    const many = Array.from({ length: 57 }, (_, i) => ev(100 + i * 60, 'network'))
    events.set('eng:saga-team', many)
    const b = board(accounts, events)

    const { container } = desktop(b)

    expect(container.querySelector<HTMLElement>('[data-older]')!.dataset.older).toBe('7')
  })

  it('draws a band per limited stretch and names the event a pressed mark is', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    expect(container.querySelectorAll('[data-lane="eng:aieng-claude1"] [data-band="five_hour"]')).toHaveLength(1)
    const mark = container.querySelector<HTMLButtonElement>('[data-mark][data-mark-of="eng:ai-eng-2"]')!
    fireEvent.click(mark)
    const picked = container.querySelector<HTMLElement>('[data-picked]')!
    expect(picked.textContent).toContain('tenant eng')
    expect(within(picked).getByRole('link').getAttribute('href')).toMatch(/^#work\/task\/task_/)
  })
})

describe('F4: the honest states', () => {
  it('says no events once, in words, when every record was read and is empty', () => {
    const { accounts } = pool()
    const quiet = accounts.map((a) => ({ ...a, health_status: 'healthy' as const, health_24h: counts([], 24) }))
    const { container } = desktop(board(quiet, new Map()))

    expect(container.querySelector('[data-empty="no-events"]')!.textContent).toContain('No events in 7 days')
    expect(container.querySelectorAll('[data-mark]')).toHaveLength(0)
    expect(container.querySelector('svg.ah-swim')).toBeNull()
  })

  it('draws a dash with its reason, never a zero, when the deployment does not serve the record', () => {
    const { accounts, events } = pool()
    const old = accounts.map((a) => {
      const { health_status: _s, health_24h: _c, ...rest } = a
      return rest as HealthAccount
    })
    const { container } = desktop(board(old, events, 'not_served'))

    expect(container.querySelector('[data-state="not-served"]')!.textContent).toContain('does not serve account health')
    for (const s of ['healthy', 'limited', 'erroring', 'reauth_needed']) {
      const n = container.querySelector(`[data-tile="${s}"] .ah-tile-n`)!
      expect(n.textContent).toBe('—')
      expect(n.querySelector('.ah-dash')!.getAttribute('title')).toMatch(/does not serve/)
    }
    const cells = [...container.querySelectorAll('tbody td.ah-num')]
    expect(cells.length).toBe(21)
    expect(cells.every((c) => c.textContent === '—')).toBe(true)
    expect(container.querySelector('[data-tile="verdict"]')!.textContent).toMatch(/Yes/)
    expect(container.querySelector('.ah-timeline')!.textContent).not.toMatch(/\b0 events/)
  })

  it('leads an account that needs re-auth with its fix, Sign in again', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))

    const callout = container.querySelector<HTMLElement>('[data-state="reauth"]')!
    expect(callout.dataset.account).toBe('eng:ai-eng-3')
    expect(within(callout).getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toBe(
      '#capacity/accounts?account=eng%3Aai-eng-3',
    )
  })

  it('says an unreadable read once and draws no lanes, keeping the list’s own statuses', () => {
    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events, 'unreadable'))

    expect(screen.getByRole('alert').textContent).toContain('The health read timed out.')
    expect(container.querySelector('svg.ah-swim')).toBeNull()
    expect(container.querySelector('.ah-timeline')!.getAttribute('data-state')).toBe('unreadable')
    expect(Number(container.querySelector('[data-tile="limited"] .ah-tile-n')!.textContent)).toBe(2)
    const last = container.querySelector('tr[data-account="eng:saga-team"] td:nth-child(6) .ah-dash')!
    expect(last.getAttribute('title')).toContain('The health read failed')
  })
})

describe('F5: the phone groups open problems first', () => {
  it('draws the switch, the summary line, the open incidents worst first with their fixes, then the closed ones', () => {
    const { accounts, events } = pool()
    const { container } = render(<AccountHealthBody board={board(accounts, events)} now={NOW} phone />)

    expect(container.querySelector('[data-layout="phone"]')).not.toBeNull()
    expect(screen.getByRole('button', { name: /Incidents · 4 open/ }).getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[data-summary]')!.textContent).toContain('4 open on 4 accounts')

    const sections = [...container.querySelectorAll<HTMLElement>('[data-section]')].map((s) => s.dataset.section)
    expect(sections).toEqual(['open', 'closed'])
    const open = [...container.querySelectorAll<HTMLElement>('[data-section="open"] [data-incident]')]
    expect(open.map((x) => x.dataset.incident)).toEqual(['cred', 'err', 'lim', 'lim'])
    expect(open.every((x) => x.dataset.open === 'true')).toBe(true)
    expect(within(open[0]!).getByRole('link', { name: 'Sign in again' })).toBeTruthy()
    expect(open[2]!.querySelector('[data-fix="clears"]')!.textContent).toMatch(/^Clears /)
    const closed = container.querySelectorAll('[data-section="closed"] [data-incident]')
    expect(closed.length).toBe(2)
  })

  it('shows the tiles, the cards and the lanes behind the Accounts switch', () => {
    const { accounts, events } = pool()
    const { container } = render(<AccountHealthBody board={board(accounts, events)} now={NOW} phone />)

    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }))

    expect(container.querySelectorAll('.ah-acard')).toHaveLength(7)
    expect(container.querySelector<HTMLElement>('.ah-acard')!.dataset.account).toBe('eng:ai-eng-3')
    expect(container.querySelector('[data-tile="verdict"]')).not.toBeNull()
  })

  it('shows a dash on the switch, not "0 open", when the record is not served', () => {
    const { accounts, events } = pool()
    render(<AccountHealthBody board={board(accounts, events, 'not_served')} now={NOW} phone />)

    const sw = screen.getByRole('button', { name: /Incidents/ })
    expect(sw.textContent).toBe('Incidents —')
  })
})

describe('review fixes: the phone, the swimlane, the list and the development pool', () => {
  it('gives an account that needs re-auth but has no credential event an open card with Sign in again', () => {
    const { accounts, events } = pool()
    // Its re-auth is older than 7 days: the record holds no events at all.
    events.set('eng:ai-eng-3', [])
    const { container } = render(<AccountHealthBody board={board(accounts, events)} now={NOW} phone />)

    expect(screen.getByRole('button', { name: /Incidents · 4 open/ })).toBeTruthy()
    const card = container.querySelector<HTMLElement>('[data-section="open"] [data-incident="cred"]')!
    expect(card.dataset.open).toBe('true')
    expect(card.textContent).toContain('ai-eng-3 needs re-auth')
    expect(card.querySelector('[data-state="no-cred-event"]')).not.toBeNull()
    expect(within(card).getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toContain('ai-eng-3')
    expect(container.textContent).not.toContain('Nothing is open on any account')
  })

  it('draws the 7 days to fit the chart’s box: the now line and every mark sit inside it', () => {
    const box = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(352)
    try {
      const { accounts, events } = pool()
      const { container } = desktop(board(accounts, events))

      const chart = container.querySelector<HTMLElement>('.ah-swim-box')!
      expect(chart.dataset.chartWidth).toBe('352')
      expect(container.querySelector('svg.ah-swim')!.getAttribute('viewBox')).toBe(`0 0 352 ${chart.style.height.replace('px', '')}`)
      const nowX = Number(container.querySelector('.ah-now')!.getAttribute('x1'))
      expect(nowX).toBeLessThanOrEqual(352)
      expect(nowX).toBeGreaterThan(340)
      const lefts = [...container.querySelectorAll<HTMLElement>('[data-mark]')].map((b) => parseFloat(b.style.left))
      expect(lefts.length).toBeGreaterThan(0)
      expect(lefts.every((l) => l >= 0 && l + 6 <= 352)).toBe(true)
    } finally {
      box.mockRestore()
    }
  })

  it('cuts the Last event message to one line with the whole of it as its title', () => {
    const { accounts, events } = pool()
    const long = 'x'.repeat(30) + ' a provider message long enough to widen the column past its card'
    events.set('eng:saga-team', [ev(600, 'network', { message: long })])
    const { container } = desktop(board(accounts, events))

    const cell = container.querySelector('tr[data-account="eng:saga-team"] td.ah-last-td')!
    const msg = cell.querySelector<HTMLElement>('.ah-msg.is-one-line')!
    expect(msg.getAttribute('title')).toBe(long)
  })

  it('tells the development pool’s re-auth story on the account whose state is REAUTH_REQUIRED', async () => {
    const r = await loadAccountHealthBoard()
    expect(r.status).toBe('ok')
    if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
    const reauth = r.data.health.pool.accounts.filter((x) => x.status === 'reauth_needed')
    expect(reauth.length).toBeGreaterThan(0)
    for (const x of reauth) expect(x.events.some((e) => e.kind === 'reauth' || e.kind === 'auth')).toBe(true)
    const healthy = r.data.health.pool.accounts.filter((x) => x.status === 'healthy')
    for (const x of healthy) expect(x.events.some((e) => e.kind === 'reauth')).toBe(false)
  })
})
