// THE WORKSPACE AND CLAUDE-ACCOUNT STEPS OF THE SETUP CHECKLIST (#847, lane
// W8; docs/workspaces.md §6.1).
//
// WHAT EACH CASE HOLDS:
//   * every record state draws its own copy -- none, requested, applying with
//     the job's steps, needs_owner, ready, denied with the admin's reason, and
//     failed with §4.3's copy word for word, once;
//   * [Request my workspace] posts once, however often it is pressed while the
//     request is in flight, and says `via: console`;
//   * while the record is not ready, `GET /v1/workspace` is read every 5 s;
//     it stops at ready, and on unmount;
//   * the claude_account step's [Request a loan] posts and then reads "Loan
//     requested"; [Add key] goes to Capacity › Accounts;
//   * a count of zero accounts is drawn as a measured zero (the `real zero`
//     mark), never as a blank, and nothing is drawn the API did not send.
//
// The step evidence is what `onboarding._workspace_step` and `_claude_step`
// serve (tests/unit/control_plane/test_onboarding_workspace_steps.py).
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const SETUP_URL = '/setup#workspace'

type Json = Record<string, unknown>

function record(over: Json = {}): Json {
  return {
    state: 'none',
    setup_url: SETUP_URL,
    setup_command: '/sc:setup',
    ...over,
  }
}

function withId(over: Json = {}): Json {
  return record({
    workspace_id: 'w-3f9a2c',
    tenant_id: 'u-example',
    request_id: 'req-1',
    requested_at: new Date(Date.now() - 2 * 3600_000).toISOString(),
    requested_via: 'console',
    decision: null,
    limits: { max_active: 8, capacity_units: 8, quota_pods: 16, quota_cpu: 64 },
    steps: {},
    failure: null,
    ready_at: null,
    request_again_at: null,
    ...over,
  })
}

function step(name: string, state: string, evidence: Json, over: Json = {}): Json {
  return { step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [], ...over }
}

const CHECKLIST_STATE: Record<string, string> = {
  none: 'todo', requested: 'in_progress', approved: 'in_progress', applying: 'in_progress',
  needs_owner: 'in_progress', denied: 'failed', failed: 'failed', ready: 'done',
}

function doc(ws: Json, claude: Json = { own: 0, lent: 0, provider_key: false, loan_request: null }, wsOver: Json = {}) {
  const evidence = { setup_url: SETUP_URL, setup_command: '/sc:setup', ...ws }
  const steps = [
    step('signed_in', 'done', { email: 'dev@swarm.example.com', tenant_id: 'eng' }),
    { ...step('workspace', CHECKLIST_STATE[String(ws.state)] ?? 'todo', evidence, wsOver), required: true },
    { ...step('claude_account', claude.own || claude.lent ? 'done' : claude.loan_request === 'requested' ? 'in_progress' : 'todo', { setup_url: '/setup#claude-account', setup_command: '/sc:setup', ...claude }), required: true },
    step('github_connected', 'todo', { via: null }),
    step('ready', 'todo', { waiting_for: 'workspace' }),
  ]
  return {
    tenant_id: 'eng', user: 'dev@swarm.example.com', user_hash: '89abcdef01234567', derived_at: new Date().toISOString(),
    next_step: 'workspace', complete: false, source: 'derived on this read', steps,
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Onboarding')
}

/** The checklist, and `GET /v1/workspace` answering `live` (a function, so a case can move it). */
function answer(checklist: Json, live: () => Json, posts: Record<string, (body: unknown) => { status: number; body: unknown }> = {}) {
  return serve((m, url, body) => {
    if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: checklist }
    if (m === 'GET' && url === '/v1/workspace') return { status: 200, body: live() }
    if (m === 'POST' && posts[url] !== undefined) return posts[url]!(body)
    return null
  })
}

const stepEl = (name: string) => document.querySelector(`li.ob-step[data-step="${name}"]`) as HTMLElement
const workspaceReads = (calls: { method: string; url: string }[]) => calls.filter((c) => c.method === 'GET' && c.url === '/v1/workspace').length

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

async function mount(checklist: Json, live: () => Json, posts = {}) {
  const calls = answer(checklist, live, posts)
  const { OnboardingScreen } = await load()
  const utils = render(<OnboardingScreen />)
  await screen.findByRole('list', { name: 'Setup steps' }, WAIT)
  // The record's own read lands too.
  await waitFor(() => expect(workspaceReads(calls)).toBeGreaterThan(0), WAIT)
  return { ...utils, calls }
}

describe('every record state draws its own copy', () => {
  it('none: says what a workspace is and offers the request', async () => {
    await mount(doc(record()), () => record())
    const el = stepEl('workspace')
    expect(within(el).getByText('Request your workspace')).toBeTruthy()
    expect(visible(el)).toContain('Your own isolated space to run agents in')
    expect(visible(el)).toContain('Team work is not affected.')
    expect(within(el).getByRole('button', { name: 'Request my workspace' })).toBeTruthy()
    // The anchor the API's setup_url names.
    expect(el.querySelector('#workspace')).not.toBeNull()
  })

  it('requested: waits for an admin, with the id and the age', async () => {
    const r = withId({ state: 'requested' })
    await mount(doc(r), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain('Workspace requested (w-3f9a2c) — waiting for an admin to approve'), WAIT)
    expect(visible(el)).toContain('2h ago')
    expect(within(el).queryByRole('button')).toBeNull()
  })

  it('applying: lists the job steps, each with its own state, the id and the elapsed time', async () => {
    const r = withId({
      state: 'applying',
      decision: { verdict: 'approved', reason: null, at: new Date(Date.now() - 250_000).toISOString() },
      steps: { A1: { state: 'done' }, A2: { state: 'done' }, A3: { state: 'done' }, A4: { state: 'done' }, A5: { state: 'running' } },
    })
    await mount(doc(r), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain('Setting up your workspace — w-3f9a2c'), WAIT)
    const rows = [...el.querySelectorAll<HTMLElement>('ol[aria-label="Workspace set-up steps"] li')]
    const label = (li: HTMLElement) => visible(li).replace(visible(li.querySelector('.ob-state')), '').trim()
    expect(rows.map((li) => [label(li), li.dataset.state, visible(li.querySelector('.ob-state'))])).toEqual([
      ['Approved', 'done', 'done'],
      ['Checking the name is free', 'done', 'done'],
      ['Identity', 'done', 'done'],
      ['Access', 'running', 'running'],
      ['Namespace', 'todo', 'not started'],
      ['Limits', 'todo', 'not started'],
      ['Final check', 'todo', 'not started'],
    ])
    expect(visible(el)).toContain('You can carry on with the steps below meanwhile.')
  })

  it('needs_owner: says the owner must review it', async () => {
    const r = withId({ state: 'needs_owner', steps: { A1: { state: 'done' }, A2: { state: 'held' } } })
    await mount(doc(r), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain("Approved. A change needs the platform owner's review before it can finish."), WAIT)
    expect(el.querySelector('li[data-state="held"]')).not.toBeNull()
  })

  it('ready: ticks the step with the workspace id', async () => {
    const r = withId({ state: 'ready', ready_at: new Date().toISOString() })
    await mount(doc(r), () => r)
    await waitFor(() => expect(visible(stepEl('workspace'))).toContain('Workspace ready (w-3f9a2c)'), WAIT)
    expect(stepEl('workspace').dataset.state).toBe('done')
  })

  it("denied: the admin's reason, and Request again once the API allows it", async () => {
    const r = withId({
      state: 'denied',
      decision: { verdict: 'denied', reason: 'Please use the eng team space for the migration work.', at: new Date(Date.now() - 2 * 86400_000).toISOString() },
      request_again_at: new Date(Date.now() - 86400_000).toISOString(),
    })
    await mount(doc(r), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain('Your workspace request was not approved'), WAIT)
    expect(visible(el)).toContain('“Please use the eng team space for the migration work.”')
    expect(within(el).getByRole('button', { name: 'Request again' })).toBeTruthy()
  })

  it('denied inside the wait: no button, and when it may be asked again', async () => {
    const r = withId({
      state: 'denied',
      decision: { verdict: 'denied', reason: 'Not yet.', at: new Date().toISOString() },
      request_again_at: new Date(Date.now() + 86400_000).toISOString(),
    })
    await mount(doc(r), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain('You can ask again from'), WAIT)
    expect(within(el).queryByRole('button', { name: 'Request again' })).toBeNull()
  })

  it("failed: §4.3's copy word for word, printed once", async () => {
    const copy = "Your workspace's Kubernetes namespace could not be applied in full, so it is not isolated yet and nothing will run in it. An admin can retry it; the reference is req-1."
    const failure = { code: 'NAMESPACE_APPLY_FAILED', step: 'A7', retryable: true, at: new Date().toISOString(), copy }
    const r = withId({ state: 'failed', failure })
    await mount(doc(r, undefined, { code: 'NAMESPACE_APPLY_FAILED', copy }), () => r)
    const el = stepEl('workspace')
    await waitFor(() => expect(visible(el)).toContain('Setting up your workspace stopped (w-3f9a2c).'), WAIT)
    expect(visible(el).split(copy).length - 1, 'the copy is not printed exactly once').toBe(1)
    expect(within(el).queryByRole('button')).toBeNull()
  })
})

describe('the request', () => {
  it('posts once, as the console, however often it is pressed', async () => {
    let state: Json = record()
    const calls = answer(doc(record()), () => state, {
      '/v1/workspace': () => ({ status: 202, body: (state = withId({ state: 'requested' })) }),
    })
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    const button = (await screen.findByRole('button', { name: 'Request my workspace' }, WAIT)) as HTMLButtonElement
    fireEvent.click(button)
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(visible(stepEl('workspace'))).toContain('waiting for an admin to approve'), WAIT)
    const posts = calls.filter((c) => c.method === 'POST' && c.url === '/v1/workspace')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.body).toEqual({ via: 'console' })
  })

  it("shows the API's refusal as it came", async () => {
    await mount(doc(record()), () => record(), {
      '/v1/workspace': () => ({ status: 403, body: { code: 'WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS', message: "A workspace is a person's own space." } }),
    })
    fireEvent.click(within(stepEl('workspace')).getByRole('button', { name: 'Request my workspace' }))
    await waitFor(() => expect(visible(stepEl('workspace'))).toContain("WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS A workspace is a person's own space."), WAIT)
  })
})

describe('the record is read every 5 s until it is ready', () => {
  it('polls while not ready, stops at ready, and reads the checklist again when the state moves', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let live: Json = withId({ state: 'applying' })
    const { calls } = await mount(doc(withId({ state: 'applying' })), () => live)
    const before = workspaceReads(calls)
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(workspaceReads(calls)).toBe(before + 1), WAIT)
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(workspaceReads(calls)).toBe(before + 2), WAIT)

    const checklistReads = () => calls.filter((c) => c.method === 'GET' && c.url === '/v1/onboarding').length
    const listedBefore = checklistReads()
    live = withId({ state: 'ready', ready_at: new Date().toISOString() })
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(visible(stepEl('workspace'))).toContain('Workspace ready (w-3f9a2c)'), WAIT)
    await waitFor(() => expect(checklistReads()).toBeGreaterThan(listedBefore), WAIT)
    const atReady = workspaceReads(calls)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(workspaceReads(calls), 'it kept polling a ready record').toBe(atReady)
  })

  it('stops when the record is refused, as for a service account', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: doc(record()) }
      if (m === 'GET' && url === '/v1/workspace') {
        return { status: 403, body: { code: 'WORKSPACE_NOT_FOR_SERVICE_ACCOUNTS', message: "A workspace is a person's own space." } }
      }
      return null
    })
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' }, WAIT)
    await waitFor(() => expect(workspaceReads(calls)).toBe(1), WAIT)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(workspaceReads(calls), 'it kept asking for a record it was refused').toBe(1)
    // The step still draws from the checklist's own evidence.
    expect(within(stepEl('workspace')).getByRole('button', { name: 'Request my workspace' })).toBeTruthy()
  })

  it('stops on unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const r = withId({ state: 'requested' })
    const { calls, unmount } = await mount(doc(r), () => r)
    unmount()
    const atUnmount = workspaceReads(calls)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(workspaceReads(calls)).toBe(atUnmount)
  })
})

describe('the claude_account step', () => {
  it('draws zero accounts as measured zeros and offers Add key and a loan', async () => {
    const r = withId({ state: 'ready' })
    await mount(doc(r), () => r)
    const el = stepEl('claude_account')
    expect(within(el).getByText('Add a Claude account')).toBeTruthy()
    const zeros = el.querySelectorAll('.ctl-mark.is-zero')
    expect(zeros).toHaveLength(2)
    expect([...zeros].map((z) => z.textContent)).toEqual(['real zero', 'real zero'])
    expect(visible(el)).toContain('Your workspace runs nothing until it has a Claude account to run on.')
    expect(within(el).getByRole('link', { name: 'Add key' }).getAttribute('href')).toBe('/capacity/accounts')
    expect(el.querySelector('#claude-account')).not.toBeNull()
  })

  it('posts the loan request, then reads Loan requested', async () => {
    const r = withId({ state: 'ready' })
    const { calls } = await mount(doc(r), () => r, {
      '/v1/workspace/loan-request': () => ({ status: 202, body: { state: 'requested', workspace_id: 'w-3f9a2c', request_id: 'loan-1', requested_at: new Date().toISOString() } }),
    })
    fireEvent.click(within(stepEl('claude_account')).getByRole('button', { name: 'Request a loan' }))
    await waitFor(() => expect(visible(stepEl('claude_account'))).toContain('Loan requested'), WAIT)
    const posts = calls.filter((c) => c.method === 'POST' && c.url === '/v1/workspace/loan-request')
    expect(posts).toHaveLength(1)
    expect(posts[0]!.body).toEqual({ via: 'console' })
    expect(within(stepEl('claude_account')).queryByRole('button', { name: 'Request a loan' })).toBeNull()
  })

  it('a done step draws the counts the API sent and no action', async () => {
    const r = withId({ state: 'ready' })
    await mount(doc(r, { own: 2, lent: 0, provider_key: false, loan_request: null }), () => r)
    const el = stepEl('claude_account')
    expect(el.dataset.state).toBe('done')
    expect(visible(el)).toContain('your own 2')
    expect(el.querySelectorAll('.ctl-mark.is-zero')).toHaveLength(1)
    expect(within(el).queryByRole('button')).toBeNull()
    expect(within(el).queryByRole('link', { name: 'Add key' })).toBeNull()
  })
})
