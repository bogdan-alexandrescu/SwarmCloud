// SPLIT (part of #2688, 2026-11-07): this file, `qa.u11a.logs.widths.test.tsx`, `qa.u11a.logs.running.test.tsx`, `qa.u11a.logs.controls.test.tsx`
// were one file, 167 s alone with three workers, over the ~150 s line under a
// CI shard. Split along its describe blocks; each file keeps its own fixtures
// and shares no state.

/**
 * U11a (owner QA re-run, 2026-10-04): LOGS ARE A TAB.
 *
 * The owner does not want the log component overlapping the agent details.
 * The bottom dock -- its strip, its resize handle, `Open full` and the page
 * scroll it caused (D7) -- is gone; the log is a full-height Logs tab at
 * `/agents/<tab>/<id>/logs`, with all four streams visible (D13 hid them in a
 * scrolling pill row). An agent opens on Details, running or finished (#1646,
 * 2026-10-19; a running one opened on Logs from 2026-10-04). Escape in the log's More closes the menu and stops there (N11).
 *
 * MUTATIONS: render a log element outside the pane or position one over it
 * (`position: fixed|absolute|sticky` on `.ag-logs`/`.ag-loglast`); drop a
 * stream from the segmented control or let it spill into More; drop the
 * default-tab effect or send a running, finished or waiting agent to Logs; drop `onMoreKey`'s
 * `stopPropagation`; restore `flex-wrap: wrap` on the bar or drop the spill
 * effect; drop the `+ 16px` from `top` or the `- 16px`. Each turns a case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { landed } from './reads'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { AgentLogs } = await import('../AgentLogs')
const { resetListSnap } = await import('../listSnap')
const { addressToPath, pathToAddress } = await import('../paths')
const { fromAddress, canonical } = await import('../App')

const WIDE: CascadeEnv = { width: 1440 }
const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function stream(name: LogStream['stream'], content: string): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/${name}.log`,
    content,
    total_bytes: content.length,
    offset: 0,
    returned_bytes: content.length,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    object_updated_at: new Date().toISOString(),
    age_seconds: 3,
  }
}

function logs(streams: LogStream[]): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: {
      status: 'latest',
      known: true,
      generation: 1,
      created_at: new Date(Date.now() - 60_000).toISOString(),
      completed_at: null,
      exit_code: null,
    },
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function running(over: Partial<Task> = {}): Task {
  // `mock` opens on the runner's two streams.
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'mock', attempt_count: 1, completed_at: null, ...over })
}

function finished(): Task {
  return running({ state: 'SUCCEEDED', completed_at: new Date(Date.now() - 3_600_000).toISOString() })
}

/**
 * The header's read and the Details read of one task. The last log line is in
 * the Details tab's leading card (agent-details-v3.html A), so it is drawn
 * once the Details read lands.
 */
function serve(t: Task) {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadAgentRun.mockResolvedValue(
    ok({ task: t, events: [], eventsDetail: null, attempts: [], attemptsDetail: null, classes: null, classesDetail: null, classesRouteMissing: false }),
  )
}

/**
 * THE TWO READS A DETAILS PANE IS DRAWN FROM (#605): the split's header read
 * of the task (`loadTask`), which decides the pane, then the Details pane's
 * own `loadAgentRun`, whose answer draws the leading card with the last log
 * line. Waiting on these rather than on `findByRole` is what keeps these
 * cases inside a 2 s window on a loaded runner: see reads.ts.
 */
async function detailsLanded(): Promise<void> {
  await landed(api.loadTask)
  await landed(api.loadAgentRun)
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

/** The split with a router that moves the pane as App's does. */
function Routed({ start, onGo }: { start: Pane; onGo: (to: string) => void }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    onGo(to)
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

function tab(name: string): HTMLElement {
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const t = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTranscript.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no transcript' } })
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'one\ntwo\n'), stream('stderr', 'three\n')])))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  delete document.documentElement.dataset.agentList
  window.history.replaceState(null, '', '/')
})

describe('the Logs tab: its address, its place, and all four streams', () => {
  it('sits beside Details and has its own route, /agents/<tab>/<id>/logs', async () => {
    serve(finished())
    const go = vi.fn()
    render(<Routed start="detail" onGo={go} />)
    await detailsLanded()
    screen.getByRole('button', { name: /^Last log line/ })
    const names = within(screen.getByRole('tablist', { name: 'Agent panes' }))
      .getAllByRole('tab')
      .map((t) => t.querySelector('.c-tab-label')?.textContent)
    expect(names.slice(0, 2)).toEqual(['Details', 'Logs'])
    fireEvent.click(tab('Logs'))
    expect(go).toHaveBeenLastCalledWith(`work/task/${ID}/logs`)
    expect(tab('Logs').getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('region', { name: 'Logs' })).toBeTruthy()
    // The address round-trips: the pane, not a piece of the id.
    expect(addressToPath(`work/task/${ID}/logs`, 'recent')).toBe(`/agents/recent/${ID}/logs`)
    expect(pathToAddress(`/agents/recent/${ID}/logs`)?.address).toBe(`work/task/${ID}/logs`)
    const r = fromAddress(`work/task/${ID}/logs`)
    expect(r).toMatchObject({ taskId: ID, taskPane: 'logs' })
    expect(canonical(r)).toBe(`work/task/${ID}/logs`)
  })

  it('renders Transcript, Agent stdout, Agent stderr and Runner as one segmented control in the row', () => {
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    const group = within(region).getByRole('group', { name: 'Which log' })
    expect(group.classList.contains('c-seg'), 'the streams are not the segmented control').toBe(true)
    expect(within(group).getAllByRole('button').map((b) => b.querySelector('.ag-logs-long')?.textContent ?? b.textContent)).toEqual([
      'Transcript',
      'Agent stdout',
      'Agent stderr',
      'Runner',
    ])
    // In the row itself, never scrolled away or under More (D13's hidden streams).
    expect(group.parentElement?.classList.contains('ag-logs-bar')).toBe(true)
    expect(group.closest('details')).toBeNull()
    expect(painted(group, ['overflow-x', 'overflow'], WIDE) ?? 'visible', 'the streams scroll inside the row').not.toMatch(/auto|scroll/)
    // The one on screen is pressed: a mock runner opens on Runner.
    expect(within(group).getByRole('button', { name: 'Runner' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('offers Follow on a live agent only: a finished one says its record is final', () => {
    const { unmount } = render(<AgentLogs task={running()} />)
    expect(within(screen.getByRole('region', { name: 'Logs' })).getByRole('button', { name: /^Following/ })).toBeTruthy()
    unmount()
    render(<AgentLogs task={finished()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    expect(within(region).queryByRole('button', { name: /Follow/ })).toBeNull()
    expect(region.querySelector('.ag-logs-final')?.getAttribute('title')).toMatch(/nothing more will arrive/)
    fireEvent.keyDown(window, { key: 'f' })
    expect(within(region).queryByRole('button', { name: /Follow/ })).toBeNull()
  })

  it('keeps the stream metadata folded when every stream has lines', async () => {
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(region.querySelectorAll('.ag-logline').length).toBeGreaterThan(0))
    const meta = region.querySelector('details.ag-logmeta')
    expect(meta, 'the gs path and size are not folded').not.toBeNull()
    expect(meta!.hasAttribute('open')).toBe(false)
  })
})

