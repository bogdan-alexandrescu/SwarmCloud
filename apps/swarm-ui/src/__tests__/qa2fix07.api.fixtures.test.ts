// QA2-FIX-07 (part of #2694, the console visual QA pass 2 epic, and #2391):
// the pass-2 findings whose main file is api.ts. Each case reads what the
// fixture build SERVES, through the real loaders, as qa1b14.api.fixtures.test.ts
// does: the defects were in the fixture. The one live-path case (automate2-01)
// turns the fixtures off and reads the URL the loader asks for.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { chooseTenant } from '../fetch'

type Api = typeof import('../api')

function setPersonaCookie(persona: string | null): void {
  document.cookie =
    persona === null
      ? 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
      : `swarm_qa_persona=${persona}; path=/`
}

/** api.ts as a page loaded under `persona` (null: no cookie), so its fixture state starts empty. */
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

afterEach(() => {
  setPersonaCookie(null)
  chooseTenant(null)
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('admin-shell-01: the eng card\'s link opens pages that describe eng', () => {
  it('a platform admin who picks eng acts in eng on the profile and on GitHub access', async () => {
    const api = await apiAs('platform-admin')
    chooseTenant('eng')
    const profile = ok(await api.loadProfile(), 'profile')
    const acting = profile.workspaces.find((w) => w.is_acting)
    // MUTATION: `is_acting: true` back on u-bogdan in fixtureProfile -> 'u-bogdan', 'user' -> fails.
    expect(acting?.tenant_id).toBe('eng')
    expect(acting?.kind).toBe('group')
    // MUTATION: `tenant_id: FIXTURE_TENANT` back in fixtureAccess -> 'u-bogdan' -> fails.
    expect(ok(await api.loadAccess(), 'access').tenant_id).toBe('eng')
  })

  it('with no pick, the personal workspace is still the acting one', async () => {
    const api = await apiAs('platform-admin')
    const profile = ok(await api.loadProfile(), 'profile')
    expect(profile.workspaces.find((w) => w.is_acting)?.tenant_id).toBe('u-bogdan')
  })
})

describe('admin2-08: a route the fixture does not serve is named, not called a repository route', () => {
  it('names the method and the template it was asked for', async () => {
    const api = await apiAs(null)
    const r = await api.authorizeGitHub()
    expect(r.status).toBe('error')
    const message = r.status === 'error' ? r.error.message : ''
    // MUTATION: restore 'serves no repository or git-token routes.' -> fails.
    expect(message).toBe('The fixture build does not serve this route (POST /v1/onboarding/github/authorize).')
  })
})

describe('automate2-01: the Approvals list can ask for the history, the inbox reads stay pending', () => {
  it('sends state=all when asked, and no state by default', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const asked: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      asked.push(String(url))
      return new Response(JSON.stringify({ approvals: [], tenant_id: 'u-bogdan' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      })
    }))
    const api = await import('../api')
    await api.loadApprovals({ state: 'all' })
    await api.loadApprovals({ kind: 'plan', state: 'all' })
    await api.loadApprovals()
    // MUTATION: drop the `state` query in loadApprovals -> no state=all -> fails.
    expect(asked[0]).toBe('/v1/approvals?state=all')
    expect(asked[1]).toBe('/v1/approvals?kind=plan&state=all')
    expect(asked[2]).toBe('/v1/approvals')
  })
})

describe('automate2-12 and automate2-13: the listed schedules open, and say their state', () => {
  it('each listed row carries the state word the route serves', async () => {
    const api = await apiAs(null)
    const rows = ok(await api.loadSchedules(), 'schedules').schedules as { schedule_id: string; state_word?: string }[]
    // MUTATION: drop state_word from fixtureScheduleRows -> undefined -> fails.
    expect(rows.map((s) => [s.schedule_id, s.state_word])).toEqual([
      ['sch_3f9a0c1e7b25', 'never_fired'],
      ['sch_8b2e6d4a1c90', 'paused'],
    ])
  })

  it('a listed schedule opens with no firings, and an unknown one is the route\'s 404', async () => {
    const api = await apiAs(null)
    const one = ok(await api.loadSchedule('sch_3f9a0c1e7b25'), 'schedule')
    // MUTATION: remove loadSchedule's fixture branch -> a fetch to the dev server -> fails.
    expect(one.schedule.name).toBe('Plan open issues')
    expect(one.firings).toEqual([])
    expect((await api.loadSchedule('sch_000000000000')).status).toBe('error')
  })

  it('New schedule reads the catalogue of the listed schedules\' types', async () => {
    const api = await apiAs(null)
    const types = ok(await api.loadScheduleTypes(), 'schedule types').types
    // MUTATION: restore scheduleNotServed in loadScheduleTypes -> an error -> fails.
    expect(types.map((t) => t.name)).toEqual(['issue-plan-only', 'cost-report'])
    expect(types.every((t) => t.available)).toBe(true)
  })
})

describe('cap2-06: the account sheet, the list and the health board tell one story', () => {
  it('weekly-burn reads limited with two events in 24 h in its sheet, as the board draws it', async () => {
    const api = await apiAs(null)
    const board = ok(await api.loadAccountHealthBoard(), 'board')
    const onBoard = board.health.kind === 'ok' ? board.health.pool.accounts.find((a) => a.account_id === 'u-bogdan:weekly-burn') : undefined
    const sheet = ok(await api.loadAccountHealth('u-bogdan:weekly-burn'), 'sheet health')
    const total = (c: Record<string, number>) => Object.values(c).reduce((n, v) => n + v, 0)
    expect(onBoard?.status).toBe('limited')
    // MUTATION: restore the sheet's own plan (a single network event, healthy) -> 'healthy', 0 -> fails.
    expect(sheet.status).toBe(onBoard?.status)
    expect(total(sheet.counts.last_24h as unknown as Record<string, number>)).toBe(2)
    // On the browser's clock, which the sheet measures on: read now, not at FIXTURE_CLOCK.
    expect(Math.abs(Date.now() - Date.parse(sheet.observed_at ?? ''))).toBeLessThan(60_000)
  })

  it('the Accounts list reads primary as read minutes ago on the page\'s clock, not days', async () => {
    const api = await apiAs(null)
    const board = ok(await api.loadAccountsBoard(), 'accounts')
    const primary = board.page.accounts.find((a) => a.account_id === 'u-bogdan:primary')
    const ago = Date.now() - Date.parse(primary?.observed_at ?? '')
    // MUTATION: serve fixtureAccounts unmoved -> about ten days -> fails.
    expect(ago).toBeGreaterThanOrEqual(0)
    expect(ago).toBeLessThan(10 * 60_000)
  })
})

describe('config2-06: the personal workspace is the signed-in person\'s', () => {
  it('alice@example.com\'s personal workspace is named Alice', async () => {
    const api = await apiAs(null)
    const me = ok(await api.loadMe(), 'me')
    expect(me.principal.email).toBe('alice@example.com')
    // MUTATION: restore 'Bogdan' -> fails.
    expect(me.tenant.display_name).toBe('Alice')
  })
})

describe('overview-agents-04: Hide keeps the checklist hidden in the fixture build', () => {
  it('the dismiss is answered and the next read says so', async () => {
    const api = await apiAs(null)
    expect(ok(await api.loadOnboarding(), 'onboarding').dismissed).toBe(false)
    // MUTATION: remove dismissOnboarding's fixture branch -> the 404 -> fails.
    const hid = ok(await api.dismissOnboarding(true), 'dismiss')
    expect(hid.dismissed).toBe(true)
    expect(hid.dismissed_at).not.toBeNull()
    expect(ok(await api.loadOnboarding(), 'onboarding again').dismissed).toBe(true)
  })
})

describe('runs2-01 and runs2-02: Submit lands on what it created in the fixture build', () => {
  it('a started agent is a QUEUED task its page can read', async () => {
    const api = await apiAs(null)
    // MUTATION: remove fixtureSubmitTask -> not a function -> fails.
    const made = await api.fixtureSubmitTask({ runner_profile: 'claude-code', input: { prompt: 'Draw the cost column' } })
    expect(made.task.state).toBe('QUEUED')
    expect(made.task.runner_profile).toBe('claude-code')
    const detail = ok(await api.loadAgentDetail(made.task.id), 'the new agent')
    expect(detail.task.id).toBe(made.task.id)
  })

  it('a submitted workflow is readable by its id, with the steps and priority sent', async () => {
    const api = await apiAs(null)
    // MUTATION: remove fixtureSubmitWorkflow -> not a function -> fails.
    const made = await api.fixtureSubmitWorkflow({ steps: [{ step_id: 'generic', runner_profile: 'generic', depends_on: [] }], priority: 5 })
    const read = ok(await api.loadWorkflow(made.workflow.workflow_id), 'the new workflow')
    expect(read.workflow.steps.map((s) => s.step_id)).toEqual(['generic'])
    expect(read.workflow.priority).toBe(5)
    expect(read.workflow.state).toBe('QUEUED')
  })
})

describe('runs2-06, runs2-20 and runs2-24: a planned run keeps its read, its rejection and its own workflow', () => {
  const body = { issue: 'example-org/agent-swarm-infra#512', plan_approval: 'required' as const, auto_merge: false, fix_rounds: 3 }

  it('a run planned now keeps the issue read it was planned from', async () => {
    const api = await apiAs(null)
    const run = ok(await api.createRun(body), 'plan this issue').run
    // MUTATION: drop issue_read from fixtureCreateRun -> null -> "older than the issue snapshot" -> fails.
    expect(run.issue_read?.title).toMatch(/cost column/)
    expect(run.issue_read?.read_at).toBe(run.created_at)
  })

  it('the reason typed is kept on the rejected run', async () => {
    const api = await apiAs(null)
    const run = ok(await api.createRun(body), 'plan this issue').run
    const rejected = ok(await api.rejectPlan(run.id, run.plan_digest, '  Too broad, split it '), 'reject').run
    // MUTATION: call fixturePlanAction without the reason -> null -> fails.
    expect(rejected.rejection_reason).toBe('Too broad, split it')
    expect(ok(await api.loadRun(run.id), 'reread').run.rejection_reason).toBe('Too broad, split it')
  })

  it('an approved plan starts a workflow of its own steps, just queued', async () => {
    const api = await apiAs(null)
    const run = ok(await api.createRun(body), 'plan this issue').run
    const approved = ok(await api.approvePlan(run.id, run.plan_digest ?? ''), 'approve').run
    // MUTATION: link wf_audit_01 again -> its five steps -> fails.
    expect(approved.workflow_id).not.toBe('wf_audit_01')
    const wf = ok(await api.loadWorkflow(approved.workflow_id ?? ''), 'the started workflow').workflow
    expect(wf.steps.map((s) => [s.step_id, s.depends_on])).toEqual([['api', []], ['ui', ['api']]])
    expect(wf.state).toBe('QUEUED')
    expect(wf.steps.every((s) => !s.task_id)).toBe(true)
  })
})
