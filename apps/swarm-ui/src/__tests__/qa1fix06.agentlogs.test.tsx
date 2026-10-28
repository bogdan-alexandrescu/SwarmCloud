/**
 * QA1-FIX-06 (part of #2596, the console visual QA pass 1 epic, 2026-10-28):
 * the Logs tab and the agent split, as the fixture console's sweep found them.
 *
 *   overview-agents-32  the error control said `0 err ln` while the log was
 *                       still being read, then `Error 1/1`: no count before the read.
 *   overview-agents-34  with Follow on, a scroll up was swallowed as the log's
 *                       own and the next read pinned the view to the bottom.
 *   overview-agents-35  one selected style across the stream buttons: every
 *                       one carries the same `aria-pressed` the one rule paints.
 *   overview-agents-40  `Last log line (not the error)` over the error itself.
 *   overview-agents-48  Escape on an open checkpoint `files` closed the agent.
 *
 * MUTATIONS are named at each assertion.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
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

const { AgentLogs, LogLastLine, sameAsError } = await import('../AgentLogs')
const { AgentSplit } = await import('../AgentSplit')

const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function attemptBlock() {
  return {
    status: 'latest' as const,
    known: true,
    generation: 1,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    completed_at: null,
    exit_code: null,
  }
}

function stream(name: LogStream['stream'], content = ''): LogStream {
  return {
    stream: name,
    source: 'live',
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

function logs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: attemptBlock(),
    read_at: new Date().toISOString(),
    streams: [stream('agent_stderr'), stream('stdout'), stream('stderr')],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
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

function transcript(steps: TranscriptStep[]): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: attemptBlock(),
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
  }
}

function running(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code', attempt_count: 1, completed_at: null, ...over })
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

/** The log's body laid out 200px tall over 2000px, its `scrollTop` kept and its scroll event fired (jsdom does neither). */
function bodyLaidOut(): () => void {
  const proto = HTMLElement.prototype
  const top = new WeakMap<Element, number>()
  const isBody = (el: HTMLElement) => el.classList.contains('ag-logs-body')
  Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: (function (this: HTMLElement) { return isBody(this) ? 2000 : 0 }) })
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get: (function (this: HTMLElement) { return isBody(this) ? 200 : 0 }) })
  Object.defineProperty(proto, 'scrollTop', {
    configurable: true,
    get(this: HTMLElement) {
      return top.get(this) ?? 0
    },
    set(this: HTMLElement, v: number) {
      const was = top.get(this) ?? 0
      const next = Math.max(0, Math.min(v, isBody(this) ? 1800 : 0))
      top.set(this, next)
      // A browser fires one scroll event for a position that moved, and none for one that did not.
      if (next !== was) queueMicrotask(() => this.dispatchEvent(new Event('scroll')))
    },
  })
  return () => {
    for (const k of ['scrollHeight', 'clientHeight', 'scrollTop']) delete (proto as unknown as Record<string, unknown>)[k]
  }
}

let undo: (() => void) | null = null

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1', task_id: ID })] }))
  api.loadTranscript.mockResolvedValue(ok(transcript([step(1)])))
  api.loadTaskLogs.mockResolvedValue(ok(logs()))
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  undo?.()
  undo = null
  vi.useRealTimers()
})

describe('overview-agents-32: the error control draws no count before the log is read', () => {
  it('says `Errors`, with no figure, while the read is in flight, and counts once it lands', async () => {
    let answer: (r: Result<TaskTranscript>) => void = () => {}
    api.loadTranscript.mockReturnValue(new Promise<Result<TaskTranscript>>((r) => (answer = r)))
    render(<AgentLogs task={running({ state: 'RUNNING' })} />)
    const err = () => dock().querySelector<HTMLButtonElement>('button[aria-keyshortcuts="E"]')!
    // MUTATION: drop `errCounted` from `errWords` -- the bar says `0 err ln` / `No error matched` before anything was counted.
    expect(err().querySelector('.ag-logs-short')?.textContent).toBe('Errors')
    expect(err().querySelector('.ag-logs-long')?.textContent).toBe('Errors · reading')
    expect(err().textContent).not.toMatch(/\d/)
    expect(err().disabled).toBe(true)
    expect(err().title).toMatch(/^Not counted yet/)
    await act(async () => answer(ok(transcript([step(1)]))))
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
    // Read, a zero is a measured zero again.
    expect(err().querySelector('.ag-logs-long')?.textContent).toBe('No error matched')
    expect(err().title).not.toMatch(/^Not counted yet/)
  })
})

describe('overview-agents-34: a scroll up pauses Follow, and the next read leaves the reader where they are', () => {
  it('turns Follow off on the first scroll away from the bottom, after renders that moved nothing', async () => {
    vi.useFakeTimers({ toFake: ['setInterval'] })
    undo = bodyLaidOut()
    render(<AgentLogs task={running()} />)
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
    const body = dock().querySelector<HTMLElement>('.ag-logs-body')!
    // Following, the body is at its end; a 5 s read that moves nothing re-renders the tab.
    expect(body.scrollTop).toBe(1800)
    await act(async () => {
      vi.advanceTimersByTime(5_000)
    })
    await landed(api.loadTranscript, 2)
    expect(within(dock()).getByRole('button', { name: /^Following/ }).getAttribute('aria-pressed')).toBe('true')
    // The reader scrolls to the top: one scroll event, as a Home key or a scrollbar drag gives.
    await act(async () => {
      body.scrollTop = 0
    })
    // MUTATION: put back `autoScroll.current = true` unconditionally in the follow effect -- the
    // reader's scroll is swallowed as the log's own and Follow stays on.
    expect(within(dock()).getByRole('button', { name: /^Paused/ }).getAttribute('aria-pressed')).toBe('false')
    await act(async () => {
      vi.advanceTimersByTime(5_000)
    })
    await landed(api.loadTranscript, 3)
    expect(body.scrollTop, 'the next read pinned the view back to the bottom').toBe(0)
  })
})

describe('overview-agents-35: every stream button is selected the same way', () => {
  it('moves one `aria-pressed` across the four, so the one `.c-seg` rule paints each', async () => {
    render(<AgentLogs task={running()} />)
    const group = within(dock()).getByRole('group', { name: 'Which log' })
    const buttons = within(group).getAllByRole('button')
    for (const b of buttons) {
      fireEvent.click(b)
      // MUTATION: give one stream its own selected marker (a class, no aria-pressed) -- two selected styles in one control.
      expect(buttons.map((x) => x.getAttribute('aria-pressed'))).toEqual(buttons.map((x) => (x === b ? 'true' : 'false')))
      expect(b.className, 'a stream button carries a class of its own').toBe('')
    }
    await waitFor(() => expect(api.loadTaskLogs).toHaveBeenCalled())
  })

  // THE RENDER FOUND THE SECOND STYLE (QA1-FIX-06 re-review, 2026-10-28): the
  // stream just clicked is still under the pointer, and `.c-seg >
  // button:hover:not(:disabled)` (0,3,1) outranks the pressed rule (0,2,1),
  // so stderr selected read as bold body text while Transcript selected read
  // blue. jsdom paints no :hover, so this reads the two stylesheets.
  it('keeps the pressed accent colour on a hovered stream button', () => {
    const css = (f: string) => readFileSync(join(__dirname, '..', 'styles', f), 'utf8')
    expect(css('components.css'), 'the hover rule this guards against moved').toMatch(/\.c-seg > button:hover:not\(:disabled\) \{ color: var\(--text\); \}/)
    // MUTATION: delete the `.ag-logs-streams.c-seg > button[aria-pressed='true']:hover` rule from agents.css -- the hovered selected stream draws body text again.
    expect(css('agents.css')).toMatch(
      /\.ag-logs-streams\.c-seg > button\[aria-pressed='true'\]:hover:not\(:disabled\) \{ color: var\(--sk-ac\); \}/,
    )
  })
})

describe('overview-agents-40: the last log line of a failed agent says when it IS the error', () => {
  const ERROR = 'exit status 2: pytest was interrupted by a collection error'
  const label = () => screen.getByRole('button', { name: /^Last log line/ }).querySelector('b')!.textContent

  it('labels the line `(the error, as logged)` when it carries the error', async () => {
    api.loadTranscript.mockResolvedValue(
      ok(transcript([step(1), step(2, { kind: 'result', text: ERROR, meta: { subtype: 'error_during_execution', is_error: true } })])),
    )
    render(<LogLastLine task={running({ state: 'FAILED', completed_at: '2026-10-28T09:10:00Z', last_error: ERROR })} onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Last log line/ }).textContent).toContain('pytest was interrupted'))
    // MUTATION: drop `isTheError` from the label -- it says `(not the error)` over the error word for word.
    expect(label()).toBe('Last log line (the error, as logged)')
  })

  it('keeps `(not the error)` when the line is something else', async () => {
    api.loadTranscript.mockResolvedValue(ok(transcript([step(1, { text: 'result ## Summary' })])))
    render(<LogLastLine task={running({ state: 'FAILED', completed_at: '2026-10-28T09:10:00Z', last_error: ERROR })} onOpen={() => {}} />)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Last log line/ }).textContent).toContain('Summary'))
    // MUTATION: make `sameAsError` answer true for any line -- every failed line is called the error.
    expect(label()).toBe('Last log line (not the error)')
  })

  it('matches on the error’s first line, whitespace folded, and never on no error', () => {
    expect(sameAsError('result exit status 2:  pytest was interrupted', 'exit status 2: pytest was interrupted\nTraceback …')).toBe(true)
    expect(sameAsError('text step 1', 'exit status 2')).toBe(false)
    // MUTATION: drop the empty check -- an error of blanks is in every line.
    expect(sameAsError('text step 1', '   \n')).toBe(false)
    expect(sameAsError('text step 1', null)).toBe(false)
  })
})

describe('overview-agents-48: Escape folds the innermost open thing before it closes the agent', () => {
  type Pane = 'detail' | 'checkpoints'
  function Routed({ onGo }: { onGo: (to: string) => void }) {
    const [pane, setPane] = useState<Pane>('checkpoints')
    const go = (to: string) => {
      onGo(to)
      setPane(to.endsWith('/checkpoints') ? 'checkpoints' : 'detail')
    }
    return (
      <div className="app has-inspector">
        <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
      </div>
    )
  }

  async function opened(go: (to: string) => void): Promise<HTMLElement> {
    const t = running()
    api.loadTask.mockResolvedValue(ok(t))
    render(<Routed onGo={go} />)
    await landed(api.loadTask)
    return document.querySelector<HTMLElement>('.ag-split')!
  }

  it('folds an open disclosure the key was pressed on, keeps the agent, and the next Escape closes it', async () => {
    const go = vi.fn()
    const split = await opened(go)
    // A checkpoint's `files` toggle, as RunFiles draws it: a button whose aria-expanded says it is open.
    const files = document.createElement('button')
    files.type = 'button'
    files.textContent = 'files'
    files.setAttribute('aria-expanded', 'true')
    files.addEventListener('click', () => files.setAttribute('aria-expanded', files.getAttribute('aria-expanded') === 'true' ? 'false' : 'true'))
    split.querySelector('.ag-ckpts-host')!.appendChild(files)
    files.focus()
    fireEvent.keyDown(files, { key: 'Escape' })
    // MUTATION: drop `foldInner` from the split's Escape -- the agent closes with the files open.
    expect(go, 'Escape on an open panel closed the agent').not.toHaveBeenCalled()
    expect(files.getAttribute('aria-expanded')).toBe('false')
    fireEvent.keyDown(files, { key: 'Escape' })
    expect(go).toHaveBeenCalledWith('work/running/live')
  })

  it('closes an open details around the key, focus back on its summary', async () => {
    const go = vi.fn()
    const split = await opened(go)
    const d = document.createElement('details')
    d.open = true
    d.innerHTML = '<summary>more</summary><button type="button">inside</button>'
    split.querySelector('.ag-ckpts-host')!.appendChild(d)
    const inside = d.querySelector('button')!
    inside.focus()
    fireEvent.keyDown(inside, { key: 'Escape' })
    // MUTATION: drop the `details[open]` branch of `foldInner` -- the agent closes over an open section.
    expect(go).not.toHaveBeenCalled()
    expect(d.open).toBe(false)
    expect(document.activeElement).toBe(d.querySelector('summary'))
  })
})

describe('overview-agents-38: a phone opens the log wrapped', () => {
  function widthIs(phone: boolean): () => void {
    const had = Object.getOwnPropertyDescriptor(window, 'matchMedia')
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (q: string) => ({
        matches: phone && q === '(max-width: 560px)',
        media: q,
        addEventListener: () => {},
        removeEventListener: () => {},
      }),
    })
    return () => {
      if (had === undefined) delete (window as unknown as Record<string, unknown>).matchMedia
      else Object.defineProperty(window, 'matchMedia', had)
    }
  }
  const wrap = () => dock().querySelector<HTMLButtonElement>('button[aria-keyshortcuts="W"]')!

  it('turns Wrap on at a phone’s width, and leaves it off wider', async () => {
    undo = widthIs(true)
    const { unmount } = render(<AgentLogs task={running()} />)
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
    // MUTATION: start `wrap` at false again -- every raw stdout line needs a sideways scroll at 390.
    expect(wrap().getAttribute('aria-pressed')).toBe('true')
    expect(dock().querySelector('.ag-logs-body')!.classList.contains('is-wrap')).toBe(true)
    // Still the reader's to turn off.
    fireEvent.click(wrap())
    expect(wrap().getAttribute('aria-pressed')).toBe('false')
    unmount()
    undo()
    undo = widthIs(false)
    render(<AgentLogs task={running()} />)
    await waitFor(() => expect(dock().textContent).toContain('step 1'))
    expect(wrap().getAttribute('aria-pressed')).toBe('false')
  })
  // THE RENDER FOUND THE ORPHANED NUMBER (QA1-FIX-06 re-review, 2026-10-28):
  // wrapped at 390, a stream-json line has no space to break at, so the break
  // after the inline-block `.ag-logno` won and `1` sat alone on a row above
  // its JSON. jsdom lays nothing out, so this reads the stylesheet.
  it('hangs the line number in a gutter when wrapped, so it never sits alone on a row', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'agents.css'), 'utf8')
    // MUTATION: drop the float -- the number is inline again and `1` sits alone above its line at 390.
    expect(css).toMatch(/\.ag-logs-body\.is-wrap \.ag-logtext \.ag-logno \{ float: inline-start; margin-inline-start: -5ch; \}/)
    // MUTATION: drop the padding -- the floated number hangs outside the log box.
    expect(css).toMatch(/\.ag-logs-body\.is-wrap \.ag-logtext \.ag-logline \{ padding-inline-start: 5ch; \}/)
  })
})

describe('overview-agents-36: an empty stderr says so in a sentence', () => {
  it('reads `Nothing on agent stderr yet.` over a folded object card', async () => {
    render(<AgentLogs task={running()} />)
    fireEvent.click(within(within(dock()).getByRole('group', { name: 'Which log' })).getByRole('button', { name: 'Agent stderr' }))
    await waitFor(() => expect(dock().querySelector('.rf-empty')).not.toBeNull())
    // MUTATION: drop the empty branch in `StreamsFrom` -- the gs:// card `0 B real zero · live` is all there is.
    expect(dock().querySelector('.rf-empty')!.textContent).toBe('Nothing on agent stderr yet.')
    expect(dock().querySelector('details.ag-logmeta')?.hasAttribute('open')).toBe(false)
  })

  it('stands clear of the menu rule above it', () => {
    // The re-review's render: the body has no top padding, so the sentence sat on the rule.
    const css = readFileSync(join(__dirname, '..', 'styles', 'agents.css'), 'utf8')
    // MUTATION: delete the `.ag-logs-body .rf-empty` rule from agents.css -- the sentence touches the rule again.
    expect(css).toMatch(/\.ag-logs-body \.rf-empty \{ margin-top: var\(--ctl-s2\); \}/)
  })
})
