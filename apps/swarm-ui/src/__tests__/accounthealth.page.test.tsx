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

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AccountHealthBody } from '../AccountHealthPage'
import { loadAccountHealthBoard, type AccountHealthBoard } from '../api'
import {
  HEALTH_KINDS,
  poolIncidents,
  statusOf,
  worstFirst,
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
    ['example-team', 'healthy', [ev(600, 'network')]],
    ['aieng-claude1', 'limited', [ev(80, 'rate_limit', { window: 'five_hour', resetIn: 120 }), ev(40, 'assign_refused', { window: 'five_hour' })]],
    ['ai-eng-2', 'erroring', [ev(30, 'overloaded'), ev(25, 'overloaded'), ev(20, 'server_error'), ev(15, 'overloaded')]],
    ['ai-eng-3', 'reauth_needed', [ev(62, 'auth'), ev(61, 'reauth'), ev(30, 'assign_refused')]],
    ['devops-main', 'healthy', []],
    ['devops-team', 'healthy', [ev(3000, 'overloaded')]],
    ['example-personal', 'limited', [ev(200, 'rate_limit', { window: 'seven_day', resetIn: 1500 })]],
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
      'eng:example-personal',
      'eng:example-team',
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
    events.set('eng:example-team', many)
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
    const last = container.querySelector('tr[data-account="eng:example-team"] td:nth-child(6) .ah-dash')!
    const why = last.getAttribute('title')!
    expect(why).toContain('The health read timed out.')
    // said once: the reason already names the read, so it is not prefixed again
    expect(why.match(/the health read/gi)).toHaveLength(1)
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
    // The scroller is padded at its sides (account-health.css gives it
    // --ctl-pad-chrome, 16 px), and clientWidth counts that padding. Its CONTENT
    // box is 384 - 2 x 16 = 352: a chart sized to clientWidth started 16 px in
    // and ended 32 px past it, which hid the newest hour and cut "now 12:00Z".
    const pad = document.createElement('style')
    pad.textContent = '.ah-chart-scroll { padding-left: 16px; padding-right: 16px; }'
    document.head.appendChild(pad)
    const box = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(384)
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
      pad.remove()
    }
  })

  it('draws the Last event message whole, wrapping inside its held column (QA1-FIX-01 chrome-C26)', () => {
    const { accounts, events } = pool()
    const long = 'x'.repeat(30) + ' a provider message long enough to widen the column past its card'
    events.set('eng:example-team', [ev(600, 'network', { message: long })])
    const { container } = desktop(board(accounts, events))

    const cell = container.querySelector('tr[data-account="eng:example-team"] td.ah-last-td')!
    const msg = cell.querySelector<HTMLElement>('.ah-msg')!
    // MUTATION: cut it to one line again (a title no keyboard or touch reader can open).
    expect(msg.classList.contains('is-one-line')).toBe(false)
    expect(msg.textContent).toBe(long)
  })

  it('lets a Limited row’s reset time wrap under its word, so the table fits its card and keeps "Sign in again ›"', () => {
    // At 1440 the table was 1049 px in a 1037 px scroller: "Limited · 7-day
    // until …" sat in the nowrap status chip, and the overflow cut the
    // re-auth row's "Sign in again ›" at the card's edge. jsdom lays nothing
    // out, so this pins the rule that lets the column give way: the chip
    // holds the word alone, the reset time is a sibling that may wrap.
    const css = readFileSync(join(__dirname, '..', 'styles', 'account-health.css'), 'utf8')
    const { accounts, events } = pool()
    const b = board(accounts, events)
    if (b.health.kind !== 'ok') throw new Error('the board has no health')
    const r = b.health.pool.accounts.find((x) => x.account_id === 'eng:example-personal')!
    r.limited_until = new Date(NOW + 1500 * MIN).toISOString()
    const { container } = desktop(b)

    const cell = container.querySelector('tr[data-account="eng:example-personal"] td:nth-child(2)')!
    const chip = cell.querySelector<HTMLElement>('.ah-st')!
    expect(chip.textContent).toBe('Limited')
    const until = cell.querySelector<HTMLElement>('.ah-st-until')!
    expect(until.textContent).toMatch(/^7-day until /)
    expect(chip.contains(until)).toBe(false)
    const rule = /\.ah-st-until\s*\{([^}]*)\}/.exec(css)
    expect(rule).not.toBeNull()
    expect(rule![1]).not.toMatch(/nowrap/)
    expect(rule![1]).toMatch(/display:\s*block/)
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

  it('never shows an open limit card on the phone for an account whose row says Healthy, in the development pool', async () => {
    // The broker's rule 2 (accountstore.health_status): a 429 short of its
    // reset is `limited`. weekly-burn's 7-day hit resets in 20 h, so a
    // fixture that called it healthy drew a "Rate-limited" card beside a
    // Healthy row: the two halves of one page disagreeing.
    const r = await loadAccountHealthBoard()
    if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
    const row = new Map((r.data.page.accounts as HealthAccount[]).map((a) => [a.account_id, a.health_status]))
    const { open } = poolIncidents(r.data.health.pool, (id) => (row.get(id) ?? null) as HealthStatus | null, r.data.readAt)
    const limits = open.filter((x) => x.family === 'lim')
    expect(limits.length).toBeGreaterThan(0)
    for (const x of limits) expect([x.account_id, row.get(x.account_id)]).toEqual([x.account_id, 'limited'])

    const { container } = render(<AccountHealthBody board={r.data} now={r.data.readAt} phone />)
    for (const x of limits) expect(container.textContent).toContain(`${x.label} rate-limited`)
  })
})

describe('fourth review: the marks in their lanes, the phone’s switch and the re-auth fix from the row’s state', () => {
  const LANE_H = 46

  /** A CSS rule's declarations, by its exact selector. */
  function ruleOf(css: string, selector: string): string | null {
    const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const m = new RegExp(`(?:^|\\})\\s*${esc}\\s*\\{([^}]*)\\}`, 'm').exec(css)
    return m === null ? null : m[1]!
  }
  function px(decls: string | null, prop: string): number | null {
    if (decls === null) return null
    const m = new RegExp(`(?:^|;|\\s)${prop}\\s*:\\s*([0-9.]+)(px)?\\s*(;|$)`, 'm').exec(decls)
    return m === null ? null : Number(m[1])
  }

  it('draws every mark button at the mark’s own height, so its centre is the lane position the chart computes', () => {
    // The console's `:where(.app button) { min-height: 28px }` made each 12 px
    // `.ah-hit` 28 px tall, and its grid-centred mark sat 8 px low: a
    // credential square of the last lane but one hung into the lane below it.
    const styles = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')
    const css = readFileSync(join(__dirname, '..', 'styles', 'account-health.css'), 'utf8')
    const globalMin = px(ruleOf(styles, ':where(.app button)'), 'min-height')
    expect(globalMin).toBe(28)
    const hit = ruleOf(css, '.ah-hit')
    const height = px(hit, 'height')!
    const ownMin = px(hit, 'min-height')
    const drawn = Math.max(height, ownMin ?? globalMin!)
    expect(drawn).toBe(12)

    const { accounts, events } = pool()
    const { container } = desktop(board(accounts, events))
    const marks = [...container.querySelectorAll<HTMLElement>('[data-mark]')]
    expect(marks.length).toBeGreaterThan(0)
    let visited = 0
    for (const b of marks) {
      const lane = container.querySelector(`[data-lane="${b.dataset.markOf}"] rect.ah-lane`)!
      const top = Number(lane.getAttribute('y'))
      const bottom = top + Number(lane.getAttribute('height'))
      const y = parseFloat(b.style.top)
      // The whole button, outline and all, inside its own lane.
      expect(y).toBeGreaterThanOrEqual(top)
      expect(y + drawn).toBeLessThanOrEqual(bottom)
      // errors high, limits on the line, credential low
      const centre = y + drawn / 2
      const line = top - 3 + LANE_H / 2 + 5
      expect(centre - line).toBe(b.dataset.mark === 'err' ? -8 : b.dataset.mark === 'lim' ? 0 : 8)
      visited += 1
    }
    expect(visited).toBe(marks.length)
  })

  it('keeps the newest marks inside the chart’s right edge on a 390 phone', () => {
    const box = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(324)
    try {
      const { accounts, events } = pool()
      // One event at the read itself: the newest a mark can be.
      events.set('eng:example-team', [ev(0, 'network')])
      const { container } = desktop(board(accounts, events))
      const W = Number(container.querySelector<HTMLElement>('.ah-swim-box')!.dataset.chartWidth)
      expect(W).toBe(324)
      const marks = [...container.querySelectorAll<HTMLElement>('[data-mark]')]
      expect(marks.length).toBeGreaterThan(0)
      const rights = marks.map((b) => parseFloat(b.style.left) + 12)
      expect(Math.max(...rights)).toBeLessThanOrEqual(W)
      expect(Number(container.querySelector('.ah-now')!.getAttribute('x1')) + 6).toBeLessThanOrEqual(W)
    } finally {
      box.mockRestore()
    }
  })

  it('never commits a frame of the chart wider than its box: the first one is drawn at the measured width', () => {
    const box = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(324)
    const host = document.body.appendChild(document.createElement('div'))
    const seen = new MutationObserver(() => undefined)
    seen.observe(host, { subtree: true, childList: true, attributes: true, attributeOldValue: true, attributeFilter: ['data-chart-width'] })
    try {
      const { accounts, events } = pool()
      render(<AccountHealthBody board={board(accounts, events)} now={NOW} phone={false} />, { container: host })
      const records = seen.takeRecords()
      const widths: string[] = []
      for (const r of records) {
        if (r.type === 'attributes' && r.oldValue !== null) widths.push(r.oldValue)
        for (const n of r.addedNodes) {
          if (!(n instanceof HTMLElement)) continue
          for (const el of [n, ...n.querySelectorAll<HTMLElement>('[data-chart-width]')])
            if (el.dataset.chartWidth !== undefined) widths.push(el.dataset.chartWidth)
        }
      }
      expect(widths.length).toBeGreaterThan(0)
      expect(widths.every((w) => w === '324')).toBe(true)
    } finally {
      seen.disconnect()
      box.mockRestore()
      host.remove()
    }
  })

  it('keeps the phone’s Incidents/Accounts choice across a resize that crosses the phone breakpoint', () => {
    const { accounts, events } = pool()
    const b = board(accounts, events)
    const { rerender } = render(<AccountHealthBody board={b} now={NOW} phone />)
    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }))
    expect(screen.getByRole('button', { name: 'Accounts' }).getAttribute('aria-pressed')).toBe('true')

    rerender(<AccountHealthBody board={b} now={NOW} phone={false} />)
    expect(document.querySelector('[data-layout="desktop"]')).not.toBeNull()
    rerender(<AccountHealthBody board={b} now={NOW} phone />)

    expect(screen.getByRole('button', { name: 'Accounts' }).getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelectorAll('.ah-acard')).toHaveLength(7)
  })

  it('shows a re-auth card with Sign in again in Incidents from the list’s own status when the health read failed', () => {
    const { accounts, events } = pool()
    const { container } = render(<AccountHealthBody board={board(accounts, events, 'unreadable')} now={NOW} phone />)

    expect(screen.getByRole('button', { name: /Incidents/ }).getAttribute('aria-pressed')).toBe('true')
    expect(container.querySelector('[data-sum="reauth_needed"]')!.textContent).toContain('1')
    const card = container.querySelector<HTMLElement>('[data-state="reauth"]')!
    expect(card.dataset.account).toBe('eng:ai-eng-3')
    expect(within(card).getByRole('link', { name: 'Sign in again' }).getAttribute('href')).toBe(
      '#capacity/accounts?account=eng%3Aai-eng-3',
    )
  })

  it('leads a REAUTH_REQUIRED row with its fix when the deployment does not serve health, keeping its figures as dashes', () => {
    const { accounts, events } = pool()
    const old = accounts.map((a) => {
      const { health_status: _s, health_24h: _c, ...rest } = a
      return rest as HealthAccount
    })
    const { container } = desktop(board(old, events, 'not_served'))

    const callout = container.querySelector<HTMLElement>('[data-state="reauth"]')!
    expect(callout.dataset.account).toBe('eng:ai-eng-3')
    expect(within(callout).getByRole('link', { name: 'Sign in again' })).toBeTruthy()
    const row = container.querySelector<HTMLElement>('tr[data-account="eng:ai-eng-3"]')!
    expect(row.dataset.status).toBe('reauth_needed')
    expect(within(row).getByRole('link', { name: 'Sign in again ›' })).toBeTruthy()
    const nums = [...row.querySelectorAll<HTMLElement>('td.ah-num')]
    expect(nums).toHaveLength(3)
    for (const c of nums) {
      expect(c.textContent).toBe('—')
      expect(c.querySelector('.ah-dash')!.getAttribute('title')).toMatch(/does not serve/)
    }
    // Only the account whose state says so: the others stay unknown.
    expect(container.querySelectorAll('[data-state="reauth"]')).toHaveLength(1)
    expect(container.querySelector('tr[data-account="eng:example-team"]')!.getAttribute('data-status')).toBe('unknown')
  })

  it('starts the failed read’s sentence with a capital letter when the server’s reason does not', () => {
    const { accounts, events } = pool()
    const b = board(accounts, events, 'unreadable')
    b.health = { kind: 'unreadable', reason: 'upstream timed out after 10 s' }
    desktop(b)

    expect(screen.getByRole('alert').querySelector('p')!.textContent).toMatch(/^Upstream timed out after 10 s\. /)
  })
})

describe('REAUTH-MODEL: the re-auth fallback from the row’s state is the model’s rule (part of #2391)', () => {
  // BREAK IT: move the REAUTH_REQUIRED fallback back into the page's cellOf
  // and statusOf answers null for this row, worstFirst sorts it last among the
  // unknowns, and the list leads with a healthy account -- each case turns red.

  /** A pool that serves health, with one REAUTH_REQUIRED account whose health is null. */
  function mixed(): { accounts: HealthAccount[]; events: Map<string, HealthEvent[]> } {
    const { accounts, events } = pool()
    const out = accounts.map((a) =>
      a.account_id === 'eng:ai-eng-3' ? { ...a, health_status: null, state: 'REAUTH_REQUIRED' as const } : a,
    )
    return { accounts: out, events }
  }

  it('statusOf reads an account whose health is null and whose state is REAUTH_REQUIRED as needing re-auth', () => {
    const { accounts, events } = mixed()
    const a = accounts.find((x) => x.account_id === 'eng:ai-eng-3')!
    const r = { ...rec(a, events.get(a.account_id) ?? []), status: null }
    expect(statusOf(a, r)).toEqual({ status: 'reauth_needed' })
    expect(statusOf(a, undefined)).toEqual({ status: 'reauth_needed' })
    // The broker's word still wins over the state, and an AVAILABLE row with no word stays unknown.
    expect(statusOf({ ...a, health_status: 'healthy' }, r)).toEqual({ status: 'healthy' })
    expect(statusOf({ ...a, state: 'AVAILABLE' }, r).status).toBeNull()
  })

  it('worstFirst puts that account first', () => {
    const { accounts, events } = mixed()
    const records = new Map(accounts.map((a) => [a.account_id, { ...rec(a, events.get(a.account_id) ?? []), status: null }]))
    expect(worstFirst(accounts, records)[0]!.account_id).toBe('eng:ai-eng-3')
  })

  it('sorts that row first on the page and counts it once in the tiles', () => {
    const { accounts, events } = mixed()
    const { container } = desktop(board(accounts, events))

    const rows = [...container.querySelectorAll<HTMLElement>('tr[data-status]')]
    expect(rows[0]!.dataset.account).toBe('eng:ai-eng-3')
    expect(rows[0]!.dataset.status).toBe('reauth_needed')
    expect(Number(container.querySelector('[data-tile="reauth_needed"] .ah-tile-n')!.textContent)).toBe(1)
    expect(rows.filter((r) => r.dataset.status === 'reauth_needed')).toHaveLength(1)
    expect(container.querySelectorAll('[data-state="reauth"]')).toHaveLength(1)
  })
})
