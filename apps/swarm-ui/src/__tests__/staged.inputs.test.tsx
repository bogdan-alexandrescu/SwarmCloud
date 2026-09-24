// WHAT LANDED IN A RUN'S WORKSPACE, ON THE RUN ITSELF (redesign-v2 Panel 3).
//
// "Inputs -- buildable. `result_summary.staged_inputs[{task_id, filename, path,
// bytes, uri, from_checkpoint}]` is what actually landed in the workspace,
// including which upstream step each file came from. That is a direct edge
// back into the workflow DAG and should be drawn as one."
//
// The workflow board draws that edge between two steps. This file pins the
// other end of it: the agent run a reader opens from a node, which has to say
// which files it was given, where each came from (a link back to the upstream
// run, or the submission), how large each was -- and, for a file it declared
// and nothing has reported, WHICH kind of not-yet that is. The four words are
// the same ones the graph and the table use (`DECLARED_WORDS`), so the run and
// the board cannot describe one file two ways.
//
// Mounted through `Run`, the component the drawer renders, so the assertion is
// about the screen a reader sees and not about a component nobody mounts.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun, ResourceClasses } from '../api'
import type { Task } from '../types'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const NOW = '2026-09-24T12:00:00.000Z'
const THEN = '2026-09-24T11:00:00.000Z'

function task(over: Partial<Task> = {}): Task {
  return {
    id: 'tsk_build',
    tenant_id: 'acme',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: THEN,
    updated_at: NOW,
    started_at: THEN,
    completed_at: NOW,
    submitted_by: 'ada@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_one',
    step_id: 'build',
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: { prompt: 'Build it.' },
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 },
}

async function renderRun(t: Task): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const run: AgentRun = {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  }
  const { container } = render(<Run run={run} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull())
  return container as HTMLElement
}

function inputs(root: HTMLElement): HTMLElement {
  const el = root.querySelector<HTMLElement>('.run-inputs')
  expect(el, 'the run does not list what was staged into its workspace').toBeTruthy()
  return el!
}

function row(root: HTMLElement, file: string): HTMLElement {
  const r = inputs(root).querySelector<HTMLElement>(`tr[data-file="${file}"]`)
  expect(r, `no row for ${file}`).toBeTruthy()
  return r!
}

describe('Panel 3: the inputs a run was given', () => {
  it('lists every staged file with its size and where it came from, and links back to the run that made it', async () => {
    const root = await renderRun(
      task({
        metadata: { input_from: { t_plan: 'plan.md', t_scan: 'scan.log' } },
        result_summary: {
          staged_inputs: [
            { task_id: 't_plan', filename: 'plan.md', path: 'plan.md', bytes: 2048, from_checkpoint: true },
            // No task_id: viz #9 -- this came from the submission.
            { filename: 'brief.txt', path: 'brief.txt', bytes: 10 },
          ],
        },
      }),
    )
    const plan = row(root, 'plan.md')
    expect(plan.querySelector('a[href="#work/task/t_plan"]'), 'no way back to the run that produced it').toBeTruthy()
    expect(plan.textContent).toContain('2 KiB')
    expect(plan.textContent).toContain('from checkpoint')

    const brief = row(root, 'brief.txt')
    expect(brief.textContent).toContain('submission')
    expect(brief.textContent).toContain('10 B')
    expect(brief.querySelector('a')).toBeNull()

    // DECLARED AND FINISHED WITH NO REPORT: a word, on the absence mark, and
    // never a size.
    const scan = row(root, 'scan.log')
    const mark = scan.querySelector('.ctl-mark.is-absent')
    expect(mark, 'a declared file nothing reported is drawn as if it arrived').toBeTruthy()
    expect(mark!.textContent).toBe('not reported')
    expect(scan.textContent).not.toMatch(/\d+ (B|KiB|MiB)/)
  })

  it('says a live run reports its inputs at finish, not that they are missing', async () => {
    const root = await renderRun(
      task({
        state: 'RUNNING',
        completed_at: null,
        metadata: { input_from: { t_plan: 'plan.md' } },
      }),
    )
    expect(row(root, 'plan.md').textContent).toContain('reported at finish')
  })

  it('counts an entry it could not read, and does not call a declared file unreported beside it', async () => {
    const root = await renderRun(
      task({
        metadata: { input_from: { t_plan: 'plan.md' } },
        result_summary: { staged_inputs: [{ task_id: 't_plan', path: 'plan.md' }] },
      }),
    )
    expect(row(root, 'plan.md').textContent).toContain('report unreadable')
    const count = inputs(root).querySelector('.ctl-mark.is-unread')
    expect(count, 'an unreadable entry was dropped without a trace').toBeTruthy()
    expect(count!.textContent).toBe('1 unreadable')
  })

  it('draws nothing for a run that declared no input and reported none', async () => {
    const root = await renderRun(task({ metadata: { ticket: 'ACME-41' } }))
    expect(root.querySelector('.run-inputs')).toBeNull()
  })
})
