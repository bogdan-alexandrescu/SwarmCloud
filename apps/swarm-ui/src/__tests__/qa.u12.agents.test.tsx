/**
 * QA ROUND 3, LANE U12 (owner, 2026-10-04, live console at main 8455be37,
 * 1440x900 and 390): the agent split, its Logs tab and the agent list.
 *
 *   R3   Enter in the log search scrolled the PAGE 0 -> 148.5, and a keyboard
 *        move of the divider shifted it 0 -> 12. Only the log body scrolls.
 *   R4   At the foot of a long list the sticky detail went from top 16 to -32
 *        and cut off Copy link / Stop / Close.
 *   R6   On a phone the stream buttons were 44px tall in a 28px control.
 *   R7   Beside the 64px strip the Runner table ran to 2049px in 899; a strip
 *        row's only accessible name was its state word.
 *   R9   The search had no room in a wide bar; the Details "Last log line"
 *        was empty with no line; the transcript's facts were open; the
 *        attempt picker said gen 1 under a gen 2 header; bold text drew its
 *        code spans' backticks raw.
 *   JUMP The Errors control was disabled at 0 on every agent, the mock's own
 *        `deterministic failure, exit 2` included.
 *   R13  The 'upstream' step's row drew its id bold and larger on line two.
 *   D21  The Checkpoints tab said `2 of 2` while the Details tile said 4, then
 *        6: the pane read once, and the tile read on its own.
 *
 * MUTATIONS: put `scrollIntoView` back in `jump`; drop `holdPageScroll`; put
 * `.app`'s 48px back under the split; drop the phone `height: auto`; drop
 * the table's fixed layout or the uri's ellipsis; drop `.cr-vh`; drop the
 * wide floor; let a blank line be the last line; unfold the transcript's
 * facts; read "latest" when the current generation is known; stop recursing
 * into bold; narrow the error pattern; drop `.tid-text`'s weight; read the
 * tile from its own listing or stop the pane's poll -- each turns a case red.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, task as runTask } from './runfixture'
import { fixedColumns } from './tablefit'

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

const { AgentSplit, HOLD_SCROLL_MS } = await import('../AgentSplit')
const { AgentLogs, LogLastLine } = await import('../AgentLogs')
const { resetListSnap } = await import('../listSnap')
const { isErrorLine } = await import('../logLines')
const { stepIsError } = await import('../logMarks')
const { Markdown } = await import('../ArtifactViewer')
const { CHECKPOINTS_POLL_MS, CheckpointsPane } = await import('../CheckpointsPane')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
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

function logs(streams: LogStream[], attemptId = 'att_1'): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: attemptId,
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
    prefix: `tenants/eng/tasks/${ID}/attempts/${attemptId}/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function transcript(text: string): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    format: 'stream-json',
    stream: {
      status: 'ok', source: 'final', detail: null, uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/agent_stdout.log`,
      next_offset: null, object_updated_at: new Date().toISOString(), age_seconds: 3,
    },
    steps: [{ id: 's1', kind: 'text', role: 'assistant', text, tool: null, tool_result: null, meta: {}, raw: null, truncated_fields: [] }],
    skipped_lines: 0,
    redaction_count: 0,
    capture_truncated: false,
  } as unknown as TaskTranscript
}

function running(over: Partial<Task> = {}): Task {
  // `mock` opens on the runner's two streams.
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'mock', attempt_count: 1, completed_at: null, ...over })
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

function Routed({ start }: { start: Pane }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

/** A page scroller whose `scrollTop` holds what is written to it (jsdom's does not). */
function pageScroller(): HTMLElement {
  const scroller = document.createElement('div')
  scroller.className = 'ctl-scroll'
  let top = 0
  Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
  document.body.appendChild(scroller)
  return scroller
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTranscript.mockResolvedValue({ status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no transcript' } })
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'one\nrepository two\n'), stream('stderr', 'repository three\n')])))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  delete document.documentElement.dataset.agentList
  delete (Element.prototype as unknown as Record<string, unknown>).scrollIntoView
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('R3: scrolling to a match moves only the log body', () => {
  it('never calls scrollIntoView on Enter, and leaves the page scroller where it was', async () => {
    const into = vi.fn()
    ;(Element.prototype as unknown as { scrollIntoView: unknown }).scrollIntoView = into
    const scroller = pageScroller()
    render(<AgentLogs task={running()} />, { container: scroller.appendChild(document.createElement('div')) })
    const region = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(region.querySelectorAll('.ag-logline').length).toBeGreaterThan(0))
    const search = within(region).getByRole('searchbox', { name: 'Search this window' })
    fireEvent.change(search, { target: { value: 'repository' } })
    await waitFor(() => expect(region.querySelectorAll('.ag-logline.is-hit').length).toBe(2))
    fireEvent.keyDown(search, { key: 'Enter' })
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(into, 'scrollIntoView scrolls the page too').not.toHaveBeenCalled()
    expect(scroller.scrollTop).toBe(0)
    expect(region.querySelector('.ag-logline.is-current')).not.toBeNull()
  })

  it('puts the page back when a divider move scrolls it', async () => {
    api.loadTask.mockResolvedValue(ok(running({ state: 'SUCCEEDED', completed_at: new Date(Date.now() - 3_600_000).toISOString() })))
    const scroller = pageScroller()
    render(<Routed start="detail" />, { container: scroller.appendChild(document.createElement('div')) })
    const divider = await screen.findByRole('separator', { name: 'Resize the agent list' })
    fireEvent.keyDown(divider, { key: 'ArrowRight' })
    // The re-layout the move causes re-anchors the page (what the browser did).
    scroller.scrollTop = 12
    fireEvent.scroll(scroller)
    expect(scroller.scrollTop, 'the page moved with the divider').toBe(0)
    fireEvent.keyDown(divider, { key: 'ArrowLeft' })
    scroller.scrollTop = 12
    fireEvent.scroll(scroller)
    expect(scroller.scrollTop).toBe(0)
    expect(HOLD_SCROLL_MS).toBeLessThanOrEqual(1000)
  })
})

describe('R4: the detail header stays in view at the foot of a long list', () => {
  it('runs the split\'s grid area to the scrollport\'s foot, so the sticky column never rises', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="sk-main"><div class="ctl-scroll"><div class="app has-inspector"><main class="work"></main>' +
      '<div class="drawer ctl-drawer ag-split"></div></div></div></div>'
    document.body.appendChild(host)
    const app = host.querySelector('.app')!
    const col = host.querySelector('.ag-split')!
    // The sticky box may rise by whatever lies between its grid area's foot
    // and the scrollport's foot: `.app`'s bottom padding.
    expect(painted(app, ['padding-bottom'], WIDE), 'the column rises by the padding under it').toBe('0')
    expect(painted(host.querySelector('.work')!, 'padding-bottom', WIDE)).toBe('48px')
    expect(painted(col, 'position', WIDE)).toBe('sticky')
    expect(painted(col, 'top', WIDE)).toBe('16px')
  })
})

describe('R6: the phone\'s stream control is sized for touch', () => {
  it('lets the small segmented control grow to its 44px buttons', () => {
    render(<AgentLogs task={running()} />)
    const seg = screen.getByRole('region', { name: 'Logs' }).querySelector('.ag-logs-streams')!
    expect(seg.classList.contains('is-sm')).toBe(true)
    expect(painted(seg, 'height', PHONE)).toBe('auto')
    for (const b of seg.querySelectorAll('button')) expect(painted(b, 'min-height', PHONE)).toBe('44px')
  })
})

describe('R7: beside the strip', () => {
  it('fits the Runner table to the log, Size and Age inside it, the uri cut', async () => {
    const env: CascadeEnv = { width: 1440, container: 899 }
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    // Unfold the table: a stream with no lines keeps it open.
    api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', ''), stream('stderr', 'x\n')])))
    const table = await waitFor(() => {
      const t = region.querySelector<HTMLTableElement>('.ctl-table > table')
      expect(t).not.toBeNull()
      return t!
    })
    expect(painted(table, 'width', env)).toBe('100%')
    const { cols } = fixedColumns(table, 899, env)
    expect(cols.length).toBe(3)
    for (const c of cols) expect(c.end, `${c.col} is past the log's edge`).toBeLessThanOrEqual(899)
    expect(cols[0]!.end - cols[0]!.start).toBeGreaterThan(400)
    const uri = table.querySelector('tbody th .uri')!
    expect(painted(uri, 'white-space', env)).toBe('nowrap')
    expect(painted(uri, 'text-overflow', env)).toBe('ellipsis')
    expect(painted(uri, ['overflow-x', 'overflow'], env)).toBe('hidden')
    expect(uri.getAttribute('title')).toMatch(/^gs:\/\//)
  })

  it('names a strip row by its agent and id, not its state word', async () => {
    document.documentElement.dataset.agentList = 'strip'
    const { TaskRow } = await import('../Agents')
    const host = document.createElement('div')
    host.className = 'app has-inspector'
    const work = document.createElement('main')
    work.className = 'work'
    host.appendChild(work)
    document.body.appendChild(host)
    render(<TaskRow task={runTask({ id: ID, state: 'RUNNING', step_id: 'upstream', runner_profile: 'mock' })} now={Date.now()} onOpen={() => {}} classes={null} />, { container: work })
    const row = work.querySelector('.row.is-compact')!
    const drawn = [...row.children].filter((c) => painted(c, 'display', WIDE) !== 'none')
    const words = drawn.map((c) => c.textContent ?? '').join(' ')
    expect(words).toContain('upstream')
    expect(words).toContain(ID)
    // Outside the strip the words are not drawn twice.
    delete document.documentElement.dataset.agentList
    expect(painted(row.querySelector('.cr-vh')!, 'display', WIDE)).toBe('none')
  })
})

describe('R13: one row format', () => {
  // The id moved from line two to its own line under the name (#94,
  // `TaskIdLine`); the rule R13 set for it holds there.
  it('draws a step row\'s id at line two\'s size and weight', async () => {
    const { TaskRow } = await import('../Agents')
    const { container } = render(<TaskRow task={runTask({ id: ID, state: 'RUNNING', step_id: 'upstream', runner_profile: 'mock' })} now={Date.now()} onOpen={() => {}} classes={null} />)
    expect(container.querySelector('.cr-sub .id'), 'the id is on line two again').toBeNull()
    const id = container.querySelector('.cr-name .tid-text')!
    expect(id.textContent).toBe(ID)
    expect(painted(id, 'font-weight', WIDE), 'the id is bold').toBe('400')
    expect(painted(id, 'font-size', WIDE)).toBe('var(--t-micro)')
  })
})

describe('R9: the Logs tab\'s polish', () => {
  it('gives the search a floor that holds its placeholder in a wide bar', () => {
    render(<AgentLogs task={running()} />)
    const label = screen.getByRole('region', { name: 'Logs' }).querySelector('.ag-logs-search')!
    const floor = Number.parseFloat(painted(label, 'min-width', { width: 1440, container: 1000 }) ?? '0')
    // 'Search this window' in the 13px sans, with the input's 16px of padding.
    expect(floor).toBeGreaterThanOrEqual('Search this window'.length * 0.56 * 13 + 16)
  })

  it('says "no log lines yet" when a running agent has written none', async () => {
    api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', '\n'), stream('stderr', '')])))
    render(<LogLastLine task={running()} onOpen={() => {}} />)
    const line = await waitFor(() => {
      const el = document.querySelector('.ag-loglast-line')!
      expect(el.textContent).not.toBe('reading…')
      return el
    })
    expect(line.textContent).toBe('no log lines yet')
  })

  it('draws a transcript line\'s code spans as code in the Details strip', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript('Edited `apps/x.py` and **ran `pytest`**')))
    render(<LogLastLine task={running({ runner_profile: 'claude-code' })} onOpen={() => {}} />)
    const line = await waitFor(() => {
      const el = document.querySelector('.ag-loglast-line')!
      expect(el.querySelector('code')).not.toBeNull()
      return el
    })
    expect(line.textContent).not.toContain('`')
  })

  it('folds the transcript\'s facts in the Logs tab, as stdout\'s are', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript('hello')))
    render(<AgentLogs task={running({ runner_profile: 'claude-code' })} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    const fold = await waitFor(() => {
      const d = region.querySelector<HTMLDetailsElement>('.arts-transcript > details.ag-logmeta')
      expect(d).not.toBeNull()
      return d!
    })
    expect(fold.open).toBe(false)
    expect(fold.querySelector('.ctl-facts')).not.toBeNull()
  })

  it('defaults the attempt picker to the current generation and reads that attempt', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' }), attempt(2, { attempt_id: 'att_2', completed_at: null, exit_code: null })] }))
    render(<AgentLogs task={running({ attempt_count: 2, current_generation: 2 })} />)
    const select = await screen.findByRole('combobox', { name: 'Attempt, by generation' })
    await waitFor(() => expect((select as HTMLSelectElement).selectedOptions[0]?.textContent).toMatch(/gen 2/))
    await waitFor(() => expect(api.loadTaskLogs.mock.calls.some((c) => (c[1] as { attemptId?: string }).attemptId === 'att_2')).toBe(true))
  })

  it('draws a code span inside bold as code', () => {
    const { container } = render(<Markdown source={'**Edit `x.py` now**'} />)
    expect(container.querySelector('strong code')?.textContent).toBe('x.py')
    expect(container.textContent).not.toContain('`')
  })
})

describe('JUMP: the error matcher', () => {
  it('matches the lines an operator calls errors', () => {
    for (const line of [
      'deterministic failure, exit 2',
      'error: no such file or directory',
      'Error: boom',
      'RuntimeException: boom',
      'ValueError: bad input',
      'Traceback (most recent call last):',
      'the step failed',
      'process exited with status 3',
      'exit_code=2',
      'Exception in thread main',
    ]) {
      expect(isErrorLine(line), line).toBe(true)
    }
    for (const line of ['exit 0', 'exit code 0', 'errors were handled', '0 failures', 'collected 214 items']) {
      expect(isErrorLine(line), line).toBe(false)
    }
  })

  it('enables Errors on the mock\'s stderr and says what it matches', async () => {
    api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'starting\n'), stream('stderr', 'deterministic failure, exit 2\n')])))
    render(<AgentLogs task={running()} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    const button = await waitFor(() => {
      const b = within(region).getByRole('button', { name: /^Error/ })
      expect((b as HTMLButtonElement).disabled).toBe(false)
      return b
    })
    const title = button.getAttribute('title') ?? ''
    for (const word of ['is_error', 'error', 'exception', 'failed', 'Traceback', 'non-zero exit']) expect(title).toContain(word)
  })
})

describe('JUMP: a final result marked is_error is a jump target', () => {
  it('counts a result step with meta.is_error and neutral text, and lands on it', async () => {
    const t = transcript('all done')
    const resultStep = {
      id: 's2', kind: 'result', role: null, text: 'stopped after the turn limit', tool: null, tool_result: null,
      meta: { subtype: 'error_max_turns', is_error: true, num_turns: 40 }, raw: null, truncated_fields: [],
    }
    ;(t as unknown as { steps: unknown[] }).steps.push(resultStep)
    expect(stepIsError(resultStep as unknown as TranscriptStep)).toBe(true)
    expect(stepIsError({ ...resultStep, meta: { subtype: 'success', is_error: false } } as unknown as TranscriptStep)).toBe(false)
    api.loadTranscript.mockResolvedValue(ok(t))
    api.loadTaskLogs.mockResolvedValue(ok(logs([stream('stdout', 'starting\n')])))
    render(<AgentLogs task={running({ runner_profile: 'claude-code' })} />)
    const region = screen.getByRole('region', { name: 'Logs' })
    const button = await waitFor(() => {
      const b = within(region).getByRole('button', { name: /^Error/ })
      expect((b as HTMLButtonElement).disabled).toBe(false)
      return b
    })
    fireEvent.click(button)
    await waitFor(() => expect(region.querySelector('[data-log-key="step:s2"]')?.className).toContain('is-current'))
  })
})

describe('D21: the Checkpoints tab and the Details tile are one read', () => {
  it('re-reads the pane while the agent runs', { timeout: 8000 }, async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    api.loadTask.mockResolvedValue(ok(running()))
    render(<CheckpointsPane taskId={ID} />)
    await waitFor(() => expect(api.loadTask).toHaveBeenCalledTimes(1))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(CHECKPOINTS_POLL_MS + 50)
    })
    await waitFor(() => expect(api.loadTask.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('draws the tab\'s count in the tile, not a second read of its own', async () => {
    const { Run } = await import('../AgentDetail')
    const t = running({ attempt_count: 1 })
    const run = {
      task: t, events: [], eventsDetail: null,
      attempts: [attempt(1, { attempt_id: 'att_1', completed_at: null, exit_code: null, checkpoints: ['c1', 'c2', 'c3', 'c4', 'c5', 'c6'] })],
      classes: null,
    } as unknown as Parameters<typeof Run>[0]['run']
    const { container } = render(<Run run={run} checkpoints={{ written: 2, kept: 2 }} />)
    // Details v3 (#572) draws the count as the Now card's checkpoint fact,
    // not a metric tile; the property is the same: the tab's read, only.
    const tile = [...container.querySelectorAll('.dt-nf')].find((m) => /Checkpoints/.test(m.textContent ?? ''))!
    expect(tile.textContent).toMatch(/2 written, 2 kept/)
    expect(tile.textContent).not.toMatch(/6/)
  })
})
