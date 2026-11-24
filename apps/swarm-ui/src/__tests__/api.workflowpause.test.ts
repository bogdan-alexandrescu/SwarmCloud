/**
 * WORKFLOW PAUSE AND RESUME, THE CALLS (docs/design/workflow-pause.md §3):
 * `POST /v1/workflows/{id}/pause` and `POST /v1/workflows/{id}/resume`, each
 * with an empty object for its body -- the console sends no reason, and the
 * resume route accepts no field at all. The live path: USE_FIXTURES is decided
 * when api.ts loads, so the switch is flipped before any import.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => {
  vi.stubEnv('VITE_LIVE', '1')
  return { write: vi.fn() }
})
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: mocks.write,
}))

const { pauseWorkflow, resumeWorkflow } = await import('../api')

beforeEach(() => {
  mocks.write.mockReset()
  mocks.write.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: {} })
})

describe('the workflow pause and resume calls', () => {
  it('pauses with POST /v1/workflows/{id}/pause and an empty body', async () => {
    await pauseWorkflow('wf_a/b')
    expect(mocks.write).toHaveBeenCalledTimes(1)
    expect(mocks.write).toHaveBeenCalledWith({ url: '/v1/workflows/wf_a%2Fb/pause', template: '/v1/workflows/{id}/pause' }, 'POST', {})
  })

  it('resumes with POST /v1/workflows/{id}/resume and an empty body', async () => {
    await resumeWorkflow('wf_broker')
    expect(mocks.write).toHaveBeenCalledTimes(1)
    expect(mocks.write).toHaveBeenCalledWith({ url: '/v1/workflows/wf_broker/resume', template: '/v1/workflows/{id}/resume' }, 'POST', {})
  })

  it('hands back the API’s own answer, a refusal included', async () => {
    const refused = {
      status: 'error',
      error: { kind: 'conflict', httpStatus: 409, code: 'workflow_terminal', message: 'the workflow has finished' },
    }
    mocks.write.mockResolvedValueOnce(refused)
    expect(await pauseWorkflow('wf_done')).toEqual(refused)
  })
})
