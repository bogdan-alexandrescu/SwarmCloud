// LANE LB-R1: THE BROWSER TAB'S STAGE IS THE WINDOW'S HEIGHT, NOT A SMALL BOX
// (part of #1030). Re-QA on SwarmCloud, 2026-10-12 08:05-08:20 PDT, after
// release f1c242c8: once LB-V1 fitted the device into the stage, the stage
// itself sat at its 320 px floor (~290 px of room under the caption), so at
// 1440×900 with the agent list open the desktop frame was drawn 424×291 and
// an iPhone 15 portrait frame 121×271, with the lower half of the window
// empty.
//
// The cause was one box: TheaterHost's `.th-host` sits between the pane (a
// flex column) and `.live-tab` (flex: 1), and as a plain block it gave the
// tab its content's height, so the stage's `flex: 1 1 0` had nothing to grow
// into. The pane's own layout carries the height down now: every box from
// the pane to the stage is a flex or grid child that takes the room left.
//
// MUTATION: drop the `.ag-split-pane.is-browser > .th-host` rule from
// live.css, and the chain test fails at `.th-host`; give `.live-stage` a
// `max-height`, and the max-height test fails.
import { render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LIVE_CSS from '../styles/live.css?raw'
import type { LiveSession } from '../api'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    USE_FIXTURES: false,
    loadTaskLogs: vi.fn(() => new Promise(() => {})),
    loadTaskEventsPage: vi.fn(() => new Promise(() => {})),
    mintLiveTicket: vi.fn(() => new Promise(() => {})),
    openLiveSocket: vi.fn(() => new Promise(() => {})),
  }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const { LiveBrowserPane, fitDevice } = await import('../LiveBrowser')

const PRESETS: LiveSession['presets'] = [
  { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
  { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
]

function session(): LiveSession {
  return {
    session_id: 'ls_r1',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: { preset: 'desktop', orientation: 'landscape', touch: false },
    presets: PRESETS,
    you: { email: ['ada', 'example.com'].join('@'), can_control: true, can_preempt: true, can_open: true, can_switch_device: true },
  }
}

function agent(): Task {
  return runTask({
    id: 'task_0123456789abcdef0r01',
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    attempt_count: 1,
    completed_at: null,
    result_summary: null,
  })
}

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  const style = document.createElement('style')
  style.dataset.r1 = ''
  style.textContent = LIVE_CSS
  document.head.appendChild(style)
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.head.querySelectorAll('style[data-r1]').forEach((n) => n.remove())
})

/** The Browser tab in the agent split's pane, as AgentSplit.tsx mounts it. */
function inPane() {
  render(
    <div className="ag-split">
      <div className="ag-split-pane is-browser">
        <LiveBrowserPane task={agent()} live={{ status: 'ok', liveBrowser: true, session: session() }} reload={() => {}} />
      </div>
    </div>,
  )
  const pane = document.querySelector<HTMLElement>('.ag-split-pane.is-browser')!
  const stage = document.querySelector<HTMLElement>('.live-stage')!
  expect(stage).not.toBeNull()
  const chain: HTMLElement[] = []
  for (let el: HTMLElement | null = stage; el !== null && el !== pane; el = el.parentElement) chain.push(el)
  return { pane, stage, chain }
}

const describeBox = (el: HTMLElement) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`

describe('LB-R1: the fit, given the room a 1440×900 window leaves', () => {
  // A measured stage of 845×560: the pane beside the agent list, less the
  // header, tabs, origin strip, toolbar and caption.
  const stage = { width: 845, height: 560 }

  it('draws the desktop the stage’s full width, about 845×528', () => {
    const fit = fitDevice(stage, { width: 1440, height: 900 })!
    expect(fit.width).toBe(845)
    expect(fit.height).toBe(528)
  })

  it('draws an iPhone 15 portrait the stage’s full height, about 560 tall', () => {
    // The framebuffer the re-QA measured (393×852 @3).
    const fit = fitDevice(stage, { width: 1179, height: 2644 })!
    expect(fit.height).toBe(560)
    expect(fit.width).toBe(Math.floor((1179 * 560) / 2644))
    // The preset's css size has the same proportions, and the same height.
    expect(fitDevice(stage, { width: 393, height: 852 })!.height).toBe(560)
  })
})

describe('LB-R1: the stage takes the height the pane has left', () => {
  it('passes the pane’s height down every box to the stage: each is a growing flex child, or a grid row of the room left', () => {
    const { pane, chain } = inPane()
    expect(getComputedStyle(pane).display).toBe('flex')
    expect(getComputedStyle(pane).flexDirection).toBe('column')
    expect(chain.some((el) => el.classList.contains('th-host'))).toBe(true)
    for (const el of chain) {
      const parent = getComputedStyle(el.parentElement!)
      const own = getComputedStyle(el)
      const where = `${describeBox(el)} in ${describeBox(el.parentElement!)}`
      if (parent.display === 'grid') {
        // A grid item stretches to its row, and the row is the room left.
        expect(parent.gridTemplateRows, where).toBe('minmax(0, 1fr)')
        continue
      }
      expect(parent.display, where).toBe('flex')
      expect(parent.flexDirection, where).toBe('column')
      expect(Number(own.flexGrow), where).toBeGreaterThanOrEqual(1)
    }
  })

  it('gives the stage a floor of 320 px and no ceiling', () => {
    const { stage, chain } = inPane()
    expect(getComputedStyle(stage).minHeight).toBe('320px')
    for (const el of chain) {
      const max = getComputedStyle(el).maxHeight
      expect(max === '' || max === 'none', `${describeBox(el)} max-height ${max}`).toBe(true)
    }
  })

  it('declares no fixed max-height or height on the boxes between the pane and the device', () => {
    const boxes = /\.(?:th-host|live-tab|live-body|live-screen-wrap|live-stage)\b(?!-)/
    const rules = LIVE_CSS.replace(/\/\*[\s\S]*?\*\//g, '').match(/[^{}]+\{[^{}]*\}/g) ?? []
    const hits = rules.filter((r) => boxes.test(r.slice(0, r.indexOf('{'))))
    expect(hits.length).toBeGreaterThanOrEqual(4)
    for (const r of hits) {
      expect(r, r).not.toMatch(/(?:^|[;{\s])max-height\s*:/)
      expect(r, r).not.toMatch(/(?:^|[;{\s])height\s*:\s*\d/)
    }
  })
})
