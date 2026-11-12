// VOICE IT'S SESSION LIVES ABOVE THE PAGE (docs/design/voice-it.md §3.12.2;
// voice/session.tsx, voice/Talk.tsx).
//
//   * leaving Talk for another screen and coming back keeps the call: the
//     peer is never closed, no end is posted, and Talk draws the same
//     session (its status, its cards, its cost pill);
//   * closing the tab (`pagehide`) posts the end once, kept alive past the
//     page, with the session cookie, the workspace header and the cause
//     `tab_closed`;
//   * a reload is a fresh holder, and a fresh holder starts no session;
//   * End still ends the session through swarm-api's end route
//     (`ended_by_person`), and signing out ends it before the page leaves.
//
// MUTATIONS: hang up when Talk unmounts; send the end with sendBeacon or
// without keepalive; drop the workspace header; send no cause or
// `ended_by_person` on pagehide; post the end on every
// pagehide; start a session when the holder mounts; navigate before the end.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakePeer, installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    get USE_FIXTURES() {
      return false
    },
  }
})

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { chooseTenant, TENANT_HEADER } from '../fetch'
import { leave } from '../signoutState'
import { FIXTURE_CARDS } from '../voice/CardList'
import { VoiceSessionHolder } from '../voice/session'
import { TalkPane } from '../voice/Talk'

const SESSION = 'vs_0001'
const WORKSPACE = 'example-org'

class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeEventSource.all.push(this)
  }
  close(): void {
    this.readyState = 2
  }
  frame(type: string, seq: number, data: Record<string, unknown>): void {
    this.dispatchEvent(new MessageEvent(type, { data: JSON.stringify({ seq, type, ...data }), lastEventId: String(seq) }))
  }
}

function cap(cap_usd: number, spent: number) {
  return { cap_usd, spent_usd: spent, reserved_usd: 0, total_usd: spent, remaining_usd: cap_usd - spent, lines: [{ line: 'talk', spent_usd: spent, reserved_usd: 0 }] }
}

const LEDGER = { session_id: SESSION, date: '2026-11-02', time_zone: 'UTC', session: cap(100, 1.5), day: cap(300, 4.25), not_counted: [] }

/** The console between screens: Talk, or another screen, under one holder, as App.tsx mounts it. */
function Console() {
  const [on, setOn] = useState<'talk' | 'elsewhere'>('talk')
  return (
    <VoiceSessionHolder>
      <button type="button" onClick={() => setOn(on === 'talk' ? 'elsewhere' : 'talk')}>
        Switch
      </button>
      {on === 'talk' ? <TalkPane /> : <p>Another screen</p>}
    </VoiceSessionHolder>
  )
}

const switchScreen = async () => {
  fireEvent.click(screen.getByRole('button', { name: 'Switch' }))
  await settle(act)
}

async function start(): Promise<FakePeer> {
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
  return FakePeer.last
}

const calls = (pred: (url: string) => boolean) => fetchMock.write.mock.calls.filter(([t]) => pred(String((t as { url: string }).url)))
const ends = () => calls((u) => u.endsWith('/end'))
const starts = () => calls((u) => u === '/v1/voice/sessions')

describe("Voice it's session lives above the page", () => {
  let fetchSpy: ReturnType<typeof vi.fn>
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    fetchSpy = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchSpy)
    installMedia({ audioContext: true })
    chooseTenant(WORKSPACE)
    fetchMock.read.mockImplementation(async (target: { url: string }) =>
      target.url === '/v1/voice/settings'
        ? { status: 'ok', data: { settings: { engine: 'gpt-live' }, debug: false }, fetchedAt: Date.now() }
        : { status: 'ok', data: LEDGER, fetchedAt: Date.now() },
    )
    fetchMock.write.mockImplementation(async (target: { url: string }) =>
      target.url === '/v1/voice/sessions'
        ? { status: 'ok', data: { session_id: SESSION, answer_sdp: 'v=0 fake-answer' }, fetchedAt: Date.now() }
        : { status: 'ok', data: null, fetchedAt: Date.now() },
    )
  })
  afterEach(() => {
    chooseTenant(null)
    vi.unstubAllGlobals()
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })

  it('leaving Talk keeps the call, and coming back draws the same session', async () => {
    const { container } = render(<Console />)
    const peer = await start()
    expect(container.querySelector('.vt-status')?.textContent).toBe('Live')

    await switchScreen()
    expect(screen.queryByRole('button', { name: 'End' })).toBeNull()
    expect(peer.closed).toBe(false)
    expect(ends()).toHaveLength(0)

    // A card drawn while Talk is off screen is still the session's.
    const base = FIXTURE_CARDS.find((c) => c.kind === 'work') ?? FIXTURE_CARDS[0]!
    act(() => FakeEventSource.all[0]!.frame('card.upsert', 1, { card: { ...base, kind: 'work', card_id: 'vc_off', version: 1, title: 'Drawn off Talk' } }))

    await switchScreen()
    expect(peer.closed).toBe(false)
    expect(ends()).toHaveLength(0)
    expect(starts()).toHaveLength(1)
    expect(FakePeer.all).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'End' })).toBeTruthy()
    expect(container.querySelector('.vt-status')?.textContent).toBe('Live')
    expect(within(screen.getByRole('region', { name: 'Cards' })).getByText('Drawn off Talk')).toBeTruthy()
  })

  it("the cost pill still reads the session's ledger after a route change", async () => {
    render(<Console />)
    await start()
    await switchScreen()
    await switchScreen()
    const pill = screen.getByRole('button', { name: /^Cost/ })
    expect(pill.getAttribute('aria-label')).toContain('this session $1.50')
    const ledgerReads = fetchMock.read.mock.calls.map(([t]) => String((t as { url: string }).url)).filter((u) => u !== '/v1/voice/settings')
    expect(ledgerReads.length).toBeGreaterThan(0)
    expect(ledgerReads.every((u) => u.includes(SESSION))).toBe(true)
  })

  it('closing the tab posts the end once, kept alive, with the workspace header', async () => {
    render(<Console />)
    const peer = await start()
    await switchScreen()

    act(() => {
      window.dispatchEvent(new Event('pagehide'))
      window.dispatchEvent(new Event('pagehide'))
    })

    const sent = fetchSpy.mock.calls.filter(([u]) => String(u).endsWith(`/v1/voice/sessions/${SESSION}/end`))
    expect(sent).toHaveLength(1)
    const init = sent[0]![1] as RequestInit
    expect(init.method).toBe('POST')
    expect(init.keepalive).toBe(true)
    expect(init.credentials).toBe('same-origin')
    expect((init.headers as Record<string, string>)[TENANT_HEADER]).toBe(WORKSPACE)
    // A closed tab is recorded as `tab_closed`, not as ended by the person.
    expect((init.headers as Record<string, string>)['content-type']).toBe('application/json')
    expect(JSON.parse(String(init.body))).toEqual({ cause: 'tab_closed' })
    expect(peer.closed).toBe(true)
  })

  it('a reload is a fresh holder, and a fresh holder starts no session', async () => {
    render(<Console />)
    await settle(act)
    expect(starts()).toHaveLength(0)
    expect(FakePeer.all).toHaveLength(0)
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
  })

  it("End still ends the session through swarm-api's end route (ended_by_person)", async () => {
    render(<Console />)
    const peer = await start()
    await switchScreen()
    await switchScreen()
    fireEvent.click(screen.getByRole('button', { name: 'End' }))
    await settle(act)
    expect(ends().map(([t]) => (t as { url: string }).url)).toEqual([`/v1/voice/sessions/${SESSION}/end`])
    // End names no cause: swarm-api records the missing one as `ended_by_person`.
    expect(JSON.stringify(ends())).not.toContain('tab_closed')
    expect(fetchSpy.mock.calls.filter(([u]) => String(u).endsWith('/end'))).toHaveLength(0)
    expect(peer.closed).toBe(true)
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
  })

  it('signing out ends the session before the page leaves', async () => {
    const order: string[] = []
    fetchSpy.mockImplementation(async (u: unknown) => {
      order.push(`end ${String(u)}`)
      return new Response(null, { status: 204 })
    })
    const go = vi.spyOn(leave, 'to').mockImplementation((url: string) => {
      order.push(`leave ${url}`)
    })
    const { unmount } = render(<Console />)
    await start()
    await switchScreen()
    act(() => leave.to('/?signed-out'))
    expect(order).toEqual([`end /v1/voice/sessions/${SESSION}/end`, 'leave /?signed-out'])
    unmount()
    go.mockRestore()
  })
})
