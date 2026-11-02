// LANE SUMMARY-TITLE (part of #2391): BGD's decision of 2026-10-26 on VQ-009,
// "Have the list's summary view carry metadata.title". The summary row's
// `metadata` is the title alone (`codec.SUMMARY_METADATA_KEYS`), so a lone
// agent's row reads its title -- `Fix flaky lease heartbeat test (#248)` --
// instead of `claude-code agent · 01234567`, with no read per row. An agent
// with no title keeps that name, and a workflow step's row is unchanged.
//
// MUTATION: draw `agentName(task)` for a lone agent again (`ownTitle` unread).

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadTasksInStates: vi.fn(),
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadResourceClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentsScreen, ownTitle } = await import('../Agents')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

/** A task id of the real shape, built at runtime. */
function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 26, 11, 0, n)).toISOString(),
    started_at: new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString(),
    attempt_count: 1,
    max_attempts: 3,
    ...over,
  })
}

afterEach(() => {
  vi.clearAllMocks()
})

const TITLE = 'Fix flaky lease heartbeat test (#248)'

describe('SUMMARY-TITLE: a lone agent’s row is named by its title', () => {
  it('reads the title from the summary row, and the profile and short id without one', async () => {
    // As `view=summary` serves them: `metadata` is the title, or `{}`.
    const titled = row(1, 'RUNNING', { metadata: { title: `  ${TITLE} ` } })
    const untitled = row(2, 'RUNNING', { metadata: {} })
    const blank = row(3, 'RUNNING', { metadata: { title: '   ' } })
    const older = row(4, 'RUNNING', { metadata: undefined as unknown as null })
    const step = row(5, 'RUNNING', { workflow_id: 'wf_st', step_id: 'implement', metadata: { title: 'Wave 7' } })
    const all = [titled, untitled, blank, older, step]
    api.loadTasks.mockResolvedValue(ok({ tasks: all, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
    // The workflow's one full read, which names its step as before.
    api.loadTask.mockImplementation(async (id: string) => ok({ ...all.find((x) => x.id === id)!, metadata: { title: 'Wave 7' } }))
    render(<AgentsScreen onOpen={() => {}} />)
    const name = (t: Task) => document.querySelector(`.row.is-compact[data-task-id="${t.id}"] .cr-name b`)

    await waitFor(() => expect(flat(name(step))).toBe('Wave 7 · implement'))
    expect(flat(name(titled))).toBe(TITLE)
    // The whole title is the name's hover, with the id beside it.
    expect(name(titled)!.getAttribute('title')).toBe(`${TITLE} · ${titled.id}`)
    // The opener, which a keyboard and a screen reader reach, says it too.
    const opener = document.querySelector(`.row.is-compact[data-task-id="${titled.id}"] .cr-open`)!
    expect(opener.getAttribute('aria-label')!.startsWith(`${TITLE} · ${titled.id}`)).toBe(true)
    // The control: no title, a blank one, or an API that sends no metadata.
    for (const t of [untitled, blank, older]) {
      // The kind alone: the whole id is the line under it (QA1-FIX-07, overview-agents-14).
      expect(flat(name(t))).toBe('claude-code agent')
    }
    // No lone agent was read for its title: the summary carried it. Nor was
    // the step, whose summary row carries its workflow's title
    // (QA1-FIX-07, overview-agents-60).
    expect(api.loadTask.mock.calls.map((c) => c[0])).toEqual([])
  })

  it('names only a lone agent by its own title, never a step', () => {
    expect(ownTitle(row(1, 'RUNNING', { metadata: { title: TITLE } }))).toBe(TITLE)
    expect(ownTitle(row(1, 'RUNNING', { metadata: { title: 7 } }))).toBeNull()
    expect(ownTitle(row(1, 'RUNNING', { metadata: { unit: 'lease' } }))).toBeNull()
    expect(ownTitle(row(1, 'RUNNING', { workflow_id: 'wf', step_id: 'build', metadata: { title: TITLE } }))).toBeNull()
  })
})
