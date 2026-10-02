// CHILD TASKS IN THE CONSOLE'S VOCABULARY (contract requests 14, 40, 41).
//
// A parent whose agent awaits the helpers it submitted is PARKED on
// CHILDREN_INCOMPLETE and holds no capacity (docs/design/child-tasks.md §3.3).
// The console must render that reason as a sentence, not the bare enum, and
// group it with the parks that end on their own: the scheduler promotes the
// parent when its children end, and past a day cancels the stragglers and
// promotes it anyway. A child cancelled because of its parent is its own
// cancel class in the outcome ledger, never "requested".

import { describe, expect, it } from 'vitest'

import {
  PARK_CLEARS_ITSELF,
  PARK_NEEDS_A_PERSON,
  PARK_REASONS,
  PARK_WAITS_ON_A_STEP,
  reasonCopy,
  type Task,
} from '../types'
import type { CancelCauseKey, CancelSplit } from '../outcomes'

describe('the await park reason', () => {
  it('is a park reason with copy of its own', () => {
    expect(PARK_REASONS).toContain('CHILDREN_INCOMPLETE')
    expect(reasonCopy('CHILDREN_INCOMPLETE')).not.toBe('CHILDREN_INCOMPLETE')
    expect(reasonCopy('CHILDREN_INCOMPLETE')).toMatch(/child tasks/)
  })

  it('ends on its own, and is in exactly one group', () => {
    expect(PARK_CLEARS_ITSELF.has('CHILDREN_INCOMPLETE')).toBe(true)
    expect(PARK_NEEDS_A_PERSON.has('CHILDREN_INCOMPLETE')).toBe(false)
    expect(PARK_WAITS_ON_A_STEP.has('CHILDREN_INCOMPLETE')).toBe(false)
  })
})

describe('the parent fields and the cascade class', () => {
  it('a task can name its parent, and an older API may omit both', () => {
    const child: Pick<Task, 'parent_task_id' | 'parent_attempt_id'> = {
      parent_task_id: 'task_parent',
      parent_attempt_id: 'att_1',
    }
    const plain: Pick<Task, 'parent_task_id' | 'parent_attempt_id'> = {}
    expect(child.parent_task_id).toBe('task_parent')
    expect(plain.parent_task_id ?? null).toBeNull()
  })

  it('counts a child cascade as its own cancel cause', () => {
    const key: CancelCauseKey = 'child_cascade'
    const split: CancelSplit = {
      total: 1, requested: 0, after_failure: 0, after_cancel: 0, workflow_sweep: 0,
      child_cascade: 1, other: 0,
    }
    expect(split[key]).toBe(1)
  })
})
