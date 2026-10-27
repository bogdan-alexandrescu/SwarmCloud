// WHAT EACH SCREEN DOES WITH A ROLE (docs/design/workspace-roles.md §4.8,
// §4.11; lane ROLES-UI-GATES, part of #1518).
//
// WHAT EACH CASE HOLDS:
//   * a `viewer` sees Approve and Reject disabled, with "Your role in eng is
//     viewer; members decide here", and a `member` sees both live;
//   * Submit is disabled for a `viewer`, with the role sentence, and posts
//     nothing; it is live for a `member`;
//   * Cancel (StopRun) is disabled for a `viewer`, with the role sentence,
//     and opens no dialog; it is live for a `member`;
//   * an agent's questions, a schedule's writes and a repository's register
//     and Index now are disabled for a `viewer`, each with its sentence;
//   * a `GET /v1/tenants/me` that carries no `role` (an older API) disables
//     nothing: an unknown role is the server's to decide;
//   * Settings › Profile shows each workspace's stored role and its source,
//     "admin (platform admin)" for a reach entry, and the derived role when
//     the `/v1/tenants/mine` entry carries no `role`.
//
// The bodies are routes/tenants.py's shapes. Placeholders only; nothing is
// token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { repo, serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

/** `GET /v1/tenants/me` for the selected workspace `eng`, with `role` when given. */
function me(role: { name: string; source: string } | null): Json {
  return {
    tenant: { tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null },
    principal: { email: 'vera@example.com', domain: 'example.com', groups: [], is_admin: false, is_admin_unresolved: false },
    environment: 'dev',
    environment_declared: true,
    ...(role === null ? {} : { role }),
  }
}

const VIEWER = { name: 'viewer', source: 'override' }
const MEMBER = { name: 'member', source: 'default' }
const VIEWER_SENTENCE = 'Your role in eng is viewer; members decide here.'

function live(handler: Handler, role: { name: string; source: string } | null) {
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.split('?')[0] === '/v1/tenants/me') return { status: 200, body: me(role) }
    return handler(m, url, body)
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return calls
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.doUnmock('../api')
  window.localStorage.clear()
  window.sessionStorage.clear()
})

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

function approval(id: string): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#12' },
    digest: 'plan-digest-1',
    summary: 'example-org/example-api#12: tighten the retry loop',
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-2),
    expires_at: hoursFromNow(70),
  }
}

async function openApproval(role: { name: string; source: string } | null) {
  const calls = live((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: [approval('run:run_8f2a')], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/approvals/run%3Arun_8f2a') {
      return { status: 200, body: { approval: { ...approval('run:run_8f2a'), projected: true }, run: { id: 'run_8f2a', state: 'PLANNED', plan: { summary: 'Tighten it.', files: ['app/retry.py'] } } } }
    }
    return null
  }, role)
  const { ApprovalsScreen } = await import('../Approvals')
  render(<ApprovalsScreen view="approval=run%3Arun_8f2a" go={vi.fn()} />)
  const detail = await waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)
  const approve = await within(detail).findByRole('button', { name: 'Approve' }, WAIT)
  // #2417 A5: Reject is one click, its reason box beside it; a reason is
  // typed so only the role gate can be what disables it.
  fireEvent.change(within(detail).getByRole('textbox', { name: /Reason/ }), { target: { value: 'not now' } })
  const reject = within(detail).getByRole('button', { name: 'Reject' })
  return { calls, detail, approve: approve as HTMLButtonElement, reject: reject as HTMLButtonElement }
}

describe('Approvals, by role', () => {
  it('disables Approve and Reject for a viewer, with the role sentence', async () => {
    const { detail, approve, reject, calls } = await openApproval(VIEWER)
    await waitFor(() => expect(approve.disabled).toBe(true), WAIT)
    expect(reject.disabled).toBe(true)
    expect(visible(detail.querySelector('[data-role-gate]'))).toBe(VIEWER_SENTENCE)
    fireEvent.click(approve)
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('leaves both live for a member, with no sentence', async () => {
    const { detail, approve, reject, calls } = await openApproval(MEMBER)
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/v1/tenants/me'))).toBe(true), WAIT)
    await new Promise((r) => setTimeout(r, 0))
    expect(approve.disabled).toBe(false)
    expect(reject.disabled).toBe(false)
    expect(detail.querySelector('[data-role-gate]')).toBeNull()
  })

  it('disables nothing when the API serves no role (an older API)', async () => {
    const { detail, approve, calls } = await openApproval(null)
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/v1/tenants/me'))).toBe(true), WAIT)
    await new Promise((r) => setTimeout(r, 0))
    expect(approve.disabled).toBe(false)
    expect(detail.querySelector('[data-role-gate]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Submit (the fixture build draws the form; only the role read is stubbed)
// ---------------------------------------------------------------------------

async function submitAs(role: { name: string; source: string } | null) {
  vi.resetModules()
  vi.doMock('../api', async (importOriginal) => {
    const actual = await importOriginal<typeof import('../api')>()
    return { ...actual, loadMe: () => Promise.resolve({ status: 'ok', data: me(role), fetchedAt: Date.now() }) }
  })
  const { SubmitScreen } = await import('../Submit')
  const { container } = render(<SubmitScreen />)
  const go = (await screen.findByRole('button', { name: 'Start one agent' }, WAIT)) as HTMLButtonElement
  fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
  return { container, go }
}

describe('Submit, by role', () => {
  it('disables Submit for a viewer, with the role sentence, and posts nothing', async () => {
    const { container, go } = await submitAs(VIEWER)
    await waitFor(() => expect(go.disabled).toBe(true), WAIT)
    expect(visible(container.querySelector('[data-role-gate]'))).toBe('Your role in eng is viewer; members submit here.')
    const posted = vi.fn()
    const prior = globalThis.fetch
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET') === 'POST') posted(String(input))
      return prior(input, init)
    }) as unknown as typeof fetch
    fireEvent.submit(go.closest('form')!)
    await new Promise((r) => setTimeout(r, 50))
    globalThis.fetch = prior
    expect(posted).not.toHaveBeenCalled()
  })

  it('leaves Submit live for a member', async () => {
    const { container, go } = await submitAs(MEMBER)
    await new Promise((r) => setTimeout(r, 50))
    expect(go.disabled).toBe(false)
    expect(container.querySelector('[data-role-gate]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Cancel (StopRun), the control on an agent, a workflow step and a graph node
// ---------------------------------------------------------------------------

const THEN = '2026-09-24T03:55:07.000Z'

/** A task StopRun offers to stop: admitted and dispatched, holding a lease. */
function stoppable(): Record<string, unknown> {
  return {
    id: 'task_2a417cb24edb4d2cb59e', tenant_id: 'eng', state: 'DISPATCHED', runner_profile: 'browser',
    resource_class: 'browser', provider: 'anthropic', priority: 0, created_at: THEN, updated_at: THEN,
    started_at: null, completed_at: null, submitted_by: 'ada@eng.test', attempt_count: 1, max_attempts: 3,
    park_reason: null, blocked_by: null, workflow_id: null, step_id: null, depends_on: null,
    cancel_requested: false, repository_url: null, model: null, timeout_seconds: 600, next_eligible_at: null,
    metadata: null, repository_ref: null, input: {}, last_error: null, result_summary: null,
    latest_checkpoint: null, current_generation: 1, current_lease_id: 'lease_c388c25e50c5421f840d',
  }
}

async function stopAs(role: { name: string; source: string } | null) {
  live(() => null, role)
  const { StopRun } = await import('../StopRun')
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { container } = render(<StopRun task={stoppable() as any} what="agent" reload={vi.fn()} />)
  const stop = (await screen.findByRole('button', { name: 'stop' }, WAIT)) as HTMLButtonElement
  return { container, stop }
}

describe('Cancel, by role', () => {
  it('disables stop for a viewer, with the role sentence, and opens no dialog', async () => {
    const { container, stop } = await stopAs(VIEWER)
    await waitFor(() => expect(stop.disabled).toBe(true), WAIT)
    expect(container.textContent).toContain('Your role in eng is viewer; members cancel here.')
    fireEvent.click(stop)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('leaves stop live for a member', async () => {
    const { container, stop } = await stopAs(MEMBER)
    await new Promise((r) => setTimeout(r, 50))
    expect(stop.disabled).toBe(false)
    expect(container.querySelector('[data-role-gate]')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Questions for you
// ---------------------------------------------------------------------------

describe('Questions for you, by role', () => {
  it('disables the options and Answer for a viewer the API would let answer, with the role sentence', async () => {
    vi.resetModules()
    const questions = {
      task_id: 'task_q1', count: 1, status: 'ok', attempt_id: 'att_1', redacted: false, redaction_count: 0,
      may_answer: true,
      questions: [{ question: 'Per task or per workflow?', context: 'One file today.', recommended: 'per task', options: [{ label: 'per task', description: 'one file per task' }, { label: 'per workflow', description: 'one for the run' }] }],
    }
    vi.doMock('../api', async (importOriginal) => {
      const actual = await importOriginal<typeof import('../api')>()
      return {
        ...actual,
        loadMe: () => Promise.resolve({ status: 'ok', data: me(VIEWER), fetchedAt: Date.now() }),
        loadQuestions: () => Promise.resolve({ status: 'ok', data: questions, fetchedAt: Date.now() }),
      }
    })
    const { QuestionsCard } = await import('../QuestionsCard')
    const { task } = await import('./runfixture')
    render(<QuestionsCard task={task({ id: 'task_q1', state: 'SUCCEEDED', submitted_by: 'vera@example.com', result_summary: { questions: 1 } })} />)
    const card = await screen.findByRole('region', { name: 'Questions for you' }, WAIT)
    await waitFor(() => expect(visible(card.querySelector('[data-role-gate]'))).toBe('Your role in eng is viewer; members answer here.'), WAIT)
    for (const radio of card.querySelectorAll<HTMLInputElement>('input[type="radio"]')) expect(radio.disabled).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Schedules and Repositories
// ---------------------------------------------------------------------------

function schedule(id: string): Json {
  return {
    schedule_id: id, tenant_id: 'eng', name: `Schedule ${id}`, type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: [] }, cron: '0 9 * * 1-5', timezone: 'Europe/London', params: {},
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'paused', pause: { by: 'a@example.com', reason: 'holiday' }, owner: 'operator@example.com', created_by: 'operator@example.com',
    created_at: hoursFromNow(-48), next_run_at: null, last_firing: null, consecutive_failures: 0, revision: 2,
    words: 'at 09:00, Monday through Friday', tier: 'R2', pending_approvals: 0,
    spend_today: { day: '2026-10-11', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
  }
}

describe('Schedules, by role', () => {
  it('offers a viewer no new schedule, no Run now and no Resume, each saying why', async () => {
    live((m, url) => {
      const path = url.split('?')[0]!
      if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } }
      if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [] } }
      return null
    }, VIEWER)
    const { SchedulesScreen } = await import('../Schedules')
    render(<SchedulesScreen view={null} go={vi.fn()} />)
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-testid="sc-acts-row"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    await waitFor(() => expect((within(row).getByRole('button', { name: 'Run now' }) as HTMLButtonElement).disabled).toBe(true), WAIT)
    expect((within(row).getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(true)
    expect(visible(row)).toContain('Your role in eng is viewer; members run and change schedules here.')
    expect(screen.queryByRole('link', { name: 'New schedule' })).toBeNull()
    expect((screen.getByRole('button', { name: 'New schedule' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('Repositories, by role', () => {
  it('offers a viewer no register and no Index now, and says why once', async () => {
    live((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/repositories' ? { status: 200, body: { repositories: [repo({ repo_id: 'repo_a' })] } } : null), VIEWER)
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    const index = (await screen.findByRole('button', { name: 'Index now' }, WAIT)) as HTMLButtonElement
    await waitFor(() => expect(index.disabled).toBe(true), WAIT)
    for (const b of screen.getAllByRole('button', { name: 'Register repository' })) expect((b as HTMLButtonElement).disabled).toBe(true)
    expect(document.querySelectorAll('[data-role-gate]')).toHaveLength(1)
    expect(visible(document.querySelector('[data-role-gate]'))).toBe('Your role in eng is viewer; members register and index repositories here.')
  })
})

// ---------------------------------------------------------------------------
// Settings › Profile
// ---------------------------------------------------------------------------

function profile(): Json {
  return {
    identity: { email: 'ada@example.com', display_name: null, display_name_suggested: null, domain: 'example.com', platform_role: 'admin', is_admin: true, is_admin_unresolved: false },
    workspaces: [
      { tenant_id: 'u-ada', display_name: 'Your workspace (u-ada)', kind: 'user', role: { name: 'owner', source: 'derived', via: null }, state: 'ready', selectable: true, is_acting: true, is_default: true },
      { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', role: { name: 'member', source: 'derived', via: 'eng@example.com' }, state: null, selectable: true, is_acting: false, is_default: false },
      { tenant_id: 'ops', display_name: 'ops@example.com', kind: 'group', role: { name: 'member', source: 'derived', via: 'ops@example.com' }, state: null, selectable: true, is_acting: false, is_default: false },
    ],
    default_workspace: { tenant_id: 'u-ada', source: 'personal', ignored: null },
    connections: { github: [], typesafe: { personal: { set: false }, acting_workspace: { tenant_id: 'u-ada', set: false }, uses: 'none' } },
    notifications: { preferences: { muted: [], reminder: 'as_set' }, topics: ['approval'] },
    claude_accounts: { personal_workspace: 'u-ada', accounts: [], unreadable_document_count: 0 },
    revision: 1,
    unavailable: [],
  }
}

const MINE = [
  { tenant_id: 'u-ada', display_name: 'Your workspace (u-ada)', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' }, is_default: true },
  { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'viewer', source: 'override' }, is_default: false },
  // No `role`: the server could not read it, so the derived role is drawn.
  { tenant_id: 'ops', display_name: 'ops@example.com', kind: 'group', via: 'member', is_default: false },
  { tenant_id: 'sales', display_name: 'sales@example.com', kind: 'group', via: 'platform_admin', role: { name: 'admin', source: 'platform_admin' }, is_default: false },
]

describe('Settings › Profile, the role seam', () => {
  async function drawn() {
    live((m, url) => {
      if (m === 'GET' && url === '/v1/profile') return { status: 200, body: profile() }
      if (m === 'GET' && url === '/v1/tenants/mine') return { status: 200, body: MINE }
      return null
    }, null)
    const { ProfileScreen } = await import('../Profile')
    render(<ProfileScreen />)
    await screen.findByText('ada@example.com', {}, WAIT)
    const list = screen.getByRole('list', { name: 'Your workspaces' })
    const row = (id: string) => list.querySelector<HTMLElement>(`[data-tenant="${id}"]`)
    await waitFor(() => expect(row('sales')).not.toBeNull(), WAIT)
    return row
  }

  it('shows the stored role and its source', async () => {
    const row = await drawn()
    expect(visible(row('eng')?.querySelector('.pf-role') ?? null)).toBe('viewer (set for you by name)')
    expect(visible(row('u-ada')?.querySelector('.pf-role') ?? null)).toBe('admin (your personal workspace)')
  })

  it('shows "admin (platform admin)" for a reach entry', async () => {
    const row = await drawn()
    expect(visible(row('sales')?.querySelector('.pf-role') ?? null)).toBe('admin (platform admin)')
  })

  it('shows the derived role when the entry carries no role, beside a stored one', async () => {
    const row = await drawn()
    // The control: the stored role is read for the entry that carries one, so
    // a page that ignored `/v1/tenants/mine` fails here rather than passing on
    // the derived role it always drew.
    expect(visible(row('eng')?.querySelector('.pf-role') ?? null)).toBe('viewer (set for you by name)')
    expect(visible(row('ops')?.querySelector('.pf-role') ?? null)).toBe('member')
    expect(visible(row('ops'))).toContain('via ops@example.com')
  })
})
