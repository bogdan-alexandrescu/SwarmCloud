// LANE QA1-FIX-07 (part of #2596, the console visual QA pass 1 epic): the
// Artifacts pane's findings. A failed workflow step whose input copy is empty
// and whose run wrote no answer and no summary -- `task_fan_3` in the fixture
// console -- and a transcript of `Bash` calls.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

const { ArtifactsScreen, firstLine, toolGist } = await import('../Artifacts')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const masked = (text: string) => ({ text, redacted: false, redaction_count: 0 })

const ID = 'task_fan_3'

function failedStep(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'FAILED',
    runner_profile: 'claude-code',
    workflow_id: 'wf_5e5ad3b6f7da4299a839',
    step_id: 'checkpoints',
    created_at: new Date(Date.now() - 3_600_000).toISOString(),
    started_at: new Date(Date.now() - 3_000_000).toISOString(),
    completed_at: new Date(Date.now() - 2_400_000).toISOString(),
    result_summary: null,
    last_error: 'exit status 2: pytest was interrupted',
    ...over,
  })
}

function serve(t: Task) {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadAnswer.mockResolvedValue(
    ok({ task_id: t.id, status: 'absent', source: null, content: null, capture_truncated: false, detail: null } as unknown as TaskAnswer),
  )
  api.loadArtifactListing.mockResolvedValue(null)
  api.loadWorkflow.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'not read in this test' }, fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockResolvedValue(
    ok({ task_id: t.id, tenant_id: 'eng', read_at: new Date().toISOString(), prompt_key: 'missing', prompt: null, rest: null, full: masked('{}'), redacted: false, redaction_count: 0 } as unknown as TaskInputCopy),
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('QA1-FIX-07: the Artifacts pane', () => {
  it('overview-agents-50: a step with an empty input copy says its prompt was not recorded, and no `masked 0`', async () => {
    // MUTATION: drop the `step` branch in `Prompt` -> it reads `none · no input`.
    // MUTATION: draw `MaskedNote` at a count of 0 -> `masked 0` is back.
    serve(failedStep())
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(flat(document.querySelector('.arts-inputs .arts-block'))).toMatch(/^prompt ?not recorded/))
    const block = document.querySelector('.arts-inputs .arts-block')!
    expect(flat(block)).not.toMatch(/no input/)
    expect(flat(block)).not.toMatch(/masked/)
  })

  it('overview-agents-50: a lone agent submitted with nothing still says `no input`', async () => {
    serve(failedStep({ workflow_id: null, step_id: null }))
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(flat(document.querySelector('.arts-inputs .arts-block'))).toMatch(/no input/))
  })

  it('overview-agents-51: a failed agent’s outputs say it failed, not `not measured` or `partial`', async () => {
    // MUTATION: drop `failedRun` from `Answer` or `Files` -> `not measured` / `partial · finished with no summary`.
    serve(failedStep())
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(document.querySelector('.arts-answer .att-none')).not.toBeNull())
    await waitFor(() => expect(flat(document.querySelector('.arts-answer .att-none'))).toBe('failed no output recorded'))
    const outputs = flat(document.querySelector('.arts-inputs')?.nextElementSibling)
    expect(outputs).not.toMatch(/not measured/)
    expect(outputs).not.toMatch(/partial/)
    expect(outputs).not.toMatch(/finished with no summary/)
  })

  it('a succeeded agent with no summary keeps its partial record mark', async () => {
    serve(failedStep({ state: 'SUCCEEDED', last_error: null }))
    render(<ArtifactsScreen taskId={ID} />)
    await waitFor(() => expect(flat(document.querySelector('.arts-inputs')?.nextElementSibling)).toMatch(/finished with no summary/))
    expect(flat(document.querySelector('.arts-answer .att-none'))).toBe('not measured no answer recorded')
  })

  it('chrome-C07: a closed tool row names what it ran and the first line of its result', () => {
    // MUTATION: return null from `toolGist` or `firstLine` -> the closed rows read `tool · Bash` / `result` alone.
    expect(toolGist(JSON.stringify({ command: 'git status --short', description: 'Show the tree' }, null, 2))).toBe('git status --short')
    expect(toolGist(JSON.stringify({ file_path: '/repo/apps/swarm-ui/src/Agents.tsx', limit: 40 }))).toBe('/repo/apps/swarm-ui/src/Agents.tsx')
    expect(toolGist(JSON.stringify({ todos: 3, note: 'only string' }))).toBe('only string')
    expect(toolGist(null)).toBeNull()
    expect(toolGist('[1, 2]')).toBeNull()
    expect(firstLine('\n\n  M src/Agents.tsx\n?? x\n')).toBe('M src/Agents.tsx')
    expect(firstLine('')).toBeNull()
    expect(firstLine('x'.repeat(300))!.length).toBe(100)
  })
})
