/**
 * AN AGENT'S DETAILS AND LOGS PANES, VISUAL QA RUN 4 (QAFIX4-AGENT-PANES,
 * part of #1680; workflow wf_ecd935541ced4a539540, the fixture build of main
 * at fabe4d6c).
 *
 * N12 (task-008): the Details pane's card titles were a bold `<b>`, so the
 * accessibility tree read `- text: Now` and a screen-reader user could not
 * jump between Now, Progress, Resources and Input, while Attempts and
 * Children are `h2`. They are `h2` now, drawn exactly as the `<b>` was.
 *
 * N13, the chip part (flow-find-failure-desktop-D6): a failed agent's Logs
 * bar said `Final record` and only a hover said what that meant. The chip now
 * says the agent ended and the log is complete, in a short label that fits
 * the 380px split (qa.u11a.logs holds short labels to 8 characters). The
 * error button's `0 err ln` stays by the owner's decision (c39, 2026-10-22).
 *
 * N14 (flow-find-failure-desktop-D7): the paused stamp was
 * `toLocaleTimeString()` -- `9:25:53 PM` beside a head reading `20:54:46`.
 * It is the page's one clock (`clockTime`) now, and says what paused means.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs, TaskTranscript } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')
const { AgentLogs } = await import('../AgentLogs')

const ID = 'task_0123456789abcdef0123'
const WIDE: CascadeEnv = { width: 1440 }
const MIN = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'mock',
    created_at: ago(15),
    started_at: ago(14),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
    ...over,
  })
}

function failed(): Task {
  return running({ state: 'FAILED', completed_at: ago(1), end_cause: 'worker_lost' } as Partial<Task>)
}

const CLASSES = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

function runOf(task: Task): AgentRun {
  return {
    task,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: task.completed_at ?? null, exit_code: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

const hosts: HTMLElement[] = []
function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

function stream(name: LogStream['stream'], over: Partial<LogStream> = {}): LogStream {
  const content = over.content ?? 'a\nb\n'
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
    ...over,
  }
}

function logs(streams: LogStream[]): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(1), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  } as TaskLogs
}

function transcript(): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: ago(1), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'live',
      status: 'ok',
      detail: null,
      uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/att_1/live/agent_stdout.log`,
      object_updated_at: new Date().toISOString(),
      age_seconds: 3,
      total_bytes: 0,
      offset: 0,
      returned_bytes: 0,
      next_offset: null,
      truncated: false,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps: [],
    complete: false,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as TaskTranscript
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTranscript.mockResolvedValue(ok(transcript()))
  api.loadTaskLogs.mockResolvedValue(ok(logs([stream('agent_stderr'), stream('stdout'), stream('stderr')])))
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

describe('N12: every Details card title is a heading, drawn as the bold title was', () => {
  it('heads Now, Progress, Against each limit and Input with an h2 each', () => {
    const el = host(runOf(running()))
    for (const name of ['Now', 'Progress', 'Against each limit', 'Input']) {
      // MUTATION: draw DtCardHead's title in a `<b>` again (AgentDetail.tsx) -- no heading by that name.
      const h = within(el).getByRole('heading', { name, level: 2 })
      // MUTATION: draw the title as an `h3` -- the level is not the other panes' 2.
      expect(h.tagName, name).toBe('H2')
      expect(h.parentElement?.classList.contains('dt-card-head'), `${name} is in its card head`).toBe(true)
    }
  })

  it('heads a failed agent’s leading card with an h2 too', () => {
    const el = host(runOf(failed()))
    // MUTATION: give DtFailure's head its own `<b>` instead of DtCardHead -- no heading.
    const h = within(el).getByRole('heading', { name: /^Failed/, level: 2 })
    expect(h.closest('.dt-now')).not.toBeNull()
  })

  it('draws each heading at the size, weight and line height `.dt-card-head > b` gave, with no margin', () => {
    const el = host(runOf(running()))
    for (const name of ['Now', 'Progress', 'Against each limit', 'Input']) {
      const h = within(el).getByRole('heading', { name, level: 2 })
      // MUTATION: leave styles/details.css's rule on `.dt-card-head > b` only -- the h2 takes no size.
      expect(painted(h, ['font-size', 'font'], WIDE), name).toBe('var(--t-body)')
      // MUTATION: drop `font-weight: 650` from the heading's rule -- the h2 is the browser's bold, not the title's 650.
      expect(painted(h, ['font-weight', 'font'], WIDE), name).toBe('650')
      // MUTATION: drop `line-height: var(--lh-body)` from the heading's rule.
      expect(painted(h, ['line-height', 'font'], WIDE), name).toBe('var(--lh-body)')
      // MUTATION: drop `margin: 0` -- the h2 keeps the browser's margins and the card head grows.
      expect(painted(h, ['margin-top', 'margin'], WIDE), name).toBe('0')
      expect(painted(h, ['margin-bottom', 'margin'], WIDE), name).toBe('0')
    }
  })

  it('paints a failed agent’s heading in the bad hue, as the bold title was', () => {
    const el = host(runOf(failed()))
    const h = within(el).getByRole('heading', { name: /^Failed/, level: 2 })
    // MUTATION: leave `.dt-now.is-bad > .dt-card-head > b` on the `b` only -- the failed title loses its red.
    expect(painted(h, 'color', WIDE)).toBe('var(--s-bad)')
  })
})

describe('N13, the chip: a finished agent’s Logs bar says its log is complete', () => {
  function finished(): Task {
    return running({ state: 'FAILED', completed_at: ago(60) })
  }

  it('names the chip in words, without a hover: the agent ended, this is its whole log, nothing to follow', () => {
    render(<AgentLogs task={finished()} />)
    // MUTATION: drop the chip's `aria-label` (or its `role="note"`) in AgentLogs.tsx -- the chip has no name.
    const chip = within(dock()).getByRole('note', { name: /ended/i })
    // MUTATION: name it `Final record` again -- it does not say the log is complete.
    expect(chip.getAttribute('aria-label')).toMatch(/complete log/i)
    expect(chip.getAttribute('aria-label')).toMatch(/nothing (more )?to follow/i)
    // The hover sentence stays.
    expect(chip.getAttribute('title')).toMatch(/nothing more will arrive/)
  })

  it('says it on the glass, long and short, the short in 8 characters or fewer', () => {
    render(<AgentLogs task={finished()} />)
    const chip = dock().querySelector<HTMLElement>('.ag-logs-final')!
    const long = chip.querySelector('.ag-logs-long')?.textContent ?? ''
    const short = chip.querySelector('.ag-logs-short')?.textContent ?? ''
    // MUTATION: print the chip's text bare again instead of through LogWords -- no long/short pair.
    expect(long).toMatch(/ended/i)
    expect(long).toMatch(/complete/i)
    expect(short.length).toBeGreaterThan(0)
    // MUTATION: give the short label a ninth character -- it spills Error under More at the 380px split.
    expect(short.length).toBeLessThanOrEqual(8)
    expect(chip.textContent).not.toMatch(/Final record/)
  })
})

describe('N14: the paused stamp is the page’s one clock, and says what paused means', () => {
  it('reads HH:MM:SS with no AM or PM, the ISO time as its title, and the words for paused', async () => {
    render(<AgentLogs task={running()} />)
    fireEvent.click(within(dock()).getByRole('group', { name: 'Which log' }).querySelector('button')!)
    await waitFor(() => expect(dock().querySelector('.ag-logs-facts')?.textContent).toMatch(/source/))
    fireEvent.click(within(dock()).getByRole('button', { name: /^Following/ }))
    const stamp = await waitFor(() => {
      const s = dock().querySelector<HTMLElement>('.ag-logs-paused time')
      expect(s).not.toBeNull()
      return s!
    })
    // jsdom's default locale draws `toLocaleTimeString()` on a 12-hour clock.
    // MUTATION: print `new Date(frozen.at).toLocaleTimeString()` again (AgentLogs.tsx) -- `9:25:53 PM`.
    expect(stamp.textContent).toMatch(/^\d{2}:\d{2}:\d{2}$/)
    expect(stamp.textContent).not.toMatch(/AM|PM/i)
    // MUTATION: drop the title -- the hover no longer carries the ISO time and its age.
    expect(stamp.getAttribute('title')).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/)
    // MUTATION: print `paused` bare again -- nothing says following stopped or where the rest went.
    const line = dock().querySelector('.ag-logs-paused')!.textContent ?? ''
    expect(line).toMatch(/following paused at/i)
    expect(line).toMatch(/counted below/i)
  })
})
