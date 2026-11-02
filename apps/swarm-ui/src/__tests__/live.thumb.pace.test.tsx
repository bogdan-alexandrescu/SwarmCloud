// The workflow graph's live thumbnails cost what is ON SCREEN, and update
// faster when only a few are (owner request 2026-11-02, part of #2688).
//
// WHAT EACH CASE HOLDS:
//   * the client reads its floor between requests before each one, so the
//     same card asks every 250 ms with three or fewer cards on screen and
//     every 1 s with more (`thumbIntervalMs`);
//   * a card off screen asks for no frame; the request that came due while it
//     was away goes out when it is back (`resume`);
//   * the card's label says "≤4 fps" only while few cards are on screen.
//
// MUTATIONS: read `minIntervalMs` once in the constructor (the 250 ms case
// asks at 1 s and turns red); ignore `paused` (the off-screen case sends a
// request and turns red).

import { act, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  LIVE_FAST_WORD,
  LiveThumb,
  RfbThumb,
  THUMB_FEW_INTERVAL_MS,
  THUMB_FEW_VISIBLE,
  THUMB_MIN_INTERVAL_MS,
  thumbIntervalMs,
  type LiveSocket,
} from '../LiveThumb'

/** A clock and a timer queue the test advances by hand. */
function clock() {
  let t = 0
  const timers: { at: number; fn: () => void; live: boolean }[] = []
  return {
    now: () => t,
    later: (fn: () => void, ms: number) => {
      const timer = { at: t + ms, fn, live: true }
      timers.push(timer)
      return () => {
        timer.live = false
      }
    },
    advance(ms: number) {
      const end = t + ms
      for (;;) {
        const due = timers.filter((x) => x.live && x.at <= end).sort((a, b) => a.at - b.at)[0]
        if (due === undefined) break
        due.live = false
        t = due.at
        due.fn()
      }
      t = end
    },
  }
}

function u16(n: number): number[] {
  return [(n >> 8) & 255, n & 255]
}
function u32(n: number): number[] {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]
}

/** The server's side up to ServerInit for a 2x1 framebuffer, then one empty update. */
const HANDSHAKE = Uint8Array.from([
  ...new TextEncoder().encode('RFB 003.008\n'),
  1, 1, // one security type: None
  ...u32(0), // security result OK
  ...u16(2), ...u16(1), ...new Array<number>(16).fill(0), ...u32(0), // ServerInit, no name
])
const EMPTY_UPDATE = Uint8Array.from([0, 0, ...u16(0)])

function client(o: { interval: () => number; paused?: () => boolean }) {
  const c = clock()
  const sent: Uint8Array[] = []
  const rfb = new RfbThumb({
    send: (b) => sent.push(b),
    sink: { resize() {}, draw() {} },
    now: c.now,
    later: c.later,
    minIntervalMs: o.interval,
    paused: o.paused,
  })
  rfb.feed(HANDSHAKE)
  return { rfb, c }
}

describe('thumbIntervalMs', () => {
  it('is 250 ms with few cards on screen and the 1 s floor with more', () => {
    expect(thumbIntervalMs(1)).toBe(THUMB_FEW_INTERVAL_MS)
    expect(thumbIntervalMs(THUMB_FEW_VISIBLE)).toBe(250)
    expect(thumbIntervalMs(THUMB_FEW_VISIBLE + 1)).toBe(THUMB_MIN_INTERVAL_MS)
    expect(thumbIntervalMs(40)).toBe(1000)
  })
})

describe('the client paces by what is on screen now', () => {
  it('reads the floor before each request', () => {
    let visible = 1
    const { rfb, c } = client({ interval: () => thumbIntervalMs(visible) })
    expect(rfb.requests).toEqual([0])
    rfb.feed(EMPTY_UPDATE)
    c.advance(250)
    expect(rfb.requests).toEqual([0, 250])
    visible = 10
    rfb.feed(EMPTY_UPDATE)
    c.advance(999)
    expect(rfb.requests).toEqual([0, 250])
    c.advance(1)
    expect(rfb.requests).toEqual([0, 250, 1250])
  })

  it('asks for nothing off screen and asks again when back', () => {
    let away = false
    const { rfb, c } = client({ interval: () => 1000, paused: () => away })
    away = true
    rfb.feed(EMPTY_UPDATE)
    c.advance(10_000)
    expect(rfb.requests).toEqual([0])
    away = false
    rfb.resume()
    expect(rfb.requests).toEqual([0, 10_000])
    rfb.resume()
    expect(rfb.requests).toHaveLength(2)
  })
})

describe('the card says its rate', () => {
  it('reads ≤4 fps while it is one of few on screen', () => {
    const sock = {
      binaryType: 'arraybuffer',
      send() {},
      close() {},
      onmessage: null,
      onclose: null,
      onerror: null,
    } as unknown as LiveSocket
    render(<LiveThumb taskId="task_one" workflowId={null} open={() => sock} />)
    const handler = (sock as unknown as { onmessage: (ev: { data: ArrayBuffer }) => void }).onmessage
    const bytes = Uint8Array.from([...HANDSHAKE, 0, 0, ...u16(1), ...u16(0), ...u16(0), ...u16(2), ...u16(1), ...u32(0), ...new Array<number>(8).fill(9)])
    act(() => handler({ data: bytes.buffer }))
    expect(screen.getByText(LIVE_FAST_WORD)).toBeTruthy()
  })
})
