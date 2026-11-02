// QAFIX4-FIXTURE-B (part of #1680): visual QA run 4 (wf_ecd935541ced4a539540,
// the fixture build of main at fabe4d6c) found the fixture build's canned data
// disagreeing with itself, F9-F14c: stats typed apart from the task list, a
// profile with no admission row, a quota row feeding a pool the board does not
// have, checkpoints owned by attempts that wrote none, a disabled runtime
// advising a sizing class, times minted independently, one success transcript
// for every task, and untitled workflows whose steps all carry one figure.
//
// These read what the fixture SERVES, through the real loaders in the fixture
// build, as qafix2.fixture.exit.test.ts does: the defect was in the fixture,
// and a hand-built body would agree with whatever the test author assumed.
// Every fixture read carries its own simulated latency, so the reads go out
// in parallel.

import { describe, expect, it } from 'vitest'

import {
  loadAdminQuota,
  loadAgentDetail,
  loadAttempts,
  loadCapacity,
  loadCheckpoints,
  loadHolders,
  loadRuntimeTopology,
  loadStats,
  loadTaskLogs,
  loadTasks,
  loadTranscript,
  loadWorkflow,
  loadWorkflowBoard,
  loadWorkflowUsage,
} from '../api'
import { workflowLabel } from '../stepviews'
import { CONCURRENCY_STATES, type AttemptRow, type Task } from '../types'

const LONG = 60_000

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

async function fixtureTasks(): Promise<Task[]> {
  // The fixture branch is taken only in a DEV build without VITE_LIVE. Said
  // here so a change to that switch fails this file by name.
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  return ok(await loadTasks(), 'GET /v1/tasks').tasks
}

/** The waiting agents (#1782), reached by id and kept off the list. */
const WAITING_IDS = [
  'task_wait_ci_pending',
  'task_wait_ready_capacity',
  'task_wait_scheduled_retry',
  'task_wait_dependency_incomplete',
  'task_wait_manual_pause',
]

async function attemptsOf(taskId: string): Promise<AttemptRow[]> {
  const r = await loadAttempts(taskId)
  if (r.status === 'empty') return []
  return ok(r, `GET /v1/tasks/${taskId}/attempts`).attempts
}

function newest(attempts: readonly AttemptRow[]): AttemptRow | undefined {
  return attempts.reduce<AttemptRow | undefined>((a, b) => (a === undefined || b.generation > a.generation ? b : a), undefined)
}

const ms = (iso: string | null | undefined): number => (iso ? Date.parse(iso) : NaN)

/**
 * Two fixture reads are two instants: each builds its documents from its own
 * `Date.now()`, as two API reads would see a task a moment apart. A time
 * compared ACROSS reads gets this much slack; within one read, none.
 */
const DRIFT = 5_000

describe('F9 (overview-automate-002, -003): the stats are the task list counted', () => {
  it('every state count, and so Waiting and Holding capacity, is what the list holds', async () => {
    const [tasks, stats] = await Promise.all([fixtureTasks(), loadStats()])
    const counts = ok(stats, 'GET /v1/stats').tasks_by_state
    const listed = new Map<string, number>()
    for (const t of tasks) listed.set(t.state, (listed.get(t.state) ?? 0) + 1)
    let visited = 0
    for (const [state, n] of Object.entries(counts)) {
      // MUTATION: type `QUEUED: 0` back into fixtureStats -> fails on QUEUED (the list holds 1).
      expect(n, `stats say ${n} ${state}, the list holds ${listed.get(state) ?? 0}`).toBe(listed.get(state) ?? 0)
      visited++
    }
    expect(visited, 'the stats served no state counts').toBeGreaterThan(10)
    for (const state of listed.keys()) expect(Object.keys(counts), `stats omit ${state}`).toContain(state)
    const holding = [...CONCURRENCY_STATES].reduce((t, s) => t + (counts[s] ?? 0), 0)
    // MUTATION: type `RUNNING: 2` back into fixtureStats -> fails (5 against the 7 the list holds).
    expect(holding).toBe(tasks.filter((t) => CONCURRENCY_STATES.has(t.state)).length)
  }, LONG)
})

describe('F10 (capacity-004, overview-automate-007): every profile carries its admission row', () => {
  it('claude-code-browser is measured, complete, and reads nothing unread', async () => {
    const cap = ok(await loadCapacity(), 'GET /v1/capacity')
    let visited = 0
    for (const [name, p] of Object.entries(cap.runner_profiles ?? {})) {
      // MUTATION: drop `admission: FIXTURE_ADMISSION['claude-code-browser']` -> fails on claude-code-browser.
      expect(p.admission, `${name} has no admission row`).toBeTruthy()
      expect(p.admission?.complete, `${name}'s admission is incomplete`).toBe(true)
      expect(p.admission?.unread, `${name} reads pools as unread`).toEqual([])
      visited++
    }
    expect(visited, 'the capacity read served no runner profiles').toBeGreaterThan(5)
    // Its headroom is the full provider pool's: 0, a measured figure, not a dash.
    expect(cap.runner_profiles?.['claude-code-browser']?.admission?.headroom).toBe(0)
  }, LONG)
})

describe('F11 (capacity-009): every quota row feeds a pool the board and Holders have', () => {
  it('names only provider:<p>:tenant:<t> pools that the capacity and holders reads list', async () => {
    const [quota, cap, holders] = await Promise.all([loadAdminQuota(), loadCapacity(), loadHolders()])
    const rows = ok(quota, 'GET /v1/admin/quota').quota
    const board = new Set(ok(cap, 'GET /v1/capacity').pools.map((p) => p.name))
    const held = new Set((ok(holders, 'the holders read').pools ?? []).map((p) => p.name))
    expect(rows.length, 'the quota read served no rows').toBeGreaterThan(3)
    for (const q of rows) {
      const pool = `provider:${q.provider}:tenant:${q.tenant_id}`
      // MUTATION: drop pool('provider:openai:tenant:u-bogdan', ...) from fixtureCapacity -> fails on openai u-bogdan.
      expect(board.has(pool), `${pool} is not on the Pools board`).toBe(true)
      expect(held.has(pool), `${pool} is not in Holders' accounting`).toBe(true)
    }
  }, LONG)
})

describe('F12 (task-004): every checkpoint is one its attempt wrote', () => {
  it('the listing and the attempt records name the same checkpoints, under started attempts', async () => {
    const tasks = await fixtureTasks()
    const visited = await Promise.all(
      tasks.map(async (t) => {
        const [attempts, page] = await Promise.all([attemptsOf(t.id), loadCheckpoints(t.id)])
        const listing = ok(page, `GET /v1/tasks/${t.id}/checkpoints`)
        const byId = new Map(attempts.map((a) => [a.attempt_id, a]))
        for (const c of listing.checkpoints) {
          const a = byId.get(c.attempt_id)
          // MUTATION: list fixtureCheckpoints' rows under `'att_2'` again -> fails: no attempt has that id.
          expect(a, `${t.id}: ${c.checkpoint_id} belongs to ${c.attempt_id}, which the attempts read does not list`).toBeDefined()
          // MUTATION: give fixtureAttemptRows' rows `checkpoints: []` -> fails: the attempt says it wrote none.
          expect(a?.checkpoints, `${t.id}: ${c.attempt_id} does not record ${c.checkpoint_id}`).toContain(c.checkpoint_id)
          // MUTATION: list the rows under attempt 1 (`fixtureAttemptId(taskId, 1)`) -> fails on task_a073aff5, whose attempt 1 never started.
          expect(a?.started_at, `${t.id}: ${c.checkpoint_id} was written by ${c.attempt_id}, which never started`).toBeTruthy()
          expect(c.attempt_known).toBe(true)
        }
        // Written == kept: no attempt records a checkpoint the listing lacks,
        // so the pane never says "written, then reclaimed" over a full listing.
        const listed = new Set(listing.checkpoints.map((c) => `${c.attempt_id}/${c.checkpoint_id}`))
        for (const a of attempts) {
          for (const id of a.checkpoints) {
            // MUTATION: spell fixtureAttemptRows' `checkpoints` as 'ckpt-00001' and 'ckpt-00002' again -> fails: written, never listed.
            expect(listed.has(`${a.attempt_id}/${id}`), `${t.id}: ${a.attempt_id} wrote ${id}, the listing lacks it`).toBe(true)
          }
        }
        expect(listing.count).toBe(listing.checkpoints.length)
        const pointer = listing.checkpoints.find((c) => c.is_latest_pointer)
        if (listing.checkpoints.length > 0) {
          expect(pointer, `${t.id}: nothing is the latest pointer`).toBeDefined()
          expect(listing.latest_checkpoint.checkpoint_id).toBe(pointer?.checkpoint_id)
        }
        return listing.checkpoints.length
      }),
    )
    expect(visited.length).toBe(tasks.length)
    // Not vacuous: the reference task's listing holds checkpoints.
    expect(visited[tasks.findIndex((t) => t.id === 'task_a073aff5')], 'task_a073aff5 lists no checkpoint').toBeGreaterThan(0)
  }, LONG)
})

describe('F13 (capacity-006): a disabled runtime advises a runtime a caller can pick', () => {
  it('every "Use X" in a disabled reason names a runtime or profile in the same catalogue', async () => {
    const [topology, cap] = await Promise.all([loadRuntimeTopology(), loadCapacity()])
    const runtimes = ok(topology, 'GET /v1/runtimes').runtimes
    const profiles = ok(cap, 'GET /v1/capacity').runner_profiles ?? {}
    const catalogues: [string, Record<string, { available?: boolean; disabled_reason?: string }>][] = [
      ['runtimes', runtimes],
      ['runner profiles', profiles],
    ]
    let advised = 0
    for (const [what, catalogue] of catalogues) {
      for (const [name, r] of Object.entries(catalogue)) {
        if (r.available !== false) continue
        for (const m of (r.disabled_reason ?? '').matchAll(/\bUse ([\w-]+)/g)) {
          // MUTATION: restore 'Use demo-small.' in demo-viewport's reason -> fails (a sizing class).
          expect(Object.keys(catalogue), `${what} ${name} advises ${m[1]}, which is not one of its runtimes`).toContain(m[1])
          advised++
        }
      }
    }
    expect(advised, 'no disabled runtime advises another, so this check read nothing').toBeGreaterThan(1)
  }, LONG)
})

describe('F14a (task-003): one timeline per task', () => {
  it('runs submitted, then attempt starts, then events, up to now', async () => {
    const listed = await fixtureTasks()
    const ids = [...listed.map((t) => t.id), ...WAITING_IDS]
    const visited = await Promise.all(
      ids.map(async (id) => {
        const [detail, attempts] = await Promise.all([loadAgentDetail(id), attemptsOf(id)])
        const { task, events } = ok(detail, `GET /v1/tasks/${id}`)
        const now = Date.now()
        const submitted = ms(task.created_at)
        expect(submitted, `${id} has no submitted time`).not.toBeNaN()
        // MUTATION: mint fixtureAttemptRows' `created_at` as `iso(39 - n * 10)` minutes ago again -> fails: attempt 3 of task_a073aff5 (submitted 4m ago) is admitted 9m ago.
        for (const a of attempts) {
          expect(ms(a.created_at), `${a.attempt_id} is admitted before ${id} was submitted`).toBeGreaterThanOrEqual(submitted - DRIFT)
          if (a.started_at !== null) expect(ms(a.started_at), `${a.attempt_id} starts before its admission`).toBeGreaterThanOrEqual(ms(a.created_at))
          if (a.completed_at !== null) {
            expect(a.started_at, `${a.attempt_id} ends but never started`).not.toBeNull()
            expect(ms(a.completed_at), `${a.attempt_id} ends before it starts`).toBeGreaterThanOrEqual(ms(a.started_at))
            expect(ms(a.completed_at), `${a.attempt_id} ends in the future`).toBeLessThanOrEqual(now)
          }
        }
        // Attempts in generation order run one after another.
        const ordered = [...attempts].sort((a, b) => a.generation - b.generation)
        for (let i = 1; i < ordered.length; i++) {
          expect(ms(ordered[i]!.created_at), `${ordered[i]!.attempt_id} is admitted before ${ordered[i - 1]!.attempt_id}`).toBeGreaterThanOrEqual(
            ms(ordered[i - 1]!.completed_at ?? ordered[i - 1]!.created_at),
          )
        }
        // The task's start is its newest attempt's start (stepviews' `ran`: the latest start).
        const started = [...ordered].reverse().find((a) => a.started_at !== null)
        // MUTATION: make fixtureTimed's `started_at` the FIRST started attempt's start -> fails on task_a073aff5: Elapsed is 'since' attempt 2.
        if (started === undefined) expect(task.started_at, `${id} starts with no attempt started`).toBeNull()
        else expect(Math.abs(ms(task.started_at) - ms(started.started_at)), `${id}'s started_at is not its newest attempt's start`).toBeLessThanOrEqual(DRIFT)
        const byId = new Map(attempts.map((a) => [a.attempt_id, a]))
        for (const e of events ?? []) {
          // MUTATION: time fixtureAgentDetail's events at fixed minutes ago again (`ready` 12m ago) -> fails: events 12m ago on a task submitted 4m ago.
          expect(ms(e.at), `${id}: ${e.type} at ${e.at} is before the task was submitted`).toBeGreaterThanOrEqual(submitted)
          expect(ms(e.at), `${id}: ${e.type} is in the future`).toBeLessThanOrEqual(now)
          if (e.attempt_id !== null) {
            const a = byId.get(e.attempt_id)
            expect(a, `${id}: ${e.type} names ${e.attempt_id}, which the attempts read does not list`).toBeDefined()
            expect(e.generation).toBe(a?.generation)
            // Admission to start is seconds: the events before a start come before its `created_at` (the start).
            expect(ms(e.at), `${id}: ${e.type} precedes the admission of ${e.attempt_id}`).toBeGreaterThanOrEqual(ms(a?.created_at) - 60_000 - DRIFT)
            if (a?.completed_at) expect(ms(e.at), `${id}: ${e.type} follows the end of ${e.attempt_id}`).toBeLessThanOrEqual(ms(a.completed_at) + DRIFT)
          }
        }
        if (task.completed_at !== null) {
          for (const a of attempts) expect(ms(a.completed_at ?? a.created_at)).toBeLessThanOrEqual(ms(task.completed_at) + DRIFT)
        }
        // The task's attempt counter and generation are its attempts'.
        expect(attempts.length, `${id} counts ${task.attempt_count} attempts and serves ${attempts.length}`).toBe(
          task.current_generation === 0 ? 0 : task.attempt_count,
        )
        return id
      }),
    )
    expect(visited.length).toBe(ids.length)
  }, LONG)
})

describe('F14b (flow-find-failure D6, D7, P5): logs are the task\'s and its attempt\'s', () => {
  it('the transcript and logs name the newest attempt, and a failed transcript ends in success only for QA-115', async () => {
    const tasks = await fixtureTasks()
    let successOnFailure: string[] = []
    const visited = await Promise.all(
      tasks.map(async (t) => {
        const [attempts, transcript, logs, agentLogs] = await Promise.all([
          attemptsOf(t.id),
          loadTranscript(t.id),
          loadTaskLogs(t.id),
          loadTaskLogs(t.id, { stream: ['agent_stdout', 'agent_stderr'] }),
        ])
        const latest = newest(attempts)
        const tr = ok(transcript, `GET /v1/tasks/${t.id}/transcript`)
        const lg = ok(logs, `GET /v1/tasks/${t.id}/logs`)
        const ag = ok(agentLogs, `GET /v1/tasks/${t.id}/logs?stream=agent_stdout`)
        for (const [what, attemptId, paths] of [
          ['transcript', tr.attempt_id, [tr.stream.uri]],
          ['logs', lg.attempt_id, lg.streams.flatMap((s) => [s.key, s.uri])],
          ['agent logs', ag.attempt_id, ag.streams.flatMap((s) => [s.key, s.uri])],
        ] as const) {
          // MUTATION: name `'att_fixture'` as fixtureTranscript's attempt_id again -> fails on every task.
          expect(attemptId, `${t.id}'s ${what} name ${attemptId}, its newest attempt is ${latest?.attempt_id}`).toBe(latest?.attempt_id ?? null)
          for (const p of paths) {
            if (p === null || p === undefined) continue
            expect(p, `${t.id}'s ${what} path names another task`).toContain(t.id)
            if (latest !== undefined) expect(p, `${t.id}'s ${what} path names another attempt`).toContain(latest.attempt_id)
          }
        }
        if (latest !== undefined) {
          expect(tr.attempt?.generation).toBe(latest.generation)
          expect(lg.attempt?.generation).toBe(latest.generation)
          expect(tr.attempt?.exit_code ?? null).toBe(latest.exit_code)
        }
        const last = [...(tr.steps ?? [])].reverse().find((s) => s.kind === 'result')
        if (t.state === 'FAILED' && last !== undefined && last.meta?.['is_error'] !== true) successOnFailure = [...successOnFailure, t.id]
        if (t.state === 'SUCCEEDED') expect(last?.meta?.['is_error'], `${t.id} succeeded`).toBe(false)
        return t.id
      }),
    )
    expect(visited.length).toBe(tasks.length)
    // QA-115, KEPT AS BUILT: one failed task whose CLI reported success, so
    // Logs' "the CLI's success is not the task's" line is drawn. Only that one.
    // MUTATION: make fixtureCliResult answer success for every FAILED task -> fails: task_fan_3 joins task_8e33a3de.
    expect([...successOnFailure].sort()).toEqual(['task_8e33a3de'])
  }, LONG)
})

describe('F14c (flow-workflow-graph-light-01, -03, -07, -08, -12): titled workflows whose steps sum their attempts', () => {
  it('each workflow is titled, spans its steps, and each step\'s figures are its attempts\' sum', async () => {
    const board = ok(await loadWorkflowBoard(), 'GET /v1/workflows')
    expect(board.workflows.length).toBeGreaterThanOrEqual(3)
    const ranFor: string[] = []
    for (const w of board.workflows) {
      // MUTATION: drop the `metadata: { title }` from the workflow step tasks -> fails: 'Untitled workflow'.
      expect(workflowLabel(w, board.taskById), `${w.workflow_id} is untitled`).not.toBeNull()
      expect(w.steps.some((s) => s.task_id && board.taskById?.has(s.task_id)), `${w.workflow_id} joins no task`).toBe(true)
      // THE STEP TASKS THEMSELVES from the workflow's own read: the board's
      // join is each step's list block (console load speed, 2026-11-01),
      // which serves no creation or update time.
      const own = ok(await loadWorkflow(w.workflow_id), `GET /v1/workflows/${w.workflow_id}`)
      const tasks = w.steps.map((s) => own.tasks.find((t) => t.id === s.task_id)).filter((t): t is Task => t !== undefined)
      expect(tasks.length, `${w.workflow_id} joins no task`).toBeGreaterThan(0)
      // MUTATION: type wf_5e5ad3b6f7da4299a839's `created_at` as 4 hours ago again instead of `spanOf(...)` -> fails (3h 31m matches neither interval).
      expect(w.created_at, `${w.workflow_id} starts after its first step`).toBe(new Date(Math.min(...tasks.map((t) => ms(t.created_at)))).toISOString())
      expect(w.updated_at, `${w.workflow_id} ends before its last step`).toBe(new Date(Math.max(...tasks.map((t) => ms(t.updated_at)))).toISOString())
      const usage = ok(await loadWorkflowUsage(tasks.map((t) => t.id)), `${w.workflow_id}'s step usage`)
      for (const t of tasks) {
        const attempts = await attemptsOf(t.id)
        const step = usage.byTaskId.get(t.id)
        expect(step, `${t.id} was not read`).toBeDefined()
        const sum = (f: (a: AttemptRow) => number | null) => {
          const got = attempts.map(f).filter((v): v is number => typeof v === 'number')
          return got.length === 0 ? null : got.reduce((x, y) => x + y, 0)
        }
        // MUTATION: give fixtureStepAttempts its own rows again (one attempt at `cost_usd: 0.0061`) -> fails: the step's total is not its attempts'.
        expect(step?.attempts, `${t.id}'s step counts other attempts`).toBe(attempts.length)
        const cost = sum((a) => a.cost_usd)
        if (cost === null) expect(step?.costUsd ?? null, `${t.id}'s step cost`).toBeNull()
        else expect(step?.costUsd, `${t.id}'s step cost`).toBeCloseTo(cost, 9)
        expect(step?.inputTokens ?? null).toBe(sum((a) => a.input_tokens))
        expect(step?.cacheReadTokens ?? null).toBe(sum((a) => a.cache_read_input_tokens))
        expect(step?.checkpoints).toBe(attempts.reduce((n, a) => n + a.checkpoints.length, 0))
        if (t.started_at !== null && t.completed_at !== null) ranFor.push(`${ms(t.completed_at) - ms(t.started_at)}`)
        // A step cancelled before it ran has no start and so no `ran`.
        if (t.state === 'CANCELLED' && t.step_id === 'synthesis') expect(t.started_at, 'the cancelled synthesis ran').toBeNull()
      }
    }
    // MUTATION: make fixtureRunMs return a constant 5 minutes -> fails: the finished steps' `ran` takes three values at most.
    expect(new Set(ranFor).size, `every finished step ran the same ${ranFor[0]} ms`).toBeGreaterThan(3)
  }, LONG)
})
