// ONE QUESTION, COUNTED ONCE, WHATEVER ITS OPTIONS (the follow-up picked under
// the owner's overnight grant, 6 November 2026). The worker keys each artifact
// question by its words alone (`result_summary.question_text_digests`), and
// `codec.step_needs` serves those keys beside `questions_unread`, and each open
// wait question's `text_digest`: an implement and a fix step asking the same
// words with other option labels count once on the Workflows list and in Needs
// you, as the bell counts them (tests/unit/control_plane/test_question_text_key.py).
// A step served without the keys counts as before.
//
// MUTATIONS: ignore the served keys; match an artifact key to a wait's words;
// drop a step whose key is new.

import { describe, expect, it } from 'vitest'

import { summaryTaskById } from '../stepviews'
import type { Workflow } from '../types'
import { listNeedItems } from '../work/listNeeds'

const KEY_A = 'a1b2c3d4e5f60718'
const KEY_B = '0f1e2d3c4b5a6978'

function workflow(steps: { id: string; state: string; needs: Record<string, unknown> }[]): Workflow {
  return {
    workflow_id: 'wf_key',
    tenant_id: 'eng',
    state: 'RUNNING',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: '2026-11-06T10:00:00Z',
    updated_at: '2026-11-06T10:30:00Z',
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
      task_summary: { state: s.state, title: null, unit: null, needs: s.needs },
    })),
  } as unknown as Workflow
}

const questionsOf = (w: Workflow) =>
  listNeedItems([w], summaryTaskById([w]), () => 'Ship it').filter((i) => i.kind === 'question')

describe('the same words asked by two steps with other options', () => {
  it('count once when both ended steps serve the key', () => {
    const w = workflow([
      { id: 'implement', state: 'SUCCEEDED', needs: { questions_unread: [0, 1], question_text_digests: [KEY_A, KEY_B] } },
      { id: 'fix', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: [KEY_A] } },
    ])
    const items = questionsOf(w)
    expect(items.map((i) => [i.kind === 'question' && i.taskId, i.kind === 'question' && i.index])).toEqual([
      ['task_implement', 0],
      ['task_implement', 1],
    ])
  })

  it("count once when one step waits on them and a later one's artifact asks them again", () => {
    const w = workflow([
      {
        id: 'implement',
        state: 'PARKED',
        needs: {
          park_reason: 'HUMAN_REQUIRED',
          question_wait: true,
          questions: [{ index: 0, question: 'Ship it?', options: [{ label: 'yes', description: null }], recommended: null, text_digest: KEY_A }],
          questions_unread: [],
          question_text_digests: null,
        },
      },
      { id: 'fix', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: [KEY_A] } },
    ])
    expect(questionsOf(w).map((i) => i.kind === 'question' && i.taskId)).toEqual(['task_implement'])
  })

  it('count as before when a step was summarised without the key', () => {
    const w = workflow([
      { id: 'implement', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: [KEY_A] } },
      { id: 'fix', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: null } },
      { id: 'review', state: 'SUCCEEDED', needs: { questions_unread: [0] } },
    ])
    expect(questionsOf(w)).toHaveLength(3)
  })

  it('keep a step whose words are new', () => {
    const w = workflow([
      { id: 'implement', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: [KEY_A] } },
      { id: 'fix', state: 'SUCCEEDED', needs: { questions_unread: [0], question_text_digests: [KEY_B] } },
    ])
    expect(questionsOf(w)).toHaveLength(2)
  })
})
