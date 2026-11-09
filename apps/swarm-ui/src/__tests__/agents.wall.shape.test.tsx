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
// THE WALL PACKS ITS CARDS WHATEVER THEIR ORDER (review of 2026-11-08): a
// flex-wrap row left a phone card alone in its row and fitted one desktop card
// a row on the inspector's 317px wall. Cards sit on grid tracks of one narrow
// unit: a portrait phone or tablet spans one, every other card two, and dense
// flow fills the gaps.
//
// MUTATIONS: draw every card desktop (`deviceFrame` ignoring the device);
// ignore the orientation; guess a phone for an unreported device; ask the gate
// for the 320 px fallback whatever the frame; go back to the flex-wrap row;
// drop `dense`; span a phone two tracks or a desktop one; let the shared
// height outgrow two tracks.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, render } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ loadLiveSession: vi.fn(), loadTaskDigests: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: true }
})

import { LiveWall, deviceFrame, type RowDevice } from '../Agents'
import { ENCODING_THUMB_WIDTH_0, LiveThumb, THUMB_FRAME_H, frameCssWidth, frameRatio, type LiveSocket } from '../LiveThumb'
import type { Task } from '../types'
import { task as runTask } from './runfixture'
import { resolveSheet } from './spaceprobe'

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
    expect(css).toMatch(/\.ag-wall-grid \{[^}]*--wall-frame-h: clamp\([^;]*var\(--wall-frame-max, 180px\)\);/)
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

  it.each(['dark', 'light'] as const)('the %s sheet still resolves against the root tokens alone, as the contrast and spacing sweeps read it', (theme) => {
    // A wall-scoped name used with no fallback threw here and failed every other screen's sweep.
    expect(() => resolveSheet(css, theme)).not.toThrow()
  })

  it('the picture letterboxes into its frame rather than stretching, every frame the shared height', () => {
    expect(css).toMatch(/\.ag-wall-card \.wf-live-thumb \{[^}]*aspect-ratio: var\(--frame-ratio, 16 \/ 10\);[^}]*object-fit: contain;/)
    // One height for every frame, so the frames in a row are level whatever their shapes.
    expect(css).toMatch(/\.ag-wall-card \.wf-live-thumb \{[^}]*height: var\(--wall-frame-h, 180px\);/)
  })
})

/** The wall's own pixel constants: the grid's gap (`--ctl-s2`) and a card's padding and border on both sides. */
const GAP = 8
const CHROME = 2 * 4 + 2 * 1

/** A CSS length expression of the wall's sheet, evaluated for a wall `wall` px wide (`100cqi` and `100%` are the wall). */
function lengthAt(expr: string, wall: number): number {
  const js = expr
    .replace(/var\(--wall-frame-max, 180px\)/g, '180px')
    .replace(/100(cqi|%)/g, String(wall))
    .replace(/(\d+(?:\.\d+)?)rem/g, (_, n: string) => String(Number(n) * 16))
    .replace(/(\d+(?:\.\d+)?)px/g, '$1')
    .replace(/calc\(/g, '(')
    .replace(/\b(min|max)\(/g, 'Math.$1(')
    .replace(/\bclamp\(/g, 'clamp(')
  expect(js, `an expression this test cannot evaluate: ${expr}`).toMatch(/^[\d\s.+\-*/(),]*(?:(?:Math\.(?:min|max)|clamp)\([\d\s.+\-*/(),]*)*[\d\s.+\-*/(),]*$/)
  return new Function('clamp', `return ${js}`)((lo: number, v: number, hi: number) => Math.max(lo, Math.min(v, hi))) as number
}

/** Grid auto-placement with `grid-auto-flow: row dense` on `cols` tracks: the tracks each row leaves empty. */
function denseRows(spans: readonly number[], cols: number): number[] {
  const rows: number[][] = []
  for (const span of spans) {
    let placed = false
    for (let r = 0; !placed; r++) {
      rows[r] ??= new Array<number>(cols).fill(0)
      for (let c = 0; c + span <= cols && !placed; c++) {
        if (rows[r]!.slice(c, c + span).every((x) => x === 0)) {
          rows[r]!.fill(1, c, c + span)
          placed = true
        }
      }
    }
  }
  return rows.map((row) => row.filter((x) => x === 0).length)
}

describe('the wall packs its cards on narrow tracks, whatever their order', () => {
  const css = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')
  const sheet = document.createElement('style')
  beforeAll(() => {
    sheet.textContent = css
    document.head.appendChild(sheet)
  })
  afterAll(() => sheet.remove())

  const tasks = Array.from({ length: 9 }, (_, i) => browser(i + 1))
  const grid = () => document.querySelector('.ag-wall-grid')!
  const span = (t: Task) => {
    const value = getComputedStyle(document.querySelector(`.ag-wall-card[data-task-id="${t.id}"]`)!).gridColumn
    return /^span (\d+)$/.exec(value) === null ? null : Number(/^span (\d+)$/.exec(value)![1])
  }
  const wall = (shapes: readonly (RowDevice | null)[]) => {
    const devices = new Map<string, RowDevice>()
    shapes.forEach((d, i) => d !== null && devices.set(tasks[i]!.id, d))
    render(<LiveWall tasks={tasks.slice(0, shapes.length)} onOpen={() => {}} title={(t) => t.id} devices={devices} />)
    return tasks.slice(0, shapes.length).map(span)
  }
  const unreported: RowDevice = { device: null, presets: PRESETS }

  it.each([
    ['a portrait phone', 1, on('iphone-15', 'portrait')],
    ['a portrait tablet', 1, on('ipad-7', 'portrait')],
    ['a desktop', 2, on('desktop', 'landscape')],
    ['a turned tablet', 2, on('ipad-7', 'landscape')],
    ['a turned phone', 2, on('iphone-15', 'landscape')],
    ['a device not reported', 2, unreported],
    ['a device not read yet', 2, null],
  ] as const)('%s spans %i track(s)', (_, tracks, device) => {
    expect(wall([device])).toEqual([tracks])
  })

  it('the wall is a grid whose flow is dense, so a narrow card fills the gap a wide one leaves', () => {
    wall([null])
    const style = getComputedStyle(grid())
    expect(style.display).toBe('grid')
    expect(style.gridAutoFlow.split(/\s+/)).toContain('dense')
    expect(style.gridTemplateColumns).toMatch(/^repeat\(auto-fill, minmax\(.*, 1fr\)\)$/)
  })

  /** The wall at `width` px: its track count, a track's width and the shared frame height. */
  function at(width: number) {
    const style = getComputedStyle(grid())
    const min = /^repeat\(auto-fill, minmax\((.*), 1fr\)\)$/.exec(style.gridTemplateColumns)![1]!
    const cols = Math.max(1, Math.floor((width + GAP) / (lengthAt(min, width) + GAP)))
    const track = (width - (cols - 1) * GAP) / cols
    const frameH = lengthAt(style.getPropertyValue('--wall-frame-h').trim(), width)
    return { cols, track, frameH }
  }

  it.each([
    ['the inspector on a 1440 desktop', 317],
    ['the sheet on a 390 phone', 374],
    ['a wider wall', 720],
    ['a full-width wall', 1200],
  ])('%s (%ipx) shows at least two desktop cards a row, each frame whole in its two tracks', (_, width) => {
    const spans = wall([null, null, null, null])
    const { cols, track, frameH } = at(width)
    expect(cols, 'tracks').toBeGreaterThanOrEqual(4)
    const rows = denseRows(spans as number[], cols)
    expect(rows.length, 'four desktop cards in two rows').toBeLessThanOrEqual(2)
    // The shared height leaves a desktop frame whole in two tracks and a portrait tablet's in one.
    expect(frameH * (16 / 10), 'desktop frame width').toBeLessThanOrEqual(2 * track + GAP - CHROME + 0.5)
    expect(frameH * (3 / 4), 'tablet-portrait frame width').toBeLessThanOrEqual(track - CHROME + 0.5)
    expect(frameH).toBeLessThanOrEqual(THUMB_FRAME_H)
    // And is the largest height that does, rather than small frames in wide cards, until the 180px maximum.
    const largest = Math.min((2 * track + GAP - CHROME) / (16 / 10), (track - CHROME) / (3 / 4), THUMB_FRAME_H)
    expect(frameH, 'shared frame height').toBeGreaterThan(largest - 2)
  })

  it.each([317, 374])('a mixed wall at %ipx leaves no row but the last more than one narrow track empty, whatever the order', (width) => {
    const phone = on('iphone-15', 'portrait')
    const tablet = on('ipad-7', 'portrait')
    const desk = on('desktop', 'landscape')
    const turned = on('iphone-15', 'landscape')
    const spans = wall([phone, desk, desk, tablet, turned, desk, phone, unreported, tablet])
    expect(spans).toEqual([1, 2, 2, 1, 2, 2, 1, 2, 1])
    const empty = denseRows(spans as number[], at(width).cols)
    for (const gap of empty.slice(0, -1)) expect(gap).toBeLessThanOrEqual(1)
    // The phone that leads the list does not sit alone: its row holds a desktop beside it.
    expect(empty[0]).toBe(0)
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
