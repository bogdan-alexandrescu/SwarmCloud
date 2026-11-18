/**
 * The Checkpoints tab is the only checkpoint panel since Details dropped its
 * copy, so it must read the attempt records itself: they are what tells
 * "written, then reclaimed" from a real zero (RunFiles' `lostCheckpoints`).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ loadTask: vi.fn(), loadAttempts: vi.fn() }))
vi.mock('../api', () => api)

import { loadCheckpointsRead } from '../CheckpointsPane'

const TASK = { task_id: 't-1' }
const ROW = { attempt_id: 'att_1' }

beforeEach(() => {
  api.loadTask.mockReset()
  api.loadAttempts.mockReset()
})

describe('the Checkpoints tab reads the attempt records with the task', () => {
  it('hands both to RunFiles when both reads land', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: TASK, fetchedAt: 1 })
    api.loadAttempts.mockResolvedValue({ status: 'ok', data: { attempts: [ROW] }, fetchedAt: 1 })
    const r = await loadCheckpointsRead('t-1')
    expect(api.loadAttempts).toHaveBeenCalledWith('t-1')
    expect(r).toMatchObject({ status: 'ok', data: { task: TASK, attempts: [ROW] } })
  })

  it('passes null, not an empty list, when the attempts read failed', async () => {
    api.loadTask.mockResolvedValue({ status: 'ok', data: TASK, fetchedAt: 1 })
    api.loadAttempts.mockResolvedValue({ status: 'error', error: { kind: 'network' } })
    const r = await loadCheckpointsRead('t-1')
    expect(r).toMatchObject({ status: 'ok', data: { task: TASK, attempts: null } })
  })

  it('returns the task read unchanged when the task itself did not load', async () => {
    const failed = { status: 'error', error: { kind: 'network' } }
    api.loadTask.mockResolvedValue(failed)
    api.loadAttempts.mockResolvedValue({ status: 'ok', data: { attempts: [ROW] }, fetchedAt: 1 })
    expect(await loadCheckpointsRead('t-1')).toBe(failed)
  })
})
