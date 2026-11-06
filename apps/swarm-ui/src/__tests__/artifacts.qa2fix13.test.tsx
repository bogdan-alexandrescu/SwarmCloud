// LANE QA2-FIX-13 (part of #2694, the console visual QA pass 2 epic):
// overview-agents-50, still open from pass 1. Inputs › prompt read `not
// measured · prompt not recorded` for a workflow step that ran the prompt its
// workflow gave it. The pane already reads the workflow (`loadWorkflow`, for
// its staged files), and the workflow route serves each step's input, masked
// by the step's task's masker -- so the step's prompt is drawn from there,
// labelled as its workflow's. With none there either, the absence is said as
// `not recorded` (a prompt is a record, not a measurement) with the way to the
// workflow's page.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { WorkflowRead } from '../api'
import type { Result } from '../fetch'
import type { Task, TaskAnswer, TaskInputCopy } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadAnswer: vi.fn(),
  loadArtifactListing: vi.fn(),
  loadWorkflow: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { ArtifactsScreen } = await import('../Artifacts')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const masked = (text: string) => ({ text, redacted: false, redaction_count: 0 })

const ID = 'task_fan_3'
const WF = 'wf_5e5ad3b6f7da4299a839'
const PROMPT = 'Write the checkpoint tests first, then make them pass.'

function step(): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    workflow_id: WF,
    step_id: 'checkpoints',
    created_at: new Date(Date.now() - 3_600_000).toISOString(),
    completed_at: new Date(Date.now() - 2_400_000).toISOString(),
  })
}

function workflow(input: unknown): Result<WorkflowRead> {
  return ok({
    workflow: {
      workflow_id: WF,
      steps: [
        { step_id: 'plan', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 'task_fan_1', input: { prompt: 'not this step' } },
        { step_id: 'checkpoints', runner_profile: 'claude-code', resource_class: 'standard', depends_on: ['plan'], input_from: {}, task_id: ID, input },
      ],
    },
    tasks: [],
  } as unknown as WorkflowRead)
}

function serve(t: Task, wf: Result<WorkflowRead>) {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadAnswer.mockResolvedValue(
    ok({ task_id: t.id, status: 'absent', source: null, content: null, capture_truncated: false, detail: null } as unknown as TaskAnswer),
  )
  api.loadArtifactListing.mockResolvedValue(null)
  api.loadWorkflow.mockResolvedValue(wf)
  api.loadTaskInputOnce.mockResolvedValue(
    ok({ task_id: t.id, tenant_id: 'eng', read_at: new Date().toISOString(), prompt_key: 'missing', prompt: null, rest: null, full: masked('{}'), redacted: false, redaction_count: 0 } as unknown as TaskInputCopy),
  )
}

const block = () => document.querySelector('.arts-inputs .arts-block')

afterEach(() => {
  vi.clearAllMocks()
})

describe('overview-agents-50: a workflow step’s prompt', () => {
  it('is drawn from its workflow’s step, labelled as the workflow’s', async () => {
    serve(step(), workflow({ prompt: PROMPT }))
    render(<ArtifactsScreen taskId={ID} />)
    // MUTATION: ignore the workflow read in `Prompt` -> `prompt not recorded` beside a prompt the route served.
    await waitFor(() => expect(flat(block())).toContain(PROMPT))
    expect(flat(block())).toContain('from its workflow')
    expect(flat(block())).not.toContain('not recorded')
    expect(flat(block())).not.toContain('not this step')
  })

  it('says `not recorded`, never `not measured`, and links the workflow’s page when its step has no prompt either', async () => {
    serve(step(), workflow({}))
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(flat(block())).toMatch(/^prompt ?not recorded/))
    // MUTATION: put back `<Mark kind="absent">` -> `not measured · prompt not recorded`.
    expect(flat(block())).not.toContain('not measured')
    const link = block()!.querySelector('a')
    expect(link?.getAttribute('href')).toBe(`#work/workflows?wf=${WF}`)
    expect(flat(link)).toBe('the workflow’s steps')
    expect(flat(block())).not.toMatch(/not recorded.*not recorded/)
  })

  it('says `not recorded` when the workflow could not be read (control)', async () => {
    serve(step(), { status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'not read in this test' } } as Result<WorkflowRead>)
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(flat(block())).toMatch(/^prompt ?not recorded/))
    expect(flat(block())).not.toContain('not measured')
  })
})
