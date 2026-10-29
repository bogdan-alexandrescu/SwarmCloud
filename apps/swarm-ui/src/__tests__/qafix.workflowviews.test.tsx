// QAFIX-RUNS-TIMELINE, THE WORKFLOW PAGE'S STEP CARD (part of #1680, the
// first console visual QA run, wf_9e9bf74834634db7bcc3, 2026-10-20):
//
//   QA-121  the only 'why' a failed step's card gave was 'exit status 1:
//           pytest collected 2 failures', with no way to its logs: the head
//           linked 'Open agent →' and 'Changes ›' and nothing else.
//
// QAFIX2-WORK-MINOR (visual QA run 2, wf_b6e2441b419f423a84eb, 2026-10-20):
//
//   WG-4, WG-6  the card's attempt facts were keyed 'gen', 'exit' and
//           'ckpts', its head said 'sub 8m ago', and its 'same step' row
//           printed the shape bare ('5 → 1 · 6 steps') though PR 1719 had
//           worded the page head's. Each is said in words now, the shape as
//           the head says it. ('exit 0' beside 'why it failed: exit status 1'
//           is the fixture build's data: its newest attempt is always exit 0.)
//   WG-7    the card's 'Changes ›' link and the page's 'Changes' tab used one
//           word for two scopes. The card's link names its scope: the step.
//
// Every assertion names the mutation that turns it red.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { ownFromBoard } from './workflowsById'
import { attempt } from './runfixture'
import { landed } from './reads'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')

const T0 = Date.parse('2026-10-20T03:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], task_id: string | null): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id, tenant_id: 'eng', state, runner_profile: 'claude-code', resource_class: 'standard', provider: 'anthropic',
    priority: 0, created_at: iso(-600), updated_at: iso(-60), started_at: iso(-500), completed_at: iso(-100),
    submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
    workflow_id: 'wf_tests', step_id: null, depends_on: null, cancel_requested: false, repository_url: null, model: null,
    timeout_seconds: null, next_eligible_at: null, metadata: { unit: 'run-the-tests' }, repository_ref: null, input: null,
    last_error: null, result_summary: null, latest_checkpoint: null, current_generation: 1, current_lease_id: null,
    ...over,
  }
}

const WF: Workflow = {
  workflow_id: 'wf_tests', state: 'FAILED', tenant_id: 'eng', stored_state: 'FAILED', state_source: 'derived',
  rollup: { state: 'FAILED', complete: true, reason: 'test', counts: { SUCCEEDED: 1, FAILED: 1 }, unreadable_steps: [], unstarted_steps: [], steps_read: 2 },
  created_at: iso(-600), updated_at: iso(-60), submitted_by: 'operator@example.com', priority: 0,
  on_step_failure: 'fail_workflow', cancel_requested: false,
  steps: [step('build', [], 't_build'), step('test', ['build'], 't_test')],
}
const TASKS = new Map<string, Task>([
  ['t_build', task('t_build', 'SUCCEEDED', { step_id: 'build' })],
  ['t_test', task('t_test', 'FAILED', { step_id: 'test', last_error: 'exit status 1: pytest collected 2 failures' })],
])

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  // MUTATION: answer this read 2.5 s late (past setup.ts's 2000 ms window)
  // and a bare `waitFor` on the card reads aria-label undefined in all five
  // cases, the QA-121 flake; `arrived` waits it out and stays green.
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows: [WF], taskById: TASKS, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

function Routed({ initial }: { initial: string }) {
  const [view, setView] = useState(initial)
  return <WorkflowsScreen view={view} onView={setView} />
}

// THE CARD ARRIVES WITH THE READS IT IS DRAWN FROM (#2379 comment 19). A
// bare `waitFor` on `section.wf-inspect` raced setup.ts's 2000 ms window, and
// once on a loaded runner the card's aria-label still read undefined when it
// expired. The page draws from its own read (`loadWorkflow`, which this
// file's double answers from the board) and reads the board beside it, so
// the wait is on those two (`landed`, reads.ts, #605), and the card is
// queried once, after: no timeout can expire while the page catches up.
async function arrived(): Promise<HTMLElement> {
  await landed(api.loadWorkflow)
  await landed(api.loadWorkflowBoard)
  const el = document.querySelector<HTMLElement>('section.wf-inspect')
  expect(el?.getAttribute('aria-label')).toBe('Step test of wf_tests')
  return el!
}

describe('QA-121: a step card links to the step agent\'s logs', () => {
  it('offers Logs beside Open agent on the failed step the page opens on, to that agent\'s Logs tab', async () => {
    render(<Routed initial="wf=wf_tests" />)
    // The page picks the first failure on arrival.
    const card = await arrived()
    const open = within(card).getByRole('link', { name: 'Open agent →' })
    // MUTATION: drop the Logs link from StepInspector's head and this is red.
    const logs = within(card).getByRole('link', { name: 'Logs ›' })
    // A FAILED task sits in Recent; its Logs tab is the agent's path plus `/logs`.
    // MUTATION: point it at the bare agent (no `/logs`), or at the Live
    // list a finished task is not in, and this is red.
    expect(open.getAttribute('href')).toBe('/agents/recent/t_test')
    expect(logs.getAttribute('href')).toBe('/agents/recent/t_test/logs')
  })
})

const openCard = async () => {
  render(<Routed initial="wf=wf_tests" />)
  return arrived()
}

describe('WG-4, WG-6: the step card says each figure in words', () => {
  it('keys the attempt facts generation, exit code and checkpoints, never gen, exit or ckpts', async () => {
    api.loadAttempts.mockResolvedValue({
      status: 'ok',
      fetchedAt: T0,
      data: { attempts: [attempt(1, { task_id: 't_test', exit_code: 1, error: 'exit status 1', checkpoints: ['ckpt-00001'] })] },
    })
    const card = await openCard()
    const scrub = card.querySelector<HTMLElement>('[data-scrub="attempt"]')!
    await waitFor(() => expect(scrub.querySelector('.ctl-facts')).not.toBeNull())
    const keys = [...scrub.querySelectorAll('.ctl-fact > b')].map((b) => b.textContent)
    // MUTATION: print `f.key` as the label again (FactList) and this reads
    // ['gen', 'took', 'exit', 'cost', 'tokens', 'ckpts', 'error'].
    // 'lease generation' and 'this attempt’s cost' since QAFIX5 (run 5b L4, D2).
    // 'saved checkpoints', and no 'error' restating the exit code ('exit
    // status 1' beside 'exit code 1'), since QAFIX6 (run 6, F2).
    // The lease generation folds under More about this attempt since
    // QA1-FIX-29 (overview-agents-66): last, behind its summary.
    expect(keys).toEqual(['took', 'exit code', 'this attempt’s cost', 'tokens', 'saved checkpoints', 'lease generation'])
    expect(scrub.querySelector('details.wf-inspect-more .ctl-fact > b')?.textContent).toBe('lease generation')
    const fact = (k: string) => [...scrub.querySelectorAll<HTMLElement>('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === k)
    // The figures are the attempt's own; only their labels changed.
    // MUTATION: map a label onto the wrong fact and one of these is red.
    // The generation's figure, then what it is for (`FACT_HINT`, QAFIX5).
    expect(fact('lease generation')?.textContent).toMatch(/^lease generation1 · raised by every new lease/)
    expect(fact('exit code')?.textContent).toBe('exit code1')
    expect(fact('saved checkpoints')?.textContent).toBe('saved checkpoints1')
  })

  it('says submitted, not sub, in the head -- and on the workflow’s own page leaves the times to the page head', async () => {
    const card = await openCard()
    // QAFIX5 (QA-120, run 5b): on the workflow's page the card is headed by
    // the step, and the workflow's start and submit are the page head's
    // count note (#138), not repeated in the card.
    // MUTATION: draw `.wf-inspect-times` under the step heading again.
    expect(card.querySelector('.wf-inspect-times')).toBeNull()
    const note = (document.querySelector<HTMLElement>('.c-count-note')?.textContent ?? '').replace(/\s+/g, ' ')
    expect(note).toMatch(/started/)
    // MUTATION: print `· sub ${submitted}` again (StepInspector's times line).
    expect(note).not.toMatch(/\bsub\b/)
  })

  it("words the same-step row's shape as the page head does: its steps, its stages, then the widths", async () => {
    const card = await openCard()
    // ONE WORKFLOW OF THIS SHAPE: nothing to page, so since QA1-FIX-29
    // (overview-agents-67) the shape is the sentence's title, not a line.
    const shape = card.querySelector<HTMLElement>('[data-scrub="workflow"] .wf-scrub-pos')!
    // MUTATION: print `${shape.text} · ${shape.steps} steps` again and the
    // row reads '1 → 1 · 2 steps', the bare widths QA-119 named in the head.
    expect(shape.getAttribute('title')).toBe('This workflow’s shape: 2 steps in 2 stages (1 → 1). Only workflows of this shape are compared.')
    expect(card.querySelector('[data-scrub="workflow"] .wf-scrub-shape')).toBeNull()
  })
})

describe("WG-7: the step card's changes link names the step, not the workflow's Changes tab", () => {
  it("reads This step's changes, and opens the Changes tab filtered to the step", async () => {
    const card = await openCard()
    // MUTATION: label the link 'Changes ›' again and there is no link of this name.
    const link = within(card).getByRole('link', { name: /^This step’s changes/ })
    expect(link.getAttribute('href')).toBe('/workflows/wf_tests/changes?step=test')
    // MUTATION: keep a second link on the card called just 'Changes' and this is red.
    expect(within(card).queryByRole('link', { name: /^Changes\b/ })).toBeNull()
    // The tab it filters keeps its own name.
    const tabs = within(document.body).getByRole('tablist', { name: 'Views of this workflow' })
    expect(within(tabs).getByRole('tab', { name: 'Changes' })).toBeTruthy()
  })
})
