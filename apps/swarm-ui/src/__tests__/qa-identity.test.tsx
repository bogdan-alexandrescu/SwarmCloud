// LANE QI-UI (docs/design/qa-identity.md §4.6, §6 row QI-UI; part of #1649):
// THE CONSOLE'S SIDE OF THE QA IDENTITY.
//
// Owner decisions 2026-10-19 (§7): option B, a QA service account whose IAP
// credential the live gate adds; granted per workspace by a platform admin
// with a reason (Q2), `eng` first. The console's three pieces:
//
//   1. Admin › Tenants draws a "QA identity" panel for an admin, and nothing
//      for anyone else -- not even the read (`GET /v1/admin/qa-identities` is
//      `require_admin`);
//   2. Grant without a reason is disabled (the API refuses a blank one: 1-200
//      characters), and a refusal -- a 403 `qa_identity_self` for the QA
//      workspace's own tenant -- is drawn as the API's code and sentence, word
//      for word;
//   3. the Submit form offers "Sign in to the console as the QA identity" only
//      for a profile that DECLARES `qa_identity` and only when
//      `GET /v1/qa-identities/mine` says this workspace is granted; ticked, it
//      sends `input.qa_identity: "console"`, unticked it sends nothing;
//   4. the Browser tab's badge row says "Signed in as QA identity (console)"
//      on a task whose input named it, and only there.
//
// The answers are the shapes swarm-api serves on main
// (swarm_api/qa_identity.py `mine`, `admin_list`, `grant_to_api`).
//
// MUTATIONS, each of which turns a case red: draw the panel without the
// admin check (1); drop `reason.trim() === ''` from Grant's `disabled` (2);
// print a fixed sentence instead of the error's message (2); offer the option
// on `configured` alone, or for a profile that does not declare it (3); send
// `qa_identity` while unticked (3); draw the badge from the profile instead of
// the task's input (4).
//
// Nothing here is credential-shaped: no token is needed, and none is built.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { LiveSession } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Profile, Task, Tenant } from '../types'
import { serve } from './repofixture'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTenants: vi.fn(),
  loadCapacity: vi.fn(),
  loadProfile: vi.fn(),
  loadAdminWorkspaces: vi.fn(),
  loadQaIdentities: vi.fn(),
  grantQaIdentity: vi.fn(),
  revokeQaIdentity: vi.fn(),
  loadQaIdentityMine: vi.fn(),
  loadProviderKeys: vi.fn(),
  loadJudgment: vi.fn(),
  mintLiveTicket: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  api.loadCapacity.mockImplementation(actual.loadCapacity)
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, ...api, USE_FIXTURES: false }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { TenantsScreen } = await import('../Activity')
const { QaIdentityGrants } = await import('../QaIdentityGrants')
const { SubmitScreen } = await import('../Submit')
const { LiveBrowserPane } = await import('../LiveBrowser')
const { BrowserBadges } = await import('../BrowserBadges')

const WAIT = { timeout: 4000 }
const HOST = 'console.example.com'
const ACCOUNT = ['qa-console', 'example-project.iam.gserviceaccount.com'].join('@')
const ADMIN = ['admin', 'example.com'].join('@')
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function refused<T>(httpStatus: number, code: string, message: string): Result<T> {
  return { status: 'error', error: { kind: 'forbidden', httpStatus, code, message } } as Result<T>
}

// ---------------------------------------------------------------------------
// 1-2. Admin › Tenants › QA identity
// ---------------------------------------------------------------------------

function tenant(id: string): Tenant {
  return {
    tenant_id: id,
    kind: 'group',
    principal: `${id}@example.com`,
    display_name: null,
    created_at: '2026-09-24T09:00:00Z',
    max_active: 4,
    capacity_units: 4,
    monthly_budget_usd: null,
    enabled: true,
    credentials: [],
    service_account: null,
    gcs_prefix: null,
    namespace: null,
  } as Tenant
}

const TENANTS = ['eng', 'research', 'u-qa-b260e6']

function profileOf(admin: boolean): Profile {
  return {
    identity: {
      email: 'someone@example.com',
      display_name: null,
      display_name_suggested: null,
      domain: 'example.com',
      platform_role: admin ? 'admin' : null,
      is_admin: admin,
      is_admin_unresolved: false,
    },
    workspaces: [],
    revision: 1,
    unavailable: [],
  } as unknown as Profile
}

/** `admin_list` as swarm-api serves it: one identity, its host, account, and the grants. */
function listing(grants: Array<Record<string, unknown>> = [], configured = true) {
  return ok({
    identities: [
      {
        identity: 'console',
        host: configured ? HOST : '',
        account: configured ? ACCOUNT : '',
        configured,
        grants,
      },
    ],
  })
}

const ENG_GRANTED = {
  tenant_id: 'eng',
  state: 'granted',
  granted_by: ADMIN,
  granted_at: '2026-10-20T09:00:00+00:00',
  reason: 'console visual QA (#1649)',
}

async function tenantsAs(admin: boolean): Promise<HTMLElement> {
  api.loadProfile.mockResolvedValue(ok(profileOf(admin)))
  api.loadAdminWorkspaces.mockResolvedValue(ok([]))
  api.loadTenants.mockResolvedValue(ok({ tenants: TENANTS.map(tenant) }))
  api.loadCapacity.mockResolvedValue(ok({ pools: [], runner_profiles: {} } as unknown as Capacity))
  const { container } = render(<TenantsScreen />)
  await screen.findByText('research', undefined, WAIT)
  await waitFor(() => expect(api.loadProfile).toHaveBeenCalled())
  await act(async () => {})
  return container
}

const panel = (root: ParentNode = document) => root.querySelector<HTMLElement>('[data-testid="qa-identity"]')

describe('1. the panel is an admin’s', () => {
  it('draws no panel, and reads no grants, for a non-admin', async () => {
    api.loadQaIdentities.mockResolvedValue(listing([ENG_GRANTED]))
    const c = await tenantsAs(false)
    expect(panel(c)).toBeNull()
    expect(screen.queryByRole('heading', { name: 'QA identity' })).toBeNull()
    expect(api.loadQaIdentities).not.toHaveBeenCalled()
  })

  it('draws it for an admin: the identity, its host, its account, configured, and each grant', async () => {
    api.loadQaIdentities.mockResolvedValue(listing([ENG_GRANTED]))
    const c = await tenantsAs(true)
    await waitFor(() => expect(panel(c)?.querySelector('[data-grant-tenant="eng"]')).not.toBeNull(), WAIT)
    const p = panel(c)!
    expect(within(p).getByRole('heading', { name: 'QA identity' })).toBeTruthy()
    expect(visible(p)).toContain(HOST)
    expect(visible(p)).toContain(ACCOUNT)
    expect(visible(p)).toMatch(/configured/i)
    const row = p.querySelector<HTMLElement>('[data-grant-tenant="eng"]')!
    expect(row.getAttribute('data-state')).toBe('granted')
    expect(visible(row)).toContain('console visual QA (#1649)')
    expect(visible(row)).toContain(ADMIN)
    expect(within(row).getByRole('button', { name: /^Revoke/ })).toBeTruthy()
  })

  it('says the deployment has no QA identity when it is not configured, and offers no Grant', async () => {
    api.loadQaIdentities.mockResolvedValue(listing([], false))
    render(<QaIdentityGrants tenants={TENANTS} />)
    const p = await waitFor(() => {
      const el = panel()
      expect(el).not.toBeNull()
      expect(visible(el)).toMatch(/not configured/i)
      return el!
    }, WAIT)
    expect(within(p).queryByRole('button', { name: 'Grant' })).toBeNull()
  })
})

describe('2. Grant needs a reason, and a refusal is the API’s own words', () => {
  async function form() {
    api.loadQaIdentities.mockResolvedValue(listing([]))
    render(<QaIdentityGrants tenants={TENANTS} />)
    const button = await screen.findByRole('button', { name: 'Grant' }, WAIT)
    const p = panel()!
    const workspace = within(p).getByLabelText(/workspace/i) as HTMLSelectElement
    const reason = within(p).getByLabelText(/reason/i) as HTMLTextAreaElement
    return { p, button: button as HTMLButtonElement, workspace, reason }
  }

  it('keeps Grant disabled until a reason is typed, and a blank one does not count', async () => {
    const { button, workspace, reason } = await form()
    fireEvent.change(workspace, { target: { value: 'eng' } })
    expect(button.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: '   ' } })
    expect(button.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: 'console visual QA' } })
    expect(button.disabled).toBe(false)
  })

  it('grants with the workspace and the trimmed reason, and reads the list again', async () => {
    const { button, workspace, reason } = await form()
    api.grantQaIdentity.mockResolvedValue(ok({ ...ENG_GRANTED, identity: 'console', revoked_by: null, revoked_at: null }))
    fireEvent.change(workspace, { target: { value: 'eng' } })
    fireEvent.change(reason, { target: { value: '  console visual QA  ' } })
    api.loadQaIdentities.mockResolvedValue(listing([ENG_GRANTED]))
    fireEvent.click(button)
    await waitFor(() => expect(api.grantQaIdentity).toHaveBeenCalledWith('console', 'eng', 'console visual QA'))
    await waitFor(() => expect(panel()!.querySelector('[data-grant-tenant="eng"]')).not.toBeNull(), WAIT)
    expect(api.loadQaIdentities).toHaveBeenCalledTimes(2)
  })

  it('draws a 403 as the API’s code and message, word for word', async () => {
    const message = 'the QA workspace cannot be granted its own identity'
    const { p, button, workspace, reason } = await form()
    api.grantQaIdentity.mockResolvedValue(refused(403, 'qa_identity_self', message))
    fireEvent.change(workspace, { target: { value: 'u-qa-b260e6' } })
    fireEvent.change(reason, { target: { value: 'try it' } })
    fireEvent.click(button)
    const alert = await within(p).findByRole('alert', undefined, WAIT)
    expect(alert.getAttribute('data-code')).toBe('qa_identity_self')
    expect(visible(alert)).toBe(`qa_identity_self ${message}`)
  })

  it('revokes a granted workspace and draws the API’s refusal the same way', async () => {
    api.loadQaIdentities.mockResolvedValue(listing([ENG_GRANTED]))
    render(<QaIdentityGrants tenants={TENANTS} />)
    const row = await waitFor(() => {
      const r = panel()?.querySelector<HTMLElement>('[data-grant-tenant="eng"]')
      expect(r).toBeTruthy()
      return r!
    }, WAIT)
    const message = 'this workspace holds no QA identity grant'
    api.revokeQaIdentity.mockResolvedValue(refused(404, 'not_found', message))
    fireEvent.click(within(row).getByRole('button', { name: /^Revoke/ }))
    await waitFor(() => expect(api.revokeQaIdentity).toHaveBeenCalledWith('console', 'eng'))
    const alert = await within(panel()!).findByRole('alert', undefined, WAIT)
    expect(visible(alert)).toBe(`not_found ${message}`)
  })

  it('offers no Revoke on a revoked grant', async () => {
    api.loadQaIdentities.mockResolvedValue(
      listing([{ ...ENG_GRANTED, state: 'revoked' }]),
    )
    render(<QaIdentityGrants tenants={TENANTS} />)
    const row = await waitFor(() => {
      const r = panel()?.querySelector<HTMLElement>('[data-grant-tenant="eng"]')
      expect(r).toBeTruthy()
      return r!
    }, WAIT)
    expect(row.getAttribute('data-state')).toBe('revoked')
    expect(within(row).queryByRole('button', { name: /^Revoke/ })).toBeNull()
  })
})

describe('2b. the routes the panel and the form call', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  async function live() {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    return vi.importActual<typeof import('../api')>('../api')
  }

  it('reads, grants and revokes on the admin routes, and reads the member route, as swarm-api mounts them', async () => {
    const calls = serve((method, url) => {
      if (url.startsWith('/v1/admin/qa-identities') || url.startsWith('/v1/qa-identities/mine'))
        return { status: 200, body: method === 'GET' && url.endsWith('/mine')
          ? { identities: [{ identity: 'console', configured: true, granted: true }] }
          : method === 'GET' ? { identities: [] } : { tenant_id: 'eng', state: 'granted' } }
      return null
    })
    const m = await live()
    const mine = await m.loadQaIdentityMine()
    expect(mine.status).toBe('ok')
    if (mine.status === 'ok') expect(mine.data).toEqual({ identities: [{ identity: 'console', configured: true, granted: true }] })
    await m.loadQaIdentities()
    await m.grantQaIdentity('console', 'eng', 'console visual QA')
    await m.revokeQaIdentity('console', 'eng')
    const seen = calls.map((c) => [c.method, c.url.split('?')[0], c.body])
    expect(seen).toEqual([
      ['GET', '/v1/qa-identities/mine', null],
      ['GET', '/v1/admin/qa-identities', null],
      ['PUT', '/v1/admin/qa-identities/console/grants/eng', { reason: 'console visual QA' }],
      ['DELETE', '/v1/admin/qa-identities/console/grants/eng', null],
    ])
  })
})

// ---------------------------------------------------------------------------
// 3. Submit offers the option only when granted
// ---------------------------------------------------------------------------

const OPTION = 'Sign in to the console as the QA identity'
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch

/** The fixture's capacity, with `claude-code-browser` declaring `qa_identity` (contract request 106) as asked. */
async function capacity(declare: boolean): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const p = data.runner_profiles['claude-code-browser']! as unknown as Record<string, unknown>
  if (declare) {
    p.input_contract = {
      required_keys: ['prompt'],
      inputs: { qa_identity: { kind: 'string', choices: ['console'], means: 'sign the live browser in to the console' } },
    }
  }
  return ok(data)
}

function mineAnswer(granted: boolean, configured = true) {
  return ok({ identities: [{ identity: 'console', configured, granted }] })
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/tasks')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({ task: { id: 't_qa_1', state: 'READY' }, scheduler_woken: true }), { status: 201, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return posts
}

async function browserForm() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code-browser"]')!)
  const prompt = r.container.querySelector<HTMLTextAreaElement>('.sbf-build textarea')!
  fireEvent.change(prompt, { target: { value: 'sweep the Overview at 390 and 1440' } })
  return r.container
}

describe('3. the Submit form offers the QA identity only when this workspace is granted', () => {
  beforeEach(() => {
    api.loadProviderKeys.mockResolvedValue(ok({ provider: 'typesafe', user: {}, tenant: {}, resolution: { tenant_id: 'eng', uses: 'none' }, consumed_by: [] }))
    api.loadJudgment.mockReturnValue(new Promise(() => {}))
  })

  afterEach(() => {
    globalThis.fetch = realFetch
  })

  it('offers it when the profile declares it and `/v1/qa-identities/mine` says granted', async () => {
    api.loadCapacity.mockResolvedValue(await capacity(true))
    api.loadQaIdentityMine.mockResolvedValue(mineAnswer(true))
    await browserForm()
    const box = await screen.findByRole('checkbox', { name: OPTION }, WAIT)
    expect((box as HTMLInputElement).checked).toBe(false)
  })

  it.each([
    ['not granted', mineAnswer(false)],
    ['not configured', mineAnswer(true, false)],
    ['the read failed', refused(500, 'internal', 'boom')],
  ])('does not offer it when %s', async (_why, answer) => {
    api.loadCapacity.mockResolvedValue(await capacity(true))
    api.loadQaIdentityMine.mockResolvedValue(answer)
    await browserForm()
    await waitFor(() => expect(api.loadQaIdentityMine).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByRole('checkbox', { name: OPTION })).toBeNull()
  })

  it('does not offer it, nor ask, for a profile that does not declare it', async () => {
    api.loadCapacity.mockResolvedValue(await capacity(false))
    api.loadQaIdentityMine.mockResolvedValue(mineAnswer(true))
    await browserForm()
    await act(async () => {})
    expect(screen.queryByRole('checkbox', { name: OPTION })).toBeNull()
    expect(api.loadQaIdentityMine).not.toHaveBeenCalled()
  })

  it('sends `qa_identity: "console"` ticked, and nothing unticked', async () => {
    api.loadCapacity.mockResolvedValue(await capacity(true))
    api.loadQaIdentityMine.mockResolvedValue(mineAnswer(true))
    const posts = capturePosts()
    await browserForm()
    const submit = screen.getByRole('button', { name: 'Start one agent' })

    await screen.findByRole('checkbox', { name: OPTION }, WAIT)
    fireEvent.click(submit)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.input).not.toHaveProperty('qa_identity')

    fireEvent.click(screen.getByRole('checkbox', { name: OPTION }))
    fireEvent.click(screen.getByRole('button', { name: 'Start one agent' }))
    await waitFor(() => expect(posts).toHaveLength(2), WAIT)
    expect((posts[1]!.input as Record<string, unknown>).qa_identity).toBe('console')
  })
})

// ---------------------------------------------------------------------------
// 4. The task page's badge
// ---------------------------------------------------------------------------

const ID = 'task_0b5c1d2e3f40516273a5'

function agent(input: unknown): Task {
  return { ...runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code-browser', resource_class: 'browser' }), input }
}

function session(): LiveSession {
  return {
    session_id: 'live_att_1a2b3c4d5e6f70819204',
    state: 'open',
    closed_reason: null,
    origin: { origin: `https://${HOST}`, host: HOST, registrable_domain: 'example.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: null,
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ADMIN, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
  } as LiveSession
}

function pane(input: unknown) {
  return (
    <LiveBrowserPane
      task={agent(input)}
      live={{ status: 'ok', liveBrowser: true, session: session() }}
      reload={() => {}}
      theater={false}
    />
  )
}

const qaBadge = (root: ParentNode = document) => root.querySelector<HTMLElement>('[data-testid="browser-badge-qa-identity"]')

describe('4. the task page says whose session the browser holds', () => {
  beforeEach(() => {
    api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
    api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
    api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
    api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
    api.loadLiveActions.mockReturnValue(new Promise(() => {}))
  })

  it('draws "Signed in as QA identity (console)" in the strip for a task that named it', () => {
    render(pane({ prompt: 'sweep the console', qa_identity: 'console' }))
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    const b = qaBadge(strip)
    expect(b).not.toBeNull()
    expect(visible(b!.querySelector('.bb-label'))).toBe('Signed in as QA identity (console)')
  })

  it.each([
    ['names no identity', { prompt: 'sweep the console' }],
    ['has no input', null],
    ['names something else', { prompt: 'x', qa_identity: 42 }],
  ])('draws no QA badge for a task that %s', (_why, input) => {
    render(pane(input))
    expect(document.querySelector('.live-strip')).not.toBeNull()
    expect(qaBadge()).toBeNull()
  })

  it('explains itself on focus: the QA account, not the submitter, and the agent never holds the credential', () => {
    render(<BrowserBadges status={null} qaIdentity="console" />)
    const b = qaBadge()!
    fireEvent.focus(b)
    const card = within(b).getByRole('tooltip')
    expect(card.hidden).toBe(false)
    expect(visible(card)).toMatch(/QA identity/)
    expect(visible(card)).toMatch(/not as the person who submitted/i)
    expect(visible(card)).toMatch(/agent never holds/i)
  })
})
