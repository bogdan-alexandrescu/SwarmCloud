// A CODEX ATTEMPT'S USAGE AND ERRORS DRAW AS ROWS OF THEIR OWN IN THE LOGS
// TAB (CODEX-ROWS, part of #2459; BGD, 2026-10-28, mock-up proposal B "New
// kinds in the drawer"). The server serves a codex `turn.completed` as a
// `usage` step whose figures are in meta, and `turn.failed` / `error` as an
// `error` step whose text is the message (swarm_api/transcript.py,
// tests/unit/control_plane/test_transcript_codex.py). Pinned here:
//
//   * a `usage` row draws its figures as labelled numbers in tabular figures,
//     "48,210 input · 41,900 cached · 2,316 output · turn 3", and a figure the
//     server did not send is left out -- never drawn as 0;
//   * an `error` row draws in the drawer's bad tone (the `is-err` row a failed
//     tool result takes, and the bad mark a failed result wears), labelled
//     "error" or "error · turn failed", the message first and any
//     "(retrying, 1 of 5)" after it, muted; "jump to error" counts it;
//   * an `other` or `system` row still draws as it did before, and a kind this
//     console does not know draws as an `other` row named by its kind (before
//     this change it threw, and the tab drew no step at all).
//
// MUTATIONS are named at each assertion.

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskLogs, TaskTranscript, TranscriptStep } from '../types'
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

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
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

const attemptBlock = {
  status: 'latest' as const,
  known: true,
  generation: 1,
  created_at: new Date(Date.now() - 60_000).toISOString(),
  completed_at: null,
  exit_code: null,
}

function noLogs(): TaskLogs {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: attemptBlock,
    read_at: new Date().toISOString(),
    streams: [],
    prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

function transcript(steps: TranscriptStep[]): TaskTranscript {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: attemptBlock,
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
    format: 'codex-exec-json',
    steps,
    complete: false,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  }
}

function running(): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'codex', attempt_count: 1, completed_at: null })
}

function dock(): HTMLElement {
  return screen.getByRole('region', { name: 'Logs' })
}

/** The drawn rows, one per step; every test passes one step or two. */
type Rows = HTMLElement[] & { 0: HTMLElement; 1: HTMLElement }

async function rows(steps: TranscriptStep[]): Promise<Rows> {
  api.loadTranscript.mockResolvedValue(ok(transcript(steps)))
  render(<AgentLogs task={running()} />)
  return waitFor(() => {
    const found = Array.from(dock().querySelectorAll<HTMLElement>('li.arts-step'))
    expect(found.length).toBe(steps.length)
    return found as Rows
  })
}

const USAGE_WORDS = 'turn completed: 48,210 input tokens (41,900 cached), 2,316 output tokens'

beforeEach(() => {
  localStorage.clear()
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1, { attempt_id: 'att_1' })] }))
  api.loadTaskLogs.mockResolvedValue(ok(noLogs()))
})

describe('a codex usage row draws its figures as labelled numbers', () => {
  it('draws every figure the server sent, grouped, in tabular figures, in order', async () => {
    const [row] = await rows([
      step(1, {
        kind: 'usage',
        role: 'system',
        text: USAGE_WORDS,
        meta: { input_tokens: 48_210, cached_input_tokens: 41_900, output_tokens: 2_316, turn: 3 },
      }),
    ])
    // MUTATION: drop the `usage` case (the row falls through to no row, or
    // to the server's words, and the figures are not labelled).
    expect(row.querySelector('.arts-step-kind')?.textContent).toBe('usage')
    const figures = row.querySelector<HTMLElement>('.arts-usage')
    // MUTATION: join the figures with ', ' or print them ungrouped (48210).
    expect(figures?.textContent).toBe('48,210 input · 41,900 cached · 2,316 output · turn 3')
    // MUTATION: drop the tabular-figures style from the figures.
    expect(figures?.style.fontVariantNumeric).toBe('tabular-nums')
    // MUTATION: draw the server's words beside the figures (said twice).
    expect(row.textContent).not.toContain('turn completed')
  })

  it('leaves a figure the server did not send out, never drawing it as 0', async () => {
    const [row, bare] = await rows([
      step(1, { kind: 'usage', role: 'system', text: 'x', meta: { input_tokens: 500, output_tokens: 7 } }),
      step(2, { kind: 'usage', role: 'system', text: 'turn completed', meta: {} }),
    ])
    // MUTATION: draw an absent figure as `?? 0` (the row gains "0 cached"),
    // or as a dash (it gains "— cached").
    expect(row.querySelector('.arts-usage')?.textContent).toBe('500 input · 7 output')
    expect(row.querySelector('[data-figure="cached"]')).toBeNull()
    expect(row.querySelector('[data-figure="turn"]')).toBeNull()
    // With no figure at all the server's words are the row, not an empty one.
    // MUTATION: draw an empty `.arts-usage` when no figure was sent.
    expect(bare.querySelector('.arts-usage')).toBeNull()
    expect(bare.textContent).toContain('turn completed')
  })
})

describe('a codex error row draws in the bad tone, the message first', () => {
  it('labels a failed turn, draws the message, and marks the row an error', async () => {
    const [failed, plain] = await rows([
      step(1, { kind: 'error', role: 'system', text: 'usage limit reached', meta: { subtype: 'turn_failed' } }),
      step(2, { kind: 'error', role: 'system', text: 'model overloaded', meta: { subtype: 'error' } }),
    ])
    // MUTATION: drop `is-err` from the error row (it draws untinted and
    // "jump to error" never reaches it).
    expect(failed.classList.contains('is-err')).toBe(true)
    expect(plain.classList.contains('is-err')).toBe(true)
    // MUTATION: draw the label as a plain `.arts-step-kind` (no bad mark).
    const mark = failed.querySelector('[data-tone="bad"]')
    expect(mark?.textContent).toContain('error · turn failed')
    expect(plain.querySelector('[data-tone="bad"]')?.textContent).toContain('error')
    expect(plain.querySelector('[data-tone="bad"]')?.textContent).not.toContain('turn failed')
    expect(failed.querySelector('pre')?.textContent).toBe('usage limit reached')
    // MUTATION: drop the error rows from the error targets (the count reads 0).
    await waitFor(() => within(dock()).getByRole('button', { name: /^2 error lines/ }))
  })

  it('draws a retry after the message, muted', async () => {
    const [row] = await rows([
      step(1, {
        kind: 'error',
        role: 'system',
        text: 'stream disconnected before completion',
        meta: { subtype: 'error', retry_attempt: 1, retry_limit: 5 },
      }),
    ])
    const message = row.querySelector('pre')
    const retry = row.querySelector<HTMLElement>('[data-retry]')
    expect(message?.textContent).toBe('stream disconnected before completion')
    // MUTATION: drop the retry counter.
    expect(retry?.textContent).toBe('(retrying, 1 of 5)')
    // MUTATION: drop the faint colour from the retry (it draws at full ink,
    // as it did under a bare `.ctl-sub`, which no rule styles in a log row).
    expect(retry?.style.color).toBe('var(--text-faint)')
    // MUTATION: draw the retry before the message.
    expect(message!.compareDocumentPosition(retry!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('a kind this console does not know still draws as before', () => {
  it('draws an `other` row and a `system` row unchanged beside the new kinds', async () => {
    const [other, system] = await rows([
      step(1, { kind: 'other', role: null, text: null, meta: { raw_type: 'thread.forked' } }),
      step(2, { kind: 'system', role: 'system', text: 'turn started', meta: { subtype: 'turn_started' } }),
    ])
    // MUTATION: route `other` or `system` through the new cases (the label
    // reads `usage` or `error`, and the row turns bad).
    expect(other.querySelector('.arts-step-kind')?.textContent).toBe('other · thread.forked')
    expect(other.classList.contains('is-err')).toBe(false)
    expect(system.querySelector('.arts-step-kind')?.textContent).toBe('system · turn_started')
    expect(system.querySelector('pre')?.textContent).toBe('turn started')
    expect(system.classList.contains('is-err')).toBe(false)
  })

  it('draws a kind newer than this console as an `other` row named by its kind', async () => {
    const future = step(1, { kind: 'compaction' as TranscriptStep['kind'], role: 'system', text: 'context compacted' })
    const [row] = await rows([future, step(2)])
    // MUTATION: drop the switch's `default` (the row is undefined, `StepRow`'s
    // `cloneElement` throws, and the Logs tab draws no step at all).
    expect(row.querySelector('.arts-step-kind')?.textContent).toBe('other · compaction')
    expect(row.querySelector('pre')?.textContent).toBe('context compacted')
  })
})
