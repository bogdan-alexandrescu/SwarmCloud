// QAFIX2-TASK-PANES (part of #1680): visual QA run 2 (wf_b6e2441b419f423a84eb,
// 2026-10-20, the fixture build of main at 75148351, 1440 and 390 px, both
// themes) still saw two findings PR 1715 and PR 1721 had fixed once.
//
//   QA-113  The Attempts tab said `3 attempts · 9 events` over cards that
//           each said `0 events`; `partial` sat beside `none on this page`;
//           Attempt 1 said `never started` over its BACKEND_REJECTED error;
//           an empty `Before any attempt` card led the list; and on a phone
//           the attempts that ran were below the fold under the empty ones.
//   QA-115  On a failed task the transcript's own `result · success` line was
//           read as the failure's explanation -- in the Failed card's last
//           log line, and at the end of the Logs tab under `Errors 0`.
//
// The fold itself is not a unit-test fact: what is held here is the ORDER the
// cards come in, which is what puts the attempts that ran at the top.

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Every read this file does not answer never answers, so nothing it does
  // not name can land and change what is asserted.
  const quiet = Object.fromEntries(
    Object.entries(actual)
      .filter(([k, v]) => k.startsWith('load') && typeof v === 'function')
      .map(([k]) => [k, () => new Promise(() => {})]),
  )
  return { ...actual, ...quiet, ...api, USE_FIXTURES: false }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { AgentLogs, LogLastLine } = await import('../AgentLogs')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockReturnValue(new Promise(() => {}))
  api.loadAgentDetail.mockReturnValue(new Promise(() => {}))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
})

// ---------------------------------------------------------------------------
// QA-113: the Attempts tab
// ---------------------------------------------------------------------------

/**
 * The fixture build's shape: attempt 1 rejected at dispatch, attempt 2 exit
 * 1, attempt 3 exit 1, and every event on the page under attempt 3.
 */
function rejectedThenFailed(): { rows: AttemptRow[]; events: TaskEvent[] } {
  const rows = [
    attempt(3, { created_at: at(30), started_at: at(30), completed_at: at(34), exit_code: 1, error: 'exit status 1' }),
    attempt(2, { created_at: at(20), started_at: at(20), completed_at: at(24), exit_code: 1, error: 'exit status 1' }),
    attempt(1, {
      created_at: at(9),
      started_at: null,
      completed_at: null,
      exit_code: null,
      execution_name: null,
      error: 'BACKEND_REJECTED',
    }),
  ]
  const events = [
    { ...ev('lease_acquired', at(30), 'att_3'), generation: 3 },
    { ...ev('running', at(31), 'att_3'), generation: 3 },
    { ...ev('failed', at(34), 'att_3'), generation: 3 },
  ]
  return { rows, events }
}

async function attemptsTab(rows: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts: rows }))
  api.loadAgentDetail.mockResolvedValue(
    ok({ task: runTask({ state: 'FAILED', started_at: at(20), completed_at: at(34) }), events, eventsDetail: null }),
  )
  const { container } = render(<AttemptTimelineScreen taskId="tsk_charts" />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

const titles = (el: HTMLElement) =>
  Array.from(el.querySelectorAll<HTMLElement>('.att-card .ctl-card-title')).map((h) => h.firstChild?.textContent ?? '')

describe('QA-113: the Attempts tab’s counts agree and what ran comes first', () => {
  it('leads with the newest attempt and draws no empty "Before any attempt" card', async () => {
    const { rows, events } = rejectedThenFailed()
    const el = await attemptsTab(rows, events)
    // MUTATION: draw the preface group when it holds no event -- an empty card leads the tab again.
    expect(el.textContent).not.toMatch(/Before any attempt/)
    // MUTATION: put the cards back oldest first -- on a phone the attempts that ran fall below the fold.
    expect(titles(el)).toEqual(['Attempt 3 · gen 3', 'Attempt 2 · gen 2', 'Attempt 1 · gen 1'])
  })

  it('keeps a "Before any attempt" card when an event really is before every attempt', async () => {
    const { rows, events } = rejectedThenFailed()
    const el = await attemptsTab(rows, [ev('submitted', at(-1), null), ...events])
    // MUTATION: drop the preface group altogether -- the submitted event vanishes from the tab.
    expect(titles(el)).toContain('Before any attempt')
    // MUTATION: leave the preface first in a newest-first list -- the oldest thing leads.
    expect(titles(el).at(-1)).toBe('Before any attempt')
  })

  it('says a card holds no events on this page in words, with no `partial` beside a `none`', async () => {
    const { rows, events } = rejectedThenFailed()
    const el = await attemptsTab(rows, events)
    const notes = Array.from(el.querySelectorAll('.att-card .ctl-card-note')).map((n) => n.textContent)
    // MUTATION: put `0 events` back on an empty card -- it reads as a measured zero against the toolbar's 3.
    // The card holding all three of the page's events names its backend only:
    // the toolbar says `3 events` (QA2-FIX-13, overview-agents-41).
    expect(notes).toEqual(['Cloud Run', 'no events on this page · Cloud Run', 'no events on this page · Cloud Run'])
    // MUTATION: restore the card's `partial · none on this page` line -- two words that contradict.
    expect(el.querySelector('.att-card .ctl-mark.is-partial')).toBeNull()
    for (const card of el.querySelectorAll('.att-card')) expect(card.textContent).not.toMatch(/none on this page/)
    // The toolbar keeps its one tally of what the page cannot prove, and it agrees with the cards.
    const toolbar = el.querySelector<HTMLElement>('.ctl-toolbar .is-end')!
    // MUTATION: drop the toolbar's tally -- nothing says two attempts are without events.
    expect(toolbar.textContent).toMatch(/3 events · first page · partial 2 attempts without events/)
  })

  it('does not say an attempt with an error never started', async () => {
    const { rows, events } = rejectedThenFailed()
    const el = await attemptsTab(rows, events)
    const first = Array.from(el.querySelectorAll<HTMLElement>('.att-card')).find((c) =>
      c.querySelector('.ctl-card-title')?.textContent?.startsWith('Attempt 1'),
    )!
    const head = first.querySelector<HTMLElement>('.ctl-card-title')!
    // MUTATION: drop the error branch in `outcome` -- `never started` sits over BACKEND_REJECTED again.
    expect(head.textContent).not.toMatch(/never started/)
    expect(within(head).getByText('failed to start')).toBeTruthy()
    // MUTATION: draw it in `unknown` -- a rejected dispatch reads as no verdict.
    expect(head.querySelector('[data-tone="bad"]')).not.toBeNull()
    // The error is said in words, and its code kept in the title (QA2-FIX-13, ovag2-07).
    expect(first.querySelector('pre.err')?.textContent).toBe('The backend refused to start it.')
    expect(first.querySelector('pre.err')?.getAttribute('title')).toBe('BACKEND_REJECTED')
  })

  it('still says "never started" for an attempt with no error', async () => {
    const el = await attemptsTab(
      [attempt(1, { started_at: null, completed_at: null, exit_code: null, execution_name: null, error: null })],
      [],
    )
    // MUTATION: say `failed to start` for every unstarted attempt -- a queued one is not a failure.
    expect(within(el.querySelector<HTMLElement>('.att-card .ctl-card-title')!).getByText('never started')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// QA-115: the agent's own `success` on a failed task
// ---------------------------------------------------------------------------

const ID = 'task_0123456789abcdef0115'

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

const successResult = (n: number) =>
  step(n, { kind: 'result', role: null, text: '## Summary\n\nDone.', meta: { subtype: 'success', is_error: false, num_turns: 7 } })

function transcript(steps: TranscriptStep[]): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: {
      status: 'latest',
      known: true,
      generation: 1,
      created_at: at(0),
      completed_at: at(10),
      exit_code: 1,
    },
    read_at: at(11),
    stream: {
      stream: 'agent_stdout',
      source: 'final',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/logs/agent_stdout.log`,
      object_updated_at: at(10),
      age_seconds: 3,
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
    answer_in_window: true,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as TaskTranscript
}

/** The polled streams' read, holding nothing: these tests are about the transcript. */
function noLogs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: transcript([]).attempt,
    read_at: at(11),
    streams: [],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function failedTask(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    runner_profile: 'claude-code',
    started_at: at(0),
    completed_at: at(10),
    last_error: 'exit status 1',
    ...over,
  })
}

describe('QA-115: a failed task’s success line is not offered as its cause', () => {
  it('the Failed card’s last log line says the CLI reported success and the task failed after it', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), successResult(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<LogLastLine task={failedTask()} onOpen={() => {}} />)
    const strip = screen.getByRole('button', { name: /^Last log line/ })
    await waitFor(() => expect(strip.querySelector('.ag-loglast-line')?.textContent).not.toBe('reading…'))
    // MUTATION: drop the success branch in `LogLastLine` -- `result ## Summary` sits under the error as its cause.
    expect(strip.querySelector('.ag-loglast-line')!.textContent).toBe(
      'its CLI reported success · it failed after that, with the error above',
    )
    expect(strip.textContent).not.toMatch(/## Summary/)
  })

  it('keeps the newest line verbatim when the task did not fail', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), successResult(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<LogLastLine task={failedTask({ state: 'SUCCEEDED', last_error: null })} onOpen={() => {}} />)
    const strip = screen.getByRole('button', { name: /^Last log line/ })
    // MUTATION: rewrite every success result -- a succeeded task's own line is replaced for nothing.
    await waitFor(() => expect(strip.textContent).toContain('Summary'))
  })

  it('the Logs tab says the transcript’s success is not the failure, and names the error', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', exit_code: 1, error: 'exit status 1' })] }))
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), successResult(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<AgentLogs task={failedTask()} />)
    const dock = screen.getByRole('region', { name: 'Logs' })
    // MUTATION: drop the note in `AgentLogs` -- `result · success · 7 turns` ends a FAILED task's log unexplained.
    // The finished log's `Ended · complete log` chip is a note too (QAFIX4-AGENT-PANES); this is the other one.
    const note = await waitFor(() => {
      const found = within(dock).getAllByRole('note').find((n) => !n.matches('.ag-logs-final'))
      expect(found).toBeDefined()
      return found!
    })
    expect(note.textContent).toMatch(/This agent failed after its CLI reported success/)
    // MUTATION: leave the error out of the note -- the reader is sent elsewhere to learn the cause.
    expect(within(note).getByText('exit status 1').tagName).toBe('CODE')
  })

  it('the Logs tab draws no such note on a failed task whose transcript ends otherwise', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', exit_code: 1, error: 'exit status 1' })] }))
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), step(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<AgentLogs task={failedTask()} />)
    const dock = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(dock.textContent).toContain('step 2'))
    // MUTATION: draw the note on every failed task -- it would claim a success the transcript never wrote.
    expect(within(dock).queryAllByRole('note').filter((n) => !n.matches('.ag-logs-final'))).toEqual([])
  })

  it('names the error button by what it counts: lines, not errors', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', exit_code: 1, error: 'exit status 1' })] }))
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), successResult(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<AgentLogs task={failedTask()} />)
    const dock = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(dock.textContent).toContain('Summary'))
    // MUTATION: restore the narrow label `Errors 0` -- on a FAILED task it reads as "no errors".
    expect(dock.textContent).not.toMatch(/Errors 0/)
    const button = within(dock).getByRole('button', { name: /^No error matched/ })
    // MUTATION: put `Errors 0` back as the narrow label -- the phone's bar says "no errors" on a failed task.
    expect(button.querySelector('.ag-logs-short')?.textContent).not.toMatch(/^Errors 0$/)
  })

  // ff-010 and task-022, QAFIX3-AGENT (visual QA run 3, wf_d598fa7bffdb41fc9d76,
  // 2026-10-21): QA-115's narrow `No match` sits beside the search box, so
  // before anything was searched the bar read as a search that found nothing.
  it('gives the error button a narrow label that names error lines, never a search outcome', async () => {
    api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', exit_code: 1, error: 'exit status 1' })] }))
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1), successResult(2)])))
    api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
    render(<AgentLogs task={failedTask()} />)
    const dock = screen.getByRole('region', { name: 'Logs' })
    await waitFor(() => expect(dock.textContent).toContain('Summary'))
    const search = within(dock).getByRole('searchbox', { name: 'Search this window' })
    expect((search as HTMLInputElement).value).toBe('')
    const button = within(dock).getByRole('button', { name: /^No error matched/ })
    const short = button.querySelector('.ag-logs-short')?.textContent ?? ''
    // MUTATION: put `No match` back -- nothing was searched, and the bar says a search found nothing.
    expect(short).not.toMatch(/match|found/i)
    // MUTATION: say `Errors 0` or `0 errors` -- QA-115's "no errors" on a failed task.
    expect(short).toBe('0 err ln')
    // MUTATION: draw a search count with an empty box -- the only search outcome is the one asked for.
    expect(dock.querySelector('.ag-logs-count')).toBeNull()
  })
})
