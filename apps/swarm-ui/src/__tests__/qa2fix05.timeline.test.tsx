// QA2-FIX-05, THE TIMELINE'S LANES PAGE (part of #2694, the console visual QA
// pass 2 epic, 2026-10-29):
//
//   runs2-04  'Outcomes Oct 6 – Oct 19: 91 % success' beside lanes over Oct
//          28–29: figures from another window, the same on every span. Over
//          a span they do not cover they are a dash and the window they are for.
//   runs2-15 / runs2-25  the head said '25 lanes' and the stat 'events not
//          read on 18 of 24 lanes'. One count, and 'not counted' said plainly.
//   runs2-16  a running workflow's span ran to the track's edge under 'now'.
//   runs2-21  after 14d → 24h the 14d lanes and counts stood under the 24h
//          dates until the new read landed.
//   runs2-22  24h reached from 14d drew 14d's workflow as a never-ran lane.
//   runs2-26  Escape on the lane panel left focus nowhere (the page's h1).
//   runs2-27  standalone lanes read 'claude-c… · 108ef29c'.
//
// Every assertion names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import { ledgerFixture } from '../outcomes.fixture'
import type { Outcomes } from '../outcomes'
import type { WorkflowRead } from '../api'
import type { AttemptRow, AttemptsPage, Task, TaskPage } from '../types'
import { byIdVia } from './workflowsById'
import TIMELINE_CSS from '../styles/timeline.css?raw'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { TimelineLanesScreen, bracketStyle, laneName } = await import('../TimelineLanes')

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const failed = <T,>(httpStatus: number, message: string): Result<T> => ({
  status: 'error',
  error: { kind: 'server', httpStatus, code: null, message } as never,
})
const NOW = Date.now()
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()

const TASK = {
  id: 'task_solo', tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard', provider: null,
  priority: 0, created_at: ago(300), updated_at: ago(100), started_at: ago(290), completed_at: ago(100),
  submitted_by: 'operator@example.com', attempt_count: 1, max_attempts: 3, park_reason: null, blocked_by: null,
  workflow_id: null, step_id: null, depends_on: null, cancel_requested: false, repository_url: null,
  current_generation: 1, current_lease_id: null,
} as unknown as Task
const WF_ID = 'wf_5e5ad3b6f7da4299a839'
const STEP = { ...TASK, id: 'task_fan_0', step_id: 'fan_0', workflow_id: WF_ID, metadata: { unit: 'Check the five contract invariants' } } as unknown as Task
const NEVER = { ...STEP, id: 'task_synth', step_id: 'synthesis', state: 'CANCELLED', attempt_count: 0, started_at: null } as unknown as Task
const attempt = (taskId: string, min = 280) =>
  ({
    attempt_id: `att_${taskId}_1`, task_id: taskId, tenant_id: 'eng', generation: 1, lease_id: 'l1',
    backend: 'cloud_run', execution_name: null, created_at: ago(min), started_at: ago(min - 1), completed_at: ago(min - 180), exit_code: 0,
    error: null, peak_rss_bytes: null, peak_disk_bytes: null, oom_near_miss: false, checkpoints: [], input_tokens: null,
    output_tokens: null, cache_read_input_tokens: null, cache_creation_input_tokens: null, cost_usd: null,
  }) as unknown as AttemptRow
const page = (attempts: AttemptRow[]) =>
  ({ tenant_id: 'eng', read_at: new Date(NOW).toISOString(), attempts, next_page_token: null, coverage: { scope: 'page' } }) as AttemptsPage
const WF = {
  workflow: {
    workflow_id: WF_ID, tenant_id: 'eng', state: 'FAILED',
    steps: [{ step_id: 'fan_0', task_id: STEP.id, depends_on: [] }, { step_id: 'synthesis', task_id: NEVER.id, depends_on: ['fan_0'] }],
  },
  tasks: [STEP, NEVER],
} as unknown as WorkflowRead
/** A ledger over the last 24 hours: the figures cover the default span. */
const day = (): Outcomes => ({ ...ledgerFixture(), tz: 'UTC', bucket: 'hour', since: ago(24 * 60), until: ago(0), generated_at: ago(0) })

beforeEach(() => {
  api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
  api.loadTaskEventsPage.mockImplementation(async () => ok({ events: [], next_page_token: null }))
  api.loadTasks.mockResolvedValue(ok({ tasks: [TASK] } as TaskPage))
  api.loadTask.mockImplementation(async (id: string) => failed(404, `no ${id}`))
  api.loadWorkflow.mockImplementation(async (id: string) => (id === WF_ID ? ok(WF) : failed(404, `no ${id}`)))
  api.loadOutcomes.mockResolvedValue(ok(day()))
  api.loadAttemptsPage.mockResolvedValue(ok(page([attempt('task_solo')])))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const text = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const strip = () => document.querySelector('.tl-strip') as HTMLElement
const cells = () => [...document.querySelectorAll('.tl-strip1 > .tl-x')]
const cell = (re: RegExp) => cells().find((el) => re.test(text(el))) ?? null
const body = () => text(document.body)
const head = () => text(document.querySelector('.c-count-note'))
const lanes = () => [...document.querySelectorAll<HTMLElement>('.tl-row[data-lane]')]

describe('runs2-04: outcomes figures are shown only over the span they cover', () => {
  it('puts a dash and the window the read answered for in place of figures from another window', async () => {
    api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(cell(/not this span, read for/)).not.toBeNull())
    // MUTATION: lead the ledger's figures with their window (the code before
    // this fix) and '91 % success' over Sep 12 – Sep 25 sits beside 24h lanes.
    expect(text(strip())).not.toMatch(/% success|decided|finished/)
    expect(document.querySelector('.tl-strip .tl-n-ended')).toBeNull()
    expect(text(cell(/not this span, read for/))).toBe('outcomes — not this span, read for Sep 12 – Sep 25')
    // MUTATION: keep footnoting the mismatch, 'Decided counts cover Sep 12 …; the lanes cover …'.
    expect(text(strip())).not.toMatch(/Decided counts cover/)
    expect(text(strip())).toMatch(/The lanes cover .+\. Parks and fences count only the lanes drawn\./)
  })

  it('keeps the figures over a ledger that covers the span', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(cell(/% success/)).not.toBeNull())
    // MUTATION: hide the figures whatever the window and the span's own outcomes are lost.
    expect(text(cell(/% success/))).toMatch(/^\d+ % success, \d+ of \d+ decided$/)
    expect(text(strip())).toMatch(/Decided counts cover/)
  })
})

describe('runs2-15 / runs2-25: one lane count, and "not counted" said plainly', () => {
  it('counts the stat out of the head’s lanes, never-ran step included, without "not read"', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [TASK, STEP] } as TaskPage))
    api.loadAttemptsPage.mockResolvedValue(ok(page([attempt('task_solo'), attempt('task_fan_0')])))
    api.loadTaskEventsPage.mockResolvedValue(failed(500, 'boom'))
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lanes()).toHaveLength(3))
    await waitFor(() => expect(cell(/not counted/)).not.toBeNull())
    expect(head()).toMatch(/· 3 lanes$/)
    // MUTATION: count the stat over the lanes that ran ('2 of 2') beside a head of 3 lanes.
    // MUTATION: say 'events not read … open a lane to read its events' again.
    expect(text(cell(/not counted/))).toBe('fenced and parks — not counted on 2 of 3 lanes · open a lane to count')
    expect(text(strip())).not.toMatch(/not read/)
  })
})

describe('runs2-16: a group’s span stops short of the track’s edge at now', () => {
  it('caps the span’s width at the track less TRACK_END_PX', () => {
    // MUTATION: draw `width: ${width}%` (the code before this fix) and a span
    // reaching now runs its right edge under the 'now' rule and the border.
    expect(bracketStyle({ left: 40, width: 60 })).toEqual({ left: '40%', width: 'min(60%, calc(60% - 4px))' })
    expect(bracketStyle({ left: 10, width: 20 }).width).toBe('min(20%, calc(90% - 4px))')
  })
})

describe('runs2-21: a new span is “reading…”, not the last span’s lanes under the new dates', () => {
  it('withholds the 14d lanes and their count while 24h is read', async () => {
    const { rerender } = render(<TimelineLanesScreen view="span=14d" onView={() => {}} />)
    await waitFor(() => expect(lanes()).toHaveLength(1))
    expect(head()).toMatch(/· 1 lane$/)
    api.loadAttemptsPage.mockImplementation(() => new Promise(() => {}))
    rerender(<TimelineLanesScreen view="span=24h" onView={() => {}} />)
    // MUTATION: keep showing the last drawing on a span change (`shown =
    // drawn` whatever its span) and the 14d lane and '1 lane' stand under 24h.
    await waitFor(() => expect(head()).toMatch(/· reading…$/))
    expect(lanes()).toHaveLength(0)
    expect(document.querySelector('.tl-chart.is-stale')).toBeNull()
    expect(cell(/fenced/) === null || /counting…/.test(text(cell(/fenced/)))).toBe(true)
  })

  it('keeps the last drawing, dimmed, while the same span is read again', async () => {
    render(<TimelineLanesScreen view="span=24h" onView={() => {}} />)
    await waitFor(() => expect(lanes()).toHaveLength(1))
    api.loadAttemptsPage.mockImplementation(() => new Promise(() => {}))
    // MUTATION: drop every drawing on a re-read and a refresh blanks the page each minute.
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.c-refresh')!)
    await waitFor(() => expect(document.querySelector('.tl-chart.is-stale')).not.toBeNull())
    expect(lanes()).toHaveLength(1)
  })
})

describe('runs2-22: a span draws the same lanes however it is reached', () => {
  it('draws no never-ran step of a workflow with no attempt in the span', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [STEP] } as TaskPage))
    api.loadAttemptsPage.mockResolvedValue(ok(page([attempt('task_fan_0', 3 * 24 * 60)])))
    const { rerender } = render(<TimelineLanesScreen view="span=14d" onView={() => {}} />)
    await waitFor(() => expect(lanes().map((l) => l.dataset.lane)).toEqual(['task_fan_0', 'task_synth']))
    api.loadAttemptsPage.mockResolvedValue(ok(page([])))
    rerender(<TimelineLanesScreen view="span=24h" onView={() => {}} />)
    await waitFor(() => expect(body()).toMatch(/No attempt held capacity/))
    // MUTATION: add every read workflow's never-ran steps (the code before
    // this fix) and 24h reached from 14d draws 'synthesis · never ran'.
    expect(lanes()).toHaveLength(0)
    expect(head()).toMatch(/· 0 lanes$/)
  })
})

describe('runs2-26: closing the lane panel hands focus back to the lane’s name', () => {
  it('focuses the name button that opened the panel on Escape', async () => {
    let q = ''
    const { rerender } = render(<TimelineLanesScreen view={q} onView={(n) => (q = n)} />)
    await waitFor(() => expect(lanes()).toHaveLength(1))
    const name = lanes()[0]!.querySelector<HTMLButtonElement>('button.tl-name')!
    fireEvent.click(name)
    rerender(<TimelineLanesScreen view={q} onView={(n) => (q = n)} />)
    await waitFor(() => expect(document.querySelector('.tl-detail')).not.toBeNull())
    document.querySelector<HTMLButtonElement>('.tl-detail .tl-close')!.focus()
    act(() => {
      fireEvent.keyDown(window, { key: 'Escape' })
    })
    rerender(<TimelineLanesScreen view={q} onView={(n) => (q = n)} />)
    await waitFor(() => expect(document.querySelector('.tl-detail')).toBeNull())
    // MUTATION: close with `select(null)` alone and focus falls to <body>,
    // which the shell hands to the page's h1.
    expect(document.activeElement).toBe(lanes()[0]!.querySelector('button.tl-name'))
  })
})

describe('runs2-27: a standalone lane is named by what it is, in full, then its short id', () => {
  it('names it by its title, else its whole profile, never “claude-code agent”', async () => {
    // MUTATION: name lanes with `agentName` (the code before this fix) and
    // every standalone row is 'claude-code agent · …', cut to 'claude-c… · …'.
    expect(laneName(TASK)).toBe('claude-code · solo')
    expect(laneName({ ...TASK, metadata: { title: '  Fix the flaky lease test  ' } } as unknown as Task)).toBe('Fix the flaky lease test · solo')
    expect(laneName({ ...TASK, runner_profile: 'browser' } as unknown as Task)).toBe('browser check · solo')
    // MUTATION: name a step by anything but its step id and the workflow's rows change.
    expect(laneName(STEP)).toBe('fan_0')
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lanes()).toHaveLength(1))
    const name = lanes()[0]!.querySelector('button.tl-name')!
    expect(text(name.querySelector('.tl-name-k'))).toBe('claude-code')
    expect(text(name.querySelector('.tl-name-id'))).toBe('· solo')
  })

  // jsdom lays nothing out (every scrollWidth and clientWidth is 0), so what
  // the 240 px label column draws is pinned by the rules that draw it.
  const rule = (sel: string) => {
    const at = TIMELINE_CSS.indexOf(`${sel} {`)
    return at < 0 ? '' : TIMELINE_CSS.slice(at, TIMELINE_CSS.indexOf('}', at))
  }
  it('draws the name whole on its own line rather than cutting it to “claude-c…” beside the state and time', async () => {
    render(<TimelineLanesScreen view={null} onView={() => {}} />)
    await waitFor(() => expect(lanes()).toHaveLength(1))
    // MUTATION: leave the label line unmarked (the code before this fix) and
    // the name shares one nowrap line with the mark and the time held, which
    // left 'claude-c…' of 'claude-code' on the 14d desktop render.
    expect(lanes()[0]!.querySelector('.tl-lab > b')!.classList.contains('is-split')).toBe(true)
    // MUTATION: drop `flex-wrap: wrap` and the mark and time keep the name's room.
    expect(rule('.tl-lab b.is-split')).toMatch(/flex-wrap: wrap;/)
    expect(rule('.tl-lab b.is-split')).toMatch(/white-space: normal;/)
    // MUTATION: keep the kind on one ellipsized line and a title reads 'Researc… · 1beb89a5'.
    const kind = rule('.tl-name.is-split .tl-name-k')
    expect(kind).toMatch(/white-space: normal;/)
    expect(kind).toMatch(/-webkit-line-clamp: 2;/)
    expect(kind).not.toMatch(/white-space: nowrap/)
  })
})
