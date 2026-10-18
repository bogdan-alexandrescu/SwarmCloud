// ONE LOADER FOR `GET /v1/tasks/{id}/live/recordings` (lane LB-UX2a, part of
// #1030). The Attempts tab's player (Recordings.tsx) and the theater's replay
// each had their own read of the route, against two shapes; LB-SEAM made the
// server answer both. Two readers of one route are how the two shapes came
// about, so both now go through `loadLiveRecordings` in api.ts, and this file
// holds that: the loader is replaced by a spy, and each view must call it.
//
// MUTATION: put a private `read(route('/v1/tasks/{id}/live/recordings'...))`
// back in either file and its case below fails -- the spy is never called.

import { act, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const spy = vi.hoisted(() => ({ calls: [] as string[] }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    USE_FIXTURES: false,
    loadLiveRecordings: (taskId: string) => {
      spy.calls.push(taskId)
      return actual.loadLiveRecordings(taskId)
    },
  }
})

import { RecordingsPanel } from '../Recordings'
import { TheaterHost } from '../Theater'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

async function flush(): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

describe('the recordings route has one loader', () => {
  it('the Attempts tab player reads through loadLiveRecordings', async () => {
    spy.calls.length = 0
    vi.stubGlobal('fetch', vi.fn(async () => json({ tenant_id: 'eng', task_id: 'task_a', recordings: [], segments: [] })))
    render(<RecordingsPanel task={{ id: 'task_a', tenant_id: 'eng' }} />)
    await flush()
    expect(spy.calls).toEqual(['task_a'])
  })

  it('the theater replay reads through loadLiveRecordings', async () => {
    spy.calls.length = 0
    vi.stubGlobal('fetch', vi.fn(async () => json({ code: 'not_found', message: 'Not found.' }, 404)))
    render(
      <TheaterHost
        session={{ taskId: 'task_b', tenantId: 'eng', attemptId: '', sessionId: 's', screen: null, live: true }}
        agents={[]}
        initiallyOpen
      >
        <div />
      </TheaterHost>,
    )
    await flush()
    expect(spy.calls).toContain('task_b')
  })
})
