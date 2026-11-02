// JEV'S TRIAGE AND THE QUALITY GATES, MADE VISIBLE (CLD's Jev report, the
// owner's choice on 2026-11-02 to build every improvement proposal).
//
//   * Each finding a review gave shows Jev's triage as a chip: "likely real"
//     with the percentage, or "verify before acting" under 0.15, and nothing
//     at all for a finding Jev did not judge (never 0%). The numbers come from
//     the review's own verdict.json (agent_worker/verdict.py
//     `annotate_findings`), read through the artifact route; the gate's
//     record keeps findings as text.
//   * The agent's Details shows one Quality gates line when the attempt's
//     summary has `quality_gates` (agent_worker/lifecycle.py
//     `_record_quality_gates`): stops blocked, rules flagged, Jev calls, or why
//     they did not run; nothing when it has none.
//
// CONTROL: each case asserts a value only its own fixture carries (62%, the
// refusal's words, the counts), so a chip or a line drawn from nothing goes red.

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, WorkflowRead } from '../api'
import type { Task, Workflow } from '../types'
import { at, attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')
const { qualityGatesOf, verdictTriage } = await import('../api')

const WF = 'wf_triage'
const REVIEW = 'task_review00000000000009'
const FIX = 'task_fix00000000000000009'

const MARK = '[verify before acting: Jev judged this unlikely to be real] '

/** The review's verdict.json as Jev's post-step left it: 0.62, 0.08 with its mark, and one not judged. */
const VERDICT = {
  verdict: 'NOT_YET',
  findings: [
    { summary: 'the lease is released twice', p_real: 0.62 },
    { summary: 'the cap counts RUNNING', p_real: 0.08, triage: 'verify before acting' },
    'the docstring names the wrong route',
  ],
}

function reviewTask(): Task {
  return runTask({
    id: REVIEW,
    workflow_id: WF,
    step_id: 'review',
    state: 'SUCCEEDED',
    started_at: at(1),
    completed_at: at(5),
    result_summary: { artifacts: [{ name: 'verdict.json', bytes: 400, uri: 'gs://b/verdict.json' }] },
  })
}

/** The gated fix step, with its gate record as the worker keeps it: text, the low one marked. */
function fixTask(summary: Record<string, unknown> = {}): Task {
  return runTask({
    id: FIX,
    workflow_id: WF,
    step_id: 'fix',
    state: 'SUCCEEDED',
    started_at: at(6),
    completed_at: at(8),
    dispatch: { verdict_gate: { task_id: REVIEW, verdict_in: ['NOT_YET'] } } as unknown as Task['dispatch'],
    result_summary: {
      verdict_gate: {
        task_id: REVIEW,
        file: 'verdict.json',
        verdict: 'NOT_YET',
        verdict_in: ['NOT_YET'],
        agent_ran: true,
        findings: ['the lease is released twice', `${MARK}the cap counts RUNNING`, 'the docstring names the wrong route'],
        findings_dropped: 0,
      },
      ...summary,
    },
  })
}

function plainTask(summary: Record<string, unknown> | null): Task {
  return runTask({ id: 'task_plain000000000000001', state: 'SUCCEEDED', started_at: at(1), completed_at: at(3), result_summary: summary })
}

function workflowRead(tasks: Task[]): WorkflowRead {
  const workflow = {
    workflow_id: WF,
    tenant_id: 'acme',
    state: 'SUCCEEDED',
    steps: tasks.map((t) => ({ step_id: t.step_id, task_id: t.id, depends_on: [], input_from: {}, runner_profile: 'claude-code', resource_class: 'standard' })),
  } as unknown as Workflow
  return { workflow, tasks }
}

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { task_id: t.id })],
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function verdictContent(body: unknown) {
  const content = JSON.stringify(body)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: REVIEW,
      tenant_id: 'acme',
      attempt_id: 'att_1',
      artifact: { name: 'verdict.json', bytes: content.length, uri: null },
      status: 'ok',
      detail: null,
      key: null,
      uri: null,
      content,
      total_bytes: content.length,
      offset: 0,
      returned_bytes: content.length,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 0 },
    },
  }
}

beforeEach(() => {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockImplementation((id: string, name: string) =>
    Promise.resolve(id === REVIEW && name === 'verdict.json' ? verdictContent(VERDICT) : { status: 'empty', fetchedAt: Date.now() }),
  )
  api.loadWorkflow.mockResolvedValue({ status: 'ok', data: workflowRead([reviewTask(), fixTask()]), fetchedAt: Date.now() })
})

afterEach(() => {
  vi.clearAllMocks()
})

function findingItems(): HTMLElement[] {
  const card = screen.getByRole('region', { name: 'Decision' })
  return Array.from(card.querySelectorAll<HTMLElement>('li.dc-finding'))
}

describe("each finding shows Jev's triage as a chip", () => {
  it('p_real 0.62 reads "likely real 62%", 0.08 with its mark "verify before acting", and an unjudged one has no chip', async () => {
    render(<Run run={run(fixTask())} />)
    await waitFor(() => expect(findingItems()[0]?.textContent).toContain('likely real 62%'))
    const [real, low, none] = findingItems()
    expect(api.loadArtifactContent).toHaveBeenCalledWith(REVIEW, 'verdict.json')
    expect(real?.querySelectorAll('.triage-chip')).toHaveLength(1)
    expect(low?.textContent).toContain('verify before acting')
    expect(low?.textContent).not.toContain('likely real')
    // The worker's text mark is the chip now, not a prefix on the finding.
    expect(low?.textContent).not.toContain('Jev judged this unlikely')
    expect(low?.querySelector('.triage-chip')?.getAttribute('title')).toContain('8%')
    expect(none?.textContent).toContain('the docstring names the wrong route')
    expect(none?.querySelector('.triage-chip')).toBeNull()
    expect(none?.textContent).not.toMatch(/0%|likely real|verify before acting/)
  })

  it("the review's own page shows the same chips on the verdict it wrote", async () => {
    render(<Run run={run(reviewTask())} />)
    await waitFor(() => expect(findingItems()[0]?.textContent).toContain('likely real 62%'))
    expect(api.loadArtifactContent).toHaveBeenCalledWith(REVIEW, 'verdict.json')
    expect(findingItems()[1]?.textContent).toContain('verify before acting')
    expect(findingItems()[2]?.querySelector('.triage-chip')).toBeNull()
  })

  it('without the verdict file, the low finding still carries the worker\'s mark as its chip, and no percentage is invented', async () => {
    api.loadArtifactContent.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    render(<Run run={run(fixTask())} />)
    await waitFor(() => expect(findingItems()[1]?.textContent).toContain('verify before acting'))
    expect(findingItems()[0]?.querySelector('.triage-chip')).toBeNull()
    expect(findingItems()[1]?.textContent).not.toMatch(/\d%/)
  })

  it("indexes the verdict file's findings as the gate recorded them: a blank finding is skipped", () => {
    const t = verdictTriage(JSON.stringify({ verdict: 'MERGE', findings: ['  ', { summary: 'a', p_real: 0.5 }, { title: 'b' }, 7] }))
    expect(t).toEqual([{ pReal: 0.5, verify: false }, null])
    expect(verdictTriage('not json')).toBeNull()
  })
})

describe("the agent's Quality gates line", () => {
  it('gates that blocked once and flagged two rules', async () => {
    const gates = { jev: true, belay_runs: 3, belay_calls: 5, belay_blocks: 1, enforce_runs: 2, enforce_calls: 4, enforce_flags: 1, enforce_rules_flagged: 2 }
    render(<Run run={run(plainTask({ quality_gates: gates }))} />)
    const line = await screen.findByRole('region', { name: 'Quality gates' })
    expect(line.textContent).toContain('1 stop blocked')
    expect(line.textContent).toContain('2 rules flagged')
    expect(line.textContent).toContain('9 Jev calls')
  })

  it('on a phone the counts wrap: they sit in a wrapping row of their own, not in the head\'s inline slot', async () => {
    const gates = { jev: true, belay_runs: 3, belay_calls: 5, belay_blocks: 1, enforce_runs: 2, enforce_calls: 4, enforce_flags: 1, enforce_rules_flagged: 2 }
    render(<Run run={run(plainTask({ quality_gates: gates }))} />)
    const line = await screen.findByRole('region', { name: 'Quality gates' })
    expect(line.querySelector('.dt-card-r')).toBeNull()
    const counts = line.querySelector('[data-testid="quality-gates-body"]')
    expect(counts?.classList.contains('dt-phases')).toBe(true)
    expect(counts?.textContent).toContain('9 Jev calls')
  })

  it('refused: says they did not run, and why, in words', async () => {
    render(<Run run={run(plainTask({ quality_gates: { jev: false, refused: 'mac_mismatch' } }))} />)
    const line = await screen.findByRole('region', { name: 'Quality gates' })
    expect(line.textContent).toContain('did not run')
    expect(line.textContent).toContain("refused: the workspace's setting does not match its signature")
    expect(line.textContent).not.toMatch(/blocked|flagged/)
  })

  it('off by the workspace: says off, with the reason the worker recorded', () => {
    const t = plainTask({ quality_gates: { jev: false, switch: { switch: 'quality_gates', value: 'off', on: false, reason: 'quality gates are off (workspace setting)' } } })
    expect(qualityGatesOf(t)).toEqual({ kind: 'off', reason: 'quality gates are off (workspace setting)' })
    render(<Run run={run(t)} />)
    expect(screen.getByRole('region', { name: 'Quality gates' }).textContent).toContain('quality gates are off (workspace setting)')
  })

  it('absent: nothing at all when the summary has no quality_gates', async () => {
    render(<Run run={run(plainTask({ runner: { status: 'succeeded', summary: 'done' } }))} />)
    await waitFor(() => expect(document.querySelector('.dt')).not.toBeNull())
    expect(screen.queryByRole('region', { name: 'Quality gates' })).toBeNull()
    expect(screen.queryByTestId('quality-gates')).toBeNull()
    expect(document.body.textContent).not.toContain('Quality gates')
  })
})
