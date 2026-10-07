/**
 * THE INSPECTOR'S WORDS, FROM THE QA PASS ON SWARMCLOUD (2026-10-07, group G2).
 *
 *   G2-25  Raw enums in prose: `DEPENDENCY_INCOMPLETE` as the parked banner's
 *          title, `ended FAILED` in the run's Why, `from PLANNING` in its
 *          Progress, `29 ev · CLOUD_RUN_JOB` on an attempt card, and
 *          `lease_acquired` on Attempts beside Details' `lease released`.
 *   G2-26  `Last log line: other`, and a transcript drowned in
 *          `other · tool_progress` / `system · thinking_tokens` rows.
 *   G2-27  `thinking [real zero]` on a thinking block with no text: the mark
 *          means a measured zero, and nothing was measured.
 *   G2-28  `It holds no capacity while it waits.` on a cancelled task, and
 *          one cause -- no attempt -- worded four ways across the tiles.
 *   G2-29  Two `lease released` per attempt: the second is the worker's
 *          account give-back (`_emit_account_released`, which rides the frozen
 *          `LEASE_RELEASED` type with `detail.cause: "account_released"`).
 *
 * MUTATIONS, one per block: print `task.park_reason` as the banner title
 * again; print `a.backend` or `eventKind(e)` verbatim on the attempt card;
 * drop `proseWords` from the run's Why or `stateWord` from its Progress; take
 * `isProgressStep` out of `lastLineOf` or `foldProgress` out of `StepList`;
 * put the `zero` mark back on an empty thinking text; put "while it waits"
 * back unconditionally, or `nothing ran yet` / `not recorded` back on a task
 * with no attempt; read the event's type alone in `eventWord`. Each turns a
 * case red.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, CheckpointsPage, Task, TaskEvent, TaskTranscript, TranscriptStep } from '../types'
import { ALL_STATES as STATES } from '../lanes'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')
const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { LogBody } = await import('../Artifacts')
const { lastLineOf } = await import('../AgentLogs')
const { stateWord: chipWord } = await import('../components/StatePill')
const words = await import('../words')

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const CLASSES: ResourceClasses = { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } }

function emptyPage(): CheckpointsPage {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    prefix: 'tenants/acme/tasks/tsk_charts/attempts/',
    checkpoints: [],
    count: 0,
    total_found: 0,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
  }
}

function agentRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(1), completed_at: at(10) }),
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

async function mountRun(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue(ok(emptyPage()))
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
  return container as HTMLElement
}

async function mountAttempts(t: Task, attempts: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events, eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

const plus = (m: number, s: number) => new Date(Date.parse(at(m)) + s * 1000).toISOString()

// ---------------------------------------------------------------------------
// G2-25
// ---------------------------------------------------------------------------

describe('G2-25: one map words every machine token the inspector prints', () => {
  it('spells every task state exactly as the state chip does, and a run state too', () => {
    for (const s of STATES) expect(words.stateWord(s), s).toBe(chipWord(s))
    expect(words.stateWord('PLANNING')).toBe('planning')
  })

  it('words the state tokens inside a server-written sentence, and nothing else in it', () => {
    expect(words.proseWords("the run's workflow wf_AB12 ended FAILED")).toBe("the run's workflow wf_AB12 ended failed")
    expect(words.proseWords('step DEAD_LETTERED after FAILEDX')).toBe('step dead-lettered after FAILEDX')
  })

  it('titles the parked banner with the pill’s words, keeping the recorded token in its title', async () => {
    const el = await mountRun(
      agentRun({
        task: task({ state: 'PARKED', park_reason: 'DEPENDENCY_INCOMPLETE', attempt_count: 0, started_at: null, completed_at: null }),
        attempts: [],
      }),
    )
    const title = el.querySelector<HTMLElement>('.bar.amber strong')
    expect(title, 'no parked banner').not.toBeNull()
    expect(title!.textContent).toBe('waiting on a step')
    expect(title!.getAttribute('title')).toBe('DEPENDENCY_INCOMPLETE')
  })

  it('words the backend and every event on the attempt card', async () => {
    const t = task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(0), completed_at: at(5) })
    const el = await mountAttempts(t, [attempt(1, { started_at: at(0), completed_at: at(5), backend: 'CLOUD_RUN_JOB' })], [
      ev('submitted', at(-1), null),
      ev('lease_acquired', plus(-1, 10), 'att_1'),
      ev('succeeded', plus(5, 0), 'att_1'),
    ])
    // The submitted event belongs to no attempt and has a card of its own; the attempt's is the one with a backend.
    const card = [...el.querySelectorAll('.att-card')].find((c) => /lease/.test(visible(c)))!
    expect(card, 'no attempt card').toBeTruthy()
    expect(visible(card.querySelector('.ctl-card-note'))).toMatch(/ev · Cloud Run$/)
    expect(visible(card)).not.toMatch(/CLOUD_RUN_JOB/)
    const types = [...card.querySelectorAll('.ev-type')].map((t) => t.textContent)
    expect(types).toContain('lease acquired')
    expect(types.some((x) => x?.includes('_'))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// G2-26 and G2-27: the transcript
// ---------------------------------------------------------------------------

let n = 0
function step(over: Partial<TranscriptStep>): TranscriptStep {
  n += 1
  return {
    id: `L${n}:0`,
    line_offset: n * 100,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: null,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: null,
    ...over,
  }
}

const progress = () => step({ kind: 'other', role: null, meta: { raw_type: 'tool_progress' } })
const thinkingTokens = () => step({ kind: 'system', role: 'system', meta: { subtype: 'thinking_tokens' } })

function transcriptOf(steps: TranscriptStep[]): TaskTranscript {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: at(0), completed_at: at(5), exit_code: 0 },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout', source: 'final', status: 'ok', detail: null, uri: null, object_updated_at: at(5), age_seconds: 30,
      total_bytes: 900, offset: 0, returned_bytes: 900, next_offset: null, truncated: false, tail_window: null,
    },
    format: 'claude-stream-json',
    steps,
    complete: true,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: false,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
  } as unknown as TaskTranscript
}

function feedOf(steps: TranscriptStep[]) {
  const t = task({ state: 'SUCCEEDED', runner_profile: 'claude-code', started_at: at(0), completed_at: at(5) })
  return {
    task: t,
    attemptId: null,
    transcript: ok(transcriptOf(steps)),
    logs: { status: 'empty', fetchedAt: Date.now() },
  } as unknown as Parameters<typeof LogBody>[0]['v']
}

describe('G2-26: progress and thinking-token rows are not the last line, and fold into one', () => {
  it('chooses the last line past the noise', () => {
    const feed = feedOf([step({ text: 'Reading the diff' }), progress(), thinkingTokens(), progress()])
    expect(lastLineOf(feed, 'transcript')).toBe('text Reading the diff')
  })

  it('says nothing rather than a noise row when the window holds only noise', () => {
    expect(lastLineOf(feedOf([progress(), thinkingTokens()]), 'transcript')).toBeNull()
  })

  it('folds a run of them into one counted row, and keeps the rows behind it', () => {
    const steps = [step({ text: 'first' }), progress(), thinkingTokens(), progress(), progress(), step({ text: 'second' }), progress()]
    const { container } = render(<LogBody v={feedOf(steps)} view="transcript" reading={{ fetchedAt: Date.now(), pollMs: null }} now={Date.now()} />)
    const folds = [...container.querySelectorAll<HTMLElement>('li.arts-step.is-fold')]
    expect(folds, 'a run of four noise rows was not folded').toHaveLength(1)
    expect(visible(folds[0]!.querySelector('summary'))).toBe('4 progress events')
    expect(folds[0]!.querySelectorAll('li.arts-step')).toHaveLength(4)
    // A lone one is drawn as itself: there is nothing to fold.
    const top = [...container.querySelector('ol.arts-steps')!.children].map((li) => li.className)
    expect(top.filter((c) => c.includes('is-fold'))).toHaveLength(1)
    expect(top).toHaveLength(4)
  })
})

describe('G2-27: a thinking block with no text says `not shown`, never `real zero`', () => {
  it('words an empty and a redacted thinking block alike, muted, with no measured-zero mark', () => {
    const steps = [step({ kind: 'thinking', text: '' }), step({ kind: 'thinking', text: null, meta: { redacted: true } }), step({ kind: 'thinking', text: 'plan it' })]
    const { container } = render(<LogBody v={feedOf(steps)} view="transcript" reading={{ fetchedAt: Date.now(), pollMs: null }} now={Date.now()} />)
    const [empty, redacted, full] = [...container.querySelectorAll<HTMLElement>('li.arts-step.is-thinking')]
    for (const li of [empty!, redacted!]) {
      expect(visible(li)).toBe('thinking · not shown')
      expect(li.querySelector('.ctl-mark.is-zero'), 'a measured-zero mark on a thinking block').toBeNull()
      expect(li.querySelector('.arts-step-kind.is-muted')).not.toBeNull()
    }
    expect(full!.querySelector('details')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// G2-28
// ---------------------------------------------------------------------------

describe('G2-28: a task with no attempt is worded by its state, and its absent tiles alike', () => {
  const none = (state: Task['state']) =>
    agentRun({
      task: task({ state, attempt_count: 0, started_at: null, completed_at: state === 'CANCELLED' ? at(3) : null }),
      attempts: [],
      events: [ev('submitted', at(-1), null), ...(state === 'CANCELLED' ? [ev('cancelled', at(3), null)] : [])],
    })

  it('does not say a cancelled task waits', async () => {
    const el = await mountRun(none('CANCELLED'))
    const progress = visible(el.querySelector('.dt-progress'))
    expect(progress).not.toMatch(/while it waits/)
    expect(progress).toMatch(/It held no capacity\./)
    expect(visible(el.querySelector('.dt-resources'))).not.toMatch(/admitted yet/)
  })

  it('still says a waiting task holds none while it waits', async () => {
    const el = await mountRun(none('READY'))
    expect(visible(el.querySelector('.dt-progress'))).toMatch(/It holds no capacity while it waits\./)
  })

  it('says `no attempt` in every absent tile and resource row', async () => {
    const el = await mountRun(none('CANCELLED'))
    const cells = [...el.querySelectorAll<HTMLElement>('.dt-sc')].filter((c) => /^(Peak memory|CPU|Cost)/.test(c.querySelector('.dt-sc-l')?.textContent ?? ''))
    expect(cells).toHaveLength(3)
    for (const c of cells) {
      const said = visible(c)
      expect(said, said).toMatch(/no attempt/)
      expect(said, said).not.toMatch(/no attempt yet|nothing ran|not recorded|not reported/)
    }
    const rows = [...el.querySelectorAll<HTMLElement>('.dt-resources .dt-rr')]
    expect(rows.length).toBeGreaterThan(0)
    for (const r of rows) expect(visible(r.querySelector('small')), visible(r)).toBe('no attempt')
  })
})

// ---------------------------------------------------------------------------
// G2-29
// ---------------------------------------------------------------------------

describe('G2-29: the account give-back is not a second `lease released`', () => {
  const released = (s: number) => ev('lease_released', plus(5, s), 'att_1', { reason: 'terminal:SUCCEEDED' })
  const account = (s: number) =>
    ev('lease_released', plus(5, s), 'att_1', { cause: 'account_released', account_id: 'acct_1', provider: 'anthropic', unusable: false })

  it('names an event by its cause where the frozen type carries one', () => {
    expect(words.eventWord(account(1))).toBe('account released')
    expect(words.eventWord(released(0))).toBe('lease released')
    expect(words.eventWord({ type: 'running', detail: { cause: 'account_assigned' } })).toBe('account assigned')
    expect(words.eventWord({ type: 'cancelled', detail: { phase: 'cancel_requested' } })).toBe('cancel requested')
  })

  it('on the Attempts tab', async () => {
    const t = task({ state: 'SUCCEEDED', attempt_count: 1, started_at: at(0), completed_at: at(5) })
    const el = await mountAttempts(t, [attempt(1, { started_at: at(0), completed_at: at(5) })], [
      ev('submitted', at(-1), null),
      ev('succeeded', plus(5, 0), 'att_1'),
      released(0.2),
      account(0.5),
    ])
    const types = [...el.querySelectorAll('.att-card .ev-type')].map((x) => x.textContent)
    expect(types.filter((x) => x === 'lease released')).toHaveLength(1)
    expect(types).toContain('account released')
  })

  it('and in Details’ Progress', async () => {
    const el = await mountRun(
      agentRun({
        events: [ev('submitted', at(-1), null), ev('succeeded', plus(5, 0), 'att_1'), released(0.2), account(0.5)],
      }),
    )
    const kinds = [...el.querySelectorAll('.dt-progress .dt-ev-k')].map((x) => x.textContent)
    expect(kinds.filter((x) => x === 'lease released')).toHaveLength(1)
    expect(kinds).toContain('account released')
  })
})
