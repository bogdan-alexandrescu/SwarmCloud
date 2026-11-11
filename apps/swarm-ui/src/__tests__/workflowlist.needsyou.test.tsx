// THE NEEDS-YOU BAR ON THE WORKFLOWS LIST (the owner's pick on PR #3147,
// "Widen view=summary"). The list read serves each step's
// `task_summary.needs` (`codec.step_needs`); `summaryTask` joins it into the
// step's task and `listNeedItems` builds the bar's rows from it, so the list
// mounts the bar with no read of its own.
//
// MUTATIONS: drop `<NeedsYou>` from `WorkflowList`; leave `human_wait`,
// `blocked_by` or `result_summary` out of `summaryTask`; list a question-wait
// park as a hand-off; let an artifact question's Answer open blind.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { summaryTask, summaryTaskById, type StepNeeds } from '../stepviews'
import type { Workflow } from '../types'
import { listNeedItems } from '../work/listNeeds'
import { needsOf } from '../workflowlist'

const { NeedsYou } = await import('../work/NeedsYou')
const { WorkflowsScreen } = await import('../Workflows')

afterEach(cleanup)

const block = (state: string, needs: StepNeeds, extra: Record<string, unknown> = {}) => ({ state, title: null, unit: null, needs, ...extra })

function workflow(steps: { id: string; summary: unknown; waiting?: unknown }[]): Workflow {
  return {
    workflow_id: 'wf_needs',
    tenant_id: 'eng',
    state: 'RUNNING',
    created_at: '2026-11-03T10:00:00Z',
    updated_at: '2026-11-03T10:30:00Z',
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'stop',
    cancel_requested: false,
    steps: steps.map((s) => ({
      step_id: s.id,
      runner_profile: 'claude-code',
      resource_class: 'standard',
      depends_on: [],
      input_from: {},
      task_id: `task_${s.id}`,
      task_summary: s.summary,
      ...(s.waiting === undefined ? {} : { waiting: s.waiting }),
    })),
  } as unknown as Workflow
}

const parkedHuman = { reason: 'HUMAN_REQUIRED', summary: 'waits for a person' }

describe('summaryTask joins the needs block', () => {
  it('carries the hand-off wait, the merge hold, the park clock and the pull request', () => {
    const w = workflow([
      { id: 'browse', summary: block('RUNNING', { human_wait: { reason: 'sign in', expect: null, deadline: '2026-11-03T11:00:00Z' } }) },
      {
        id: 'merge',
        summary: block('PARKED', {
          next_eligible_at: '2026-11-03T10:20:00Z',
          owner_questions: { questions: [{ task_id: 'task_impl', step_id: 'implement', question_index: 0, question: 'Which?' }], unreadable: 0 },
          pull_request: { number: 7, url: 'https://github.com/o/r/pull/7' },
        }),
        waiting: parkedHuman,
      },
    ])
    const browse = summaryTask(w, w.steps[0]!)!
    expect(browse.human_wait?.reason).toBe('sign in')
    expect(browse.human_wait?.deadline).toBe('2026-11-03T11:00:00Z')
    const merge = summaryTask(w, w.steps[1]!)!
    expect(merge.park_reason).toBe('HUMAN_REQUIRED')
    expect(merge.next_eligible_at).toBe('2026-11-03T10:20:00Z')
    expect(merge.blocked_by).toEqual([
      { reason: 'HUMAN_REQUIRED', park_phase: 'owner_questions', owner_questions: expect.objectContaining({ unreadable: 0 }) },
    ])
    expect(merge.result_summary).toEqual({ git: { pull_request: { number: 7, url: 'https://github.com/o/r/pull/7' } } })
  })

  it('reads "not known" for a block from an API older than the needs, never 0', () => {
    const w = workflow([
      { id: 'old', summary: { state: 'PARKED' }, waiting: parkedHuman },
      { id: 'broke', summary: { state: 'FAILED' } },
    ])
    const t = summaryTask(w, w.steps[0]!)!
    expect(t.human_wait).toBeNull()
    expect(t.blocked_by).toBeNull()
    expect(t.result_summary).toBeNull()
    // The page cannot know whether the park is a question wait, a merge's
    // hold or a browser hand-off, nor what the agent asked: those chips say
    // so. Failed is the step's state, which the old block serves.
    render(<NeedsYou items={listNeedItems([w], summaryTaskById([w]), () => 'Old')} approvals={null} />)
    const chip = (word: string) => [...document.querySelectorAll('.ny-chip')].find((c) => c.querySelector('.ny-word')?.textContent === word)!
    for (const word of ['Question', 'Merge waiting', 'Your turn in a browser', 'Approvals']) {
      expect(chip(word).textContent).toContain('not known')
    }
    expect(screen.getByRole('img', { name: 'Failed: 1' })).toBeTruthy()
  })
})

describe('listNeedItems', () => {
  const w = workflow([
    { id: 'build', summary: block('FAILED', {}, { last_error_line: 'tests failed' }) },
    {
      id: 'ask',
      summary: block('PARKED', {
        question_wait: true,
        questions: [{ index: 0, question: 'Which database?', options: [{ label: 'Postgres', description: null }], recommended: 'Postgres' }],
      }),
      waiting: parkedHuman,
    },
    { id: 'review', summary: block('SUCCEEDED', { questions_unread: [1] }) },
    { id: 'merge', summary: block('PARKED', { owner_questions: { questions: [], unreadable: 1 } }), waiting: parkedHuman },
    { id: 'browse', summary: block('PARKED', { next_eligible_at: '2026-11-03T10:20:00Z' }), waiting: parkedHuman },
    { id: 'input', summary: block('PARKED', {}), waiting: { reason: 'AWAITING_INPUT', summary: 'waits for input' } },
    { id: 'done', summary: block('SUCCEEDED', {}) },
  ])
  const items = listNeedItems([w], summaryTaskById([w]), () => 'Ship it')

  it('builds one row per need, titled by the workflow and the step', () => {
    expect(items.map((i) => [i.kind, i.title])).toEqual([
      ['failed', 'Ship it › build'],
      ['question', 'Ship it › ask'],
      ['question', 'Ship it › review'],
      ['merge', 'Ship it › merge'],
      ['handoff', 'Ship it › browse'],
      // An await-input step: its input is given on its page, never a hand-off.
      ['question', 'Ship it › input'],
    ])
  })

  it("serves a wait's question with its words and an artifact's as on its page", () => {
    const [, wait, file] = items
    expect(wait).toMatchObject({ kind: 'question', taskId: 'task_ask', index: 0, question: 'Which database?', recommended: 'Postgres' })
    expect(file).toMatchObject({ kind: 'question', taskId: 'task_review', index: 1, question: null, onItsPage: true })
  })

  it('never lists a question-wait park as a hand-off', () => {
    expect(items.filter((i) => i.kind === 'handoff').map((i) => i.title)).toEqual(['Ship it › browse'])
  })

  it('an artifact question says its words are on its page and does not open blind', () => {
    render(<NeedsYou items={items.filter((i) => i.kind === 'question')} approvals={null} />)
    // The strip starts collapsed: its header opens the rows.
    fireEvent.click(screen.getByRole('button', { name: /^Needs you/, expanded: false }))
    const row = screen.getByText('Its words are on its page').closest('li')!
    const answer = within(row).getByRole('button', { name: /Answer/ })
    expect(answer.getAttribute('aria-disabled') === 'true' || (answer as HTMLButtonElement).disabled).toBe(true)
  })
})

describe("a row's Needs mark and the bar classify a step alike", () => {
  const w = workflow([
    { id: 'ask', summary: block('PARKED', { question_wait: true, questions: [{ index: 0, question: 'Which?', options: [], recommended: null }] }), waiting: parkedHuman },
    { id: 'review', summary: block('SUCCEEDED', { questions_unread: [0, 2] }) },
    { id: 'merge', summary: block('PARKED', { owner_questions: { questions: [], unreadable: 1 } }), waiting: parkedHuman },
    { id: 'browse', summary: block('PARKED', {}), waiting: parkedHuman },
    { id: 'input', summary: block('PARKED', {}), waiting: { reason: 'AWAITING_INPUT', summary: 'waits for input' } },
    { id: 'build', summary: block('FAILED', {}) },
  ])
  Object.assign(w, { state_source: 'derived', rollup: { state: 'PARKED', complete: true } })
  const tasks = summaryTaskById([w])
  const items = listNeedItems([w], tasks, () => 'Ship it')
  const of = (...kinds: string[]) => items.filter((i) => kinds.includes(i.kind)).length

  it("counts a question-wait park and a finished step's artifact questions as questions, as the bar does", () => {
    expect(needsOf(w, tasks)).toEqual({ failed: 1, questions: 4, held: 2 })
  })

  it('agrees with the bar kind by kind', () => {
    expect(needsOf(w, tasks)).toEqual({ failed: of('failed'), questions: of('question'), held: of('merge', 'handoff') })
  })
})

// THE THIRD REVIEW OF PR #3147: a derived-state workflow whose step's task
// summary had no needs gave the bar unknownKinds [question, merge, handoff]
// but the row {failed 0, questions 0, held 0}, and a PARKED HUMAN_REQUIRED
// step without a block counted '1 held' though it may be a question wait.
// MUTATION: count a block-less step's kinds as 0 in `needsOf`, list its park
// as a hand-off, or let the bar skip a step whose task is not in the read.
describe('a step without its needs block reads not known in the row as in the bar', () => {
  const derived = (w: Workflow) => Object.assign(w, { state_source: 'derived', rollup: { state: 'PARKED', complete: true } })
  const w = derived(workflow([{ id: 'old', summary: { state: 'PARKED' }, waiting: parkedHuman }]))
  const tasks = summaryTaskById([w])

  it('gives the row the kinds the bar cannot count as null, never 0', () => {
    const items = listNeedItems([w], tasks, () => 'Old')
    expect(items.unknownKinds).toEqual(['question', 'merge', 'handoff'])
    expect(items.map((i) => i.kind)).toEqual([])
    expect(needsOf(w, tasks)).toEqual({ failed: 0, questions: null, held: null })
  })

  it('still counts a failed step beside a block-less one, in the row and the bar alike', () => {
    const mixed = derived(
      workflow([
        { id: 'old', summary: { state: 'PARKED' }, waiting: parkedHuman },
        { id: 'build', summary: block('FAILED', {}) },
      ]),
    )
    const t = summaryTaskById([mixed])
    expect(listNeedItems([mixed], t, () => 'Mixed').map((i) => i.kind)).toEqual(['failed'])
    expect(needsOf(mixed, t)).toEqual({ failed: 1, questions: null, held: null })
  })

  it('reads every kind not known in the bar when a step has no task in the read, as the row does', () => {
    const lost = derived(workflow([{ id: 'lost', summary: null }, { id: 'build', summary: block('FAILED', {}) }]))
    const t = summaryTaskById([lost])
    expect(listNeedItems([lost], t, () => 'Lost').unknownKinds).toEqual(['failed', 'question', 'merge', 'handoff'])
    expect(needsOf(lost, t)).toBeNull()
  })
})

describe('the Workflows list mounts the bar', () => {
  it('draws Needs you above the filters, with approvals not known', async () => {
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const bar = await waitFor(
      () => {
        const s = document.querySelector<HTMLElement>('.wfg section[aria-label="Needs you"]')
        expect(s).toBeTruthy()
        return s!
      },
      { timeout: 4000 },
    )
    const approvals = [...bar.querySelectorAll('.ny-chip')].find((c) => c.querySelector('.ny-word')?.textContent === 'Approvals')!
    expect(approvals.textContent).toContain('not known')
  })
})
