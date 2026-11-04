// THE FIXTURE BOARD SERVES EACH STEP'S NEEDS BLOCK (the owner's follow-up on
// PR #3147, part of #2688). The fixture build's Workflows list read every
// step's `task_summary` without `needs`, so its Needs-you bar read "not
// known" for questions, merges and hand-offs: a development screen that never
// showed what the list draws. `fixtureStepNeeds` builds the block off each
// fixture task as `codec.step_needs` builds it off the task document; the
// cases below are that function's rules, one per field.
//
// MUTATION: drop `needs: fixtureStepNeeds(t)` from `withTaskSummaries` ->
// the board's bar reads its kinds not known.

import { describe, expect, it } from 'vitest'

import { fixtureStepNeeds, loadWorkflowBoard } from '../api'
import { stepNeedsOf } from '../stepviews'
import { type Task } from '../types'
import { listNeedItems } from '../work/listNeeds'

function task(over: Partial<Task>): Task {
  return {
    id: 't_needs',
    tenant_id: 'tn',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: null,
    priority: 0,
    created_at: '2026-10-19T11:00:00.000Z',
    updated_at: '2026-10-19T11:30:00.000Z',
    started_at: null,
    completed_at: null,
    submitted_by: null,
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_n',
    step_id: 's',
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    ...over,
  } as Task
}

const wait = (attempt_id: string) => ({
  attempt_id,
  blocking: [0, 2],
  questions: [
    { question: 'Which region?', options: [{ label: 'eu', description: 'Europe' }, { label: 'us' }], recommended: 'eu' },
    { question: 'Advisory only' },
    { question: 'Which tier?', options: [] },
  ],
})

describe('the fixture board serves each step a needs block, as codec.step_needs builds it', () => {
  it('gives every step with a task a block, so the bar counts rather than reads "not known"', async () => {
    const r = await loadWorkflowBoard()
    if (r.status !== 'ok') throw new Error(`the fixture board answered ${r.status}`)
    const board = r.data
    let withTask = 0
    for (const w of board.workflows) {
      for (const s of w.steps) {
        if (!s.task_id) continue
        withTask++
        expect(stepNeedsOf(s), `${w.workflow_id} › ${s.step_id} has no needs block`).not.toBeNull()
      }
    }
    expect(withTask).toBeGreaterThan(0)
    const items = listNeedItems(board.workflows, board.taskById, (w) => w.workflow_id)
    expect(items.unknownKinds).toEqual([])
    // The fixture's two failed steps are the bar's only rows.
    expect(items.map((i) => i.kind)).toEqual(['failed', 'failed'])
  }, 60_000)

  it('serves a hand-off only while the task runs', () => {
    const human_wait = { reason: 'Sign in', expect: 'a session', deadline: '2026-10-19T12:00:00.000Z', requested_at: null, controller: null }
    expect(fixtureStepNeeds(task({ state: 'RUNNING', human_wait })).human_wait).toEqual({
      reason: 'Sign in',
      expect: 'a session',
      deadline: '2026-10-19T12:00:00.000Z',
    })
    expect(fixtureStepNeeds(task({ state: 'PARKED', human_wait })).human_wait).toBeNull()
  })

  it("serves a HUMAN_REQUIRED park's owner-questions hold, an unreadable question as null", () => {
    const blocked_by = [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { questions: [{ task_id: 't1', step_id: 'a', question_index: 1, question: 'Ship it?' }, 'junk'], unreadable: 2 },
      },
    ] as unknown as Task['blocked_by']
    const held = fixtureStepNeeds(task({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED', blocked_by }))
    expect(held.owner_questions).toEqual({
      questions: [{ task_id: 't1', step_id: 'a', question_index: 1, question: 'Ship it?' }, null],
      unreadable: 2,
    })
    expect(held.question_wait).toBe(false)
    expect(fixtureStepNeeds(task({ state: 'PARKED', park_reason: 'CI_PENDING', blocked_by })).owner_questions).toBeNull()
  })

  it("serves the parked wait's blocking questions, its own answers only once it was adopted", () => {
    const blocked_by = [{ reason: 'HUMAN_REQUIRED', question_wait: wait('att_2') }] as unknown as Task['blocked_by']
    const answers = { owner_answers: [{ question_index: 0, choice: 'eu' }] }
    const fresh = fixtureStepNeeds(task({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED', blocked_by, metadata: answers }))
    expect(fresh.question_wait).toBe(true)
    // Not adopted: an earlier wait's answer never hides this wait's question 0.
    expect(fresh.questions?.map((q) => q.index)).toEqual([0, 2])
    expect(fresh.questions?.[0]).toEqual({
      index: 0,
      question: 'Which region?',
      options: [{ label: 'eu', description: 'Europe' }, { label: 'us', description: null }],
      recommended: 'eu',
    })
    const adopted = fixtureStepNeeds(
      task({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED', blocked_by, metadata: { ...answers, question_wait: wait('att_2') } }),
    )
    expect(adopted.questions?.map((q) => q.index)).toEqual([2])
    // A wait in another shape is no wait.
    const bad = [{ reason: 'HUMAN_REQUIRED', question_wait: { ...wait('att_2'), blocking: [9] } }] as unknown as Task['blocked_by']
    expect(fixtureStepNeeds(task({ state: 'PARKED', park_reason: 'HUMAN_REQUIRED', blocked_by: bad })).question_wait).toBe(false)
  })

  it("serves an ended task's unanswered artifact questions, never past the worker's cap of 20", () => {
    const metadata = { owner_answers: [{ question_index: 1 }] }
    expect(fixtureStepNeeds(task({ state: 'SUCCEEDED', result_summary: { questions: 3 }, metadata })).questions_unread).toEqual([0, 2])
    expect(fixtureStepNeeds(task({ state: 'SUCCEEDED', result_summary: { questions: 50 } })).questions_unread).toHaveLength(20)
    expect(fixtureStepNeeds(task({ state: 'RUNNING', result_summary: { questions: 3 } })).questions_unread).toEqual([])
  })

  it('serves the pull request off result_summary.git, and only with an integer number', () => {
    const url = 'https://github.com/example-org/repo/pull/7'
    expect(fixtureStepNeeds(task({ result_summary: { git: { pull_request: { number: 7, url } } } })).pull_request).toEqual({ number: 7, url })
    expect(fixtureStepNeeds(task({ result_summary: { git: { pull_request: { number: '7', url } } } })).pull_request).toBeNull()
  })

  it("carries the park reason and the hand-off clock's instants", () => {
    const n = fixtureStepNeeds(task({ state: 'PARKED', park_reason: 'DEPENDENCY_INCOMPLETE', next_eligible_at: '2026-10-19T12:06:00.000Z' }))
    expect(n.park_reason).toBe('DEPENDENCY_INCOMPLETE')
    expect(n.next_eligible_at).toBe('2026-10-19T12:06:00.000Z')
    expect(n.updated_at).toBe('2026-10-19T11:30:00.000Z')
  })
})
