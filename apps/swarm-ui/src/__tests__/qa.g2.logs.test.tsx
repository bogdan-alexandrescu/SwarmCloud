/**
 * THE LOGS TAB, FROM THE QA PASS ON DEPLOYED DEV (2026-10-07, group G2).
 *
 *   G2-07  Runner view: `stdout 0 B · real zero` beside an `Agent stdout`
 *          sub-tab and a 152 KiB `claude-code.stdout.log` -- three things
 *          called stdout -- and the `real zero` chip floating mid-row, away
 *          from its `0 B`.
 *   G2-15  The search placeholder was cut to `Search this wind`.
 *   G2-16  More's attempt row said the id twice
 *          (`attempt attempt att_8f8e… · att_8f8e…`), the second running past
 *          the popover, and the attempt select was cut.
 *
 * MUTATIONS: print `stream.stream` in the row header again; take `0 B` out
 * of `.rf-zero`; put the placeholder back to `Search this window`; put the
 * `picked.attempt_id` span back after `attemptLine(logs)`; set the menu's
 * max-width back to `min(420px, 80vw)`. Each turns a case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { LogStream, Task, TaskLogs } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
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

const { AgentLogs } = await import('../AgentLogs')

const ID = 'task_0123456789abcdef0123'
/** A long attempt id, the shape dev printed twice. */
const ATT = 'att_8f8ecaa6e5734df6a2ee'
const SPLIT: CascadeEnv = { width: 1440, container: 630 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function stream(name: LogStream['stream'], content: string): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/eng/tasks/${ID}/attempts/${ATT}/logs/${name}.log`,
    uri: `gs://swarm-artifacts/tenants/eng/tasks/${ID}/attempts/${ATT}/logs/${name}.log`,
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
    attempt_id: ATT,
    attempt: {
      status: 'latest',
      known: true,
      generation: 1,
      created_at: new Date(Date.now() - 60_000).toISOString(),
      completed_at: new Date().toISOString(),
      exit_code: 0,
    },
    read_at: new Date().toISOString(),
    streams,
    prefix: `tenants/eng/tasks/${ID}/attempts/${ATT}/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  } as TaskLogs
}

function finished(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'SUCCEEDED', runner_profile: 'claude-code', attempt_count: 1, current_generation: 1, ...over })
}

function region(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: ATT, generation: 1 })] }))
  api.loadTranscript.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue(
    ok(logs([stream('agent_stderr', 'warn\n'), stream('agent_stdout', 'out\n'), stream('stdout', ''), stream('stderr', 'started\n')])),
  )
})

describe('G2-07: the runner’s streams are named the runner’s, and a zero keeps its mark beside it', () => {
  it('heads the Runner rows `runner stdout` / `runner stderr`, against the `Agent stdout` sub-tab', async () => {
    render(<AgentLogs task={finished()} />)
    const group = within(region()).getByRole('group', { name: 'Which log' })
    fireEvent.click(within(group).getByRole('button', { name: 'Runner' }))
    const heads = await waitFor(() => {
      const hs = within(region()).getAllByRole('rowheader')
      expect(hs.length).toBe(2)
      return hs
    })
    const names = heads.map((h) => h.querySelector('.mono')?.textContent)
    expect(names).toEqual(['runner stdout', 'runner stderr'])
    // The agent's sub-tab keeps its own word, so the two never share one.
    expect(within(group).getByRole('button', { name: 'Agent stdout' })).toBeTruthy()
  })

  it('draws `0 B` and its `real zero` mark as one item of the Size cell', async () => {
    render(<AgentLogs task={finished()} />)
    fireEvent.click(within(within(region()).getByRole('group', { name: 'Which log' })).getByRole('button', { name: 'Runner' }))
    const row = await waitFor(() => {
      const h = within(region()).getAllByRole('rowheader').find((x) => x.textContent?.startsWith('runner stdout'))
      expect(h).toBeTruthy()
      return h!.closest('tr')!
    })
    const size = row.querySelector('td[data-label="Size"]')!
    const zero = size.querySelector(':scope > .rf-zero')
    expect(zero, 'the zero and its mark are separate items of the cell').not.toBeNull()
    expect(zero!.textContent).toMatch(/^0 B/)
    expect(zero!.querySelector('[data-mark="zero"], .ctl-mark')).not.toBeNull()
    // Nothing of the value is left loose beside it.
    expect([...size.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== '')).toHaveLength(0)
    expect(painted(zero!, 'white-space', SPLIT)).toBe('nowrap')
  })
})

describe('G2-15: the search keeps its placeholder', () => {
  it('says `Search…`, and keeps the whole phrase as its name and its title', () => {
    render(<AgentLogs task={finished()} />)
    const box = within(region()).getByRole('searchbox', { name: 'Search this window' })
    expect(box.getAttribute('placeholder')).toBe('Search…')
    expect(box.getAttribute('title')).toMatch(/^Search this window/)
  })
})

describe('G2-16: More names the attempt once, inside the popover', () => {
  it('reads `attempt 1 · gen 1 · att_8f8ecaa6…` with a copy of the whole id', async () => {
    render(<AgentLogs task={finished()} />)
    const facts = await waitFor(() => {
      const f = region().querySelector<HTMLElement>('.ag-logs-facts')
      expect(f?.textContent).toMatch(/attempt/)
      expect(f?.querySelector('.ag-logs-attfact')).toBeTruthy()
      return f!
    })
    const fact = [...facts.querySelectorAll('li')].find((li) => li.querySelector('b')?.textContent === 'attempt')!
    const text = (fact.textContent ?? '').replace(/\s+/g, ' ').trim()
    expect(text).toMatch(/^attempt ?1 · gen 1 · att_8f8ecaa6… copy$/)
    // The whole id is its title and its copy, never printed twice.
    expect(text.split(ATT.slice(0, 12)).length - 1).toBe(1)
    expect(fact.querySelector(`[title="${ATT}"]`)).not.toBeNull()
    expect(within(fact).getByRole('button', { name: `Copy attempt id ${ATT}` })).toBeTruthy()
    // A long value wraps inside the popover rather than running past it.
    expect(painted(fact, 'overflow-wrap', { width: 1440 })).toBe('anywhere')
  })

  it('never draws the popover past the window', () => {
    render(<AgentLogs task={finished()} />)
    const menu = region().querySelector('.ag-logs-menu')!
    expect(painted(menu, 'max-width', { width: 1440 })?.replace(/\s+/g, ' ')).toBe('min(420px, calc(100vw - 32px))')
  })
})
