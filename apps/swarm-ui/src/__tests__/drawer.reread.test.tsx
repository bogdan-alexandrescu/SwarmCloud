// THE DRAWER RE-READS, AND EVERYTHING IN IT IS AS OF ONE READ (AG-1, AG-2).
//
// #152 made the agent inspector re-read its run every 10s. The review of that
// change found five ways the rest of the drawer did not follow, and one way
// the list's poll broke a rule its own spec states. Each test below renders
// the real `AgentDetailScreen` (or `AgentsScreen`) under fake timers and
// drives it through the reads a browser would see; none of them calls a
// helper and checks its return value, because the defect in every case was in
// the wiring, not in a helper.
//
//   1. The drawer re-reads at all -- 10s while the task is live, and no more
//      once a finished task has settled. The only test of this used to call
//      `drawerPoll()` directly, so dropping `pollMs` from the screen passed.
//   2. A checkpoint written after the drawer opened is not "reclaimed". The
//      listing was read once, at open, and compared with attempt records from
//      the latest poll, so every checkpoint written while someone watched was
//      drawn as lost.
//   3. The log panel follows the task. It was read once, so a task that
//      started while the drawer was open still said it had no attempt.
//   4. A finish is watched to its end. `finish()` writes the terminal state,
//      then the attempt's end, then the terminal event; a poll that lands
//      between them used to stop the drawer on a half-written finish.
//   5. The drawer's clock stops over a read that failed to refresh. A live
//      agent read once and then unreachable used to slide to `silent`.
//   6. The Agents list at phone width reads 50 rows, not 200, and says so
//      (docs/web-ui/03-agents-and-workflows.md §2.1 and §2.5).

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type {
  AttemptRow,
  CheckpointRecord,
  CheckpointsPage,
  Task,
  TaskEvent,
  TaskLogs,
  TaskPage,
  TaskState,
} from '../types'

const api = vi.hoisted(() => ({
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTasks: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentDetailScreen, DRAWER_POLL_MS, drawerPoll } from '../AgentDetail'
import { AgentsScreen } from '../Agents'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TASK_ID = 'task_b5dc2568713a40158851'
const PREFIX = `tenants/acme/tasks/${TASK_ID}/attempts/`

function task(state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: new Date(Date.now() - 120_000).toISOString(),
    updated_at: new Date().toISOString(),
    started_at: state === 'READY' ? null : new Date(Date.now() - 60_000).toISOString(),
    completed_at: null,
    submitted_by: 'ada@acme.test',
    attempt_count: state === 'READY' ? 0 : 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: state === 'READY' ? null : 'lse_1',
    ...over,
  }
}

function attempt(over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: 'att_1',
    task_id: TASK_ID,
    tenant_id: 'acme',
    generation: 1,
    lease_id: 'lse_1',
    backend: 'cloudrun',
    execution_name: 'exec-1',
    created_at: new Date(Date.now() - 70_000).toISOString(),
    started_at: new Date(Date.now() - 60_000).toISOString(),
    completed_at: null,
    exit_code: null,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...over,
  }
}

function event(type: string, msAgo: number): TaskEvent {
  return {
    event_id: `ev_${type}_${msAgo}`,
    task_id: TASK_ID,
    type,
    at: new Date(Date.now() - msAgo).toISOString(),
    attempt_id: 'att_1',
    lease_id: 'lse_1',
    generation: 1,
    detail: {},
  }
}

function agentRun(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: task('RUNNING'),
    events: [],
    eventsDetail: null,
    attempts: [attempt()],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function checkpoint(id: string): CheckpointRecord {
  return {
    checkpoint_id: id,
    attempt_id: 'att_1',
    attempt_known: true,
    attempt_created_at: null,
    attempt_completed_at: null,
    prefix: `${PREFIX}att_1/checkpoints/${id}/`,
    uri: `gs://swarm-artifacts/${PREFIX}att_1/checkpoints/${id}/`,
    is_latest_pointer: false,
    objects: [],
    stored_bytes: 4096,
    manifest: 'present',
    manifest_detail: null,
    created_at: null,
    seq: null,
    generation: 1,
    label: null,
    archive_bytes: null,
    archive_sha256: null,
    file_count: 3,
    resumable: true,
    resumable_detail: null,
  }
}

/** A whole listing -- read to its end -- holding exactly these checkpoints. */
function listing(ids: string[]): CheckpointsPage {
  return {
    task_id: TASK_ID,
    tenant_id: 'acme',
    prefix: PREFIX,
    checkpoints: ids.map(checkpoint),
    count: ids.length,
    total_found: ids.length,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
  }
}

function logs(status: TaskLogs['attempt']['status']): TaskLogs {
  return {
    task_id: TASK_ID,
    tenant_id: 'acme',
    attempt_id: status === 'latest' ? 'att_1' : null,
    attempt: {
      status,
      known: status === 'latest',
      generation: status === 'latest' ? 1 : null,
      created_at: null,
      completed_at: null,
      exit_code: null,
    },
    streams: [],
    prefix: PREFIX,
    redaction: { applied_at_read_time: true, rules: 12 },
  }
}

const NO_BODY = { status: 'empty' as const, fetchedAt: 0 }

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

function fakeClock(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
}

/**
 * Move the fake clock, then let what it set off finish. A drawer read is a
 * chain -- the run lands, the body renders, the panels' effects start their
 * own reads, those land -- and one `act` does not see past the first link.
 * The extra zero-length steps run no timer that was not already due.
 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  }
}

/** The drawer, mounted the way App mounts it. */
async function openDrawer(): Promise<HTMLElement> {
  const { container } = render(<AgentDetailScreen taskId={TASK_ID} onClose={() => {}} />)
  await advance(0)
  return container as HTMLElement
}

/** The inspector section headed `title`, or undefined. */
function section(root: HTMLElement, title: string): HTMLElement | undefined {
  return [...root.querySelectorAll<HTMLElement>('section')].find(
    (s) => s.querySelector('h2')?.textContent === title,
  )
}

/** A head fact's value, by its key (`run`, `wait`, `age` ...). */
function fact(root: HTMLElement, key: string): string | undefined {
  const li = [...root.querySelectorAll<HTMLElement>('.ctl-fact')].find(
    (el) => el.querySelector('b')?.textContent === key,
  )
  if (li === undefined) return undefined
  return (li.textContent ?? '').slice(key.length).trim()
}

// Every count below starts from zero, whatever an earlier test in this file
// called, and every loader answers only what the test in hand tells it to.
beforeEach(() => {
  for (const loader of Object.values(api)) loader.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// 1. The drawer re-reads
// ---------------------------------------------------------------------------

describe('the open drawer re-reads its run (AG-2)', () => {
  /**
   * BREAK IT: drop `pollMs={drawerPoll}` from AgentDetailScreen. The drawer
   * reads once, and `loadAgentRun` is called once in ten seconds.
   */
  it('re-reads a live task every 10s, and stops once a finished task has settled', async () => {
    fakeClock()
    api.loadCheckpoints.mockImplementation(async () => NO_BODY)
    api.loadTaskLogs.mockImplementation(async () => NO_BODY)
    let finished = false
    api.loadAgentRun.mockImplementation(async () =>
      ok(
        finished
          ? agentRun({
              // SETTLED: the terminal state, the attempt's end and the
              // terminal event are all on the page.
              task: task('SUCCEEDED', { completed_at: new Date().toISOString() }),
              attempts: [attempt({ completed_at: new Date().toISOString(), exit_code: 0 })],
              events: [event('succeeded', 0)],
            })
          : agentRun(),
      ),
    )
    await openDrawer()
    expect(api.loadAgentRun).toHaveBeenCalledTimes(1)
    await advance(DRAWER_POLL_MS)
    expect(api.loadAgentRun, 'the drawer did not re-read a RUNNING task at 10s').toHaveBeenCalledTimes(2)

    finished = true
    await advance(DRAWER_POLL_MS)
    expect(api.loadAgentRun).toHaveBeenCalledTimes(3)
    await advance(30_000)
    expect(api.loadAgentRun, 'the drawer kept re-reading a settled, finished task').toHaveBeenCalledTimes(3)
  })
})

// ---------------------------------------------------------------------------
// 2. The checkpoint listing follows the records it is compared with
// ---------------------------------------------------------------------------

describe('a checkpoint written while the drawer is open is not drawn as lost', () => {
  /**
   * THE REVIEW'S CASE. The listing at open holds ckpt-00001. The worker then
   * uploads ckpt-00002 and records it on the attempt, and the next drawer
   * read brings that record. The listing read at open does not hold it -- it
   * did not exist yet -- and comparing the two drew "Written, then reclaimed"
   * over a checkpoint that is sitting in the bucket.
   *
   * The second listing is SLOW on purpose: while it is in flight, the panel
   * must not compare the old listing with the new records either.
   */
  it('re-reads the listing with the drawer, and never compares an older listing with newer records', async () => {
    fakeClock()
    api.loadTaskLogs.mockImplementation(async () => NO_BODY)
    let reads = 0
    api.loadAgentRun.mockImplementation(async () => {
      reads += 1
      return ok(
        agentRun({
          attempts: [
            attempt({ checkpoints: reads === 1 ? ['ckpt-00001'] : ['ckpt-00001', 'ckpt-00002'] }),
          ],
        }),
      )
    })
    let listings = 0
    api.loadCheckpoints.mockImplementation(async () => {
      listings += 1
      if (listings === 1) return ok(listing(['ckpt-00001']))
      await new Promise((resolve) => setTimeout(resolve, 2_000))
      return ok(listing(['ckpt-00001', 'ckpt-00002']))
    })

    const root = await openDrawer()
    const panel = () => section(root, 'Checkpoints')?.textContent ?? ''
    expect(panel()).toContain('ckpt-00001')
    expect(panel()).not.toMatch(/reclaimed/i)

    // The drawer's second read lands: the attempt now records ckpt-00002.
    await advance(DRAWER_POLL_MS)
    expect(reads).toBe(2)
    expect(panel(), 'an older listing was compared with newer attempt records').not.toMatch(/reclaimed/i)

    // The re-read listing lands, holding it.
    await advance(2_000)
    expect(listings, 'the checkpoint listing was not re-read with the drawer').toBe(2)
    expect(panel()).toContain('ckpt-00002')
    expect(panel(), 'a checkpoint in the bucket was drawn as reclaimed').not.toMatch(/reclaimed/i)
  })
})

// ---------------------------------------------------------------------------
// 3. The log panel follows the task
// ---------------------------------------------------------------------------

describe('the log panel is re-read with the drawer', () => {
  /**
   * THE REVIEW'S CASE (a). Opened on a READY task, the panel says there is no
   * attempt. Ten seconds later the chip reads RUNNING -- and the panel, read
   * once at open, went on saying there was no attempt for as long as the
   * drawer stayed open.
   */
  it('stops saying there is no attempt once one has started', async () => {
    fakeClock()
    api.loadCheckpoints.mockImplementation(async () => NO_BODY)
    let started = false
    api.loadAgentRun.mockImplementation(async () =>
      ok(started ? agentRun() : agentRun({ task: task('READY'), attempts: [] })),
    )
    api.loadTaskLogs.mockImplementation(async () => ok(logs(started ? 'latest' : 'no_attempt_yet')))

    const root = await openDrawer()
    // #184: the panel is the RUNNER's log and is titled so; it still follows the drawer.
    const panel = () => section(root, 'Runner log (platform)')?.textContent ?? ''
    expect(panel()).toMatch(/no attempt yet/i)

    started = true
    await advance(DRAWER_POLL_MS)
    expect(root.querySelector('.ctl-chip')?.textContent).toMatch(/RUNNING/i)
    expect(panel(), 'the log panel still says no attempt under a RUNNING chip').not.toMatch(/no attempt yet/i)
    expect(panel()).toMatch(/Latest attempt att_1/)
  })
})

// ---------------------------------------------------------------------------
// 4. A finish is watched to its end
// ---------------------------------------------------------------------------

describe('the drawer does not stop on a half-written finish', () => {
  /**
   * `finish()` (agent_worker/control.py) commits the terminal state first,
   * then the attempt's end, then the terminal event. The drawer's three reads
   * run in parallel, so one can land between those writes: SUCCEEDED, with
   * the attempt still open and no terminal event. Stopping there left the
   * drawer on `ended, no end recorded` and a missing terminal event for good.
   */
  it('keeps re-reading a terminal task whose attempt end and terminal event are not in yet', () => {
    const justNow = new Date(Date.now() - 1_000).toISOString()
    const halfWritten = agentRun({
      task: task('SUCCEEDED', { completed_at: justNow }),
      attempts: [attempt()],
      events: [event('running', 60_000)],
    })
    expect(drawerPoll(halfWritten), 'the drawer stopped on a half-written finish').toBe(DRAWER_POLL_MS)
  })

  it('stops once the finish is whole, and stops on an old finish that never will be', () => {
    const justNow = new Date(Date.now() - 1_000).toISOString()
    const whole = agentRun({
      task: task('SUCCEEDED', { completed_at: justNow }),
      attempts: [attempt({ completed_at: justNow, exit_code: 0 })],
      events: [event('succeeded', 1_000)],
    })
    expect(drawerPoll(whole)).toBeNull()
    // A cancel of a PARKED task leaves its attempt with no end, forever. Past
    // the settle window there is nothing left to wait for.
    const longAgo = new Date(Date.now() - 10 * 60_000).toISOString()
    const neverWhole = agentRun({
      task: task('CANCELLED', { completed_at: longAgo }),
      attempts: [attempt()],
      events: [],
    })
    expect(drawerPoll(neverWhole)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 5. The clock stops over a stale read
// ---------------------------------------------------------------------------

describe('the drawer clock does not age a read that failed to refresh (AG-2)', () => {
  /**
   * THE REVIEW'S CASE. Read at t0 with the newest event 20s old, then every
   * re-read fails (503) for eight minutes. `Screen` keeps the last good run on
   * screen, dimmed -- and the 1s clock went on aging it: `live 20s ago`, then
   * `quiet`, then `silent 7m ago` with "the worker may be gone". Nothing about
   * the worker was observed; only the reads failed.
   */
  it('holds the liveness badge and the run figure one poll past the last good read', async () => {
    fakeClock()
    api.loadCheckpoints.mockImplementation(async () => NO_BODY)
    api.loadTaskLogs.mockImplementation(async () => NO_BODY)
    let reads = 0
    api.loadAgentRun.mockImplementation(async (): Promise<Result<AgentRun>> => {
      reads += 1
      if (reads > 1) {
        return {
          status: 'error',
          error: { kind: 'upstream_degraded', httpStatus: 503, code: 'upstream_unavailable', message: 'Busy.' },
        }
      }
      return ok(agentRun({ events: [event('heartbeat', 20_000)] }))
    })

    const root = await openDrawer()
    const badge = () => root.querySelector<HTMLElement>('.liveness')
    expect(badge()?.dataset['liveness']).toBe('live')
    expect(fact(root, 'run')).toBe('1m 0s')

    // IN 10s STEPS, NOT ONE 8-MINUTE JUMP. A poll timer only bumps `Screen`'s
    // nonce; the read it asks for starts in an effect, and `act` holds that
    // render back until its scope ends. One jump therefore starts exactly one
    // re-read, at the end -- which is what the first version of this test did,
    // so it stopped on `reads` and never reached the badge. Stepping lets each
    // poll's read start, fail, and plan the back-off's next one (10s, then
    // 20, 40, 80, 160s: five failed reads inside the eight minutes).
    for (let t = 0; t < 8 * 60_000; t += DRAWER_POLL_MS) await advance(DRAWER_POLL_MS)
    expect(reads, 'the drawer did not keep trying to re-read').toBeGreaterThan(2)
    expect(badge()?.dataset['liveness'], 'failed reads were drawn as a silent worker').toBe('live')
    expect(badge()?.textContent).toContain('30s ago')
    expect(fact(root, 'run'), 'the run figure went on counting over a read nobody refreshed').toBe('1m 10s')
  })
})

// ---------------------------------------------------------------------------
// 6. The Agents list at phone width
// ---------------------------------------------------------------------------

describe('the Agents list reads the phone page at phone width (§2.5)', () => {
  function media(phone: boolean): void {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: phone && query.includes('560'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
  }

  function page(): Result<TaskPage> {
    return ok({
      tasks: [
        task('RUNNING', { id: 'task_aaaaaaaa00000000000a' }),
        task('READY', { id: 'task_bbbbbbbb00000000000b' }),
        task('SUCCEEDED', { id: 'task_cccccccc00000000000c' }),
      ],
      tenant_id: 'acme',
      next_page_token: 'more',
    })
  }

  /**
   * §2.5: a 200-row page is 2-4 MB, and the list re-reads every 5s while Live
   * holds a row -- "the phone build must cap at limit=50 and say 'showing the
   * 50 most recent' rather than ship a 4 MB poll." It shipped the 4 MB poll.
   */
  it('asks for 50 rows at phone width and says the list is the most recent', async () => {
    fakeClock()
    media(true)
    api.loadTasks.mockImplementation(async () => page())
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(api.loadTasks, 'the phone read asked for the full 200-row page').toHaveBeenCalledWith(50)
    expect(container.querySelector('.ag-scope')?.textContent).toMatch(/showing the 3 most recent/)
  })

  /** The control: the same page on a wide screen is the full page, as before. */
  it('reads the full page on a wide screen', async () => {
    fakeClock()
    media(false)
    api.loadTasks.mockImplementation(async () => page())
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await advance(0)
    expect(api.loadTasks).toHaveBeenCalledTimes(1)
    expect(api.loadTasks).not.toHaveBeenCalledWith(50)
    expect(container.querySelector('.ag-scope')?.textContent).not.toMatch(/most recent/)
  })
})
