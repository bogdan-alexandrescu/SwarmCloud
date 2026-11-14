// VOICE IT'S COST PILL in Talk's header row (the owner's pick of 11 November
// 2026, "Header pill"; docs/design/voice-it.md §3.8).
//
//   * the pill shows this session's cost and today's total against the day's
//     cap, the figures swarm-api's ledger serves (it sums nothing);
//   * pressing it opens a panel with every ledger line, spent and reserved;
//     what the figures leave out is behind the panel's (i);
//   * near a cap the ring and the figure take the warning colour;
//   * a ledger read that fails says so in the pill, with no figure;
//   * the big budget meter beside the pane is gone and the pane's words went
//     down, not up (the prose guard);
//   * on a phone (390) the pill takes its own row and nothing is wider than
//     the viewport.
//
// MUTATIONS: show the day's spent alone as its total; drop the reserved
// column; draw the warning tone at 50% of a cap; print $0.00 when the read
// fails; put the meter back beside the pane; fix the panel at 360px on a phone.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { cascade } from './cssgate'
import { measure } from './proseapp'
import { installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { CostPill } from '../voice/CostPill'
import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'

const SESSION = 'vs_0001'

function cap(cap_usd: number, talk: number, finished: number, reserved: number) {
  const spent = talk + finished
  return {
    cap_usd,
    spent_usd: spent,
    reserved_usd: reserved,
    total_usd: spent + reserved,
    remaining_usd: cap_usd - spent - reserved,
    lines: [
      { line: 'talk', spent_usd: talk, reserved_usd: 0 },
      { line: 'planner', spent_usd: 0, reserved_usd: 0 },
      { line: 'speech_machine', spent_usd: 0, reserved_usd: 0 },
      { line: 'work_running', spent_usd: 0, reserved_usd: reserved },
      { line: 'work_finished', spent_usd: finished, reserved_usd: 0 },
      { line: 'followups', spent_usd: 0, reserved_usd: 0 },
    ],
  }
}

const LEDGER = {
  session_id: SESSION,
  date: '2026-11-02',
  time_zone: 'Europe/London',
  session: cap(100, 1.5, 2, 9),
  day: cap(300, 4.25, 30, 60),
  not_counted: ['pod compute for lanes', 'CI runner minutes', 'deploys'],
}

/** The session at 95 of its 100: nearly full, the day still roomy. */
const NEAR = { ...LEDGER, session: cap(100, 40, 45, 10), day: cap(300, 60, 45, 10) }

function serveLedger(data: unknown = LEDGER): void {
  fetchMock.read.mockImplementation(async () => ({ status: 'ok', data, fetchedAt: Date.now() }))
}

const pill = () => screen.getByRole('button', { name: /^Cost/ })

afterEach(() => {
  fetchMock.read.mockReset()
  fetchMock.write.mockReset()
  vi.useRealTimers()
})

describe('the cost pill', () => {
  it("shows the session's cost and today's total against the day's cap, as served", async () => {
    serveLedger()
    render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)

    expect(fetchMock.read.mock.calls[0]?.[0].url).toBe(`/v1/voice/sessions/${SESSION}/ledger`)
    const p = pill()
    expect(p.getAttribute('aria-expanded')).toBe('false')
    // Session: 1.50 + 2.00 spent + 9.00 reserved; today 34.25 spent + 60.00 reserved, of 300.
    expect(p.textContent).toContain('Session$12.50')
    expect(p.textContent).toContain('Today$94.25/ $300')
    expect(p.getAttribute('aria-label')).toBe('Cost: this session $12.50 of $100, today $94.25 of $300')
    expect(p.querySelector('svg.vcp-ring')).not.toBeNull()
    // Nothing of the panel until it is pressed.
    expect(screen.queryByRole('region', { name: 'Cost' })).toBeNull()
  })

  it('reads the ledger again while the session is live, the way the meter did', async () => {
    vi.useFakeTimers()
    serveLedger()
    render(<CostPill sessionId={SESSION} live />)
    await settle(act)
    const before = fetchMock.read.mock.calls.length
    await act(async () => {
      vi.advanceTimersByTime(30_000)
    })
    expect(fetchMock.read.mock.calls.length).toBe(before + 1)
  })

  it('opens a panel that lists every ledger line, spent and reserved, for the session and the day', async () => {
    serveLedger()
    render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    fireEvent.click(pill())
    expect(pill().getAttribute('aria-expanded')).toBe('true')

    const panel = screen.getByRole('region', { name: 'Cost' })
    const session = within(panel).getByRole('region', { name: 'This session' })
    const day = within(panel).getByRole('region', { name: 'Today' })
    const rows = (block: HTMLElement) =>
      within(block)
        .getAllByRole('row')
        .map((r) => [...r.querySelectorAll('th, td')].map((c) => c.textContent))
    expect(rows(session)).toEqual([
      ['Line', 'Spent', 'Reserved'],
      ['Talk', '$1.50', '–'],
      ['Planner', '$0.00', '–'],
      ['Speech machine', '$0.00', '–'],
      ['Work running', '$0.00', '$9.00'],
      ['Work finished', '$2.00', '–'],
      ['Follow-ups', '$0.00', '–'],
      ['Left', '$87.50'],
    ])
    expect(rows(day)).toContainEqual(['Work running', '$0.00', '$60.00'])
    expect(rows(day)).toContainEqual(['Work finished', '$30.00', '–'])
    expect(rows(day)).toContainEqual(['Left', '$205.75'])
    expect(within(day).getByText('2026-11-02 · Europe/London')).toBeTruthy()
    expect(within(session).getByRole('img').getAttribute('aria-label')).toBe('$3.50 spent and $9.00 reserved of $100')

    // What is not counted sits behind the (i), not on the surface.
    expect(measure(panel).tokens.join(' ')).not.toContain('pod')
    const info = within(panel).getByRole('button', { name: 'What is not counted' })
    expect(info.closest('.c-tip-host')?.textContent).toContain('Not counted: pod compute for lanes, CI runner minutes, deploys.')

    // Escape closes it and gives focus back to the pill.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('region', { name: 'Cost' })).toBeNull()
    expect(document.activeElement).toBe(pill())
  })

  it('names no "Not counted" when the ledger leaves nothing out; the (i) still says what reserved is', async () => {
    serveLedger({ ...LEDGER, not_counted: [] })
    render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    fireEvent.click(pill())
    const panel = screen.getByRole('region', { name: 'Cost' })
    expect(panel.textContent).not.toContain('Not counted')
    expect(within(panel).queryByRole('button', { name: 'What is not counted' })).toBeNull()
    const info = within(panel).getByRole('button', { name: 'What reserved means' })
    expect(info.closest('.c-tip-host')?.textContent).toContain('Reserved is running work, held at its step budgets.')
  })

  it('takes the warning colour, ring and figure, near a cap and not before', async () => {
    serveLedger(NEAR)
    const { container, unmount } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    const host = container.querySelector('.vcp') as HTMLElement
    expect(host.dataset.tone).toBe('warn')
    expect(pill().getAttribute('aria-label')).toBe('Cost: this session $95.00 of $100, today $115.00 of $300, nearly at a cap')
    const [dayArc, sessionArc] = [...container.querySelectorAll('.vcp-arc')]
    expect(sessionArc?.getAttribute('class')).toBe('vcp-arc is-warn')
    expect(dayArc?.getAttribute('class')).toBe('vcp-arc is-ok')
    const figs = [...container.querySelectorAll('.vcp-fig')]
    expect(figs.map((f) => f.className)).toEqual(['vcp-fig is-warn', 'vcp-fig is-ok'])
    expect(figs[0]?.querySelector('.vcp-mark.is-warn svg')).not.toBeNull()
    unmount()

    serveLedger()
    const calm = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    expect((calm.container.querySelector('.vcp') as HTMLElement).dataset.tone).toBe('ok')
    expect(calm.container.querySelectorAll('.is-warn')).toHaveLength(0)
  })

  it('says in the pill that the ledger could not be read, with no figure', async () => {
    fetchMock.read.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: "voice session 'vs_0001' not found" },
    })
    const { container } = render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)

    const p = pill()
    expect(p.textContent).not.toMatch(/\$|\d/)
    expect(p.textContent).toContain('Unread')
    expect(p.getAttribute('aria-label')).toBe("Cost: the ledger could not be read: voice session 'vs_0001' not found")
    expect((container.querySelector('.vcp') as HTMLElement).dataset.tone).toBe('bad')
    fireEvent.click(p)
    const panel = screen.getByRole('region', { name: 'Cost' })
    expect(within(panel).getByRole('alert').textContent).toBe("voice session 'vs_0001' not found")
    expect(panel.textContent).not.toMatch(/\$/)
  })

  it('refuses an answer that is not a ledger, in the pill', async () => {
    serveLedger({ session: {}, day: {} })
    render(<CostPill sessionId={SESSION} live={false} />)
    await settle(act)
    expect(pill().textContent).not.toMatch(/\$|\d/)
    expect(pill().getAttribute('aria-label')).toBe("Cost: the ledger could not be read: swarm-api's answer was not a ledger.")
  })
})

describe('the Talk pane carries the pill in its header row', () => {
  beforeEach(() => {
    installMedia({ audioContext: true })
    serveLedger()
    fetchMock.write.mockImplementation(async (target: { url: string }) => {
      if (target.url === '/v1/voice/sessions') {
        return { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
      }
      return { status: 'ok', data: null, fetchedAt: Date.now() }
    })
  })

  it('shows no pill before a session; once it starts, the pill sits beside End and the meter is gone', async () => {
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    expect(screen.queryByRole('button', { name: /^Cost/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)

    const actions = container.querySelector('.vt-hud .vt-hud-actions') as HTMLElement
    const p = within(actions).getByRole('button', { name: /^Cost/ })
    expect(p.closest('.vcp')?.nextElementSibling?.textContent).toBe('End')
    expect(screen.queryByRole('region', { name: 'Budget' })).toBeNull()
    expect(container.querySelector('.vt-meter')).toBeNull()
  })

  it("the prose guard: the live pane's words went down with the meter gone", async () => {
    const { container } = render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)
    // The live pane on this ledger, measured with the budget meter beside it
    // (main at 032085c): 76 words. The pill carries figures and two labels.
    const words = measure(container.querySelector('.vt') as HTMLElement).words
    expect(words).toBeLessThan(76)
    expect((container.querySelector('.vcp')?.textContent ?? '').replace(/[$\d.,/\s]/g, '')).toBe('SessionToday')
  })
})

describe('on a phone (390) the pill takes its own row', () => {
  const styles = join(__dirname, '..', 'styles')
  const SHEETS = [
    readFileSync(join(styles, 'talk-stage.css'), 'utf8'),
    readFileSync(join(styles, 'talk-pane.css'), 'utf8'),
    readFileSync(join(styles, 'cost-pill.css'), 'utf8'),
    readFileSync(join(__dirname, '..', 'styles.css'), 'utf8'),
  ].join('\n')
  const won = (el: Element, prop: string | readonly string[], width: number) => cascade(SHEETS, el, prop, { width }).winner?.value ?? null
  const fixedPx = (v: string | null) => (v === null ? 0 : [...v.replace(/minmax\([^)]*\)|calc\([^)]*\)/g, '').matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((n, m) => n + parseFloat(m[1]!), 0))
  const tooWide = (root: Element, width: number) =>
    [root, ...root.querySelectorAll('*')].flatMap((el) =>
      ['width', 'min-width', 'flex-basis'].filter((p) => fixedPx(won(el, p, width)) > 390 - 2 * 18).map((p) => `${el.getAttribute('class')} ${p}`),
    )

  it('a whole row of its own, the panel the row’s width, nothing wider than the viewport', async () => {
    serveLedger()
    const { container } = render(
      <div className="vt">
        <div className="vt-hud">
          <p className="vt-status">Live</p>
          <span className="vt-hud-actions">
            <CostPill sessionId={SESSION} live={false} />
            <button type="button">End</button>
          </span>
        </div>
      </div>,
    )
    await settle(act)
    fireEvent.click(pill())
    const host = container.querySelector('.vcp') as HTMLElement
    const panel = container.querySelector('.vcp-panel') as HTMLElement

    expect(won(host, 'flex', 390)).toBe('1 1 100%')
    expect(won(host, 'order', 390)).toBe('1')
    expect(won(container.querySelector('.vt-hud-actions')!, 'display', 390)).toBe('contents')
    expect(won(panel, 'width', 390)).toBe('auto')
    expect(won(panel, 'max-width', 390)).toBe('none')
    expect([won(panel, 'left', 390), won(panel, 'right', 390)]).toEqual(['0', '0'])
    // On a phone the panel draws both caps side by side in one table; at 1440, the two blocks.
    expect(won(panel.querySelector('.vcp-both')!, 'display', 390)).toBe('table')
    expect(won(panel.querySelector('.vcp-blocks')!, 'display', 390)).toBe('none')
    expect(won(panel.querySelector('.vcp-both')!, 'display', 1440)).toBe('none')
    expect(tooWide(container, 390)).toEqual([])
    // At 1440 the panel is a fixed 360 that never passes the viewport.
    expect(won(panel, 'width', 1440)).toBe('360px')
    expect(won(panel, 'max-width', 1440)).toBe('calc(100vw - 24px)')
  })

  it('the rules name console tokens, never a literal colour, so both themes hold', () => {
    const sheet = readFileSync(join(styles, 'cost-pill.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
    expect(sheet).not.toMatch(/#[0-9a-f]{3,8}\b|hsla?\(/i)
    expect(sheet.match(/rgba?\([^)]*\)/g) ?? []).toEqual(['rgb(0 0 0 / 0.25)'])
    expect(sheet).not.toMatch(/MOCK|paused|is-still/)
  })
})
