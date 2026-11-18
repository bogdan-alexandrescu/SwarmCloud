// VOICE IT'S LEDGER behind Talk's cost pill (docs/design/voice-it.md §3.8;
// section 10, phase three's ledger row). The pill itself, its panel and its
// place in the Talk pane: voice.costpill.test.tsx. The big budget meter that
// stood beside the pane is gone (the owner's pick of 11 November 2026).
//
//   * the read is the session's own ledger, refused when the answer is not one;
//   * it sums nothing of its own: the figures are the served ones.
//
// MUTATIONS: read the ledger of another session id; accept a ledger with no
// caps.

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { asLedger, useLedger } from '../voice/Meter'

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

function Probe({ sessionId }: { sessionId: string }) {
  const { ledger, problem } = useLedger(sessionId, false)
  return <p>{problem ?? (ledger === null ? 'reading' : `${ledger.session.total_usd} ${ledger.day.total_usd}`)}</p>
}

describe('the ledger read', () => {
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })

  it("reads the session's own ledger and hands over the served figures", async () => {
    serveLedger()
    const { container } = render(<Probe sessionId={SESSION} />)
    await settle(act)

    expect(fetchMock.read.mock.calls[0]?.[0].url).toBe(`/v1/voice/sessions/${SESSION}/ledger`)
    expect(container.textContent).toBe('10.5 94.25')
  })

  it("hands over swarm-api's refusal in its own words", async () => {
    fetchMock.read.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: "voice session 'vs_0001' not found" },
    })
    const { container } = render(<Probe sessionId={SESSION} />)
    await settle(act)

    expect(container.textContent).toBe("voice session 'vs_0001' not found")
  })

  it('refuses an answer that is not a ledger', () => {
    expect(asLedger({ session: {}, day: {} })).toBeNull()
    expect(asLedger(LEDGER)?.day.cap_usd).toBe(300)
  })
})
