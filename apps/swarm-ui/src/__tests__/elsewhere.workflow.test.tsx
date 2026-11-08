/**
 * A WORKFLOW LINK INTO ANOTHER OF YOUR WORKSPACES OFFERS THE SWITCH (the
 * owner's pick of 2026-11-04, "offer to switch"; elsewhere.tsx).
 *
 * Seen on the live deployment: an Engineering workflow's link, opened in a tab
 * acting as the personal workspace, drew `Not found · HTTP 404. No workflow
 * <id> exists in this tenant.` and nothing else. The page now asks each OTHER
 * workspace the person is a member of -- the profile's list, never a platform
 * admin's reach -- for the same id, one read each, with that workspace's
 * header, and says what they answered.
 *
 * Held here against the real WorkflowsScreen and the real api.ts, with `fetch`
 * stubbed by path and workspace header:
 *
 *   1. found in one other workspace: the sentence and the button, and NO switch;
 *   2. the click switches as the switcher does and reads the page again there;
 *   3. found nowhere: today's 404 sentence, unchanged, and no offer;
 *   4. only the person's own workspaces are asked, never a reach entry;
 *   5. a read that fails is said, never counted as an absence.
 *
 * MUTATIONS: draw the 404 sentence alone again (Workflows.tsx
 * `WorkflowNotHere`); ask `/v1/tenants/mine`'s list instead of the profile's;
 * count a failed read as a 404 in `lookElsewhere`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

// The LIVE read path: USE_FIXTURES is decided when api.ts loads.
vi.hoisted(() => {
  vi.stubEnv('VITE_LIVE', '1')
})

const { WorkflowsScreen } = await import('../Workflows')
const { TENANT_HEADER, TENANT_PREF, chosenTenant, forgetReads, tenantSwitchSnapshot, clearTenantSwitch } = await import('../fetch')
const { forgetToasts, Toaster } = await import('../components')

const WF = 'wf_6d1f0c2a9b8e4f37a5c1'
// The first test pays the page's cold render; under a loaded runner that alone
// passed 4 s (the pre-publish guard run), so the waits allow for it.
const WAIT = { timeout: 15_000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const T0 = Date.now()
const iso = (ms: number) => new Date(T0 + ms).toISOString()

/** The profile's list: memberships only. Group rows carry the group's email as their name. */
function profile(acting: string) {
  const row = (tenant_id: string, display_name: string, kind: string) => ({
    tenant_id,
    display_name,
    kind,
    role: { name: 'member', source: 'derived' },
    state: kind === 'user' ? 'ready' : null,
    selectable: true,
    is_acting: tenant_id === acting,
    is_default: tenant_id === 'u-owner',
  })
  return {
    identity: { email: 'owner@example.com', display_name: null, display_name_suggested: null, domain: 'example.com', platform_role: 'owner', is_admin: true, is_admin_unresolved: false },
    workspaces: [row('u-owner', 'Your workspace (u-owner)', 'user'), row('eng', 'eng@example.com', 'group'), row('ops', 'ops@example.com', 'group')],
    default_workspace: { tenant_id: 'u-owner', source: 'personal', ignored: null },
    connections: {},
    revision: 1,
    unavailable: [],
  }
}

/** `/v1/tenants/mine`: the stored names, and one platform-admin reach entry the profile does not list. */
const MINE = [
  { tenant_id: 'u-owner', display_name: 'Your workspace (u-owner)', kind: 'user', via: 'personal' },
  { tenant_id: 'eng', display_name: 'Engineering', group: 'eng@example.com', kind: 'group', via: 'member' },
  { tenant_id: 'ops', display_name: 'Operations', group: 'ops@example.com', kind: 'group', via: 'member' },
  { tenant_id: 'finance', display_name: 'Finance', group: 'finance@example.com', kind: 'group', via: 'platform_admin' },
]

const TASK = {
  id: 'task_6d1f0c2a9b8e4f37a5c2', tenant_id: 'eng', state: 'SUCCEEDED', runner_profile: 'claude-code', resource_class: 'standard',
  provider: 'anthropic', priority: 0, created_at: iso(-600_000), updated_at: iso(-60_000), started_at: iso(-500_000),
  completed_at: iso(-300_000), submitted_by: 'alice@example.com', attempt_count: 1, max_attempts: 3, park_reason: null,
  blocked_by: null, workflow_id: WF, step_id: 'plan', depends_on: null, cancel_requested: false, input: null,
}
const WORKFLOW = {
  workflow_id: WF, state: 'SUCCEEDED', tenant_id: 'eng', stored_state: 'SUCCEEDED', state_source: 'derived',
  rollup: { state: 'SUCCEEDED', complete: true, reason: null, counts: { SUCCEEDED: 1 }, unreadable_steps: [], unstarted_steps: [], steps_read: 1 },
  created_at: iso(-900_000), updated_at: iso(-60_000), submitted_by: 'alice@example.com', priority: 0,
  on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false,
  steps: [{ step_id: 'plan', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: TASK.id }],
}

interface Sent {
  path: string
  tenant: string | null
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
const notFound = () => json(404, { code: 'not_found', message: 'Not found.' })

/**
 * `fetch`: the workflow answers 200 under `holders`, 503 under `failing`,
 * 404 everywhere else; the profile and `/mine` answer as above.
 */
function serve(holders: readonly string[], failing: readonly string[] = []) {
  const sent: Sent[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const path = url.split('?')[0] ?? url
      const tenant = new Headers(init?.headers).get(TENANT_HEADER)
      sent.push({ path, tenant })
      if (path.endsWith('/v1/profile')) return json(200, profile(tenant ?? 'u-owner'))
      if (path.endsWith('/v1/tenants/mine')) return json(200, MINE)
      if (path.endsWith(`/v1/workflows/${WF}`)) {
        if (tenant !== null && failing.includes(tenant)) return json(503, { code: 'unavailable', message: 'Firestore did not answer.' })
        return tenant !== null && holders.includes(tenant) ? json(200, { workflow: WORKFLOW, tasks: [TASK] }) : notFound()
      }
      return notFound()
    }),
  )
  return sent
}

const workflowReads = (sent: readonly Sent[]) => sent.filter((s) => s.path.endsWith(`/v1/workflows/${WF}`))

beforeEach(() => {
  forgetReads()
  forgetToasts()
  clearTenantSwitch()
  window.sessionStorage.setItem(TENANT_PREF, 'u-owner')
})

afterEach(() => {
  cleanup()
  forgetReads()
  forgetToasts()
  window.sessionStorage.clear()
  vi.unstubAllGlobals()
})

describe('a workflow 404 asks the person’s other workspaces', { timeout: 30_000 }, () => {
  it('says which workspace holds it and offers the switch, without switching', async () => {
    serve(['eng'])
    render(<WorkflowsScreen view={`wf=${WF}`} />)

    const offer = await screen.findByRole('button', { name: 'Switch to Engineering' }, WAIT)
    expect(document.body.textContent).toContain('This workflow is in Engineering, not Your workspace.')
    expect(screen.getByRole('link', { name: 'Back to the list' })).toBeTruthy()
    // Display names, never the group's email.
    expect(document.body.textContent).not.toContain('eng@example.com')
    // Offered, never made.
    expect(chosenTenant()).toBe('u-owner')
    expect(offer).toBeTruthy()
  })

  it('switches as the switcher does when clicked, and reads the page again there', async () => {
    const sent = serve(['eng'])
    render(
      <>
        <WorkflowsScreen view={`wf=${WF}`} />
        <Toaster />
      </>,
    )

    const offer = await screen.findByRole('button', { name: 'Switch to Engineering' }, WAIT)
    const before = workflowReads(sent).filter((s) => s.tenant === 'eng').length
    fireEvent.click(offer)

    expect(chosenTenant()).toBe('eng')
    // The switcher's note and its notice, with the way back.
    expect(tenantSwitchSnapshot()).toEqual({ from: { id: 'u-owner', name: 'Your workspace' }, to: { id: 'eng', name: 'Engineering' }, kept: false })
    expect(document.body.textContent).toContain('Now acting as Engineering')
    expect(screen.getByRole('button', { name: 'Back to Your workspace' })).toBeTruthy()
    // The page reads its workflow again, under the workspace switched to.
    await waitFor(() => expect(workflowReads(sent).filter((s) => s.tenant === 'eng').length).toBeGreaterThan(before), WAIT)
    await waitFor(() => expect(document.body.textContent).not.toContain('This workflow is in'), WAIT)
  })

  it('keeps today’s 404 sentence when no other workspace has it', async () => {
    const sent = serve([])
    render(<WorkflowsScreen view={`wf=${WF}`} />)

    await waitFor(() => expect(workflowReads(sent).map((s) => s.tenant).sort()).toEqual(['eng', 'ops', 'u-owner']), WAIT)
    await waitFor(() => expect(document.querySelector('[data-elsewhere]')).toBeNull(), WAIT)
    const missing = document.querySelector('.wfp-missing')
    expect(missing?.textContent).toBe(`Not found · HTTP 404. No workflow ${WF} exists in this tenant. Back to the list`)
    expect(screen.queryByRole('button', { name: /^Switch to/ })).toBeNull()
    expect(chosenTenant()).toBe('u-owner')
  })

  it('asks only the person’s own workspaces, each once, in parallel', async () => {
    const sent = serve(['eng', 'finance'])
    render(<WorkflowsScreen view={`wf=${WF}`} />)

    await screen.findByRole('button', { name: 'Switch to Engineering' }, WAIT)
    const tenants = workflowReads(sent).map((s) => s.tenant)
    // The page's own read under u-owner, then one read each of eng and ops.
    expect([...tenants].sort()).toEqual(['eng', 'ops', 'u-owner'])
    // A platform admin's reach is never asked.
    expect(sent.some((s) => s.tenant === 'finance')).toBe(false)
  })

  it('says a failed read plainly, and offers each workspace it could be in', async () => {
    serve([], ['ops'])
    render(<WorkflowsScreen view={`wf=${WF}`} />)

    await waitFor(() => expect(document.body.textContent).toContain('Could not tell which of your workspaces holds this workflow.'), WAIT)
    expect(document.body.textContent).toContain('Reading it in Operations failed')
    expect(document.body.textContent).toContain('Firestore did not answer.')
    // Not said as an absence.
    expect(document.querySelector('.wfp-missing')).toBeNull()
    const button = screen.getByRole('button', { name: 'Operations' })
    expect(chosenTenant()).toBe('u-owner')
    fireEvent.click(button)
    expect(chosenTenant()).toBe('ops')
  })

  it('names each candidate when two workspaces answer for one id', async () => {
    serve(['eng', 'ops'])
    render(<WorkflowsScreen view={`wf=${WF}`} />)

    await waitFor(() => expect(document.body.textContent).toContain('Could not tell which of your workspaces holds this workflow.'), WAIT)
    expect(screen.getByRole('button', { name: 'Engineering' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Operations' })).toBeTruthy()
    expect(chosenTenant()).toBe('u-owner')
  })
})
