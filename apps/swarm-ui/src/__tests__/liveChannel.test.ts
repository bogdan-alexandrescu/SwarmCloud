import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { openLiveSocket } from '../api'

// @ts-expect-error -- noVNC 1.4.0 ships no type declarations; this test drives its real Websock.
import Websock from '@novnc/novnc/core/websock.js'

/**
 * LB-STREAM (#1030). swarm-api sends its own `{"type": "status"}` TEXT frames
 * on the live socket ahead of, and between, the relay's binary RFB frames.
 * Measured 2026-10-12 on SwarmCloud: the first frame was the status, the
 * second "RFB 003.008\n"; noVNC was handed the raw socket, read the status as
 * RFB bytes, and the Browser tab sat on "Connecting" forever.
 */

/** A WebSocket as the browser has one: it fires BOTH its `on*` property and its listeners. */
class FakeRaw extends EventTarget {
  static made: FakeRaw[] = []
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
    FakeRaw.made.push(this)
  }
  send(d: unknown) {
    this.sent.push(d)
  }
  close() {
    this.readyState = 3
  }
  fire(type: 'open' | 'message' | 'close' | 'error', ev: Event) {
    const handler = (this as unknown as Record<string, ((e: Event) => void) | null>)[`on${type}`]
    handler?.call(this, ev)
    this.dispatchEvent(ev)
  }
  open() {
    this.readyState = 1
    this.fire('open', new Event('open'))
  }
  text(data: string) {
    this.fire('message', new MessageEvent('message', { data }))
  }
  binary(data: ArrayBuffer) {
    this.fire('message', new MessageEvent('message', { data }))
  }
}

const RFB_VERSION = 'RFB 003.008\n'
const versionBytes = (): ArrayBuffer => new TextEncoder().encode(RFB_VERSION).buffer as ArrayBuffer
const statusFrame = JSON.stringify({ type: 'status', open: true, in_control: false, controller: null })

// Built at run time: nothing here may look like a credential in the source.
const ticket = () => `vt.${crypto.randomUUID()}`

beforeEach(() => {
  FakeRaw.made = []
  vi.stubGlobal('WebSocket', FakeRaw)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

interface Consumer {
  onmessage: ((e: { data: unknown }) => void) | null
  onclose: ((e: unknown) => void) | null
  onerror: ((e: unknown) => void) | null
}

describe('the live socket hands an RFB client binary frames only (LB-STREAM)', () => {
  it('delivers ONLY "RFB 003.008\\n" to the consumer, after the ticket, with the status frame going to the callback', async () => {
    const got: unknown[] = []
    const statuses: unknown[] = []
    const t = ticket()
    const attached = openLiveSocket(
      'ls_one',
      t,
      (ch) => {
        const c = ch as unknown as Consumer
        c.onmessage = (e) => got.push(e.data)
        return ch
      },
      (s) => statuses.push(s),
    )
    const raw = FakeRaw.made[0]!
    raw.open()
    await attached
    expect(raw.sent[0], 'the ticket is the first message').toBe(JSON.stringify({ ticket: t }))
    raw.text(statusFrame)
    const version = versionBytes()
    raw.binary(version)
    expect(got, 'a text status frame reached the RFB client').toEqual([version])
    expect(new TextDecoder().decode(got[0] as ArrayBuffer)).toBe(RFB_VERSION)
    expect(statuses).toEqual([{ open: true, in_control: false, controller: null, cause: null }])
  })

  it('keeps binary frames in order around later status frames', async () => {
    const got: unknown[] = []
    const statuses: { in_control: boolean; controller: string | null }[] = []
    const attached = openLiveSocket(
      'ls_one',
      ticket(),
      (ch) => {
        ;(ch as unknown as Consumer).onmessage = (e) => got.push(e.data)
      },
      (s) => statuses.push(s),
    )
    const raw = FakeRaw.made[0]!
    raw.open()
    await attached
    const a = new Uint8Array([1]).buffer
    const b = new Uint8Array([2]).buffer
    const who = ['ada', 'example.com'].join('@')
    raw.binary(a)
    raw.text(JSON.stringify({ type: 'status', open: true, in_control: true, controller: who }))
    raw.binary(b)
    raw.text('not json')
    expect(got).toEqual([a, b])
    expect(statuses).toEqual([{ open: true, in_control: true, controller: who, cause: null }])
  })

  it('hands the relay’s control refusal to its own callback, with its cause, and never to the RFB client (LB-CURSOR2)', async () => {
    const got: unknown[] = []
    const refusals: string[] = []
    const attached = openLiveSocket(
      'ls_one',
      ticket(),
      (ch) => {
        ;(ch as unknown as Consumer).onmessage = (e) => got.push(e.data)
      },
      undefined,
      (cause) => refusals.push(cause),
    )
    const raw = FakeRaw.made[0]!
    raw.open()
    await attached
    raw.text(JSON.stringify({ type: 'refused', reason: 'control', cause: 'wrong_session' }))
    // An older relay names no cause; anything that is not a code reads as unknown.
    raw.text(JSON.stringify({ type: 'refused', reason: 'control' }))
    raw.text(JSON.stringify({ type: 'refused', reason: 'control', cause: '<b>x</b>' }))
    // A refusal of anything else is not a control refusal.
    raw.text(JSON.stringify({ type: 'refused', reason: 'malformed' }))
    expect(refusals).toEqual(['wrong_session', 'unknown', 'unknown'])
    expect(got).toEqual([])
  })

  it('propagates error and close to the consumer once it holds the channel', async () => {
    const errors: unknown[] = []
    const closes: unknown[] = []
    const attached = openLiveSocket('ls_one', ticket(), (ch) => {
      const c = ch as unknown as Consumer
      c.onerror = (e) => errors.push(e)
      c.onclose = (e) => closes.push(e)
    })
    const raw = FakeRaw.made[0]!
    raw.open()
    await attached
    const err = new Event('error')
    raw.fire('error', err)
    const closed = new Event('close')
    raw.readyState = 3
    raw.fire('close', closed)
    expect(errors).toEqual([err])
    expect(closes).toEqual([closed])
  })

  it('rejects when the socket closes before it opens, and attaches nothing', async () => {
    const attach = vi.fn()
    const attached = openLiveSocket('ls_one', ticket(), attach)
    const raw = FakeRaw.made[0]!
    raw.readyState = 3
    raw.fire('close', new Event('close'))
    await expect(attached).rejects.toThrow('closed before it opened')
    expect(attach).not.toHaveBeenCalled()
  })

  it('is a channel noVNC 1.4.0 accepts, and noVNC reads the handshake rather than the status', async () => {
    const sock = new Websock()
    const opened = vi.fn()
    let read: string | null = null
    sock.on('open', opened)
    sock.on('message', () => {
      read = sock.rQshiftStr(sock.rQlen)
    })
    const attached = openLiveSocket('ls_one', ticket(), (ch) => {
      // What noVNC's RFB does with a channel: attach it (which checks its members), then read readyState.
      sock.attach(ch)
      return sock.readyState as string
    })
    const raw = FakeRaw.made[0]!
    raw.open()
    expect(await attached).toBe('open')
    raw.text(statusFrame)
    raw.binary(versionBytes())
    expect(read).toBe(RFB_VERSION)
    expect(raw.binaryType).toBe('arraybuffer')
    sock.close()
    expect(raw.readyState, 'noVNC closing the channel closes the socket').toBe(3)
  })
})
