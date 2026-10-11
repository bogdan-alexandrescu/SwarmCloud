// THE WORKFLOW GRAPH PANS TO EVERY PART OF A TALL DAG (owner bug, 2026-10-11,
// on `wf_0c305fd932ef4239816f`). The vertical map beside the canvas (GR1) sat
// on the top of the graph whatever the reader scrolled to, and a click or a
// drag on it moved nothing: it read `window.scrollY` and called
// `window.scrollTo`, and the console's window never scrolls -- the frame's
// `.ctl-scroll` does (styles.css). Measured in Chromium at 1440x900 before the
// fix: the frame scrolled 0 -> 1542px under the wheel while the map's view
// stayed at y=0, and a click on the map's foot left the frame at 0.
//
// WHAT EACH CASE HOLDS, on the owner's exact DAG (8 steps):
//   * the canvas's scroll port is the frame's scroller -- not the canvas's own
//     sideways wrapper, whose `overflow-x: auto` computes `overflow-y` to
//     `auto` as well -- and it scrolls to its bottom, where the map's view
//     covers the last level;
//   * a click on the map at a level's height scrolls the port so that level is
//     on screen; a drag keeps steering until the button is released;
//   * no wheel, touch or key handler on the canvas, its wrapper or the map
//     cancels the default, so the port's own scrolling is never blocked.
//
// jsdom lays nothing out, so the frame's geometry is a model below: the port
// is 600px tall starting 50px down the window, the canvas starts 300px into
// the port's content, and the map's strip is 480px tall at y=100 -- the
// arrangement Chromium measured, in round numbers.
//
// MUTATIONS: point `scrollPortOf` back at the window (return null) -- the
// first two cases turn red; drop the `scrollHeight > clientHeight` test from
// `scrollPortOf` and the wrapper case turns red; add a native, non-passive
// `wheel` listener that calls `preventDefault` to `.wf-canvas-wrap` and the
// last case turns red. (A React `onWheel` cannot be the mutation: React
// attaches wheel and touch listeners passively, so its `preventDefault` is
// ignored by the browser and by jsdom alike.)

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import { WorkflowCard, scrollPortOf, stageKey } from '../Workflows'
import { levelsOf } from '../dag'
import type { Workflow, WorkflowStep } from '../types'

const step = (step_id: string, depends_on: string[]): WorkflowStep => ({
  step_id,
  runner_profile: 'claude-code',
  resource_class: 'standard',
  depends_on,
  input_from: {},
  timeout_seconds: 3600,
  task_id: null,
})

const IMPL = [
  'impl-contract',
  'impl-api-reads-check',
  'impl-web-lib',
  'impl-web-editor',
  'impl-web-jobs-publish-knowledge',
  'impl-web-runwarnings-preview',
]

/** `wf_0c305fd932ef4239816f` as the owner reported it, every step unstarted. */
const ownerWorkflow: Workflow = {
  workflow_id: 'wf_0c305fd932ef4239816f',
  tenant_id: 'eng',
  state: 'QUEUED',
  stored_state: 'QUEUED',
  state_source: 'stored',
  created_at: '2026-10-11T10:00:00.000Z',
  updated_at: '2026-10-11T10:01:00.000Z',
  submitted_by: 'operator@swarm.example.com',
  priority: 0,
  on_step_failure: 'FAIL_WORKFLOW',
  cancel_requested: false,
  steps: [
    step('impl-contract', []),
    step('impl-api-reads-check', ['impl-contract']),
    step('impl-web-lib', ['impl-contract']),
    step('impl-web-editor', ['impl-api-reads-check', 'impl-web-lib']),
    step('impl-web-jobs-publish-knowledge', ['impl-web-lib']),
    step('impl-web-runwarnings-preview', ['impl-web-editor', 'impl-contract']),
    step('review', IMPL),
    step('fix', ['review']),
  ],
}

const PORT_TOP = 50
const PORT_H = 600
const CANVAS_OFFSET = 300
const BELOW = 400
const STRIP_TOP = 100
const STRIP_H = 480

const rect = (top: number, height: number, left = 0, width = 100): DOMRect =>
  ({ top, height, left, width, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}) }) as DOMRect

let scrollTop = 0
const canvasH = () => Number(document.querySelector<HTMLElement>('.wf-canvas')?.style.height.replace('px', '') ?? 0)
const isPort = (el: Element) => el.classList.contains('ctl-scroll')
const maxScroll = () => CANVAS_OFFSET + canvasH() + BELOW - PORT_H

beforeEach(() => {
  scrollTop = 0
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (isPort(this)) return rect(PORT_TOP, PORT_H)
    if (this.classList.contains('wf-canvas')) return rect(PORT_TOP + CANVAS_OFFSET - scrollTop, canvasH())
    if (this.classList.contains('wf-vmap')) return rect(STRIP_TOP, STRIP_H)
    return rect(0, 0)
  })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function (this: HTMLElement) {
    if (isPort(this)) return PORT_H
    if (this.classList.contains('wf-canvas-wrap')) return canvasH()
    return 0
  })
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(function (this: HTMLElement) {
    if (isPort(this)) return CANVAS_OFFSET + canvasH() + BELOW
    if (this.classList.contains('wf-canvas-wrap')) return canvasH()
    return 0
  })
  vi.spyOn(HTMLElement.prototype, 'scrollTop', 'get').mockImplementation(function (this: HTMLElement) {
    return isPort(this) ? scrollTop : 0
  })
  vi.spyOn(HTMLElement.prototype, 'scrollTop', 'set').mockImplementation(function (this: HTMLElement, v: number) {
    if (isPort(this)) scrollTop = Math.max(0, Math.min(v, maxScroll()))
  })
  // The window must not be what moves: jsdom does not implement it, and the
  // console's window never scrolls.
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** The workflow's page frame: a header, then the scroller the card sits in. */
function drawInFrame() {
  const allOpen = Object.fromEntries(levelsOf(ownerWorkflow.steps).map((_, i) => [stageKey(ownerWorkflow.workflow_id, i), true]))
  const view = render(
    <div className="ctl-scroll" style={{ overflowY: 'auto' }}>
      <WorkflowCard
        workflow={ownerWorkflow}
        taskById={new Map()}
        usage={{ kind: 'ready', usage: null }}
        openStages={allOpen}
        onToggleStage={() => undefined}
        reload={() => undefined}
      />
    </div>,
  )
  const port = view.container.querySelector<HTMLElement>('.ctl-scroll')!
  const canvas = view.container.querySelector<HTMLElement>('.wf-canvas')!
  // The level rail's captions sit at each level's top: the last is `fix`'s.
  const tops = [...view.container.querySelectorAll<HTMLElement>('.wf-levels > li')].map((li) => Number(li.style.top.replace('px', '')))
  return { ...view, port, canvas, tops }
}

/** The map's drawn viewport, in canvas pixels. */
function mapView(container: HTMLElement): { y: number; h: number } | null {
  const r = container.querySelector('.wf-vmap .wf-mini-view')
  return r === null ? null : { y: Number(r.getAttribute('y')), h: Number(r.getAttribute('height')) }
}

/** A pointer event with a height, which jsdom's `fireEvent.pointerDown` does not carry. */
function pointer(el: Element, type: 'pointerdown' | 'pointermove' | 'pointerup', clientY: number) {
  act(() => {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientY }))
  })
}

describe('the workflow graph pans to every part of a tall DAG', () => {
  it('is the owner’s DAG: six levels, taller than the port', () => {
    const { canvas, tops } = drawInFrame()
    expect(levelsOf(ownerWorkflow.steps).map((lv) => lv.map((s) => s.step_id).sort())).toEqual([
      ['impl-contract'],
      ['impl-api-reads-check', 'impl-web-lib'],
      ['impl-web-editor', 'impl-web-jobs-publish-knowledge'],
      ['impl-web-runwarnings-preview'],
      ['review'],
      ['fix'],
    ])
    expect(tops).toHaveLength(6)
    expect(canvasH()).toBeGreaterThan(PORT_H)
    expect(canvas.querySelector('[data-step="fix"]')).toBeTruthy()
  })

  it('scrolls the frame, not the canvas’s own wrapper, and the frame reaches the last level', () => {
    const { container, port, canvas, tops } = drawInFrame()
    // The wrapper's `overflow-x: auto` computes `overflow-y` to `auto` too
    // (styles.css); it is exactly as tall as the canvas and must be passed over.
    container.querySelector<HTMLElement>('.wf-canvas-wrap')!.style.overflowY = 'auto'
    expect(scrollPortOf(canvas)).toBe(port)
    expect(port.scrollHeight).toBeGreaterThan(port.clientHeight)

    act(() => {
      port.scrollTop = port.scrollHeight - port.clientHeight
      port.dispatchEvent(new Event('scroll'))
    })
    expect(port.scrollTop).toBe(port.scrollHeight - port.clientHeight)
    const last = tops[tops.length - 1]!
    const v = mapView(container)
    expect(v, 'the map draws no viewport once the frame has scrolled').not.toBeNull()
    // The map follows the frame: its view is on the last level, not stuck on the first.
    expect(v!.y).toBeGreaterThan(0)
    expect(v!.y).toBeLessThanOrEqual(last)
    expect(v!.y + v!.h).toBeGreaterThan(last)
    expect(container.querySelector('.wf-vmap')!.getAttribute('aria-label')).toContain(`starting ${Math.round(v!.y)} pixels from the top`)
  })

  it('moves the frame to the level clicked on the map, and steers while dragged', () => {
    const { container, port, tops } = drawInFrame()
    const map = container.querySelector<HTMLElement>('.wf-vmap')!
    const H = canvasH()
    const last = tops[tops.length - 1]!
    const atLevel = (top: number) => STRIP_TOP + (top / H) * STRIP_H

    pointer(map, 'pointerdown', atLevel(last))
    pointer(map, 'pointerup', atLevel(last))
    expect(port.scrollTop, 'a click on the map did not move the frame').toBeGreaterThan(0)
    expect(window.scrollTo).not.toHaveBeenCalled()
    // The clicked level is inside the port now.
    const levelY = PORT_TOP + CANVAS_OFFSET - port.scrollTop + last
    expect(levelY).toBeGreaterThanOrEqual(PORT_TOP)
    expect(levelY).toBeLessThan(PORT_TOP + PORT_H)
    const v = mapView(container)!
    expect(v.y).toBeLessThanOrEqual(last)
    expect(v.y + v.h).toBeGreaterThan(last)

    // A drag: down at the top, moved to the foot, released, moved again.
    pointer(map, 'pointerdown', STRIP_TOP)
    expect(port.scrollTop).toBe(0)
    pointer(map, 'pointermove', STRIP_TOP + STRIP_H)
    const dragged = port.scrollTop
    expect(dragged, 'a drag on the map did not move the frame').toBeGreaterThan(0)
    pointer(map, 'pointerup', STRIP_TOP + STRIP_H)
    pointer(map, 'pointermove', STRIP_TOP)
    expect(port.scrollTop, 'the map kept steering after the button was released').toBe(dragged)
  })

  it('cancels no wheel, touch or key event on the canvas, its wrapper or the map', () => {
    const { container } = drawInFrame()
    const targets = ['.wf-canvas', '.wf-canvas-wrap', '.wf-graph', '.wf-vmap', '.wf-canvas-row']
    let visited = 0
    for (const sel of targets) {
      const el = container.querySelector<HTMLElement>(sel)
      expect(el, `${sel} is not drawn`).toBeTruthy()
      expect(fireEvent.wheel(el!, { deltaY: 400 }), `${sel} cancels the wheel`).toBe(true)
      expect(fireEvent.touchStart(el!), `${sel} cancels a touch start`).toBe(true)
      expect(fireEvent.touchMove(el!), `${sel} cancels a touch move`).toBe(true)
      for (const key of ['ArrowDown', 'PageDown', 'End', ' ']) {
        expect(fireEvent.keyDown(el!, { key }), `${sel} cancels ${key}`).toBe(true)
      }
      visited++
    }
    expect(visited).toBe(targets.length)
  })
})
