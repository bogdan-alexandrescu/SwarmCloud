// THE LIVE BROWSERS WALL DRAWS EACH BROWSER IN ITS DEVICE'S SHAPE (the
// owner's request of 2026-11-08: "the live browser views when in thumbnail
// mode should still display in the resolution, iPhone mode or iPad or web").
// Every wall card was one 16:10 box whatever the browser emulated.
//
//   * a phone is a narrow portrait frame, a tablet an iPad-proportioned one, a
//     desktop the web landscape frame; landscape turns phone and tablet;
//   * a device not reported keeps the web landscape frame the wall drew;
//   * the stream asked for is as wide as the frame, so a phone frame is not
//     fed a desktop-width picture.
//
// MUTATIONS: draw every card desktop (`deviceFrame` ignoring the device);
// ignore the orientation; guess a phone for an unreported device; ask the gate
// for the 320 px fallback whatever the frame.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ loadLiveSession: vi.fn(), loadTaskDigests: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: true }
})

import { LiveWall, deviceFrame, type RowDevice } from '../Agents'
import { ENCODING_THUMB_WIDTH_0, LiveThumb, THUMB_FRAME_H, frameCssWidth, frameRatio, type LiveSocket } from '../LiveThumb'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const PRESETS = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

function on(preset: string, orientation: 'portrait' | 'landscape'): RowDevice {
  return { device: { preset, orientation, touch: false }, presets: PRESETS }
}

describe('a wall card’s frame follows its session’s device and orientation', () => {
  it.each([
    ['iphone-15', 'portrait', 'phone', 'portrait'],
    ['iphone-15', 'landscape', 'phone', 'landscape'],
    ['ipad-7', 'portrait', 'tablet', 'portrait'],
    ['ipad-7', 'landscape', 'tablet', 'landscape'],
    ['desktop', 'landscape', 'desktop', 'landscape'],
    // A desktop is the web landscape frame whatever orientation is reported.
    ['desktop', 'portrait', 'desktop', 'landscape'],
  ] as const)('%s in %s is a %s %s frame', (preset, orientation, device, drawn) => {
    expect(deviceFrame(on(preset, orientation))).toEqual({ device, orientation: drawn })
  })

  it('a device not reported, or not read yet, keeps the web landscape frame', () => {
    expect(deviceFrame(null)).toEqual({ device: 'desktop', orientation: 'landscape' })
    expect(deviceFrame({ device: null, presets: PRESETS })).toEqual({ device: 'desktop', orientation: 'landscape' })
    expect(deviceFrame(on('kiosk-x', 'portrait'))).toEqual({ device: 'desktop', orientation: 'landscape' })
  })

  it('a phone is narrow and tall, a tablet iPad-shaped, the web 16:10; landscape turns them', () => {
    expect(frameRatio({ device: 'phone', orientation: 'portrait' })).toBeCloseTo(9 / 19.5)
    expect(frameRatio({ device: 'phone', orientation: 'landscape' })).toBeCloseTo(19.5 / 9)
    expect(frameRatio({ device: 'tablet', orientation: 'portrait' })).toBeCloseTo(3 / 4)
    expect(frameRatio({ device: 'tablet', orientation: 'landscape' })).toBeCloseTo(4 / 3)
    expect(frameRatio({ device: 'desktop', orientation: 'landscape' })).toBeCloseTo(16 / 10)
    expect(frameCssWidth({ device: 'phone', orientation: 'portrait' })).toBe(Math.round((THUMB_FRAME_H * 9) / 19.5))
  })
})

function browser(n: number): Task {
  return runTask({
    id: `task_${n.toString(16).padStart(20, '0')}`,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    started_at: new Date(Date.UTC(2026, 10, 8, 3, 0, n)).toISOString(),
    completed_at: null,
  })
}

describe('the Live browsers wall draws each card in its frame', () => {
  const tasks = [browser(1), browser(2), browser(3), browser(4)]
  const card = (t: Task) => document.querySelector(`.ag-wall-card[data-task-id="${t.id}"]`)!
  const picture = (t: Task) => card(t).querySelector('canvas.wf-live-thumb')!

  it('a phone, a turned tablet, a desktop and an unreported device each take their shape', () => {
    const devices = new Map<string, RowDevice>([
      [tasks[0]!.id, on('iphone-15', 'portrait')],
      [tasks[1]!.id, on('ipad-7', 'landscape')],
      [tasks[2]!.id, on('desktop', 'landscape')],
      [tasks[3]!.id, { device: null, presets: PRESETS }],
    ])
    render(<LiveWall tasks={tasks} onOpen={() => {}} title={(t) => t.id} devices={devices} />)
    const drawn = tasks.map((t) => [picture(t).getAttribute('data-device'), picture(t).getAttribute('data-orientation')])
    expect(drawn).toEqual([
      ['phone', 'portrait'],
      ['tablet', 'landscape'],
      ['desktop', 'landscape'],
      ['desktop', 'landscape'],
    ])
    // The card carries the shape too: the stylesheet sizes the card to its frame.
    expect(card(tasks[0]!).classList.contains('is-phone')).toBe(true)
    expect(card(tasks[0]!).classList.contains('is-portrait')).toBe(true)
    expect(card(tasks[1]!).classList.contains('is-tablet')).toBe(true)
    expect(card(tasks[1]!).classList.contains('is-landscape')).toBe(true)
  })

  it('with no device read at all every card is the web landscape frame, as before', () => {
    render(<LiveWall tasks={tasks} onOpen={() => {}} title={(t) => t.id} />)
    for (const t of tasks) {
      expect(picture(t).getAttribute('data-device')).toBe('desktop')
      expect(card(t).classList.contains('is-desktop')).toBe(true)
    }
    expect(api.loadLiveSession, 'a session read per card').not.toHaveBeenCalled()
  })
})

describe('the stylesheet draws the same frames', () => {
  const css = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')

  it('every frame on the wall is at most THUMB_FRAME_H tall, as the stream asked for assumes', () => {
    expect(css).toMatch(new RegExp(`\\.ag-wall-grid \\{[^}]*--wall-frame-max: ${THUMB_FRAME_H}px;`))
    expect(css).toMatch(/\.ag-wall-grid \{[^}]*--wall-frame-h: clamp\([^;]*var\(--wall-frame-max\)\);/)
  })

  it('a narrow wall shrinks the shared height so a desktop card fits beside a tablet or phone card', () => {
    // The grid is the container its cards' `cqi` measures.
    expect(css).toMatch(/\.ag-wall-grid \{[^}]*container-type: inline-size;/)
    // Desktop 16:10 beside tablet portrait 3:4, and beside a phone card at its 6.5rem minimum.
    expect(css).toMatch(/--wall-frame-h: clamp\(90px, min\(\(100cqi - 32px\) \/ 2\.35, \(100cqi - 6\.5rem - 22px\) \/ 1\.6\)/)
    expect(css).toMatch(/\.ag-wall-card \{[^}]*min-width: min\(6\.5rem, 100%\);/)
  })

  it('a status badge wraps inside a narrow card rather than running past its border', () => {
    expect(css).toMatch(/\.ag-wall-card \.wf-live-head \.ctl-mark \{[^}]*white-space: normal;[^}]*max-width: 100%;/)
  })

  it.each([
    ['is-phone.is-portrait', '9 / 19.5'],
    ['is-phone.is-landscape', '19.5 / 9'],
    ['is-tablet.is-portrait', '3 / 4'],
    ['is-tablet.is-landscape', '4 / 3'],
  ])('a %s card is %s', (cls, ratio) => {
    const rule = new RegExp(`\\.ag-wall-card\\.${cls.replace('.', '\\.')} \\{[^}]*--frame-ratio: ${ratio.replace('.', '\\.')};`)
    expect(css).toMatch(rule)
  })

  it('the picture letterboxes into its frame rather than stretching', () => {
    expect(css).toMatch(/\.ag-wall-card \.wf-live-thumb \{[^}]*aspect-ratio: var\(--frame-ratio\);[^}]*object-fit: contain;/)
  })
})

const HANDSHAKE = Uint8Array.from([
  ...new TextEncoder().encode('RFB 003.008\n'),
  1, 1,
  0, 0, 0, 0,
  0, 100, 0, 100, ...new Array<number>(16).fill(0), 0, 0, 0, 0,
])

/** The thumbnail width a LiveThumb asks the gate for, in jsdom (which never lays out). */
function askedWidth(frame: Parameters<typeof LiveThumb>[0]['frame']): number {
  const sent: Uint8Array[] = []
  const sock = { binaryType: 'arraybuffer', send: (b: Uint8Array) => sent.push(b), close() {}, onmessage: null, onclose: null, onerror: null }
  const { unmount } = render(<LiveThumb taskId="task_frame" workflowId={null} open={() => sock as unknown as LiveSocket} frame={frame ?? null} />)
  act(() => {
    ;(sock as unknown as { onmessage: (ev: { data: ArrayBuffer }) => void }).onmessage({ data: HANDSHAKE.buffer })
  })
  unmount()
  const msg = sent.find((m) => m[0] === 2)!
  const v = new DataView(msg.buffer, msg.byteOffset, msg.byteLength)
  const codes = Array.from({ length: v.getUint16(2) }, (_, i) => v.getInt32(4 + 4 * i))
  return codes.find((c) => c >= ENCODING_THUMB_WIDTH_0 && c < ENCODING_THUMB_WIDTH_0 + 4096)! - ENCODING_THUMB_WIDTH_0
}

describe('the stream asked for suits the frame', () => {
  it('a phone frame asks for a phone frame’s width, not a desktop card’s', () => {
    const was = window.devicePixelRatio
    Object.defineProperty(window, 'devicePixelRatio', { value: 1, configurable: true })
    try {
      expect(askedWidth({ device: 'phone', orientation: 'portrait' })).toBe(frameCssWidth({ device: 'phone', orientation: 'portrait' }))
      expect(askedWidth({ device: 'tablet', orientation: 'portrait' })).toBe(frameCssWidth({ device: 'tablet', orientation: 'portrait' }))
      expect(askedWidth({ device: 'desktop', orientation: 'landscape' })).toBe(frameCssWidth({ device: 'desktop', orientation: 'landscape' }))
      // No frame (the workflow card): the 320 px fallback, unchanged.
      expect(askedWidth(null)).toBe(320)
    } finally {
      Object.defineProperty(window, 'devicePixelRatio', { value: was, configurable: true })
    }
  })
})
