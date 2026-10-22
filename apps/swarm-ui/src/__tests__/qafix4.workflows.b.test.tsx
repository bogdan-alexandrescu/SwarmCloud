/**
 * A STEP THE CASCADE CANCELLED NAMES THE STEP THAT FAILED (visual QA run 4,
 * N23: flow-find-failure-desktop-D3, flow-workflow-graph-light-06, dark-06;
 * QAFIX4-WORKFLOWS-B, part of #1680).
 *
 * Run 4 saw 'synthesis' read "An upstream step did not succeed." on
 * Overview's Recent failures and on Agents › Recent, and its graph node say
 * only "cancelled", while the workflow table's Why column said "blocked:
 * checkpoints failed". Now:
 *
 *   * `whyAgent` handed the rows a screen read names the failed parent,
 *     "Cancelled: checkpoints failed.", and keeps the old sentence when the
 *     parent is not among them; a requested cancel stays "Cancelled by request.";
 *   * Overview's Recent failures and Agents › Recent pass the rows they read;
 *   * `nodeNote` draws `stepWhy`'s "blocked: checkpoints failed" line for a
 *     cascade-cancelled step, and nothing for a requested cancel.
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage, WorkflowStep } from '../types'
import { whyAgent } from '../types'
import { nodeNote, stepWhy } from '../stepviews'
import { task } from './runfixture'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen, CANCEL_NOTE } from '../Agents'
import { RecentFailures } from '../OverviewRegions'

// Recent failures keeps the last 24h, so the ends are dated from now.
const ago = (m: number): string => new Date(Date.now() - m * 60_000).toISOString()

const OLD = 'An upstream step did not succeed.'
const NAMED = 'Cancelled: checkpoints failed.'

const failed: Task = task({
  id: 'tsk_checkpoints',
  state: 'FAILED',
  workflow_id: 'wf_research',
  step_id: 'checkpoints',
  started_at: ago(30),
  completed_at: ago(25),
  last_error: 'exit status 1',
})
const cascaded: Task = task({
  id: 'tsk_synthesis',
  state: 'CANCELLED',
  workflow_id: 'wf_research',
  step_id: 'synthesis',
  depends_on: ['tsk_checkpoints'],
  completed_at: ago(24),
})
const requested: Task = { ...cascaded, id: 'tsk_synthesis_req', cancel_requested: true }

const byId = (...ts: Task[]): ReadonlyMap<string, Task> => new Map(ts.map((t) => [t.id, t]))

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

describe('whyAgent on a cancelled task', () => {
  it('names the failed parent when the caller hands it one', () => {
    // MUTATION: return 'An upstream step did not succeed.' unconditionally in whyAgent's CANCELLED branch.
    expect(whyAgent(cascaded, null, byId(failed, cascaded))).toBe(NAMED)
  })

  it('falls back to the parent’s title, then its id, when it has no step id', () => {
    const titled = { ...failed, step_id: null, metadata: { title: 'Gather checkpoints' } }
    // MUTATION: drop the `metadata.title` arm of `taskName` (types.ts).
    expect(whyAgent(cascaded, null, byId(titled))).toBe('Cancelled: Gather checkpoints failed.')
    // MUTATION: make `taskName` return '' for a parent with neither.
    expect(whyAgent(cascaded, null, byId({ ...failed, step_id: null }))).toBe('Cancelled: tsk_checkpoints failed.')
  })

  it('keeps the old sentence when the parent is not in hand, or did not fail', () => {
    // MUTATION: make the `taskById` default anything but null-safe (name a parent from nothing).
    expect(whyAgent(cascaded)).toBe(OLD)
    // MUTATION: drop the `taskById.get` miss check in `failedParentTask`.
    expect(whyAgent(cascaded, null, byId(cascaded))).toBe(OLD)
    // MUTATION: drop the `CASCADE_PARENT_STATES` check, so a SUCCEEDED parent is named.
    expect(whyAgent(cascaded, null, byId({ ...failed, state: 'SUCCEEDED' }))).toBe(OLD)
  })

  it('says a requested cancel was requested, failed parent in hand or not', () => {
    // MUTATION: drop `!task.cancel_requested` from whyAgent's CANCELLED branch.
    expect(whyAgent(requested, null, byId(failed))).toBe('Cancelled by request.')
    expect(whyAgent(requested)).toBe('Cancelled by request.')
  })
})

function page(tasks: Task[]): Result<TaskPage> {
  return { status: 'ok', data: { tasks, tenant_id: 'acme', next_page_token: null }, fetchedAt: Date.now() }
}

describe('Overview › Recent failures', () => {
  it('names the failed step on the cascade-cancelled row when the parent is among the rows read', () => {
    const { container } = render(<RecentFailures tasks={page([cascaded, failed])} />)
    const row = [...container.querySelectorAll('tbody tr')].find((r) => r.textContent?.includes('synthesis'))!
    // MUTATION: call `whyAgent(t)` again in `failureTable` (OverviewRegions.tsx), dropping `taskById`.
    expect(row.querySelector('.ov-why')?.textContent).toBe(NAMED)
  })

  it('keeps the old sentence when the parent was not read', () => {
    const { container } = render(<RecentFailures tasks={page([cascaded])} />)
    // MUTATION: name a parent the screen did not read (e.g. fall back to `depends_on[0]`).
    expect(container.querySelector('tbody tr .ov-why')?.textContent).toBe(OLD)
  })
})

describe('Agents › Recent', () => {
  it('names the failed step on the cascade-cancelled row, and keeps its meta line’s span note', async () => {
    api.loadTasks.mockResolvedValue(page([{ ...cascaded, started_at: ago(40) }, failed]))
    const { container } = render(<AgentsScreen onOpen={() => {}} taskId={null} />)
    await waitFor(() => expect(container.querySelector('.row')).not.toBeNull())
    // MUTATION: drop `taskById` from `rowReason`'s `whyAgent` call (Agents.tsx).
    await waitFor(() => expect(container.textContent).toContain(NAMED))
    expect(container.textContent).not.toContain(OLD)
    // MUTATION: move the cause onto the meta line in place of the `to cancel` note (U10a D20).
    expect(container.querySelector('.cr-sub .when-note')?.textContent).toBe(CANCEL_NOTE)
  })
})

const step = (step_id: string, depends_on: string[], task_id: string): WorkflowStep => ({
  step_id,
  runner_profile: 'claude-code',
  resource_class: 'standard',
  depends_on,
  input_from: {},
  task_id,
})

describe('nodeNote on a cancelled step', () => {
  const steps = [step('checkpoints', [], 'tsk_checkpoints'), step('synthesis', ['checkpoints'], 'tsk_synthesis')]
  const joined = (t: Task) => ({ kind: 'state' as const, state: t.state, task: t })

  it('draws the table’s blocked line for a cascade cancel', () => {
    const tasks = byId(failed, cascaded)
    const note = nodeNote(steps[1]!, joined(cascaded), steps, tasks, null)
    // MUTATION: return null for every CANCELLED state in `nodeNote` (stepviews.ts), as before.
    expect(note?.text).toBe('blocked: checkpoints failed')
    // The node and the table say one thing.
    expect(note).toEqual(stepWhy(steps[1]!, joined(cascaded), steps, tasks, null))
  })

  it('draws nothing for a requested cancel, or a cascade whose failed parent was not read', () => {
    const req = { ...cascaded, cancel_requested: true }
    // MUTATION: drop the `cancel_requested` check from `nodeNote`.
    expect(nodeNote(steps[1]!, joined(req), steps, byId(failed, req), null)).toBeNull()
    // MUTATION: drop the `failedParentOf(...) === null` check from `nodeNote`.
    expect(nodeNote(steps[1]!, joined(cascaded), steps, byId(cascaded), null)).toBeNull()
  })
})
