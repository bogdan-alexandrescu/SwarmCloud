/**
 * LANE TASKPANES-FOLLOW (part of #1680, console visual QA run 6). The three
 * items QAFIX6-TASKPANES (PR 2081) left as questions, because each needed a
 * shared test file or a file outside its territory. CLD took the lane's
 * recommended answers on 2026-10-23 under BGD's overnight authority
 * (task_85db365560234379a4d8); they are reversible UI choices.
 *
 *   1  box 6343793416       (6:flow-find-failure-phone-05) the overflow names
 *                           what it hides and keeps its count:
 *                           `2 more: Artifacts, Checkpoints ▾`, named by that
 *                           text.
 *   2  box 6343764730       (6:flow-find-failure-desktop-04, the error half)
 *                           the error jump never draws a dash: `Error 1 of 1`
 *                           with one, `N error lines` with several and none
 *                           jumped to, `Error k of N` after a jump.
 *   3  6:task-002           the Children tab counts the read the pane draws:
 *                           on `/children` the count is read whether or not
 *                           the agent offers children.
 */
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskPage, TaskTranscript, TranscriptStep } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadChildren: vi.fn(),
  loadTask: vi.fn(),
  loadAgentRun: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgTabsEdge, paneTabs } = await import('../agent-split/tabs')
const { AgentLogs } = await import('../AgentLogs')
const { useChildCount } = await import('../AgentChildren')
const { AgentSplit } = await import('../AgentSplit')

const ID = 'task_0123456789abcdef0f01'
const PARENT = 'task_0123456789abcdef0f00'
const MIN = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', completed_at: null, ...over })
}
const failed = () => row({ state: 'FAILED', completed_at: ago(30), attempt_count: 1, current_generation: 1 })

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

// ------------------------------------------------------------ 1: the overflow

// THE BAR AS A 390 PHONE LAYS IT OUT (as qafix4.agent.head.test.tsx gives it):
// jsdom lays nothing out, so each tab's width is given.
const WIDTH: Readonly<Record<string, number>> = {
  Details: 62,
  Browser: 70,
  Logs: 46,
  Changes: 130,
  Children: 90,
  Attempts: 84,
  Artifacts: 130,
  Checkpoints: 118,
}
const proto = HTMLElement.prototype
const had = {
  client: Object.getOwnPropertyDescriptor(proto, 'clientWidth'),
  offset: Object.getOwnPropertyDescriptor(proto, 'offsetWidth'),
}

function sizeTabs(room: number) {
  Object.defineProperty(proto, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('ag-tabs-edge') ? room : 0
    },
  })
  Object.defineProperty(proto, 'offsetWidth', {
    configurable: true,
    get(this: HTMLElement) {
      if (this.classList.contains('ag-tabs-more-host')) return 110
      if (this.getAttribute('role') !== 'tab' || this.closest('.ag-tabs-edge') === null) return 0
      return WIDTH[this.querySelector('.c-tab-label')?.textContent ?? ''] ?? 0
    },
  })
}

function unsizeTabs() {
  for (const [k, d] of [['clientWidth', had.client], ['offsetWidth', had.offset]] as const) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
}

function bar(t: Task) {
  const tabs = paneTabs({ task: t, base: '/agents/live/x', hasBrowser: false, sessionOpen: false, childCount: null, runs: 1, ckpt: null, ckptCount: 2 })
  render(
    <div className="drawer ctl-drawer ag-split">
      <AgTabsEdge
        current="detail"
        onSelect={() => {}}
        tabs={tabs.map((x) => ({
          key: x.id,
          label: x.label,
          ...((x.id === 'detail' && x.count === null) || x.id === 'logs' ? {} : { count: x.count, why: x.say ?? undefined }),
        }))}
      />
    </div>,
  )
  act(() => {
    fireEvent(window, new Event('resize'))
  })
  const strip = screen.getByRole('tablist', { name: 'Agent panes' })
  const onBar = () => within(strip).getAllByRole('tab').map((b) => b.querySelector('.c-tab-label')?.textContent ?? '')
  return { strip, onBar }
}

describe('1 (box 6343793416): the overflow names the tabs it hides, and keeps the count', () => {
  afterEach(unsizeTabs)

  it('reads `N more: <Tab>, <Tab> ▾`, and is named by exactly that text', () => {
    sizeTabs(358)
    const { onBar } = bar(failed())
    const more = document.querySelector<HTMLElement>('.ag-tabs-more')!
    expect(more).not.toBeNull()
    const all = ['Details', 'Logs', 'Changes', 'Attempts', 'Artifacts', 'Checkpoints']
    const hidden = all.filter((l) => !onBar().includes(l))
    expect(hidden.length).toBeGreaterThan(1)
    // MUTATION: draw `2 more tabs ▾` again -- the phone reader still cannot see Checkpoints is behind it.
    expect(flat(more)).toBe(`${hidden.length} more: ${hidden.join(', ')} ▾`)
    // MUTATION (c02/c20 kept): drop the count and the button says the names only.
    expect(flat(more)).toMatch(/^\d+ more: /)
    // MUTATION: keep a separate aria-label (`2 more tabs: ...`) -- the name and the text it draws part again.
    expect(more.hasAttribute('aria-label')).toBe(false)
    expect(screen.getByRole('button', { name: `${hidden.length} more: ${hidden.join(', ')}` })).toBe(more)
  })

  it('one hidden tab reads `1 more: <Tab> ▾`', () => {
    sizeTabs(62 + 46 + 84 + 130 + 130 + 110)
    const { onBar } = bar(failed())
    const more = document.querySelector<HTMLElement>('.ag-tabs-more')!
    expect(onBar()).not.toContain('Checkpoints')
    // MUTATION: say `1 more tab` and the one tab behind it is not named on the glass.
    expect(flat(more)).toBe('1 more: Checkpoints ▾')
  })
})

// ---------------------------------------------------------- 2: the error jump

function step(n: number, over: Partial<TranscriptStep> = {}): TranscriptStep {
  return {
    id: `L${n}:0`,
    line_offset: n * 100,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: `step ${n}`,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: null,
    ...over,
  }
}

/** A failed tool call: its call, then its result marked is_error. */
function failedTool(n: number): TranscriptStep[] {
  const id = `toolu_${n}`
  return [
    step(n, { kind: 'tool_call', tool: { id, name: 'Bash', input: '{}' } }),
    step(n + 1, { kind: 'tool_result', role: 'user', text: null, tool_result: { tool_use_id: id, is_error: true, content: 'exit 1', images: 0 } }),
  ]
}

function transcript(steps: TranscriptStep[]): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(40), completed_at: ago(30), exit_code: 1 },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'final',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/final/agent_stdout.log`,
      object_updated_at: ago(30),
      age_seconds: 1800,
      total_bytes: 1000,
      offset: 0,
      returned_bytes: 1000,
      next_offset: null,
      truncated: false,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps,
    complete: true,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as TaskTranscript
}

function stream(name: LogStream['stream']): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    content: '',
    total_bytes: 0,
    offset: 0,
    returned_bytes: 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: ago(30),
    age_seconds: 1800,
  } as LogStream
}

function logs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(40), completed_at: ago(30), exit_code: 1 },
    read_at: new Date().toISOString(),
    streams: [stream('agent_stderr'), stream('stdout'), stream('stderr')],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  } as TaskLogs
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

const errButton = () => dock().querySelector<HTMLButtonElement>('button[aria-keyshortcuts="E"]')!
const long = () => errButton().querySelector('.ag-logs-long')?.textContent
const short = () => errButton().querySelector('.ag-logs-short')?.textContent

describe('2 (box 6343764730): the error jump never shows a dash', () => {
  beforeEach(() => {
    localStorage.clear()
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', task_id: ID, exit_code: 1 })] }))
    api.loadTaskLogs.mockResolvedValue(ok(logs()))
  })

  it('one error line, none jumped to: `Error 1 of 1`, short `Error 1/1`', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1, { text: 'reading the repository' }), ...failedTool(2)])))
    render(<AgentLogs task={failed()} />)
    await waitFor(() => expect(dock().querySelectorAll('[data-log-key].is-err')).toHaveLength(1))
    await waitFor(() => expect(errButton().disabled).toBe(false))
    // MUTATION: draw `Error – of 1` again -- the dash reads as a missing figure where there is only one.
    expect(long()).toBe('Error 1 of 1')
    expect(short()).toBe('Error 1/1')
    // MUTATION: name the control from different words than it draws.
    expect(screen.getByRole('button', { name: /^Error 1 of 1/ })).toBe(errButton())
  })

  it('several error lines, none jumped to: `N error lines`, short `N err ln`; a jump then reads `Error k of N`', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1, { text: 'reading the repository' }), ...failedTool(2), ...failedTool(4)])))
    render(<AgentLogs task={failed()} />)
    await waitFor(() => expect(dock().querySelectorAll('[data-log-key].is-err')).toHaveLength(2))
    await waitFor(() => expect(errButton().disabled).toBe(false))
    // MUTATION: draw `Error – of 2` again -- a dash where no line is current.
    expect(long()).toBe('2 error lines')
    // MUTATION: short `Error –/2` -- and not the owner's `N err ln` shape (N13).
    expect(short()).toBe('2 err ln')
    expect(long()).not.toContain('–')
    fireEvent.click(errButton())
    // MUTATION: keep the count words after a jump -- the reader loses which error is on screen.
    await waitFor(() => expect(long()).toBe('Error 1 of 2'))
    expect(short()).toBe('Error 1/2')
    fireEvent.click(errButton())
    await waitFor(() => expect(long()).toBe('Error 2 of 2'))
  })
})

// -------------------------------------------------------- 3: the children count

function page(tasks: Task[]): TaskPage {
  return { tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage
}

describe('3 (6:task-002): the Children tab and pane count the same read', () => {
  beforeEach(() => {
    api.loadChildren.mockReset()
  })

  it('on `/children`, an agent that does not offer children still reads its count', async () => {
    // A child: the API serves the fields, so the pane reads and draws `none`.
    const child = row({ parent_task_id: PARENT } as Partial<Task>)
    api.loadChildren.mockResolvedValue(ok(page([])))
    const { result } = renderHook(() => useChildCount(child, '0', 'children'))
    // MUTATION: read only when the agent offers children -- the tab draws `—` over the pane's `none`.
    await waitFor(() => expect(result.current).toBe(0))
    expect(api.loadChildren).toHaveBeenCalledWith(child.id)
  })

  it('the count is the read, not a guess: a row the read returns is counted', async () => {
    const child = row({ parent_task_id: PARENT } as Partial<Task>)
    const grandchild = row({ id: 'task_0123456789abcdef0f02', parent_task_id: child.id } as Partial<Task>)
    api.loadChildren.mockResolvedValue(ok(page([grandchild])))
    const { result } = renderHook(() => useChildCount(child, '0', 'children'))
    // MUTATION: count a child's children as 0 without reading -- the tab and the pane's list disagree.
    await waitFor(() => expect(result.current).toBe(1))
  })

  it('off `/children`, an agent that does not offer children reads nothing', async () => {
    const child = row({ parent_task_id: PARENT } as Partial<Task>)
    api.loadChildren.mockResolvedValue(ok(page([])))
    const { result } = renderHook(() => useChildCount(child, '0', 'detail'))
    await new Promise((r) => setTimeout(r, 20))
    // MUTATION: read for every agent on every pane -- a child's every pane costs a list read.
    expect(api.loadChildren).not.toHaveBeenCalled()
    expect(result.current).toBeNull()
  })
})

describe('3 (6:task-002): the split hands the count its pane', () => {
  beforeEach(() => {
    localStorage.clear()
    api.loadChildren.mockReset()
    api.loadAgentRun.mockReturnValue(new Promise(() => {}))
    api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
    api.loadTranscript.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  })

  it('<AgentSplit pane="children"> on a child counts the row its read returns', async () => {
    const child = row({ parent_task_id: PARENT } as Partial<Task>)
    const grandchild = row({ id: 'task_0123456789abcdef0f02', parent_task_id: child.id } as Partial<Task>)
    api.loadTask.mockResolvedValue(ok(child))
    api.loadChildren.mockResolvedValue(ok(page([grandchild])))
    render(<AgentSplit taskId={child.id} pane="children" artifact={null} closeTo="work/running/live" go={() => {}} base={`work/task/${child.id}`} />)
    await waitFor(() => expect(api.loadChildren).toHaveBeenCalledWith(child.id))
    // MUTATION: drop `pane` from AgentSplit's `useChildCount(task, \`${reads}\`, pane)` -- the child is never
    // counted, paneTabs falls back to 0, and the tab reads `Children, 0` over the pane's one row.
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Children, 1' })).toBeTruthy())
    expect(screen.queryByRole('tab', { name: 'Children, 0' })).toBeNull()
  })
})
