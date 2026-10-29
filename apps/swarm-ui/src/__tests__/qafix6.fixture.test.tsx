// QAFIX6-FIXTURE (part of #1680): visual QA run 6 (wf_31aca00c977e4d37a935,
// the fixture build at 1440 and 390, light and dark) and earlier runs found the
// fixture build serving times that move between reads, two failures that end
// together, pools that count fewer leases than the list holds, reads it does
// not serve at all, a ledger window a month before the clock, a parked time
// already past, a five-hour window resetting in 19 hours, MCP-taking profiles
// that do not say so, and quota caps that disagree with the pools they feed.
//
// These read what the fixture SERVES, through the real loaders in the fixture
// build, as qafix4.fixture.data.test.ts does: a hand-built body would agree
// with whatever the test author assumed. Each case pins one item of the lane's
// brief (a-i) and names its #1680 box.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  FIXTURE_CLOCK,
  loadAccess,
  loadAccessOwners,
  loadAccountsBoard,
  loadAdminQuota,
  loadAttempts,
  loadAttemptsPage,
  loadCapacity,
  loadForgePermissions,
  loadOnboarding,
  loadOutcomes,
  loadTasks,
  loadWorkflowBoard,
  loadWorkflowUsage,
} from '../api'
import { CONCURRENCY_STATES, TERMINAL_STATES, type AttemptRow, type Task } from '../types'

const LONG = 60_000

/** The capture clock the visual QA run freezes the page at: the fixture's "now". */
const ROUTES = JSON.parse(readFileSync(join(__dirname, '..', '..', 'qa', 'routes.json'), 'utf8')) as { clock: string }
const CLOCK = Date.parse(ROUTES.clock)

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

async function fixtureTasks(): Promise<Task[]> {
  // The fixture branch is taken only in a DEV build without VITE_LIVE.
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  return ok(await loadTasks(), 'GET /v1/tasks').tasks
}

async function attemptsOf(taskId: string): Promise<AttemptRow[]> {
  const r = await loadAttempts(taskId)
  if (r.status === 'empty') return []
  return ok(r, `GET /v1/tasks/${taskId}/attempts`).attempts
}

const ms = (iso: string | null | undefined): number => (iso ? Date.parse(iso) : NaN)

/** Move the browser's clock on by `by` ms, leaving timers real so the fixture's latency still elapses. */
function clockOn(by: number): void {
  vi.setSystemTime(Date.now() + by)
}

afterEach(() => {
  vi.useRealTimers()
})

describe('a (box 6343795203; 6:flow-find-failure-desktop-02, -phone-03): a finished task keeps its end time', () => {
  it('reads the same completed_at for every finished task 14 s later, counted from the capture clock', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const first = await fixtureTasks()
    clockOn(14_000)
    const again = new Map((await fixtureTasks()).map((t) => [t.id, t]))
    const finished = first.filter((t) => TERMINAL_STATES.has(t.state))
    expect(finished.length, 'the fixture serves no finished task').toBeGreaterThan(3)
    for (const t of finished) {
      // MUTATION: build fixtureTaskRows' `now` from Date.now() again -> fails: every end moves 14 s.
      expect(again.get(t.id)?.completed_at, `${t.id}'s end moved between two reads`).toBe(t.completed_at)
      expect(ms(t.completed_at), `${t.id} ended after the clock`).toBeLessThanOrEqual(CLOCK)
    }
    // The one instant, said once: the export the fixture counts from is the capture clock.
    // MUTATION: set FIXTURE_CLOCK to any other instant -> fails here.
    expect(Date.parse(FIXTURE_CLOCK)).toBe(CLOCK)
  }, LONG)
})

describe('b (box 6343763965; 6:flow-find-failure-desktop-01, -phone-02): the two failures end apart', () => {
  it('ends every failed task at least a minute from every other', async () => {
    const failed = (await fixtureTasks()).filter((t) => t.state === 'FAILED')
    expect(failed.length, 'the fixture serves fewer than two failures').toBeGreaterThanOrEqual(2)
    for (const a of failed) {
      for (const b of failed) {
        if (a.id >= b.id) continue
        // MUTATION: put task_8e33a3de back at 42 minutes ago -> fails: it ends 6 s from task_fan_3.
        expect(Math.abs(ms(a.completed_at) - ms(b.completed_at)), `${a.id} and ${b.id} end together`).toBeGreaterThanOrEqual(60_000)
      }
    }
  }, LONG)
})

describe('c (box 6343775508; 6:task-007): the global and tenant pools count the leases the list holds', () => {
  it('serves global and tenant:u-bogdan active as the LEASED-to-RUNNING task count', async () => {
    const [tasks, cap] = await Promise.all([fixtureTasks(), loadCapacity()])
    const holding = tasks.filter((t) => CONCURRENCY_STATES.has(t.state) && t.tenant_id === 'u-bogdan').length
    expect(holding, 'no fixture task holds capacity').toBeGreaterThan(0)
    const pools = new Map(ok(cap, 'GET /v1/capacity').pools.map((p) => [p.name, p]))
    for (const name of ['global', 'tenant:u-bogdan']) {
      // MUTATION: type pool('global', 20, 5) again -> fails: 5 against the list's 7.
      expect(pools.get(name)?.active, `${name} counts other leases than the list holds`).toBe(holding)
    }
  }, LONG)
})

describe('d (6:work-b-006): Setup and Access are served', () => {
  it('answers the onboarding, GitHub-permissions and access reads ok, with steps in mixed states', async () => {
    const [onboarding, perms, access, owners] = await Promise.all([loadOnboarding(), loadForgePermissions(), loadAccess(), loadAccessOwners()])
    // MUTATION: drop '/v1/onboarding' from FIXTURE_READS -> fails: not served.
    const doc = ok(onboarding, 'GET /v1/onboarding')
    expect(new Set(doc.steps.map((s) => s.state)).size, 'every onboarding step reads alike').toBeGreaterThanOrEqual(3)
    expect(doc.next_step, 'next_step is not the first unfinished step').toBe(doc.steps.find((s) => s.state !== 'done')?.step ?? null)
    // MUTATION: drop '/v1/forge/permissions' from FIXTURE_READS -> fails: not served.
    expect(ok(perms, 'GET /v1/forge/permissions').repositories.length).toBeGreaterThan(0)
    // MUTATION: drop '/v1/access' from FIXTURE_READS -> fails: not served.
    expect(ok(access, 'GET /v1/access').grants.length, 'Access has no rows').toBeGreaterThan(0)
    expect(ok(owners, 'GET /v1/access/orgs').owners.length).toBeGreaterThan(0)
  }, LONG)
})

describe('e (box 6343760837; 6:work-a-003): the decided window ends at the clock', () => {
  it("serves the ledger's until and generated_at at the capture clock, its since inside the 14 days before", async () => {
    const ledger = ok(await loadOutcomes(new URLSearchParams({ span: '14d' })), 'GET /v1/outcomes')
    // MUTATION: serve ledgerFixture() canned at 2026-09-25 again -> fails: until is Sep 25.
    expect(ms(ledger.until), 'the ledger ends elsewhere than the clock').toBe(CLOCK)
    expect(ms(ledger.generated_at)).toBe(CLOCK)
    expect(CLOCK - ms(ledger.since), 'the window is not the 14 days it says').toBeLessThanOrEqual(14 * 86_400_000)
    expect(ms(ledger.since)).toBeLessThan(CLOCK)
  }, LONG)
})

describe('f (6:overview-automate-015, -016 box 6343774039, -019 box 6343758363): three times and a field', () => {
  it("serves a parked task's next eligible time after the clock", async () => {
    // The page clock as the capture freezes it, so a time counted from the
    // reader's own clock cannot pass by being minted later.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(CLOCK)
    const parked = (await fixtureTasks()).filter((t) => t.state === 'PARKED' && t.next_eligible_at !== null)
    expect(parked.length, 'no parked task carries a time').toBeGreaterThan(0)
    for (const t of parked) {
      // MUTATION: put next_eligible_at back at at(minsAgo - 15) -> fails: 9 minutes before the clock.
      expect(ms(t.next_eligible_at), `${t.id} is eligible already`).toBeGreaterThan(CLOCK)
    }
  }, LONG)

  it('resets every unreset five-hour window within five hours of the clock', async () => {
    const board = ok(await loadAccountsBoard(), 'GET /v1/accounts')
    const accounts = board.page.accounts
    // THE LIST'S CLOCK IS THE READ'S (QA2-FIX-07, cap2-06): the Accounts list
    // measures on the browser's clock, so the fixture serves its times moved
    // onto it, and the windows are read against `readAt`, not FIXTURE_CLOCK.
    const clock = board.readAt
    const windows = accounts.map((a) => ({ id: a.account_id, w: a.windows.five_hour })).filter((x) => x.w !== undefined && x.w !== null && !x.w.reset)
    expect(windows.length, 'no account has an open five-hour window').toBeGreaterThan(1)
    for (const { id, w } of windows) {
      // MUTATION: put overflow's five-hour reset back at 19h 43m -> fails on u-bogdan:overflow.
      expect(ms(w?.resets_at) - clock, `${id}'s five-hour window resets outside five hours`).toBeLessThanOrEqual(5 * 3_600_000)
      expect(ms(w?.resets_at), `${id}'s open window reset before the clock`).toBeGreaterThan(clock)
    }
  }, LONG)

  it('says which runner profiles take MCP servers, as input_contract.inputs.mcp_servers', async () => {
    const profiles = ok(await loadCapacity(), 'GET /v1/capacity').runner_profiles ?? {}
    // The catalogue's MCP-taking profiles the fixture lists (swarm_api.runnerinputs.input_contract).
    for (const name of ['claude-code', 'codex']) {
      const contract = profiles[name]?.input_contract as { inputs?: Record<string, unknown> } | undefined
      // MUTATION: drop `inputs` from FIXTURE_INPUT_CONTRACTS['claude-code'] -> fails: McpServers' form reads no MCP profile.
      expect(contract?.inputs?.mcp_servers, `${name} does not say it takes MCP servers`).toBeTruthy()
    }
    const mock = profiles.mock?.input_contract as { inputs?: Record<string, unknown> } | undefined
    expect(mock?.inputs?.mcp_servers, 'mock takes no MCP server').toBeUndefined()
  }, LONG)
})

describe('g (box 6329561768): a step that never started has no start, no cost and no run', () => {
  it('serves a never-admitted CANCELLED step with no started_at, no attempt and no cost', async () => {
    const tasks = await fixtureTasks()
    const never = tasks.filter((t) => t.state === 'CANCELLED' && t.current_generation === 0)
    expect(never.length, 'the fixture has no step cancelled before it ran').toBeGreaterThan(0)
    const usage = ok(await loadWorkflowUsage(never.map((t) => t.id)), 'the step usage read')
    for (const t of never) {
      // MUTATION: give task_fan_join attempt_count 1 and generation 1 again -> fails: it 'ran' and cost.
      expect(t.started_at, `${t.id} never started but has a start`).toBeNull()
      expect(await attemptsOf(t.id), `${t.id} never started but has attempts`).toEqual([])
      expect(usage.byTaskId.get(t.id)?.costUsd ?? null, `${t.id} never started but has a cost`).toBeNull()
    }
  }, LONG)
})

describe('h (box 6343765465): no attempt of a finished task is running', () => {
  it('ends every started attempt of a terminal task', async () => {
    const finished = (await fixtureTasks()).filter((t) => TERMINAL_STATES.has(t.state))
    let visited = 0
    for (const t of finished) {
      for (const a of await attemptsOf(t.id)) {
        visited += 1
        // MUTATION: make fixtureSpans leave a terminal task's newest attempt open (ended: null) -> fails.
        if (a.started_at !== null) expect(a.completed_at, `${t.id} ${a.attempt_id} is still running`).not.toBeNull()
      }
    }
    expect(visited, 'no attempt of a finished task was read').toBeGreaterThan(5)
  }, LONG)
})

describe('i (boxes 6343778433, 6343779183, 6343783860): Agents, Timeline, Workflows and Quota agree', () => {
  it("gives the Agents list's start and the Timeline's attempt start one time, read 14 s apart", async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    const tasks = await fixtureTasks()
    clockOn(14_000)
    const page = ok(
      await loadAttemptsPage({ since: new Date(CLOCK - 3 * 3_600_000).toISOString(), until: new Date(Math.max(CLOCK, Date.now()) + 3_600_000).toISOString() }),
      'GET /v1/attempts',
    )
    let compared = 0
    for (const t of tasks.filter((x) => x.started_at !== null)) {
      const newest = page.attempts.filter((a) => a.task_id === t.id).sort((a, b) => b.generation - a.generation)[0]
      if (newest === undefined) continue
      compared += 1
      // MUTATION: build fixtureTaskRows' `now` from Date.now() again -> fails: the Timeline's read is 14 s later.
      expect(newest.started_at, `${t.id}: Agents and Timeline give two starts`).toBe(t.started_at)
    }
    expect(compared, 'no task was found on both reads').toBeGreaterThan(5)
  }, LONG)

  it("counts Workflows' Steps done as the step tasks that succeeded", async () => {
    const board = ok(await loadWorkflowBoard(), 'GET /v1/workflows')
    let counted = 0
    for (const w of board.workflows) {
      if (w.rollup === undefined || w.rollup === null) continue
      const states = w.steps.map((s) => (s.task_id ? board.taskById?.get(s.task_id)?.state : undefined))
      const tally: Record<string, number> = {}
      for (const s of states) tally[s ?? 'unstarted'] = (tally[s ?? 'unstarted'] ?? 0) + 1
      counted += 1
      // MUTATION: type wf_audit_01's rollup counts as { SUCCEEDED: 1, RUNNING: 2, READY: 1, unstarted: 1 } again -> fails: 1 of 2.
      expect(w.rollup.counts, `${w.workflow_id}'s rollup counts other steps than it has`).toEqual(tally)
    }
    expect(counted, 'no workflow carries a rollup').toBeGreaterThan(0)
  }, LONG)

  it("feeds each quota cap into its pool's ceiling, as the broker writes it", async () => {
    const [quota, cap] = await Promise.all([loadAdminQuota(), loadCapacity()])
    const pools = new Map(ok(cap, 'GET /v1/capacity').pools.map((p) => [p.name, p]))
    const rows = ok(quota, 'GET /v1/admin/quota').quota
    expect(rows.length).toBeGreaterThan(3)
    for (const q of rows) {
      const name = `provider:${q.provider}:tenant:${q.tenant_id}`
      const pool = pools.get(name)
      expect(pool, `${name} is not on the board`).toBeDefined()
      if (pool === undefined || pool.hard_limit === null) continue
      // MUTATION: drop quota_derived_limit from the provider pools -> fails: openai · eng is exhausted at 0 and its pool reads 5.
      expect(pool.effective_limit, `${name}'s ceiling ignores its quota cap ${q.effective_limit}`).toBe(Math.min(pool.hard_limit, q.effective_limit))
      expect(pool.available, `${name}'s room is not its ceiling less its leases`).toBe(Math.max(0, Math.min(pool.hard_limit, q.effective_limit) - pool.active))
      // MUTATION: drop `feeds_pool` from fixtureAdminQuota -> fails: Provider quota cannot say the ceiling sits below the cap.
      expect(q.feeds_pool?.effective_limit, `${name}: the quota row names another ceiling than Pools`).toBe(pool.effective_limit)
    }
  }, LONG)
})
