// A CHECKPOINT THAT WAS WRITTEN AND IS GONE IS NOT A TASK THAT NEVER CHECKPOINTED.
//
// THE DEFECT, measured on the live inspector on 2026-09-24 for
// task_62dc9718bf1c4e3397c5. The run's figures said `Checkpoints 1` -- the
// attempt document records one -- and the Checkpoints section under them said,
// in consecutive lines:
//
//   "The task points at gs://.../ckpt-00001/ and the listing did not find it.
//    the pointer names a checkpoint of this task that is no longer in the
//    bucket; it may have been reclaimed"
//   "The listing succeeded and this task has written no checkpoint. A real zero."
//
// The second sentence is false, and the first one on the same screen says so.
// A listing that finds nothing is a measured zero of OBJECTS; it is only a
// zero of CHECKPOINTS WRITTEN when nothing else records one. Two records can:
// the task's `latest_checkpoint` pointer, and each attempt document's
// `checkpoints` list (which is what the `Checkpoints 1` figure counts). When
// either names a checkpoint the listing no longer finds, the absence has an
// explanation -- written, then reclaimed -- and "real zero" is a lie about the
// task's history.
//
// A LISTING THAT WAS CUT is the third case and is not a zero either: an empty
// page with more pages behind it, or a scan that hit its limit, measured
// nothing about what lies beyond it.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { CheckpointsPage, LatestCheckpointPointer } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const PREFIX = 'tenants/eng/tasks/tsk_charts/attempts/'
const POINTER = `gs://swarm-artifacts/${PREFIX}att_1/checkpoints/ckpt-00001/`

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' }
}

const UNSET: LatestCheckpointPointer = { pointer: null, status: 'unset', checkpoint_id: null }

/** The route's answer when the listing succeeded and found no object. */
function emptyPage(over: Partial<CheckpointsPage> = {}): CheckpointsPage {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'eng',
    prefix: PREFIX,
    checkpoints: [],
    count: 0,
    total_found: 0,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: UNSET,
    ...over,
  }
}

function agentRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1 }),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

/** The Checkpoints section of the inspector, once its listing has landed. */
async function checkpointsSection(run: AgentRun, page: CheckpointsPage): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue(ok(page))
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={run} />)
  let section: HTMLElement | undefined
  await waitFor(
    () => {
      section = [...container.querySelectorAll<HTMLElement>('section')].find(
        (s) => s.querySelector('h2')?.textContent === 'Checkpoints',
      )
      expect(section, 'the inspector drew no Checkpoints section').toBeTruthy()
      expect(section!.textContent).toMatch(/found/)
    },
    { timeout: 5000 },
  )
  return section!
}

describe('the Checkpoints section tells a reclaimed checkpoint from a real zero', () => {
  /**
   * THE LIVE CASE: the pointer names ckpt-00001, the attempt records it, and
   * the listing found nothing. MUTATION: restore the unconditional "A real
   * zero." branch and this goes red.
   */
  it('says written, then reclaimed, when the pointer names a checkpoint the listing lost', async () => {
    const section = await checkpointsSection(
      agentRun({
        task: task({ state: 'SUCCEEDED', latest_checkpoint: POINTER }),
        attempts: [attempt(1, { checkpoints: ['ckpt-00001'] })],
      }),
      emptyPage({
        latest_checkpoint: {
          pointer: POINTER,
          status: 'missing',
          checkpoint_id: 'ckpt-00001',
          detail:
            'the pointer names a checkpoint of this task that is no longer in the bucket; it may have been reclaimed',
        },
      }),
    )
    expect(section.textContent).toMatch(/written, then reclaimed/i)
    expect(section.textContent).toContain('ckpt-00001')
    expect(section.textContent, 'a reclaimed checkpoint was called a real zero').not.toMatch(
      /real zero/i,
    )
    expect(section.textContent).not.toMatch(/has written no checkpoint/i)
  })

  /**
   * THE ATTEMPT RECORD ALONE IS ENOUGH. A task can carry no pointer (a later
   * attempt cleared or never set it) while an attempt document still lists
   * what it wrote -- the figure above the section counts exactly that list.
   */
  it('says written, then reclaimed, when only an attempt record names one', async () => {
    const section = await checkpointsSection(
      agentRun({ attempts: [attempt(1, { checkpoints: ['ckpt-00002'] })] }),
      emptyPage(),
    )
    expect(section.textContent).toMatch(/written, then reclaimed/i)
    expect(section.textContent).toContain('ckpt-00002')
    expect(section.textContent).not.toMatch(/real zero/i)
  })

  /** The honest zero survives: nothing records a checkpoint, the list is whole. */
  it('still calls a complete, empty listing with nothing recorded a real zero', async () => {
    const section = await checkpointsSection(agentRun(), emptyPage())
    expect(section.textContent).toMatch(/real zero/i)
    expect(section.textContent).not.toMatch(/reclaimed/i)
  })

  /** An empty but CUT listing measured nothing past the cut. */
  it('never calls an empty page of a cut listing a zero', async () => {
    const section = await checkpointsSection(
      agentRun(),
      emptyPage({ truncated: true, next_page_token: 'more' }),
    )
    expect(section.textContent).not.toMatch(/real zero/i)
    expect(section.textContent).not.toMatch(/has written no checkpoint/i)
  })
})
