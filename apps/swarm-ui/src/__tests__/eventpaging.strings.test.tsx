// THE EVENTS ROUTE PAGES; THIS SCREEN READS ONE PAGE. Say the second, not "it cannot".
//
// THE DEFECT. Since #19, `GET /v1/tasks/{id}/events` returns `next_page_token`
// whenever more events exist and accepts `order=desc`
// (swarm_api/routes/tasks.py, `list_events`). #145 rewrote the help topic
// `event-paging` to say what is true -- this screen reads one page,
// oldest-first, and does not follow the token -- and left the older claim in
// the places that feed it: the checkpoint-location mark and the
// missing-terminal-event mark in the inspector, the peak-memory chart's
// `page full` flag, and the comments in types.ts, duration.ts, api.ts,
// AttemptTimeline.tsx and the checkpoint strip. Each said the route "returns
// no page token", which blames the platform for a limit that is this
// screen's, and sends a reader to file a bug against an API that already does
// the thing. The timeline's own qualifier said the screen "asks for no page
// size", which went false when the reads started sending `limit=200`.
//
// WHAT IS ASSERTED. The rendered sentences say what the topic says, and no
// sentence or comment in the files that repeated it still says the old thing
// -- WHILE the route still pages. If `list_events` ever stops returning a
// token, the premise is gone and this fails by saying so, rather than holding
// the screen to a claim that became false the other way.
//
// MUTATION: restore "returns no page token" in any of the rendered sentences
// or any of the scanned files.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import { EVENT_PAGE_LIMIT } from '../api'
import { PeakMemoryChart } from '../charts/PeakMemory'
import type { TaskEvent } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { Run } from '../AgentDetail'

const SRC = join(__dirname, '..')

/** The old claim, in every spelling these files used for it. */
const STALE = /no page token|asks for no page size|sends no `limit`/i
/** What `event-paging` says, and what each sentence must say instead. */
const TRUE = /does not follow the page token/i

function routePages(): boolean {
  const route = readFileSync(join(SRC, '..', '..', 'swarm-api', 'swarm_api', 'routes', 'tasks.py'), 'utf8')
  const start = route.indexOf('def list_events(')
  expect(start, 'routes/tasks.py has no list_events; this check is reading the wrong file').toBeGreaterThanOrEqual(0)
  const end = route.indexOf('\n@router', start)
  const body = route.slice(start, end === -1 ? undefined : end)
  return body.includes('page_token') && body.includes('"next_page_token"')
}

function run(over: Partial<AgentRun>): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1, completed_at: at(12) }),
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [attempt(1)],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

async function mount(r: AgentRun): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={r} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull())
  return container as HTMLElement
}

describe('the premise: the events route pages', () => {
  it('returns next_page_token from list_events', () => {
    expect(routePages(), 'list_events no longer pages; the sentences below are held to the wrong claim').toBe(true)
  })
})

describe('the rendered sentences put the one-page limit on this screen', () => {
  it('the checkpoint-location mark, on a checkpoint whose event is off the page', async () => {
    const el = await mount(run({ attempts: [attempt(1, { checkpoints: ['ck_1'] })] }))
    const mark = el.querySelector('td[data-label="Location"] .ctl-mark.is-partial')
    expect(mark, 'the off-page checkpoint drew no partial mark').not.toBeNull()
    const say = mark!.getAttribute('aria-label') ?? ''
    expect(say).not.toMatch(STALE)
    expect(say).toMatch(TRUE)
  })

  it('the missing-terminal-event mark, on a finished task whose ending is off the page', async () => {
    const el = await mount(run({}))
    const marks = [...el.querySelectorAll('.ctl-toolbar .ctl-mark.is-partial')].filter((m) =>
      /terminal event/i.test(m.getAttribute('aria-label') ?? ''),
    )
    expect(marks, 'the finished task with no terminal event drew no partial mark').toHaveLength(1)
    const say = marks[0]!.getAttribute('aria-label') ?? ''
    expect(say).not.toMatch(STALE)
    expect(say).toMatch(TRUE)
  })

  it('the timeline\'s paging qualifier, where nothing proves the page short', async () => {
    const el = await mount(
      run({
        task: task({ state: 'RUNNING', attempt_count: 1, started_at: at(1) }),
        attempts: [attempt(1, { completed_at: null, exit_code: null })],
      }),
    )
    const note = [...el.querySelectorAll('.ctl-toolbar .ctl-card-note')].find((n) =>
      (n.textContent ?? '').includes('cap unknown'),
    )
    expect(note, 'the timeline toolbar lost its paging qualifier').toBeTruthy()
    const say = note!.getAttribute('aria-label') ?? ''
    expect(say, 'the qualifier says the screen sends no page size; it sends limit=200').not.toMatch(STALE)
    expect(say).toMatch(TRUE)
  })

  it('the peak-memory chart\'s `page full` flag', () => {
    const a = attempt(1, { started_at: at(1), completed_at: at(20) })
    const events: TaskEvent[] = [
      ev('heartbeat', at(3), 'att_1', { peak_rss_bytes: 1_000_000 }),
      ...Array.from({ length: EVENT_PAGE_LIMIT }, (_, i) => ev('checkpoint_started', at(1 + i / 1000), 'att_1')),
    ]
    const { container } = render(<PeakMemoryChart attempt={a} events={events} />)
    const flag = container.querySelector('.ctl-chart-flag')
    expect(flag?.textContent).toBe('page full')
    const say = flag!.getAttribute('aria-label') ?? ''
    expect(say).not.toMatch(STALE)
    expect(say).toMatch(TRUE)
  })
})

describe('no file that repeated the old claim still makes it', () => {
  const FILES = [
    'AgentDetail.tsx',
    'AttemptTimeline.tsx',
    'api.ts',
    'duration.ts',
    'types.ts',
    join('charts', 'PeakMemory.tsx'),
    join('charts', 'CheckpointStrip.tsx'),
  ]

  it('scans all seven, comments included', () => {
    expect(routePages()).toBe(true)
    const visited: string[] = []
    const found: string[] = []
    for (const f of FILES) {
      const text = readFileSync(join(SRC, f), 'utf8')
      expect(text.length, `${f} is empty; the scan would pass over nothing`).toBeGreaterThan(0)
      visited.push(f)
      text.split('\n').forEach((line, i) => {
        if (STALE.test(line)) found.push(`${f}:${i + 1}: ${line.trim()}`)
      })
    }
    expect(visited).toHaveLength(FILES.length)
    expect(found, 'these lines still say the events route cannot page').toEqual([])
  })
})
