// QAFIX3-AGENT (part of #1680): the Details tab's findings from visual QA run 3
// (wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of main at
// 51d99c53, 1440 and 390 px, light and dark).
//
//   task-012  The CPU tile's caption was cut to `cgroup limit (clas…`: in a
//             480-639px pane every strip sub-line is clamped to two lines,
//             and the CPU line carries three facts since FF-D5a and the
//             sized words -- the reading, whose limit it is, and the size.
//   task-015  The Progress card drew a failed attempt's run in red with no
//             legend entry saying what red is.
//   task-016  The Progress card's phase track (`--surface-2` on the card's
//             `--surface`) and its queue segment (`--s-neu` at 45%) fell
//             below 3:1 against what they sit on.
//   task-018  At 390 a `›` began a wrapped line of the Now card's phases:
//             each arrow led the chip after it, and the two wrapped as one.
//
// jsdom has no layout engine, so the layout and the colour are read off the
// shipped sheets through `painted()` (marks.ts), as access.page.test.tsx
// does, and the structure that produces them off the rendered DOM.

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { AttemptRow, Task } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted, resolveColour, THEMES, tokensIn } from './marks'
import { contrast, over } from './spaceprobe'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const MIN = 60_000

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

function runOf(t: Partial<Task>, attempts: AttemptRow[], events: AgentRun['events'] = []): AgentRun {
  return {
    task: task({ attempt_count: attempts.length, ...t }),
    events,
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    // cpu 4 in the catalogue, 2 written by the kernel: the caption names both.
    classes: { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

/** What run 3 measured on the fixture: the kernel's 2 under a 4-cpu class, sized 4 / 8. */
function cgroupAttempt(): AttemptRow {
  return Object.assign(attempt(1, { backend: 'GKE_AUTOPILOT' }), {
    cpu_seconds: 402.3,
    peak_cpu_cores: 1.5,
    mean_cpu_cores: 0.84,
    cpu_limit_cores: 2,
    cpu_limit_source: 'cgroup',
    size_version: 0,
    sized_cpu_request: 4,
    sized_cpu_limit: 8,
    sized_memory_gib: 8,
    sized_disk_gib: 4,
    sized_live_split: null,
  })
}

function cpuCell(root: HTMLElement): HTMLElement {
  const cell = [...root.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].find((c) =>
    (c.querySelector('.dt-sc-l')?.textContent ?? '').startsWith('CPU'),
  )
  expect(cell, 'no CPU tile').toBeTruthy()
  return cell!
}

/** The 1440 split's pane: 480-639px wide, on a window tall enough for the strip to stick. */
const SPLIT_1440: CascadeEnv = { width: 1440, height: 900, container: 600 }

describe('task-012: the CPU tile’s caption is never cut', () => {
  it('draws the whole caption in the 480-639px pane where the other sub-lines clamp', () => {
    const root = host(runOf({ state: 'SUCCEEDED' }, [cgroupAttempt()]))
    const sub = cpuCell(root).querySelector<HTMLElement>('.dt-sc-s')!
    expect(sub.textContent).toContain('cgroup limit (class 4 vCPU)')
    expect(sub.textContent).toContain('4 cores guaranteed, bursts to 8')
    for (const theme of THEMES) {
      const env = { ...SPLIT_1440, theme }
      // MUTATION: drop the CPU tile's exemption from the two-line clamp -- the
      // caption is cut to `cgroup limit (clas…` again, the size behind it gone.
      expect(painted(sub, ['-webkit-line-clamp', 'line-clamp'], env) ?? 'none').toBe('none')
      // MUTATION: keep `overflow: hidden` on the CPU sub-line -- a third line is clipped without an ellipsis.
      expect(painted(sub, 'overflow', env) ?? 'visible').toBe('visible')
    }
  })

  it('still clamps every other tile’s sub-line, which its title holds whole', () => {
    const root = host(runOf({ state: 'SUCCEEDED' }, [cgroupAttempt()]))
    const others = [...root.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].filter((c) => c !== cpuCell(root))
    const sub = others.map((c) => c.querySelector<HTMLElement>('.dt-sc-s')).find((s) => s !== null)
    expect(sub, 'no other tile draws a sub-line').toBeTruthy()
    // MUTATION: lift the clamp from every tile -- the stuck strip grows back to ~220px (V058).
    expect(painted(sub!, ['-webkit-line-clamp', 'line-clamp'], SPLIT_1440)).toBe('2')
  })
})

/** A failed first attempt and a successful second: one red run, one green. */
function failedThenOk(): AgentRun {
  return runOf({ state: 'SUCCEEDED' }, [
    attempt(1, { exit_code: 1, error: 'exit status 1' }),
    attempt(2, { created_at: at(11), started_at: at(12), completed_at: at(20) }),
  ])
}

describe('task-015: the Progress card’s legend names the red run', () => {
  it('has a legend entry for a failed run whenever one is drawn', () => {
    const root = host(failedThenOk())
    const bars = root.querySelectorAll('.dt-progress .dt-phb > .is-b')
    expect(bars.length, 'the fixture drew no failed run').toBeGreaterThan(0)
    const legend = root.querySelector<HTMLElement>('.dt-progress .dt-lg')!
    const key = legend.querySelector<HTMLElement>('i.is-b')
    // MUTATION: drop the `is-b` entry from `.dt-lg` -- the red bar is unexplained again (task-015).
    expect(key, 'the legend has no swatch for the red run').not.toBeNull()
    // MUTATION: leave the swatch unlabelled -- a red square with no words explains nothing.
    expect(key!.parentElement!.textContent?.trim()).toBe('failed')
    for (const theme of THEMES) {
      const env: CascadeEnv = { width: 1440, container: 700, theme }
      const swatch = painted(key!, ['background', 'background-color'], env)
      const bar = painted(bars[0]!, ['background', 'background-color'], env)
      expect(swatch, 'the swatch is not painted').not.toBeNull()
      // MUTATION: paint the swatch anything but the bar's red -- the key names a colour no bar wears.
      expect(resolveColour(swatch!, theme)).toEqual(resolveColour(bar!, theme))
    }
  })

  it('names no failed run when none is drawn', () => {
    const root = host(runOf({ state: 'SUCCEEDED' }, [attempt(1)]))
    // MUTATION: draw the `failed` entry unconditionally -- the legend names a mark the reader cannot find.
    expect(root.querySelector('.dt-progress .dt-lg i.is-b')).toBeNull()
  })
})

describe('task-016: the phase track and the queue segment hold 3:1', () => {
  for (const theme of THEMES) {
    const env: CascadeEnv = { width: 1440, container: 700, theme }
    it(`${theme}: the track's edge against the card, the queue against the track`, () => {
      const root = host(runOf({ state: 'SUCCEEDED' }, [attempt(1)]))
      const card = root.querySelector<HTMLElement>('.dt-progress')!
      const track = root.querySelector<HTMLElement>('.dt-progress .dt-phb')!
      const queue = track.querySelector<HTMLElement>('.is-q')!
      const cardBg = resolveColour(painted(card, ['background', 'background-color'], env)!, theme)
      const trackBg = resolveColour(painted(track, ['background', 'background-color'], env)!, theme)

      const edge = painted(track, ['border', 'border-color'], env)
      expect(edge, 'the track has no edge of its own').not.toBeNull()
      const edgeTokens = tokensIn(edge!, theme)
      expect(edgeTokens, `${edge} names no single colour token`).toHaveLength(1)
      // MUTATION: drop the track's border -- `--surface-2` on `--surface` is 1.1:1, the track invisible (task-016).
      expect(contrast(over(edgeTokens[0]!.rgba, cardBg), cardBg)).toBeGreaterThanOrEqual(3)

      const seen = (seg: HTMLElement) => {
        const fill = resolveColour(painted(seg, ['background', 'background-color'], env)!, theme)
        const opacity = Number(painted(seg, 'opacity', env) ?? '1')
        return contrast(over({ ...fill, a: fill.a * opacity }, trackBg), trackBg)
      }
      // MUTATION: put `opacity: .45` back on the queue segment -- about 2:1 on the track in light.
      expect(seen(queue)).toBeGreaterThanOrEqual(3)
      // MUTATION: put `opacity: .55` back on the cold start -- 2.3:1 on the track in light, the same defect.
      expect(seen(track.querySelector<HTMLElement>('.is-c')!)).toBeGreaterThanOrEqual(3)
    })
  }
})

describe('task-018: a wrapped line of the Now card’s phases never starts with `›`', () => {
  it('puts each arrow after the chip it follows, inside that chip’s step', () => {
    const t0 = Date.now()
    const iso = (m: number) => new Date(t0 - m * MIN).toISOString()
    const root = host(
      runOf(
        { state: 'RUNNING', created_at: iso(15), started_at: iso(14), completed_at: null, current_lease_id: 'lse_1' },
        [attempt(1, { created_at: iso(15), started_at: iso(14), completed_at: null, exit_code: null })],
        [ev('lease_acquired', iso(15), 'att_1'), ev('running', iso(14), 'att_1'), ev('heartbeat', iso(1), 'att_1')],
      ),
    )
    const steps = [...root.querySelectorAll<HTMLElement>('.dt-phases > .dt-phase-step')]
    expect(steps.length, 'the fixture drew fewer than two phases').toBeGreaterThanOrEqual(2)
    steps.forEach((s, i) => {
      const kids = [...s.children]
      // MUTATION: put the arrow back in front of its chip -- `› run 14m` opens the wrapped line (task-018).
      expect(kids[0]!.classList.contains('dt-phase-arr'), `step ${i} starts with an arrow`).toBe(false)
      // MUTATION: drop the trailing arrow -- the phases read as unordered chips.
      expect(s.querySelector('.dt-phase-arr') !== null, `step ${i}'s arrow`).toBe(i < steps.length - 1)
    })
  })
})
