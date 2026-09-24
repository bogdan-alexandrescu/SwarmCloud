// A cancel that is only REQUESTED is not the end of a run.
//
// Contract request 17 (docs/contract-change-requests.md), accepted by the
// owner on 2026-09-24. The API's flag-only cancel used to be written as
// `type: cancelled` with `detail.phase: cancel_requested`, and the timeline
// counted every `cancelled` as a terminal event. So a CANCELLED task whose
// page held only that REQUEST -- the reconciler's or the worker's real
// `cancelled` off the end of the page -- read as a complete history, and the
// "the end of this task's history is missing" mark could not fire.
//
// The API now writes `cancel_requested` and serves stored history in that
// vocabulary (`swarm_api.codec.event_from_dict`). The screen still reads the
// old shape correctly, because the console can meet an API image older than
// itself during a rollout. Each case below is a CANCELLED task on the real
// `Run` component; only the events differ.

import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { TaskEvent } from '../types'
import { at, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { Run } from '../AgentDetail'

function cancelledRun(events: TaskEvent[]): AgentRun {
  return {
    task: task({ state: 'CANCELLED', cancel_requested: true, completed_at: at(90) }),
    events,
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function timelineNote(events: TaskEvent[]): Promise<{ text: string; rows: string[] }> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={cancelledRun(events)} />)
  await waitFor(() => expect(container.querySelector('ol.timeline')).not.toBeNull())
  // Several panels on this page carry an `.is-end` note; the timeline's is the
  // one in the toolbar under the "Timeline" heading.
  const heading = Array.from(container.querySelectorAll('.ctl-toolbar h2')).find((h) =>
    (h.textContent ?? '').startsWith('Timeline'),
  )
  const note = heading?.parentElement?.querySelector('.is-end') ?? null
  expect(note, 'the timeline toolbar note is not rendered').not.toBeNull()
  const rows = Array.from(container.querySelectorAll('.timeline .ev-type')).map(
    (el) => el.textContent ?? '',
  )
  return { text: note!.textContent ?? '', rows }
}

const REQUESTED_BY = { requested_by: 'alice@saga.xyz', from_state: 'DISPATCHED' }

describe('a cancel request on the timeline', () => {
  it('does not count a stored legacy request as the end of a cancelled task', async () => {
    // check-2 of wf_ebb3ab2d65664707a559, as an older API serves it.
    const { text, rows } = await timelineNote([
      ev('submitted', at(0), null),
      ev('cancelled', at(10), null, { ...REQUESTED_BY, phase: 'cancel_requested' }),
    ])
    expect(text, 'a cancel REQUEST was read as the terminal event').toContain('ends at')
    expect(rows).toEqual(['submitted', 'cancel_requested'])
  })

  it('does not count a cancel_requested event as the end either', async () => {
    const { text, rows } = await timelineNote([
      ev('submitted', at(0), null),
      ev('cancel_requested', at(10), null, { ...REQUESTED_BY, phase: 'cancel_requested' }),
    ])
    expect(text).toContain('ends at cancel_requested')
    expect(rows).toEqual(['submitted', 'cancel_requested'])
  })

  it('reads the real cancel that follows the request as the end', async () => {
    const { text, rows } = await timelineNote([
      ev('submitted', at(0), null),
      ev('cancel_requested', at(10), null, { ...REQUESTED_BY, phase: 'cancel_requested' }),
      ev('cancelled', at(90), null, { source: 'reconciler', phase: 'cancelled' }),
    ])
    expect(text).not.toContain('ends at')
    expect(rows).toEqual(['submitted', 'cancel_requested', 'cancelled'])
  })

  it("still reads the scheduler's cascade cancel, which carries no phase, as the end", async () => {
    const { text, rows } = await timelineNote([
      ev('submitted', at(0), null),
      ev('cancelled', at(5), null, { reason: 'an upstream workflow step did not succeed' }),
    ])
    expect(text).not.toContain('ends at')
    expect(rows).toEqual(['submitted', 'cancelled'])
  })
})
