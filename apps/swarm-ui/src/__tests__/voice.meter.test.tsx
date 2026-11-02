// VOICE IT'S BUDGET METER beside the Talk pane (docs/design/voice-it.md §3.8;
// section 10, phase three's ledger row).
//
//   * the meter reads swarm-api's ledger for the session and draws the
//     session against $100 and the day against $300, each as spent plus
//     reserved, with the lines that make them up;
//   * it sums nothing of its own: the figures are the served ones;
//   * a refusal from swarm-api is shown in its own words;
//   * the Talk pane shows the meter once a session has started.
//
// MUTATIONS: read the ledger of another session id; draw the day against
// the session's cap; drop the reserved figure from a bar's words.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { BudgetMeter, asLedger } from '../voice/Meter'
import { TalkPane } from '../voice/Talk'

const SESSION = 'vs_0001'

function cap(cap_usd: number, spent: number, reserved: number) {
  return {
    cap_usd,
    spent_usd: spent,
    reserved_usd: reserved,
    total_usd: spent + reserved,
    remaining_usd: cap_usd - spent - reserved,
    lines: [
      { line: 'talk', spent_usd: spent, reserved_usd: 0 },
      { line: 'planner', spent_usd: 0, reserved_usd: 0 },
      { line: 'work_running', spent_usd: 0, reserved_usd: reserved },
      { line: 'work_finished', spent_usd: 0, reserved_usd: 0 },
      { line: 'followups', spent_usd: 0, reserved_usd: 0 },
    ],
  }
}

const LEDGER = {
  session_id: SESSION,
  date: '2026-11-02',
  time_zone: 'Europe/London',
  session: cap(100, 1.5, 9),
  day: cap(300, 4.25, 90),
  not_counted: ['pod compute for lanes', 'CI runner minutes', 'deploys'],
}

function serveLedger(data: unknown = LEDGER): void {
  fetchMock.read.mockImplementation(async () => ({ status: 'ok', data, fetchedAt: Date.now() }))
}

describe('the budget meter', () => {
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })

  it("reads the session's ledger and draws both caps with spent and reserved", async () => {
    serveLedger()
    render(<BudgetMeter sessionId={SESSION} live={false} />)
    await settle(act)

    expect(fetchMock.read.mock.calls[0]?.[0].url).toBe(`/v1/voice/sessions/${SESSION}/ledger`)
    const session = screen.getByRole('group', { name: 'This session' })
    expect(session.textContent).toContain('10.5 / 100 USD')
    expect(session.textContent).toContain('$1.50 spent, $9.00 reserved')
    expect(within(session).getByText('Work running: $0.00 spent, $9.00 reserved')).toBeTruthy()
    const day = screen.getByRole('group', { name: 'Today (2026-11-02, Europe/London)' })
    expect(day.textContent).toContain('94.25 / 300 USD')
    expect(day.textContent).toContain('$4.25 spent, $90.00 reserved')
    expect(screen.getByText(/Not counted: pod compute for lanes, CI runner minutes, deploys/)).toBeTruthy()
  })

  it("shows swarm-api's refusal in its own words", async () => {
    fetchMock.read.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: "voice session 'vs_0001' not found" },
    })
    render(<BudgetMeter sessionId={SESSION} live={false} />)
    await settle(act)

    expect(screen.getByRole('alert').textContent).toContain("voice session 'vs_0001' not found")
  })

  it('refuses an answer that is not a ledger', () => {
    expect(asLedger({ session: {}, day: {} })).toBeNull()
    expect(asLedger(LEDGER)?.day.cap_usd).toBe(300)
  })
})

describe('the Talk pane with its meter', () => {
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
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })

  it('shows no meter before a session and the session’s meter once it starts', async () => {
    render(<TalkPane />)
    expect(screen.queryByRole('region', { name: 'Budget' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
    await settle(act)

    expect(screen.getByRole('region', { name: 'Budget' })).toBeTruthy()
    expect(fetchMock.read.mock.calls.some(([t]) => t.url === `/v1/voice/sessions/${SESSION}/ledger`)).toBe(true)
  })
})
