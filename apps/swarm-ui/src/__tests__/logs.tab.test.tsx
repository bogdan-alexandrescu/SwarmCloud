// THE LOGS TAB (owner decision 2026-10-04; it replaced viewers.html pick A's
// bottom dock).
//
// The log is a tab of the agent; it reads the attempt the reader picks, by
// attempt_id; it follows and pauses, counting what arrived while paused; it
// searches "this window", wraps, jumps to the next error (a transcript tool
// result's is_error plus a console-side pattern, and says it can miss),
// counts what the server masked and draws the mask as ********, and says
// when output fell out of the live tail between two reads. Details carries
// its one newest line, which opens the tab.
//
// MUTATIONS, one per block: drop `attemptId` from the tab's reads or from
// the stdout view's; let a paused log show the newest read; count hits over
// the whole stream; drop the transcript's `is_error` from the error targets;
// draw the mask as plain text; compare a read with itself for the gap; drop
// the last line or its Open logs.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentLogs, LogLastLine, defaultView } = await import('../AgentLogs')

const ID = 'task_0123456789abcdef0123'
const MASK = '*'.repeat(8)

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function attemptBlock(generation: number, ended = false) {
  return {
    status: 'latest' as const,
    known: true,
    generation,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    completed_at: ended ? new Date().toISOString() : null,
    exit_code: ended ? 0 : null,
  }
}

function stream(name: LogStream['stream'], over: Partial<LogStream> = {}): LogStream {
  const content = over.content ?? ''
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
    ...over,
  }
}

function logs(streams: LogStream[], generation = 1, attemptId: string | null = 'att_1'): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: attemptId,
    attempt: attemptBlock(generation),
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${ID}/attempts/${attemptId}/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

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

function transcript(steps: TranscriptStep[], over: Partial<TaskTranscript> = {}): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: attemptBlock(1),
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'live',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/live/agent_stdout.log`,
      object_updated_at: new Date().toISOString(),
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
    complete: false,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
    ...over,
  }
}

function running(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', attempt_count: 2, completed_at: null, ...over })
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

function chooseStream(label: string) {
  const group = within(dock()).getByRole('group', { name: 'Which log' })
  fireEvent.click(within(group).getByRole('button', { name: label }))
}

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' }), attempt(2, { attempt_id: 'att_2' })] }))
  api.loadTranscript.mockResolvedValue(ok(transcript([step(1)])))
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('agent_stderr'), stream('stdout'), stream('stderr')])))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the tab opens on the stream the runner has', () => {
  it('opens on Transcript for an agent CLI, and on Runner for a runner with none', () => {
    expect(defaultView('claude-code')).toBe('transcript')
    expect(defaultView('codex')).toBe('transcript')
    expect(defaultView('browser')).toBe('runner')
    expect(defaultView('mock')).toBe('runner')
  })

  it('offers Transcript, Agent stdout, Agent stderr and Runner', async () => {
    render(<AgentLogs task={running()} />)
    const group = within(dock()).getByRole('group', { name: 'Which log' })
    // By the name a reader hears: a narrow bar draws a short label beside it.
    expect(within(group).getAllByRole('button').map((b) => b.querySelector('.ag-logs-long')?.textContent ?? b.textContent)).toEqual([
      'Transcript',
      'Agent stdout',
      'Agent stderr',
      'Runner',
    ])
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
  })
})

describe('the attempt picker reads the picked attempt, by attempt_id', () => {
  it('labels each attempt by generation and passes its id to every log read', async () => {
    render(<AgentLogs task={running()} />)
    const picker = await waitFor(() => {
      const s = within(dock()).getByRole('combobox', { name: 'Attempt, by generation' }) as HTMLSelectElement
      expect(s.options.length).toBe(3)
      return s
    })
    expect([...picker.options].map((o) => o.textContent)).toEqual(['latest', 'gen 2 · att_2 · exit 0', 'gen 1 · att_1 · exit 0'])
    // The latest first, with no attempt named: what the Artifacts pane always read.
    expect(api.loadTaskLogs.mock.calls[0]![1]).not.toHaveProperty('attemptId')
    fireEvent.change(picker, { target: { value: 'att_1' } })
    await waitFor(() => expect(api.loadTaskLogs.mock.calls.some((c) => c[1]?.attemptId === 'att_1')).toBe(true))
    expect(api.loadTranscript.mock.calls.some((c) => c[1]?.attemptId === 'att_1')).toBe(true)
    // ARTIFACTS.TSX ~251 READ THE LATEST ALWAYS. The stdout view, which reads
    // for itself, follows the picked attempt too.
    chooseStream('Agent stdout')
    await waitFor(() =>
      expect(
        api.loadTaskLogs.mock.calls.some((c) => c[1]?.attemptId === 'att_1' && c[1]?.stream === 'agent_stdout'),
      ).toBe(true),
    )
  })
})

describe('search covers this window, and wrap and the keys work', () => {
  it('counts the hits in this window, walks them, and says so', async () => {
    api.loadTaskLogs.mockResolvedValue(
      ok(logs([stream('agent_stderr', { content: 'Traceback one\nok\ntraceback two\n' }), stream('stdout'), stream('stderr')])),
    )
    render(<AgentLogs task={running()} />)
    chooseStream('Agent stderr')
    await waitFor(() => expect(dock().querySelectorAll('.ag-logline')).toHaveLength(3))
    fireEvent.change(within(dock()).getByRole('searchbox', { name: 'Search this window' }), { target: { value: 'traceback' } })
    await waitFor(() => expect(dock().querySelector('.ag-logs-count .ag-logs-long')?.textContent).toBe('– of 2 · this window'))
    fireEvent.keyDown(within(dock()).getByRole('searchbox', { name: 'Search this window' }), { key: 'Enter' })
    await waitFor(() => expect(dock().querySelector('.ag-logs-count .ag-logs-long')?.textContent).toBe('1 of 2 · this window'))
    expect(dock().querySelector('.ag-logline.is-current')?.textContent).toContain('Traceback one')
    expect(dock().querySelectorAll('.ag-logline mark')).toHaveLength(2)

    const wrap = within(dock()).getByRole('button', { name: /^Wrap/ })
    expect(wrap.getAttribute('aria-pressed')).toBe('false')
    fireEvent.keyDown(window, { key: 'w' })
    expect(wrap.getAttribute('aria-pressed')).toBe('true')
    expect(dock().querySelector('.ag-logtext')?.classList.contains('is-wrap')).toBe(true)

    // A letter typed while a control OUTSIDE the dock holds focus is not the
    // log's. MUTATION: drop the outside-control check from the key handler.
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    try {
      fireEvent.keyDown(outside, { key: 'w' })
      expect(wrap.getAttribute('aria-pressed'), 'a key typed on another control toggled wrap').toBe('true')
    } finally {
      outside.remove()
    }
    fireEvent.keyDown(wrap, { key: 'w' })
    expect(wrap.getAttribute('aria-pressed')).toBe('false')
  })
})

describe('jump to error: a transcript is_error and a console-side pattern', () => {
  it('counts a failed tool result and a Traceback line, and says what it matches and that it can miss', async () => {
    api.loadTranscript.mockResolvedValue(
      ok(
        transcript([
          step(1, { text: 'reading the file' }),
          step(2, { kind: 'tool_call', tool: { id: 'toolu_1', name: 'Read', input: '{}' } }),
          step(3, { kind: 'tool_result', role: 'user', text: null, tool_result: { tool_use_id: 'toolu_1', is_error: true, content: 'ENOENT', images: 0 } }),
          step(4, { text: 'Traceback (most recent call last):' }),
          step(5, { text: 'all good' }),
        ]),
      ),
    )
    render(<AgentLogs task={running()} />)
    const button = await waitFor(() => {
      const b = within(dock()).getByRole('button', { name: /^Error/ })
      expect(b.textContent).toContain('of 2')
      return b
    })
    expect(button.getAttribute('title')).toMatch(/is_error/)
    expect(button.getAttribute('title')).toMatch(/can miss/)
    fireEvent.keyDown(window, { key: 'e' })
    await waitFor(() => expect(button.textContent).toContain('Error 1 of 2'))
    expect(dock().querySelector('.arts-step.is-current')?.textContent).toContain('Read')
    fireEvent.click(button)
    await waitFor(() => expect(button.textContent).toContain('Error 2 of 2'))
    expect(dock().querySelector('.arts-step.is-current')?.textContent).toContain('Traceback')
  })
})

describe('the masked count is the server’s, and a masked value is ********', () => {
  it('counts the window’s masks and draws each in its own ink', async () => {
    api.loadTaskLogs.mockResolvedValue(
      ok(
        logs([
          stream('agent_stderr', { content: `token=${MASK} sent\n`, redacted: true, redaction_count: 1 }),
          stream('stdout'),
          stream('stderr'),
        ]),
      ),
    )
    render(<AgentLogs task={running()} />)
    chooseStream('Agent stderr')
    await waitFor(() => expect(dock().querySelector('.ag-mask')?.textContent).toBe(MASK))
    const facts = dock().querySelector('.ag-logs-facts')!
    expect(facts.textContent).toContain('1 in this window')
    expect(facts.textContent).toMatch(/masking\s*at read time/)
  })
})

describe('follow and pause', () => {
  it('pauses on Paused, keeps the paused read on screen, and counts the lines that arrived since', async () => {
    vi.useFakeTimers({ toFake: ['setInterval'] })
    const tail = (objectOffset: number, content: string): LogStream =>
      stream('agent_stderr', {
        source: 'live',
        content,
        offset: 30,
        returned_bytes: content.length,
        total_bytes: 30 + content.length,
        tail_window: { object_offset: objectOffset, stream_size: objectOffset + content.length, published_at: new Date().toISOString() },
      })
    api.loadTaskLogs.mockResolvedValue(ok(logs([tail(0, 'a\nb\n'), stream('stdout'), stream('stderr')])))
    render(<AgentLogs task={running()} />)
    chooseStream('Agent stderr')
    await waitFor(() => expect(dock().querySelectorAll('.ag-logline')).toHaveLength(2))
    const follow = within(dock()).getByRole('button', { name: /^Following/ })
    expect(follow.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(follow)
    expect(within(dock()).getByRole('button', { name: /^Paused/ }).getAttribute('aria-pressed')).toBe('false')

    api.loadTaskLogs.mockResolvedValue(ok(logs([tail(0, 'a\nb\nc\nd\ne\n'), stream('stdout'), stream('stderr')])))
    await act(async () => {
      vi.advanceTimersByTime(5_000)
    })
    const pill = await waitFor(() => {
      const p = dock().querySelector('.ag-logs-arrived')
      expect(p).not.toBeNull()
      return p!
    })
    expect(pill.textContent).toBe('↓ 3 new lines since you paused · Follow')
    // What is on screen is still the read the reader paused on.
    expect(dock().querySelectorAll('.ag-logline')).toHaveLength(2)
    fireEvent.click(pill)
    await waitFor(() => expect(dock().querySelectorAll('.ag-logline')).toHaveLength(5))
    expect(dock().querySelector('.ag-logs-arrived')).toBeNull()
  })
})

describe('output missing between two reads', () => {
  it('draws an amber notice with both bytes when the tail moved past output nobody read', async () => {
    vi.useFakeTimers({ toFake: ['setInterval'] })
    const tail = (objectOffset: number, content: string, at: string): LogStream =>
      stream('agent_stderr', {
        source: 'live',
        content,
        offset: 30,
        returned_bytes: content.length,
        total_bytes: 30 + content.length,
        tail_window: { object_offset: objectOffset, stream_size: objectOffset + content.length, published_at: at },
      })
    api.loadTaskLogs.mockResolvedValue(ok(logs([tail(1_000_000, 'x'.repeat(100), '2026-10-02T14:02:11Z'), stream('stdout'), stream('stderr')])))
    render(<AgentLogs task={running()} />)
    chooseStream('Agent stderr')
    await waitFor(() => expect(dock().querySelectorAll('.ag-logline')).toHaveLength(1))
    expect(dock().querySelector('.ag-gap'), 'a gap drawn off one read').toBeNull()
    api.loadTaskLogs.mockResolvedValue(ok(logs([tail(1_318_400, 'y'.repeat(100), '2026-10-02T14:02:16Z'), stream('stdout'), stream('stderr')])))
    await act(async () => {
      vi.advanceTimersByTime(5_000)
    })
    const gap = await waitFor(() => {
      const g = dock().querySelector('.ag-gap')
      expect(g).not.toBeNull()
      return g!
    })
    expect(gap.textContent).toContain('Output missing between byte 1,000,100 and 1,318,400')
    expect(gap.getAttribute('role')).toBe('note')
  })

  // With the picker on "latest", the next read can be a NEW attempt's. Its
  // first tail is not the continuation of the old attempt's, so comparing the
  // two would draw a gap that is only a new attempt. MUTATION: key the
  // comparison on the pick ('latest') instead of the attempt the read returned.
  it('draws no gap when the latest read is a new attempt', async () => {
    vi.useFakeTimers({ toFake: ['setInterval'] })
    const tail = (objectOffset: number, content: string, at: string): LogStream =>
      stream('agent_stderr', {
        source: 'live',
        content,
        offset: 30,
        returned_bytes: content.length,
        total_bytes: 30 + content.length,
        tail_window: { object_offset: objectOffset, stream_size: objectOffset + content.length, published_at: at },
      })
    api.loadTaskLogs.mockResolvedValue(ok(logs([tail(1_000_000, 'x'.repeat(100), '2026-10-02T14:02:11Z'), stream('stdout'), stream('stderr')])))
    render(<AgentLogs task={running()} />)
    chooseStream('Agent stderr')
    await waitFor(() => expect(dock().querySelectorAll('.ag-logline')).toHaveLength(1))
    api.loadTaskLogs.mockResolvedValue(
      ok(logs([tail(1_318_400, 'y'.repeat(100), '2026-10-02T14:02:16Z'), stream('stdout'), stream('stderr')], 2, 'att_2')),
    )
    await act(async () => {
      vi.advanceTimersByTime(5_000)
    })
    await waitFor(() => expect(dock().textContent).toContain('y'.repeat(100)))
    expect(dock().querySelector('.ag-gap'), 'a new attempt drawn as missing output').toBeNull()
  })
})

describe('Details carries the newest log line, and it opens the Logs tab', () => {
  it('draws the newest line of the default stream and switches to the tab on a click', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1, { text: 'first' }), step(2, { text: 'the newest line' })])))
    const open = vi.fn()
    render(<LogLastLine task={running()} onOpen={open} />)
    const strip = screen.getByRole('button', { name: /^Last log line/ })
    await waitFor(() => expect(strip.textContent).toContain('the newest line'))
    expect(strip.textContent).toContain('Open logs')
    fireEvent.click(strip)
    expect(open).toHaveBeenCalledTimes(1)
    // A line, never an overlay: nothing it draws is a dialog or is fixed.
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says a read in flight is reading, never that the log is empty', () => {
    api.loadTranscript.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    render(<LogLastLine task={running()} onOpen={() => {}} />)
    expect(screen.getByRole('button', { name: /^Last log line/ }).textContent).toContain('reading…')
  })
})

describe('honest forms', () => {
  it('says a read in flight is reading, never an empty log', () => {
    api.loadTranscript.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    render(<AgentLogs task={running()} />)
    expect(dock().querySelector('.art-loading')).not.toBeNull()
    expect(dock().querySelector('.ag-logline')).toBeNull()
  })

  it('says the attempt list did not load, and still reads the latest', async () => {
    api.loadAttempts.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'down' } })
    render(<AgentLogs task={running()} />)
    await waitFor(() => expect(dock().textContent).toContain('attempts not read'))
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
  })
})

