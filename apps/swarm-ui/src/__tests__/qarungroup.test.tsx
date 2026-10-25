// THE QA RUN PAGE, `/qa-runs/<group_id>` (lane QAENV-UI-RUNPAGE, part of
// #2136; docs/design/qa-environments.md §7, §3.5, §5 "Between legs"; Config
// proposal E, S9b and S9d, BGD's pick 2026-10-24).
//
// WHAT EACH CASE HOLDS, over `GET /v1/qa-runs/{group_id}` as routes/qatargets.py
// serves it (`QaTargets.group`), each leg's `GET /v1/workflows/{id}`, the
// steps' artifacts and the target's per-leg `discovered`:
//   * two legs draw two columns, each with its environment and its workflow's
//     state, the workflow linked to its own page;
//   * a leg still `pending` after its neighbour started says "not started"
//     with why, and a `not_started` leg says the refusal's own sentence;
//   * the routes seen on one leg only are listed above the columns, both ways;
//   * Work › Submit a workflow's "Visual QA of a registered app" entry leads
//     to the QA targets card's form, so there is one form;
//   * `/qa-runs/<id>` and its address are inverses;
//   * the routes a run found that are not stored as screens yet (each leg's
//     `new_since_last_run`) are listed under "New since last run" and counted
//     by a lead above the run's own; Promote, a workspace admin's PATCH of
//     `screens`, stores one and it leaves the table; a member sees no Promote;
//     a run with none draws neither (QA-RUN-NEW-ROUTES, part of #2157).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
// The fixture build's catalogue read, behind the workflow form, is slower than one live answer.
const FORM_WAIT = { timeout: 12000 }
const realFetch = globalThis.fetch
const GROUP = 'qrg_' + '5e'.repeat(8)
const TARGET = 'qat_' + '2c'.repeat(8)
const WF_STAGING = 'wf_' + 'a1'.repeat(10)
const WF_PRODUCTION = 'wf_' + 'b2'.repeat(10)

type Json = Record<string, unknown>

function leg(environment: string, over: Json = {}): Json {
  return { environment, state: 'submitted', workflow_id: null, browser_steps: ['capture-g1'], reason: null, workflow_state: null, ...over }
}

function group(legs: Json[]): Json {
  return {
    qa_run: {
      group_id: GROUP, tenant_id: 'eng', target_id: TARGET, revision: 4, file: 'issues', browser_tool: null,
      schedule_id: null, by: 'vera@example.com', at: '2026-10-23T21:12:00+00:00', legs,
    },
  }
}

function workflow(id: string, state: string, steps: [string, string][]): Json {
  return {
    workflow: { workflow_id: id, state, tenant_id: 'eng', created_at: '2026-10-23T21:12:01+00:00', steps: steps.map(([s]) => ({ step_id: s })) },
    tasks: steps.map(([s, st]) => ({ id: `task_${id}_${s}`, step_id: s, workflow_id: id, state: st, tenant_id: 'eng' })),
  }
}

function target(discovered: Record<string, string[]>): Json {
  return {
    qa_target: {
      id: TARGET, tenant_id: 'eng', name: 'Studio', revision: 4,
      screens: [{ id: 'home', path: '/' }],
      environments: Object.entries(discovered).map(([name, paths]) => ({
        name, host: `${name}.example.test`, environment: name === 'production' ? 'production' : 'non_production',
        discovered: paths.map((p, i) => ({ id: `r${i}`, path: p, first_seen: '2026-10-23T21:40:00+00:00' })),
      })),
    },
  }
}

function live(answers: Record<string, Json>) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m !== 'GET') return null
    const body = answers[path]
    return body === undefined ? null : { status: 200, body }
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

async function mountRun() {
  // Imported by a computed name, so each case fails on its own while the page
  // does not exist, rather than the file failing to collect.
  const page = '../QaRunGroup'
  const { QaRunGroupScreen } = (await import(/* @vite-ignore */ page)) as typeof import('../QaRunGroup')
  render(<QaRunGroupScreen groupId={GROUP} go={vi.fn()} />)
}

const legs = () => Array.from(document.querySelectorAll<HTMLElement>('.qa-leg'))
const legOf = (env: string) => document.querySelector<HTMLElement>(`.qa-leg[data-environment="${env}"]`)

afterEach(() => {
  cleanup()
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
  window.localStorage.clear()
  window.sessionStorage.clear()
})

describe('QA run page: one column per leg (QAENV-UI-RUNPAGE, §7)', () => {
  it('two legs draw two columns, each with its environment, its workflow state and a link to the workflow', async () => {
    const calls = live({
      [`/v1/qa-runs/${GROUP}`]: group([
        leg('staging', { workflow_id: WF_STAGING, workflow_state: 'SUCCEEDED' }),
        leg('production', { workflow_id: WF_PRODUCTION, workflow_state: 'RUNNING' }),
      ]),
      [`/v1/workflows/${WF_STAGING}`]: workflow(WF_STAGING, 'SUCCEEDED', [['capture-g1', 'SUCCEEDED'], ['review-g1', 'SUCCEEDED'], ['report', 'SUCCEEDED']]),
      [`/v1/workflows/${WF_PRODUCTION}`]: workflow(WF_PRODUCTION, 'RUNNING', [['capture-g1', 'RUNNING'], ['review-g1', 'READY'], ['report', 'READY']]),
      [`/v1/qa-targets/${TARGET}`]: target({ staging: [], production: [] }),
    })
    await mountRun()
    // MUTATION: draw the legs as one list, or drop one, and the page is no longer a column per environment.
    await waitFor(() => expect(legs().length).toBe(2), WAIT)
    // MUTATION: read any route but the group's own, in its tenant.
    expect(calls.some((c) => c.url.split('?')[0] === `/v1/qa-runs/${GROUP}`)).toBe(true)
    const staging = legOf('staging')!
    const production = legOf('production')!
    // MUTATION: order the columns other than as the group lists its legs.
    expect(legs().map((l) => l.dataset.environment)).toEqual(['staging', 'production'])
    // MUTATION: draw one state for both, or the group's instead of each leg's workflow's.
    expect(staging.querySelector('[data-leg-state]')?.getAttribute('data-leg-state')).toBe('SUCCEEDED')
    expect(production.querySelector('[data-leg-state]')?.getAttribute('data-leg-state')).toBe('RUNNING')
    expect(visible(staging)).toContain('staging')
    expect(visible(production)).toContain('production')
    // MUTATION: name the workflow without linking it, and nobody can follow it to its own page.
    const link = staging.querySelector<HTMLAnchorElement>(`a[href="/workflows/${WF_STAGING}"]`)
    expect(link).not.toBeNull()
    // MUTATION: count steps from the group rather than the leg's own workflow.
    await waitFor(() => expect(visible(staging.querySelector('[data-figure="steps"]'))).toContain('3 of 3'), WAIT)
    await waitFor(() => expect(visible(production.querySelector('[data-figure="steps"]'))).toContain('0 of 3'), WAIT)
  })

  it('a pending leg shows "not started" with its reason, and a refused leg its refusal', async () => {
    live({
      [`/v1/qa-runs/${GROUP}`]: group([
        leg('staging', { workflow_id: WF_STAGING, workflow_state: 'RUNNING' }),
        leg('production', { state: 'pending' }),
        leg('preview', { state: 'not_started', reason: { code: 'qa_target_host_pending', message: 'This leg’s host is waiting for a platform admin’s approval.' } }),
      ]),
      [`/v1/workflows/${WF_STAGING}`]: workflow(WF_STAGING, 'RUNNING', [['capture-g1', 'RUNNING']]),
      [`/v1/qa-targets/${TARGET}`]: target({ staging: [], production: [], preview: [] }),
    })
    await mountRun()
    await waitFor(() => expect(legs().length).toBe(3), WAIT)
    const pending = legOf('production')!
    // MUTATION: draw a pending leg as running, or as a blank column, and a leg that never started reads as one in flight.
    expect(visible(pending)).toContain('not started')
    expect(pending.querySelector('[data-leg-state]')?.getAttribute('data-leg-state')).toBe('pending')
    // MUTATION: say "not started" without why (§2.1: the crash window between two submissions).
    expect(visible(pending.querySelector('[data-leg-reason]'))).toMatch(/staging started|was never submitted/)
    // MUTATION: drop a refused leg's own sentence.
    expect(visible(legOf('preview')!.querySelector('[data-leg-reason]'))).toContain('waiting for a platform admin')
    // MUTATION: link a workflow for a leg that has none.
    expect(pending.querySelector('a[href^="/workflows/"]')).toBeNull()
  })

  it('lists the routes seen on one leg only, above the columns, both ways, as information', async () => {
    live({
      [`/v1/qa-runs/${GROUP}`]: group([
        leg('staging', { workflow_id: WF_STAGING, workflow_state: 'SUCCEEDED' }),
        leg('production', { workflow_id: WF_PRODUCTION, workflow_state: 'SUCCEEDED' }),
      ]),
      [`/v1/workflows/${WF_STAGING}`]: workflow(WF_STAGING, 'SUCCEEDED', [['report', 'SUCCEEDED']]),
      [`/v1/workflows/${WF_PRODUCTION}`]: workflow(WF_PRODUCTION, 'SUCCEEDED', [['report', 'SUCCEEDED']]),
      [`/v1/qa-targets/${TARGET}`]: target({ staging: ['/beta/reports', '/settings', '/settings/labs'], production: ['/settings', '/legal/imprint'] }),
    })
    await mountRun()
    await waitFor(() => expect(document.querySelector('[data-one-leg-only]')).not.toBeNull(), WAIT)
    const only = document.querySelector<HTMLElement>('[data-one-leg-only]')!
    await waitFor(() => expect(visible(only)).toContain('/beta/reports'), WAIT)
    const rows = Array.from(only.querySelectorAll<HTMLElement>('[data-only-on]'))
    const on = (env: string) => visible(rows.find((r) => r.dataset.onlyOn === env) ?? null)
    // MUTATION: list one direction only, or the union, and "on staging, not on production" says nothing.
    expect(on('staging')).toContain('/beta/reports')
    expect(on('staging')).toContain('/settings/labs')
    expect(on('production')).toContain('/legal/imprint')
    // MUTATION: list a route both legs have.
    expect(visible(only)).not.toMatch(/\/settings(?!\/)/)
    // MUTATION: draw the list below the columns (§7: "above them").
    const first = legs()[0]!
    expect(only.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // MUTATION: offer to file it: §5 says it is information, never a filed finding.
    expect(Array.from(only.querySelectorAll('button')).some((b) => /file/i.test(visible(b)))).toBe(false)
  })
})

function content(taskId: string, name: string, doc: unknown): Json {
  return {
    task_id: taskId, tenant_id: 'eng', attempt_id: 'att_1', artifact: { name, bytes: null, uri: null }, status: 'ok', detail: null,
    key: null, uri: null, content: JSON.stringify(doc), total_bytes: null, offset: 0, returned_bytes: 0, next_offset: null,
    truncated: false, redacted: false, redaction_count: 0, redaction: { applied_at_read_time: true, rules: 6 },
  }
}

describe('QA run page: a leg\'s figures come from its own steps\' files (§3.5)', () => {
  it('sums screens, coverage, findings by severity, issues filed and gate refusals; a running leg says when', async () => {
    const steps: [string, string][] = [['capture-g1', 'SUCCEEDED'], ['capture-g2', 'SUCCEEDED'], ['report', 'SUCCEEDED'], ['file', 'SUCCEEDED']]
    const t = (s: string) => `task_${WF_STAGING}_${s}`
    const files: Record<string, unknown> = {
      [`${t('capture-g1')}|capture-g1/manifest.json`]: { routes: [{ id: 'home' }, { id: 'home' }, { id: 'settings' }, { id: 'beta-reports' }], gate_refusals: { read: true, count: 2 } },
      [`${t('capture-g2')}|capture-g2/manifest.json`]: { routes: [{ id: 'billing' }], gate_refusals: { read: true, count: 1 } },
      [`${t('capture-g1')}|capture-g1/found.json`]: { format: 1, captured_new: [{ id: 'beta-reports', path: '/beta/reports' }], not_owned: [{ path: '/labs', owner: 2 }], over_cap: [], crawl: { stopped_by: 'done' } },
      [`${t('capture-g2')}|capture-g2/found.json`]: { format: 1, captured_new: [], not_owned: [], over_cap: [{ path: '/x' }], crawl: { stopped_by: 'time' } },
      [`${t('report')}|findings.json`]: { format: 1, findings: [{ severity: 'major' }, { severity: 'minor' }, { severity: 'minor' }], about_run: [] },
      [`${t('file')}|filed.json`]: [{ finding: 'g1-1', issue: 41 }, { finding: 'g1-2', issue: '#42' }, { finding: 'g1-3', issue: 42 }],
    }
    serve((m, url) => {
      const [path, query] = url.split('?')
      if (path === `/v1/qa-runs/${GROUP}`) return { status: 200, body: group([leg('staging', { workflow_id: WF_STAGING, workflow_state: 'SUCCEEDED' }), leg('production', { workflow_id: WF_PRODUCTION, workflow_state: 'RUNNING' })]) }
      if (path === `/v1/workflows/${WF_STAGING}`) return { status: 200, body: workflow(WF_STAGING, 'SUCCEEDED', steps) }
      if (path === `/v1/workflows/${WF_PRODUCTION}`) return { status: 200, body: workflow(WF_PRODUCTION, 'RUNNING', [['capture-g1', 'RUNNING'], ['report', 'READY']]) }
      if (path === `/v1/qa-targets/${TARGET}`) return { status: 200, body: target({ staging: [], production: [] }) }
      const m2 = /^\/v1\/tasks\/([^/]+)\/artifacts\/content$/.exec(path ?? '')
      if (m === 'GET' && m2 !== null) {
        const name = new URLSearchParams(query).get('name') ?? ''
        const doc = files[`${decodeURIComponent(m2[1]!)}|${name}`]
        return doc === undefined ? { status: 404, body: { detail: 'Not Found' } } : { status: 200, body: content(m2[1]!, name, doc) }
      }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    await mountRun()
    await waitFor(() => expect(legs().length).toBe(2), WAIT)
    const staging = legOf('staging')!
    const fig = (el: HTMLElement, f: string) => visible(el.querySelector(`[data-figure="${f}"]`))
    // MUTATION: count a route once per width and theme, or count new routes as stored.
    await waitFor(() => expect(fig(staging, 'screens')).toBe('3 stored · 1 new'), WAIT)
    // MUTATION: drop the new route's path, or call a crawl stopped by its time bound complete.
    expect(fig(staging, 'coverage')).toContain('/beta/reports')
    expect(fig(staging, 'coverage')).toContain('1 route found past the step’s cap')
    expect(fig(staging, 'coverage')).toContain('1 route found, not reached by the step that owns it')
    expect(fig(staging, 'coverage')).toContain('time bound')
    // MUTATION: draw findings as one total, or in another order than most severe first.
    expect(fig(staging, 'findings')).toBe('1 major · 2 minor')
    // MUTATION: count filed.json's rows instead of the distinct issues they name.
    expect(fig(staging, 'issues')).toBe('2')
    // MUTATION: drop a capture step's refusals from the sum.
    expect(fig(staging, 'refusals')).toBe('3')
    const production = legOf('production')!
    // MUTATION: draw 0 for a figure not read yet: a running leg says when it fills in.
    await waitFor(() => expect(fig(production, 'findings')).toContain('When the report step ends'), WAIT)
    expect(production.querySelector('[data-figure="findings"] .c-dash')).not.toBeNull()
    expect(fig(production, 'screens')).toContain('When a capture step ends')
  })
})

describe('Work › Submit a workflow: "Visual QA of a registered app" (§7, one form)', () => {
  it('leads to the QA targets card’s form, and draws no second form', async () => {
    // The fixture build's catalogue: the cases above left the API module live.
    vi.resetModules()
    const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
    render(<SubmitWorkflowScreen />)
    const entry = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-entry="visual-qa"]')
      expect(el).not.toBeNull()
      return el!
    }, FORM_WAIT)
    // MUTATION: drop the entry, and a member looking for visual QA on Submit finds nothing.
    expect(visible(entry)).toContain('Visual QA of a registered app')
    const link = entry.querySelector<HTMLAnchorElement>('a')
    // MUTATION: lead anywhere but Config › Repositories › QA targets.
    expect(link?.getAttribute('href')).toBe('/config/repositories?page=qa-targets')
    // MUTATION: draw a second QA form here (a target picker, environment boxes, a URL field).
    expect(entry.querySelectorAll('input, select, textarea').length).toBe(0)
  }, 20000)
})

describe('/qa-runs/<group_id> (paths.ts)', () => {
  it('is its own path, and the address it reads as goes back to it', async () => {
    const { addressToPath, pathToAddress, qaRunAddress } = await import('../paths')
    const route = pathToAddress(`/qa-runs/${GROUP}`)
    // MUTATION: serve no route for the path and the run page's links land on Overview.
    expect(route?.address).toBe(qaRunAddress(GROUP))
    // MUTATION: write the address back as another page's path.
    expect(addressToPath(qaRunAddress(GROUP))).toBe(`/qa-runs/${GROUP}`)
    const { fromAddress } = await import('../App')
    const at = fromAddress(qaRunAddress(GROUP))
    // MUTATION: route it outside Config › Repositories (the mock-up's breadcrumb), or drop the group.
    expect([at.sectionId, at.tab]).toEqual(['config', 'repositories'])
    expect(new URLSearchParams(at.view ?? '').get('qarun')).toBe(GROUP)
  })
})

// -- New since last run (QA-RUN-NEW-ROUTES, part of #2157; config-e.html S9b) ----

type Found = { id: string; path: string; workflow_id?: string }

/** The target as `_entry_api` serves it: each leg's `discovered` and `new_since_last_run`. */
function targetWithNew(fresh: Record<string, Found[]>): Json {
  return {
    qa_target: {
      id: TARGET, tenant_id: 'eng', name: 'Studio', revision: 4,
      screens: [{ id: 'home', path: '/' }, { id: 'settings', path: '/settings' }],
      environments: Object.entries(fresh).map(([name, found]) => ({
        name, host: `${name}.example.test`, environment: name === 'production' ? 'production' : 'non_production',
        discovered: [{ id: 'settings', path: '/settings', first_seen: '2026-10-20T10:00:00+00:00' }, ...found.map((f) => ({ ...f, first_seen: '2026-10-23T21:40:00+00:00' }))],
        new_since_last_run: found.map((f) => ({ ...f, first_seen: '2026-10-23T21:40:00+00:00' })),
      })),
    },
  }
}

function me(role: string): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    role: { name: role, source: 'override' },
  }
}

function liveNew(role: string, fresh: Record<string, Found[]>) {
  const answers: Record<string, Json> = {
    [`/v1/qa-runs/${GROUP}`]: group([
      leg('staging', { workflow_id: WF_STAGING, workflow_state: 'SUCCEEDED' }),
      leg('production', { workflow_id: WF_PRODUCTION, workflow_state: 'SUCCEEDED' }),
    ]),
    [`/v1/workflows/${WF_STAGING}`]: workflow(WF_STAGING, 'SUCCEEDED', [['report', 'SUCCEEDED']]),
    [`/v1/workflows/${WF_PRODUCTION}`]: workflow(WF_PRODUCTION, 'SUCCEEDED', [['report', 'SUCCEEDED']]),
    [`/v1/qa-targets/${TARGET}`]: targetWithNew(fresh),
    '/v1/tenants/me': me(role),
  }
  const calls = serve((m, url) => {
    const path = url.split('?')[0] ?? ''
    if (m === 'PATCH' && path === `/v1/qa-targets/${TARGET}`) return { status: 200, body: answers[path]! }
    if (m !== 'GET') return null
    const body = answers[path]
    return body === undefined ? null : { status: 200, body }
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

const FOUND: Record<string, Found[]> = {
  staging: [{ id: 'beta-reports', path: '/beta/reports', workflow_id: WF_STAGING }, { id: 'settings-labs', path: '/settings/labs', workflow_id: WF_STAGING }],
  production: [{ id: 'legal-imprint', path: '/legal/imprint', workflow_id: WF_PRODUCTION }],
}

const newCard = () => document.querySelector<HTMLElement>('[data-new-routes]')
const newLead = () => document.querySelector<HTMLElement>('[data-new-routes-lead]')
const newRow = (path: string) =>
  Array.from(newCard()?.querySelectorAll<HTMLElement>('tbody tr') ?? []).find((r) => visible(r.querySelector('code')) === path) ?? null
const promoteOf = (path: string) =>
  Array.from(newRow(path)?.querySelectorAll<HTMLButtonElement>('button') ?? []).find((b) => /^Promote/.test(visible(b))) ?? null

describe('QA run page: New since last run (QA-RUN-NEW-ROUTES, part of #2157; S9b)', () => {
  it('draws the found-but-unstored routes under New since last run, and the lead counts them above the run\'s own', async () => {
    liveNew('admin', FOUND)
    await mountRun()
    // MUTATION: drop the card, and the routes a run found are never offered to store.
    await waitFor(() => expect(newCard()).not.toBeNull(), WAIT)
    const card = newCard()!
    expect(visible(card)).toContain('New since last run')
    // MUTATION: list one leg's routes only, or the target's stored screens.
    expect(newRow('/beta/reports')).not.toBeNull()
    expect(newRow('/settings/labs')).not.toBeNull()
    expect(newRow('/legal/imprint')).not.toBeNull()
    expect(newRow('/settings')).toBeNull()
    // MUTATION: name the wrong leg the route was found on.
    expect(visible(newRow('/legal/imprint'))).toContain('production')
    expect(visible(newRow('/beta/reports'))).toContain('staging')
    // MUTATION: drop "this run" when the route's last sighting is this leg's own workflow.
    expect(visible(newRow('/beta/reports'))).toContain('this run')
    // MUTATION: draw the card above the columns (S9b: "below").
    expect(legs()[legs().length - 1]!.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // MUTATION: count something other than the routes listed, or drop the lead.
    const lead = newLead()
    expect(lead).not.toBeNull()
    expect(visible(lead)).toContain('3 routes found that are not stored yet')
    expect(Array.from(lead!.querySelectorAll('button')).some((b) => visible(b) === 'Review below')).toBe(true)
    // MUTATION: draw it below the run's own lead, or in its place (the run's "ended" lead is still drawn).
    const own = Array.from(document.querySelectorAll<HTMLElement>('.c-banner')).find((b) => /environments ended/.test(visible(b)))
    expect(own).not.toBeUndefined()
    expect(lead!.compareDocumentPosition(own!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('Promote stores one route as a screen (the PATCH of screens) and it leaves the table', async () => {
    const calls = liveNew('admin', FOUND)
    await mountRun()
    await waitFor(() => expect(promoteOf('/legal/imprint')).not.toBeNull(), WAIT)
    fireEvent.click(promoteOf('/legal/imprint')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(1), WAIT)
    const sent = calls.find((c) => c.method === 'PATCH')!
    // MUTATION: patch another target, or another route.
    expect(sent.url).toBe(`/v1/qa-targets/${TARGET}`)
    // MUTATION: replace `screens` with the new route alone, or send anything else, and the stored list is lost.
    expect(sent.body).toEqual({
      screens: [
        { id: 'home', path: '/' },
        { id: 'settings', path: '/settings' },
        { id: 'legal-imprint', path: '/legal/imprint' },
      ],
    })
    // MUTATION: keep a stored route in the table, and the admin promotes it twice.
    await waitFor(() => expect(newRow('/legal/imprint')).toBeNull(), WAIT)
    expect(newRow('/beta/reports')).not.toBeNull()
    await waitFor(() => expect(visible(newLead())).toContain('2 routes found that are not stored yet'), WAIT)

    // A second Promote keeps the first: `screens` holds both.
    fireEvent.click(promoteOf('/beta/reports')!)
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH').length).toBe(2), WAIT)
    // MUTATION: build the second PATCH from the target as first read, and the first promoted route is dropped.
    expect(calls.filter((c) => c.method === 'PATCH')[1]!.body).toEqual({
      screens: [
        { id: 'home', path: '/' },
        { id: 'settings', path: '/settings' },
        { id: 'legal-imprint', path: '/legal/imprint' },
        { id: 'beta-reports', path: '/beta/reports' },
      ],
    })
  })

  it('a member sees the routes and no Promote', async () => {
    liveNew('member', FOUND)
    await mountRun()
    await waitFor(() => expect(newRow('/beta/reports')).not.toBeNull(), WAIT)
    // The role read lands after the target's; wait for the gate's sentence before asserting the absence.
    await waitFor(() => expect(newCard()!.querySelector('[data-role-gate="admin"]')).not.toBeNull(), WAIT)
    // MUTATION: draw Promote to every role, and a member's press is a certain 403.
    expect(Array.from(newCard()!.querySelectorAll('button')).some((b) => /Promote/.test(visible(b)))).toBe(false)
  })

  it('a run with none draws neither the table nor that lead', async () => {
    liveNew('admin', { staging: [], production: [] })
    await mountRun()
    await waitFor(() => expect(legs().length).toBe(2), WAIT)
    // The target's read has landed: its one-leg-only card says so.
    await waitFor(() => expect(visible(document.querySelector('[data-one-leg-only]'))).toContain('Every route found is on every environment'), WAIT)
    // MUTATION: draw an empty table, or a lead counting 0.
    expect(newCard()).toBeNull()
    expect(newLead()).toBeNull()
    expect(document.body.textContent ?? '').not.toContain('not stored yet')
  })
})
