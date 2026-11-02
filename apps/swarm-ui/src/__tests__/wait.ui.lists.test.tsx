// WAIT-UI-LISTS (part of #1782, owner decision 4): the Agents list's Waiting
// tab and Overview's "Needs a look" say each waiting task's OWN reason -- the
// short form of `waiting.summary`, which WAIT-API serves on every task-listing
// row -- instead of one generic "waiting for CI" for every merge step.
//
//   - the Waiting tab's reason line is the short form ("CI #1779: 5 pending",
//     "capacity: runner:merge full"), and the whole summary is the line's
//     title and the row's accessible name;
//   - "Needs a look"'s parked card names each item with its short reason;
//   - a row from an API that predates the field (no `waiting`) says what it
//     said before.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { whyAgent, type Task, type TaskPage, type TaskState, type WaitingRow } from '../types'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(async () => ({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { CheckCard } from '../Overview'
import { deriveChecks, type CheckInputs } from '../checks'
import { resetListSnap } from '../listSnap'
import { waitingShort } from '../waitingShort'

const NOW = '2026-10-21T02:40:00.000Z'
const THEN = '2026-10-21T01:40:00.000Z'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: THEN,
    updated_at: NOW,
    started_at: null,
    completed_at: null,
    submitted_by: 'alex@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

// The summaries exactly as `swarm_api/waiting_detail.py` writes them.
const CI: WaitingRow = {
  reason: 'CI_PENDING',
  summary: 'CI on PR #1779: 5 checks pending (shell, ruff, python, ...); re-checked 2 times; next check 02:46Z',
}
const CI_FAILED: WaitingRow = {
  reason: 'CI_PENDING',
  summary: 'CI on PR #1780: 2 checks pending (ui (typecheck), python), 1 failed (shell); re-checked 1 times',
}
const CAPACITY: WaitingRow = {
  reason: 'READY_CAPACITY',
  summary: 'Ready; capacity binds on runner:merge (4 of 4 units in use)',
}

async function land(tasks: Task[]): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={null} />)
  await waitFor(() => expect(container.querySelector('.row.is-compact')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

const rowOf = (c: HTMLElement, id: string) => c.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)!
const whyOf = (c: HTMLElement, id: string) => rowOf(c, id).querySelector<HTMLElement>(':scope > .why')!

afterEach(() => {
  document.body.innerHTML = ''
  resetListSnap()
})

describe('waitingShort: the one-line reason a list row has room for', () => {
  it('says a CI park as its pull request and how many checks are pending or failed', () => {
    // MUTATION: return the summary's first clause unparsed ("CI on PR #1779: 5 checks pending (...)").
    expect(waitingShort(CI)).toBe('CI #1779: 5 pending')
    // MUTATION: drop the failed count, or count a check name's words as one
    // ("ui (typecheck)" must not break the parse).
    expect(waitingShort(CI_FAILED)).toBe('CI #1780: 2 pending, 1 failed')
    // MUTATION: drop the "on its pull request" branch: no number, no "#".
    expect(waitingShort({ reason: 'CI_PENDING', summary: 'CI on its pull request: checks not recorded' })).toBe(
      'CI: checks not recorded',
    )
  })

  it('says a READY task by the pool that binds', () => {
    // MUTATION: drop the READY_CAPACITY branch; it read "Ready".
    expect(waitingShort(CAPACITY)).toBe('capacity: runner:merge full')
    // MUTATION: call every binding pool full, whatever its count.
    expect(
      waitingShort({ reason: 'READY_CAPACITY', summary: 'Ready; capacity binds on runner:merge (3 of 4 units in use)' }),
    ).toBe('capacity: runner:merge 3/4 used')
    // MUTATION: drop the paused branch.
    expect(waitingShort({ reason: 'READY_CAPACITY', summary: 'Ready; pool tenant:acme is paused' })).toBe(
      'capacity: tenant:acme paused',
    )
    // MUTATION: drop the scheduler-pass branch.
    expect(
      waitingShort({ reason: 'READY_CAPACITY', summary: "Ready; every pool has room: waiting for the scheduler's next pass" }),
    ).toBe('next scheduler pass')
  })

  it('says the other reasons by their own fact', () => {
    // MUTATION: drop any one branch below; the fallback says the first clause.
    expect(waitingShort({ reason: 'SCHEDULED_RETRY', summary: 'Retry scheduled: attempt 2 of 3 at 03:00Z (exit 1)' })).toBe(
      'retry 2 of 3 at 03:00Z',
    )
    expect(
      waitingShort({ reason: 'PROVIDER_QUOTA_EXHAUSTED', summary: 'Provider quota exhausted on anthropic (account a1); until 04:00Z' }),
    ).toBe('quota: anthropic until 04:00Z')
    expect(
      waitingShort({ reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 2 upstream steps: plan (RUNNING), scan (QUEUED)' }),
    ).toBe('waiting on plan, scan')
    expect(waitingShort({ reason: 'CHILDREN_INCOMPLETE', summary: 'Waiting for 3 of 5 children to finish (2 RUNNING, 1 QUEUED)' })).toBe(
      'children: 3 of 5 open',
    )
    expect(waitingShort({ reason: 'MANUAL_PAUSE', summary: 'Paused by alex@acme.test' })).toBe('paused by alex@acme.test')
  })

  it('says a reason it does not parse by the summary’s first clause, and nothing for an empty one', () => {
    // MUTATION: return the whole summary, "; eligible since ..." and all.
    expect(
      waitingShort({ reason: 'SOMETHING_NEW', summary: "Parked: SOMETHING_NEW; eligible since 19:46Z, still parked: waiting for the scheduler's next pass" }),
    ).toBe('Parked: SOMETHING_NEW')
    // MUTATION: return '' for an empty summary: the row would lose its line.
    expect(waitingShort({ reason: 'CI_PENDING', summary: '' })).toBeNull()
  })
})

describe('the Agents Waiting tab says each task’s own reason', () => {
  const A = 'task_1779aaaa'
  const B = 'task_1780bbbb'
  const R = 'task_readycap'

  it('draws the short form on the reason line, and the whole summary as its title and in the row’s name', async () => {
    const c = await land([
      task(A, 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-a', waiting: CI }),
      task(B, 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-b', waiting: CI_FAILED }),
      task(R, 'READY', { step_id: 'merge-c', waiting: CAPACITY }),
    ])
    // MUTATION: keep `whyAgent`'s generic CI_PENDING sentence on the row.
    expect(whyOf(c, A).textContent).toBe('CI #1779: 5 pending')
    expect(whyOf(c, B).textContent).toBe('CI #1780: 2 pending, 1 failed')
    // Visual QA pass 2 (overview-agents-56): the row names the pool by `poolLabel`.
    expect(whyOf(c, R).textContent).toBe('capacity: merge full')
    // MUTATION: title the line with the short form: the whole summary is lost to a pointer.
    expect(whyOf(c, A).getAttribute('title')).toBe(CI.summary)
    // MUTATION: name the opener with the short form (`why.text`) rather than the whole summary.
    const opener = screen.getByRole('button', { name: new RegExp(`^merge-a · ${A}`) })
    expect(opener.getAttribute('aria-label')).toContain(CI.summary)
    expect(rowOf(c, R).querySelector('.cr-open')!.getAttribute('aria-label')).toContain(CAPACITY.summary)
  })

  it('leaves a row whose API sends no `waiting` exactly as it was', async () => {
    const old = task(A, 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-a' })
    const c = await land([old])
    // MUTATION: draw the short form from `park_reason` when `waiting` is absent,
    // or drop the line: an older API's row must say today's sentence.
    expect(whyOf(c, A).textContent).toBe(whyAgent(old))
    expect(whyOf(c, A).getAttribute('title')).toBe(whyAgent(old))
    expect(rowOf(c, A).querySelector('.cr-open')!.getAttribute('aria-label')).toContain(whyAgent(old))
  })
})

describe('Overview’s "Needs a look" names each parked item’s reason', () => {
  const empty = { status: 'loading' } as const

  function parked(tasks: Task[]) {
    const c = {
      capacity: empty,
      tasks: { status: 'ok', data: { tasks, tenant_id: 'acme' }, fetchedAt: Date.now() },
      leases: empty,
      providers: empty,
      accounts: empty,
      stats: empty,
      workflows: empty,
    } as unknown as CheckInputs
    const check = deriveChecks(c, Date.parse(NOW)).find((x) => x.label === 'Parked work')!
    expect(check.status).toBe('found')
    return check.status === 'found' ? check.problems : []
  }

  it('puts the short reason beside each item on the card', () => {
    const problems = parked([
      task('task_1779aaaa', 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-a', waiting: CI }),
      task('task_1780bbbb', 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-b', waiting: CI_FAILED }),
    ])
    const p = problems[0]!
    // MUTATION: leave `whyEach` counting reasons only ("CI_PENDING ×2 — ...").
    expect(p.detail).toContain('CI #1779: 5 pending (merge-a)')
    expect(p.detail).toContain('CI #1780: 2 pending, 1 failed (merge-b)')
    // And the card draws it, not only its accessible name.
    const { container } = render(<CheckCard problem={p} />)
    // MUTATION: drop the detail line from CheckCard.
    expect(container.querySelector('.ov-att-t small:last-child')!.textContent).toContain('CI #1779: 5 pending (merge-a)')
  })

  it('says what it said before for items whose API sends no `waiting`', () => {
    const problems = parked([
      task('task_1779aaaa', 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-a' }),
      task('task_1780bbbb', 'PARKED', { park_reason: 'CI_PENDING', step_id: 'merge-b' }),
    ])
    // MUTATION: list the items' names with an empty reason, or invent one from `park_reason`.
    expect(problems[0]!.detail).toMatch(/^CI_PENDING ×2 — Waiting for the pull request checks to finish\. Holds no capacity\. /)
    expect(problems[0]!.detail).not.toContain('merge-a')
  })
})
