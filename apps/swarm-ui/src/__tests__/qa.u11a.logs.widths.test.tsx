// SPLIT (part of #2688, 2026-11-07): this file, `qa.u11a.logs.test.tsx`, `qa.u11a.logs.running.test.tsx`, `qa.u11a.logs.controls.test.tsx`
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
import { render, screen } from '@testing-library/react'
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
const { resetListSnap, setListSnap, SNAPS } = await import('../listSnap')

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

describe('no element of the detail is covered by a log element, at any split width', () => {
  const POSITIONED = /^(fixed|absolute|sticky)$/

  async function check(start: Pane, task: Task) {
    serve(task)
    const r = render(<Routed start={start} onGo={() => {}} />)
    await landed(api.loadTask)
    expect(document.querySelector('.ag-head-facts')).not.toBeNull()
    // Details draws its pane from a second read (loadAgentRun); wait for it, or a loaded runner asserts on "Reading…".
    if (start === 'detail') {
      await landed(api.loadAgentRun)
      expect(document.querySelector('.ag-loglast')).not.toBeNull()
    }
    return r
  }

  // ONE CASE PER SNAP AND PANE, not one per snap walking all five panes: a
  // case is a whole split render and its CSSOM reads, ~2.5 s idle, and five
  // in one case went past the 30 s timeout on a loaded CI shard (PR #1707).
  // A timed-out case leaves its `landed` act scope open, and every later
  // synchronous render in the file then draws nothing.
  const STARTS: readonly (readonly [Pane, () => Task])[] = [
    ['detail', finished],
    ['logs', running],
    ['attempts', running],
    ['artifacts', running],
    ['checkpoints', running],
  ]
  for (const snap of SNAPS) {
    for (const [start, make] of STARTS) {
      it(`draws the log in the pane's flow and nowhere else, list at ${snap}, opened on ${start}`, async () => {
        setListSnap(snap)
        // The detail column's width at this snap, 1440 wide: what the container
        // rules in the sheet are evaluated at.
        const column = snap === 'strip' ? 1000 : snap === 'list' ? 630 : 500
        const env: CascadeEnv = { width: 1440, container: column }
        const { unmount, container } = await check(start, make())
        const split = container.querySelector('.ag-split')!
        // No dock, no strip, no grip, no full view: the dock is gone.
        expect(document.querySelector('[class*="ag-logdock"]'), `${start}: a dock element is drawn`).toBeNull()
        expect(split.querySelector('[role="separator"][aria-label="Resize the log"]')).toBeNull()
        expect(screen.queryByRole('button', { name: 'Open full' })).toBeNull()
        // Every log element is inside the open pane.
        const pane = split.querySelector(':scope > .ag-split-pane')!
        for (const el of document.querySelectorAll('.ag-logs, .ag-loglast')) {
          expect(pane.contains(el), `${start}: a log element outside the pane`).toBe(true)
          expect(painted(el, 'position', env) ?? 'static', `${start}: ${el.className} is laid over the pane`).not.toMatch(POSITIONED)
        }
        if (start === 'logs') {
          const logs = pane.querySelector(':scope > .ag-logs')!
          // The whole detail area under the header, and nothing beneath it.
          const shown = [...pane.children].filter((c) => !(c as HTMLElement).hidden)
          expect(shown, 'something is drawn beside the log in its pane').toEqual([logs])
          expect(pane.classList.contains('is-logs')).toBe(true)
          expect(painted(pane, ['overflow-y', 'overflow'], env)).toBe('hidden')
          expect(painted(logs, ['flex', 'flex-grow'], env)).toMatch(/^1\b/)
          expect(painted(logs.querySelector('.ag-logs-body')!, ['overflow-y', 'overflow'], env)).toBe('auto')
          // The pane is the column's last child: nothing is docked under it.
          expect(split.lastElementChild).toBe(pane)
        }
        if (start === 'detail') {
          const line = pane.querySelector('.ag-loglast')
          expect(line, 'Details has no last log line').not.toBeNull()
          // IN THE LEADING CARD, at the top of the Details stack
          // (agent-details-v3.html A): in the pane's flow, over nothing.
          expect(line!.closest('.dt-now')).toBe(pane.querySelector('.dt > .dt-now'))
        }
        unmount()
      })
    }
  }
})

