// QA2B-K (part of #2694, the console visual QA pass 2 epic, and #2391): the
// pass-2 fix lanes' patches whose target is api.ts or outcomes.fixture.ts,
// landed here. Each case reads what the fixture build SERVES, through the
// real loaders, as qa2fix07.api.fixtures.test.ts does: the defects were in
// the fixture.

import { afterEach, describe, expect, it, vi } from 'vitest'

type Api = typeof import('../api')

/** api.ts as a freshly loaded page, so its fixture state starts empty. */
async function api(): Promise<Api> {
  expect(import.meta.env.DEV, 'this file reads the fixture build').toBeTruthy()
  expect(import.meta.env.VITE_LIVE, 'VITE_LIVE turns the fixtures off').toBeFalsy()
  vi.resetModules()
  return import('../api')
}

function ok<T>(r: { status: string; data?: T; error?: unknown }, what: string): T {
  expect(r.status, `${what} answered ${r.status}: ${JSON.stringify(r.error ?? null)}`).toBe('ok')
  return r.data as T
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const FAN = 'wf_5e5ad3b6f7da4299a839'

describe('overview-agents-50 (QA2-FIX-13): a workflow step carries the prompt it was given', () => {
  it('the fan-out workflow serves its checkpoints step with an input prompt', async () => {
    const a = await api()
    const wf = ok(await a.loadWorkflow(FAN), 'workflow').workflow
    const step = wf.steps.find((s) => s.step_id === 'checkpoints') as unknown as { input?: { prompt?: unknown } }
    // MUTATION: drop the `{ prompt: ... }` from step('checkpoints') -> {} -> fails.
    expect(typeof step?.input?.prompt).toBe('string')
    expect(String(step?.input?.prompt)).toMatch(/checkpoint/i)
  })
})

describe('runs2-36 (QA2-FIX-16): a workflow read carries its steps\' own tasks', () => {
  it('every workflow\'s read serves each step task the task list holds', async () => {
    const a = await api()
    const all = ok(await a.loadTasks(), 'tasks').tasks.map((t) => t.id)
    let checked = 0
    for (const w of ok(await a.loadWorkflows(), 'workflows').workflows) {
      const read = ok(await a.loadWorkflow(w.workflow_id), w.workflow_id)
      const served = read.tasks.map((t) => t.id)
      for (const id of read.workflow.steps.map((s) => s.task_id)) {
        if (!id || !all.includes(id)) continue
        checked += 1
        // MUTATION: filter on `t.workflow_id === workflowId` alone -> a step task missing -> fails.
        expect(served, `${w.workflow_id} serves ${id}`).toContain(id)
      }
    }
    expect(checked).toBeGreaterThan(0)
  }, 20000)
})

describe('runs2 tenant (QA2-FIX-16): runs and Outcomes are the signed-in workspace\'s', () => {
  it('the runs page and every run on it are u-bogdan\'s, as the capacity fixture is', async () => {
    const a = await api()
    const page = ok(await a.loadRuns(), 'runs')
    // MUTATION: `tenant_id: 'eng'` back in fixtureRuns or fixtureIssueRun -> fails.
    expect(page.tenant_id).toBe('u-bogdan')
    expect(page.runs.length).toBeGreaterThan(0)
    for (const run of page.runs) expect(run.tenant_id).toBe('u-bogdan')
  })

  it('the Outcomes ledger\'s scope and failed workflows are u-bogdan\'s', async () => {
    const a = await api()
    const out = ok(await a.loadOutcomes(new URLSearchParams()), 'outcomes') as unknown as {
      scope: { tenant_id?: string }
      failed_workflows?: { rows: { tenant_id: string }[] }
    } & Record<string, unknown>
    // MUTATION: `tenant_id: 'eng'` back in ledgerFixture's scope -> fails.
    expect(out.scope.tenant_id).toBe('u-bogdan')
    const rows = JSON.stringify(out).match(/"workflow_id":"wf_[^"]+","tenant_id":"[^"]+"/g) ?? []
    expect(rows.length).toBeGreaterThan(0)
    // MUTATION: `tenant_id: 'eng'` back on a failed-workflow row -> fails.
    for (const r of rows) expect(r).toMatch(/"tenant_id":"u-bogdan"$/)
  })
})

describe('runs2-03 (QA2-FIX-20): each fixture run carries the issue it read', () => {
  it('every run on the runs page has an issue_read title', async () => {
    const a = await api()
    const page = ok(await a.loadRuns(), 'runs')
    // MUTATION: drop `issue_read` from fixtureIssueRun -> undefined -> fails.
    for (const run of page.runs) expect(run.issue_read?.title ?? '').not.toBe('')
  })
})

describe('ovag2-03 (QA2-FIX-18): a lease is taken after its task was submitted', () => {
  it('b1 and a1 are leased after their tasks were submitted, their deadlines after the lease', async () => {
    const a = await api()
    const leases = ok(await a.loadLeases(), 'leases').leases
    const tasks = ok(await a.loadTasks(), 'tasks').tasks
    let checked = 0
    for (const l of leases.filter((x) => x.task_id === 'task_19d91b82' || x.task_id === 'task_460409cd')) {
      const t = tasks.find((x) => x.id === l.task_id)
      if (t === undefined) continue
      checked += 1
      // MUTATION: drop `created_at: iso(160)` from row('b1') -> leased 10 min ago, before the task -> fails.
      expect(Date.parse(l.created_at), `${l.lease_id} vs ${t.id}`).toBeGreaterThanOrEqual(Date.parse(t.created_at))
      expect(Date.parse(l.dispatch_deadline ?? l.created_at)).toBeGreaterThanOrEqual(Date.parse(l.created_at))
    }
    expect(checked).toBe(2)
  })
})
