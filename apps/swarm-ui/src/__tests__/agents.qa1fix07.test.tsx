// LANE QA1-FIX-07 (part of #2596, the console visual QA pass 1 epic): the
// Agents list's findings from the sweep lane on the fixture console. Each
// `it` names its finding and the mutation that turns it red.

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
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
  loadLiveSession: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentsScreen, NO_BLOCKER, matchesQuery } = await import('../Agents')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function tid(n: number): string {
  return `task_${n.toString(16).padStart(20, '0')}`
}

function row(n: number, state: TaskState, over: Partial<Task> = {}): Task {
  const ended = state === 'SUCCEEDED' || state === 'FAILED' || state === 'CANCELLED'
  const started = state === 'RUNNING' || state === 'STARTING' || ended
  return runTask({
    id: tid(n),
    state,
    runner_profile: 'claude-code',
    created_at: new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 9, 26, 11, 0, n)).toISOString(),
    started_at: started ? new Date(Date.UTC(2026, 9, 26, 10, 0, n)).toISOString() : null,
    completed_at: ended ? new Date(Date.UTC(2026, 9, 26, 10, 30, n)).toISOString() : null,
    attempt_count: started ? 1 : 0,
    max_attempts: 3,
    metadata: {},
    ...over,
  })
}

const WF = 'wf_audit_01'
const WF_TITLE = 'Audit the operator scripts and the Terraform'
const step = (n: number, state: TaskState, stepId: string) =>
  row(n, state, { workflow_id: WF, step_id: stepId, metadata: { title: WF_TITLE } })

const flaky = row(1, 'RUNNING', { metadata: { title: 'Fix flaky lease heartbeat test' } })
const untitled = row(2, 'RUNNING')
const leased = row(3, 'LEASED')
const queued = row(4, 'QUEUED')
const ready = row(5, 'READY')
const plan = step(6, 'SUCCEEDED', 'plan')
const scan = step(7, 'RUNNING', 'scan-terraform')
const report = step(8, 'PARKED', 'report')
const lone = row(9, 'SUCCEEDED')
const full = row(11, 'READY', { blocked_by: [{ reason: 'POOL_FULL', pool: 'anthropic', active: 4, limit: 4 }] as unknown as Task['blocked_by'] })
const ALL = [flaky, untitled, leased, queued, ready, plan, scan, report, lone, full]

function serve(tasks: Task[] = ALL) {
  api.loadTasks.mockResolvedValue(ok({ tasks, next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadTasksInStates.mockResolvedValue(ok({ tasks: [], next_page_token: null, tenant_id: 'eng' } satisfies TaskPage))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [{ generation: 1 }] }))
  api.loadResourceClasses.mockReturnValue(new Promise(() => {}))
  // The workflow's full read never lands: the row must not need it.
  api.loadTask.mockReturnValue(new Promise(() => {}))
  api.loadLiveSession.mockReturnValue(new Promise(() => {}))
}

const rowOf = (t: Task) => document.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${t.id}"]`)
const tab = (name: RegExp) => fireEvent.click(screen.getByRole('tab', { name }))

afterEach(() => {
  vi.clearAllMocks()
})

describe('QA1-FIX-07: the Agents list', () => {
  it('overview-agents-15: the search finds a row by its title and its drawn name', async () => {
    // MUTATION: drop `metadata.title` from `matchesQuery`'s fields -> `flaky` finds nothing.
    expect(matchesQuery(flaky, 'flaky')).toBe(true)
    expect(matchesQuery(scan, 'terraform')).toBe(true)
    expect(matchesQuery(untitled, 'claude-code agent')).toBe(true)
    expect(matchesQuery(untitled, 'flaky')).toBe(false)
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find by name or task id' }), { target: { value: 'Flaky' } })
    await waitFor(() => expect(rowOf(untitled)).toBeNull())
    expect(rowOf(flaky)).not.toBeNull()
  })

  it('overview-agents-11, -19, -14: every live row says its state, carries an age, and its id once', async () => {
    // MUTATION: lead line two with the state word only on leased/dispatched/starting rows -> `running` is missing.
    // MUTATION: leave a wordOnly row's `.when` blank -> the leased row has no age.
    // MUTATION: draw `agentKind · shortTaskId` as the untitled name again -> the id is printed twice.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    expect(flat(rowOf(flaky)!.querySelector('.cr-sub .cr-state'))).toBe('running')
    expect(flat(rowOf(leased)!.querySelector('.cr-sub .cr-state'))).toBe('leased')
    const age = rowOf(leased)!.querySelector('.when')!
    expect(flat(age)).not.toBe('')
    expect(age.getAttribute('title')).toMatch(/since it was submitted at 2026-10-26 10:00Z/)
    expect(flat(rowOf(untitled)!.querySelector('.cr-name b'))).toBe('claude-code agent')
    expect(flat(rowOf(untitled)!.querySelector('.cr-name')).split('00000000000000000002').length).toBe(2)
  })

  it('overview-agents-56: a waiting row says its state in words', async () => {
    // MUTATION: drop the `waitingRow` state word -> `queued` and `ready` are not drawn.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    tab(/^Waiting/)
    await waitFor(() => expect(rowOf(queued)).not.toBeNull())
    expect(flat(rowOf(queued)!.querySelector('.cr-sub .cr-state'))).toBe('queued')
    expect(flat(rowOf(ready)!.querySelector('.cr-sub .cr-state'))).toBe('ready')
    expect(flat(rowOf(report)!.querySelector('.cr-sub .cr-state'))).toBe('parked')
    // MUTATION: drop the `NO_BLOCKER` line -> a row with no recorded blocker draws no reason at all.
    expect(flat(rowOf(queued)!.querySelector('.why'))).toBe(NO_BLOCKER)
    expect(flat(rowOf(ready)!.querySelector('.why'))).toBe(NO_BLOCKER)
    // A row whose blocker is recorded names it.
    expect(flat(rowOf(full)!.querySelector('.why'))).not.toBe(NO_BLOCKER)
    expect(flat(rowOf(full)!.querySelector('.why'))).not.toBe('')
  })

  it('overview-agents-56 (re-review): the no-blocker line reads under a count, said once for the group', async () => {
    // The re-review's render of Waiting, grouped, read `2 No pool named: the
    // scheduler recorded no reason this one waits.` -- a count of two before
    // `this one`. The line is the group's as well as one row's.
    // MUTATION: put `this one waits` back in `NO_BLOCKER` -> the group line says `2 … this one`.
    // Since pass 2 (overview-agents-56) a queued row is under `Not yet
    // eligible`, apart from the ready one, so two queued rows share the line.
    serve([...ALL, row(12, 'QUEUED')])
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    tab(/^Waiting/)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Group by workflow' }))
    await waitFor(() => expect(document.querySelector('.group-why')).not.toBeNull())
    const said = [...document.querySelectorAll('.group-why')].map(flat)
    expect(said).toContain(`2 ${NO_BLOCKER}`)
    expect(said.join(' ')).not.toMatch(/this one|No pool/)
    expect(NO_BLOCKER).toMatch(/^no pool named/)
  })

  it('overview-agents-13: `N loaded` is said once, in the header, not again beside the search', async () => {
    // MUTATION: draw `${n} loaded` in `.ag-find .ag-scope` again.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    expect(flat(document.querySelector('.ag-find'))).not.toMatch(/loaded/)
  })

  it('overview-agents-60: a step row is named by its workflow title on its first draw, without the full read', async () => {
    // MUTATION: name a step from `useRowTitles` alone -> it reads `scan-terraform` until the read lands.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(scan)).not.toBeNull())
    expect(flat(rowOf(scan)!.querySelector('.cr-name b'))).toBe(`${WF_TITLE} · scan-terraform`)
  })

  it('overview-agents-53, -54: a workflow group is headed by its title and its own state; rows are the step; No workflow has no state', async () => {
    // MUTATION: head the group with `<Id>{wf}</Id>` -> the heading is the raw id.
    // MUTATION: roll up only this tab's rows -> Recent says `succeeded` for a running workflow.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    tab(/^Recent/)
    fireEvent.click(screen.getByRole('checkbox', { name: 'Group by workflow' }))
    await waitFor(() => expect(document.querySelectorAll('section.group').length).toBe(2))
    const [wf, none] = [...document.querySelectorAll('section.group')]
    const head = wf!.querySelector('h2')!
    expect(flat(head.querySelector('a'))).toBe(WF_TITLE)
    expect(head.querySelector('a')!.getAttribute('title')).toBe(WF)
    expect(flat(head)).toMatch(/running/)
    expect(flat(head)).not.toMatch(/succeeded/)
    expect(flat(rowOf(plan)!.querySelector('.cr-name b'))).toBe('plan')
    expect(flat(none!.querySelector('h2'))).toMatch(/^No workflow ?1 here$/)
  })

  it('overview-agents-16: a row drawn again keeps the attempt figure it already read', async () => {
    // MUTATION: start `useRuns` at null on every mount -> the second draw reads `attempt … of 3`.
    serve()
    const first = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(flat(rowOf(flaky)!.querySelector('.cr-try'))).toBe('attempt 1 of 3'))
    first.unmount()
    api.loadAttempts.mockReturnValue(new Promise(() => {}))
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    expect(flat(rowOf(flaky)!.querySelector('.cr-try'))).toBe('attempt 1 of 3')
  })

  it('overview-agents-55: a row opened by a click is not scrolled into view', async () => {
    // MUTATION: drop `scrolledTo.current = taskId` from `openRow` -> the click scrolls the page column.
    serve()
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    function Host() {
      const [open, setOpen] = useState<string | null>(null)
      return <AgentsScreen onOpen={setOpen} taskId={open} />
    }
    render(<Host />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    act(() => rowOf(flaky)!.querySelector<HTMLElement>('.cr-open')!.click())
    await waitFor(() => expect(rowOf(flaky)!.getAttribute('aria-current')).toBe('true'))
    expect(scroll).not.toHaveBeenCalled()
  })

  it('overview-agents-55: focus handed from a clicked row to its opener scrolls nothing', async () => {
    // The scroll the review measured (0 -> 37px on `.ctl-scroll`) followed
    // focus(), not scrollIntoView: jsdom has no layout, so the mechanism is
    // what is pinned -- every focus the click causes asks for no scroll.
    // MUTATION: call `.cr-open`'s `focus()` without `{ preventScroll: true }` in the row's onFocus.
    serve()
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(flaky)).not.toBeNull())
    const opener = rowOf(flaky)!.querySelector<HTMLElement>(':scope > .cr-open')!
    const focus = vi.spyOn(opener, 'focus')
    act(() => rowOf(flaky)!.focus())
    expect(focus).toHaveBeenCalledTimes(1)
    expect(focus.mock.calls[0]![0]).toEqual({ preventScroll: true })
  })

  it('overview-agents-18: the theater link says what it does, in sentence case', async () => {
    // MUTATION: label the link `⤢ theater all` again.
    const browser = row(10, 'RUNNING', { runner_profile: 'claude-code-browser' })
    serve([...ALL, browser])
    render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(rowOf(browser)).not.toBeNull())
    fireEvent.click(screen.getByRole('button', { name: /^Browsers/ }))
    await waitFor(() => expect(document.querySelector('.ag-theater-all')).not.toBeNull())
    expect(flat(document.querySelector('.ag-theater-all'))).toBe('⤢ Watch all browsers')
  })
})
