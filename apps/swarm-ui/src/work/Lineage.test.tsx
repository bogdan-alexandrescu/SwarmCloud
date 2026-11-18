import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { chipLineOf, gapLanesOf, gapPx, lineageOf, LineageStrip, restRowsOf } from './Lineage'
import { StageGrid, stageGridOf } from './StageGrid'

const NOW = Date.parse('2026-11-05T12:00:00Z')
const ago = (m: number) => new Date(NOW - m * 60_000).toISOString()

function step(step_id: string, depends_on: string[], task_id: string | null = null, input_from: Record<string, string> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from, task_id }
}

function task(id: string, state: TaskState, extra: Partial<Task> = {}): Task {
  return {
    id,
    state,
    workflow_id: 'wf_lin',
    created_at: ago(60),
    updated_at: ago(1),
    started_at: null,
    completed_at: null,
    park_reason: null,
    result_summary: null,
    blocked_by: null,
    metadata: null,
    attempt_count: 1,
    ...extra,
  } as unknown as Task
}

function wf(steps: WorkflowStep[]): Workflow {
  return { workflow_id: 'wf_lin', state: 'RUNNING', steps } as unknown as Workflow
}

const staged = (taskId: string, filename: string) => ({ task_id: taskId, filename, path: filename, bytes: 10 })

/** implement -> review -> fix -> re-review -> merge, review and fix both reading implement's patch. */
function lane(): { workflow: Workflow; taskById: Map<string, Task> } {
  const workflow = wf([
    step('implement-login', [], 't_impl'),
    step('review-login', ['implement-login'], 't_rev', { 'implement-login': 'patch.diff' }),
    step('fix-login', ['review-login', 'implement-login'], 't_fix', { 'review-login': 'review.md', 'implement-login': 'patch.diff' }),
    step('re-review-login', ['fix-login'], null, { 'fix-login': 'fix.diff' }),
    step('merge-login', ['re-review-login']),
  ])
  const taskById = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED', { started_at: ago(50), completed_at: ago(40) })],
    ['t_rev', task('t_rev', 'SUCCEEDED', { started_at: ago(38), completed_at: ago(20), result_summary: { staged_inputs: [staged('t_impl', 'patch.diff')] } as Task['result_summary'] })],
    ['t_fix', task('t_fix', 'RUNNING', { started_at: ago(13) })],
  ])
  return { workflow, taskById }
}

const PAGES = ['home', 'agents', 'settings'] as const
/** Three captures, three reviews each reading its capture, and a report reading all six. */
function qa(): Workflow {
  const from: Record<string, string> = {}
  for (const p of PAGES) {
    from[`capture-${p}`] = `${p}.png`
    from[`review-${p}`] = `${p}-review.md`
  }
  return wf([
    ...PAGES.map((p) => step(`capture-${p}`, [])),
    ...PAGES.map((p) => step(`review-${p}`, [`capture-${p}`], null, { [`capture-${p}`]: `${p}.png` })),
    step('report', Object.keys(from), null, from),
  ])
}

/** Two builds feed integrate-app; test-web waits on build-web, which failed. */
function mixed(): { workflow: Workflow; taskById: Map<string, Task> } {
  const workflow = wf([
    step('build-api', [], 't_ba'),
    step('build-web', [], 't_bw'),
    step('integrate-app', ['build-api', 'build-web'], null, { 'build-api': 'api.tar', 'build-web': 'web.tar' }),
    step('test-api', ['build-api'], 't_ta', { 'build-api': 'api.tar' }),
    step('test-web', ['build-web'], null, { 'build-web': 'web.tar' }),
  ])
  const taskById = new Map<string, Task>([
    ['t_ba', task('t_ba', 'SUCCEEDED', { started_at: ago(30), completed_at: ago(20) })],
    ['t_bw', task('t_bw', 'FAILED', { started_at: ago(30), completed_at: ago(25) })],
    ['t_ta', task('t_ta', 'RUNNING', { started_at: ago(5) })],
  ])
  return { workflow, taskById }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('the chip line', () => {
  it('says what a step starts after and what takes its work, by stage within its column', () => {
    const { workflow, taskById } = lane()
    const shape = stageGridOf(workflow.steps)
    const lin = lineageOf(workflow, taskById)
    const fix = chipLineOf(shape, lin, 'fix-login')
    expect(`${fix.after} · ${fix.feeds}`).toBe('after review + implement · feeds re-review')
    expect(chipLineOf(shape, lin, 'implement-login').after).toBe('starts first')
    expect(chipLineOf(shape, lin, 'merge-login').feeds).toBe('last step')
    // The tooltip names every step and file in full.
    expect(fix.tip).toBe('starts after review-login (review.md), implement-login (patch.diff) · its work goes to re-review-login (fix.diff)')
  })

  it('counts a fan-in, and keeps arrows and counts for the dense form', () => {
    const w = qa()
    const shape = stageGridOf(w.steps)
    const lin = lineageOf(w, null)
    const report = chipLineOf(shape, lin, 'report')
    expect(report.after).toBe('after 6 steps')
    expect([report.ins, report.outs]).toEqual([6, 0])
    const cap = chipLineOf(shape, lin, 'capture-home')
    expect(cap.feeds).toBe('feeds review + report')
    expect([cap.ins, cap.outs]).toEqual([0, 2])
    render(<StageGrid workflow={w} taskById={null} onSelect={() => undefined} now={NOW} />)
    const short = document.querySelector('[data-step="report"] .lg-line-short')!
    expect(short.textContent).toBe('↑6')
    expect(document.querySelector('[data-step="capture-home"] .lg-line-short')!.textContent).toBe('↓2')
  })

  it('says a step whose parent failed is blocked, and names the parent whole', () => {
    const { workflow, taskById } = mixed()
    const shape = stageGridOf(workflow.steps)
    const lin = lineageOf(workflow, taskById)
    expect(chipLineOf(shape, lin, 'test-web').blockedBy).toEqual({ ids: 'build-web', verb: 'failed' })
    expect(chipLineOf(shape, lin, 'integrate-app').blocked).toBe('build-web failed')
    expect(chipLineOf(shape, lin, 'test-api').blocked).toBeNull()
  })

  it('draws no dependency the workflow does not have: one edge per depends_on entry, its file from input_from', () => {
    const { workflow, taskById } = lane()
    const lin = lineageOf(workflow, taskById)
    const edges = [...lin.parents.values()].flat().map((e) => `${e.from}->${e.to}:${e.look}:${e.file ?? '-'}`)
    expect(edges).toEqual([
      'implement-login->review-login:staged:patch.diff',
      'review-login->fix-login:declared:review.md',
      'implement-login->fix-login:declared:patch.diff',
      'fix-login->re-review-login:declared:fix.diff',
      're-review-login->merge-login:order:-',
    ])
  })
})

describe('the connectors', () => {
  it('runs one lane per parent, one rail for a fan-in, and straight drops need no lane', () => {
    const m = mixed()
    const mShape = stageGridOf(m.workflow.steps)
    const mLanes = gapLanesOf(mShape, lineageOf(m.workflow, m.taskById))
    expect(mLanes.count[0]).toBe(2)
    expect(mLanes.group.get('build-api->integrate-app')).toBe('p:build-api')
    expect(mLanes.group.get('build-api->test-api')).toBe('p:build-api')
    expect(mLanes.group.get('build-web->integrate-app')).toBe('p:build-web')
    const w = qa()
    const qShape = stageGridOf(w.steps)
    const qLanes = gapLanesOf(qShape, lineageOf(w, null))
    // capture -> review drops straight down its column; the reviews share one rail into the report.
    expect(qLanes.count[0]).toBe(0)
    expect(qLanes.count[1]).toBe(1)
    // The report sits under the first column, so review-home drops straight into it; the others share the rail.
    expect(qShape.at.get('report')!.column).toBe(qShape.at.get('review-home')!.column)
    expect(PAGES.filter((p) => p !== 'home').map((p) => qLanes.group.get(`review-${p}->report`))).toEqual(['c:report', 'c:report'])
    expect(gapPx(0)).toBe(12)
    expect(gapPx(2)).toBe(24)
  })

  it('enters a fan-in child once, at its centre, when one of its parents sits in its own column', () => {
    const w = qa()
    const shape = stageGridOf(w.steps)
    const lanes = gapLanesOf(shape, lineageOf(w, null))
    // review-home sits over the report: it joins the report's rail rather than drawing a drop of its own.
    expect(lanes.group.get('review-home->report')).toBe('c:report')
    expect(lanes.count[1]).toBe(1)
    // Each chip 100px wide in a 150px column, 40px tall in a 60px stage.
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const at = shape.at.get(this.dataset.step ?? '')
      const r = at === undefined ? { left: 0, top: 0, width: 600, height: 400 } : { left: at.column * 150, top: at.stage * 60, width: 100, height: 40 }
      return { ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top, toJSON: () => r } as DOMRect
    })
    render(<StageGrid workflow={w} taskById={new Map()} selected={null} onSelect={() => undefined} now={NOW} />)
    const col = shape.at.get('report')!.column
    const centre = col * 150 + 50.5
    const top = shape.at.get('report')!.stage * 60
    // Every vertical piece that touches the report's top edge does so at its centre: one way in.
    const verticals = (d: string) => {
      const out: { x: number; y0: number; y1: number }[] = []
      let x = 0
      let y = 0
      for (const [, op, a, b] of d.matchAll(/([MHV])(-?[\d.]+)(?: (-?[\d.]+))?/g)) {
        if (op === 'M') [x, y] = [Number(a), Number(b)]
        else if (op === 'H') x = Number(a)
        else {
          out.push({ x, y0: y, y1: Number(a) })
          y = Number(a)
        }
      }
      return out
    }
    const into = [...document.querySelectorAll('.lg-wires .lg-wire')]
      .flatMap((p) => verticals(p.getAttribute('d') ?? ''))
      .filter((v) => v.y0 === top || v.y1 === top)
    expect(into.length).toBeGreaterThan(0)
    expect(into.filter((s) => s.x !== centre)).toEqual([])
  })

  it('draws adjacent stages only, in the three looks, and lights the picked step\'s lines', () => {
    const { workflow, taskById } = lane()
    // Every chip laid out down one column, 40px tall with a 20px gap.
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const order = ['implement-login', 'review-login', 'fix-login', 're-review-login', 'merge-login']
      const i = order.indexOf(this.dataset.step ?? '')
      const r = i < 0 ? { left: 0, top: 0, width: 400, height: 400 } : { left: 100, top: i * 60, width: 100, height: 40 }
      return { ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top, toJSON: () => r } as DOMRect
    })
    render(<StageGrid workflow={workflow} taskById={taskById} selected="fix-login" onSelect={() => undefined} now={NOW} />)
    const wires = [...document.querySelectorAll('.lg-wires .lg-wire')]
    // implement -> fix skips review: named in the chip line, never drawn across review's chip.
    expect(wires).toHaveLength(4)
    expect(wires.map((p) => p.getAttribute('d'))).toContain('M150.5 40 V60')
    expect(document.querySelectorAll('.lg-wires .lg-wire.is-staged')).toHaveLength(1)
    expect(document.querySelectorAll('.lg-wires .lg-wire.is-declared')).toHaveLength(2)
    expect(document.querySelectorAll('.lg-wires .lg-wire.is-order')).toHaveLength(1)
    expect(document.querySelectorAll('.lg-wires .lg-wire.is-lit')).toHaveLength(2)
    expect(document.querySelector('.lg-wires')!.getAttribute('class')).toContain('has-pick')
  })
})

describe('the lineage strip', () => {
  const pick = () => undefined

  it('is one line at rest, and nothing at all when no step runs or is held', () => {
    const { workflow, taskById } = lane()
    const shape = stageGridOf(workflow.steps)
    const lin = lineageOf(workflow, taskById)
    expect(restRowsOf(workflow, lin).map((r) => `${r.id} ${r.verb} -> ${r.then.join(',')}`)).toEqual(['fix-login running -> re-review-login'])
    const { container, unmount } = render(<LineageStrip workflow={workflow} shape={shape} lin={lin} selected={null} onPick={pick} now={NOW} />)
    expect(container.querySelectorAll('.lg-strip.is-rest > .lg-rest')).toHaveLength(1)
    expect(container.querySelector('.lg-rest')!.textContent).toContain('re-review-loginwaits for it')
    unmount()
    const done = wf([step('a', [], 't_a')])
    const doneTasks = new Map([['t_a', task('t_a', 'SUCCEEDED')]])
    const r = render(<LineageStrip workflow={done} shape={stageGridOf(done.steps)} lin={lineageOf(done, doneTasks)} selected={null} onPick={pick} now={NOW} />)
    expect(r.container.innerHTML).toBe('')
  })

  it('grows with the rows a pick has: a card per neighbour, one more when the workflow gains one', () => {
    const { workflow, taskById } = lane()
    const draw = (w: Workflow) => {
      cleanup()
      render(<LineageStrip workflow={w} shape={stageGridOf(w.steps)} lin={lineageOf(w, taskById)} selected="fix-login" onPick={pick} now={NOW} />)
      return document.querySelectorAll('.lg-side.is-up .lg-card').length
    }
    expect(draw(workflow)).toBe(2)
    const more = wf([step('plan', []), ...workflow.steps.map((s) => (s.step_id === 'fix-login' ? { ...s, depends_on: [...s.depends_on, 'plan'] } : s))])
    expect(draw(more)).toBe(3)
    // Upstream, the step, downstream: each neighbour with its state and the file on the edge.
    const up = document.querySelector('.lg-side.is-up')!.textContent!
    expect(up).toContain('review-logindone')
    expect(up).toContain('review.md reported at finish')
    expect(document.querySelector('.lg-self .lg-head')!.textContent).toBe('running 13m')
    expect(document.querySelector('.lg-side.is-down')!.textContent).toContain('re-review-loginnot started')
    expect(document.querySelector('.lg-side.is-down .lg-far')!.textContent).toBe('then: merge-login')
  })

  it("has no fixed height: a capped one, past which it scrolls inside itself", async () => {
    const css = (await import('./stage-lineage.css?raw')).default as string
    const rule = css.match(/\n\.lg-strip \{([^}]*)\}/)![1]!
    expect(rule).toMatch(/--lg-strip-cap: 138px;/)
    expect(rule).toMatch(/max-height: var\(--lg-strip-cap\);/)
    expect(rule).toMatch(/overflow-y: auto;/)
    expect(rule).not.toMatch(/(^|[\s;])height:/)
    // The phone's stacked strip lives in a scrolling sheet: no cap there.
    expect(css).toMatch(/\.lg-strip\.is-stacked \{[^}]*max-height: none;/)
  })

  it("never cuts a neighbour's id for its state: the state wraps under it", async () => {
    const css = (await import('./stage-lineage.css?raw')).default as string
    expect(css).toMatch(/\n\.lg-card-head \{[^}]*flex-wrap: wrap;/)
  })

  it("says a failed maker's file will not come, and why the step cannot start, in red", () => {
    const { workflow, taskById } = mixed()
    render(<LineageStrip workflow={workflow} shape={stageGridOf(workflow.steps)} lin={lineageOf(workflow, taskById)} selected="test-web" onPick={pick} now={NOW} />)
    const head = document.querySelector('.lg-self .lg-head')!
    expect(head.textContent).toBe('cannot start: build-web failed')
    expect(head.className).toContain('is-bad')
    expect(document.querySelector('.lg-side.is-up .lg-edge')!.textContent).toBe('web.tar not staged: build-web failed')
    expect(document.querySelector('.lg-side.is-down')!.textContent).toBe('Its work goes tonothing: it is the last step')
  })
})
