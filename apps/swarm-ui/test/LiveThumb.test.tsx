// LB-L12b UI-GRAPH acceptance (docs/design/live-browser.md §6 and §9):
//
//   * a running browser node expands a card with a thumbnail at <= 1 fps;
//   * Open Browser tab and ⤢ Theater navigate to the same session;
//   * no thumbnail stream for a finished step.
//
// The card is INLINE in the running browser step's graph node (`StepNode`,
// drawn here through `WorkflowCard`; LB-UX2b moved it out of the step
// inspector, as the owner's chosen mockup draws it), and the stream is a fake
// socket speaking the gate's side of RFB 3.8
// (`livegate/rfb.py`): this suite is offline, and the gate answers an update
// request only when asked, which is what the <= 1 fps assertion measures.

import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  LIVE_BROWSER_PROFILES,
  RfbThumb,
  THUMB_MIN_INTERVAL_MS,
  browserTabPath,
  liveSocketUrl,
  theaterPath,
  type LiveSocket,
  type OpenLive,
} from '../src/LiveThumb'
import { WorkflowCard } from '../src/Workflows'
import type { Task, TaskState, Workflow } from '../src/types'
import { fixtureTask } from '../src/__tests__/dagfixture'

const W = 4
const H = 2

function u16(n: number): number[] {
  return [(n >> 8) & 255, n & 255]
}
function u32(n: number): number[] {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]
}

/** One full raw FramebufferUpdate of the whole W x H framebuffer. */
function rawUpdate(): Uint8Array {
  return Uint8Array.from([0, 0, ...u16(1), ...u16(0), ...u16(0), ...u16(W), ...u16(H), ...u32(0), ...new Array(W * H * 4).fill(7)])
}

/** The gate's half of a connection: handshake on demand, an update for every request answered. */
class FakeGate implements LiveSocket {
  binaryType = 'blob'
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: unknown) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  sent: Uint8Array[] = []
  requestTimes: number[] = []
  pending = 0
  closed = false

  send(data: Uint8Array): void {
    this.sent.push(Uint8Array.from(data))
    if (data[0] === 3 && data.length === 10) {
      this.requestTimes.push(Date.now())
      this.pending += 1
    }
  }
  close(): void {
    this.closed = true
  }
  push(bytes: Uint8Array | number[]): void {
    const b = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes)
    this.onmessage?.({ data: b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) })
  }
  handshake(): void {
    this.push(new TextEncoder().encode('RFB 003.008\n'))
    this.push([1, 1]) // one security type: None
    this.push(u32(0)) // SecurityResult OK
    this.push([...u16(W), ...u16(H), ...new Array(16).fill(0), ...u32(0)]) // ServerInit, empty name
  }
  /** Answer every outstanding request at once, as a gate whose frame changed does. */
  answer(): void {
    while (this.pending > 0) {
      this.pending -= 1
      this.push(rawUpdate())
    }
  }
}

/** A one-step workflow whose step is the browser step, in `state`. */
function oneStep(state: TaskState, profile = 'claude-code-browser'): { workflow: Workflow; taskById: Map<string, Task> } {
  const task = { ...fixtureTask('task-live-1', 'browse', state, 'wf-1', { started_at: null, completed_at: null }), runner_profile: profile }
  const workflow = {
    workflow_id: 'wf-1',
    state: 'RUNNING',
    tenant_id: 'eng',
    stored_state: 'RUNNING',
    state_source: 'stored',
    created_at: task.created_at,
    updated_at: task.updated_at,
    submitted_by: task.submitted_by,
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps: [{ step_id: 'browse', runner_profile: profile, resource_class: 'browser', depends_on: [], input_from: {}, task_id: 'task-live-1' }],
  } as unknown as Workflow
  return { workflow, taskById: new Map([[task.id, task]]) }
}

function card(state: TaskState, open: OpenLive, profile?: string) {
  const { workflow, taskById } = oneStep(state, profile)
  return (
    <WorkflowCard
      workflow={workflow}
      taskById={taskById}
      usage={{ kind: 'ready', usage: null }}
      openStages={{}}
      onToggleStage={() => {}}
      reload={() => {}}
      openLive={open}
    />
  )
}

function inspect(state: TaskState, open: OpenLive, profile?: string) {
  return render(card(state, open, profile))
}

let putImageData: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  putImageData = vi.fn()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ putImageData } as never)
  vi.stubGlobal(
    'ImageData',
    class {
      constructor(
        public data: Uint8ClampedArray,
        public width: number,
        public height: number,
      ) {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('a running browser node expands a card with a thumbnail at <= 1 fps', () => {
  it('draws the card with a live thumbnail and never asks the gate for more than one frame a second', () => {
    const gates: FakeGate[] = []
    const open = vi.fn<OpenLive>(() => {
      const g = new FakeGate()
      gates.push(g)
      return g
    })
    inspect('RUNNING', open)

    expect(screen.getByRole('region', { name: 'Live browser' })).toBeTruthy()
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith('task-live-1')
    const gate = gates[0] as FakeGate
    expect(gate.binaryType).toBe('arraybuffer')

    act(() => gate.handshake())
    // Ten seconds of a gate that answers at once, every 50 ms.
    for (let t = 0; t < 10_000; t += 50) {
      act(() => {
        gate.answer()
        vi.advanceTimersByTime(50)
      })
    }

    const times = gate.requestTimes
    expect(times.length).toBeGreaterThan(5)
    expect(times.length).toBeLessThanOrEqual(11)
    for (let i = 1; i < times.length; i += 1) {
      expect((times[i] as number) - (times[i - 1] as number)).toBeGreaterThanOrEqual(THUMB_MIN_INTERVAL_MS)
    }
    expect(putImageData.mock.calls.length).toBeGreaterThan(5)
    expect(putImageData.mock.calls.length).toBeLessThanOrEqual(11)
    const canvas = document.querySelector('canvas.wf-live-thumb') as HTMLCanvasElement
    expect(Number(canvas.dataset.frames)).toBe(putImageData.mock.calls.length)
    expect(canvas.width).toBe(W)
    expect(canvas.height).toBe(H)
  })

  it('is view only: after the handshake it sends only SetPixelFormat, SetEncodings and update requests', () => {
    let gate: FakeGate | null = null
    inspect('RUNNING', () => (gate = new FakeGate()))
    const g = gate as unknown as FakeGate
    act(() => g.handshake())
    for (let t = 0; t < 3000; t += 100) {
      act(() => {
        g.answer()
        vi.advanceTimersByTime(100)
      })
    }
    // version, security choice, ClientInit; then client messages by type byte.
    const messages = g.sent.slice(3).map((m) => m[0])
    expect(messages.length).toBeGreaterThan(3)
    for (const t of messages) expect([0, 2, 3]).toContain(t)
  })

  it('a resize from the gate is followed by a full request, still paced', () => {
    const sent: Uint8Array[] = []
    let clock = 0
    const timers: { at: number; fn: () => void }[] = []
    const client = new RfbThumb({
      send: (b) => sent.push(Uint8Array.from(b)),
      sink: { resize: () => {}, draw: () => {} },
      now: () => clock,
      later: (fn, ms) => {
        timers.push({ at: clock + ms, fn })
        return () => {}
      },
    })
    client.feed(new TextEncoder().encode('RFB 003.008\n'))
    client.feed(Uint8Array.from([1, 1, ...u32(0), ...u16(W), ...u16(H), ...new Array(16).fill(0), ...u32(0)]))
    expect(client.requests).toEqual([0])
    // DesktopSize to 8 x 4, then a ServerCutText the client must skip unread.
    clock = 200
    client.feed(Uint8Array.from([0, 0, ...u16(1), 0, 0, 0, 0, ...u16(8), ...u16(4), ...u32(-223 >>> 0)]))
    client.feed(Uint8Array.from([3, 0, 0, 0, ...u32(3), 65, 66, 67]))
    expect(client.frames).toBe(1)
    expect(timers.at(-1)?.at).toBe(THUMB_MIN_INTERVAL_MS)
    clock = THUMB_MIN_INTERVAL_MS
    timers.at(-1)?.fn()
    const last = sent.at(-1) as Uint8Array
    expect(last[0]).toBe(3)
    expect(last[1]).toBe(0) // full, not incremental, after a resize
    expect((last[6]! << 8) | last[7]!).toBe(8)
    expect((last[8]! << 8) | last[9]!).toBe(4)
  })
})

describe('Open Browser tab and ⤢ Theater navigate to the same session', () => {
  it('both links open the one agent the thumbnail watches, and the socket names only the task', () => {
    inspect('RUNNING', () => new FakeGate())
    const tab = screen.getByRole('link', { name: 'Open Browser tab' }) as HTMLAnchorElement
    const theater = screen.getByRole('link', { name: 'Open in theater' }) as HTMLAnchorElement
    expect(theater.textContent).toContain('⤢')

    const tabUrl = new URL(tab.href)
    const theaterUrl = new URL(theater.href)
    expect(tabUrl.pathname).toBe('/agents/live/task-live-1/browser')
    // The theater is a view over the same tab, not a second session.
    expect(theaterUrl.pathname).toBe(tabUrl.pathname)
    expect(theaterUrl.searchParams.get('theater')).toBe('1')
    expect(theaterUrl.searchParams.get('wf')).toBe('wf-1')
    expect(browserTabPath('task-live-1')).toBe(tabUrl.pathname)
    expect(theaterPath('task-live-1', 'wf-1')).toBe(`${tabUrl.pathname}${theaterUrl.search}`)

    // The session's stream (§5.2), the one swarm-api serves: the session id
    // and nothing else in the URL; the ticket goes as the first message.
    const ws = new URL(liveSocketUrl('live_att_1', { protocol: 'https:', host: 'console.example' }))
    expect(ws.protocol).toBe('wss:')
    expect(ws.pathname).toBe('/v1/live/live_att_1/stream')
    expect(ws.search).toBe('')
  })
})

describe('no thumbnail stream for a finished step', () => {
  it.each<TaskState>(['SUCCEEDED', 'FAILED', 'CANCELLED', 'PARKED', 'QUEUED', 'STARTING'])(
    'a %s browser step draws no card and opens no socket',
    (state) => {
      const open = vi.fn<OpenLive>(() => new FakeGate())
      inspect(state, open)
      expect(screen.queryByRole('region', { name: 'Live browser' })).toBeNull()
      expect(open).not.toHaveBeenCalled()
    },
  )

  it('a running step on a profile that is not a live-browser profile opens nothing', () => {
    expect(LIVE_BROWSER_PROFILES.has('browser')).toBe(false)
    const open = vi.fn<OpenLive>(() => new FakeGate())
    inspect('RUNNING', open, 'browser')
    expect(screen.queryByRole('region', { name: 'Live browser' })).toBeNull()
    expect(open).not.toHaveBeenCalled()
  })

  it('a step that finishes under the card closes its stream and does not reconnect', () => {
    const gates: FakeGate[] = []
    const open: OpenLive = () => {
      const g = new FakeGate()
      gates.push(g)
      return g
    }
    const view = inspect('RUNNING', open)
    const gate = gates[0] as FakeGate
    act(() => gate.handshake())
    act(() => gate.answer())
    const asked = gate.requestTimes.length

    view.rerender(card('SUCCEEDED', open))
    expect(screen.queryByRole('region', { name: 'Live browser' })).toBeNull()
    expect(gate.closed).toBe(true)
    act(() => vi.advanceTimersByTime(60_000))
    expect(gates.length).toBe(1)
    expect(gate.requestTimes.length).toBe(asked)
  })
})
