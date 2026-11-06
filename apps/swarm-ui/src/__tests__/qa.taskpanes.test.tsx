// QAFIX-TASKPANES (part of #1680): the first console visual-QA run
// (wf_9e9bf74834634db7bcc3, 2026-10-20, the offline preview at 1440 and 390
// px, both themes) on a task's Details, Attempts and start-up panes.
//
//   QA-115  The Failed card linked to the Attempts tab beside the Progress
//           card's own link to it -- two links, one place -- and its absent
//           end cause said `end cause — not recorded`, which tells a reader
//           nothing about where to look instead.
//   QA-113  The Attempts tab spoke shorthand: `3 att · 9 ev`, `ckpt`, `peak
//           rss`, `gen`, `2 blind`; and at 390 its rows wrapped unreadably.
//   QA-086  The start-up's first state drew two sentences side by side in a
//           flex row: `The agent’s browser is starting.Reading its start-up.`
//
// QA-088 (checkpoint paths breaking mid-token) is drawn by RunFiles.tsx,
// outside this lane: see the PR body.

import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAgentRun: vi.fn(),
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadStartup: vi.fn(),
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

const { AgentDetailScreen } = await import('../AgentDetail')
const { LogLastLine } = await import('../AgentLogs')
const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { StartupProgress } = await import('../StartupProgress')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

beforeEach(() => {
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockReturnValue(new Promise(() => {}))
  api.loadAgentDetail.mockReturnValue(new Promise(() => {}))
  api.loadStartup.mockReturnValue(new Promise(() => {}))
})

// ---------------------------------------------------------------------------
// QA-115: the Failed card
// ---------------------------------------------------------------------------

const ID = 'task_0123456789abcdef0115'

function failed(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    attempt_count: 1,
    max_attempts: 3,
    started_at: at(1),
    completed_at: at(10),
    last_error: 'the agent exited 1',
    ...over,
  })
}

async function details(t: Task, withLastLine = false): Promise<HTMLElement> {
  const run: AgentRun = {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { task_id: ID, exit_code: 1 })],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
  api.loadAgentRun.mockResolvedValue(ok(run))
  const lastLine = withLastLine ? <LogLastLine task={t} onOpen={() => {}} /> : undefined
  const { container } = render(<AgentDetailScreen taskId={t.id} onClose={() => {}} lastLine={lastLine} />)
  await waitFor(() => expect(container.querySelector('.dt-card[data-lead="failed"]')).not.toBeNull())
  return container as HTMLElement
}

describe('QA-115: the Failed card on a task’s Details', () => {
  it('leaves one link to the Attempts tab on the page, the Progress card’s', async () => {
    const el = await details(failed())
    const toAttempts = within(el)
      .getAllByRole('link')
      .filter((a) => (a.getAttribute('href') ?? '').endsWith('/attempts'))
    // MUTATION: put the `Attempts ›` DtMore back in DtFailure's head -- two links, one place.
    expect(toAttempts.map((a) => a.textContent)).toEqual(['Attempts tab ›'])
    // MUTATION: drop the Progress card's link instead -- the one left is not the tab's.
    expect(toAttempts[0]!.closest('.dt-progress')).not.toBeNull()
  })

  it('says where to read why it ended when no end cause was written', async () => {
    const el = await details(failed({ end_cause: null } as Partial<Task>))
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    // MUTATION: restore `end cause — not recorded`, which points nowhere.
    expect(card.textContent).not.toMatch(/end cause — not recorded/)
    // MUTATION: drop the pointer to the error -- the absence says nothing again.
    // It leads with where the cause is (QAFIX5-TASK-DETAIL, QA-115 run 5b).
    expect(within(card).getByText('why it ended: the error below · no end-cause code was written')).toBeTruthy()
  })

  it('labels the log preview under the error as not the error (owner 2026-10-20)', async () => {
    const el = await details(failed(), true)
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    const line = within(card).getByRole('button', { name: /^Last log line/ })
    // MUTATION: drop the FAILED label in `LogLastLine` -- `Last log line:
    // result ## Summary` under the error reads as its cause.
    expect(line.querySelector('b')!.textContent).toBe('Last log line (not the error)')
  })

  it('says so when neither an end cause nor an error was written', async () => {
    const el = await details(failed({ end_cause: null, last_error: null } as Partial<Task>))
    const card = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    // MUTATION: point at "the error below" when there is none.
    expect(within(card).getByText('no end cause recorded')).toBeTruthy()
    expect(card.textContent).not.toMatch(/the error below/)
  })
})

// ---------------------------------------------------------------------------
// QA-113: the Attempts tab
// ---------------------------------------------------------------------------

async function attempts(rows: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts: rows }))
  api.loadAgentDetail.mockResolvedValue(
    ok({ task: runTask({ state: 'SUCCEEDED', started_at: at(1) }), events, eventsDetail: null }),
  )
  const { container } = render(<AttemptTimelineScreen taskId="tsk_charts" />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

/** Attempt 2 has one event under its generation; attempt 1 has none on the page. */
function twoAttempts(): Promise<HTMLElement> {
  const leased = { ...ev('lease_acquired', at(21), 'att_2'), generation: 2 }
  return attempts(
    [
      attempt(2, { created_at: at(20), started_at: at(21), completed_at: at(40), peak_rss_bytes: 1024 ** 3, checkpoints: ['ckpt-00001'] }),
      attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(10) }),
    ],
    [ev('submitted', at(-1), null), leased],
  )
}

describe('QA-113: the Attempts tab says its figures in words', () => {
  it('counts attempts and events in words in the summary and the toolbar', async () => {
    const el = await twoAttempts()
    // MUTATION: summary back to `${n} att · ${n} ev`.
    // The `submitted` event is no attempt's, and the totals say so (VQ-011).
    // The event count is the toolbar's alone since QA2-FIX-13 (overview-agents-41): the summary counts attempts.
    expect(screen.getByText('2 attempts')).toBeTruthy()
    const toolbar = el.querySelector<HTMLElement>('.ctl-toolbar .is-end')!
    // MUTATION: toolbar back to `2 ev · first page · 1 blind`.
    expect(toolbar.textContent).toMatch(/2 events, 1 before any attempt · first page/)
    expect(toolbar.textContent).toMatch(/· partial 1 attempt without events\b/)
    expect(toolbar.textContent).not.toMatch(/\bev\b|\bblind\b/)
  })

  it('names every attempt fact with a word, not an abbreviation', async () => {
    const el = await twoAttempts()
    const keys = Array.from(el.querySelectorAll('.att-card .ctl-fact > b')).map((b) => b.textContent)
    // MUTATION: put any of `att`, `exec`, `peak rss` or `ckpt` back as a key.
    for (const short of ['att', 'exec', 'peak rss', 'ckpt']) expect(keys).not.toContain(short)
    for (const word of ['attempt', 'execution', 'peak memory', 'checkpoints']) expect(keys).toContain(word)
  })

  it('says events and generation in full on each card', async () => {
    const el = await twoAttempts()
    const notes = Array.from(el.querySelectorAll('.att-card .ctl-card-note')).map((n) => n.textContent)
    // MUTATION: card note back to `${n} ev · Cloud Run`.
    expect(notes).toContain('1 event · Cloud Run')
    // QA-113 run 2: an empty card says so in words rather than `0 events`.
    expect(notes).toContain('no events on this page · Cloud Run')
    // MUTATION: the event row back to `gen 2`.
    expect(el.querySelector('.ev-gen')?.textContent).toBe('generation 2')
  })

  it('lays a card out in rows a phone can read at 390', async () => {
    const el = await twoAttempts()
    const card = Array.from(el.querySelectorAll<HTMLElement>('.att-card')).find(
      (c) => c.querySelector('.ctl-facts') !== null && c.querySelector('ol.timeline') !== null,
    )!
    const facts = card.querySelector<HTMLElement>('.ctl-facts')!
    // MUTATION: drop the phone rule -- the facts wrap as one ragged line again.
    expect(painted(facts, 'flex-direction', PHONE)).toBe('column')
    expect(painted(facts, 'flex-direction', WIDE)).not.toBe('column')
    const row = card.querySelector<HTMLElement>('ol.timeline > li')!
    const type = row.querySelector<HTMLElement>('.ev-type')!
    // MUTATION: drop the phone rule -- the event's word, its gap and its generation wrap mid-row.
    expect(painted(row, 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(type, 'flex', PHONE)).toBe('1 0 100%')
    expect(painted(row, 'flex-wrap', WIDE)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// QA-086: the start-up's first state
// ---------------------------------------------------------------------------

describe('QA-086: the start-up before its first read', () => {
  it('stacks its two sentences in the card instead of running them together', () => {
    render(<StartupProgress taskId="task_startup0123456789ab" />)
    const title = screen.getByText('The agent’s browser is starting.')
    const say = screen.getByText('Reading its start-up.')
    const box = title.parentElement!
    expect(say.parentElement).toBe(box)
    // MUTATION: draw the two sentences straight in `.startup` again -- its flex row runs them together.
    for (const env of [WIDE, PHONE]) {
      expect(painted(box, 'display', env)).toBe('flex')
      expect(painted(box, 'flex-direction', env)).toBe('column')
    }
  })
})
