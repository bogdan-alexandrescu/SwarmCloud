// HOME, THE SIGNED-IN PERSON'S OWN PAGE (the owner's pick of the 'desk'
// mock-up; part of #2688). Each kind of waiting item and the console call its
// action makes, the member persona, the empty state and the answered effect
// (the item leaves and its count drops). The root route and the menu order
// are home.route.test.tsx's.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskState } from '../types'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadApprovals: vi.fn(),
  loadApproval: vi.fn(),
  approveApproval: vi.fn(),
  rejectApproval: vi.fn(),
  loadPeople: vi.fn(),
  loadProfile: vi.fn(),
  loadStats: vi.fn(),
  loadOnboarding: vi.fn(),
  loadLiveSession: vi.fn(),
  takeLiveControl: vi.fn(),
  answerQuestion: vi.fn(),
  approveWorkspace: vi.fn(),
  denyWorkspace: vi.fn(),
  setWorkspaceLoan: vi.fn(),
  declineLoanRequest: vi.fn(),
}))
const handoff = vi.hoisted(() => ({ humanReady: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})
vi.mock('../Handoff', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../Handoff')>()
  return { ...actual, ...handoff }
})

import { HomeScreen, ownerQuestionsOf, waitWhy } from '../Home'

const NOW = Date.parse('2026-11-02T12:00:00.000Z')
const iso = (ms: number): string => new Date(ms).toISOString()
const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: NOW })
const refused = (httpStatus: number): Result<never> => ({ status: 'error', error: { kind: 'forbidden', httpStatus, code: null, message: 'no' } as never })

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-ada',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: iso(NOW - 3_600_000),
    updated_at: iso(NOW - 60_000),
    started_at: iso(NOW - 3_000_000),
    completed_at: null,
    submitted_by: 'ada@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

/** A merge parked on one owner question its review named. */
const merge = task('task_merge1', 'PARKED', {
  runner_profile: 'merge',
  park_reason: 'HUMAN_REQUIRED',
  metadata: { title: 'Merge the login fix' },
  blocked_by: [
    {
      reason: 'HUMAN_REQUIRED',
      park_phase: 'owner_questions',
      owner_questions: { questions: [{ task_id: 'task_review1', step_id: 'review', question_index: 0, question: 'Keep the old flag?' }], unreadable: 0 },
    },
  ] as unknown as Task['blocked_by'],
})
/** A browser agent parked for a person: I'm ready. */
const parkedForMe = task('task_browser1', 'PARKED', { runner_profile: 'claude-code-browser', park_reason: 'HUMAN_REQUIRED', metadata: { title: 'Sign in to the vendor' } })
/** A running browser agent holding for a person at its browser: Take control. */
const heldForMe = task('task_held1', 'RUNNING', {
  runner_profile: 'claude-code-browser',
  metadata: { title: 'Pass the captcha' },
  human_wait: { reason: 'a captcha', expect: null, requested_at: iso(NOW - 60_000), deadline: iso(NOW + 600_000), controller: null },
} as Partial<Task>)
/** The held agent's live session: open, with what this caller may do. */
function liveRead(you: Record<string, boolean>, controller: { email: string; since: string } | null = null) {
  return {
    live_browser: true,
    session: {
      session_id: 'ls_1',
      state: 'open' as const,
      origin: null,
      human_wait: { reason: 'a captcha', expect: null, requested_at: iso(NOW - 60_000), deadline: iso(NOW + 600_000) },
      controller,
      open_to_members: false,
      device: null,
      presets: [],
      you: { email: 'ada@acme.test', can_control: true, can_preempt: true, can_open: true, can_switch_device: false, ...you },
    },
  }
}
const failedToday = task('task_fail1', 'FAILED', { metadata: { title: 'Port the parser' }, last_error: 'tests failed', updated_at: iso(NOW - 120_000) })
const running = task('task_run1', 'RUNNING', { metadata: { title: 'Write the docs' } })

const approval = {
  approval_id: 'appr_1',
  kind: 'schedule_firing',
  subject: {},
  summary: 'Run the nightly sweep',
  state: 'pending',
  requested_at: iso(NOW - 600_000),
  expires_at: iso(NOW + 5 * 3_600_000),
}

function person(email: string, over: Record<string, unknown>) {
  return {
    email,
    display_name: null,
    teams: [],
    github: 'none',
    workspace: { workspace_id: `u-${email.split('@')[0]}`, state: 'none', requested_at: null, held: null },
    claude_account: { lent_accounts: [] },
    loan_request: null,
    first_seen: null,
    last_seen: null,
    last_submitted: null,
    last_active: null,
    ...over,
  }
}

const platformPeople = {
  scope: 'platform',
  people: [
    person('bob@acme.test', { workspace: { workspace_id: 'u-bob', state: 'requested', requested_at: iso(NOW - 900_000), held: null } }),
    person('cy@acme.test', { workspace: { workspace_id: 'u-cy', state: 'ready', requested_at: null, held: null }, loan_request: { state: 'requested', requested_at: iso(NOW - 800_000), reason: 'a deadline' } }),
  ],
  count: 2,
  pending: 1,
  audit: [],
  lendable_accounts: [{ account_id: 'acct_1', label: 'pool one', display_name: null, owner_tenant: 'u-ada', state: 'AVAILABLE' }],
  lendable_error: null,
}

function profile(platformRole: string | null) {
  return {
    identity: { email: 'ada@acme.test', display_name: 'Ada', platform_role: platformRole, is_admin: platformRole !== null },
    workspaces: [{ tenant_id: 'u-ada', kind: 'user', display_name: 'Ada', is_acting: true, selectable: true, state: 'ready', role: { name: platformRole === null ? 'member' : 'admin' } }],
  }
}

function serve({ tasks, approvals, people, role }: { tasks: Task[]; approvals: unknown[]; people: Result<unknown>; role: string | null }) {
  api.loadTasks.mockResolvedValue(ok({ tasks }))
  api.loadApprovals.mockResolvedValue(ok({ approvals }))
  api.loadPeople.mockResolvedValue(people)
  api.loadProfile.mockResolvedValue(ok(profile(role)))
  api.loadStats.mockResolvedValue(ok({ tasks_by_state: { RUNNING: 1, QUEUED: 0, FAILED: 1 } }))
  api.loadOnboarding.mockResolvedValue({ status: 'empty', fetchedAt: NOW })
  api.loadLiveSession.mockResolvedValue({ status: 'empty', fetchedAt: NOW })
}

const total = () => screen.getByTestId('home-total').textContent
const item = (kind: string) => document.querySelector(`.hm-items [data-kind="${kind}"]`) as HTMLElement | null

beforeEach(() => {
  for (const f of [...Object.values(api), ...Object.values(handoff)]) f.mockReset()
})
afterEach(cleanup)

describe('Home opens on what waits on the signed-in person', () => {
  it('draws each kind of waiting item, and the total counts them all', async () => {
    serve({ tasks: [merge, parkedForMe, failedToday, running], approvals: [approval], people: ok(platformPeople), role: 'admin' })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('6 need you'))
    expect(item('question')?.textContent).toContain('Keep the old flag?')
    expect(item('approval')?.textContent).toContain('Run the nightly sweep')
    expect(item('workspace-request')?.textContent).toContain('bob@acme.test')
    expect(item('loan-request')?.textContent).toContain('a deadline')
    expect(item('handoff')?.textContent).toContain('Sign in to the vendor')
    expect(item('failed')?.textContent).toContain('tests failed')
    expect(api.loadTasks).toHaveBeenCalledWith(100, { submittedBy: 'me' })
  })

  it('answers an owner question in place: posted to the asking task, then the item leaves and the count drops', async () => {
    serve({ tasks: [merge, running], approvals: [], people: refused(403), role: null })
    api.answerQuestion.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('1 needs you'))
    const q = item('question') as HTMLElement
    const send = within(q).getByRole('button', { name: 'Send answer' })
    expect(send).toHaveProperty('disabled', true)
    fireEvent.change(within(q).getByLabelText('Your answer'), { target: { value: 'Yes, keep it' } })
    fireEvent.click(send)
    await waitFor(() => expect(item('question')).toBeNull())
    expect(api.answerQuestion).toHaveBeenCalledWith('task_review1', { question_index: 0, choice: null, text: 'Yes, keep it' })
    expect(total()).toBe('0 need you')
    expect(screen.getByRole('status').textContent).toContain('Answer sent to Merge the login fix')
  })

  it('a refused answer stays, with the refusal drawn', async () => {
    serve({ tasks: [merge], approvals: [], people: refused(403), role: null })
    api.answerQuestion.mockResolvedValue(refused(403))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(item('question')).not.toBeNull())
    fireEvent.change(screen.getByLabelText('Your answer'), { target: { value: 'no' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(screen.getByText(/The answer was not stored/)).toBeTruthy())
    expect(item('question')).not.toBeNull()
  })

  it('approves with the digest the detail read serves, and declines only with a reason', async () => {
    serve({ tasks: [], approvals: [approval, { ...approval, approval_id: 'appr_2', summary: 'Open the PR' }], people: refused(403), role: null })
    api.loadApproval.mockResolvedValue(ok({ approval: { digest: 'd-1' } }))
    api.approveApproval.mockResolvedValue(ok({}))
    api.rejectApproval.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('2 need you'))
    const rows = () => [...document.querySelectorAll('.hm-items [data-kind="approval"]')] as HTMLElement[]
    const firstRow = (): HTMLElement => {
      const r = rows()[0]
      if (r === undefined) throw new Error('no approval row')
      return r
    }
    fireEvent.click(within(firstRow()).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(api.approveApproval).toHaveBeenCalledWith('appr_1', { digest: 'd-1' })
    fireEvent.click(within(firstRow()).getByRole('button', { name: 'Decline…' }))
    const decline = within(firstRow()).getByRole('button', { name: 'Decline' })
    expect(decline).toHaveProperty('disabled', true)
    fireEvent.change(within(firstRow()).getByLabelText('Reason, required'), { target: { value: 'not tonight' } })
    fireEvent.click(decline)
    await waitFor(() => expect(rows()).toHaveLength(0))
    expect(api.rejectApproval).toHaveBeenCalledWith('appr_2', { reason: 'not tonight' })
    expect(total()).toBe('0 need you')
  })

  it("decides a workspace request (approve, or deny with a reason) and a Claude account loan (lend, or decline)", async () => {
    serve({ tasks: [], approvals: [], people: ok(platformPeople), role: 'admin' })
    api.approveWorkspace.mockResolvedValue(ok({}))
    api.setWorkspaceLoan.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('2 need you'))
    fireEvent.click(within(item('workspace-request') as HTMLElement).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(item('workspace-request')).toBeNull())
    expect(api.approveWorkspace).toHaveBeenCalledWith('u-bob')
    const loan = item('loan-request') as HTMLElement
    fireEvent.click(within(loan).getByRole('button', { name: 'Lend…' }))
    fireEvent.change(within(loan).getByLabelText('Account to lend to cy@acme.test'), { target: { value: 'acct_1' } })
    fireEvent.click(within(loan).getByRole('button', { name: 'Lend…' }))
    expect(within(loan).getByText(/Lend pool one \(u-ada\) to cy@acme.test\?/)).toBeTruthy()
    fireEvent.click(within(loan).getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(api.setWorkspaceLoan).toHaveBeenCalledWith('u-cy', 'acct_1', true))
    await waitFor(() => expect(item('loan-request')).toBeNull())
    expect(total()).toBe('0 need you')
  })

  it('denies a workspace request only with a reason, and declines a loan', async () => {
    serve({ tasks: [], approvals: [], people: ok(platformPeople), role: 'admin' })
    api.denyWorkspace.mockResolvedValue(ok({}))
    api.declineLoanRequest.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(item('workspace-request')).not.toBeNull())
    const ws = item('workspace-request') as HTMLElement
    fireEvent.click(within(ws).getByRole('button', { name: 'Deny…' }))
    expect(within(ws).getByRole('button', { name: 'Deny' })).toHaveProperty('disabled', true)
    fireEvent.change(within(ws).getByLabelText('Reason, shown to bob@acme.test'), { target: { value: 'use the team one' } })
    fireEvent.click(within(ws).getByRole('button', { name: 'Deny' }))
    await waitFor(() => expect(api.denyWorkspace).toHaveBeenCalledWith('u-bob', 'use the team one'))
    const loan = item('loan-request') as HTMLElement
    fireEvent.click(within(loan).getByRole('button', { name: 'Decline…' }))
    fireEvent.click(within(loan).getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(api.declineLoanRequest).toHaveBeenCalledWith('u-cy', ''))
    await waitFor(() => expect(total()).toBe('0 need you'))
  })

  it("an agent parked for the person at a browser offers I'm ready, which calls human-ready", async () => {
    serve({ tasks: [parkedForMe], approvals: [], people: refused(403), role: null })
    handoff.humanReady.mockResolvedValue(ok({}))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(item('handoff')).not.toBeNull())
    fireEvent.click(within(item('handoff') as HTMLElement).getByRole('button', { name: "I'm ready" }))
    await waitFor(() => expect(item('handoff')).toBeNull())
    expect(handoff.humanReady).toHaveBeenCalledWith('task_browser1')
  })

  it('a held hand-off whose open session the person may control offers Take control, which calls the live take', async () => {
    serve({ tasks: [heldForMe], approvals: [], people: refused(403), role: null })
    api.loadLiveSession.mockResolvedValue(ok(liveRead({})))
    api.takeLiveControl.mockResolvedValue(ok(liveRead({}).session))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(within(item('handoff') as HTMLElement).queryByRole('button', { name: 'Take control' })).not.toBeNull())
    expect(api.loadLiveSession).toHaveBeenCalledWith('task_held1')
    fireEvent.click(within(item('handoff') as HTMLElement).getByRole('button', { name: 'Take control' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Take control' }))
    await waitFor(() => expect(api.takeLiveControl).toHaveBeenCalledWith('task_held1', {}))
  })

  it.each([
    ['the session says the person may not control it', { can_control: false }, null],
    ['the person already controls it', {}, { email: 'ada@acme.test', since: iso(NOW - 30_000) }],
  ])('a held hand-off draws no Take control when %s', async (_why, you, controller) => {
    serve({ tasks: [heldForMe], approvals: [], people: refused(403), role: null })
    api.loadLiveSession.mockResolvedValue(ok(liveRead(you, controller)))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(api.loadLiveSession).toHaveBeenCalledWith('task_held1'))
    await waitFor(() => expect(item('handoff')).not.toBeNull())
    // The session read has answered and been drawn before the row is read.
    await new Promise((r) => setTimeout(r, 0))
    expect(within(item('handoff') as HTMLElement).queryByRole('button', { name: /Take control/ })).toBeNull()
    expect(api.takeLiveControl).not.toHaveBeenCalled()
  })

  it('a failed run of theirs today opens its agent page', async () => {
    serve({ tasks: [failedToday], approvals: [], people: refused(403), role: null })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(item('failed')).not.toBeNull())
    const open = within(item('failed') as HTMLElement).getByRole('link', { name: 'Open' })
    expect(open.getAttribute('href')).toContain('task_fail1')
  })

  it('a member sees no Requests at all: the people read refuses them, and that is not a gap', async () => {
    serve({ tasks: [running], approvals: [], people: refused(403), role: null })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('0 need you'))
    expect(screen.queryByText('Requests')).toBeNull()
    expect(screen.getByText('Ada · member of your own workspace')).toBeTruthy()
    expect(screen.getByText('Write the docs')).toBeTruthy()
  })

  it("names a group workspace by the name the profile serves, never its group email", async () => {
    serve({ tasks: [running], approvals: [], people: refused(403), role: null })
    const named = {
      ...profile(null),
      workspaces: [
        { tenant_id: 'u-ada', kind: 'user', display_name: 'Ada', is_acting: false, selectable: true, state: 'ready', role: { name: 'member' } },
        // `GET /v1/profile` serves the workspace record's name, the group email beside it as `principal`.
        { tenant_id: 'eng', kind: 'group', display_name: 'Engineering', principal: 'eng@acme.test', is_acting: true, selectable: true, state: null, role: { name: 'member' } },
      ],
    }
    api.loadProfile.mockResolvedValue(ok(named))
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(screen.getByText('Ada · member of Engineering')).toBeTruthy())
    const rows = [...document.querySelectorAll('.hm-wslist li')].map((li) => li.textContent ?? '')
    expect(rows.some((t) => t.startsWith('Engineering'))).toBe(true)
    expect(document.body.textContent).not.toContain('eng@acme.test')
  })

  it('the empty state says what it checked', async () => {
    serve({ tasks: [], approvals: [], people: refused(403), role: null })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(screen.getByTestId('home-clear').textContent).toContain('Nothing waits on you'))
    expect(screen.getByText('Nothing running.')).toBeTruthy()
  })

  it('a count the console cannot know reads not known, never 0', async () => {
    serve({ tasks: [], approvals: [], people: refused(403), role: null })
    api.loadTasks.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 500, code: null, message: 'down' } })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(api.loadApprovals).toHaveBeenCalled())
    await waitFor(() => expect(total()).toBe('— need you'))
    expect(screen.queryByTestId('home-clear')).toBeNull()
  })

  it('an await-input park is not a hand-off: it waits in Your work on your input', async () => {
    const awaiting = task('task_await1', 'PARKED', { park_reason: 'AWAITING_INPUT', metadata: { title: 'Pick a release name' } })
    expect(waitWhy(awaiting)).toBe('your input')
    serve({ tasks: [awaiting], approvals: [], people: refused(403), role: null })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(total()).toBe('0 need you'))
    expect(item('handoff')).toBeNull()
    expect(document.getElementById('home-work')?.textContent).toContain('your input')
  })

  it('a person with no workspace yet gets the starter guide in one press, as a real button that opens Start here', async () => {
    serve({ tasks: [], approvals: [], people: refused(403), role: null })
    api.loadProfile.mockResolvedValue(ok({ ...profile(null), workspaces: [] }))
    window.history.replaceState(null, '', '/')
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(screen.getByText(/^No workspace yet\./)).toBeTruthy())
    // MUTATION: drop the button from the no-workspace line -> this fails.
    const guide = screen.getByRole('link', { name: 'Read the starter guide' })
    // A real button in the theme's style, not a text link.
    expect(guide.className).toContain('c-btn')
    expect(guide.getAttribute('href')).toBe('/guide/start-here')
    fireEvent.click(guide)
    expect(window.location.pathname).toBe('/guide/start-here')
    window.history.replaceState(null, '', '/')
  })

  it('a person with a workspace is not offered the starter guide', async () => {
    serve({ tasks: [running], approvals: [], people: refused(403), role: null })
    render(<HomeScreen now={NOW} />)
    await waitFor(() => expect(screen.getByText('Ada · member of your own workspace')).toBeTruthy())
    expect(screen.queryByText(/^No workspace yet\./)).toBeNull()
    expect(screen.queryByRole('link', { name: 'Read the starter guide' })).toBeNull()
  })

  it('reads owner questions from the merge park only', () => {
    expect(ownerQuestionsOf([merge, parkedForMe, running]).map((q) => q.key)).toEqual(['task_review1:0'])
  })
})
