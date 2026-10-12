// LANE LB-V1: THE BROWSER TAB'S DEVICE VIEW AND TOOLBAR MATCH THE MOCKUP
// ("Browser Tab and Theater"; part of #1030). Visual QA pass 5 on SwarmCloud,
// 2026-10-12 05:45-06:00 PDT, measured four defects:
//
//   B3  an iPhone 15 frame (393×852 @3, a 1179×2644 framebuffer) was drawn at
//       the stage's full width, 692×1552 css px in a 900 px window, so only
//       the top ~40% of the phone could be seen. The device is now fitted
//       into the stage both ways (contain), centred, in an outline for a
//       mobile preset, with its caption under it -- and noVNC still scales
//       its canvas to the screen element, so a click lands where it is made;
//   B2  the caption read "Desktop · 900×1440" for the desktop in landscape:
//       width × height now follows the orientation;
//   U3  the hint "Only the person in control can change the device." pushed
//       ⤢ Theater to a second toolbar row: it is now the disabled controls'
//       tooltip and accessible description;
//   U4  the Take-control ask had an empty band under its title.
//
// MUTATIONS, one per block: size the frame by the room's width alone; swap
// the preset's sides in landscape instead of ordering them; put the
// `.live-toolbar-why` line back; drop `.live-ask h4 { margin: 0 }`.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LIVE_CSS from '../styles/live.css?raw'
import type { LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  mintLiveTicket: vi.fn(),
  openLiveSocket: vi.fn(),
  takeLiveControl: vi.fn(),
  returnLiveControl: vi.fn(),
  renewLiveControl: vi.fn(),
  openLiveToMembers: vi.fn(),
  setLiveDevice: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

/** noVNC's RFB, as far as LiveBrowser.tsx touches it. */
const rfb = vi.hoisted(() => {
  class FakeRfb {
    static made: FakeRfb[] = []
    viewOnly = true
    scaleViewport = false
    resizeSession = true
    focusOnClick = false
    readonly canvas: HTMLCanvasElement
    constructor(readonly target: HTMLElement) {
      this.canvas = document.createElement('canvas')
      target.appendChild(this.canvas)
      FakeRfb.made.push(this)
    }
    addEventListener() {}
    disconnect() {}
    clipboardPasteFrom() {}
  }
  return { FakeRfb }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: rfb.FakeRfb }))

const { LiveBrowserPane, LiveControl, deviceCaption, deviceSize, fitDevice } = await import('../LiveBrowser')

const ID = 'task_0123456789abcdef0v01'
const ME = ['ada', 'example.com'].join('@')
const OTHER = ['grace', 'example.com'].join('@')
const NOW = Date.now()
const iso = (ms: number) => new Date(ms).toISOString()
const DEVICE_WHY = 'Only the person in control can change the device.'

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

// The gate's list (apps/agent-worker/agent_worker/livegate/presets.py) as
// swarm-api serves it: desktop is written landscape, every mobile preset
// portrait.
const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
  { name: 'pixel-8', width: 412, height: 915, scale: 2.625, mobile: true },
  { name: 'iphone-se', width: 375, height: 667, scale: 2, mobile: true },
  { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
]

type Orientation = 'portrait' | 'landscape'

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    session_id: 'ls_v1',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'iphone-15', orientation: 'portrait', touch: false },
    presets: PRESETS,
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
    ...over,
  }
}

const on = (preset: string, orientation: Orientation) => session({ device: { preset, orientation, touch: false } })
const viewer = (over: Partial<LiveSession> = {}) =>
  session({
    controller: { email: OTHER, since: iso(NOW) },
    you: { email: ME, can_control: false, can_preempt: false, can_open: false, can_switch_device: false },
    ...over,
  })

function pane(s: LiveSession) {
  return render(<LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: s }} reload={() => {}} />)
}

// ---------------------------------------------------------------------------
// The stage's box, as a browser would lay it out. jsdom lays nothing out, so
// the stage's client size and the caption's height are given here, and the
// ResizeObserver is one the test can fire.
// ---------------------------------------------------------------------------

const room = { width: 692, height: 760, caption: 18 }
let observers: Array<() => void> = []

class FakeResizeObserver {
  constructor(readonly cb: () => void) {}
  observe() {
    observers.push(this.cb)
  }
  unobserve() {}
  disconnect() {
    observers = observers.filter((c) => c !== this.cb)
  }
}

function layOut() {
  const isStage = (el: HTMLElement) => el.classList.contains('live-stage')
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
    return isStage(this) ? room.width : 0
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return isStage(this) ? room.height : 0
  })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
    return this.classList.contains('live-stage-caption') ? room.caption : 0
  })
}

const frame = () => document.querySelector<HTMLElement>('.live-frame')!
const px = (v: string) => Number(v.replace(/px$/, ''))

/** noVNC 1.4.0's `Display.autoscale`: the canvas scaled to fit the screen element, aspect kept. */
function novncScale(box: { width: number; height: number }, fb: { width: number; height: number }): number {
  return fb.width / fb.height >= box.width / box.height ? box.width / fb.width : box.height / fb.height
}

beforeEach(() => {
  rfb.FakeRfb.made = []
  observers = []
  Object.assign(room, { width: 692, height: 760, caption: 18 })
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.mintLiveTicket.mockResolvedValue(ok({ session_id: 'ls_v1', ticket: 't'.repeat(8), expires_at: iso(NOW + 30_000) }))
  // The socket opens at once and hands noVNC its channel, as openLiveSocket does.
  api.openLiveSocket.mockImplementation(async (_s: string, _t: string, onOpen: (ws: WebSocket) => void) => {
    onOpen({ readyState: 1, close() {} } as unknown as WebSocket)
  })
  api.takeLiveControl.mockResolvedValue(ok(null))
  api.renewLiveControl.mockResolvedValue(ok(null))
  api.openLiveToMembers.mockResolvedValue(ok(null))
  api.setLiveDevice.mockResolvedValue(ok(null))
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.head.querySelectorAll('style[data-v1]').forEach((n) => n.remove())
})

function injectLiveCss() {
  const style = document.createElement('style')
  style.dataset.v1 = ''
  style.textContent = LIVE_CSS
  document.head.appendChild(style)
}

// ---------------------------------------------------------------------------
// B2: width × height follows the orientation
// ---------------------------------------------------------------------------

describe('B2: the caption’s width × height follows the orientation', () => {
  const cases: Array<[string, Orientation, string]> = [
    ['desktop', 'landscape', 'Desktop · 1440×900 · path not reported'],
    ['desktop', 'portrait', 'Desktop · 900×1440 · path not reported'],
    ['iphone-15', 'portrait', 'iPhone 15 · 393×852 · path not reported'],
    ['iphone-15', 'landscape', 'iPhone 15 · 852×393 · path not reported'],
    ['ipad-7', 'portrait', 'iPad · 810×1080 · path not reported'],
    ['ipad-7', 'landscape', 'iPad · 1080×810 · path not reported'],
  ]
  for (const [preset, orientation, want] of cases) {
    it(`${preset} ${orientation} reads "${want}"`, () => {
      expect(deviceCaption(on(preset, orientation))).toBe(want)
    })
  }

  it('draws the desktop in landscape as 1440×900 under the device', () => {
    layOut()
    pane(on('desktop', 'landscape'))
    expect(document.querySelector('.live-caption')?.textContent).toBe('Desktop · 1440×900 · path not reported')
  })
})

// ---------------------------------------------------------------------------
// B3: the device is fitted into the stage, both ways
// ---------------------------------------------------------------------------

describe('B3: the device fits the stage’s height and width, centred, in its outline', () => {
  it('fits an iPhone 15 to the stage’s HEIGHT, not its width (the measured 692×1552)', () => {
    const fit = fitDevice({ width: 692, height: 760 }, { width: 393, height: 852 })!
    expect(fit.height).toBe(760)
    expect(fit.width).toBe(Math.floor((393 * 760) / 852))
  })

  it('fits a desktop to the stage’s WIDTH when the stage is narrow', () => {
    const fit = fitDevice({ width: 692, height: 760 }, { width: 1440, height: 900 })!
    expect(fit).toEqual({ width: 692, height: Math.floor((900 * 692) / 1440) })
  })

  it('never draws a device larger than the room, for every preset both ways', () => {
    const rooms = [
      { width: 692, height: 760 },
      { width: 320, height: 900 },
      { width: 1400, height: 400 },
      { width: 1001, height: 999 },
    ]
    for (const p of PRESETS) {
      for (const orientation of ['portrait', 'landscape'] as const) {
        const size = deviceSize(on(p.name, orientation))!
        for (const r of rooms) {
          const fit = fitDevice(r, size)!
          expect(fit.width, `${p.name} ${orientation} in ${r.width}×${r.height}`).toBeLessThanOrEqual(r.width)
          expect(fit.height).toBeLessThanOrEqual(r.height)
          // contain: one side meets the room (to the pixel).
          expect(r.width - fit.width < 1 || r.height - fit.height < 1).toBe(true)
        }
      }
    }
  })

  it('has nothing to fit into before the stage is laid out', () => {
    expect(fitDevice({ width: 0, height: 0 }, { width: 393, height: 852 })).toBeNull()
  })

  it('sizes the iPhone frame to the stage’s height less the caption and the outline, in a phone outline', async () => {
    layOut()
    pane(session())
    const f = frame()
    expect(f.classList.contains('is-phone')).toBe(true)
    // 760 − 18 caption − 6 gap − 2 × 10 outline = 716 for the screen, whose
    // 1 px hairline leaves 714 for noVNC's canvas.
    expect(px(f.style.height)).toBe(716)
    expect(px(f.style.width)).toBe(Math.floor((393 * 714) / 852) + 2)
    expect(px(f.style.width)).toBeLessThan(room.width)
  })

  it('draws the caption under the device, inside the stage', () => {
    layOut()
    pane(session())
    const stage = document.querySelector('.live-stage')!
    const cap = stage.querySelector('.live-caption')!
    expect(cap).not.toBeNull()
    expect(frame().compareDocumentPosition(cap) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('draws a tablet in a tablet outline and a desktop in none', () => {
    layOut()
    const { unmount } = pane(on('ipad-7', 'portrait'))
    expect(frame().classList.contains('is-tablet')).toBe(true)
    unmount()
    pane(on('desktop', 'landscape'))
    expect(frame().classList.contains('is-desktop')).toBe(true)
    expect(frame().classList.contains('is-phone') || frame().classList.contains('is-tablet')).toBe(false)
    // No outline to leave room for: the whole width.
    expect(px(frame().style.width)).toBe(692)
  })

  it('fits again when the stage changes size', () => {
    layOut()
    pane(session())
    room.height = 500
    act(() => observers.forEach((cb) => cb()))
    expect(px(frame().style.height)).toBe(500 - 18 - 6 - 20)
  })

  it('fits again when the device is rotated', () => {
    layOut()
    const { rerender } = pane(session())
    rerender(
      <LiveBrowserPane
        task={agent()}
        live={{ status: 'ok', liveBrowser: true, session: on('iphone-15', 'landscape') }}
        reload={() => {}}
      />,
    )
    // Landscape in 692 wide: width-bound, 692 − 20 outline; 670 inside the hairline.
    expect(px(frame().style.width)).toBe(672)
    expect(px(frame().style.height)).toBe(Math.floor((393 * 670) / 852) + 2)
  })

  it('outlines the frame in CSS, centred in the stage, outside the screen’s size', () => {
    injectLiveCss()
    layOut()
    pane(session())
    const st = getComputedStyle(document.querySelector('.live-stage')!)
    expect(st.alignItems).toBe('center')
    expect(st.justifyContent).toBe('center')
    expect(st.overflow).toBe('hidden')
    const f = getComputedStyle(frame())
    expect(f.boxSizing).toBe('content-box')
    expect(f.borderTopWidth).toBe('10px')
  })
})

// ---------------------------------------------------------------------------
// B3: a click still lands where it is made
// ---------------------------------------------------------------------------

describe('B3: input lands where the person clicks after the fit', () => {
  it('keeps noVNC scaling its canvas to the screen element (scaleViewport), with no transform of its own', async () => {
    layOut()
    pane(session())
    await vi.waitFor(() => expect(rfb.FakeRfb.made).toHaveLength(1))
    const r = rfb.FakeRfb.made[0]!
    expect(r.scaleViewport).toBe(true)
    expect(r.resizeSession).toBe(false)
    const host = r.target
    // noVNC is attached to the screen, the screen fills the fitted frame, and
    // nothing between them moves pixels away from where noVNC maps a click.
    expect(host.classList.contains('live-screen')).toBe(true)
    expect(host.parentElement).toBe(frame())
    for (const el of [host, frame(), r.canvas]) {
      expect(el.getAttribute('style') ?? '').not.toMatch(/transform|scale|zoom/)
    }
    // The screen carries no size of its own: the theater, which borrows it, sizes it to its stage.
    expect(host.style.width).toBe('')
    expect(host.style.height).toBe('')
  })

  it('makes the frame the framebuffer’s shape, so noVNC’s canvas fills it and a click maps to the pixel under it', () => {
    // noVNC maps a click by the canvas's own box and its scale: framebuffer x
    // = (clientX − canvas.left) / scale. That is right wherever the canvas
    // sits; what the fit must add is that the canvas IS the device the
    // person sees -- no letterbox band where a click would clip to an edge.
    const rooms = [
      { width: 692, height: 760 },
      { width: 1200, height: 640 },
      { width: 360, height: 700 },
    ]
    for (const p of PRESETS) {
      for (const orientation of ['portrait', 'landscape'] as const) {
        const size = deviceSize(on(p.name, orientation))!
        const fb = { width: Math.round(size.width * size.scale), height: Math.round(size.height * size.scale) }
        for (const r of rooms) {
          // The fit is the box inside the screen's hairline, where noVNC draws.
          const box = fitDevice(r, size)!
          const k = novncScale(box, fb)
          const canvas = { width: fb.width * k, height: fb.height * k }
          const tag = `${p.name} ${orientation} in ${r.width}×${r.height}`
          // Within whole-pixel rounding, never a band: the fit cuts its free
          // side to a whole pixel, which a 393×852 phone repays as at most
          // 852 / 393 ≈ 2.2 px along the other.
          const slack = Math.max(size.width, size.height) / Math.min(size.width, size.height) + 0.01
          expect(box.width - canvas.width, tag).toBeLessThan(slack)
          expect(box.height - canvas.height, tag).toBeLessThan(slack)
          // A click at the canvas's centre and its far corner reach the framebuffer's.
          const at = (x: number, y: number) => ({ x: x / k, y: y / k })
          const centre = at(canvas.width / 2, canvas.height / 2)
          expect(Math.abs(centre.x - fb.width / 2), tag).toBeLessThan(1)
          expect(Math.abs(centre.y - fb.height / 2), tag).toBeLessThan(1)
          const corner = at(canvas.width, canvas.height)
          expect(Math.abs(fb.width - corner.x), tag).toBeLessThan(1)
          expect(Math.abs(fb.height - corner.y), tag).toBeLessThan(1)
        }
      }
    }
  })
})

// ---------------------------------------------------------------------------
// U3: the toolbar keeps one row; the why is a tooltip
// ---------------------------------------------------------------------------

describe('U3: the device bar’s why is its tooltip, not a toolbar line', () => {
  const toolbar = () => screen.getByRole('toolbar', { name: 'Browser view' })

  it('draws no visible hint line in the toolbar for a viewer', () => {
    layOut()
    pane(viewer())
    const bar = toolbar()
    expect(bar.querySelector('.live-toolbar-why')).toBeNull()
    const visible = [...bar.querySelectorAll('span, p, div')].filter(
      (el) => el.textContent === DEVICE_WHY && !el.classList.contains('sk-vh'),
    )
    expect(visible).toEqual([])
  })

  it('puts the why on every disabled device control, as its title and its accessible description', () => {
    layOut()
    pane(viewer())
    const bar = toolbar()
    for (const name of ['Desktop', 'iPhone 15', 'iPad', '⟲ rotate', 'touch: off']) {
      const b = within(bar).getByRole('button', { name }) as HTMLButtonElement
      expect(b.disabled, name).toBe(true)
      expect(b.title, name).toBe(DEVICE_WHY)
      const id = b.getAttribute('aria-describedby')
      expect(id, name).toBeTruthy()
      const said = document.getElementById(id!)
      expect(said?.textContent, name).toBe(DEVICE_WHY)
      expect(said?.classList.contains('sk-vh'), name).toBe(true)
    }
  })

  it('describes nothing for the person in control, whose controls work', () => {
    layOut()
    pane(session({ controller: { email: ME, since: iso(NOW) } }))
    const b = within(toolbar()).getByRole('button', { name: 'Desktop' })
    expect(b.getAttribute('aria-describedby')).toBeNull()
    expect(document.body.textContent).not.toContain(DEVICE_WHY)
  })

  it('keeps ⤢ Theater and "Let any member take control" together at the right, unbroken', () => {
    injectLiveCss()
    layOut()
    pane(session())
    const bar = toolbar()
    const end = bar.querySelector<HTMLElement>('.live-toolbar-end')!
    expect(end.contains(within(bar).getByRole('button', { name: 'Open theater (T)' }))).toBe(true)
    expect(end.contains(within(bar).getByRole('checkbox', { name: 'Let any member take control' }))).toBe(true)
    expect(getComputedStyle(end).marginLeft).toBe('auto')
    expect(getComputedStyle(end).flexWrap).toBe('nowrap')
    expect(getComputedStyle(bar.querySelector('.live-open')!).whiteSpace).toBe('nowrap')
  })
})

// ---------------------------------------------------------------------------
// U4: no empty band under the Take-control ask's title
// ---------------------------------------------------------------------------

describe('U4: the Take-control ask has no gap under its title', () => {
  it('sets the title’s and the text’s margins to nothing, so only the ask’s gap spaces them', () => {
    injectLiveCss()
    render(<LiveControl taskId={ID} session={session()} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Take control' }))
    const ask = screen.getByRole('dialog', { name: 'Take control of the agent’s browser' })
    const title = within(ask).getByRole('heading', { name: 'Take control of the agent’s browser?' })
    expect(getComputedStyle(title).marginBottom).toBe('0px')
    expect(getComputedStyle(title).marginTop).toBe('0px')
    expect(getComputedStyle(ask.querySelector('p')!).marginTop).toBe('0px')
  })
})
