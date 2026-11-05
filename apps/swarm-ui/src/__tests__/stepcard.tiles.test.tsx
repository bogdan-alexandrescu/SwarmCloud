/**
 * THE STEP CARD'S ACTION BAR AND STAT TILES (owner's pick, 2026-11-05).
 *
 * `StepCard` draws, from one `StepRowModel` and its attempts: a toolbar
 * (Agent, Logs, Changes, the PR link, Stop/Retry on the right), four tiles
 * for the selected attempt (Elapsed, Attempt n/N, Cost, Tokens), a timeline
 * of segments sized by how long each attempt ran, and a Details fold for
 * everything else. This file renders the component directly with a fake
 * `AttemptLoader` -- no network, no cloud credentials -- and covers one state
 * per `it`: running, finished-with-a-PR, failed, parked-never-attempted,
 * attempts of different durations, the Details fold, and no task at all.
 */
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import { absentCell, measuredCell } from '../measure'
import { StepCard, type StepCardProps } from '../StepCard'
import type { AttemptRow, WorkflowStep } from '../types'
import type { AttemptLoader, SiblingRef, StepRowModel } from '../WorkflowViews'
import { at, attempt, task } from './runfixture'

afterEach(() => {
  cleanup()
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function wfStep(over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id: 'build', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, ...over }
}

function stepRow(over: Partial<StepRowModel> = {}): StepRowModel {
  return {
    step: wfStep(),
    taskId: 'tsk_build',
    look: { tone: 'live', word: 'running', title: 'running', mark: 'running', hue: 'live' },
    times: { spans: [], mark: null, attempts: null, waitedMs: null, waitedOpen: false, parentsMs: null, ranMs: null, sentence: '' },
    ran: absentCell({ text: '—', note: '' }),
    attempts: measuredCell('1 attempt', ''),
    attemptsOver: 0,
    cost: absentCell({ text: '—', note: '' }),
    tokens: absentCell({ text: '—', note: '' }),
    costFrom: null,
    tokensFrom: null,
    result: null,
    pending: false,
    inputs: { declared: new Map(), stray: [], malformed: 0 },
    why: null,
    sort: { order: 0, stateRank: null, waitedMs: null, ranMs: null, attempts: null, costUsd: null, tokens: null },
    ...over,
  }
}

const DEFAULT_SIBLING: SiblingRef = { workflowId: 'wf_1', mark: 'running', hue: 'live', word: 'running' }

function loaderFor(attempts: AttemptRow[]): AttemptLoader {
  return () => Promise.resolve({ status: 'ok', data: { attempts }, fetchedAt: 0 } as Result<{ attempts: AttemptRow[] }>)
}

const emptyLoader: AttemptLoader = () => Promise.resolve({ status: 'empty', fetchedAt: 0 } as Result<{ attempts: AttemptRow[] }>)

function renderCard(props: Partial<StepCardProps> & { row: StepRowModel }) {
  const defaults: StepCardProps = {
    workflowId: 'wf_1',
    row: props.row,
    task: null,
    taskState: null,
    changes: null,
    now: Date.parse(at(10)),
    load: emptyLoader,
    siblings: [DEFAULT_SIBLING],
    siblingIndex: 0,
    onSibling: () => {},
    stop: null,
    retry: null,
  }
  return render(<StepCard {...defaults} {...props} />)
}

/** The Details fold's `<dt>`/`<dd>` pair text for one label, or null. */
function factText(container: HTMLElement, label: string): string | null {
  const dt = Array.from(container.querySelectorAll('dt')).find((d) => d.textContent === label)
  const dd = dt?.nextElementSibling
  return dd === null || dd === undefined ? null : dd.textContent
}

// ---------------------------------------------------------------------------
// 1. A running step
// ---------------------------------------------------------------------------

it('a running step: toolbar with Stop, a running tile set, one segment "so far", ends in "now"', async () => {
  const a = attempt(1, { started_at: at(1), completed_at: null, exit_code: null, cost_usd: null, input_tokens: 1200, output_tokens: 340 })
  const t = task({ id: 'tsk_run', state: 'RUNNING', started_at: at(1), completed_at: null, attempt_count: 1, max_attempts: 3 })
  const row = stepRow({ taskId: 'tsk_run' })
  const changes = { href: '/w/wf_1/changes?step=build', count: 3, say: null }

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'RUNNING',
    changes,
    now: Date.parse(at(6)),
    load: loaderFor([a]),
    stop: <button>Stop</button>,
    retry: null,
  })

  await waitFor(() => expect(container.querySelector('.sc-tiles')).toBeTruthy())

  const bar = container.querySelector('.sc-bar') as HTMLElement
  expect(bar.getAttribute('role')).toBe('toolbar')
  expect(bar.getAttribute('aria-label')).toBe('Step actions')
  expect(within(bar).getByRole('link', { name: 'Agent' })).toBeTruthy()
  expect(within(bar).getByRole('link', { name: 'Logs' })).toBeTruthy()
  const changesLink = within(bar)
    .getAllByRole('link')
    .find((el) => el.textContent?.startsWith('Changes'))
  expect(changesLink).toBeTruthy()

  const end = container.querySelector('.sc-end') as HTMLElement
  expect(end.textContent).toBe('Stop')

  const elapsed = container.querySelector('[data-tile="elapsed"] .sc-tile-v')
  expect(elapsed?.textContent).toContain('so far')

  expect(container.querySelector('[data-tile="attempt"] .sc-tile-v')?.textContent).toBe('1 / 3')

  const cost = container.querySelector('[data-tile="cost"] .sc-tile-v') as HTMLElement
  expect(cost.textContent).toBe('—')
  expect(cost.title).toContain('No attempt reported a cost')

  const tokens = container.querySelector('[data-tile="tokens"]') as HTMLElement
  expect(tokens.classList.contains('is-absent')).toBe(false)
  expect(tokens.querySelector('.sc-tile-v')?.textContent).not.toBe('—')

  expect(container.querySelector('.sc-tl-bar.is-one .sc-seg.is-running')).toBeTruthy()

  const endsSpans = container.querySelectorAll('.sc-tl-ends span')
  expect(endsSpans[1]?.textContent).toBe('now')
})

// ---------------------------------------------------------------------------
// 2. A finished step with a PR
// ---------------------------------------------------------------------------

it('a finished step with a PR: the PR link, a Changes count, no Stop/Retry', async () => {
  const a = attempt(1, { started_at: at(0), completed_at: at(3), exit_code: 0 })
  const t = task({
    id: 'tsk_pr',
    state: 'SUCCEEDED',
    started_at: at(0),
    completed_at: at(3),
    attempt_count: 1,
    max_attempts: 1,
    result_summary: { git: { pull_request: { number: 3251, url: 'https://github.com/o/r/pull/3251', state: 'open' } } },
  })
  const row = stepRow({ taskId: 'tsk_pr' })
  const changes = { href: '/w/wf_1/changes?step=build', count: 7, say: null }

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'SUCCEEDED',
    changes,
    now: Date.parse(at(10)),
    load: loaderFor([a]),
    stop: null,
    retry: null,
  })

  await waitFor(() => expect(container.querySelector('.sc-tiles')).toBeTruthy())

  const bar = container.querySelector('.sc-bar') as HTMLElement
  const pr = within(bar).getByRole('link', { name: 'PR #3251' })
  expect(pr.getAttribute('href')).toBe('https://github.com/o/r/pull/3251')
  expect(bar.querySelector('.sc-count')?.textContent).toBe('7')
  expect(container.querySelector('.sc-end')).toBeNull()
})

// ---------------------------------------------------------------------------
// 3. A failed step
// ---------------------------------------------------------------------------

it('a failed step: Retry, the whole error, three segments with the third pressed, clicking one moves the selection', async () => {
  const err = 'boom: agent crashed\nmore detail about the crash'
  const attempts = [1, 2, 3].map((n) => attempt(n, { created_at: at(n), started_at: at(n), completed_at: at(n + 1), exit_code: 2 }))
  const t = task({ id: 'tsk_fail', state: 'FAILED', started_at: at(1), completed_at: at(4), attempt_count: 3, max_attempts: 3, last_error: err })
  const row = stepRow({
    taskId: 'tsk_fail',
    why: { kind: 'cause', text: 'boom: agent crashed', full: err, warn: true },
  })

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'FAILED',
    changes: null,
    now: Date.parse(at(10)),
    load: loaderFor(attempts),
    stop: null,
    retry: <button>Retry</button>,
  })

  await waitFor(() => expect(container.querySelector('.sc-tl-bar')).toBeTruthy())

  const end = container.querySelector('.sc-end') as HTMLElement
  expect(end.textContent).toBe('Retry')

  const errGroup = container.querySelector('[role="group"][aria-label="Why it failed"]') as HTMLElement
  expect(errGroup).toBeTruthy()
  expect(errGroup.querySelector('.sc-err-text')?.textContent).toBe(err)

  const segs = Array.from(container.querySelectorAll('.sc-tl-bar button.sc-seg'))
  expect(segs.length).toBe(3)
  expect(segs[2]?.getAttribute('aria-pressed')).toBe('true')

  fireEvent.click(segs[0]!)
  expect(segs[0]?.getAttribute('aria-pressed')).toBe('true')
  expect(container.querySelector('[data-tile="attempt"] .sc-tile-v')?.textContent).toBe('1 / 3')
})

// ---------------------------------------------------------------------------
// 4. A parked step never attempted
// ---------------------------------------------------------------------------

it('a parked step never attempted: only Agent in the toolbar, no tiles/timeline/fold, the wait sentence shown', async () => {
  const t = task({ id: 'tsk_parked', state: 'PARKED', attempt_count: 0, max_attempts: 3, park_reason: 'CI_PENDING' })
  const row = stepRow({
    taskId: 'tsk_parked',
    why: { kind: 'why', text: 'parked: waiting for retry window', full: 'parked: waiting for retry window', warn: false },
  })
  const changes = { href: '/x', count: 0, say: null }

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'PARKED',
    changes,
    now: Date.parse(at(10)),
    load: emptyLoader,
    stop: null,
    retry: null,
    siblings: [DEFAULT_SIBLING],
  })

  await waitFor(() => expect(container.querySelector('.sc-wait')).toBeTruthy())

  const bar = container.querySelector('.sc-bar') as HTMLElement
  expect(within(bar).getByRole('link', { name: 'Agent' })).toBeTruthy()
  expect(within(bar).queryByRole('link', { name: 'Logs' })).toBeNull()
  expect(Array.from(bar.querySelectorAll('a')).some((el) => el.textContent?.startsWith('Changes'))).toBe(false)

  expect(container.querySelector('.sc-tiles')).toBeNull()
  expect(container.querySelector('.sc-tl')).toBeNull()
  expect(container.querySelector('details.sc-more')).toBeNull()
  expect(container.querySelector('.sc-wait')?.textContent).toBe('parked: waiting for retry window')
})

// ---------------------------------------------------------------------------
// 5. Attempts of different durations
// ---------------------------------------------------------------------------

it('attempts of different durations: segments sized by duration, Previous/Next move the selection, a null-figure attempt dashes both tiles', async () => {
  const a1m05 = attempt(1, { created_at: at(0), started_at: at(0), completed_at: at(65 / 60), exit_code: 0, cost_usd: 0.5, input_tokens: 10, output_tokens: 5 })
  const a5m02 = attempt(2, { created_at: at(0), started_at: at(0), completed_at: at(302 / 60), exit_code: 0, cost_usd: 1.2, input_tokens: 100, output_tokens: 20 })
  const a2m10 = attempt(3, {
    created_at: at(0),
    started_at: at(0),
    completed_at: at(130 / 60),
    exit_code: 0,
    cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
  })
  const t = task({ id: 'tsk_multi', state: 'SUCCEEDED', attempt_count: 3, max_attempts: 3, completed_at: at(302 / 60) })
  const row = stepRow({ taskId: 'tsk_multi' })

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'SUCCEEDED',
    changes: null,
    now: Date.parse(at(10)),
    load: loaderFor([a1m05, a5m02, a2m10]),
    stop: null,
    retry: null,
  })

  await waitFor(() => expect(container.querySelector('.sc-tl-bar')).toBeTruthy())

  const lis = Array.from(container.querySelectorAll('.sc-tl-bar li')) as HTMLElement[]
  expect(lis.length).toBe(3)
  const grows = lis.map((li) => parseFloat(li.style.flexGrow))
  expect(grows[1]).toBeGreaterThan(grows[0]!)
  expect(grows[1]).toBeGreaterThan(grows[2]!)

  // The default selection is the newest attempt (2m10s), whose cost and
  // tokens are both unreported: dashes on both tiles, each with a title.
  const cost = container.querySelector('[data-tile="cost"] .sc-tile-v') as HTMLElement
  const tokens = container.querySelector('[data-tile="tokens"] .sc-tile-v') as HTMLElement
  expect(cost.textContent).toBe('—')
  expect(cost.title.length).toBeGreaterThan(0)
  expect(tokens.textContent).toBe('—')
  expect(tokens.title.length).toBeGreaterThan(0)
  expect(container.querySelector('[data-tile="attempt"] .sc-tile-v')?.textContent).toBe('3 / 3')

  const prev = within(container).getByRole('button', { name: 'Previous attempt' })
  const next = within(container).getByRole('button', { name: 'Next attempt' })

  fireEvent.click(prev)
  expect(container.querySelector('[data-tile="attempt"] .sc-tile-v')?.textContent).toBe('2 / 3')

  fireEvent.click(next)
  expect(container.querySelector('[data-tile="attempt"] .sc-tile-v')?.textContent).toBe('3 / 3')
})

// ---------------------------------------------------------------------------
// 6. The Details fold
// ---------------------------------------------------------------------------

it('the Details fold: Exit code, Checkpoints, Generation, the extras node, and the siblings pager', async () => {
  const a = attempt(2, { created_at: at(0), started_at: at(0), completed_at: at(1), exit_code: 0, checkpoints: ['c1', 'c2'] })
  const t = task({ id: 'tsk_details', state: 'SUCCEEDED', attempt_count: 1, max_attempts: 1, completed_at: at(1) })
  const row = stepRow({ taskId: 'tsk_details' })
  const siblings: SiblingRef[] = [
    { ...DEFAULT_SIBLING, workflowId: 'wf_1' },
    { ...DEFAULT_SIBLING, workflowId: 'wf_2' },
    { ...DEFAULT_SIBLING, workflowId: 'wf_3' },
  ]
  const onSibling = vi.fn()

  const { container } = renderCard({
    row,
    task: t,
    taskState: 'SUCCEEDED',
    changes: null,
    now: Date.parse(at(5)),
    load: loaderFor([a]),
    stop: null,
    retry: null,
    siblings,
    siblingIndex: 1,
    onSibling,
    extras: <section aria-label="Review verdict">x</section>,
    hasExtras: true,
  })

  await waitFor(() => expect(container.querySelector('details.sc-more')).toBeTruthy())

  expect(factText(container, 'Exit code')).toBe('0')
  expect(factText(container, 'Checkpoints')).toBe('2')
  expect(factText(container, 'Generation')).toBe('2')

  expect(container.querySelector('.sc-extras [aria-label="Review verdict"]')).toBeTruthy()

  const prevSib = within(container).getByRole('button', { name: 'Same step, newer workflow' })
  const nextSib = within(container).getByRole('button', { name: 'Same step, older workflow' })
  fireEvent.click(prevSib)
  expect(onSibling).toHaveBeenCalledWith(-1)
  fireEvent.click(nextSib)
  expect(onSibling).toHaveBeenCalledWith(1)
})

// ---------------------------------------------------------------------------
// 7. A step with no task
// ---------------------------------------------------------------------------

it('a step with no task: no toolbar at all', () => {
  const row = stepRow({ taskId: null })
  const { container } = renderCard({ row, task: null, taskState: null, changes: null })
  expect(container.querySelector('.sc-bar')).toBeNull()
})
