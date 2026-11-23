// VOICE IT'S SPEND CAPS, ENFORCED (the owner's decision of 23 November 2026;
// docs/design/voice-it.md §3.8; swarm-api's voice/budget.py):
//
//   * the Cost pill takes the warning tone once swarm-api warned of a cap at
//     80%, even where the console's own forms are still calm, and says so to
//     a screen reader;
//   * a cap reached takes the bad tone and shows, with no word, the time the
//     session ends, in the pill and beside that cap in the panel;
//   * the Budget card states the cap it is about, what is spent and reserved
//     against it, and when the session ends once it is reached; a planner's
//     card with no cap is held against both, as before;
//   * the card and the pill add no prose to the page, and nothing wider than
//     390 px at phone width.
//
// MUTATIONS: ignore the ledger's `budget`; draw the warned state in the bad
// tone; print "Capped" in the pill; draw the day's bar on a session card; drop
// the end time from a capped card; show the end time on a warning card.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { cascade } from './cssgate'
import { measure } from './proseapp'
import { settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { specOf, type VoiceCard } from '../voice/CardList'
import { BudgetCard } from '../voice/cards/WorkCards'
import { CostPill } from '../voice/CostPill'

const SESSION = 'vs_0002'
const ENDS = '2026-11-23T10:02:00+00:00'
const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

function cap(cap_usd: number, spent: number, reserved: number) {
  return {
    cap_usd,
    spent_usd: spent,
    reserved_usd: reserved,
    total_usd: spent + reserved,
    remaining_usd: cap_usd - spent - reserved,
    lines: [
      { line: 'talk', spent_usd: spent, reserved_usd: 0 },
      { line: 'work_running', spent_usd: 0, reserved_usd: reserved },
    ],
  }
}

/** The session at 82 of its 100 (calm by the console's own 90% forms), the day roomy. */
function ledger(budget: unknown, session = cap(100, 70, 12)) {
  return {
    session_id: SESSION,
    date: '2026-11-23',
    time_zone: 'UTC',
    session,
    day: cap(300, 90, 12),
    not_counted: ['deploys'],
    budget,
  }
}

function serve(data: unknown): void {
  fetchMock.read.mockImplementation(async () => ({ status: 'ok', data, fetchedAt: Date.now() }))
}

const pill = () => screen.getByRole('button', { name: /^Cost/ })

afterEach(() => {
  fetchMock.read.mockReset()
  fetchMock.write.mockReset()
})

describe('the cost pill, with the caps enforced', () => {
  it('shows nothing of the caps before swarm-api warned of one', async () => {
    serve(ledger({ warned: [], capped: null, ends_at: null }))
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    const host = container.querySelector('.vcp') as HTMLElement
    expect(host.dataset.tone).toBe('ok')
    expect(host.dataset.budget).toBeUndefined()
    expect(container.querySelector('.vcp-ends')).toBeNull()
  })

  it('takes the warning tone once the session cap was warned of at 80%, with no word', async () => {
    serve(ledger({ warned: ['session'], capped: null, ends_at: null }))
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    const host = container.querySelector('.vcp') as HTMLElement
    expect(host.dataset.tone).toBe('warn')
    expect(host.dataset.budget).toBe('warned')
    const figs = [...container.querySelectorAll('.vcp-fig')]
    expect(figs.map((f) => f.className)).toEqual(['vcp-fig is-warn', 'vcp-fig is-ok'])
    expect(figs[0]?.querySelector('.vcp-mark.is-warn svg')).not.toBeNull()
    const [, sessionArc] = [...container.querySelectorAll('.vcp-arc')]
    expect(sessionArc?.getAttribute('class')).toBe('vcp-arc is-warn')
    expect(pill().getAttribute('aria-label')).toBe('Cost: this session $82.00 of $100, today $102.00 of $300, nearly at a cap')
    expect((host.textContent ?? '').replace(/[$\d.,/:\s]/g, '')).toBe('SessionToday')
  })

  it('a reached cap takes the bad tone and shows when the session ends, in the pill and the panel', async () => {
    serve(ledger({ warned: ['session'], capped: 'session', ends_at: ENDS }, cap(100, 88, 12)))
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    const host = container.querySelector('.vcp') as HTMLElement
    expect(host.dataset.tone).toBe('bad')
    expect(host.dataset.budget).toBe('capped')
    const ends = pill().querySelector('time.vcp-ends') as HTMLElement
    expect(ends.getAttribute('datetime')).toBe(ENDS)
    expect(ends.textContent).toBe(clock(ENDS))
    expect(pill().getAttribute('aria-label')).toBe(
      `Cost: this session $100.00 of $100, today $102.00 of $300, the session cap is reached: the session ends at ${clock(ENDS)}`,
    )
    // No status word in the pill: the figures, the time and the two labels only.
    expect((host.textContent ?? '').replace(/[$\d.,/:\s]/g, '').replace(/AM|PM/gi, '')).toBe('SessionToday')

    fireEvent.click(pill())
    const panel = screen.getByRole('region', { name: 'Cost' })
    const session = within(panel).getByRole('region', { name: 'This session' })
    const day = within(panel).getByRole('region', { name: 'Today' })
    expect(session.querySelector('time.vcp-ends')?.textContent).toBe(clock(ENDS))
    // Why it is that time is behind the time's tooltip, not on the page.
    expect(session.querySelector('time.vcp-ends')?.getAttribute('title')).toContain('the session ends at')
    expect(day.querySelector('time.vcp-ends')).toBeNull()
    expect(session.querySelector('.vcp-block-fig')?.className).toBe('vcp-block-fig is-bad')
  })

  it('a reached day cap marks the day, not the session', async () => {
    serve(ledger({ warned: ['day'], capped: 'day', ends_at: ENDS }))
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    const figs = [...container.querySelectorAll('.vcp-fig')]
    expect(figs.slice(0, 2).map((f) => f.className)).toEqual(['vcp-fig is-ok', 'vcp-fig is-bad'])
  })

  it('reads an answer without `budget` as nothing warned', async () => {
    const { budget: _none, ...old } = ledger(null)
    serve(old)
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    expect((container.querySelector('.vcp') as HTMLElement).dataset.budget).toBeUndefined()
  })
})

function budgetCard(body: Record<string, unknown>): VoiceCard {
  return {
    card_id: 'vc_b',
    kind: 'budget',
    version: 1,
    state: 'waiting',
    title: 'Session budget reached',
    summary: '$88.00 spent and $12.00 reserved of the $100 session cap.',
    created_by: 'platform',
    references: {},
    body,
  } as unknown as VoiceCard
}

const BODY = {
  lines: [
    { label: 'Talk', spent_usd: 88, reserved_usd: 0 },
    { label: 'Work running', spent_usd: 0, reserved_usd: 12 },
  ],
  spent_usd: 88,
  reserved_usd: 12,
  session_cap_usd: 100,
  day_cap_usd: 300,
  not_counted: ['deploys'],
}

function drawCard(body: Record<string, unknown>) {
  const spec = specOf('budget')
  if (spec === null) throw new Error('no budget kind in the catalogue')
  const c = budgetCard(body)
  return render(<BudgetCard card={c} spec={spec} body={body} />)
}

describe('the Budget card', () => {
  it('the catalogue names the cap, the status and when the session ends', () => {
    expect(specOf('budget')?.fields).toEqual(expect.arrayContaining(['status', 'cap', 'ends_at']))
  })

  it('a reached cap states that cap, what is spent and reserved, and when the session ends', () => {
    const { container } = drawCard({ ...BODY, status: 'capped', cap: 'session', ends_at: ENDS })
    const bars = [...container.querySelectorAll('[aria-label]')].map((e) => e.getAttribute('aria-label'))
    expect(bars.some((l) => l?.includes('session cap'))).toBe(true)
    expect(bars.some((l) => l?.includes('day cap'))).toBe(false)
    const ends = container.querySelector('time') as HTMLElement
    expect(ends.getAttribute('datetime')).toBe(ENDS)
    expect(ends.textContent).toBe(clock(ENDS))
    const rows = [...container.querySelectorAll('tbody tr')].map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent))
    expect(rows).toEqual([
      ['Talk', '$88', '$0'],
      ['Work running', '$0', '$12'],
    ])
  })

  it('a warning states its cap and no end', () => {
    const { container } = drawCard({ ...BODY, status: 'warning', cap: 'day', ends_at: null })
    const bars = [...container.querySelectorAll('[aria-label]')].map((e) => e.getAttribute('aria-label'))
    expect(bars.some((l) => l?.includes('day cap'))).toBe(true)
    expect(bars.some((l) => l?.includes('session cap'))).toBe(false)
    expect(container.querySelector('time[datetime="' + ENDS + '"]')).toBeNull()
  })

  it("a planner's card without a cap is held against both, as before", () => {
    const { container } = drawCard(BODY)
    const bars = [...container.querySelectorAll('[aria-label]')].map((e) => e.getAttribute('aria-label'))
    expect(bars.some((l) => l?.includes('session cap'))).toBe(true)
    expect(bars.some((l) => l?.includes('day cap'))).toBe(true)
  })

  it('the prose guard: a capped card adds one time to the warning card, no sentence', () => {
    const warned = drawCard({ ...BODY, status: 'warning', cap: 'session', ends_at: null })
    const before = measure(warned.container).words
    warned.unmount()
    const capped = drawCard({ ...BODY, status: 'capped', cap: 'session', ends_at: ENDS })
    expect(measure(capped.container).words - before).toBeLessThanOrEqual(3)
  })

  it('nothing in the capped card is fixed wider than a phone', () => {
    const styles = join(__dirname, '..', 'styles')
    const sheets = [readFileSync(join(__dirname, '..', 'styles.css'), 'utf8'), readFileSync(join(styles, 'cost-pill.css'), 'utf8')].join('\n')
    const { container } = drawCard({ ...BODY, status: 'capped', cap: 'session', ends_at: ENDS })
    const px = (v: string | null) => (v === null ? 0 : [...v.replace(/minmax\([^)]*\)|calc\([^)]*\)/g, '').matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((n, m) => n + parseFloat(m[1]!), 0))
    const wide = [container, ...container.querySelectorAll('*')].flatMap((el) =>
      ['width', 'min-width', 'flex-basis'].filter((p) => px(cascade(sheets, el, p, { width: 390 }).winner?.value ?? null) > 390 - 2 * 18),
    )
    expect(wide).toEqual([])
  })
})
