import { useEffect, useRef, useState } from 'react'

import { USE_FIXTURES, loadLiveSession, mintLiveTicket, openLiveChannel, type LiveChannel } from './api'
import { route } from './fetch'
import { addressToPath } from './paths'
import { StartupBar } from './StartupProgress'
import type { LiveAgentRef } from './Theater'
import { stepState, type Task, type TaskState, type Workflow } from './types'

/**
 * THE WORKFLOW GRAPH'S LIVE THUMBNAIL (docs/design/live-browser.md §6, lanes
 * L12b and LB-UX2b). A running browser step's NODE carries the card inline
 * (`StepNode` in Workflows.tsx, the height reserved by `LIVE_CARD_H` in
 * dag.ts): a low-rate live thumbnail of the agent's browser with two ways into
 * the full view, Open Browser tab and ⤢ Theater. It sat in the step details
 * panel until LB-UX2b; the owner's chosen mockup ("Browser Tab and Theater")
 * draws it in the node, and the panel no longer carries a second one. Both
 * links are addresses of the SAME agent, and the live session is the task's
 * (one per task), so both land on the one session this thumbnail is watching.
 *
 * AT MOST ONE FRAME A SECOND, BY CONSTRUCTION. RFB is pull-based: the gate
 * (`livegate/rfb.py`) sends a FramebufferUpdate only in answer to a
 * FramebufferUpdateRequest, and this client keeps ONE request outstanding and
 * sends the next no sooner than `THUMB_MIN_INTERVAL_MS` after the last. So the
 * rate is decided here, in the viewer, and not by the gate's 10 fps viewer
 * rate (§5.10): a thumbnail on a graph does not cost the pod a full-rate
 * stream. noVNC is not used for the thumbnail because it requests as fast as
 * it can draw; that is right for the Browser tab and wrong for a card.
 *
 * VIEW ONLY. This client never sends a key, a pointer or a cut-text message:
 * the only client messages it writes are SetPixelFormat, SetEncodings and
 * FramebufferUpdateRequest. A ServerCutText from the gate (which the gate
 * never sends, §5.6) is skipped unread.
 *
 * ONLY WHILE RUNNING. The card is drawn only for a step whose task is
 * `RUNNING` on a live-browser profile (§3.4: the live session exists only
 * then). A finished step opens no socket at all, and a step that finishes
 * while its card is open closes its socket when the card unmounts. The
 * recording of a finished step is the attempt view's (§7), not this card's.
 *
 * NOTHING SECRET IN THE URL (§5.2). The socket's URL carries only the task id.
 * The console holds no ticket: swarm-api authenticates the connection as it
 * does every route and mints the viewer ticket for itself. Every frame is
 * masked in the pod before it leaves (§5.3); there is no unmasked stream for
 * this to show.
 */

/** §6: "a low-rate (about 1 fps) live thumbnail". The floor between two requests. */
export const THUMB_MIN_INTERVAL_MS = 1000

/**
 * After the socket closes (Cloud Run ends a viewer's socket at its request
 * timeout, §3.3 R2; a relay restart drops every session), reconnect after
 * this long. 5 s keeps a reconnecting card at 12 viewer tickets a minute, under
 * the 30 a minute per person swarm-api allows (§5.10).
 */
export const THUMB_RECONNECT_MS = 5000

/**
 * Consecutive connections that close without a single frame before the card
 * stops trying and says the live view is unavailable (a Retry button starts
 * again). swarm-api answers a foreign or non-live task with a refusal the
 * browser reports only as a close, so retrying forever would spend tickets on
 * a 404.
 */
export const THUMB_MAX_FAILURES = 3

/**
 * NO SESSION IS NOT A DROPPED CONNECTION (LB-UX2b). The session read answers
 * `session: null` for a live-browser task whose tunnel is not up (or is down
 * for good), and a closed session for one that ended. The card said
 * "reconnecting" over both and spent a viewer ticket every 5 s doing it, for a
 * stream that did not exist. Now `ticketedSocket` closes with this value, the
 * card says "no live view", mints nothing, and only RE-READS the session --
 * a GET, no ticket, nothing against the 30-a-minute ticket limit (§5.10) --
 * every `THUMB_SESSION_RECHECK_MS`, so a browser that starts later still
 * appears on the node without a reload. Only a session that exists is
 * reconnected to on the 5 s cadence.
 */
export const NO_LIVE_SESSION: { readonly reason: 'no-session' } = Object.freeze({ reason: 'no-session' as const })

/** How often a card with no session re-reads the session (a GET only). */
export const THUMB_SESSION_RECHECK_MS = 30_000

/**
 * THE LIVE-BROWSER PROFILES, BY NAME. The authority is `RunnerProfile.live_browser`
 * in `apps/common/swarm_common/profiles.py` (contract request 71: only
 * `claude-code-browser`). `GET /v1/runtimes` does not serve that flag yet, so
 * this is a restatement, and a stale one fails closed: the live route refuses a
 * task whose profile is not a live-browser profile (§3.3 step 1), and the card
 * then reads "live view unavailable". Serving `live_browser` on the catalogue
 * is requested in this lane's PR so this set can become a read.
 */
export const LIVE_BROWSER_PROFILES: ReadonlySet<string> = new Set(['claude-code-browser'])

/** Whether a step's card carries the live thumbnail: a RUNNING task on a live-browser profile. */
export function showsLiveThumb(runnerProfile: string, taskState: TaskState | null, taskId: string | null): boolean {
  return taskId !== null && taskState === 'RUNNING' && LIVE_BROWSER_PROFILES.has(runnerProfile)
}

/**
 * THE WORKFLOW'S LIVE STEPS, for the theater's prev and next (§6: "In the
 * theater, prev and next step through that workflow's live steps"): every
 * step whose card is drawn (`showsLiveThumb`), in step order, labelled by step
 * id and marked needs-you where its task's `human_wait` is set. The theater
 * orders them needs-you first itself (`orderLiveAgents`).
 *
 * The ⤢ link carries the WORKFLOW (`theaterPath`'s `wf=`), not this list:
 * the router pushes a bare path and no history state, and a list in the query
 * would be stale the moment a step finished. A theater that reads `wf=` loads
 * the workflow and calls this, so the graph and the theater agree on the list.
 */
export function workflowLiveAgents(workflow: Pick<Workflow, 'steps'>, taskById: ReadonlyMap<string, Task> | null): LiveAgentRef[] {
  const out: LiveAgentRef[] = []
  for (const s of workflow.steps) {
    const st = stepState(s, taskById)
    if (st.kind !== 'state' || !showsLiveThumb(s.runner_profile, st.state, st.task.id)) continue
    out.push({ taskId: st.task.id, label: s.step_id, needsYou: st.task.human_wait != null })
  }
  return out
}

/** The agent's Browser tab (§6): a pane of the agent's own address, as Logs is. */
export function browserTabPath(taskId: string): string {
  return addressToPath(`work/task/${encodeURIComponent(taskId)}/browser`, 'live')
}

/**
 * Theater over the same Browser tab (§6: "Theater is a view, not a second
 * session: it reuses the tab's tunnel and ticket"), so it is the tab's address
 * with the view named in the query. `wf` says which workflow's live steps
 * prev and next walk ("In the theater, prev and next step through that
 * workflow's live steps").
 */
export function theaterPath(taskId: string, workflowId: string | null): string {
  const q = new URLSearchParams({ theater: '1' })
  if (workflowId !== null) q.set('wf', workflowId)
  return `${browserTabPath(taskId)}?${q.toString()}`
}

/**
 * The console's live socket for a SESSION (§3.3 step 3, §5.2): same origin,
 * `/v1/live/{session_id}/stream`, the session id and nothing else. The one
 * swarm-api serves; the ticket travels as the socket's first message.
 *
 * LB-SEAM (#1030): this card used to open `/v1/tasks/{task_id}/live` with no
 * ticket. swarm-api serves no such route, and must not: §5.2 refuses every
 * socket that does not present a one-use viewer ticket first.
 */
export function liveSocketUrl(sessionId: string, loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  const scheme = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${loc.host}${route('/v1/live/{id}/stream', { id: sessionId }).url}`
}

/** The part of a WebSocket this card uses, so a test can hand it a fake. */
export interface LiveSocket {
  binaryType: string
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: unknown) => void) | null
  onerror: ((ev: unknown) => void) | null
  send(data: Uint8Array): void
  close(): void
}

/** Opens the live socket for a task, or null where there is no live path (fixture mode). */
export type OpenLive = (taskId: string) => LiveSocket | null

export const openLiveSocket: OpenLive = (taskId) => (USE_FIXTURES ? null : ticketedSocket(taskId))

/**
 * A `LiveSocket` over the ticketed path (§5.2): read the task's session, mint
 * a viewer ticket and open the session's `LiveChannel` (api.ts), which sends
 * the ticket FIRST and hands on binary frames only: swarm-api's own
 * `{"type": "status"}` text frames are not RFB and never reach the RFB client.
 * Bytes the card sends before the socket is open are held, and go after the
 * ticket. Any step that fails reads as a close, which the card already counts
 * towards "unavailable".
 */
function ticketedSocket(taskId: string): LiveSocket {
  let ws: LiveChannel | null = null
  let closed = false
  const held: Uint8Array[] = []
  const sock: LiveSocket = {
    binaryType: 'arraybuffer',
    onmessage: null,
    onclose: null,
    onerror: null,
    send(data) {
      if (ws !== null && ws.readyState === 1) ws.send(data)
      else held.push(data)
    },
    close() {
      closed = true
      ws?.close()
    },
  }
  const fail = (ev: unknown) => {
    if (closed) return
    closed = true
    sock.onclose?.(ev)
  }
  void (async () => {
    const read = await loadLiveSession(taskId)
    if (read.status === 'error' || read.status === 'loading') return fail(null)
    const session = read.status === 'ok' || read.status === 'stale' ? read.data.session : null
    // No session, or one that has ended: there is nothing to reconnect to.
    if (session === null || session.state !== 'open') return fail(NO_LIVE_SESSION)
    const minted = await mintLiveTicket(taskId)
    if (minted.status !== 'ok' || closed) return fail(null)
    const channel = openLiveChannel(minted.data.session_id, minted.data.ticket)
    ws = channel
    channel.onopen = () => {
      for (const b of held.splice(0)) channel.send(b)
    }
    channel.onmessage = (ev) => sock.onmessage?.({ data: ev.data })
    channel.onclose = (ev) => fail(ev)
    channel.onerror = (ev) => {
      if (!closed) sock.onerror?.(ev)
    }
  })().catch(fail)
  return sock
}

// ---------------------------------------------------------------------------
// A view-only RFB 3.8 client that asks for at most one frame a second
// ---------------------------------------------------------------------------

/** The client could not follow the server; the card closes the socket. */
export class RfbThumbError extends Error {}

/** The largest framebuffer accepted: twice the largest preset (§6.1) and then some. */
const MAX_SIDE = 4096

const VERSION = 'RFB 003.008\n'
const SECURITY_NONE = 1
const ENCODING_RAW = 0
const ENCODING_DESKTOP_SIZE = -223

/** Bytes received but not yet parsed, without re-copying them on every message. */
class ByteQueue {
  private chunks: Uint8Array[] = []
  private head = 0
  length = 0

  push(chunk: Uint8Array): void {
    if (chunk.length === 0) return
    this.chunks.push(chunk)
    this.length += chunk.length
  }

  /** Copy `n` bytes (n <= length) into `dest` at `at`, consuming them. */
  takeInto(dest: Uint8Array | Uint8ClampedArray, at: number, n: number): void {
    let o = 0
    while (o < n) {
      const c = this.chunks[0] as Uint8Array
      const k = Math.min(c.length - this.head, n - o)
      dest.set(c.subarray(this.head, this.head + k), at + o)
      o += k
      this.head += k
      if (this.head === c.length) {
        this.chunks.shift()
        this.head = 0
      }
    }
    this.length -= n
  }

  take(n: number): Uint8Array {
    const out = new Uint8Array(n)
    this.takeInto(out, 0, n)
    return out
  }

  skip(n: number): void {
    this.take(n)
  }
}

/** Where decoded frames go. The component draws them on a canvas. */
export interface ThumbSink {
  resize(width: number, height: number): void
  draw(pixels: Uint8ClampedArray<ArrayBuffer>, width: number, height: number): void
}

export interface RfbThumbOptions {
  send: (bytes: Uint8Array) => void
  sink: ThumbSink
  now: () => number
  /** Run `fn` after `ms`; the client's own pacing timer, cleared by `stop`. */
  later: (fn: () => void, ms: number) => () => void
  minIntervalMs?: number
}

type Rect = { x: number; y: number; w: number; h: number; row: number }

type Phase =
  | { k: 'version' }
  | { k: 'security-count' }
  | { k: 'security-types'; n: number }
  | { k: 'security-result' }
  | { k: 'reason'; why: string }
  | { k: 'reason-body'; why: string; n: number }
  | { k: 'server-init' }
  | { k: 'server-name'; n: number }
  | { k: 'message' }
  | { k: 'update-head' }
  | { k: 'rect-head'; left: number }
  | { k: 'rect-raw'; left: number; rect: Rect }
  | { k: 'cut-text-head' }
  | { k: 'cut-text-body'; n: number }

export class RfbThumb {
  private q = new ByteQueue()
  private phase: Phase = { k: 'version' }
  private width = 0
  private height = 0
  private pixels: Uint8ClampedArray<ArrayBuffer> = new Uint8ClampedArray(0)
  private lastRequestAt = Number.NEGATIVE_INFINITY
  private full = true
  private cancel: (() => void) | null = null
  private stopped = false
  private readonly min: number
  /** FramebufferUpdates fully received and drawn. */
  frames = 0
  /** When each FramebufferUpdateRequest was sent, by `now()`. */
  readonly requests: number[] = []

  constructor(private readonly o: RfbThumbOptions) {
    this.min = o.minIntervalMs ?? THUMB_MIN_INTERVAL_MS
  }

  stop(): void {
    this.stopped = true
    this.cancel?.()
    this.cancel = null
  }

  feed(chunk: Uint8Array): void {
    if (this.stopped) return
    this.q.push(chunk)
    while (!this.stopped && this.step()) {
      // each step consumed something; keep going until a step needs more bytes
    }
  }

  /** One parse step. False when it needs more bytes than are queued. */
  private step(): boolean {
    const q = this.q
    const p = this.phase
    switch (p.k) {
      case 'version': {
        if (q.length < 12) return false
        const v = String.fromCharCode(...q.take(12))
        if (!/^RFB 003\.00[89]\n$/.test(v)) throw new RfbThumbError('the server does not speak RFB 3.8')
        this.o.send(new TextEncoder().encode(VERSION))
        this.phase = { k: 'security-count' }
        return true
      }
      case 'security-count': {
        if (q.length < 1) return false
        const n = q.take(1)[0] as number
        this.phase = n === 0 ? { k: 'reason', why: 'refused' } : { k: 'security-types', n }
        return true
      }
      case 'security-types': {
        if (q.length < p.n) return false
        const types = q.take(p.n)
        if (!types.includes(SECURITY_NONE)) throw new RfbThumbError('the server offers no security type this view takes')
        this.o.send(Uint8Array.of(SECURITY_NONE))
        this.phase = { k: 'security-result' }
        return true
      }
      case 'security-result': {
        if (q.length < 4) return false
        const r = u32(q.take(4), 0)
        if (r !== 0) {
          this.phase = { k: 'reason', why: 'security failed' }
          return true
        }
        // ClientInit: shared. Other viewers (the Browser tab, the theater) stay connected.
        this.o.send(Uint8Array.of(1))
        this.phase = { k: 'server-init' }
        return true
      }
      case 'reason': {
        if (q.length < 4) return false
        this.phase = { k: 'reason-body', why: p.why, n: u32(q.take(4), 0) }
        return true
      }
      case 'reason-body': {
        // The reason is the server's prose; it is not shown or logged, only consumed.
        if (q.length < p.n) return false
        q.skip(p.n)
        throw new RfbThumbError(p.why)
      }
      case 'server-init': {
        if (q.length < 24) return false
        const b = q.take(24)
        this.resize(u16(b, 0), u16(b, 2))
        this.phase = { k: 'server-name', n: u32(b, 20) }
        return true
      }
      case 'server-name': {
        if (q.length < p.n) return false
        q.skip(p.n)
        this.o.send(setPixelFormat())
        this.o.send(setEncodings([ENCODING_RAW, ENCODING_DESKTOP_SIZE]))
        this.request()
        this.phase = { k: 'message' }
        return true
      }
      case 'message': {
        if (q.length < 1) return false
        const t = q.take(1)[0] as number
        if (t === 0) this.phase = { k: 'update-head' }
        else if (t === 2) this.phase = { k: 'message' } // Bell: no body
        else if (t === 3) this.phase = { k: 'cut-text-head' }
        else throw new RfbThumbError(`unexpected server message ${t}`)
        return true
      }
      case 'cut-text-head': {
        if (q.length < 7) return false
        this.phase = { k: 'cut-text-body', n: u32(q.take(7), 3) }
        return true
      }
      case 'cut-text-body': {
        // §5.6: pod-to-console clipboard is never read. Skipped, not decoded.
        if (q.length < p.n) return false
        q.skip(p.n)
        this.phase = { k: 'message' }
        return true
      }
      case 'update-head': {
        if (q.length < 3) return false
        const left = u16(q.take(3), 1)
        this.phase = left === 0 ? this.updateDone() : { k: 'rect-head', left }
        return true
      }
      case 'rect-head': {
        if (q.length < 12) return false
        const b = q.take(12)
        const rect: Rect = { x: u16(b, 0), y: u16(b, 2), w: u16(b, 4), h: u16(b, 6), row: 0 }
        const enc = i32(b, 8)
        if (enc === ENCODING_DESKTOP_SIZE) {
          this.resize(rect.w, rect.h)
          this.full = true
          this.phase = p.left === 1 ? this.updateDone() : { k: 'rect-head', left: p.left - 1 }
          return true
        }
        if (enc !== ENCODING_RAW) throw new RfbThumbError(`unrequested encoding ${enc}`)
        if (rect.x + rect.w > this.width || rect.y + rect.h > this.height) {
          throw new RfbThumbError('a rectangle outside the framebuffer')
        }
        this.phase = rect.w === 0 || rect.h === 0
          ? (p.left === 1 ? this.updateDone() : { k: 'rect-head', left: p.left - 1 })
          : { k: 'rect-raw', left: p.left, rect }
        return true
      }
      case 'rect-raw': {
        const { rect } = p
        const rowBytes = rect.w * 4
        if (q.length < rowBytes) return false
        // Row by row, as the bytes arrive: a full frame is megabytes and
        // arrives in many websocket messages.
        while (rect.row < rect.h && q.length >= rowBytes) {
          const at = ((rect.y + rect.row) * this.width + rect.x) * 4
          q.takeInto(this.pixels, at, rowBytes)
          for (let i = at + 3; i < at + rowBytes; i += 4) this.pixels[i] = 255
          rect.row += 1
        }
        if (rect.row === rect.h) {
          this.phase = p.left === 1 ? this.updateDone() : { k: 'rect-head', left: p.left - 1 }
        }
        return true
      }
    }
  }

  private resize(w: number, h: number): void {
    if (w === 0 || h === 0 || w > MAX_SIDE || h > MAX_SIDE) throw new RfbThumbError('a framebuffer size this view refuses')
    this.width = w
    this.height = h
    this.pixels = new Uint8ClampedArray(w * h * 4)
    this.o.sink.resize(w, h)
  }

  /** A whole FramebufferUpdate has arrived: draw it and pace the next request. */
  private updateDone(): Phase {
    this.frames += 1
    this.o.sink.draw(this.pixels, this.width, this.height)
    const wait = Math.max(0, this.lastRequestAt + this.min - this.o.now())
    this.cancel?.()
    this.cancel = this.o.later(() => {
      this.cancel = null
      if (!this.stopped) this.request()
    }, wait)
    return { k: 'message' }
  }

  private request(): void {
    const b = new Uint8Array(10)
    const v = new DataView(b.buffer)
    b[0] = 3
    b[1] = this.full ? 0 : 1
    v.setUint16(6, this.width)
    v.setUint16(8, this.height)
    this.full = false
    this.lastRequestAt = this.o.now()
    this.requests.push(this.lastRequestAt)
    this.o.send(b)
  }
}

function u16(b: Uint8Array, at: number): number {
  return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint16(at)
}
function u32(b: Uint8Array, at: number): number {
  return new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(at)
}
function i32(b: Uint8Array, at: number): number {
  return new DataView(b.buffer, b.byteOffset, b.byteLength).getInt32(at)
}

/** 32 bpp true colour, little-endian, red at shift 0: the bytes land as R, G, B, x -- ImageData's order. */
function setPixelFormat(): Uint8Array {
  const b = new Uint8Array(20)
  const v = new DataView(b.buffer)
  b[0] = 0
  b[4] = 32 // bits per pixel
  b[5] = 24 // depth
  b[6] = 0 // little-endian
  b[7] = 1 // true colour
  v.setUint16(8, 255)
  v.setUint16(10, 255)
  v.setUint16(12, 255)
  b[14] = 0 // red shift
  b[15] = 8 // green shift
  b[16] = 16 // blue shift
  return b
}

function setEncodings(encodings: readonly number[]): Uint8Array {
  const b = new Uint8Array(4 + 4 * encodings.length)
  const v = new DataView(b.buffer)
  b[0] = 2
  v.setUint16(2, encodings.length)
  encodings.forEach((e, i) => v.setInt32(4 + 4 * i, e))
  return b
}

function bytesOf(data: unknown): Uint8Array | null {
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
  return null
}

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

type Status = 'connecting' | 'live' | 'reconnecting' | 'none' | 'unavailable'

const STATUS_WORD: Record<Status, string> = {
  connecting: 'connecting',
  live: 'live · ≤1 fps',
  reconnecting: 'reconnecting',
  none: 'no live view',
  unavailable: 'live view unavailable',
}

const STATUS_NOTE: Record<Status, string> = {
  connecting: 'Opening the live view of this step’s browser.',
  live: 'The masked live view of this step’s browser, at most one frame a second. Open the Browser tab for the full view.',
  reconnecting: 'The live view closed; it reconnects on its own.',
  none: 'This step has no live browser session right now. The card checks again on its own.',
  unavailable: 'The live view could not be opened. The task may have stopped, or this view may not be allowed.',
}

export function LiveThumb({
  taskId,
  workflowId,
  open = openLiveSocket,
}: {
  taskId: string
  workflowId: string | null
  open?: OpenLive
}) {
  const canvas = useRef<HTMLCanvasElement | null>(null)
  const failures = useRef(0)
  const [status, setStatus] = useState<Status>('connecting')
  const [frames, setFrames] = useState(0)
  // Bumped to open a new connection: a reconnect, or Retry.
  const [generation, setGeneration] = useState(0)

  useEffect(() => {
    const sock = open(taskId)
    if (sock === null) {
      setStatus('unavailable')
      return
    }
    let ended = false
    let sawFrame = false
    let reconnect: ReturnType<typeof setTimeout> | null = null
    const sink: ThumbSink = {
      resize(w, h) {
        const c = canvas.current
        if (c === null) return
        c.width = w
        c.height = h
      },
      draw(pixels, w, h) {
        const ctx = canvas.current?.getContext('2d') ?? null
        if (ctx !== null && typeof ImageData !== 'undefined') ctx.putImageData(new ImageData(pixels, w, h), 0, 0)
        sawFrame = true
        failures.current = 0
        setFrames((n) => n + 1)
        setStatus('live')
      },
    }
    const client = new RfbThumb({
      send: (b) => sock.send(b),
      sink,
      now: () => Date.now(),
      later: (fn, ms) => {
        const t = setTimeout(fn, ms)
        return () => clearTimeout(t)
      },
    })
    const finish = (why?: unknown) => {
      if (ended) return
      ended = true
      client.stop()
      sock.onmessage = null
      sock.onclose = null
      sock.onerror = null
      sock.close()
      if (why === NO_LIVE_SESSION) {
        // Not a failure of the stream: there is none. Said as such, and only
        // the session is read again, slowly (`NO_LIVE_SESSION`).
        failures.current = 0
        setStatus('none')
        reconnect = setTimeout(() => setGeneration((g) => g + 1), THUMB_SESSION_RECHECK_MS)
        return
      }
      if (!sawFrame) failures.current += 1
      if (failures.current >= THUMB_MAX_FAILURES) {
        setStatus('unavailable')
        return
      }
      setStatus('reconnecting')
      reconnect = setTimeout(() => setGeneration((g) => g + 1), THUMB_RECONNECT_MS)
    }
    sock.binaryType = 'arraybuffer'
    sock.onmessage = (ev) => {
      const bytes = bytesOf(ev.data)
      try {
        if (bytes === null) throw new RfbThumbError('a text message on a binary stream')
        client.feed(bytes)
      } catch {
        finish()
      }
    }
    sock.onclose = (ev) => finish(ev)
    sock.onerror = () => finish()
    return () => {
      // Unmount (the step finished, the card closed, another step was picked):
      // the stream ends here and does not reconnect.
      if (reconnect !== null) clearTimeout(reconnect)
      if (ended) return
      ended = true
      client.stop()
      sock.onmessage = null
      sock.onclose = null
      sock.onerror = null
      sock.close()
    }
  }, [taskId, open, generation])

  return (
    // A FIXED BOX: `.wf-live` fills the node's `.node-live` slot, whose height
    // is `LIVE_CARD_H` (dag.ts) less the gap; the canvas letterboxes into what
    // the head and link rows leave (styles/workflows.css).
    <section className="wf-live" aria-label="Live browser" data-status={status}>
      <div className="wf-live-head">
        <span className="ctl-eyebrow">live browser</span>{' '}
        <span
          className={`ctl-mark ${
            status === 'live' ? 'is-live' : status === 'unavailable' ? 'is-unread' : status === 'none' ? 'is-absent' : 'is-pending'
          }`} title={STATUS_NOTE[status]}>
          {STATUS_WORD[status]}
        </span>
        {status === 'unavailable' && (
          <>
            {' '}
            <button
              type="button"
              className="ctl-link"
              onClick={() => {
                failures.current = 0
                setStatus('connecting')
                setGeneration((g) => g + 1)
              }}
            >
              Retry
            </button>
          </>
        )}
      </div>
      {/* WHILE ITS BROWSER STARTS (owner request 2026-10-13): a thin bar and
          "starting m:ss" until the first frame; it draws nothing once done. */}
      {status !== 'live' && <StartupBar taskId={taskId} />}
      <canvas
        ref={canvas}
        className="wf-live-thumb"
        role="img"
        aria-label={`${STATUS_NOTE[status]} Frames shown: ${frames}.`}
        data-frames={frames}
      />
      <div className="wf-live-links">
        <a className="ctl-link" href={browserTabPath(taskId)}>
          Open Browser tab
        </a>{' '}
        <a className="ctl-link" href={theaterPath(taskId, workflowId)} aria-label="Open in theater">
          ⤢ Theater
        </a>
      </div>
    </section>
  )
}
