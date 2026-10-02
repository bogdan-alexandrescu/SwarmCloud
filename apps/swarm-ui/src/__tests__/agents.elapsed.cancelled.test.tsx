// #163: A CANCELLED RUN'S ELAPSED FIGURE SAYS WHAT IT SPANS.
//
// For a finished task `elapsed()` is last start to end. A task cancelled while
// PARKED, after an earlier start, has a start and an end and nothing that
// says it sat parked in between, so on a CANCELLED row the figure can include
// parked time. The row says so beside the figure; every other state is
// unchanged.
//
// MUTATION: drop `cancelSpan` from TaskRow. The CANCELLED test fails.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { at, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen, CANCEL_SPAN } from '../Agents'

const LABEL = '(last start to cancel, may include parked time)'

function finished(state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({ id: 'task_aaaaaaaa00000000000a', state, started_at: at(1), completed_at: at(13), ...over })
}

/** The Agents list over one task, and that task's elapsed cell. */
async function whenCell(t: Task): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks: [t], tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} />)
  await waitFor(() => expect(container.querySelector('.row.clickable .when')).not.toBeNull())
  return container.querySelector<HTMLElement>('.row.clickable .when')!
}

describe('the Agents list elapsed figure on a cancelled task', () => {
  it('is the literal the owner asked for', () => {
    expect(CANCEL_SPAN).toBe(LABEL)
  })

  it('labels a CANCELLED task with a start as last start to cancel', async () => {
    const cell = await whenCell(finished('CANCELLED'))
    expect(cell.textContent).toBe(`12m 0s ${LABEL}`)
    // A truncated cell still carries it.
    expect(cell.getAttribute('title')).toBe(`12m 0s ${LABEL}`)
  })

  it('leaves a SUCCEEDED task’s elapsed figure unlabelled', async () => {
    const cell = await whenCell(finished('SUCCEEDED'))
    expect(cell.textContent).toBe('12m 0s')
    expect(cell.textContent).not.toContain('last start to cancel')
    expect(cell.getAttribute('title')).toBeNull()
  })

  it('does not label a CANCELLED task that never started: there is no span to qualify', async () => {
    const cell = await whenCell(finished('CANCELLED', { started_at: null }))
    expect(cell.textContent).toBe('never ran')
  })
})
