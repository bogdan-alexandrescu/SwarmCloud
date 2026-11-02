// ONE CONNECTION, HELD; A CHEAP SESSION READ BESIDE IT (LIVE-STREAM-SPEED;
// part of #2688).
//
// MEASURED 2026-11-02 about 04:00Z in the owner's Chrome: on an idle browser
// agent the Browser tab minted a new viewer ticket every 5-17 s ("The live
// connection closed. Reconnecting with a new ticket"), and read the whole
// session every 3 s beside the stream while the API's reads ran at p95
// 16.67 s.
//
//   1. A connected viewer mints no new ticket and opens no new socket while
//      its socket stays open, however long the person watches.
//   2. While the socket is connected the session is read every
//      `LIVE_STREAMING_POLL_MS`, not every `LIVE_POLL_MS`; a status frame that
//      says control moved still reads at once.
//
// MUTATIONS: re-read at `LIVE_POLL_MS` whatever the socket (2 fails); stop
// marking the socket connected in LiveScreen (2 fails); tear the connection
// down on a session re-read (1 fails).

import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadLiveSession: vi.fn(),
  loadStartup: vi.fn(),
  mintLiveTicket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
  loadTaskEventsPage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    showDotCursor = false
    qualityLevel = 6
    compressionLevel = 2
    private listeners = new Map<string, ((e: Event) => void)[]>()
    constructor(target: HTMLElement) {
      target.appendChild(document.createElement('canvas'))
      FakeRfb.made.push(this)
      // noVNC says it connected once the handshake is through.
      queueMicrotask(() => this.emit('connect'))
    }
    addEventListener(type: string, fn: (e: Event) => void) {
      this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn])
    }
    emit(type: string) {
      for (const fn of this.listeners.get(type) ?? []) fn(new Event(type))
    }
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

/** A browser WebSocket that opens at once and stays open until the test closes it. */
class FakeSocket extends EventTarget {
  static made: FakeSocket[] = []
  binaryType = 'blob'
  readyState = 0
  protocol = ''
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  readonly sent: unknown[] = []
  constructor(readonly url: string) {
    super()
    FakeSocket.made.push(this)
    queueMicrotask(() => {
      this.readyState = 1
      this.onopen?.(new Event('open'))
    })
  }
  send(data: unknown) {
    this.sent.push(data)
  }
  close() {
    this.readyState = 3
  }
}

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

const { AgentSplit } = await import('../AgentSplit')
const { LIVE_POLL_MS, LIVE_STREAMING_POLL_MS, liveStreaming } = await import('../LiveBrowser')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef1343'
const ME = ['ada', 'example.com'].join('@')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
    ...over,
  })
}

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

const ORIGIN: NonNullable<LiveSession['origin']> = {
  origin: 'https://github.com',
  host: 'github.com',
  registrable_domain: 'github.com',
  scheme: 'https:',
  path: '/login',
}

/** An open session this viewer controls; origin and device as the gate has reported them. */
function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_poll',
    state: 'open',
    closed_reason: null,
    origin: ORIGIN,
    human_wait: null,
    controller: { email: ME, since: new Date().toISOString() },
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

const read = (s: LiveSession | null): Result<LiveRead> => ok<LiveRead>({ live_browser: true, session: s })

function Split() {
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane="browser"
        theater={false}
        artifact={null}
        closeTo="work/running/live"
        go={() => {}}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

/** Move the clock, letting every read it starts land. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

let hidden = false
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
  localStorage.clear()
  resetListSnap()
  hidden = false
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') })
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200, headers: { 'content-type': 'application/json' } })),
  )
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadStartup.mockReturnValue(new Promise(() => {}))
  rfb.FakeRfb.made = []
  FakeSocket.made = []
  vi.stubGlobal('WebSocket', FakeSocket)
  // Built at run time: nothing here may look like a credential in the source.
  api.mintLiveTicket.mockImplementation(async () =>
    ok({ ticket: `vt.${crypto.randomUUID()}`, session_id: 'ls_poll', expires_in: 30, socket_path: '/v1/live/ls_poll/stream' }),
  )
  api.renewLiveControl.mockResolvedValue(ok(null))
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  // The prototype's own accessors come back.
  delete (document as unknown as Record<string, unknown>).visibilityState
  delete (document as unknown as Record<string, unknown>).hidden
  vi.clearAllMocks()
})


/** Session reads made in the last `ms` of the clock, with the socket connected throughout. */
async function readsOver(ms: number): Promise<number> {
  const before = api.loadLiveSession.mock.calls.length
  await advance(ms)
  return api.loadLiveSession.mock.calls.length - before
}

async function watching(): Promise<void> {
  api.loadTask.mockResolvedValue(ok(agent()))
  api.loadLiveSession.mockResolvedValue(read(session()))
  render(<Split />)
  await waitFor(() => expect(rfb.FakeRfb.made).toHaveLength(1))
  await waitFor(() => expect(liveStreaming(ID)).toBe(true))
}

describe('1. one connection for as long as the person watches', () => {
  it('mints one ticket and opens one socket over ten minutes of an idle agent', async () => {
    await watching()
    await advance(10 * 60_000)
    expect(api.mintLiveTicket).toHaveBeenCalledTimes(1)
    expect(FakeSocket.made).toHaveLength(1)
    expect(rfb.FakeRfb.made).toHaveLength(1)
    expect(liveStreaming(ID)).toBe(true)
  })
})

describe('2. the session read while connected is cheap', () => {
  it('reads the session every LIVE_STREAMING_POLL_MS while the socket is connected', async () => {
    expect(LIVE_STREAMING_POLL_MS).toBeGreaterThan(LIVE_POLL_MS)
    await watching()
    // Let the read scheduled before the socket connected land.
    await advance(LIVE_POLL_MS)
    const reads = await readsOver(60_000)
    expect(reads, 'a full session read every 3 s beside the stream').toBeLessThanOrEqual(Math.ceil(60_000 / LIVE_STREAMING_POLL_MS))
    expect(reads, 'the reads stopped').toBeGreaterThanOrEqual(Math.floor(60_000 / LIVE_STREAMING_POLL_MS) - 1)
  })

  it('reads at LIVE_POLL_MS again once nothing is connected', async () => {
    api.loadTask.mockResolvedValue(ok(agent()))
    api.loadLiveSession.mockResolvedValue(read(session()))
    // No ticket is ever issued: there is no socket.
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    render(<Split />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalled())
    expect(liveStreaming(ID)).toBe(false)
    const reads = await readsOver(30_000)
    expect(reads).toBeGreaterThanOrEqual(Math.floor(30_000 / LIVE_POLL_MS) - 1)
  })

  it('forgets the socket when the tab goes', async () => {
    await watching()
    cleanup()
    expect(liveStreaming(ID)).toBe(false)
  })
})
