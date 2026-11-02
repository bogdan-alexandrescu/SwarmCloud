// QA1B-14 (part of #2596): pass 1b of the 2026-10-28 visual QA applies, on
// main, the api.ts patches pass-1 lanes wrote outside their territory. Each
// case reads what the fixture build SERVES, through the real loaders, as
// qafix4.fixture.reads.test.ts does: the defects were in the fixture.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { chooseTenant } from '../fetch'

type Api = typeof import('../api')

function setPersonaCookie(persona: string | null): void {
  document.cookie =
    persona === null
      ? 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      : `swarm_qa_persona=${persona}; path=/`
}

/** api.ts as a page loaded under `persona` (null: no cookie). */
async function apiAs(persona: string | null): Promise<Api> {
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  setPersonaCookie(persona)
  vi.resetModules()
  return import('../api')
}

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

const NOW = Date.parse('2026-10-19T12:00:00Z')

afterEach(() => {
  setPersonaCookie(null)
  chooseTenant(null)
})

describe('Account health draws the resets Accounts draws (QA1-FIX-01 capacity-04)', () => {
  it('weekly-burn limits until its 7-day reset and five-hour-floor until its 5-hour one', async () => {
    const api = await apiAs(null)
    const board = ok(await api.loadAccountHealthBoard(), 'the health board')
    expect(board.health.kind).toBe('ok')
    const pool = board.health.kind === 'ok' ? board.health.pool : null
    for (const [id, win] of [['u-bogdan:weekly-burn', 'seven_day'], ['u-bogdan:five-hour-floor', 'five_hour']] as const) {
      const acct = board.page.accounts.find((a) => a.account_id === id)
      const health = pool?.accounts.find((a) => a.account_id === id)
      const resets = (acct?.windows as Record<string, { resets_at: string | null }> | undefined)?.[win]?.resets_at
      // MUTATION: put back the 20 h / 50 min resets -> fails.
      expect(health?.limited_until, id).toBe(resets)
    }
  })
})

describe('a never-admitted fixture agent (QA1 overview-agents-56, -58)', () => {
  it('the READY agent counts no attempt, has a submitted event and names the pool that refuses it', async () => {
    const api = await apiAs(null)
    const task = ok(await api.loadTask('task_950b8e4a'), 'the READY task')
    expect(task.state).toBe('READY')
    // MUTATION: attempt_count 1 for READY -> fails.
    expect(task.attempt_count).toBe(0)
    // MUTATION: drop the waiting_for -> fails.
    expect(task.waiting_for?.lead?.pool).toBe('provider:anthropic:tenant:u-bogdan')
    expect(task.waiting_for?.lead?.state).toBe('full')
    const page = ok(await api.loadTaskEventsPage('task_950b8e4a', { order: 'asc' }), 'its events')
    // MUTATION: drop the submitted event -> the events read answers none.
    expect(page.events.map((e) => e.type)).toEqual(['submitted'])
    const queued = ok(await api.loadTask('task_e7d210b6'), 'the QUEUED task')
    expect(queued.attempt_count).toBe(0)
  })
})

describe('Submit and the runs fixtures (QA1-FIX-12, -22, -24)', () => {
  it('the issue preview names the capacity tenant and answers the merge step', async () => {
    const api = await apiAs(null)
    const p = ok(await api.loadIssuePreview('example-org/swarm#12'), 'the preview')
    expect(p.tenant_id).toBe('u-bogdan')
    expect(p.auto_merge).toEqual({ available: true, requires: '#295', reason: null })
  })

  it('the running run names a workflow the fixture holds and its whole history', async () => {
    const api = await apiAs(null)
    const run = ok(await api.loadRun('run_19ab77e0'), 'the running run').run
    // MUTATION: wf_8b21d0e4 -> fails.
    expect(run.workflow_id).toBe('wf_audit_01')
    expect(run.history.map((h) => h.to)).toEqual(['PLANNING', 'PLANNED', 'APPROVED', 'RUNNING'])
  })

  it('a planned run is a new run, and an approval stays on the next read', async () => {
    const api = await apiAs(null)
    const made = ok(await api.createRun({ issue: 'example-org/swarm#12', plan_approval: 'required', auto_merge: true, fix_rounds: 2 }), 'the create').run
    expect(made.id).not.toBe('run_4c1e09d2')
    expect(made.auto_merge).toBe(true)
    const runs = ok(await api.loadRuns(), 'the list').runs
    expect(runs.map((r) => r.id)).toContain(made.id)
    const approved = ok(await api.approvePlan('run_4c1e09d2', 'digest'), 'the approval').run
    // Its own workflow, not wf_audit_01's five steps (QA2-FIX-07, runs2-24).
    expect(approved.workflow_id).not.toBe('wf_audit_01')
    expect(ok(await api.loadWorkflow(approved.workflow_id ?? ''), 'its workflow').workflow.steps.map((st) => st.step_id)).toEqual(['api', 'ui'])
    const again = ok(await api.loadRun('run_4c1e09d2'), 'the re-read').run
    // MUTATION: forget the decision -> the re-read is PLANNED again.
    expect(again.state).toBe('RUNNING')
    expect(again.history.at(-1)?.to).toBe('RUNNING')
  })
})

describe('Holders names each lease by a task the agent fixture knows (QA1-FIX-12 chrome-C25)', () => {
  it('every fixture lease holds a served task', async () => {
    const api = await apiAs('platform-admin')
    const leases = ok(await api.loadLeases(), 'the leases').leases
    expect(leases.length).toBeGreaterThan(0)
    for (const l of leases) {
      // MUTATION: `task_${id}` -> task_d1 is not a task -> fails.
      expect((await api.loadTask(l.task_id)).status, l.task_id).toBe('ok')
    }
  })
})

describe('the bell dates Carol\'s request from the fixture clock (QA1 admin-shell-06)', () => {
  it('40 minutes before FIXTURE_NOW, not before the browser clock', async () => {
    const api = await apiAs(null)
    const feed = ok(await api.loadNotifications(), 'the feed')
    expect(Date.parse(feed.items[0]!.at)).toBe(NOW - 40 * 60_000)
  })
})

describe('the secrets overview (QA1-FIX-25 config-22)', () => {
  it('the workspace provider key is set and the GitHub token names a forge login', async () => {
    const api = await apiAs(null)
    const doc = ok(await api.loadSecretsOverview('workspace'), 'the overview')
    const rows = doc.rows as unknown as Record<string, unknown>[]
    expect(rows.find((r) => r.store === 'provider_keys' && r.name === 'typesafe')?.status).toBe('set')
    expect(rows.find((r) => r.store === 'git_tokens')?.forge_login).toBe('alice-example')
  })
})

describe('/v1/tenants/me honours the pick (QA1-FIX-32 admin-shell-01)', () => {
  it('an admin who picks eng reads eng; the member still reads its own workspace', async () => {
    let api = await apiAs('platform-admin')
    chooseTenant('eng')
    const me = ok(await api.loadMe(), 'me as the admin')
    // MUTATION: ignore the pick -> u-bogdan -> fails.
    expect(me.tenant.tenant_id).toBe('eng')
    expect(me.tenant.namespace).toBe('swarm-tenant-eng')
    api = await apiAs(null)
    chooseTenant('eng')
    expect(ok(await api.loadMe(), 'me as the member').tenant.tenant_id).toBe('u-bogdan')
  })
})

describe('onboarding and Submit\'s repository picker (QA1-FIX-10)', () => {
  it('onboarding carries config_setup, and the picker lists the repositories open to agents', async () => {
    const api = await apiAs(null)
    const doc = ok(await api.loadOnboarding(), 'onboarding') as unknown as Record<string, unknown>
    // MUTATION: drop config_setup -> fails.
    expect((doc.config_setup as { secrets: { needed: number } }).secrets.needed).toBe(1)
    const dispatch = await import('../Dispatch')
    const list = ok(await dispatch.loadTaskReadableRepositories(), 'the picker')
    // MUTATION: read through fetch.read in the fixture build -> an error; drop readable_by_tasks -> empty.
    expect(list.repositories.length).toBe(2)
  })
})
