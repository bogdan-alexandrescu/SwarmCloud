import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { densityAt, StageGrid, stageGridOf, walkStage } from './StageGrid'

// ---------------------------------------------------------------------------
// The fixture: 40 steps, 4 stages x 10 features, `re-review` holding a `-`.
// ---------------------------------------------------------------------------

const WF = 'wf_stage_grid'
const NOW = Date.parse('2026-11-03T10:00:00Z')
const STAGES = ['build', 'review', 'fix', 're-review'] as const
const FEATURES = ['login', 'search', 'calendar', 'invoices', 'contacts', 'reports', 'settings', 'booking', 'export', 'onboarding'] as const

const ago = (m: number) => new Date(NOW - m * 60_000).toISOString()
const taskId = (stage: string, f: string) => `task_${stage.replace('-', '')}_${f}`

function task(id: string, stepId: string | null, state: TaskState, extra: Partial<Task> = {}): Task {
  return {
    id,
    state,
    workflow_id: WF,
    step_id: stepId,
    created_at: ago(60),
    updated_at: ago(1),
    started_at: null,
    completed_at: null,
    park_reason: null,
    result_summary: null,
    blocked_by: null,
    metadata: null,
    ...extra,
  } as unknown as Task
}

/**
 * build:     login..settings done (PR 2400+f), booking failed, export and onboarding running 12m
 * review:    login..calendar done, invoices..reports running, settings parked on provider quota,
 *            booking/export/onboarding not started
 * fix:       login running; search's task not in the read; the rest not started
 * re-review: nothing started
 * A merge parked HUMAN_REQUIRED on the owner's questions of build-search and build-calendar.
 */
function fixture(): { workflow: Workflow; taskById: Map<string, Task> } {
  const steps: WorkflowStep[] = []
  const tasks: Task[] = []
  STAGES.forEach((stage, si) => {
    FEATURES.forEach((f, fi) => {
      const stepId = `${stage}-${f}`
      const id = taskId(stage, f)
      let state: TaskState | null = null
      let extra: Partial<Task> = {}
      if (stage === 'build') {
        state = fi < 7 ? 'SUCCEEDED' : fi === 7 ? 'FAILED' : 'RUNNING'
        extra = fi < 8
          ? { started_at: ago(50), completed_at: ago(30), result_summary: { git: { pull_request: { number: 2400 + fi, url: 'u', state: 'open', created: true } } } }
          : { started_at: ago(12) }
      } else if (stage === 'review') {
        if (fi < 3) state = 'SUCCEEDED'
        else if (fi < 6) state = 'RUNNING'
        else if (fi === 6) state = 'PARKED'
        extra = fi < 3 ? { started_at: ago(25), completed_at: ago(20) } : fi < 6 ? { started_at: ago(5) } : { park_reason: 'PROVIDER_QUOTA_EXHAUSTED' }
      } else if (stage === 'fix' && fi === 0) {
        state = 'RUNNING'
        extra = { started_at: ago(3) }
      }
      const unread = stage === 'fix' && fi === 1
      steps.push({
        step_id: stepId,
        runner_profile: 'claude-code',
        resource_class: 'standard',
        depends_on: si === 0 ? [] : [`${STAGES[si - 1]}-${f}`],
        input_from: {},
        task_id: state !== null || unread ? id : null,
      })
      if (state !== null) tasks.push(task(id, stepId, state, extra))
    })
  })
  tasks.push(task('task_merge', null, 'PARKED', {
    park_reason: 'HUMAN_REQUIRED',
    blocked_by: [{
      reason: 'HUMAN_REQUIRED',
      park_phase: 'owner_questions',
      owner_questions: {
        unreadable: 0,
        questions: [1, 2].map((fi) => ({ task_id: taskId('build', FEATURES[fi]!), step_id: `build-${FEATURES[fi]}`, question_index: 0, question: 'Which layout?' })),
      },
    }] as unknown as Task['blocked_by'],
  }))
  const workflow = { workflow_id: WF, state: 'RUNNING', steps } as unknown as Workflow
  return { workflow, taskById: new Map(tasks.map((t) => [t.id, t])) }
}

const chip = (stepId: string) => document.querySelector<HTMLButtonElement>(`[data-step="${stepId}"]`)!
const row = (label: string) => document.querySelector<HTMLElement>(`.sg-row[data-stage="${label}"]`)!

/** A ResizeObserver that reports the given width for whatever it observes. */
function stubWidth(width: number) {
  vi.stubGlobal('ResizeObserver', class {
    constructor(private cb: ResizeObserverCallback) {}
    observe(el: Element) {
      this.cb([{ target: el, contentRect: { width } as DOMRectReadOnly } as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
    unobserve() {}
    disconnect() {}
  })
}

afterEach(cleanup)

describe('stageGridOf: where each step goes', () => {
  it('lays 40 steps out as 4 stages by 10 features, re-review named whole', () => {
    const g = stageGridOf(fixture().workflow.steps)
    expect(g.stages.map((s) => s.label)).toEqual(['build', 'review', 'fix', 're-review'])
    expect(g.columns.map((c) => c.label)).toEqual([...FEATURES])
    for (const [si, stage] of STAGES.entries()) {
      for (const [fi, f] of FEATURES.entries()) expect(g.at.get(`${stage}-${f}`)).toEqual({ stage: si, column: fi })
    }
  })

  it('takes the stage from the dependencies and the column from the step it depends on when the name says otherwise', () => {
    const s = (id: string, deps: string[]): WorkflowStep => ({ step_id: id, runner_profile: 'p', resource_class: 'r', depends_on: deps, input_from: {} })
    // review-a depends on build-b: the data puts it under b, whatever its name says.
    // late-c is named like the first stage but depends on review-b, so it is the third stage.
    const g = stageGridOf([s('build-a', []), s('build-b', []), s('review-a', ['build-b']), s('review-b', ['build-a']), s('late-c', ['review-b'])])
    expect(g.columns.map((c) => c.label)).toEqual(['a', 'b'])
    expect(g.at.get('review-a')).toEqual({ stage: 1, column: 1 })
    expect(g.at.get('review-b')).toEqual({ stage: 1, column: 0 })
    expect(g.at.get('late-c')).toEqual({ stage: 2, column: 0 })
    expect(g.stages[2]!.label).toBe('late')
  })

  it('opens a column per fan-out child rather than piling them under their parent', () => {
    const s = (id: string, deps: string[]): WorkflowStep => ({ step_id: id, runner_profile: 'p', resource_class: 'r', depends_on: deps, input_from: {} })
    const g = stageGridOf([s('plan', []), s('build-x', ['plan']), s('build-y', ['plan'])])
    expect(g.columns.map((c) => c.label)).toEqual(['plan', 'x', 'y'])
    expect(g.stages[1]!.cells).toEqual([null, 'build-x', 'build-y'])
  })
})

describe('StageGrid', () => {
  it('draws a chip per step in its row and column, each with its mark, time and pull request', () => {
    const { workflow, taskById } = fixture()
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
    expect(document.querySelectorAll('.sg-chip')).toHaveLength(40)
    const rows = [...document.querySelectorAll('.sg-row:not(.is-head)')]
    expect(rows.map((r) => r.getAttribute('data-stage'))).toEqual([...STAGES])
    // Row 2 (review), column 7 (settings): the parked step.
    const reviewChips = [...rows[1]!.querySelectorAll('.sg-chip')]
    expect(reviewChips[6]!.getAttribute('data-step')).toBe('review-settings')
    expect(chip('review-settings').className).toContain('is-parked')
    expect(chip('build-login').querySelector('.sg-chip-time')!.textContent).toBe('20m')
    expect(chip('build-login').querySelector('.sg-chip-pr')!.textContent).toBe('#2400')
    expect(chip('build-export').querySelector('.sg-chip-time')!.textContent).toBe('12m')
    expect(chip('build-booking').className).toContain('is-failed')
    // The park reason and the full name are in the tooltip.
    expect(chip('review-settings').title).toContain('review-settings')
    expect(chip('review-settings').title).toContain('provider quota')
    // The column labels are the features.
    expect([...document.querySelectorAll('.sg-col')].map((c) => c.textContent)).toEqual([...FEATURES])
  })

  it('tallies each stage: done, failed, running, waiting, and what it could not read as not known', () => {
    const { workflow, taskById } = fixture()
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
    expect(row('build').getAttribute('aria-label')).toBe('build: 7 done, 1 failed, 2 running, 0 waiting')
    expect(row('review').getAttribute('aria-label')).toBe('review: 3 done, 0 failed, 3 running, 4 waiting')
    expect(row('fix').getAttribute('aria-label')).toBe('fix: 0 done, 0 failed, 1 running, 8 waiting, 1 not known')
    expect(row('re-review').getAttribute('aria-label')).toBe('re-review: 0 done, 0 failed, 0 running, 10 waiting')
    expect([...row('build').querySelectorAll('.sg-n')].map((n) => n.textContent)).toEqual(['succeeded7', 'failed1', 'running2'])
    // The unread step is drawn "not known", never as waiting.
    expect(chip('fix-search').className).toContain('is-unknown')
    expect(chip('fix-search').textContent).toContain('not known')
  })

  it('marks the steps whose agent asks the owner with a question mark', () => {
    const { workflow, taskById } = fixture()
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
    const asking = [...document.querySelectorAll('.sg-chip')].filter((c) => c.querySelector('.sg-ask')).map((c) => c.getAttribute('data-step'))
    expect(asking).toEqual(['build-search', 'build-calendar'])
    expect(chip('build-search').title).toContain('asks you a question')
  })

  it("counts an agent's own unanswered questions too", () => {
    const { workflow, taskById } = fixture()
    const t = taskById.get(taskId('review', 'login'))!
    taskById.set(t.id, { ...t, result_summary: { questions: 2 }, owner_answers: [] } as unknown as Task)
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
    expect(chip('review-login').querySelector('.sg-ask')).not.toBeNull()
    expect(chip('review-login').title).toContain('asks you 2 questions')
  })

  it('dims every chip outside the set kept bright', () => {
    const { workflow, taskById } = fixture()
    const bright = new Set(['build-booking', 'build-search'])
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} bright={bright} now={NOW} />)
    const dimmed = document.querySelectorAll('.sg-chip.is-dimmed')
    expect(dimmed).toHaveLength(38)
    expect(chip('build-booking').className).not.toContain('is-dimmed')
    expect(chip('build-search').className).not.toContain('is-dimmed')
    cleanup()
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
    expect(document.querySelectorAll('.sg-chip.is-dimmed')).toHaveLength(0)
  })

  it('reports a clicked chip and a clicked stage count to the page', () => {
    const { workflow, taskById } = fixture()
    const onSelect = vi.fn()
    const onOpenStage = vi.fn()
    render(<StageGrid workflow={workflow} taskById={taskById} onSelect={onSelect} onOpenStage={onOpenStage} now={NOW} />)
    fireEvent.click(chip('review-reports'))
    expect(onSelect).toHaveBeenCalledWith('review-reports')
    fireEvent.click(screen.getByRole('button', { name: /^fix: .*Open the Table on this stage$/ }))
    expect(onOpenStage).toHaveBeenCalledTimes(1)
    expect(onOpenStage.mock.calls[0]![0]).toMatchObject({ index: 2, label: 'fix' })
    expect(onOpenStage.mock.calls[0]![0].stepIds).toHaveLength(10)
  })

  it('walks stages with up and down, and a stage with left and right', () => {
    const { workflow, taskById } = fixture()
    const onSelect = vi.fn()
    render(<StageGrid workflow={workflow} taskById={taskById} selected="build-invoices" onSelect={onSelect} now={NOW} />)
    expect(chip('build-invoices').tabIndex).toBe(0)
    expect(chip('build-login').tabIndex).toBe(-1)
    fireEvent.keyDown(chip('build-invoices'), { key: 'ArrowDown' })
    expect(onSelect).toHaveBeenLastCalledWith('review-invoices')
    expect(document.activeElement).toBe(chip('review-invoices'))
    fireEvent.keyDown(chip('review-invoices'), { key: 'ArrowRight' })
    expect(onSelect).toHaveBeenLastCalledWith('review-contacts')
    fireEvent.keyDown(chip('review-contacts'), { key: 'ArrowUp' })
    expect(onSelect).toHaveBeenLastCalledWith('build-contacts')
    onSelect.mockClear()
    fireEvent.keyDown(chip('build-contacts'), { key: 'ArrowUp' })
    expect(onSelect).not.toHaveBeenCalled()
    expect(walkStage(stageGridOf(workflow.steps), 're-review-login', 1)).toBeNull()
  })

  it('drops the pull request, then the times, then shows marks only with vertical labels as it narrows', () => {
    const { workflow, taskById } = fixture()
    const at = (width: number) => {
      cleanup()
      stubWidth(width)
      render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
      return document.querySelector('.sg')!
    }
    let g = at(1000)
    expect(g.getAttribute('data-density')).toBe('full')
    expect(chip('build-login').querySelector('.sg-chip-pr')).not.toBeNull()

    g = at(640)
    expect(g.getAttribute('data-density')).toBe('no-pr')
    expect(chip('build-login').querySelector('.sg-chip-pr')).toBeNull()
    expect(chip('build-login').querySelector('.sg-chip-time')!.textContent).toBe('20m')

    g = at(480)
    expect(g.getAttribute('data-density')).toBe('no-time')
    expect(chip('build-login').querySelector('.sg-chip-time')).toBeNull()

    g = at(360)
    expect(g.getAttribute('data-density')).toBe('marks')
    expect(g.className).toContain('is-marks')
    expect(chip('build-login').querySelector('.sg-chip-mark')).not.toBeNull()
    // What a chip drops stays in its tooltip, and the question mark stays on the chip.
    expect(chip('build-login').title).toContain('pull request #2400')
    expect(chip('build-login').title).toContain('ran 20m')
    expect(chip('build-search').querySelector('.sg-ask')).not.toBeNull()
  })

  it('keeps the times of a four-feature grid at a width where ten features lose them', () => {
    expect(densityAt(480, 10)).toBe('no-time')
    expect(densityAt(480, 4)).toBe('full')
    expect(densityAt(null, 10)).toBe('full')
  })

  it('turns the column labels vertical only when marks are all that is left', async () => {
    const { workflow, taskById } = fixture()
    const STYLES = (await import('./stagegrid.css?raw')).default
    const style = document.createElement('style')
    style.textContent = STYLES
    document.head.appendChild(style)
    try {
      stubWidth(360)
      render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
      await act(async () => undefined)
      expect(getComputedStyle(document.querySelector('.sg-col')!).writingMode).toBe('vertical-rl')
      cleanup()
      stubWidth(1000)
      render(<StageGrid workflow={workflow} taskById={taskById} onSelect={() => undefined} now={NOW} />)
      expect(getComputedStyle(document.querySelector('.sg-col')!).writingMode).not.toBe('vertical-rl')
    } finally {
      style.remove()
    }
  })
})
