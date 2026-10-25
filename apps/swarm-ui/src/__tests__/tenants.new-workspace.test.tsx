// ADMIN › TENANTS › NEW WORKSPACE: THE DIALOG (#847, lane WK-UI-DIALOG;
// docs/design/workspace-kinds.md §4.2.4, §6).
//
// WHAT EACH CASE HOLDS:
//   * each of the three tabs posts ONCE to `POST /v1/admin/workspaces`, with
//     `{kind, principal, display_name}` and nothing else -- no limits, no
//     image, no command (invariant 10) -- however often Create is pressed
//     while the request is in flight;
//   * a refusal is the API's code and sentence, word for word;
//   * every tab says, before submit, what the kind will get, and the service
//     tab says the platform owner approves it (Q2 (a));
//   * after submit the job's steps are drawn as a timeline, in the kind's
//     order (S1 and S2 only for a service), and `GET /v1/admin/workspaces/{id}`
//     is read every 5 s; the reads stop at `ready`, and when the dialog closes;
//   * `needs_owner` and `failed` show the stopped step and the API's copy;
//   * the list read Admin › Tenants' "Being created" section draws from
//     (`loadAdminWorkspaces`) sends its filters as a query and no email.
//
// The records are what `people._admin_view` serves
// (tests/unit/control_plane/test_workspace_kinds_record.py). Fixtures only;
// no network. Nothing here is token-shaped.

import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const WS_ID = 'w-4c1e0b'
const RECORD_URL = `/v1/admin/workspaces/${WS_ID}`

type Json = Record<string, unknown>

/** A record as `people._admin_view` serves it. */
function record(over: Json = {}): Json {
  return {
    state: 'approved',
    kind: 'group',
    setup_url: '/setup#workspace',
    setup_command: '/sc:setup',
    provisioning: { available: true, waiting_because: null, approved_minutes_ago: 0 },
    workspace_id: WS_ID,
    display_name: 'Research',
    principal: 'research@example.com',
    request_id: 'req-7',
    requested_at: new Date(Date.now() - 60_000).toISOString(),
    requested_via: 'console',
    decision: { verdict: 'approved', reason: null, at: new Date().toISOString(), auto: true },
    held: null,
    limits: { max_active: 8, capacity_units: 8 },
    steps: {},
    failure: null,
    ready_at: null,
    request_again_at: null,
    dispatch: null,
    run: null,
    retried_at: null,
    ...over,
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../NewWorkspace')
}

type Calls = { method: string; url: string; body: unknown }[]

/** The create route answers `created`; the record route answers `live()` (a function, so a case can move it). */
function answer(created: () => { status: number; body: unknown }, live: () => Json = () => record()): Calls {
  return serve((m, url) => {
    if (m === 'POST' && url === '/v1/admin/workspaces') return created()
    if (m === 'GET' && url === RECORD_URL) return { status: 200, body: { workspace: live() } }
    return null
  })
}

const posts = (calls: Calls) => calls.filter((c) => c.method === 'POST' && c.url === '/v1/admin/workspaces')
const recordReads = (calls: Calls) => calls.filter((c) => c.method === 'GET' && c.url === RECORD_URL).length
const dialog = () => screen.getByRole('dialog', { name: 'New workspace' })

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

/** The dialog behind an opener, as Admin › Tenants holds it: closing unmounts it. */
async function mount() {
  const { NewWorkspaceDialog } = await load()
  function Host() {
    const [open, setOpen] = useState(true)
    return open ? <NewWorkspaceDialog onClose={() => setOpen(false)} /> : <p>closed</p>
  }
  return render(<Host />)
}

function fill(tab: string, principal: string, displayName: string) {
  fireEvent.click(within(dialog()).getByRole('tab', { name: tab }))
  // The kind's own address: the group tab also asks for a first admin's (WORKSPACE-ADMIN-FIELDS).
  fireEvent.change(within(dialog()).getByLabelText(/^(Person’s email address|Group address|Service account address)$/), { target: { value: principal } })
  fireEvent.change(within(dialog()).getByLabelText(/display name/i), { target: { value: displayName } })
}

describe('each tab posts once, with {kind, principal, display_name} only', () => {
  const cases: ReadonlyArray<{ tab: string; kind: string; principal: string; name: string }> = [
    { tab: 'Personal', kind: 'personal', principal: 'new.person@example.com', name: 'New Person' },
    { tab: 'Group', kind: 'group', principal: 'research@example.com', name: 'Research' },
    { tab: 'Service', kind: 'service', principal: 'reports-bot@example-project.iam.gserviceaccount.com', name: 'Reports bot' },
  ]
  for (const c of cases) {
    it(`${c.tab}`, async () => {
      let release: () => void = () => {}
      const gate = new Promise<void>((r) => {
        release = r
      })
      const calls = serve((m, url) => {
        if (m === 'POST' && url === '/v1/admin/workspaces') return { status: 202, body: record({ kind: c.kind }) }
        if (m === 'GET' && url === RECORD_URL) return { status: 200, body: { workspace: record({ kind: c.kind }) } }
        return null
      })
      // Hold the POST in flight so a second press lands while it is.
      const served = globalThis.fetch
      globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        if ((init?.method ?? 'GET') === 'POST') await gate
        return served(input, init)
      }) as typeof fetch
      await mount()
      fill(c.tab, `  ${c.principal} `, c.name)
      const create = within(dialog()).getByRole('button', { name: 'Create workspace' })
      fireEvent.click(create)
      fireEvent.click(create)
      fireEvent.click(create)
      release()
      await waitFor(() => expect(within(dialog()).getByRole('list', { name: 'Workspace job steps' })).toBeTruthy(), WAIT)
      expect(posts(calls)).toHaveLength(1)
      expect(posts(calls)[0]!.body).toEqual({ kind: c.kind, principal: c.principal, display_name: c.name })
    })
  }

  it('sends a blank display name as null, so the API picks its default', async () => {
    const calls = answer(() => ({ status: 202, body: record() }))
    await mount()
    fill('Group', 'research@example.com', '   ')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    await waitFor(() => expect(posts(calls)).toHaveLength(1), WAIT)
    expect(posts(calls)[0]!.body).toEqual({ kind: 'group', principal: 'research@example.com', display_name: null })
  })

  it('posts nothing with no address', async () => {
    const calls = answer(() => ({ status: 202, body: record() }))
    await mount()
    const create = within(dialog()).getByRole('button', { name: 'Create workspace' }) as HTMLButtonElement
    expect(create.disabled).toBe(true)
    fireEvent.click(create)
    expect(posts(calls)).toHaveLength(0)
  })
})

describe('a refusal is the API’s, word for word', () => {
  it('draws the 403’s code and message, and stays on the form', async () => {
    const message = 'A platform service account cannot be given a workspace: reports-bot is one of swarm-api’s own.'
    answer(() => ({ status: 403, body: { code: 'WORKSPACE_SERVICE_PLATFORM', message } }))
    await mount()
    fill('Service', 'swarm-api@example-project.iam.gserviceaccount.com', '')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    const alert = await within(dialog()).findByRole('alert', {}, WAIT)
    expect(visible(alert)).toBe(`WORKSPACE_SERVICE_PLATFORM ${message}`)
    expect(within(dialog()).getByRole('button', { name: 'Create workspace' })).toBeTruthy()
    expect(within(dialog()).queryByRole('list', { name: 'Workspace job steps' })).toBeNull()
  })
})

describe('each tab says what the kind will get before submit', () => {
  it('the service tab says the platform owner approves it', async () => {
    answer(() => ({ status: 202, body: record() }))
    await mount()
    fireEvent.click(within(dialog()).getByRole('tab', { name: 'Service' }))
    const what = visible(within(dialog()).getByTestId('new-workspace-gets'))
    expect(what).toContain('admitted at the API’s front door')
    expect(what).toContain('The platform owner approves it.')
    expect(what).toContain('It runs nothing until a Claude account is lent to it or added.')
  })

  it('the personal and group tabs say an admin’s request is approved at once, and never that the owner approves', async () => {
    answer(() => ({ status: 202, body: record() }))
    await mount()
    for (const tab of ['Personal', 'Group']) {
      fireEvent.click(within(dialog()).getByRole('tab', { name: tab }))
      const what = visible(within(dialog()).getByTestId('new-workspace-gets'))
      expect(what).toContain('gets its own tenant, worker account, storage prefix and namespace')
      expect(what).toContain('approved at once')
      expect(what).not.toContain('platform owner approves')
    }
  })
})

describe('after submit: the steps as a timeline, read every 5 s', () => {
  it('draws a service record’s steps in the job’s order, S1 first after the claim and S2 before the final check', async () => {
    const at = new Date().toISOString()
    const steps = { A1: { state: 'done', at }, S1: { state: 'done', at }, A2: { state: 'running', at } }
    answer(() => ({ status: 202, body: record({ kind: 'service', state: 'applying', steps }) }), () =>
      record({ kind: 'service', state: 'applying', steps }),
    )
    await mount()
    fill('Service', 'reports-bot@example-project.iam.gserviceaccount.com', 'Reports bot')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    const list = await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    const ids = Array.from(list.querySelectorAll('li')).map((li) => li.getAttribute('data-step'))
    expect(ids).toEqual(['A1', 'S1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'S2', 'A9'])
    const state = (id: string) => list.querySelector(`li[data-step="${id}"]`)!.getAttribute('data-state')
    expect([state('A1'), state('S1'), state('A2'), state('A3')]).toEqual(['done', 'done', 'running', 'todo'])
    expect(visible(dialog())).toContain(WS_ID)
  })

  it('draws no S step for a group record, and appends a step id the console does not know', async () => {
    const steps = { A1: { state: 'done' }, X1: { state: 'running' } }
    answer(() => ({ status: 202, body: record({ steps }) }), () => record({ steps }))
    await mount()
    fill('Group', 'research@example.com', 'Research')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    const list = await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    const ids = Array.from(list.querySelectorAll('li')).map((li) => li.getAttribute('data-step'))
    expect(ids).toEqual(['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9', 'X1'])
  })

  it('polls every 5 s while not ready and stops at ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let live = record({ state: 'applying', steps: { A1: { state: 'done' } } })
    const calls = answer(() => ({ status: 202, body: record() }), () => live)
    await mount()
    fill('Group', 'research@example.com', 'Research')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    const before = recordReads(calls)
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(recordReads(calls)).toBe(before + 1), WAIT)
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(recordReads(calls)).toBe(before + 2), WAIT)

    live = record({ state: 'ready', ready_at: new Date().toISOString() })
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(visible(dialog())).toContain(`Workspace ready (${WS_ID})`), WAIT)
    const atReady = recordReads(calls)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(recordReads(calls), 'it kept polling a ready record').toBe(atReady)
  })

  it('stops polling when the dialog closes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const calls = answer(() => ({ status: 202, body: record() }), () => record({ state: 'applying' }))
    await mount()
    fill('Group', 'research@example.com', 'Research')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(recordReads(calls)).toBeGreaterThan(0), WAIT)
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Close' }))
    await screen.findByText('closed', {}, WAIT)
    const atClose = recordReads(calls)
    await vi.advanceTimersByTimeAsync(20_000)
    expect(recordReads(calls), 'it kept polling after the dialog closed').toBe(atClose)
  })

  it('a service record waiting for approval says the platform owner approves it', async () => {
    const waiting = record({ kind: 'service', state: 'requested', decision: null })
    answer(() => ({ status: 202, body: waiting }), () => waiting)
    await mount()
    fill('Service', 'reports-bot@example-project.iam.gserviceaccount.com', 'Reports bot')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    expect(visible(dialog())).toContain('Waiting for the platform owner to approve it')
  })

  it('needs_owner shows the stopped step, and failed the API’s copy word for word', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const copy = 'Setting up stopped at the namespace. Retry, or ask the platform owner (request req-7).'
    let live = record({ state: 'needs_owner', needs_owner_step: 'A7', steps: { A7: { state: 'held', code: 'CALL_REFUSED' } } })
    answer(() => ({ status: 202, body: live }), () => live)
    await mount()
    fill('Group', 'research@example.com', 'Research')
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Create workspace' }))
    await within(dialog()).findByRole('list', { name: 'Workspace job steps' }, WAIT)
    expect(visible(dialog())).toContain('Stopped at A7 for the platform owner’s review')
    const held = dialog().querySelector('li[data-step="A7"]')!
    expect(held.getAttribute('data-state')).toBe('held')
    expect(visible(held)).toContain('CALL_REFUSED')

    live = record({ state: 'failed', failure: { code: 'NAMESPACE_APPLY_FAILED', step: 'A7', retryable: true, at: null, copy } })
    await vi.advanceTimersByTimeAsync(5_000)
    await waitFor(() => expect(visible(dialog())).toContain(copy), WAIT)
  })
})

describe('the list read Admin › Tenants draws "Being created" from', () => {
  it('sends kind and state as a query, and reads the rows', async () => {
    const calls = serve((m, url) =>
      m === 'GET' && url.startsWith('/v1/admin/workspaces?')
        ? { status: 200, body: { workspaces: [record({ kind: 'service' })], count: 1 } }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { loadAdminWorkspaces } = await import('../api')
    const r = await loadAdminWorkspaces({ kind: 'service', state: 'applying' })
    expect(calls.map((c) => c.url)).toEqual(['/v1/admin/workspaces?kind=service&state=applying'])
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.map((w) => [w.workspace_id, w.kind])).toEqual([[WS_ID, 'service']])
  })

  it('a record with no kind reads as personal', async () => {
    serve((m, url) => (m === 'GET' && url === RECORD_URL ? { status: 200, body: { workspace: record({ kind: undefined }) } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { loadAdminWorkspace } = await import('../api')
    const r = await loadAdminWorkspace(WS_ID)
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.kind).toBe('personal')
  })
})
