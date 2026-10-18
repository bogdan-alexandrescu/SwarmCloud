// LANE STARTUP-UI (owner request 2026-10-13): a browser agent's start-up is
// a bar with named steps and a time estimate -- on the Browser tab, on the
// workflow graph's live thumbnail, and in the theater.
//
// WHAT EACH CASE HOLDS:
//   * the stages render in the owner's order from a /startup fixture, ✓ for
//     done with its m:ss, ● for the current one, ○ for those to come;
//   * the current stage's counter counts from when the stage began;
//   * "~N s left" from the estimate, "taking longer than usual" past it, and
//     nothing when there is no history (sample_size 0);
//   * a 404 from /startup draws the bar from the task's events instead;
//   * the thumbnail draws a thin bar and "starting m:ss";
//   * the theater draws the same component over its screen area;
//   * polling every 2 s stops on `done`;
//   * the Browser tab with no session yet draws it in place of the old note.
//
// MUTATIONS: reorder STARTUP_STAGES (the order case turns red); count the
// current stage from the first step (the counter case reads 0:50); drop the
// `left > 0` branch (the "taking longer" case reads "~-10 s left"); return null
// on a 404 instead of reading the events (the fallback case finds no bar);
// drop `<StartupBar>` from LiveThumb or `<StartupProgress>` from Theater (their
// cases find none); poll on regardless of `done` (the polling case counts 3).

import { act, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { StartupRead } from '../api'
import type { Result } from '../fetch'
import type { TaskEvent, TaskEventsPage } from '../types'

const api = vi.hoisted(() => ({
  loadStartup: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadTasks: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { StartupProgress, STARTUP_POLL_MS, startupView, clock } = await import('../StartupProgress')
const { LiveThumb } = await import('../LiveThumb')
const { TheaterHost } = await import('../Theater')
const { LiveBrowserPane } = await import('../LiveBrowser')
const { task: runTask } = await import('./runfixture')

const ID = 'task_startup0123456789ab'
const T0 = Date.parse('2026-10-13T09:00:00Z')
const iso = (s: number) => new Date(T0 + s * 1000).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function startup(over: Partial<StartupRead> = {}): StartupRead {
  return {
    attempt_id: 'att_1',
    generation: 1,
    steps: [
      { step: 'lease_acquired', at: iso(0) },
      { step: 'dispatched', at: iso(2) },
      { step: 'pod_scheduled', at: iso(10) },
      { step: 'image_pulling', at: iso(11) },
    ],
    estimate: {},
    sample_size: 0,
    done: false,
    ...over,
  }
}

function event(type: string, s: number, attempt = 'att_1'): TaskEvent {
  return { event_id: `${type}-${s}`, task_id: ID, type, at: iso(s), attempt_id: attempt, lease_id: null, generation: 1, detail: null }
}

async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
  vi.setSystemTime(T0 + 60_000)
  api.loadStartup.mockReset()
  api.loadTaskEventsPage.mockReset()
  api.loadLiveRecordings.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTasks.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('StartupProgress', () => {
  it('renders the stages from a fixture, in order, done, current and to come', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    render(<StartupProgress taskId={ID} />)
    await flush()
    const items = within(screen.getByRole('list')).getAllByRole('listitem')
    expect(items.map((li) => li.querySelector('.startup-label')?.textContent)).toEqual([
      'Capacity reserved',
      'Machine found',
      'Downloading browser image',
      'Starting the agent',
      'Starting Chromium',
      'Live',
    ])
    expect(items.map((li) => li.getAttribute('data-status'))).toEqual(['done', 'done', 'current', 'todo', 'todo', 'todo'])
    expect(items[0]!.textContent).toContain('✓')
    // lease_acquired at 0 s, dispatched at 2 s: Capacity reserved took 0:02.
    expect(items[0]!.querySelector('.startup-time')?.textContent).toBe('0:02')
    // pod_scheduled at 10 s, eight seconds after the dispatch.
    expect(items[1]!.querySelector('.startup-time')?.textContent).toBe('0:08')
    expect(items[2]!.textContent).toContain('●')
    expect(items[3]!.textContent).toContain('○')
    expect(items[3]!.querySelector('.startup-time')).toBeNull()
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('33')
    expect(api.loadStartup).toHaveBeenCalledWith(ID)
  })

  it('counts the current stage live, from when it began', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    render(<StartupProgress taskId={ID} />)
    await flush()
    const current = () => screen.getByRole('list').querySelector('[data-status="current"] .startup-time')?.textContent
    // The image stage began when the pod was scheduled (10 s); it is now 60 s.
    expect(current()).toBe('0:50')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(current()).toBe('0:51')
  })

  it('says what is left from the estimate, and "taking longer than usual" past it', async () => {
    // 60 s now, dispatched at 2 s: 58 s since dispatch, median first frame at 90 s.
    api.loadStartup.mockResolvedValue(ok(startup({ estimate: { first_frame: 90 }, sample_size: 20 })))
    const { unmount } = render(<StartupProgress taskId={ID} />)
    await flush()
    expect(screen.getByTestId('startup-progress').textContent).toContain('~32 s left')
    unmount()

    api.loadStartup.mockResolvedValue(ok(startup({ estimate: { first_frame: 48 }, sample_size: 20 })))
    const again = render(<StartupProgress taskId={ID} />)
    await flush()
    const text = screen.getByTestId('startup-progress').textContent ?? ''
    expect(text).toContain('taking longer than usual')
    expect(text).not.toMatch(/~-?\d+ s left/)
    again.unmount()

    // No history: no time at all, however much the estimate holds.
    api.loadStartup.mockResolvedValue(ok(startup({ estimate: { first_frame: 90 }, sample_size: 0 })))
    render(<StartupProgress taskId={ID} />)
    await flush()
    const bare = screen.getByTestId('startup-progress').textContent ?? ''
    expect(bare).not.toContain('left')
    expect(bare).not.toContain('taking longer')
  })

  it('draws the bar from the task events when /startup answers 404', async () => {
    api.loadStartup.mockResolvedValue({
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'Not Found' },
    } satisfies Result<StartupRead>)
    const page: TaskEventsPage = {
      // Newest first, and an older attempt's events that must not count.
      events: [event('dispatched', 5), event('lease_acquired', 1), event('running', -100, 'att_0'), event('lease_acquired', -200, 'att_0')],
      next_page_token: null,
    }
    api.loadTaskEventsPage.mockResolvedValue(ok(page))
    render(<StartupProgress taskId={ID} state="DISPATCHED" />)
    await flush()
    expect(api.loadTaskEventsPage).toHaveBeenCalledWith(ID, { order: 'desc' })
    const items = within(screen.getByRole('list')).getAllByRole('listitem')
    expect(items.map((li) => li.querySelector('.startup-label')?.textContent)).toEqual(['Capacity reserved', 'Starting the agent'])
    expect(items.map((li) => li.getAttribute('data-status'))).toEqual(['done', 'current'])
    expect(items[0]!.querySelector('.startup-time')?.textContent).toBe('0:04')
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('50')
  })

  it('polls every 2 s while starting, and stops once done', async () => {
    api.loadStartup
      .mockResolvedValueOnce(ok(startup()))
      .mockResolvedValueOnce(ok(startup({ done: true })))
      .mockResolvedValue(ok(startup({ done: true })))
    render(<StartupProgress taskId={ID} />)
    await flush()
    expect(api.loadStartup).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTUP_POLL_MS)
    })
    expect(api.loadStartup).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTUP_POLL_MS * 5)
    })
    expect(api.loadStartup).toHaveBeenCalledTimes(2)
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100')
  })

  it('does not poll while the tab is hidden', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    render(<StartupProgress taskId={ID} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(STARTUP_POLL_MS * 3)
    })
    expect(api.loadStartup).not.toHaveBeenCalled()
    hidden.mockReturnValue('visible')
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(api.loadStartup).toHaveBeenCalledTimes(1)
  })

  // LIVE-UNUSED (#1030), task_84cb2038aa704c829d48 (2026-10-13 13:3x UTC): a
  // warm node with the image cached reported none of the pod's steps.
  it('marks earlier stages no step of which arrived as skipped (already ready) once a later one has', async () => {
    api.loadStartup.mockResolvedValue(
      ok(
        startup({
          steps: [
            { step: 'lease_acquired', at: iso(0) },
            { step: 'dispatched', at: iso(2) },
            { step: 'starting', at: iso(5) },
            { step: 'running', at: iso(6) },
            { step: 'browser_launching', at: iso(7) },
          ],
        }),
      ),
    )
    render(<StartupProgress taskId={ID} />)
    await flush()
    const items = within(screen.getByRole('list')).getAllByRole('listitem')
    expect(items.map((li) => li.getAttribute('data-status'))).toEqual(['done', 'skipped', 'skipped', 'done', 'current', 'todo'])
    expect(items[1]!.textContent).toContain('✓')
    expect(items[1]!.textContent).toContain('skipped (already ready)')
    expect(items[2]!.textContent).toContain('skipped (already ready)')
    expect(items[1]!.querySelector('.startup-time')).toBeNull()
    // The bar fills by the latest stage reached: four of six behind it.
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('67')
  })

  it('skips nothing that a later stage has not passed', () => {
    const v = startupView(
      { steps: [{ step: 'lease_acquired', at: iso(0) }, { step: 'dispatched', at: iso(2) }], estimate: {}, sampleSize: 0, done: false, fallback: false },
      T0 + 10_000,
    )
    expect(v.stages.map((s) => s.status)).toEqual(['done', 'current', 'todo', 'todo', 'todo', 'todo'])
  })

  it('never shows a negative time', () => {
    const v = startupView(
      { steps: [{ step: 'lease_acquired', at: iso(100) }], estimate: {}, sampleSize: 0, done: false, fallback: false },
      T0,
    )
    expect(clock(v.elapsed)).toBe('0:00')
    expect(clock(-5)).toBe('0:00')
    expect(clock(125)).toBe('2:05')
  })
})

describe('the thumbnail', () => {
  it('draws a thin bar and "starting m:ss" while the browser starts', async () => {
    api.loadStartup.mockResolvedValue(ok(startup({ steps: [{ step: 'lease_acquired', at: iso(19) }, { step: 'dispatched', at: iso(20) }] })))
    render(<LiveThumb taskId={ID} workflowId={null} open={() => null} />)
    await flush()
    const bar = screen.getByTestId('startup-bar')
    expect(within(bar).getByRole('progressbar')).toBeTruthy()
    // First step at 19 s, now 60 s.
    expect(bar.textContent).toContain('starting 0:41')
  })

  it('draws nothing once the start-up is done', async () => {
    api.loadStartup.mockResolvedValue(ok(startup({ done: true })))
    render(<LiveThumb taskId={ID} workflowId={null} open={() => null} />)
    await flush()
    expect(screen.queryByTestId('startup-bar')).toBeNull()
  })
})

describe('the theater', () => {
  it('draws the start-up over its screen area until the first frame', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    render(
      <TheaterHost
        initiallyOpen
        session={{ taskId: ID, tenantId: 'eng', attemptId: '', sessionId: 'ses_1', screen: null, live: true }}
      >
        <p>tab</p>
      </TheaterHost>,
    )
    await flush()
    const centre = document.querySelector('.th-center')
    expect(centre).not.toBeNull()
    const progress = within(centre as HTMLElement).getByTestId('startup-progress')
    expect(progress.className).toContain('is-overlay')
    expect(within(progress).getByRole('progressbar')).toBeTruthy()
  })

  it('steps aside once the borrowed screen has connected', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    const node = document.createElement('div')
    node.dataset.conn = 'connected'
    render(
      <TheaterHost
        initiallyOpen
        session={{ taskId: ID, tenantId: 'eng', attemptId: '', sessionId: 'ses_1', screen: node, live: true }}
      >
        <p>tab</p>
      </TheaterHost>,
    )
    await flush()
    expect(document.querySelector('.th-center [data-testid="startup-progress"]')).toBeNull()
  })
})

describe('the Browser tab', () => {
  it('draws the start-up where it said only that the browser is starting', async () => {
    api.loadStartup.mockResolvedValue(ok(startup()))
    const t = runTask({ id: ID, state: 'STARTING', runner_profile: 'claude-code-browser', completed_at: null, result_summary: null })
    render(<LiveBrowserPane task={t} live={{ status: 'ok', liveBrowser: true, session: null }} reload={() => {}} />)
    await flush()
    expect(screen.getByTestId('startup-progress')).toBeTruthy()
    expect(screen.getByRole('progressbar')).toBeTruthy()
    expect(document.body.textContent).not.toContain('It appears here once its pod is running')
  })
})
