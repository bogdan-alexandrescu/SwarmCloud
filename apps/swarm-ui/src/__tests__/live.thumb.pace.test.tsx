// The workflow graph's live thumbnails cost what is ON SCREEN, and update
// faster when only a few are (owner request 2026-11-02; pacing table the owner's
// pick on 2026-11-06, part of #2688).
//
// WHAT EACH CASE HOLDS:
//   * the client reads its floor between requests before each one, so the
//     same card asks every 100 ms with six or fewer cards on screen, every
//     200 ms with seven to twenty, and every 500 ms beyond (`thumbIntervalMs`,
//     `THUMB_INTERVALS`);
//   * a card off screen asks for no frame; the request that came due while it
//     was away goes out when it is back (`resume`);
//   * the card's live badge reads the rate it is actually asking for, not a
//     hard-coded number;
//   * a use may pass its own slower pace (`paceMs`, Home's tiny rows at about
//     a second), and the table's floor still holds under it.
//
// MUTATIONS: read `minIntervalMs` once in the constructor (the 100 ms case
// asks at 500 ms and turns red); ignore `paused` (the off-screen case sends a
// request and turns red).

import { act, cleanup, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { LiveThumb, RfbThumb, THUMB_INTERVALS, pacedIntervalMs, thumbIntervalMs, type LiveSocket, type ThumbSink } from '../LiveThumb'

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

/** A `ThumbSink` that does nothing: these tests watch `requests`, not pixels. */
const NULL_SINK: ThumbSink = {
  resize() {},
  fillRect() {},
  drawRaw() {},
  drawImage() {},
  commit() {},
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
    sink: NULL_SINK,
    now: c.now,
    later: c.later,
    minIntervalMs: o.interval,
    paused: o.paused,
  })
  rfb.feed(HANDSHAKE)
  return { rfb, c }
}

describe('thumbIntervalMs', () => {
  it('is 100 ms at six or fewer cards, 200 ms up to twenty, and 500 ms beyond', () => {
    expect(thumbIntervalMs(1)).toBe(100)
    expect(thumbIntervalMs(6)).toBe(100)
    expect(thumbIntervalMs(7)).toBe(200)
    expect(thumbIntervalMs(20)).toBe(200)
    expect(thumbIntervalMs(21)).toBe(500)
    expect(thumbIntervalMs(40)).toBe(500)
  })

  it('matches the published table', () => {
    expect(THUMB_INTERVALS).toEqual([
      [6, 100],
      [20, 200],
      [Infinity, 500],
    ])
  })
})

describe('the client paces by what is on screen now', () => {
  it('reads the floor before each request', () => {
    let visible = 1
    const { rfb, c } = client({ interval: () => thumbIntervalMs(visible) })
    expect(rfb.requests).toEqual([0])
    rfb.feed(EMPTY_UPDATE)
    c.advance(100)
    expect(rfb.requests).toEqual([0, 100])
    visible = 10
    rfb.feed(EMPTY_UPDATE)
    c.advance(199)
    expect(rfb.requests).toEqual([0, 100])
    c.advance(1)
    expect(rfb.requests).toEqual([0, 100, 300])
  })

  it('asks for nothing off screen and asks again when back', () => {
    let away = false
    const { rfb, c } = client({ interval: () => 500, paused: () => away })
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
  it('reads the rate for how many cards are on screen, not a hard-coded number', () => {
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
    // One card on screen: the 100 ms tier, 10 fps.
    expect(screen.getByText('live · ≤10 fps')).toBeTruthy()
  })
})

describe("a use's own pace (Home's tiny thumbnails)", () => {
  it('only slows the table, never speeds it', () => {
    expect(pacedIntervalMs(1, 1000)).toBe(1000)
    expect(pacedIntervalMs(40, 1000)).toBe(1000)
    expect(pacedIntervalMs(40, 50)).toBe(500)
    expect(pacedIntervalMs(1)).toBe(100)
  })

  it('a card paced at a second asks for its next frame a second after the last, and says so', () => {
    vi.useFakeTimers({ now: 0 })
    try {
      const sent: Uint8Array[] = []
      const sock = {
        binaryType: 'arraybuffer',
        send(b: Uint8Array) {
          sent.push(b)
        },
        close() {},
        onmessage: null,
        onclose: null,
        onerror: null,
      } as unknown as LiveSocket
      render(<LiveThumb taskId="task_one" workflowId={null} open={() => sock} paceMs={1000} />)
      const handler = (sock as unknown as { onmessage: (ev: { data: ArrayBuffer }) => void }).onmessage
      const requests = () => sent.filter((b) => b[0] === 3 && b.length === 10).length
      const bytes = Uint8Array.from([...HANDSHAKE, 0, 0, ...u16(1), ...u16(0), ...u16(0), ...u16(2), ...u16(1), ...u32(0), ...new Array<number>(8).fill(9)])
      act(() => handler({ data: bytes.buffer }))
      const first = requests()
      expect(first).toBeGreaterThan(0)
      act(() => {
        vi.advanceTimersByTime(999)
      })
      expect(requests()).toBe(first)
      act(() => {
        vi.advanceTimersByTime(1)
      })
      expect(requests()).toBe(first + 1)
      expect(screen.getByText('live · ≤1 fps')).toBeTruthy()
    } finally {
      cleanup()
      vi.useRealTimers()
    }
  })
})
