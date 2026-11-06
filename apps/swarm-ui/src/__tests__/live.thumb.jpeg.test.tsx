// THE NEW GATE PROTOCOL: thumbnail mode, Tight-encoded (part of #2688).
//
// The gate (`livegate/rfb.py`) now scales the framebuffer on its own side and
// sends it as Tight rectangles -- a JPEG-compressed rectangle, or a flat-colour
// fill -- once asked for thumbnail mode with a private pseudo-encoding that
// names the wanted width. This client asks for that mode, decodes what comes
// back, and still never drives the remote browser.
//
// WHAT EACH CASE HOLDS:
//   * SetEncodings carries Tight, Raw, DesktopSize and the thumbnail
//     pseudo-encoding with the device-pixel width this card wants
//     (`thumbWidth`, `ENCODING_THUMB_WIDTH_0`);
//   * no client message this view ever writes is a key, pointer or cut-text
//     message (types 4, 5, 6) -- it is view only, by construction;
//   * a DesktopSize pseudo-rectangle followed by a Tight JPEG rectangle
//     decodes (`createImageBitmap`) and draws onto the canvas at the
//     rectangle's position and size;
//   * a Tight fill rectangle paints its flat colour, with no decode at all.
//
// MUTATIONS: drop the thumbnail pseudo-encoding from SetEncodings (the width
// case turns red); paint a Tight JPEG rectangle before its decode resolves
// (the order case turns red, drawImage would see the wrong, or no, bitmap).

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ENCODING_THUMB_WIDTH_0, LiveThumb, RfbThumb, thumbWidth, type LiveSocket, type ThumbSink } from '../LiveThumb'

function u16(n: number): number[] {
  return [(n >> 8) & 255, n & 255]
}
function u32(n: number): number[] {
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255]
}
/** A big-endian signed 32-bit encoding value, as the rectangle header carries it. */
function i32(n: number): number[] {
  const b = new Uint8Array(4)
  new DataView(b.buffer).setInt32(0, n)
  return [...b]
}

const HANDSHAKE = Uint8Array.from([
  ...new TextEncoder().encode('RFB 003.008\n'),
  1, 1, // one security type: None
  ...u32(0), // security result OK
  ...u16(100), ...u16(100), ...new Array<number>(16).fill(0), ...u32(0), // ServerInit, no name
])

/** Reads the four encodings of a SetEncodings message (type 2) sent to the server. */
function readSetEncodings(msg: Uint8Array): number[] {
  expect(msg[0]).toBe(2)
  const v = new DataView(msg.buffer, msg.byteOffset, msg.byteLength)
  const n = v.getUint16(2)
  const out: number[] = []
  for (let i = 0; i < n; i += 1) out.push(v.getInt32(4 + 4 * i))
  return out
}

describe('SetEncodings asks for thumbnail mode, and only ever looks', () => {
  it('carries Tight, Raw, DesktopSize and the thumbnail width pseudo-encoding', () => {
    const sent: Uint8Array[] = []
    const sink: ThumbSink = { resize() {}, fillRect() {}, drawRaw() {}, drawImage() {}, commit() {} }
    const rfb = new RfbThumb({
      send: (b) => sent.push(b),
      sink,
      now: () => 0,
      later: () => () => {},
      thumbWidthPx: () => 222,
    })
    rfb.feed(HANDSHAKE)
    // sent: [version, security choice, ClientInit, SetPixelFormat, SetEncodings, FramebufferUpdateRequest]
    const setEncodingsMsg = sent.find((m) => m[0] === 2)!
    const encodings = readSetEncodings(setEncodingsMsg)
    expect(encodings).toContain(7) // Tight
    expect(encodings).toContain(0) // Raw
    expect(encodings).toContain(-223) // DesktopSize
    expect(encodings).toContain(ENCODING_THUMB_WIDTH_0 + 222)
  })

  it('never sends a key, pointer or cut-text message (types 4, 5, 6)', () => {
    const sent: Uint8Array[] = []
    const sink: ThumbSink = { resize() {}, fillRect() {}, drawRaw() {}, drawImage() {}, commit() {} }
    const rfb = new RfbThumb({ send: (b) => sent.push(b), sink, now: () => 0, later: () => () => {} })
    rfb.feed(HANDSHAKE)
    for (const msg of sent) expect([4, 5, 6]).not.toContain(msg[0])
  })
})

describe('thumbWidth', () => {
  it('is the CSS width times the device pixel ratio, clamped to 1..640', () => {
    expect(thumbWidth(200, 2)).toBe(400)
    expect(thumbWidth(1000, 2)).toBe(640) // clamped: would be 2000
    expect(thumbWidth(0, 2)).toBe(640) // unknown width falls back to 320 CSS px, then *2
    expect(thumbWidth(200, 0)).toBe(200) // unknown ratio falls back to 1
  })
})

describe('Tight rectangles are decoded and painted in order', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function openSocket(): { sock: LiveSocket; handler: () => (ev: { data: ArrayBuffer }) => void } {
    const sock = {
      binaryType: 'arraybuffer',
      send() {},
      close() {},
      onmessage: null,
      onclose: null,
      onerror: null,
    } as unknown as LiveSocket
    return { sock, handler: () => (sock as unknown as { onmessage: (ev: { data: ArrayBuffer }) => void }).onmessage }
  }

  it('a DesktopSize then a Tight JPEG rectangle draws the decoded bitmap at its rect', async () => {
    const fakeBitmap = { width: 40, height: 20 } as unknown as ImageBitmap
    const decode = vi.fn(async () => fakeBitmap)
    vi.stubGlobal('createImageBitmap', decode)
    const drawImage = vi.fn()
    const fakeCtx = { drawImage, fillRect: vi.fn(), putImageData: vi.fn(), fillStyle: '' }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(fakeCtx as unknown as CanvasRenderingContext2D)

    const { sock, handler } = openSocket()
    render(<LiveThumb taskId="task_jpeg" workflowId={null} open={() => sock} />)

    const jpeg = Uint8Array.from([11, 22, 33, 44])
    const bytes = Uint8Array.from([
      ...HANDSHAKE,
      0, 0, ...u16(2), // FramebufferUpdate, 2 rectangles
      ...u16(0), ...u16(0), ...u16(50), ...u16(30), ...i32(-223), // DesktopSize: scaled to 50x30
      ...u16(3), ...u16(4), ...u16(40), ...u16(20), ...i32(7), // Tight rectangle at (3,4), 40x20
      0x90, jpeg.length, ...jpeg, // JPEG control byte, compact length, JPEG bytes
    ])
    await act(async () => {
      handler()({ data: bytes.buffer })
      // Let the stubbed `createImageBitmap` promise (and the client's own
      // await of it) resolve before the assertion.
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(decode).toHaveBeenCalledTimes(1)
    expect(drawImage).toHaveBeenCalledWith(fakeBitmap, 3, 4, 40, 20)
  })

  it('a Tight fill rectangle paints its colour, with no decode', () => {
    const decode = vi.fn()
    vi.stubGlobal('createImageBitmap', decode)
    const fillRect = vi.fn()
    const fakeCtx = { drawImage: vi.fn(), fillRect, putImageData: vi.fn(), fillStyle: '' }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(fakeCtx as unknown as CanvasRenderingContext2D)

    const { sock, handler } = openSocket()
    render(<LiveThumb taskId="task_fill" workflowId={null} open={() => sock} />)

    const bytes = Uint8Array.from([
      ...HANDSHAKE,
      0, 0, ...u16(1), // FramebufferUpdate, 1 rectangle
      ...u16(5), ...u16(6), ...u16(10), ...u16(8), ...i32(7), // Tight rectangle at (5,6), 10x8
      0x80, 200, 100, 50, // FILL control byte, then R, G, B
    ])
    act(() => handler()({ data: bytes.buffer }))

    expect(decode).not.toHaveBeenCalled()
    expect(fillRect).toHaveBeenCalledWith(5, 6, 10, 8)
    expect(fakeCtx.fillStyle).toBe('rgb(200, 100, 50)')
  })
})

describe('one update is painted in order, and a JPEG that fails to decode ends the stream', () => {
  function update(...rects: number[][]): Uint8Array {
    return Uint8Array.from([...HANDSHAKE, 0, 0, ...u16(rects.length), ...rects.flat()])
  }
  const jpegRect = (x: number, byte: number) => [...u16(x), ...u16(0), ...u16(10), ...u16(10), ...i32(7), 0x90, 1, byte]

  it('paints the first JPEG before the second even when the second decodes first', async () => {
    const drawn: number[] = []
    let releaseFirst: () => void = () => {}
    const decodeJpeg = (bytes: Uint8Array<ArrayBuffer>) =>
      bytes[0] === 1
        ? new Promise<ImageBitmap>((done) => {
            releaseFirst = () => done({ id: 1 } as unknown as ImageBitmap)
          })
        : Promise.resolve({ id: 2 } as unknown as ImageBitmap)
    const sink: ThumbSink = {
      resize() {}, fillRect() {}, drawRaw() {}, commit() {},
      drawImage(bitmap) {
        drawn.push((bitmap as unknown as { id: number }).id)
      },
    }
    const rfb = new RfbThumb({ send: () => {}, sink, now: () => 0, later: () => () => {}, decodeJpeg })
    rfb.feed(update(jpegRect(0, 1), jpegRect(20, 2)))
    await Promise.resolve()
    expect(drawn).toEqual([])
    releaseFirst()
    await new Promise((done) => setTimeout(done, 0))
    expect(drawn).toEqual([1, 2])
    expect(rfb.frames).toBe(1)
  })

  it('a rejected decode calls onError and sends no further request', async () => {
    const sent: Uint8Array[] = []
    const onError = vi.fn()
    const sink: ThumbSink = { resize() {}, fillRect() {}, drawRaw() {}, drawImage() {}, commit() {} }
    const later = vi.fn(() => () => {})
    const rfb = new RfbThumb({
      send: (b) => sent.push(b), sink, now: () => 0, later, onError,
      decodeJpeg: () => Promise.reject(new Error('not a JPEG')),
    })
    rfb.feed(update(jpegRect(0, 9)))
    await new Promise((done) => setTimeout(done, 0))
    expect(onError).toHaveBeenCalledTimes(1)
    expect(later).not.toHaveBeenCalled()
    expect(rfb.frames).toBe(0)
  })
})
